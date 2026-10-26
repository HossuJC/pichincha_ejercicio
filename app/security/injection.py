import re

# Instruction-like phrasing addressed to automated systems. Heuristic only: the real
# defence is that provider text is labelled as untrusted data and that the guard
# and escalation rules are enforced in code, not by the model.
_PATTERNS = [
    r"sistemas? automatizad",
    r"\b(ignora|ignore|olvida|disregard)\b",
    r"\binstrucci[oó]n",
    r"\bmarcar? (cualquier|todo|el|los)\b.*\breclamo",
    r"\bno escal",
    r"\b(asistente|assistant|system prompt|modelo de lenguaje|llm|ia)\b",
    r"\b(aprobar|rechazar|denegar)\b.*\breclamo",
]
_REGEX = re.compile("|".join(_PATTERNS), re.IGNORECASE)


def looks_like_injection(text: str | None) -> bool:
    return bool(text) and bool(_REGEX.search(text))
