import os
import re
import json
import time
import asyncio
import threading
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

ESP32_STREAM_URL = os.getenv("ESP32_STREAM_URL", "http://192.168.68.108/mjpeg")
CAPTURES_DIR = "captures"
KNOWN_FACES_DIR = "known_faces"
MODELS_DIR = "models"
DB_FILE = os.path.join(KNOWN_FACES_DIR, "persons.json")

DETECTOR_MODEL = os.path.join(MODELS_DIR, "face_detection_yunet.onnx")
RECOGNIZER_MODEL = os.path.join(MODELS_DIR, "face_recognition_sface.onnx")

ALERT_COOLDOWN = 6.0
FRAME_CHECK_INTERVAL = 0.5
COSINE_SIMILARITY_THRESHOLD = 0.363
MAX_HISTORY = 20
MAX_CAPTURES = 100
JPEG_QUALITY = 80

os.makedirs(CAPTURES_DIR, exist_ok=True)
os.makedirs(KNOWN_FACES_DIR, exist_ok=True)

for model_path in (DETECTOR_MODEL, RECOGNIZER_MODEL):
    if not os.path.exists(model_path):
        raise RuntimeError(
            f"Falta el modelo {model_path}. Colócalo en ./models o reconstruye la imagen Docker."
        )

cv2.setNumThreads(2)

detector = cv2.FaceDetectorYN.create(
    model=DETECTOR_MODEL,
    config="",
    input_size=(640, 480),
    score_threshold=0.7,
    nms_threshold=0.3,
    top_k=5000,
)

recognizer = cv2.FaceRecognizerSF.create(model=RECOGNIZER_MODEL, config="")

templates = Jinja2Templates(directory="templates")

system_state = {
    "stream_url": ESP32_STREAM_URL,
    "ai_active": True,
    "camera_online": False,
    "history": [],
}

latest_jpeg_frame: Optional[bytes] = None
active_capture: Optional[cv2.VideoCapture] = None

state_lock = threading.Lock()
model_lock = threading.Lock()
capture_lock = threading.Lock()

known_faces: List[Tuple[str, np.ndarray]] = []


def load_db() -> Dict[str, dict]:
    if not os.path.exists(DB_FILE):
        return {}
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_db(data: Dict[str, dict]):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def extract_face_feature(img: np.ndarray) -> Optional[np.ndarray]:
    h, w = img.shape[:2]
    with model_lock:
        detector.setInputSize((w, h))
        _, faces = detector.detect(img)
        if faces is None or len(faces) == 0:
            return None
        aligned_face = recognizer.alignCrop(img, faces[0])
        return recognizer.feature(aligned_face)


def reload_known_faces():
    global known_faces
    loaded = []
    db = load_db()

    for filename in os.listdir(KNOWN_FACES_DIR):
        if not filename.lower().endswith((".jpg", ".jpeg", ".png")):
            continue
        filepath = os.path.join(KNOWN_FACES_DIR, filename)
        person_id = os.path.splitext(filename)[0]
        name = db.get(person_id, {}).get("name", person_id.replace("_", " ").title())

        img = cv2.imread(filepath)
        if img is None:
            continue
        feat = extract_face_feature(img)
        if feat is not None:
            loaded.append((name, feat))

    known_faces = loaded
    print(f"Indexados {len(known_faces)} rostros conocidos.")


def detect_and_identify(frame: np.ndarray) -> List[Tuple[str, Tuple[int, int, int, int]]]:
    h, w = frame.shape[:2]
    with model_lock:
        detector.setInputSize((w, h))
        _, faces = detector.detect(frame)
        if faces is None or len(faces) == 0:
            return []

        results = []
        for face in faces:
            aligned_face = recognizer.alignCrop(frame, face)
            feature = recognizer.feature(aligned_face)
            best_score = -1.0
            best_name = "Desconocido"

            for name, known_feat in known_faces:
                score = recognizer.match(known_feat, feature, cv2.FaceRecognizerSF_FR_COSINE)
                if score > best_score and score >= COSINE_SIMILARITY_THRESHOLD:
                    best_score = score
                    best_name = name

            x, y, fw, fh = (int(v) for v in face[:4])
            results.append((best_name, (x, y, fw, fh)))
    return results


def prune_captures():
    files = sorted(f for f in os.listdir(CAPTURES_DIR) if f.endswith(".jpg"))
    for filename in files[:-MAX_CAPTURES]:
        try:
            os.remove(os.path.join(CAPTURES_DIR, filename))
        except OSError:
            pass


