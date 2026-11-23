import re
import unicodedata
from typing import Dict, List, Optional

INTENTS: Dict[str, dict] = {
    "ABRIR_PUERTA": {
        "cmd": "OPEN",
        "tts": "Abriendo la puerta enseguida.",
    },
    "RECHAZAR": {
        "cmd": "REJECT",
        "tts": "Entendido, no abriré.",
    },
    "CONSULTA": {
        "cmd": "CHECK",
        "tts": "Verificando el registro de la cámara.",
    },
    "DESCONOCIDO": {
        "cmd": "NONE",
        "tts": "No entendí la instrucción. Puedes decir: abre, no abras, o quién hay.",
    },
}

STOPWORDS = [
    "por favor",
    "porfa",
    "porfavor",
    "gracias",
    "hola",
    "buenas",
    "buenos dias",
    "portero",
    "asistente",
]

RE_OPEN_FUERTE = re.compile(
    r"\b(?:abre|abrir|abra|abran|abrela|abreme|abrele|abrir la puerta|"
    r"abre la puerta|abra la puerta|deja entrar|dejar entrar|dejalo pasar|"
    r"deja pasar|puede pasar|pueden pasar|adelante|pasa|pasen|permite|"
    r"permitir|permite el ingreso|acceso|autorizo|autorizado|esta bien|"
    r"afirmativo|correcto|claro)\b"
)
RE_OPEN_LEVE = re.compile(r"\b(?:si|ok|vale|listo|hecho|dale)\b")

RE_REJECT_FUERTE = re.compile(
    r"\b(?:no abras|no abrir|noabra|no le abras|no lo dejes entrar|"
    r"no dejes entrar|no permitas|no puede pasar|dile que no|decile que no|"
    r"di que no|rechaza|rechazar|rechazado|deniega|denegar|denegado|negar|"
    r"negativo|prohibido|no autorizado|cierra|cerrar|cierra la puerta|"
    r"mandalo lejos|fuera de aqui|vete|ignora|ignorar)\b"
)
RE_REJECT_LEVE = re.compile(r"\b(?:no|nunca|jamas)\b")

RE_CHECK_FUERTE = re.compile(
    r"\b(?:quien esta|quien esta en la puerta|quienes estan|quien hay|"
    r"quienes hay|hay alguien|alguien en la puerta|cuantas personas|"
    r"cuantos registrados|personas registradas|lista de personas|"
    r"registro de personas|que personas|cuantas hay|quien es)\b"
)
RE_CHECK_LEVE = re.compile(r"\b(?:quien|quienes|registro|registrados|personas|estado|historial)\b")

PRIORIDAD = {"CONSULTA": 2, "RECHAZAR": 1, "ABRIR_PUERTA": 0}


def normalizar(texto: str) -> str:
    texto = (texto or "").lower()
    texto = "".join(
        caracter
        for caracter in unicodedata.normalize("NFKD", texto)
        if not unicodedata.combining(caracter)
    )
    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    for palabra in STOPWORDS:
        texto = re.sub(rf"\b{palabra}\b", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def _puntaje(pattern: re.Pattern, texto: str, peso: int) -> int:
    return peso * len(pattern.findall(texto))


def _unir(nombres: List[str]) -> str:
    nombres = list(dict.fromkeys(nombres))[:5]
    if len(nombres) == 1:
        return nombres[0]
    if len(nombres) == 2:
        return f"{nombres[0]} y {nombres[1]}"
    return f"{', '.join(nombres[:-1])} y {nombres[-1]}"


def _frase_consulta(
    personas: Optional[List[str]],
    ultima_deteccion: Optional[List[str]],
) -> str:
    detectados = [n for n in (ultima_deteccion or []) if n and n != "Sin rostro visible"]
    registrados = [n for n in (personas or []) if n]

    if detectados:
        return f"En la puerta detecté a {_unir(detectados)}."
    if registrados:
        return f"No detecté a nadie ahora. Tengo registrados a {_unir(registrados)}."
    return "No detecté a nadie y todavía no tengo personas registradas."


def _respuesta(
    intent: str,
    confianza: float,
    texto_normalizado: str,
    personas: Optional[List[str]] = None,
    ultima_deteccion: Optional[List[str]] = None,
) -> dict:
    datos = dict(INTENTS[intent])
    if intent == "CONSULTA":
        datos["tts"] = _frase_consulta(personas, ultima_deteccion)
    return {
        "intent": intent,
        "cmd": datos["cmd"],
        "tts": datos["tts"],
        "confidence": round(confianza, 2),
        "texto_normalizado": texto_normalizado,
    }


def interpretar(
    texto: str,
    personas: Optional[List[str]] = None,
    ultima_deteccion: Optional[List[str]] = None,
) -> dict:
    limpio = normalizar(texto)
    if not limpio:
        return _respuesta("DESCONOCIDO", 0.0, limpio)

    abrir = _puntaje(RE_OPEN_FUERTE, limpio, 2) + _puntaje(RE_OPEN_LEVE, limpio, 1)
    rechazar = _puntaje(RE_REJECT_FUERTE, limpio, 3) + _puntaje(RE_REJECT_LEVE, limpio, 1)
    consultar = _puntaje(RE_CHECK_FUERTE, limpio, 3) + _puntaje(RE_CHECK_LEVE, limpio, 1)

    if rechazar and abrir and not RE_REJECT_FUERTE.search(limpio):
        rechazar += abrir + 1
        abrir = 0
    if RE_REJECT_FUERTE.search(limpio):
        abrir = 0

    puntajes = {
        "ABRIR_PUERTA": abrir,
        "RECHAZAR": rechazar,
        "CONSULTA": consultar,
    }
    mejor = max(puntajes.values())
    if mejor <= 0:
        return _respuesta("DESCONOCIDO", 0.0, limpio)

    ganadores = [nombre for nombre, valor in puntajes.items() if valor == mejor]
    elegida = max(ganadores, key=lambda nombre: PRIORIDAD[nombre])
    confianza = min(0.99, 0.55 + 0.11 * mejor)
    return _respuesta(elegida, confianza, limpio, personas, ultima_deteccion)
