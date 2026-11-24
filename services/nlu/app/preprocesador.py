"""Capa léxica: sanitización determinista del texto reconstruido por ASR.

Todos los patrones se compilan una sola vez a nivel de módulo para no pagar
el coste de ``re.compile`` en cada fotograma de audio procesado en tiempo real.
"""

import re

# ``\w`` incluye acentos y ñ en Python 3 (Unicode), a diferencia de [a-z].
RE_NO_ALFANUMERICO = re.compile(r"[^\w\s]", re.UNICODE)
RE_MULETILLAS = re.compile(
    r"\b(?:oye|por favor|porfa|porfavor|este|eh|disculpa|puedes)\b",
    re.IGNORECASE,
)
RE_ESPACIOS = re.compile(r"\s+")


def limpiar_texto(texto_crudo: str) -> str:
    """Normaliza una transcripción eliminando ruido conversacional.

    Las palabras de negación ("no", "nunca", "jamás") se conservan
    deliberadamente: la capa sintáctica es la encargada de detectarlas.
    """
    texto = (texto_crudo or "").lower()
    texto = RE_NO_ALFANUMERICO.sub(" ", texto)
    texto = RE_MULETILLAS.sub(" ", texto)
    return RE_ESPACIOS.sub(" ", texto).strip()