def log_detection(frame: np.ndarray, detections: List[Tuple[str, Tuple[int, int, int, int]]]):
    annotated = frame.copy()
    for name, (x, y, w, h) in detections:
        color = (0, 255, 0) if name != "Desconocido" else (0, 0, 255)
        cv2.rectangle(annotated, (x, y), (x + w, y + h), color, 2)
        cv2.putText(annotated, name, (x, max(y - 10, 20)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)

    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    display_time = datetime.now().strftime("%H:%M:%S - %d/%m/%Y")
    filename = f"det_{timestamp_str}.jpg"
    cv2.imwrite(os.path.join(CAPTURES_DIR, filename), annotated,
                [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])

    names = [name for name, _ in detections]
    event = {
        "id": timestamp_str,
        "time": display_time,
        "count": len(detections),
        "people": names or ["Sin rostro visible"],
        "image": f"/captures/{filename}",
    }
    with state_lock:
        system_state["history"].insert(0, event)
        del system_state["history"][MAX_HISTORY:]

    prune_captures()


def capture_loop():
    global latest_jpeg_frame, active_capture

    while True:
        url = system_state["stream_url"]
        cap = cv2.VideoCapture(url)

        if not cap.isOpened():
            system_state["camera_online"] = False
            print(f"No se pudo abrir el stream en {url}. Reintentando en 3s...")
            cap.release()
            time.sleep(3)
            continue

        with capture_lock:
            active_capture = cap

        system_state["camera_online"] = True
        print(f"Conectado al stream: {url}")
        last_check = 0.0
        last_alert = 0.0

        try:
            while cap.isOpened():
                if system_state["stream_url"] != url:
                    break

                ret, frame = cap.read()
                if not ret:
                    break

                ok, buffer = cv2.imencode(".jpg", frame,
                                          [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                if ok:
                    with state_lock:
                        latest_jpeg_frame = buffer.tobytes()

                now = time.time()
                if system_state["ai_active"] and (now - last_check >= FRAME_CHECK_INTERVAL):
                    last_check = now
                    detections = detect_and_identify(frame)
                    if detections and (now - last_alert >= ALERT_COOLDOWN):
                        last_alert = now
                        log_detection(frame, detections)

                time.sleep(0.01)
        except Exception as e:
            print(f"Error en monitor de video: {e}")
        finally:
            with capture_lock:
                if active_capture is cap:
                    active_capture = None
            cap.release()
            system_state["camera_online"] = False

        time.sleep(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    reload_known_faces()
    threading.Thread(target=capture_loop, daemon=True).start()
    yield


app = FastAPI(title="Smart Doorbell Gateway", lifespan=lifespan)

app.mount("/captures", StaticFiles(directory=CAPTURES_DIR), name="captures")
app.mount("/known_faces", StaticFiles(directory=KNOWN_FACES_DIR), name="known_faces")


@app.get("/", response_class=HTMLResponse)
def index_page(request: Request):
    return templates.TemplateResponse(request=request, name="home.html", context={})


@app.get("/register-view", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(request=request, name="register.html", context={})


async def generate_web_stream():
    while True:
        with state_lock:
            frame = latest_jpeg_frame
        if frame is not None:
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
        await asyncio.sleep(0.05)


@app.get("/video_feed")
async def video_feed():
    return StreamingResponse(
        generate_web_stream(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/api/system/status")
def get_system_status():
    with state_lock:
        return {
            "stream_url": system_state["stream_url"],
            "ai_active": system_state["ai_active"],
            "camera_online": system_state["camera_online"],
            "history": list(system_state["history"]),
        }


class StreamConfig(BaseModel):
    url: str


@app.post("/api/system/stream-url")
def update_stream_url(config: StreamConfig):
    nueva_url = config.url.strip()
    if not nueva_url:
        raise HTTPException(400, "La URL no puede estar vacía")

    system_state["stream_url"] = nueva_url

    with capture_lock:
        if active_capture is not None:
            active_capture.release()

    return {"status": "ok", "message": f"URL actualizada a {nueva_url}"}


@app.post("/api/system/toggle-ai")
def toggle_ai():
    system_state["ai_active"] = not system_state["ai_active"]
    return {"status": "ok", "ai_active": system_state["ai_active"]}


@app.get("/api/persons")
def list_persons():
    return list(load_db().values())


def safe_id(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", name.strip().lower()).strip("_")


@app.post("/api/persons/register")
def register_person(
    name: str = Form(...),
    notes: Optional[str] = Form(""),
    phone: Optional[str] = Form(""),
    file: Optional[UploadFile] = File(None),
):
    clean_id = safe_id(name)
    if not clean_id:
        raise HTTPException(400, "El nombre no es válido.")

    filename = f"{clean_id}.jpg"
    filepath = os.path.join(KNOWN_FACES_DIR, filename)

    if file and file.filename:
        content = file.file.read()
        img = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
    else:
        with state_lock:
            frame_bytes = latest_jpeg_frame
        if frame_bytes is None:
            raise HTTPException(400, "No hay cuadro disponible del stream para capturar.")
        img = cv2.imdecode(np.frombuffer(frame_bytes, np.uint8), cv2.IMREAD_COLOR)

    if img is None or extract_face_feature(img) is None:
        raise HTTPException(400, "No se detectó un rostro nítido en la imagen.")

    cv2.imwrite(filepath, img, [cv2.IMWRITE_JPEG_QUALITY, 95])

    db = load_db()
    db[clean_id] = {
        "id": clean_id,
        "name": name.strip(),
        "notes": notes.strip() if notes else "Sin notas",
        "phone": phone.strip() if phone else "N/A",
        "photo": f"/known_faces/{filename}",
        "registered_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    save_db(db)
    reload_known_faces()
    return {"status": "ok", "message": f"{name} guardado con éxito."}


@app.delete("/api/persons/{person_id}")
def delete_person(person_id: str):
    clean_id = safe_id(person_id)
    db = load_db()
    if clean_id in db:
        del db[clean_id]
        save_db(db)

    photo_path = os.path.join(KNOWN_FACES_DIR, f"{clean_id}.jpg")
    if os.path.exists(photo_path):
        os.remove(photo_path)

    reload_known_faces()
    return {"status": "ok", "message": "Persona eliminada"}


@app.get("/api/status")
def get_status():
    return {
        "status": "online",
        "registered_people": [name for name, _ in known_faces],
        "total_registered": len(known_faces),
        "esp32_source": system_state["stream_url"],
    }
