FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /code

# Dependencias nativas: Gráficos (OpenCV) + Audio (sounddevice, pyttsx3/espeak)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libxcb1 \
    libgl1 \
    libasound2 \
    libasound2-plugins \
    portaudio19-dev \
    alsa-utils \
    espeak-ng \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiamos las carpetas locales directamente (los modelos ONNX deben estar en ./models)
COPY ./models ./models
COPY ./app ./app
COPY ./templates ./templates

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
