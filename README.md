# Smart Doorbell

Infraestructura unificada de un videoportero inteligente para **ESP32-CAM**. Este repositorio agrupa los servicios de visión por computadora y de voz, y está preparado para incorporar nuevos servicios (por ejemplo, recepción de audio) bajo `services/`.

## Servicios

| Servicio | Contenedor | Puerto | Descripción |
|---|---|---|---|
| `services/api` | `doorbell_api` | `8000` | Gateway de video: recibe el stream MJPEG, detecta/reconoce rostros, registra eventos y expone el panel web. |
| `services/voice` | `doorbell_voice` | `8001` | Servicio de voz: wake word, reconocimiento de comandos con Vosk y respuestas por TTS (espeak). |

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
- Anuncios y respuestas por TTS con espeak.
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

## Audio en Raspberry Pi

El servicio de voz necesita acceso al hardware de audio del host:

- El compose monta `/dev/snd:/dev/snd` y usa `privileged: true` para acceder a ALSA.
- `group_add` usa `AUDIO_GID` para permitir el acceso al dispositivo.
- Si no hay hardware de audio disponible, el servicio arranca igualmente en **modo log** (`audio_available: false`) y no tumba el contenedor.

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
