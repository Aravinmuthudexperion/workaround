"""Sign-in (password or OIDC SSO), sign-out, and the tenant switcher."""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from arc_tower.core.config import get_settings
from arc_tower.services import access, users

from .. import session as web_session
from ..deps import current_scope, get_db
from ..templating import render

log = logging.getLogger(__name__)
router = APIRouter()


def _safe_next(target: str | None) -> str:
    """Only allow local redirects after login."""
    if not target or urlparse(target).netloc or not target.startswith("/"):
        return "/"
    return target


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    return render(request, "login.html", next=_safe_next(next), oidc=get_settings().oidc_enabled)


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/"),
          db: Session = Depends(get_db)):
    user = users.authenticate(db, email, password)
    if user is None:
        return render(request, "login.html", status_code=401, next=_safe_next(next),
                      oidc=get_settings().oidc_enabled, error="Wrong e-mail or password.", email=email)
    web_session.login(request, user.id)
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/logout", dependencies=[Depends(web_session.verify_csrf)])
def logout(request: Request):
    web_session.logout(request)
    return RedirectResponse("/login", status_code=303)


@router.post("/tenant/select", dependencies=[Depends(web_session.verify_csrf)])
def select_tenant(request: Request, tenant: str = Form(...), back: str = Form("/"),
                  db: Session = Depends(get_db), scope: access.Scope = Depends(current_scope)):
    if tenant == web_session.ALL_TENANTS:
        web_session.select_tenant(request, web_session.ALL_TENANTS)
    else:
        access.get_tenant(db, scope, int(tenant))       # raises if not visible
        web_session.select_tenant(request, int(tenant))
    return RedirectResponse(_safe_next(back), status_code=303)


# --- OIDC single sign-on (e.g. Entra ID) --------------------------------------

def _oauth():
    from authlib.integrations.starlette_client import OAuth  # optional dependency: pip install .[oidc]
    settings = get_settings()
    oauth = OAuth()
    oauth.register("sso", server_metadata_url=f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration",
                   client_id=settings.oidc_client_id, client_secret=settings.oidc_client_secret,
                   client_kwargs={"scope": "openid email profile"})
    return oauth


@router.get("/auth/oidc/login")
async def oidc_login(request: Request):
    if not get_settings().oidc_enabled:
        return RedirectResponse("/login", status_code=303)
    return await _oauth().sso.authorize_redirect(request, f"{get_settings().base_url}/auth/oidc/callback")


@router.get("/auth/oidc/callback")
async def oidc_callback(request: Request, db: Session = Depends(get_db)):
    token = await _oauth().sso.authorize_access_token(request)
    claims = token.get("userinfo") or {}
    email = (claims.get("email") or claims.get("preferred_username") or "").lower()
    user = users.get_by_email(db, email) if email else None
    if user is None or not user.active:
        # SSO proves identity; access still requires an ARC account created by an admin.
        log.warning("SSO login for unknown user %s", email)
        return render(request, "login.html", status_code=403, next="/", oidc=True,
                      error=f"{email or 'This account'} has no ARC Tower access yet. Ask a platform admin.")
    web_session.login(request, user.id)
    return RedirectResponse("/", status_code=303)
