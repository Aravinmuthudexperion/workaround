"""String helpers."""
from __future__ import annotations

import re
import unicodedata

_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(value: str, max_length: int = 48) -> str:
    """'Claims Assistant (Prod)' -> 'claims-assistant-prod'. Safe as a Prometheus/Loki label value."""
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = _NON_SLUG.sub("-", normalized.lower()).strip("-")
    return slug[:max_length].rstrip("-") or "item"


def humanize_number(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "–"
    value = float(value)
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(value) >= size:
            return f"{value / size:.{digits}f}{unit}"
    return f"{value:.{digits}f}".rstrip("0").rstrip(".") if value % 1 else str(int(value))
