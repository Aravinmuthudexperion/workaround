"""ARC Tower web application factory."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from arc_tower import __version__
from arc_tower.core.config import get_settings
from arc_tower.core.logging import configure_logging
from arc_tower.services.errors import ServiceError

from . import grafana_proxy
from .api.v1.router import router as api_v1_router
from .deps import LoginRequired
from .routes import (admin, agents, alerts, auth, connectors, dashboards, executive, home, layouts, onboarding,
                     products, system, traces)
from .templating import render


def create_app() -> FastAPI:
    configure_logging("arc-app")
    settings = get_settings()
    app = FastAPI(title="ARC Tower", version=__version__, docs_url="/api/docs", redoc_url=None,
                  openapi_url="/api/openapi.json")
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, session_cookie="arc_session",
                       same_site="lax", https_only=settings.base_url.startswith("https"), max_age=12 * 3600)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    for module in (system, auth, home, layouts, products, onboarding, connectors, agents, dashboards, traces, alerts,
                   executive, admin):
        app.include_router(module.router)
    app.include_router(api_v1_router, prefix="/api/v1")
    app.include_router(grafana_proxy.router)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/api/") or request.headers.get("hx-request"):
            return JSONResponse({"detail": "login required"}, status_code=401,
                                headers={"HX-Redirect": "/login"})
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)
        return render(request, "error.html", status_code=exc.status_code, message=str(exc),
                      code=exc.status_code)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return render(request, "error.html", status_code=exc.status_code, message=exc.detail, code=exc.status_code)

    return app


app = create_app()
