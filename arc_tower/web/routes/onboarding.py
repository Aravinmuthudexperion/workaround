"""The 6-step onboarding wizard (deck slide 4):
1 Identify & connect · 2 Ingest · 3 Map & standardise · 4 KPIs & targets · 5 Alerts · 6 Dashboard."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from arc_tower.connectors import catalog
from arc_tower.core.config import get_settings
from arc_tower.core.constants import ContactPointType, LangfuseMode
from arc_tower.integrations.grafana import dashboards as std
from arc_tower.services import access, alerts, connectors, coverage, kpis, products
from arc_tower.services.access import Scope
from arc_tower.services.errors import ValidationFailed

from .. import session as web_session
from ..deps import TenantContext, current_scope, get_db, tenant_context
from ..grafana_proxy import grafana_url
from ..templating import render
from .products import pop_new_key, remember_new_key

router = APIRouter()

STEPS = ["Identify & connect", "Ingest data", "Map & standardise", "KPIs & targets", "Alerts", "Dashboard"]


def _step_url(product_id: int, step: int) -> str:
    return f"/products/{product_id}/onboard/{step}"


@router.get("/onboard")
def new_product(request: Request, ctx: TenantContext = Depends(tenant_context)):
    settings = get_settings()
    return render(request, "onboarding/step1.html", ctx=ctx, steps=STEPS, step=1, product=None,
                  categories=catalog.by_category(), managed_available=settings.langfuse_managed)


@router.post("/onboard", dependencies=[Depends(web_session.verify_csrf)])
def create_product(request: Request, tenant_id: int = Form(...), name: str = Form(...),
                   connector_type: str = Form(...), description: str = Form(""), environment: str = Form("prod"),
                   langfuse_mode: str = Form(LangfuseMode.SHARED.value), db: Session = Depends(get_db),
                   scope: Scope = Depends(current_scope), ctx: TenantContext = Depends(tenant_context)):
    tenant = access.get_tenant(db, scope, tenant_id, edit=True)
    try:
        product = products.create_product(db, scope.email, tenant, name, connector_type, description, environment,
                                          langfuse_mode)
    except ValidationFailed as exc:
        return render(request, "onboarding/step1.html", status_code=422, ctx=ctx, steps=STEPS, step=1,
                      product=None, categories=catalog.by_category(), error=str(exc),
                      managed_available=get_settings().langfuse_managed,
                      form={"name": name, "connector_type": connector_type, "description": description,
                            "tenant_id": tenant_id, "environment": environment})
    products.advance_onboarding(product, 2)
    if product.is_push:
        issued = products.issue_ingest_key(db, scope.email, product)
        remember_new_key(request, product.id, issued.key)
    return RedirectResponse(_step_url(product.id, 2), status_code=303)


@router.get("/products/{product_id}/onboard/{step}")
def wizard_step(request: Request, product_id: int, step: int, db: Session = Depends(get_db),
                scope: Scope = Depends(current_scope), ctx: TenantContext = Depends(tenant_context)):
    product = access.get_product(db, scope, product_id)
    step = min(max(step, 2), 6)
    spec = catalog.get(product.connector_type)
    context = {"ctx": ctx, "steps": STEPS, "step": step, "product": product, "spec": spec,
               "can_edit": scope.can_edit(product.tenant_id)}
    if step == 2:
        key = pop_new_key(request, product.id)
        context |= {"new_key": key, "env_block": products.env_block(product, key),
                    "connector": next((c for c in product.connectors if c.type == product.connector_type), None)}
    elif step == 3:
        rows, score = coverage.report(product)
        context |= {"coverage_rows": rows, "coverage_score": score}
    elif step == 4:
        context |= {"catalog": kpis.catalog(db), "targets": kpis.targets_for(product)}
    elif step == 5:
        context |= {"contact_points": product.tenant.contact_points, "channel_types": [c.value for c in ContactPointType],
                    "targets": [t for t in product.targets if t.kpi.alertable]}
    elif step == 6:
        org = product.tenant.grafana_org_id
        context |= {"links": {
            "overview": grafana_url(f"d/{std.OVERVIEW_UID}", org, **{"var-product": product.slug}),
            "finops": grafana_url(f"d/{std.FINOPS_UID}", org, **{"var-product": product.slug}),
            "quality": grafana_url(f"d/{std.QUALITY_UID}", org, **{"var-product": product.slug}),
        }}
    return render(request, f"onboarding/step{step}.html", **context)


@router.post("/products/{product_id}/onboard/2/connector", dependencies=[Depends(web_session.verify_csrf)])
async def save_connector(request: Request, product_id: int, db: Session = Depends(get_db),
                         scope: Scope = Depends(current_scope)):
    product = access.get_product(db, scope, product_id, edit=True)
    form = {k: str(v) for k, v in (await request.form()).items()}
    try:
        connectors.configure(db, scope.email, product, form, int(form.get("interval_seconds") or 0) or None)
    except ValidationFailed as exc:
        web_session.flash(request, str(exc), "error")
        return RedirectResponse(_step_url(product.id, 2), status_code=303)
    web_session.flash(request, "Connector saved. The worker runs it within a minute - watch the status below.")
    return RedirectResponse(_step_url(product.id, 2), status_code=303)


@router.post("/products/{product_id}/onboard/4", dependencies=[Depends(web_session.verify_csrf)])
async def save_targets(request: Request, product_id: int, db: Session = Depends(get_db),
                       scope: Scope = Depends(current_scope)):
    product = access.get_product(db, scope, product_id, edit=True)
    form = await request.form()
    entries = {}
    for kpi in kpis.catalog(db):
        raw = str(form.get(f"threshold_{kpi.key}", "")).strip()
        try:
            threshold = float(raw) if raw else None
        except ValueError:
            threshold = None
        entries[kpi.key] = {"threshold": threshold, "alert": form.get(f"alert_{kpi.key}") == "on",
                            "severity": str(form.get(f"severity_{kpi.key}", "warning"))}
    kpis.save_targets(db, scope.email, product, entries)
    products.advance_onboarding(product, 5)
    return RedirectResponse(_step_url(product.id, 5), status_code=303)


@router.post("/products/{product_id}/onboard/5", dependencies=[Depends(web_session.verify_csrf)])
def save_alerting(request: Request, product_id: int, channel_type: str = Form(""), channel_name: str = Form(""),
                  channel_target: str = Form(""), db: Session = Depends(get_db),
                  scope: Scope = Depends(current_scope)):
    product = access.get_product(db, scope, product_id, edit=True)
    try:
        if channel_target.strip():
            alerts.save_contact_point(db, scope.email, product.tenant, channel_name, channel_type, channel_target)
        synced = alerts.sync_product_rules(db, product)
    except ValidationFailed as exc:
        web_session.flash(request, str(exc), "error")
        return RedirectResponse(_step_url(product.id, 5), status_code=303)
    products.advance_onboarding(product, 6)
    web_session.flash(request, f"{synced} alert rule(s) active in Grafana.")
    return RedirectResponse(_step_url(product.id, 6), status_code=303)


@router.post("/products/{product_id}/onboard/{step}/next", dependencies=[Depends(web_session.verify_csrf)])
def next_step(product_id: int, step: int, db: Session = Depends(get_db), scope: Scope = Depends(current_scope)):
    product = access.get_product(db, scope, product_id, edit=True)
    products.advance_onboarding(product, step + 1)
    return RedirectResponse(_step_url(product.id, step + 1), status_code=303)
