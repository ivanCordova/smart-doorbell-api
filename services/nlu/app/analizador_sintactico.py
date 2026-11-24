"""Capa sintáctica: dependencias, lemas y negación con spaCy.

Se carga una única instancia global del pipeline. En español las órdenes
suelen omitir el sujeto ("Abre la puerta"), por lo que **no** se exige
``nsubj`` para considerar válida una instrucción.
"""

from typing import Dict, List, Set

import spacy

nlp = spacy.load("es_core_news_sm")

LEMAS_NEGACION = {"no", "nunca", "jamas", "jamás"}
DEPS_OBJETIVO = {"obj", "obl", "nmod"}
POS_OBJETIVO = {"NOUN", "PROPN", "ADJ"}

# es_core_news_sm lematiza el subjuntivo "abras" como "abra" y lo marca ADJ.
LEMAS_BASE = {"abra": "abrir", "abran": "abrir", "abraos": "abrir"}


def _lema(token) -> str:
    lema = token.lemma_.lower().strip()
    return LEMAS_BASE.get(lema, lema)


def _debug_token(token) -> dict:
    return {
        "texto": token.text,
        "lema": _lema(token),
        "pos": token.pos_,
        "dep": token.dep_,
        "head": token.head.text,
    }


def _raiz(doc) -> object:
    """Localiza el núcleo verbal (ROOT o verbo principal de una perífrasis)."""
    for token in doc:
        if token.pos_ == "VERB" and token.dep_ == "ROOT":
            return token

    # El modelo español marca "No abras..." como ADJ/ROOT. Aceptamos también
    # ROOT con AUX/ADJ si rige un objeto o lleva una negación: es una orden
    # en modo subjuntivo, no una oración casual.
    for token in doc:
        if token.dep_ != "ROOT" or token.pos_ not in {"VERB", "AUX", "ADJ"}:
            continue
        hijos = list(token.children)
        if any(h.dep_ in DEPS_OBJETIVO for h in hijos) or any(
            h.dep_ == "advmod" and _lema(h) in LEMAS_NEGACION for h in hijos
        ):
            return token
        for hijo in hijos:
            if hijo.pos_ == "VERB":
                return hijo
        for candidato in token.subtree:
            if candidato.pos_ == "VERB":
                return candidato
    return None


def _negado(token) -> bool:
    """Detecta ``advmod`` de negación, incluso si cuelga de un auxiliar."""
    for hijo in token.children:
        if hijo.dep_ == "advmod" and _lema(hijo) in LEMAS_NEGACION:
            return True

    for t in token.subtree:
        if t is token or t.pos_ not in {"AUX", "VERB"}:
            continue
        for hijo in t.children:
            if hijo.dep_ == "advmod" and _lema(hijo) in LEMAS_NEGACION:
                return True
    return False


def _lemas_objetivo(token) -> Set[str]:
    """Recoge lemas de NOUN/PROPN/ADJ bajo obj, obl y nmod.

    ``nmod`` se incluye porque el modelo español etiqueta los complementos
    introducidos por "de" (ej. "de la cochera") como ``nmod`` y no ``obl``.
    """
    lemas: Set[str] = set()
    for hijo in token.children:
        if hijo.dep_ not in DEPS_OBJETIVO:
            continue
        for t in hijo.subtree:
            if t.pos_ in POS_OBJETIVO:
                lemas.add(_lema(t))
    return lemas


def analizar(texto: str) -> Dict[str, object]:
    """Analiza un texto ya normalizado y devuelve la estructura de intención."""
    doc = nlp(texto or "")
    raiz = _raiz(doc)

    if raiz is None:
        return {
            "valido": False,
            "lema_accion": "",
            "lemas_objetivo": set(),
            "negado": False,
            "tokens_debug": [_debug_token(t) for t in doc],
        }

    return {
        "valido": bool(doc.text.strip()),
        "lema_accion": _lema(raiz),
        "lemas_objetivo": _lemas_objetivo(raiz),
        "negado": _negado(raiz),
        "tokens_debug": [_debug_token(t) for t in doc],
    }
