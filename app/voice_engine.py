import os
import sounddevice as sd
import pyttsx3

AUDIO_AVAILABLE = False
tts_engine = None

def verificar_dispositivos_audio() -> bool:
    """Verifica si existen dispositivos de entrada (micrófono) y salida (bocina)."""
    try:
        devices = sd.query_devices()
        input_dev = any(d['max_input_channels'] > 0 for d in devices)
        output_dev = any(d['max_output_channels'] > 0 for d in devices)
        return input_dev and output_dev
    except Exception as e:
        print(f"⚠️ [Audio Hardware]: No se detectaron interfaces de sonido ({e}).")
        return False

# Inicialización segura
if os.path.exists("/dev/snd") and verificar_dispositivos_audio():
    try:
        tts_engine = pyttsx3.init()
        tts_engine.setProperty("rate", 165)
        AUDIO_AVAILABLE = True
        print("✅ [Audio Hardware]: Micrófono y bocina listos.")
    except Exception as e:
        print(f"⚠️ [Audio Hardware]: Fallo al inicializar TTS ({e}).")
        AUDIO_AVAILABLE = False
else:
    print("🔇 [Audio Hardware]: Sistema operando en modo silencioso (sin periféricos conectados).")


def hablar(texto: str):
    """Reproduce voz si hay hardware; de lo contrario, solo registra el texto."""
    if AUDIO_AVAILABLE and tts_engine is not None:
        try:
            print(f"🔊 [Bocina]: {texto}")
            tts_engine.say(texto)
            tts_engine.runAndWait()
        except Exception as e:
            print(f"⚠️ Error al reproducir audio: {e}")
    else:
        print(f"🔊 [Simulación Bocina]: '{texto}'")


def iniciar_asistente_voz(callback_accion=None):
    """Bucle seguro: no intenta abrir RawInputStream si no hay micrófono."""
    if not AUDIO_AVAILABLE:
        print("⚠️ [Asistente de Voz]: Deshabilitado por falta de dispositivos de audio.")
        return

    # Si hay hardware, continúa con el bucle normal de Vosk y sounddevice...