import re

# 13-19 digits, optionally grouped with spaces or dashes. Every candidate is masked,
# Luhn-valid or not: customers mistype cards and a near-PAN is still sensitive.
_PAN = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def mask_pan(text: str) -> tuple[str, str | None]:
    """Masks card numbers. Returns (masked_text, last4 of the last card seen)."""
    last4: str | None = None

    def _replace(match: re.Match) -> str:
        nonlocal last4
        digits = re.sub(r"\D", "", match.group(0))
        last4 = digits[-4:]
        return f"[tarjeta terminada en {last4}]"

    return _PAN.sub(_replace, text), last4
