"""Punto de entrada y orquestación del pipeline PLN de 4 capas.

Flujo: ASR (externo) -> LÉXICO -> SINTÁCTICO -> ONTOLOGÍA -> ACCIÓN.
También funciona como runner de los casos de aceptación en consola:

    python -m app.main --verificar
"""

import json
import sys
from typing import Dict, List, Optional

from app import ontologia
from app.actuador import ejecutar_accion
from app.analizador_sintactico import analizar
from app.intents import _frase_consulta as frase_consulta
from app.intents import interpretar as interpretar_legacy
from app.ontologia import inferir_intencion
from app.preprocesador import limpiar_texto


def _analisis_serializable(analisis: dict) -> dict:
    return {
        "valido": analisis.get("valido", False),
        "lema_accion": analisis.get("lema_accion", ""),
        "lemas_objetivo": sorted(analisis.get("lemas_objetivo") or []),
        "negado": analisis.get("negado", False),
        "tokens_debug": analisis.get("tokens_debug", []),
    }


def _resumen(respuesta: dict) -> dict:
    return {
        "estado": respuesta.get("estado"),
        "accion": respuesta.get("accion"),
        "individuo": respuesta.get("individuo"),
        "cmd": respuesta.get("cmd"),
        "coincidencias": respuesta.get("coincidencias"),
    }


def clasificar_texto(
    texto: str,
    personas: Optional[List[str]] = None,
    ultima_deteccion: Optional[List[str]] = None,
    trazar: bool = True,
) -> dict:
    """Ejecuta las capas léxica, sintáctica y semántica. No mueve hardware."""
    limpio = limpiar_texto(texto)
    if trazar:
        print(f"[LÉXICO] {texto!r} -> {limpio!r}")

    analisis = analizar(limpio)
    if trazar:
        print(f"[SINTÁCTICO] {json.dumps(_analisis_serializable(analisis), ensure_ascii=False)}")

    respuesta = inferir_intencion(analisis)
    respuesta["texto_normalizado"] = limpio
    respuesta["debug"] = {
        "analisis": _analisis_serializable(analisis),
        "inferencia": respuesta.pop("detalle", {}),
    }
    respuesta["pipeline"] = ["LÉXICO", "SINTÁCTICO", "ONTOLOGÍA"]
    if trazar:
        print(f"[ONTOLOGÍA] {json.dumps(_resumen(respuesta), ensure_ascii=False)}")

    # Puente de compatibilidad: conserva frases del prototipo que la ontología
    # nueva no modela ("pasa", "adelante", "quién hay", etc.).
    if respuesta["estado"] == ontologia.ESTADO_FUERA_DOMINIO:
        legacy = interpretar_legacy(
            texto or limpio,
            personas=personas,
            ultima_deteccion=ultima_deteccion,
        )
        if legacy["intent"] != "DESCONOCIDO":
            if trazar:
                print(f"[COMPATIBILIDAD] {legacy['intent']} ({legacy['cmd']})")
            respuesta.update(
                {
                    "estado": "COMPATIBILIDAD",
                    "accion": None,
                    "individuo": None,
                    "intent": legacy["intent"],
                    "cmd": legacy["cmd"],
                    "tts": legacy["tts"],
                    "confidence": legacy.get("confidence", 0.0),
                    "coincidencias": 0,
                    "estado_actual": None,
                    "estado_objetivo": None,
                    "requiere_pin": False,
                    "debe_ejecutar": False,
                }
            )
            respuesta["pipeline"] = respuesta["pipeline"] + ["COMPATIBILIDAD"]

    if respuesta.get("cmd") == "CHECK":
        respuesta["tts"] = frase_consulta(personas, ultima_deteccion)

    return respuesta


def procesar_texto(
    texto: str,
    personas: Optional[List[str]] = None,
    ultima_deteccion: Optional[List[str]] = None,
    trazar: bool = True,
) -> dict:
    """Orquesta las 4 capas y devuelve la respuesta para voice/CLI."""
    respuesta = clasificar_texto(
        texto,
        personas=personas,
        ultima_deteccion=ultima_deteccion,
        trazar=trazar,
    )
    respuesta["ejecutada"] = False

    if respuesta.get("debe_ejecutar"):
        metadatos = {
            "coincidencias": respuesta.get("coincidencias"),
            "estado_actual": respuesta.get("estado_actual"),
            "estado_objetivo": respuesta.get("estado_objetivo"),
            "confidence": respuesta.get("confidence"),
        }
        respuesta["ejecutada"] = ejecutar_accion(
            respuesta["accion"], respuesta["individuo"], metadatos
        )
        if not respuesta["ejecutada"]:
            # Defensivo: si otra petición cambió el estado entre inferencia
            # y ejecución, no se envía cmd que dispare hardware.
            estado = ontologia.estado_de(respuesta["individuo"])
            respuesta["cmd"] = "NONE"
            respuesta["tts"] = (
                f"{respuesta['individuo']} ya está "
                f"{ontologia.descripcion_estado(respuesta['individuo'], estado)}."
            )
    return respuesta


