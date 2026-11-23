import os
import json
import re
import queue
import time
import threading
import subprocess
import tempfile
import wave
from pathlib import Path
from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import sounddevice as sd
from vosk import Model, KaldiRecognizer
import pyttsx3
import urllib.request

try:
    from piper import PiperVoice, SynthesisConfig
    PIPER_IMPORT_AVAILABLE = True
except Exception as e:
    PiperVoice = None
    SynthesisConfig = None
    PIPER_IMPORT_AVAILABLE = False
    print(f"⚠️ [Voice Service]: piper-tts no está instalado ({e}).")

app = FastAPI(title="Doorbell Voice Service")

# ---------------------------------------------------------
# 1. Configuración de Modelos y Hardware
# ---------------------------------------------------------
AUDIO_RATE = 16000
AUDIO_CHANNELS = 1
BASE_DIR = Path(__file__).resolve().parent.parent
MODEL_PATH = str(BASE_DIR / "models" / "vosk-model-small-es-0.42")
API_SERVICE_URL = os.getenv("API_SERVICE_URL", "http://doorbell-api:8000")
NLU_SERVICE_URL = os.getenv("NLU_SERVICE_URL", "http://doorbell-nlu:8010").rstrip("/")
NLU_TIMEOUT = float(os.getenv("NLU_TIMEOUT", "2.0"))

WAKE_WORD = os.getenv("WAKE_WORD", "portero").strip().lower() or "portero"
WAKE_TIMEOUT = float(os.getenv("WAKE_TIMEOUT", "6.0"))
WAKE_COOLDOWN = float(os.getenv("WAKE_COOLDOWN", "0.8"))
# Solo absorbe reintentos HTTP duplicados. La visita y su recordatorio los decide doorbell-api.
VOICE_DEDUPE_S = float(os.getenv("VOICE_DEDUPE_S", "3.0"))

