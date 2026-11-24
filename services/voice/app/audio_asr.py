"""Capa acústica / ASR: captura con sounddevice y decodificación Vosk (WFST).

Toda la configuración de audio y la cola de chunks viven aquí para mantener
desacoplado el bucle de estados del servicio de voz.
"""

import json
import queue
from pathlib import Path
from typing import Optional

import sounddevice as sd
from vosk import KaldiRecognizer, Model

AUDIO_RATE = 16000
AUDIO_CHANNELS = 1
BLOCKSIZE = 8000

BASE_DIR = Path(__file__).resolve().parent.parent
MODEL_PATH = str(BASE_DIR / "models" / "vosk-model-small-es-0.42")


class ReconocedorVosk:
    """Modelo, recognizers y cola de audio compartidos por el bucle de voz."""

    def __init__(
        self,
        wake_word: str = "portero",
        device: Optional[str] = None,
        model_path: Optional[str] = None,
    ) -> None:
        self.wake_word = (wake_word or "").strip().lower() or "portero"
        self.device = device
        self.modelo = Model(model_path or MODEL_PATH)
        self.rec_wake = KaldiRecognizer(
            self.modelo,
            AUDIO_RATE,
            json.dumps([self.wake_word, "[unk]"]),
        )
        self.rec_command = KaldiRecognizer(self.modelo, AUDIO_RATE)
        self.audio_queue: "queue.Queue[bytes]" = queue.Queue()

    def callback(self, indata, frames, time_info, status) -> None:
        """Callback de sounddevice: solo encola bytes, sin procesar PLN."""
        self.audio_queue.put(bytes(indata))

    def stream(self):
        """Context manager con el formato exigido por Vosk (16 kHz, mono, int16)."""
        return sd.RawInputStream(
            samplerate=AUDIO_RATE,
            blocksize=BLOCKSIZE,
            dtype="int16",
            channels=AUDIO_CHANNELS,
            device=self.device,
            callback=self.callback,
        )

    def limpiar(self) -> None:
        """Descarta el eco en cola y reinicia ambos recognizers tras TTS."""
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break
        self.rec_wake.Reset()
        self.rec_command.Reset()
