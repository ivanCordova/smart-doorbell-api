import os
import json
import re
import queue
import time
import threading
from pathlib import Path
from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import sounddevice as sd
from vosk import Model, KaldiRecognizer
import pyttsx3
import urllib.request

app = FastAPI(title="Doorbell Voice Service")

# ---------------------------------------------------------
# 1. Configuración de Modelos y Hardware
# ---------------------------------------------------------
AUDIO_RATE = 16000
AUDIO_CHANNELS = 1
BASE_DIR = Path(__file__).resolve().parent.parent
MODEL_PATH = str(BASE_DIR / "models" / "vosk-model-small-es-0.42")
API_SERVICE_URL = os.getenv("API_SERVICE_URL", "http://doorbell-api:8000")

AUDIO_AVAILABLE = False
tts_engine = None

try:
    if os.path.exists("/dev/snd"):
        devices = sd.query_devices()
        has_in = any(d['max_input_channels'] > 0 for d in devices)
        has_out = any(d['max_output_channels'] > 0 for d in devices)
        if has_in and has_out:
            tts_engine = pyttsx3.init()
            tts_engine.setProperty("rate", 165)
            AUDIO_AVAILABLE = True
            print("✅ [Voice Service]: Hardware de audio vinculado.")
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
def hablar(texto: str):
    """Reproducción síncrona controlada."""
    voice_state["status"] = "ANUNCIANDO"
    if AUDIO_AVAILABLE and tts_engine:
        try:
            print(f"🔊 [Bocina]: {texto}")
            tts_engine.say(texto)
            tts_engine.runAndWait()
        except Exception as e:
            print(f"⚠️ Error TTS: {e}")
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
    if not AUDIO_AVAILABLE:
        print("🔇 Bucle de micrófono detenido (sin hardware).")
        return

    print("🎤 [Voice Service]: Escuchando activación por palabra clave ('asistente')...")
    with sd.RawInputStream(samplerate=AUDIO_RATE, blocksize=8000, dtype='int16',
                           channels=AUDIO_CHANNELS, callback=callback_audio):
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

@app.on_event("startup")
async def startup():
    threading.Thread(target=background_voice_loop, daemon=True).start()

@app.get("/health")
def health():
    return {
        "status": "online",
        "audio_available": AUDIO_AVAILABLE,
        "voice_status": voice_state["status"],
    }

@app.post("/api/voice/announce")
def announce_visitor(payload: AlertPayload, background_tasks: BackgroundTasks):
    """Llamado por doorbell-api cuando detecta una persona."""
    if payload.name != "Desconocido":
        frase = f"{payload.name} está en la puerta. ¿Deseas abrir?"
    else:
        frase = "Hay una persona no identificada en la puerta. ¿Qué deseas hacer?"

    def flujo_anuncio():
        hablar(frase)
        # Tras anunciarlo, abre directamente la ventana de escucha sin pedir wake word
        while not audio_queue.empty():
            audio_queue.get_nowait()
        rec_command.Reset()
        voice_state["status"] = "ESCUCHA_COMANDO"
        voice_state["timeout_comando"] = time.time() + 7.0

    background_tasks.add_task(flujo_anuncio)
    return {"status": "ok", "message": "Anuncio programado"}