MIC_ENABLED = os.getenv("MIC_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
AUDIO_INPUT_DEVICE = os.getenv("AUDIO_INPUT_DEVICE") or None

TTS_ENGINE = os.getenv("TTS_ENGINE", "piper").strip().lower()
PIPER_VOICE = os.getenv("PIPER_VOICE", "es_MX-claude-high")
PIPER_VOICE_PATH = os.getenv(
    "PIPER_VOICE_PATH",
    str(BASE_DIR / "models" / "piper" / f"{PIPER_VOICE}.onnx"),
)
PIPER_LENGTH_SCALE = float(os.getenv("PIPER_LENGTH_SCALE", "1.0"))
PIPER_VOLUME = float(os.getenv("PIPER_VOLUME", "1.0"))
ESPEAK_VOICE = os.getenv("ESPEAK_VOICE", "roa/es-419")

AUDIO_INPUT_AVAILABLE = False
AUDIO_OUTPUT_AVAILABLE = False
tts_engine = None
piper_voice = None
# tts_lock serializa la síntesis (puede tardar ~6 s). state_lock protege el
# estado de la FSM para que las decisiones HTTP no esperen a que termine TTS.
tts_lock = threading.RLock()
state_lock = threading.RLock()

print(f"🔈 [Voice Service]: PULSE_SERVER={os.getenv('PULSE_SERVER', '(no definido)')}")

# Carga del modelo Piper (una sola vez)
if TTS_ENGINE in ("piper", "auto") and PIPER_IMPORT_AVAILABLE:
    if os.path.exists(PIPER_VOICE_PATH):
        try:
            inicio_piper = time.time()
            piper_voice = PiperVoice.load(PIPER_VOICE_PATH)
            print(
                f"✅ [Voice Service]: Piper cargado ({PIPER_VOICE}, "
                f"{time.time() - inicio_piper:.2f}s)."
            )
        except Exception as e:
            print(f"⚠️ [Voice Service]: No se pudo cargar Piper ({e}).")
    else:
        print(f"⚠️ [Voice Service]: Modelo Piper no encontrado en {PIPER_VOICE_PATH}.")
elif TTS_ENGINE == "piper" and not PIPER_IMPORT_AVAILABLE:
    print("⚠️ [Voice Service]: TTS_ENGINE=piper pero piper-tts no está instalado.")

try:
    devices = sd.query_devices()
    AUDIO_INPUT_AVAILABLE = any(d["max_input_channels"] > 0 for d in devices)
    AUDIO_OUTPUT_AVAILABLE = any(d["max_output_channels"] > 0 for d in devices)

    if AUDIO_OUTPUT_AVAILABLE:
        tts_engine = pyttsx3.init()
        tts_engine.setProperty("rate", 160)
        try:
            tts_engine.setProperty("voice", ESPEAK_VOICE)
        except Exception as e:
            print(f"⚠️ [Voice Service]: Voz espeak '{ESPEAK_VOICE}' no disponible ({e}).")
        print("✅ [Voice Service]: Salida de audio vinculada.")
    else:
        print("⚠️ [Voice Service]: Sin salida de audio. Anuncios simulados por log.")

    if AUDIO_INPUT_AVAILABLE:
        print("🎤 [Voice Service]: Entrada de audio detectada.")
    else:
        print("🔇 [Voice Service]: Sin entrada de audio.")
except Exception as e:
    print(f"⚠️ [Voice Service]: Audio no disponible ({e}). Operando en modo log.")

# Carga de modelo acústico Vosk
print(f"Cargando Vosk desde {MODEL_PATH}...")
vosk_model = Model(MODEL_PATH)
rec_wake = KaldiRecognizer(vosk_model, AUDIO_RATE, json.dumps([WAKE_WORD, "[unk]"]))
rec_command = KaldiRecognizer(vosk_model, AUDIO_RATE)

audio_queue = queue.Queue()
voice_state = {
    "status": "ESPERA_WAKEWORD",  # ESPERA_WAKEWORD, ANUNCIANDO, ESCUCHA_COMANDO
    "timeout_comando": 0.0,
    "wake_cooldown_until": 0.0,
}
announce_state = {
    "ultimo_nombre": None,
    "ultimo_ts": 0.0,
    "motivo": None,
    "omitidos": 0,
}


def _set_voice_status(nuevo_estado: str):
    with state_lock:
        voice_state["status"] = nuevo_estado

# ---------------------------------------------------------
# 2. Utilidades de Audio y PLN
# ---------------------------------------------------------
def _hablar_piper(texto: str) -> None:
    """Sintetiza con Piper y reproduce el WAV por ALSA/PipeWire."""
    wav_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = tmp.name

        with wave.open(wav_path, "wb") as wav_file:
            piper_voice.synthesize_wav(
                texto,
                wav_file,
                syn_config=SynthesisConfig(
                    volume=PIPER_VOLUME,
                    length_scale=PIPER_LENGTH_SCALE,
                ),
            )

        subprocess.run(["aplay", "-q", wav_path], check=True)
    finally:
        if wav_path and os.path.exists(wav_path):
            os.remove(wav_path)


def _hablar_espeak(texto: str) -> bool:
    """Respaldo con espeak-ng vía pyttsx3."""
    if not (AUDIO_OUTPUT_AVAILABLE and tts_engine):
        return False
    try:
        tts_engine.say(texto)
        tts_engine.runAndWait()
        return True
    except Exception as e:
        print(f"⚠️ Error TTS espeak: {e}")
        return False


def _limpiar_audio_tras_tts():
    """Evita que el eco de la bocina dispare la palabra clave."""
    while not audio_queue.empty():
        try:
            audio_queue.get_nowait()
        except queue.Empty:
            break
    rec_wake.Reset()
    rec_command.Reset()
    with state_lock:
        voice_state["wake_cooldown_until"] = time.time() + WAKE_COOLDOWN


def _hablar_bajo_lock(texto: str):
    """Reproduce un texto. Debe llamarse con ``tts_lock`` ya adquirido."""
    _set_voice_status("ANUNCIANDO")
    inicio = time.time()

    if piper_voice is not None:
        try:
            _hablar_piper(texto)
            print(f"🔊 [Piper]: {texto} ({time.time() - inicio:.2f}s)")
            _limpiar_audio_tras_tts()
            _set_voice_status("ESPERA_WAKEWORD")
            return
        except Exception as e:
            print(f"⚠️ Error Piper: {e}. Usando espeak de respaldo.")

    if _hablar_espeak(texto):
        print(f"🔊 [Bocina]: {texto} ({time.time() - inicio:.2f}s)")
    else:
        print(f"🔊 [Simulación Bocina]: '{texto}'")

    _limpiar_audio_tras_tts()
    _set_voice_status("ESPERA_WAKEWORD")


def hablar(texto: str):
    """Reproducción síncrona controlada."""
    with tts_lock:
        _hablar_bajo_lock(texto)

def callback_audio(indata, frames, time_info, status):
    if status:
        pass
    audio_queue.put(bytes(indata))

def consultar_nlu(texto: str) -> dict:
    """Pide la intención al servicio doorbell-nlu."""
    fallback = {
        "intent": "DESCONOCIDO",
        "cmd": "NONE",
        "tts": "No pude procesar la orden en este momento.",
        "confidence": 0.0,
    }
    try:
        url = f"{NLU_SERVICE_URL}/api/nlu/interpret"
        payload = json.dumps({"text": texto}).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=NLU_TIMEOUT) as resp:
            datos = json.loads(resp.read().decode('utf-8'))
        return {
            "intent": datos.get("intent", "DESCONOCIDO"),
            "cmd": datos.get("cmd", "NONE"),
            "tts": datos.get("tts", fallback["tts"]),
            "confidence": float(datos.get("confidence", 0.0)),
        }
    except Exception as e:
        print(f"⚠️ [Voice Service] Error consultando doorbell-nlu: {e}")
        return fallback