def verificar() -> int:
    """Corre los casos de aceptación de la especificación."""
    fallos: List[str] = []

    def caso(
        nombre: str,
        texto: str,
        esperado: Dict[str, object],
        estado: Optional[str] = None,
        individuo: Optional[str] = None,
    ) -> dict:
        antes = len(fallos)
        respuesta = procesar_texto(texto, trazar=True)
        for clave, valor in esperado.items():
            actual = respuesta.get(clave)
            if actual != valor:
                fallos.append(f"{nombre}: {clave}={actual!r} (esperado {valor!r})")
        if estado is not None:
            objetivo = individuo or respuesta.get("individuo")
            actual_estado = ontologia.estado_de(objetivo)
            if actual_estado != estado:
                fallos.append(
                    f"{nombre}: estado de {objetivo}={actual_estado!r} (esperado {estado!r})"
                )
        marca = "✅" if len(fallos) == antes else "❌"
        print(f"{marca} {nombre}: {texto!r} -> {_resumen(respuesta)}")
        return respuesta

    ontologia.reiniciar_estados()
    caso(
        "1. Imperativo directo",
        "Abre la puerta",
        {
            "accion": "ABRIR",
            "individuo": "PUERTA_PRINCIPAL",
            "estado": ontologia.ESTADO_LISTO,
            "cmd": "OPEN",
            "ejecutada": True,
        },
        estado=ontologia.ESTADO_ABIERTO,
    )
    caso(
        "5a. Cerrar (primera vez)",
        "Cierra la puerta",
        {
            "accion": "CERRAR",
            "individuo": "PUERTA_PRINCIPAL",
            "estado": ontologia.ESTADO_LISTO,
            "cmd": "REJECT",
            "ejecutada": True,
        },
        estado=ontologia.ESTADO_CERRADO,
    )
    redundante = caso(
        "5b. Cerrar (ya cerrada)",
        "Cierra la puerta",
        {"estado": ontologia.ESTADO_REDUNDANTE, "cmd": "NONE", "ejecutada": False},
        estado=ontologia.ESTADO_CERRADO,
    )
    if "ya está cerrada" not in redundante.get("tts", ""):
        fallos.append(f"5b: TTS inesperado {redundante.get('tts')!r}")

    ontologia.reiniciar_estados()
    caso(
        "2. Morfología y sinónimos",
        "Por favor desbloquea el zaguán de la cochera",
        {
            "accion": "ABRIR",
            "individuo": "PORTON_COCHERA",
            "estado": ontologia.ESTADO_LISTO,
            "cmd": "OPEN",
            "ejecutada": True,
        },
        estado=ontologia.ESTADO_ABIERTO,
    )

    ontologia.reiniciar_estados()
    negada = caso(
        "3. Negación",
        "No abras el portón",
        {
            "estado": ontologia.ESTADO_ACCION_NEGADA,
            "cmd": "REJECT",
            "ejecutada": False,
        },
        individuo="PORTON_COCHERA",
        estado=ontologia.ESTADO_CERRADO,
    )
    if negada["debug"]["analisis"]["negado"] is not True:
        fallos.append("3: la capa sintáctica no marcó negado=True")

    caso(
        "4. Ruido / conversación casual",
        "Hoy hace mucho calor afuera",
        {"estado": ontologia.ESTADO_FUERA_DOMINIO, "cmd": "NONE", "ejecutada": False},
    )
    caso(
        "Compatibilidad: consulta",
        "quién hay",
        {"cmd": "CHECK", "estado": "COMPATIBILIDAD"},
    )

    if fallos:
        print(f"\n❌ {len(fallos)} fallo(s):")
        for fallo in fallos:
            print(f"  - {fallo}")
        return 1
    print("\n✅ Todos los casos de aceptación pasaron.")
    return 0


if __name__ == "__main__":
    if "--verificar" in sys.argv:
        raise SystemExit(verificar())
    frases = [argumento for argumento in sys.argv[1:] if argumento != "--verificar"]
    if not frases:
        print("Uso: python -m app.main --verificar | python -m app.main 'abre la puerta'")
        raise SystemExit(0)
    for frase in frases:
        procesar_texto(frase)
