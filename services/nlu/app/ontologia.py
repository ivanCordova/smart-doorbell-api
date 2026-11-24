"""Capa semántica: ontología del dominio de control de acceso.

Modela Clases (``AccionAcceso``), Individuos (``PuntosAcceso``) y propiedades
léxicas para resolver sinónimos, desambiguar objetivos y validar axiomas de
estado antes de tocar hardware.
"""

from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Dict, Optional

ESTADO_ABIERTO = "ABIERTO"
ESTADO_CERRADO = "CERRADO"
ESTADO_ACTIVO = "ACTIVO"

ESTADO_LISTO = "LISTO"
ESTADO_ACCION_NEGADA = "ACCION_NEGADA"
ESTADO_FUERA_DOMINIO = "FUERA_DOMINIO"
ESTADO_REDUNDANTE = "REDUNDANTE"
ESTADO_REQUIERE_PIN = "REQUIERE_PIN"

_CMD = {"ABRIR": "OPEN", "CERRAR": "REJECT", "CONSULTAR": "CHECK"}
_INTENT = {"ABRIR": "ABRIR_PUERTA", "CERRAR": "RECHAZAR", "CONSULTAR": "CONSULTA"}
_TTS = {
    "ABRIR": "Abriendo la puerta enseguida.",
    "CERRAR": "Cerrando la puerta.",
    "CONSULTAR": "Verificando el registro de la cámara.",
}
_TTS_NEGADA = "Entendido, no abriré."
_TTS_FUERA_DOMINIO = (
    "No entendí la instrucción. Puedes decir: abre, no abras, o quién hay."
)


class AccionAcceso(Enum):
    """Clase de acciones admitidas y sus lemas equivalents."""

    ABRIR = frozenset({"abrir", "desbloquear", "liberar", "activar", "franquear", "pasar"})
    CERRAR = frozenset({"cerrar", "bloquear", "asegurar", "trancar"})
    CONSULTAR = frozenset({"ver", "mostrar", "revisar", "checar", "consultar"})

    @classmethod
    def desde_lema(cls, lema: str) -> Optional["AccionAcceso"]:
        for accion in cls:
            if lema in accion.value:
                return accion
        return None


@dataclass
class IndividuoAcceso:
    """Individuo de ``PuntoAcceso`` con estado mutable en memoria."""

    nombre: str
    lemas: frozenset
    estado_inicial: str
    requiere_pin: bool = False
    estado: str = field(init=False)

    def __post_init__(self) -> None:
        self.estado = self.estado_inicial


PUNTOS_ACCESO: Dict[str, IndividuoAcceso] = {
    "PUERTA_PRINCIPAL": IndividuoAcceso(
        nombre="PUERTA_PRINCIPAL",
        lemas=frozenset({"puerta", "entrada", "principal", "acceso"}),
        estado_inicial=ESTADO_CERRADO,
        requiere_pin=False,
    ),
    "PORTON_COCHERA": IndividuoAcceso(
        nombre="PORTON_COCHERA",
        lemas=frozenset({"portón", "cochera", "garaje", "reja", "zaguán"}),
        estado_inicial=ESTADO_CERRADO,
        requiere_pin=False,
    ),
    "CAMARA_EXTERIOR": IndividuoAcceso(
        nombre="CAMARA_EXTERIOR",
        lemas=frozenset({"cámara", "timbre", "exterior", "calle"}),
        estado_inicial=ESTADO_ACTIVO,
        requiere_pin=False,
    ),
}

# FastAPI puede atender varias peticiones a la vez: el estado se lee y se
# escribe siempre bajo este cerrojo.
ESTADO_LOCK = RLock()


def reiniciar_estados() -> None:
    """Devuelve todos los individuos a su ``estado_inicial`` (útil en pruebas)."""
    with ESTADO_LOCK:
        for punto in PUNTOS_ACCESO.values():
            punto.estado = punto.estado_inicial


def estado_de(individuo: str) -> Optional[str]:
    with ESTADO_LOCK:
        punto = PUNTOS_ACCESO.get(individuo)
        return punto.estado if punto else None


def estados_actuales() -> Dict[str, str]:
    with ESTADO_LOCK:
        return {nombre: punto.estado for nombre, punto in PUNTOS_ACCESO.items()}


def descripcion_estado(individuo: Optional[str], estado: Optional[str]) -> str:
    """Texto con género coherente con el individuo para las respuestas TTS."""
    if estado == ESTADO_ABIERTO:
        return "abierta" if individuo == "PUERTA_PRINCIPAL" else "abierto"
    if estado == ESTADO_CERRADO:
        return "cerrada" if individuo == "PUERTA_PRINCIPAL" else "cerrado"
    if estado == ESTADO_ACTIVO:
        return "activa" if individuo == "CAMARA_EXTERIOR" else "activo"
    return str(estado or "desconocido").lower()


def aplicar_transicion(individuo: str, estado: str) -> bool:
    with ESTADO_LOCK:
        punto = PUNTOS_ACCESO.get(individuo)
        if punto is None:
            return False
        punto.estado = estado
        return True