def _sufijo_tras_wake(texto: str) -> str:
    coincidencia = re.search(rf"\b{re.escape(WAKE_WORD)}\b", texto, re.IGNORECASE)
    if not coincidencia:
        return ""
    return texto[coincidencia.end():].strip(" ,.;:¿?¡!")


def _procesar_comando(comando: str):
    resultado = consultar_nlu(comando)
    print(
        f"🧠 [Voice Service] '{comando}' -> {resultado['intent']} "
        f"({resultado['confidence']:.2f})"
    )
    hablar(resultado["tts"])
    if resultado["cmd"] != "NONE":
        notificar_a_vision(resultado["cmd"])
    rec_wake.Reset()
    _set_voice_status("ESPERA_WAKEWORD")

def notificar_a_vision(cmd: str):
    """Envía la orden detectada de vuelta al servicio de visión."""
    try:
        url = f"{API_SERVICE_URL}/api/system/voice-command"
        payload = json.dumps({"command": cmd}).encode('utf-8')
        req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=2) as resp:
            pass
    except Exception as e:
        print(f"⚠️ No se pudo notificar a doorbell-api: {e}")

# ---------------------------------------------------------
# 3. Bucle Principal de la FSM de Audio
# ---------------------------------------------------------
def background_voice_loop():
    if not MIC_ENABLED:
        print("🔇 [Voice Service]: Micrófono desactivado (MIC_ENABLED=false).")
        return

    if not AUDIO_INPUT_AVAILABLE:
        print("🔇 Bucle de micrófono detenido (sin entrada de audio).")
        return

    print(f"🎤 [Voice Service]: Escuchando activación por palabra clave ('{WAKE_WORD}')...")
    with sd.RawInputStream(samplerate=AUDIO_RATE, blocksize=8000, dtype='int16',
                           channels=AUDIO_CHANNELS, device=AUDIO_INPUT_DEVICE,
                           callback=callback_audio):
        while True:
            data = audio_queue.get()

            with state_lock:
                estado = voice_state["status"]
                cooldown = voice_state["wake_cooldown_until"]
                timeout_comando = voice_state["timeout_comando"]

            # Estado 1: Palabra clave
            if estado == "ESPERA_WAKEWORD":
                if time.time() < cooldown:
                    continue
                if rec_wake.AcceptWaveform(data):
                    res = json.loads(rec_wake.Result())
                    txt = res.get("text", "")
                    if re.search(rf"\b{re.escape(WAKE_WORD)}\b", txt, re.IGNORECASE):
                        print(f"🔔 Wake word detectada: {txt}")
                        hablar("¿Te escucho?")
                        sufijo = _sufijo_tras_wake(txt)
                        if sufijo:
                            print(f"🗣️ Orden en la misma frase: '{sufijo}'")
                            _procesar_comando(sufijo)
                        else:
                            rec_command.Reset()
                            _set_voice_status("ESCUCHA_COMANDO")
                            with state_lock:
                                voice_state["timeout_comando"] = time.time() + WAKE_TIMEOUT

            # Estado 2: Escuchando comando
            elif estado == "ESCUCHA_COMANDO":
                if time.time() > timeout_comando:
                    print("⏱️ Tiempo agotado.")
                    hablar("Cancelado.")
                    rec_wake.Reset()
                    _set_voice_status("ESPERA_WAKEWORD")
                    continue

                if rec_command.AcceptWaveform(data):
                    res = json.loads(rec_command.Result())
                    comando = res.get("text", "").strip()
                    if comando:
                        _procesar_comando(comando)

