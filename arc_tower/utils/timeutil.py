"""Time helpers. All timestamps inside ARC Tower are timezone-aware UTC."""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def to_unix_nano(value: datetime) -> int:
    return int(as_utc(value).timestamp() * 1_000_000_000)


def ago(value: datetime | None, now: datetime | None = None) -> str:
    if value is None:
        return "never"
    seconds = int(((now or utcnow()) - as_utc(value)).total_seconds())
    if seconds < 60:
        return f"{max(seconds, 0)}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"
