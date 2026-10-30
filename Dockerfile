FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ./app ./app
COPY ./templates ./templates
COPY ./models ./models

# Descarga los modelos de OpenCV Zoo si no están presentes en ./models
RUN set -eux; \
    if [ ! -f models/face_detection_yunet.onnx ]; then \
        curl -fsSL -o models/face_detection_yunet.onnx \
        https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx; \
    fi; \
    if [ ! -f models/face_recognition_sface.onnx ]; then \
        curl -fsSL -o models/face_recognition_sface.onnx \
        https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx; \
    fi

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
