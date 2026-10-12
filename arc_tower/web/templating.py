"""Jinja environment with ARC filters and the per-request base context."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from arc_tower import __version__
from arc_tower.core.config import get_settings
from arc_tower.utils.text import humanize_number
from arc_tower.utils.timeutil import ago

from . import session as web_session

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_STATIC = Path(__file__).parent / "static"


def _asset_version() -> str:
    """Changes whenever a CSS/JS file changes, so browsers never run stale assets."""
    newest = max((f.stat().st_mtime for f in _STATIC.rglob("*") if f.suffix in (".css", ".js")), default=0)
    return f"{__version__}-{int(newest)}"


def _money(value: float | None, digits: int = 2) -> str:
    return "–" if value is None else f"${value:,.{digits}f}"


def _fmt(value: float | None, unit: str = "") -> str:
    if value is None:
        return "–"
    if unit == "USD":
        return _money(value, 3 if abs(value) < 1 else 2)
    if unit in ("%",):
        return f"{value:.1f}%"
    if unit == "ms":
        return f"{value / 1000:.2f}s" if value >= 1000 else f"{value:.0f}ms"
    if unit in ("0-1", "ratio", "z"):
        return f"{value:.2f}"
    # Counters come from Prometheus increase(), which extrapolates; counts are whole numbers.
    return humanize_number(round(value))


def _count(value: float | None) -> str:
    return humanize_number(None if value is None else round(value))


templates.env.filters.update(ago=ago, money=_money, fmt=_fmt, num=_count, tojson_safe=lambda v: json.dumps(v))
templates.env.globals.update(version=__version__, asset_version=_asset_version())


def render(request: Request, name: str, status_code: int = 200, **context):
    settings = get_settings()
    base = {
        "request": request,
        "csrf_token": web_session.csrf_token(request),
        "flashes": web_session.pop_flashes(request),
        "settings": settings,
    }
    return templates.TemplateResponse(request, name, {**base, **context}, status_code=status_code)
