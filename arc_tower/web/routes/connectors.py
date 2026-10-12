"""Connector catalog and the status of configured pull connectors."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from arc_tower.connectors import catalog
from arc_tower.db.models import Connector
from arc_tower.services import access, audit
from arc_tower.services import connectors as connector_service
from arc_tower.services.access import Scope
from arc_tower.services.errors import NotFound

from .. import session as web_session
from ..deps import TenantContext, current_scope, get_db, tenant_context
from ..templating import render

router = APIRouter()


def _connector(db: Session, scope: Scope, connector_id: int) -> Connector:
    connector = db.get(Connector, connector_id)
    if connector is None:
        raise NotFound("connector not found")
    access.get_product(db, scope, connector.product_id, edit=True)
    return connector


@router.get("/connectors")
def connector_page(request: Request, ctx: TenantContext = Depends(tenant_context)):
    configured = [(p, c) for t in ctx.active for p in t.products for c in p.connectors]
    push_products = [p for t in ctx.active for p in t.products if p.is_push]
    return render(request, "connectors.html", ctx=ctx, categories=catalog.by_category(), configured=configured,
                  push_products=push_products)


@router.post("/connectors/{connector_id}/run", dependencies=[Depends(web_session.verify_csrf)])
def run_now(request: Request, connector_id: int, db: Session = Depends(get_db),
            scope: Scope = Depends(current_scope)):
    connector = _connector(db, scope, connector_id)
    connector_service.request_run(connector)
    web_session.flash(request, "Queued - the worker picks it up within a few seconds.")
    return RedirectResponse(request.headers.get("referer") or "/connectors", status_code=303)


@router.post("/connectors/{connector_id}/toggle", dependencies=[Depends(web_session.verify_csrf)])
def toggle(request: Request, connector_id: int, db: Session = Depends(get_db), scope: Scope = Depends(current_scope)):
    connector = _connector(db, scope, connector_id)
    connector.enabled = not connector.enabled
    audit.record(db, scope.email, "connector.toggle", "connector", connector.id,
                 tenant_id=connector.product.tenant_id, enabled=connector.enabled)
    return RedirectResponse("/connectors", status_code=303)
