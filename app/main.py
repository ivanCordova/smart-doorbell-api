import os
import time
import asyncio
from datetime import datetime
from contextlib import asynccontextmanager
from typing import List
import cv2
import numpy as np
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from ultralytics import YOLO

# 1. Variables de entorno y rutas
ESP32_STREAM_URL = os.getenv("ESP32_STREAM_URL", "http://192.168.68.108/mjpeg")
CAPTURES_DIR = "captures"
KNOWN_FACES_DIR = "known_faces"
MODELS_DIR = "models"

os.makedirs(CAPTURES_DIR, exist_ok=True)
os.makedirs(KNOWN_FACES_DIR, exist_ok=True)

ALERT_COOLDOWN = 8.0
FRAME_CHECK_INTERVAL = 0.5
COSINE_SIMILARITY_THRESHOLD = 0.363

print("Cargando modelo YOLO...")
yolo_model = YOLO("yolo11n.pt")

templates = Jinja2Templates(directory="templates")

# Carga de modelos de OpenCV
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
latest_jpeg_frame = None

# 2. Funciones auxiliares de visión
def extract_face_feature(img: np.ndarray) -> np.ndarray | None:
    h, w, _ = img.shape
    detector.setInputSize((w, h))
    _, faces = detector.detect(img)
    if faces is None or len(faces) == 0:
        return None
    face = faces[0]
    aligned_face = recognizer.alignCrop(img, face)
    return recognizer.feature(aligned_face)

def reload_known_faces():
    global known_face_features, known_face_names
    features = []
    names = []
    print("Indexando rostros conocidos...")
    for filename in os.listdir(KNOWN_FACES_DIR):
        if not filename.lower().endswith((".jpg", ".jpeg", ".png")):
            continue
        filepath = os.path.join(KNOWN_FACES_DIR, filename)
        name = os.path.splitext(filename)[0].replace("_", " ").title()
        img = cv2.imread(filepath)
        if img is None:
            continue
        feat = extract_face_feature(img)
        if feat is not None:
            features.append(feat)
            names.append(name)
            print(f"  [+] Rostro registrado: {name}")
    known_face_features = features
    known_face_names = names
    print(f"Total registrados: {len(known_face_names)}")

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

def trigger_alert(frame, persons_count: int, identified_names: List[str]):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    people_str = "_".join([n.replace(" ", "") for n in identified_names]) if identified_names else "SinRostro"
    filename = f"alerta_{timestamp}_{people_str}.jpg"
    filepath = os.path.join(CAPTURES_DIR, filename)
    cv2.imwrite(filepath, frame)
    print("\n" + "=" * 50)
    print(f"🚨 [ALERTA DE TIMBRE] {timestamp}")
    print(f"👥 Personas detectadas: {persons_count}")
    print(f"🔍 Identificación: {', '.join(identified_names) if identified_names else 'Rostro no visible'}")
    print(f"💾 Guardado: {filepath}")
    print("=" * 50 + "\n")

# 3. Bucle de fondo para capturar el stream
async def stream_monitor_loop():
    global latest_jpeg_frame
    while True:
        try:
            print(f"Conectando a {ESP32_STREAM_URL}...")
            cap = cv2.VideoCapture(ESP32_STREAM_URL)
            if not cap.isOpened():
                await asyncio.sleep(3)
                continue

            last_check = 0.0
            last_alert_time = 0.0

            while cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    break

                _, buffer = cv2.imencode('.jpg', frame)
                latest_jpeg_frame = buffer.tobytes()

                now = time.time()
                if now - last_check >= FRAME_CHECK_INTERVAL:
                    last_check = now
                    results = yolo_model.predict(source=frame, classes=[0], conf=0.55, verbose=False)
                    boxes = results[0].boxes
                    if len(boxes) > 0:
                        names = identify_faces(frame)
                        if now - last_alert_time > ALERT_COOLDOWN:
                            last_alert_time = now
                            annotated_frame = results[0].plot()
                            y_offset = 35
                            for name in names:
                                color = (0, 255, 0) if name != "Desconocido" else (0, 0, 255)
                                cv2.putText(annotated_frame, f"ID: {name}", (20, y_offset),
                                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 2)
                                y_offset += 35
                            trigger_alert(annotated_frame, len(boxes), names)

                await asyncio.sleep(0.01)

            cap.release()
        except Exception as e:
            print(f"Error en monitor de video: {e}")
            await asyncio.sleep(3)

# 4. Contexto Lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    reload_known_faces()
    task = asyncio.create_task(stream_monitor_loop())
    yield
    task.cancel()

# 5. CREACIÓN DE LA INSTANCIA (Debe ir ANTES de cualquier @app.get o @app.post)
app = FastAPI(title="Smart Doorbell Vision & Face ID", lifespan=lifespan)

# 6. ENDPOINTS DE LA APLICACIÓN
async def generate_web_stream():
    global latest_jpeg_frame
    while True:
        if latest_jpeg_frame is not None:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + latest_jpeg_frame + b'\r\n')
        await asyncio.sleep(0.04)

@app.get("/video_feed")
async def video_feed():
    return StreamingResponse(
        generate_web_stream(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )

@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"stream_url": "/video_feed"}
    )

@app.get("/")
def get_status():
    return {
        "status": "online",
        "registered_people": known_face_names,
        "total_registered": len(known_face_names),
        "esp32_source": ESP32_STREAM_URL
    }

@app.post("/faces/register")
async def register_face(name: str = Form(...), file: UploadFile = File(...)):
    clean_name = name.strip().replace(" ", "_")
    filename = f"{clean_name}.jpg"
    filepath = os.path.join(KNOWN_FACES_DIR, filename)

    contents = await file.read()
    with open(filepath, "wb") as f:
        f.write(contents)

    img = cv2.imread(filepath)
    if img is None:
        os.remove(filepath)
        raise HTTPException(status_code=400, detail="El archivo no es una imagen válida")

    feat = extract_face_feature(img)
    if feat is None:
        os.remove(filepath)
        raise HTTPException(status_code=400, detail="No se detectó un rostro nítido en la imagen")

    reload_known_faces()

    return {
        "status": "success",
        "message": f"'{name}' ha sido registrado exitosamente",
        "filename": filename
    }

@app.post("/faces/register-from-stream")
async def register_face_from_stream(name: str = Form(...)):
    global latest_jpeg_frame
    
    if latest_jpeg_frame is None:
        raise HTTPException(status_code=503, detail="No hay señal de video disponible desde la cámara")

    clean_name = name.strip().replace(" ", "_")
    filename = f"{clean_name}.jpg"
    filepath = os.path.join(KNOWN_FACES_DIR, filename)

    # Decodificar el cuadro JPEG actual directamente desde la memoria RAM
    nparr = np.frombuffer(latest_jpeg_frame, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if img is None:
        raise HTTPException(status_code=500, detail="Error al procesar el fotograma actual")

    # Validar que se detecte un rostro con YuNet antes de guardarlo
    feat = extract_face_feature(img)
    if feat is None:
        raise HTTPException(
            status_code=400, 
            detail="No se detectó un rostro nítido en este fotograma. Mira directo a la cámara e inténtalo de nuevo."
        )

    # Guardar en disco para persistencia
    cv2.imwrite(filepath, img)

    # Recargar embeddings en memoria
    reload_known_faces()

    return {
        "status": "success",
        "message": f"'{name}' registrado exitosamente desde la cámara en vivo",
        "filename": filename
    }