import json
import os
import urllib.request
from typing import List, Tuple

from fastapi import FastAPI
from pydantic import BaseModel

from app.intents import INTENTS, interpretar

app = FastAPI(title="Doorbell NLU Service")

API_SERVICE_URL = os.getenv("API_SERVICE_URL", "http://doorbell-api:8000").rstrip("/")
API_TIMEOUT = float(os.getenv("NLU_API_TIMEOUT", "1.5"))


def _obtener_json(ruta: str) -> dict:
    try:
        with urllib.request.urlopen(f"{API_SERVICE_URL}{ruta}", timeout=API_TIMEOUT) as resp:
            return json.load(resp)
    except Exception as exc:
        print(f"⚠️ [NLU] No se pudo consultar {ruta}: {exc}")
        return {}


def _contexto_visitas() -> Tuple[List[str], List[str]]:
    personas: List[str] = []
    deteccion: List[str] = []

    estado = _obtener_json("/api/status")
    if isinstance(estado, dict):
        personas = [str(n) for n in estado.get("registered_people", [])][:5]

    sistema = _obtener_json("/api/system/status")
    if isinstance(sistema, dict):
        historial = sistema.get("history") or []
        if historial and isinstance(historial[0], dict):
            deteccion = [
                str(n)
                for n in historial[0].get("people", [])
                if str(n) != "Sin rostro visible"
            ][:5]

    return personas, deteccion


class InterpretPayload(BaseModel):
    text: str


@app.get("/health")
def health():
    return {
        "status": "online",
        "api_service_url": API_SERVICE_URL,
        "intents": list(INTENTS.keys()),
    }


@app.get("/api/nlu/intents")
def listar_intents():
    return [
        {"intent": nombre, "cmd": datos["cmd"], "tts": datos["tts"]}
        for nombre, datos in INTENTS.items()
    ]


@app.post("/api/nlu/interpret")
def interpretar_texto(payload: InterpretPayload):
    texto = payload.text.strip()
    resultado = interpretar(texto)

    if resultado["intent"] == "CONSULTA":
        personas, deteccion = _contexto_visitas()
        resultado = interpretar(texto, personas=personas, ultima_deteccion=deteccion)

    print(
        f"🧠 [NLU] '{texto}' -> {resultado['intent']} "
        f"({resultado['confidence']:.2f})"
    )
    return resultado
