"""Reverse proxy for Grafana under /grafana/, signing the ARC user in via auth-proxy headers.

Grafana trusts X-WEBAUTH-USER only from 127.0.0.1 (see grafana.ini), and this proxy strips
any X-WEBAUTH-* header the browser sends, so users cannot impersonate each other.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask

from arc_tower.core.config import GRAFANA_URL
from arc_tower.db.models import User
from arc_tower.db.session import session_scope

from . import session as web_session

router = APIRouter()

_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
               "transfer-encoding", "upgrade", "host", "content-length"}
_client = httpx.AsyncClient(base_url=GRAFANA_URL, timeout=httpx.Timeout(60.0))


def _user(request: Request) -> tuple[str, str] | None:
    uid = web_session.user_id(request)
    if not uid:
        return None
    with session_scope() as session:
        user = session.get(User, uid)
        return (user.email, user.name) if user and user.active else None


@router.api_route("/grafana/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                  include_in_schema=False)
async def grafana(path: str, request: Request):
    who = _user(request)
    if who is None:
        return RedirectResponse(f"/login?next=/grafana/{path}", status_code=303)
    # Forward raw header bytes: Grafana sends non-ASCII values (e.g. X-Dashboard-Title with "·"),
    # which must pass through unchanged rather than be re-encoded.
    headers = [(k, v) for k, v in request.headers.raw
               if k.decode("latin-1").lower() not in _HOP_BY_HOP and not k.lower().startswith(b"x-webauth")]
    headers += [(b"x-webauth-user", who[0].encode()), (b"x-webauth-email", who[0].encode()),
                (b"x-webauth-name", (who[1] or who[0]).encode())]
    upstream_request = _client.build_request(request.method, f"/grafana/{path}", params=request.query_params,
                                             headers=headers, content=await request.body())
    upstream = await _client.send(upstream_request, stream=True)
    response = StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                                 background=BackgroundTask(upstream.aclose))
    # multi_items() keeps repeated headers (Grafana sets several cookies). Raw bytes are passed
    # through unchanged, so Content-Encoding (gzip) is kept as well.
    response.raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1"))
                            for k, v in upstream.headers.multi_items() if k.lower() not in _HOP_BY_HOP]
    return response


def grafana_url(path: str, org_id: int | None, **params: str | None) -> str:
    """Link into Grafana. None values are skipped; "" adds a bare flag (e.g. kiosk)."""
    parts = [f"orgId={org_id or 1}"]
    parts += [k if v == "" else f"{k}={v}" for k, v in params.items() if v is not None]
    return f"/grafana/{path.lstrip('/')}?{'&'.join(parts)}"
