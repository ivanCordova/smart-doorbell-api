# Smart Doorbell

Infraestructura unificada de un videoportero inteligente para **ESP32-CAM**. Este repositorio agrupa los servicios de visión por computadora y de voz, y está preparado para incorporar nuevos servicios (por ejemplo, recepción de audio) bajo `services/`.

## Servicios

| Servicio | Contenedor | Puerto | Descripción |
|---|---|---|---|
| `services/api` | `doorbell_api` | `8000` | Gateway de video: recibe el stream MJPEG, detecta/reconoce rostros, registra eventos y expone el panel web. |
| `services/voice` | `doorbell_voice` | `8001` | Servicio de voz: wake word, reconocimiento de comandos con Vosk y respuestas por TTS (Piper neuronal, respaldo espeak). |

Flujo de integración:

```
ESP32-CAM --MJPEG--> doorbell-api --HTTP--> doorbell-voice
                          ^                      |
                          +---- voice-command ---+
```

Cuando `doorbell-api` detecta una persona, notifica a `doorbell-voice` en `/api/voice/announce`. El servicio de voz anuncia al visitante, escucha un comando y devuelve la orden (`OPEN`, `REJECT`, `CHECK`) a `/api/system/voice-command`.

## Características

- Stream en vivo del ESP32-CAM retransmitido al navegador en formato MJPEG.
- Detección de rostros con `face_detection_yunet.onnx`.
- Reconocimiento facial con `face_recognition_sface.onnx` (similitud coseno, umbral `0.363`).
- Registro de personas con foto subida o capturada desde el stream.
- Historial de eventos con la imagen anotada.
- Wake word y comandos de voz en español con Vosk.
- Anuncios y respuestas por TTS neuronal con Piper (respaldo espeak).
- Persistencia simple: metadatos en `services/api/known_faces/persons.json` y capturas en `services/api/captures/`.

## Estructura del repositorio

```
.
├── docker-compose.yml          # Compose único para todos los servicios
├── .env.example                # Variables de entorno de ejemplo
├── services/
│   ├── api/
│   │   ├── app/main.py
│   │   ├── templates/
│   │   ├── models/             # Modelos ONNX (no se versionan)
│   │   ├── captures/           # Capturas de eventos (no se versionan)
│   │   ├── known_faces/        # Rostros registrados (no se versionan)
│   │   ├── Dockerfile
│   │   ├── .dockerignore
│   │   └── requirements.txt
│   └── voice/
│       ├── app/voice_service.py
│       ├── Dockerfile
│       ├── .dockerignore
│       └── requirements.txt
└── services/audio/             # (futuro)
```

## Requisitos

- Docker y Docker Compose v2.
- Stream MJPEG accesible desde la cámara, por ejemplo `http://192.168.68.108/mjpeg`.
- Raspberry Pi (arm64) o Linux para el servicio de voz con audio real.
- Acceso a `/dev/snd` y pertenencia al grupo `audio` del host.

Las imágenes descargan automáticamente los modelos durante el build:

- API: modelos ONNX de OpenCV Zoo (`YuNet` + `SFace`) si no están en `services/api/models/`.
- Voz: `vosk-model-small-es-0.42` desde `alphacephei.com`.

## Configuración

Copia el archivo de ejemplo y ajusta los valores:

```bash
cp .env.example .env
```

| Variable | Descripción | Valor por defecto |
|---|---|---|
| `ESP32_STREAM_URL` | URL del stream MJPEG del ESP32-CAM | `http://192.168.68.108/mjpeg` |
| `AUDIO_GID` | GID del grupo `audio` del host | `29` |

Verifica el GID del grupo de audio en el host:

```bash
getent group audio
```

## Ejecución con Docker

```bash
docker compose up -d --build
```

- Panel web: <http://localhost:8000>
- Salud del API: <http://localhost:8000/api/status>
- Salud de voz: <http://localhost:8001/health>

Comandos útiles:

```bash
docker compose ps
docker compose logs -f doorbell-api doorbell-voice
docker compose down
```

## Audio en Raspberry Pi (Bluetooth / PipeWire)

En Linux, una bocina Bluetooth **no aparece como tarjeta ALSA**: la gestiona PipeWire/PulseAudio. Por eso el contenedor de voz se conecta al servidor de audio del usuario en lugar de usar únicamente `/dev/snd`.

El compose de `doorbell-voice`:

- Monta el socket `/run/user/<UID>/pulse/native` y el cookie `~/.config/pulse/cookie`.
- Define `PULSE_SERVER`, `PULSE_COOKIE` y `MIC_ENABLED`.
- Instala `libasound2-plugins` y configura `/etc/asound.conf` para que `aplay` (usado por `pyttsx3`) salga por PipeWire.
- Sigue el **sink predeterminado** de PipeWire. Si la bocina Bluetooth está conectada y seleccionada, el audio sale por ella.

Variables relevantes en `.env`:

```env
PULSE_UID=1000
PULSE_COOKIE_PATH=/home/ivan/.config/pulse/cookie
MIC_ENABLED=false
```

Comprobaciones útiles en el host:

```bash
id -u
wpctl status
wpctl inspect @DEFAULT_AUDIO_SINK@ | grep node.name
ls -l /run/user/1000/pulse/native ~/.config/pulse/cookie
```

