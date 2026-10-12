"""Agent registry & inventory."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from arc_tower.db.models import Agent, User
from arc_tower.integrations.grafana import dashboards as std
from arc_tower.integrations.prometheus.promql import selector
from arc_tower.services import access, kpis, layouts, metrics
from arc_tower.services import agents as agent_service
from arc_tower.services.access import Scope
from arc_tower.services.errors import NotFound

from .. import session as web_session
from ..deps import TenantContext, current_scope, current_user, get_db, tenant_context
from ..grafana_proxy import grafana_url
from ..templating import render
from .layouts import agent_selector

router = APIRouter()


@router.get("/agents")
def registry(request: Request, product: int | None = None, db: Session = Depends(get_db),
             ctx: TenantContext = Depends(tenant_context)):
    rows = agent_service.list_agents(db, ctx.tenant_ids, product)
    # Keyed by (service, agent name): several agents can share one service.
    by = "arc_product_id, arc_service_name, gen_ai_agent_name"
    cost = dict(metrics.breakdown(ctx.promql_selector(), by, "cost", "24h"))
    calls = dict(metrics.breakdown(ctx.promql_selector(), by, "calls", "24h"))
    active_count = sum(1 for a in rows if agent_service.status(a) == "active")
    return render(request, "agents.html", ctx=ctx, agents=rows, status=agent_service.status, cost=cost, calls=calls,
                  active_count=active_count,
                  risk_classes=agent_service.RISK_CLASSES, products=[p for t in ctx.active for p in t.products])


@router.post("/agents", dependencies=[Depends(web_session.verify_csrf)])
def register(request: Request, product_id: int = Form(...), service_name: str = Form(...),
             display_name: str = Form(""), owner: str = Form(""), purpose: str = Form(""),
             risk_class: str = Form("unclassified"), db: Session = Depends(get_db),
             scope: Scope = Depends(current_scope)):
    product = access.get_product(db, scope, product_id, edit=True)
    agent_service.register_manual(db, scope.email, product, service_name, display_name=display_name, owner=owner,
                                  purpose=purpose, risk_class=risk_class)
    web_session.flash(request, "Agent registered. It becomes active when telemetry with this service name arrives.")
    return RedirectResponse("/agents", status_code=303)


@router.post("/agents/{agent_id}", dependencies=[Depends(web_session.verify_csrf)])
def update(request: Request, agent_id: int, display_name: str = Form(""), owner: str = Form(""),
           purpose: str = Form(""), risk_class: str = Form("unclassified"), db: Session = Depends(get_db),
           scope: Scope = Depends(current_scope)):
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise NotFound("agent not found")
    access.get_product(db, scope, agent.product_id, edit=True)
    agent_service.update_metadata(db, scope.email, agent, display_name, owner, purpose, risk_class)
    web_session.flash(request, f"{agent.label} updated.")
    return RedirectResponse("/agents", status_code=303)


@router.get("/agents/{agent_id}")
def agent_detail(request: Request, agent_id: int, window: str = "24h", db: Session = Depends(get_db),
                 scope: Scope = Depends(current_scope), user: User = Depends(current_user),
                 ctx: TenantContext = Depends(tenant_context)):
    agent, sel = agent_selector(db, scope, agent_id)
    window = window if window in kpis.WINDOWS else "24h"
    layout, source = layouts.get_layout(db, user, "agent")
    values = {v.kpi.key: v for v in kpis.evaluate(db, sel, window, kpis.targets_for(agent.product))}
    return render(request, "agent_detail.html", ctx=ctx, agent=agent, window=window, layout=layout,
                  layout_source=source, tiles=[values[k] for k in layout["tiles"] if k in values],
                  status=agent_service.status(agent), risk_classes=agent_service.RISK_CLASSES,
                  can_edit=scope.can_edit(agent.product.tenant_id),
                  grafana=grafana_url(f"d/{std.OVERVIEW_UID}", agent.product.tenant.grafana_org_id,
                                      **{"var-product": agent.product.slug, "var-service": agent.service_name}))
