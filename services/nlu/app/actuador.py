"""Capa de ejecución: desacopla PLN de la acción física o simulada.

El estado de la cerradura vive en ``ontologia``; aquí solo se aplica la
transición, se imprime una salida estructurada y se deja un hook para
integrar RPi.GPIO, pyserial o paho-mqtt en el futuro.
"""

import json
from typing import Dict, Optional

from app import ontologia

ESTADOS_POR_ACCION = {"ABRIR": ontologia.ESTADO_ABIERTO, "CERRAR": ontologia.ESTADO_CERRADO}


def _hook_hardware(accion: str, individuo: str, metadatos: dict) -> None:
    """Punto de extensión para hardware real. No hace nada en simulación."""
    return None


def _imprimir(salida: dict) -> None:
    print(f"[ACCIÓN] {json.dumps(salida, ensure_ascii=False, sort_keys=True)}")


def ejecutar_accion(accion: str, individuo: str, metadatos: dict) -> bool:
    """Aplica la transición de estado y notifica la acción.

    Devuelve ``True`` si la acción se ejecutó (o no requiere ejecución) y
    ``False`` si se bloqueó por redundancia, negación o dato inválido.
    """
    metadatos = metadatos or {}

    if accion == "CONSULTAR":
        salida = {
            "accion": accion,
            "individuo": individuo,
            "estado_anterior": ontologia.estado_de(individuo),
            "estado_nuevo": None,
            "disparar_hardware": False,
            "metadatos": metadatos,
        }
        _imprimir(salida)
        _hook_hardware(accion, individuo, metadatos)
        return True

    estado_destino: Optional[str] = ESTADOS_POR_ACCION.get(accion)
    if estado_destino is None:
        _imprimir(
            {
                "accion": accion,
                "individuo": individuo,
                "disparar_hardware": False,
                "razon": "acción no ejecutable",
                "metadatos": metadatos,
            }
        )
        return False

    estado_anterior = ontologia.estado_de(individuo)
    if estado_anterior is None:
        _imprimir(
            {
                "accion": accion,
                "individuo": individuo,
                "disparar_hardware": False,
                "razon": "individuo desconocido",
                "metadatos": metadatos,
            }
        )
        return False

    if estado_anterior == estado_destino:
        _imprimir(
            {
                "accion": accion,
                "individuo": individuo,
                "estado_anterior": estado_anterior,
                "estado_nuevo": estado_anterior,
                "disparar_hardware": False,
                "razon": "ya está en el estado destino",
                "metadatos": metadatos,
            }
        )
        return False

    ontologia.aplicar_transicion(individuo, estado_destino)
    salida: Dict[str, object] = {
        "accion": accion,
        "individuo": individuo,
        "estado_anterior": estado_anterior,
        "estado_nuevo": estado_destino,
        "disparar_hardware": True,
        "metadatos": metadatos,
    }
    _imprimir(salida)
    _hook_hardware(accion, individuo, metadatos)
    return True
