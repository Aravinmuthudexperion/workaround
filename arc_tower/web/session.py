"""Session-cookie helpers: signed-in user, selected tenant, flash messages, CSRF token."""
from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request

_USER = "uid"
_TENANT = "tenant"
_FLASH = "flash"
_CSRF = "csrf"
ALL_TENANTS = "all"


def login(request: Request, user_id: int) -> None:
    request.session.clear()
    request.session[_USER] = user_id
    request.session[_CSRF] = secrets.token_urlsafe(24)


def logout(request: Request) -> None:
    request.session.clear()


def user_id(request: Request) -> int | None:
    return request.session.get(_USER)


def selected_tenant(request: Request) -> int | str | None:
    return request.session.get(_TENANT)


def select_tenant(request: Request, tenant: int | str | None) -> None:
    request.session[_TENANT] = tenant


def flash(request: Request, message: str, level: str = "success") -> None:
    request.session.setdefault(_FLASH, []).append({"message": message, "level": level})


def pop_flashes(request: Request) -> list[dict]:
    return request.session.pop(_FLASH, [])


def csrf_token(request: Request) -> str:
    token = request.session.get(_CSRF)
    if not token:
        token = request.session[_CSRF] = secrets.token_urlsafe(24)
    return token


async def verify_csrf(request: Request) -> None:
    """Dependency for every state-changing route: token from the form field or the X-CSRF-Token header."""
    expected = request.session.get(_CSRF, "")
    sent = request.headers.get("x-csrf-token")
    if not sent:
        form = await request.form()
        sent = str(form.get("csrf_token", ""))
    if not expected or not hmac.compare_digest(expected, sent):
        raise HTTPException(status_code=403, detail="Your session expired - reload the page and try again.")
