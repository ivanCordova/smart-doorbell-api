import os
import json
import time
import asyncio
from datetime import datetime
from contextlib import asynccontextmanager
from typing import List, Dict, Optional
import cv2
import numpy as np
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from ultralytics import YOLO

# Rutas del sistema
ESP32_STREAM_URL = os.getenv("ESP32_STREAM_URL", "http://192.168.68.108/mjpeg")
CAPTURES_DIR = "captures"
KNOWN_FACES_DIR = "known_faces"
MODELS_DIR = "models"
DB_FILE = "known_faces/persons.json"

os.makedirs(CAPTURES_DIR, exist_ok=True)
os.makedirs(KNOWN_FACES_DIR, exist_ok=True)

ALERT_COOLDOWN = 6.0
FRAME_CHECK_INTERVAL = 0.5
COSINE_SIMILARITY_THRESHOLD = 0.363

# Estado global del sistema
system_state = {
    "stream_url": ESP32_STREAM_URL,
    "ai_active": True,
    "camera_online": False,
    "history": []  # Registro temporal de eventos
}

latest_jpeg_frame = None

print("Cargando modelo YOLO...")
yolo_model = YOLO("yolo11n.pt")

templates = Jinja2Templates(directory="templates")

# Modelos biométricos de OpenCV
detector = cv2.FaceDetectorYN.create(
    model=os.path.join(MODELS_DIR, "face_detection_yunet.onnx"),
    config="",
    input_size=(640, 480),
    score_threshold=0.7,
    nms_threshold=0.3,
    top_k=5000
)

recognizer = cv2.FaceRecognizerSF.create(
    model=os.path.join(MODELS_DIR, "face_recognition_sface.onnx"),
    config=""
)

known_face_features: List[np.ndarray] = []
known_face_names: List[str] = []

# --- Manejo de la pequeña base de datos de personas ---
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
    h, w, _ = img.shape
    detector.setInputSize((w, h))
    _, faces = detector.detect(img)
    if faces is None or len(faces) == 0:
        return None
    aligned_face = recognizer.alignCrop(img, faces[0])
    return recognizer.feature(aligned_face)

def reload_known_faces():
    global known_face_features, known_face_names
    features = []
    names = []
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
            features.append(feat)
            names.append(name)

    known_face_features = features
    known_face_names = names
    print(f"Indexados {len(known_face_names)} rostros conocidos.")

def identify_faces(frame: np.ndarray) -> List[str]:
    if not known_face_features:
        return []
    h, w, _ = frame.shape
    detector.setInputSize((w, h))
    _, faces = detector.detect(frame)
    if faces is None or len(faces) == 0:
        return []

    identified = []
    for face in faces:
        aligned_face = recognizer.alignCrop(frame, face)
        feature = recognizer.feature(aligned_face)
        best_score = -1.0
        best_name = "Desconocido"

        for idx, known_feat in enumerate(known_face_features):
            score = recognizer.match(known_feat, feature, cv2.FaceRecognizerSF_FR_COSINE)
            if score > best_score and score >= COSINE_SIMILARITY_THRESHOLD:
                best_score = score
                best_name = known_face_names[idx]
        identified.append(best_name)
    return identified

def log_detection(annotated_frame, count: int, people: List[str]):
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    display_time = datetime.now().strftime("%H:%M:%S - %d/%m/%Y")
    filename = f"det_{timestamp_str}.jpg"
    filepath = os.path.join(CAPTURES_DIR, filename)
    cv2.imwrite(filepath, annotated_frame)

    event = {
        "id": timestamp_str,
        "time": display_time,
        "count": count,
        "people": people if people else ["Sin rostro visible"],
        "image": f"/captures/{filename}"
    }
    system_state["history"].insert(0, event)
    if len(system_state["history"]) > 20:
        system_state["history"].pop()

# --- Monitor de Video y Detección ---
async def stream_monitor_loop():
    global latest_jpeg_frame
    while True:
        try:
            url = system_state["stream_url"]
            cap = cv2.VideoCapture(url)
            if not cap.isOpened():
                system_state["camera_online"] = False
                await asyncio.sleep(3)
                continue

            system_state["camera_online"] = True
            last_check = 0.0
            last_alert = 0.0

            while cap.isOpened():
                # Si el usuario cambió la URL, rompemos el ciclo para conectar al nuevo stream
                if system_state["stream_url"] != url:
                    break

                ret, frame = cap.read()
                if not ret:
                    break

                _, buffer = cv2.imencode('.jpg', frame)
                latest_jpeg_frame = buffer.tobytes()

                now = time.time()
                # Ejecutar IA solo si está habilitada por el usuario
                if system_state["ai_active"] and (now - last_check >= FRAME_CHECK_INTERVAL):
                    last_check = now
                    results = yolo_model.predict(source=frame, classes=[0], conf=0.55, verbose=False)
                    boxes = results[0].boxes

                    if len(boxes) > 0:
                        names = identify_faces(frame)
                        if now - last_alert > ALERT_COOLDOWN:
                            last_alert = now
                            annotated = results[0].plot()
                            y = 35
                            for n in names:
                                color = (0, 255, 0) if n != "Desconocido" else (0, 0, 255)
                                cv2.putText(annotated, f"ID: {n}", (20, y),
                                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 2)
                                y += 35
                            log_detection(annotated, len(boxes), names)

                await asyncio.sleep(0.01)

            cap.release()
            system_state["camera_online"] = False
        except Exception as e:
            print(f"Error en video loop: {e}")
            await asyncio.sleep(3)