Si el Pi arranca sin iniciar sesión gráfica, habilita la sesión persistente del usuario para que PipeWire exista siempre:

```bash
sudo loginctl enable-linger ivan
```

Con `MIC_ENABLED=false` el servicio **solo reproduce anuncios**: no escucha wake word ni comandos. Para habilitar la escucha, usa un micrófono adecuado y define `MIC_ENABLED=true` (y opcionalmente `AUDIO_INPUT_DEVICE`). Usar el micrófono de la misma bocina Bluetooth puede provocar eco.

Si no hay salida de audio disponible, el servicio arranca igualmente en **modo log** (`audio_available: false`) y no tumba el contenedor.

## TTS neuronal con Piper

El servicio usa **Piper** para sintetizar voz de forma local y natural. La voz por defecto es `es_MX-claude-high` (español de México, 22.05 kHz, licencia Apache-2.0).

- El modelo se descarga durante el build y queda en `/code/models/piper/`.
- Piper carga el modelo una sola vez al arrancar y sintetiza cada frase a un WAV temporal que se reproduce con `aplay` → PipeWire.
- Si Piper no está disponible, el servicio usa `espeak-ng` como respaldo (voz `roa/es-419` por defecto).

Variables relevantes:

```env
TTS_ENGINE=piper          # piper | espeak | auto
PIPER_VOICE=es_MX-claude-high
PIPER_LENGTH_SCALE=1.0    # menor = más rápido, mayor = más pausado
PIPER_VOLUME=1.0
ESPEAK_VOICE=roa/es-419   # Latinoamérica; usa roa/es para España
```

Para cambiar de voz, edita `PIPER_VOICE` en `.env` y reconstruye:

```bash
docker compose build doorbell-voice
docker compose up -d doorbell-voice
```

Voces disponibles (muestras en <https://rhasspy.github.io/piper-samples/>):

| Voz | Acento | Calidad | Licencia |
|---|---|---|---|
| `es_MX-claude-high` | México | high | Apache-2.0 |
| `es_ES-davefx-medium` | España | medium | CC0 |
| `es_ES-sharvard-medium` | España (2 voces) | medium | CC BY 3.0 |
| `es_AR-daniela-high` | Argentina | high | CC BY-SA 4.0 |

`piper-tts` se distribuye bajo GPL-3.0; revisa la licencia si vas a redistribuir el proyecto.

Prueba de audio:

```bash
docker exec doorbell_voice curl -s -X POST http://localhost:8001/api/voice/test \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hola, soy el portero. ¿Deseas abrir la puerta?"}'
```

El endpoint `/health` indica el motor activo:

```json
{
  "audio_available": true,
  "tts_engine": "piper",
  "piper_voice": "es_MX-claude-high"
}
```

## Agregar un nuevo servicio (por ejemplo, audio)

1. Crea `services/audio/` con su `app/`, `Dockerfile`, `requirements.txt` y `.dockerignore`.
2. Añade el bloque del servicio en el `docker-compose.yml` de la raíz.
3. Usa los nombres de servicio (`doorbell-api`, `doorbell-voice`, `doorbell-audio`) como hostnames dentro de la red de Compose.
4. Documenta sus variables en `.env.example`.

## Endpoints del API

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/` | Panel principal |
| `GET` | `/register-view` | Formulario de registro de personas |
| `GET` | `/video_feed` | Stream MJPEG retransmitido |
| `GET` | `/api/system/status` | Estado, historial y URL actual |
| `POST` | `/api/system/stream-url` | Cambiar la URL del stream |
| `POST` | `/api/system/toggle-ai` | Activar/desactivar la IA |
| `GET` | `/api/persons` | Listar personas registradas |
| `POST` | `/api/persons/register` | Registrar persona |
| `DELETE` | `/api/persons/{person_id}` | Eliminar persona |
| `GET` | `/api/status` | Healthcheck y resumen del sistema |
| `POST` | `/api/system/voice-command` | Recibir órdenes del servicio de voz |

## Endpoints del servicio de voz

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/health` | Estado del servicio y disponibilidad de audio |
| `POST` | `/api/voice/announce` | Anunciar visitante y abrir ventana de escucha |
| `POST` | `/api/voice/test` | Reproducir una frase de prueba por la salida de audio |

## Ejecución local sin Docker

API:

```bash
cd services/api
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export ESP32_STREAM_URL="http://192.168.68.108/mjpeg"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Voz:

```bash
cd services/voice
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export API_SERVICE_URL="http://localhost:8000"
uvicorn app.voice_service:app --host 0.0.0.0 --port 8001
```

Para la ejecución local del servicio de voz necesitas descargar el modelo Vosk en `services/voice/models/`:

```bash
curl -fsSL -o /tmp/vosk.zip https://alphacephei.com/vosk/models/vosk-model-small-es-0.42.zip
unzip -q /tmp/vosk.zip -d services/voice/models
```

## Notas

- El sistema compara cada rostro detectado contra todos los rostros registrados y elige la mejor coincidencia sobre el umbral.
- Si no hay coincidencia, la persona se marca como **Desconocido** y la captura se resalta en rojo.
- Las capturas antiguas se eliminan automáticamente al superar `MAX_CAPTURES`.
- Las notificaciones al servicio de voz se envían en un hilo aparte para no bloquear el bucle de captura.
