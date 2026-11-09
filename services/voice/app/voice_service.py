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
tts_lock = threading.Lock()

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
rec_wake = KaldiRecognizer(vosk_model, AUDIO_RATE, '["asistente", "timbre", "[unk]"]')
rec_command = KaldiRecognizer(vosk_model, AUDIO_RATE)

audio_queue = queue.Queue()
voice_state = {
    "status": "ESPERA_WAKEWORD",  # ESPERA_WAKEWORD, ANUNCIANDO, ESCUCHA_COMANDO
    "timeout_comando": 0.0
}

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


def hablar(texto: str):
    """Reproducción síncrona controlada."""
    with tts_lock:
        voice_state["status"] = "ANUNCIANDO"
        inicio = time.time()

        if piper_voice is not None:
            try:
                _hablar_piper(texto)
                print(f"🔊 [Piper]: {texto} ({time.time() - inicio:.2f}s)")
                voice_state["status"] = "ESPERA_WAKEWORD"
                return
            except Exception as e:
                print(f"⚠️ Error Piper: {e}. Usando espeak de respaldo.")

        if _hablar_espeak(texto):
            print(f"🔊 [Bocina]: {texto} ({time.time() - inicio:.2f}s)")
        else:
            print(f"🔊 [Simulación Bocina]: '{texto}'")

        voice_state["status"] = "ESPERA_WAKEWORD"

def callback_audio(indata, frames, time_info, status):
    if status:
        pass
    audio_queue.put(bytes(indata))

def interpretar_intencion(texto: str) -> dict:
    limpio = re.sub(r'[^a-záéíóúñ0-9\s]', '', texto.lower()).strip()
    print(f"🧠 [PLN]: Interpretando '{limpio}'")

    if re.search(r'\b(abre|abrir|ábrela|abreme|adelante|pasa)\b', limpio):
        return {"intent": "ABRIR_PUERTA", "tts": "Abriendo la puerta enseguida.", "cmd": "OPEN"}
    elif re.search(r'\b(no|ignora|ignorar|dile que no|rechazar|cerrar)\b', limpio):
        return {"intent": "RECHAZAR", "tts": "Entendido, no abriré.", "cmd": "REJECT"}
    elif re.search(r'\b(quién|quien|quiénes|quienes)\b', limpio):
        return {"intent": "CONSULTA", "tts": "Verificando el registro de la cámara.", "cmd": "CHECK"}
    else:
        return {"intent": "DESCONOCIDO", "tts": "No logré entender la instrucción.", "cmd": "NONE"}

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

    print("🎤 [Voice Service]: Escuchando activación por palabra clave ('asistente')...")
    with sd.RawInputStream(samplerate=AUDIO_RATE, blocksize=8000, dtype='int16',
                           channels=AUDIO_CHANNELS, device=AUDIO_INPUT_DEVICE,
                           callback=callback_audio):
        while True:
            data = audio_queue.get()

            # Estado 1: Palabra clave
            if voice_state["status"] == "ESPERA_WAKEWORD":
                if rec_wake.AcceptWaveform(data):
                    res = json.loads(rec_wake.Result())
                    txt = res.get("text", "")
                    if any(w in txt for w in ["asistente", "timbre"]):
                        print(f"🔔 Wake word detectada: {txt}")
                        hablar("¿Te escucho?")
                        while not audio_queue.empty():
                            audio_queue.get_nowait()
                        rec_command.Reset()
                        voice_state["status"] = "ESCUCHA_COMANDO"
                        voice_state["timeout_comando"] = time.time() + 6.0

            # Estado 2: Escuchando comando
            elif voice_state["status"] == "ESCUCHA_COMANDO":
                if time.time() > voice_state["timeout_comando"]:
                    print("⏱️ Tiempo agotado.")
                    hablar("Cancelado.")
                    rec_wake.Reset()
                    voice_state["status"] = "ESPERA_WAKEWORD"
                    continue

                if rec_command.AcceptWaveform(data):
                    res = json.loads(rec_command.Result())
                    comando = res.get("text", "").strip()
                    if comando:
                        resultado = interpretar_intencion(comando)
                        hablar(resultado["tts"])
                        if resultado["cmd"] != "NONE":
                            notificar_a_vision(resultado["cmd"])
                        rec_wake.Reset()
                        voice_state["status"] = "ESPERA_WAKEWORD"

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

    return {
        "status": "online",
        "audio_available": AUDIO_OUTPUT_AVAILABLE,
        "audio_output_available": AUDIO_OUTPUT_AVAILABLE,
        "audio_input_available": AUDIO_INPUT_AVAILABLE,
        "mic_enabled": MIC_ENABLED,
        "tts_engine": motor_tts,
        "piper_voice": PIPER_VOICE if piper_voice is not None else None,
        "voice_status": voice_state["status"],
    }

@app.post("/api/voice/test")
def test_audio(payload: TestPayload, background_tasks: BackgroundTasks):
    """Reproduce una frase de prueba para verificar la salida de audio."""
    background_tasks.add_task(hablar, payload.text)
    return {"status": "ok", "message": "Prueba de audio programada"}

@app.post("/api/voice/announce")
def announce_visitor(payload: AlertPayload, background_tasks: BackgroundTasks):
    """Llamado por doorbell-api cuando detecta una persona."""
    if payload.name != "Desconocido":
        frase = f"{payload.name} está en la puerta. ¿Deseas abrir?"
    else:
        frase = "Hay una persona no identificada en la puerta. ¿Qué deseas hacer?"

    def flujo_anuncio():
        hablar(frase)
        if not (MIC_ENABLED and AUDIO_INPUT_AVAILABLE):
            voice_state["status"] = "ESPERA_WAKEWORD"
            return

        # Tras anunciarlo, abre directamente la ventana de escucha sin pedir wake word
        while not audio_queue.empty():
            audio_queue.get_nowait()
        rec_command.Reset()
        voice_state["status"] = "ESCUCHA_COMANDO"
        voice_state["timeout_comando"] = time.time() + 7.0

    background_tasks.add_task(flujo_anuncio)
    return {"status": "ok", "message": "Anuncio programado"}