@asynccontextmanager
async def lifespan(app: FastAPI):
    reload_known_faces()
    task = asyncio.create_task(stream_monitor_loop())
    yield
    task.cancel()

app = FastAPI(title="Smart Doorbell Gateway", lifespan=lifespan)

# Montar carpeta de fotos para ver las detecciones en la interfaz
app.mount("/captures", StaticFiles(directory=CAPTURES_DIR), name="captures")

# --- Rutas de Vistas HTML ---
@app.get("/", response_class=HTMLResponse)
def index_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={}
    )

@app.get("/register-view", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={}
    )

# --- Stream Relay ---
async def generate_web_stream():
    global latest_jpeg_frame
    while True:
        if latest_jpeg_frame is not None:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + latest_jpeg_frame + b'\r\n')
        await asyncio.sleep(0.04)

@app.get("/video_feed")
async def video_feed():
    return StreamingResponse(generate_web_stream(), media_type="multipart/x-mixed-replace; boundary=frame")

# --- Endpoints de Control y API ---
@app.get("/api/system/status")
def get_system_status():
    return {
        "stream_url": system_state["stream_url"],
        "ai_active": system_state["ai_active"],
        "camera_online": system_state["camera_online"],
        "history": system_state["history"]
    }

class StreamConfig(BaseModel):
    url: str

@app.post("/api/system/stream-url")
def update_stream_url(config: StreamConfig):
    system_state["stream_url"] = config.url.strip()
    return {"status": "ok", "message": f"URL actualizada a {system_state['stream_url']}"}

@app.post("/api/system/toggle-ai")
def toggle_ai():
    system_state["ai_active"] = not system_state["ai_active"]
    return {"status": "ok", "ai_active": system_state["ai_active"]}

# --- CRUD de Personas ---
@app.get("/api/persons")
def list_persons():
    db = load_db()
    return list(db.values())

@app.post("/api/persons/register")
async def register_person(
    name: str = Form(...),
    notes: Optional[str] = Form(""),
    phone: Optional[str] = Form(""),
    file: Optional[UploadFile] = File(None)
):
    global latest_jpeg_frame
    clean_id = name.strip().lower().replace(" ", "_")
    filename = f"{clean_id}.jpg"
    filepath = os.path.join(KNOWN_FACES_DIR, filename)

    if file and file.filename:
        content = await file.read()
        with open(filepath, "wb") as f:
            f.write(content)
        img = cv2.imread(filepath)
    else:
        # Tomar del stream en vivo si no se adjunta archivo
        if latest_jpeg_frame is None:
            raise HTTPException(400, "No hay cuadro disponible del stream para capturar.")
        nparr = np.frombuffer(latest_jpeg_frame, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is not None:
            cv2.imwrite(filepath, img)

    if img is None or extract_face_feature(img) is None:
        if os.path.exists(filepath):
            os.remove(filepath)
        raise HTTPException(400, "No se detectó un rostro nítido en la imagen.")

    # Guardar metadatos
    db = load_db()
    db[clean_id] = {
        "id": clean_id,
        "name": name.strip(),
        "notes": notes.strip() if notes else "Sin notas",
        "phone": phone.strip() if phone else "N/A",
        "photo": f"/{filepath}",
        "registered_at": datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    save_db(db)
    reload_known_faces()
    return {"status": "ok", "message": f"{name} guardado con éxito."}

@app.delete("/api/persons/{person_id}")
def delete_person(person_id: str):
    db = load_db()
    if person_id in db:
        del db[person_id]
        save_db(db)

    photo_path = os.path.join(KNOWN_FACES_DIR, f"{person_id}.jpg")
    if os.path.exists(photo_path):
        os.remove(photo_path)

    reload_known_faces()
    return {"status": "ok", "message": "Persona eliminada"}

@app.get("/api/status")
def get_status():
    return {
        "status": "online",
        "registered_people": known_face_names,
        "total_registered": len(known_face_names),
        "esp32_source": system_state["stream_url"]
    }