def _respuesta(
    estado: str,
    *,
    accion: Optional[str] = None,
    individuo: Optional[str] = None,
    cmd: str = "NONE",
    intent: str = "DESCONOCIDO",
    tts: str = _TTS_FUERA_DOMINIO,
    confidence: float = 0.0,
    coincidencias: int = 0,
    estado_actual: Optional[str] = None,
    estado_objetivo: Optional[str] = None,
    requiere_pin: bool = False,
    debe_ejecutar: bool = False,
    razon: str = "",
    detalle_base: Optional[dict] = None,
) -> dict:
    detalle = dict(detalle_base or {})
    detalle["razon"] = razon
    return {
        "estado": estado,
        "accion": accion,
        "individuo": individuo,
        "cmd": cmd,
        "intent": intent,
        "tts": tts,
        "confidence": round(confidence, 2),
        "coincidencias": coincidencias,
        "estado_actual": estado_actual,
        "estado_objetivo": estado_objetivo,
        "requiere_pin": requiere_pin,
        "debe_ejecutar": debe_ejecutar,
        "detalle": detalle,
    }


def inferir_intencion(analisis_sintactico: dict) -> dict:
    """Resuelve la intención de dominio a partir del análisis sintáctico.

    No muta estado: valida axiomas y decide si el actuador debe ejecutar.
    """
    lema = str(analisis_sintactico.get("lema_accion") or "").lower().strip()
    lemas_objetivo = set(analisis_sintactico.get("lemas_objetivo") or [])
    detalle = {"lema_accion": lema, "lemas_objetivo": sorted(lemas_objetivo)}

    if not analisis_sintactico.get("valido"):
        return _respuesta(
            ESTADO_FUERA_DOMINIO,
            tts=_TTS_FUERA_DOMINIO,
            razon="texto sin núcleo verbal",
            detalle_base=detalle,
        )

    if analisis_sintactico.get("negado"):
        return _respuesta(
            ESTADO_ACCION_NEGADA,
            cmd="REJECT",
            intent="RECHAZAR",
            tts=_TTS_NEGADA,
            confidence=0.95,
            razon="negación detectada por advmod",
            detalle_base=detalle,
        )

    accion = AccionAcceso.desde_lema(lema)
    if accion is None:
        return _respuesta(
            ESTADO_FUERA_DOMINIO,
            tts=_TTS_FUERA_DOMINIO,
            razon=f"lema '{lema}' no pertenece a AccionAcceso",
            detalle_base=detalle,
        )

    with ESTADO_LOCK:
        panorama = {
            nombre: {
                "estado": punto.estado,
                "lemas": set(punto.lemas),
                "requiere_pin": punto.requiere_pin,
            }
            for nombre, punto in PUNTOS_ACCESO.items()
        }

    mejor: Optional[str] = None
    mejor_coincidencias = 0
    for nombre, datos in panorama.items():
        coincidencias = len(lemas_objetivo & datos["lemas"])
        if coincidencias > mejor_coincidencias:
            mejor = nombre
            mejor_coincidencias = coincidencias

    if mejor is None:
        return _respuesta(
            ESTADO_FUERA_DOMINIO,
            accion=accion.name,
            tts=_TTS_FUERA_DOMINIO,
            razon="sin intersección con los lemas de PuntoAcceso",
            detalle_base=detalle,
        )

    datos = panorama[mejor]
    estado_actual = datos["estado"]
    requiere_pin = datos["requiere_pin"]

    if requiere_pin:
        return _respuesta(
            ESTADO_REQUIERE_PIN,
            accion=accion.name,
            individuo=mejor,
            tts=f"Se requiere PIN para operar {mejor}.",
            confidence=0.9,
            coincidencias=mejor_coincidencias,
            estado_actual=estado_actual,
            requiere_pin=True,
            razon="el individuo exige verificación adicional",
            detalle_base=detalle,
        )

    if accion is AccionAcceso.ABRIR:
        estado_objetivo = ESTADO_ABIERTO
    elif accion is AccionAcceso.CERRAR:
        estado_objetivo = ESTADO_CERRADO
    else:
        estado_objetivo = estado_actual

    if accion is not AccionAcceso.CONSULTAR and estado_actual == estado_objetivo:
        return _respuesta(
            ESTADO_REDUNDANTE,
            accion=accion.name,
            individuo=mejor,
            intent=_INTENT[accion.name],
            tts=f"{mejor} ya está {descripcion_estado(mejor, estado_actual)}.",
            confidence=0.95,
            coincidencias=mejor_coincidencias,
            estado_actual=estado_actual,
            estado_objetivo=estado_objetivo,
            razon="axioma de estado: ya está en el estado destino",
            detalle_base=detalle,
        )

    return _respuesta(
        ESTADO_LISTO,
        accion=accion.name,
        individuo=mejor,
        cmd=_CMD[accion.name],
        intent=_INTENT[accion.name],
        tts=_TTS[accion.name],
        confidence=0.99,
        coincidencias=mejor_coincidencias,
        estado_actual=estado_actual,
        estado_objetivo=estado_objetivo,
        debe_ejecutar=accion is not AccionAcceso.CONSULTAR,
        razon="intención resuelta en la ontología",
        detalle_base=detalle,
    )