# ---------------------------------------------------------
# 4. Endpoints HTTP para ser llamado por doorbell-api
# ---------------------------------------------------------
class AlertPayload(BaseModel):
    name: str

class TestPayload(BaseModel):
    text: str = "Prueba de audio en la bocina."

@app.on_event("startup")
async def startup():
    threading.Thread(target=background_voice_loop, daemon=True).start()

@app.get("/health")
def health():
    if piper_voice is not None:
        motor_tts = "piper"
    elif tts_engine is not None:
        motor_tts = "espeak"
    else:
        motor_tts = "none"

    with state_lock:
        ultimo_ts = announce_state["ultimo_ts"]
        return {
            "status": "online",
            "audio_available": AUDIO_OUTPUT_AVAILABLE,
            "audio_output_available": AUDIO_OUTPUT_AVAILABLE,
            "audio_input_available": AUDIO_INPUT_AVAILABLE,
            "mic_enabled": MIC_ENABLED,
            "tts_engine": motor_tts,
            "piper_voice": PIPER_VOICE if piper_voice is not None else None,
            "voice_status": voice_state["status"],
            "wake_word": WAKE_WORD,
            "nlu_service_url": NLU_SERVICE_URL,
            "ultimo_anuncio": announce_state["ultimo_nombre"],
            "ultimo_anuncio_hace_s": round(time.time() - ultimo_ts, 1) if ultimo_ts else None,
            "ultimo_motivo": announce_state["motivo"],
            "anuncios_omitidos": announce_state["omitidos"],
        }

@app.post("/api/voice/test")
def test_audio(payload: TestPayload, background_tasks: BackgroundTasks):
    """Reproduce una frase de prueba para verificar la salida de audio."""
    background_tasks.add_task(hablar, payload.text)
    return {"status": "ok", "message": "Prueba de audio programada"}

@app.post("/api/voice/announce")
def announce_visitor(payload: AlertPayload, background_tasks: BackgroundTasks):
    """Anuncia al visitante sin pisar una orden en curso.

    La decisión de *cuándo* anunciar (visita nueva o recordatorio) la toma
    doorbell-api. Aquí solo se protege el estado de audio y se absorben
    reintentos HTTP duplicados.
    """
    nombre = (payload.name or "Desconocido").strip() or "Desconocido"
    if nombre != "Desconocido":
        frase = f"{nombre} está en la puerta. Di la palabra clave si deseas dar una orden."
    else:
        frase = "Hay una persona no identificada en la puerta. Di la palabra clave para indicar qué hacer."

    ahora = time.time()
    with state_lock:
        estado = voice_state["status"]

        if estado in ("ANUNCIANDO", "ESCUCHA_COMANDO"):
            motivo = f"omitido_{estado.lower()}"
            announce_state["motivo"] = motivo
            announce_state["omitidos"] += 1
            print(f"🔕 [Voice Service]: Anuncio omitido ({motivo}): {nombre}")
            return {"status": "ok", "reason": motivo, "spoken": False}

        if (
            announce_state["ultimo_nombre"] == nombre
            and ahora - announce_state["ultimo_ts"] < VOICE_DEDUPE_S
        ):
            motivo = "omitido_duplicado"
            announce_state["motivo"] = motivo
            announce_state["omitidos"] += 1
            print(f"🔕 [Voice Service]: Anuncio omitido ({motivo}): {nombre}")
            return {"status": "ok", "reason": motivo, "spoken": False}

        announce_state["ultimo_nombre"] = nombre
        announce_state["ultimo_ts"] = ahora
        announce_state["motivo"] = "anunciado"
        # Reservamos el estado antes de devolver la respuesta HTTP para que
        # ningún chunk de audio pueda disparar la wake word por medio.
        voice_state["status"] = "ANUNCIANDO"
        motivo = "anunciado"

    def flujo_anuncio():
        with tts_lock:
            _hablar_bajo_lock(frase)

    background_tasks.add_task(flujo_anuncio)
    return {"status": "ok", "reason": motivo, "spoken": True}
