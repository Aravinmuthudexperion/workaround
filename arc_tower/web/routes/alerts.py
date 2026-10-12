"""Alerts: firing alerts, targets per product, delivery channels."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from arc_tower.core.constants import ContactPointType
from arc_tower.services import access, alerts
from arc_tower.services.access import Scope
from arc_tower.services.errors import ValidationFailed

from .. import session as web_session
from ..deps import TenantContext, current_scope, get_db, tenant_context
from ..templating import render

router = APIRouter()


@router.get("/alerts")
def alert_page(request: Request, ctx: TenantContext = Depends(tenant_context)):
    firing = [(t, a) for t in ctx.active for a in alerts.firing(t)]
    targets = [(p, t) for tenant in ctx.active for p in tenant.products for t in p.targets]
    channels = [(t, c) for t in ctx.active for c in t.contact_points]
    return render(request, "alerts.html", ctx=ctx, firing=firing, targets=targets, channels=channels,
                  channel_types=[c.value for c in ContactPointType])


@router.post("/alerts/channels", dependencies=[Depends(web_session.verify_csrf)])
def add_channel(request: Request, tenant_id: int = Form(...), channel_type: str = Form(...), name: str = Form(""),
                target: str = Form(...), db: Session = Depends(get_db), scope: Scope = Depends(current_scope)):
    tenant = access.get_tenant(db, scope, tenant_id, edit=True)
    try:
        alerts.save_contact_point(db, scope.email, tenant, name, channel_type, target)
        web_session.flash(request, "Channel added and set as the default for this tenant's alerts.")
    except ValidationFailed as exc:
        web_session.flash(request, str(exc), "error")
    return RedirectResponse("/alerts", status_code=303)


@router.post("/alerts/sync/{product_id}", dependencies=[Depends(web_session.verify_csrf)])
def sync(request: Request, product_id: int, db: Session = Depends(get_db), scope: Scope = Depends(current_scope)):
    product = access.get_product(db, scope, product_id, edit=True)
    try:
        count = alerts.sync_product_rules(db, product)
        web_session.flash(request, f"{count} rule(s) synced to Grafana for {product.name}.")
    except ValidationFailed as exc:
        web_session.flash(request, str(exc), "error")
    return RedirectResponse("/alerts", status_code=303)
