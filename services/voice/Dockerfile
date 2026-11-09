FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

RUN apt-get update && apt-get install -y --no-install-recommends \
    libportaudio2 \
    espeak-ng \
    libespeak-ng1 \
    alsa-utils \
    libasound2-plugins \
    pulseaudio-utils \
    libgomp1 \
    curl \
    unzip \
    libatomic1 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 --retries 5 -r requirements.txt

RUN set -eux; \
    curl -fsSL -o /tmp/vosk-model.zip \
        https://alphacephei.com/vosk/models/vosk-model-small-es-0.42.zip; \
    unzip -q /tmp/vosk-model.zip -d models; \
    rm -f /tmp/vosk-model.zip

ARG PIPER_VOICE=es_MX-claude-high
RUN python3 -m piper.download_voices ${PIPER_VOICE} --data-dir models/piper

COPY asound.conf /etc/asound.conf
COPY ./app ./app

EXPOSE 8001

CMD ["uvicorn", "app.voice_service:app", "--host", "0.0.0.0", "--port", "8001"]
