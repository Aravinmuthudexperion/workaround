"""Helpers shared by platform plugins."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone


def id_bytes(value: str, size: int) -> bytes:
    """Platform ids -> OTel trace (16 bytes) / span (8 bytes) ids. Hex ids are used as-is, others hashed."""
    cleaned = (value or "").replace("-", "").lower()
    if len(cleaned) == size * 2:
        try:
            return bytes.fromhex(cleaned)
        except ValueError:
            pass
    return hashlib.sha256((value or "").encode()).digest()[:size]


def parse_time(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.fromisoformat(text) if fmt is None else datetime.strptime(text, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def as_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
