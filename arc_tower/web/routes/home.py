"""Overview: KPI tiles, trend charts and product status for the current tenant context."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from arc_tower.core.constants import KpiCategory
from arc_tower.db.models import User
from arc_tower.services import agents as agent_service
from arc_tower.services import alerts, kpis, layouts, metrics

from ..deps import TenantContext, current_user, get_db, tenant_context
from ..templating import render

router = APIRouter()


@router.get("/")
def overview(request: Request, window: str = "24h", ctx: TenantContext = Depends(tenant_context),
             db: Session = Depends(get_db), user: User = Depends(current_user)):
    window = window if window in kpis.WINDOWS else "24h"
    values = {v.kpi.key: v for v in kpis.evaluate(db, ctx.promql_selector(), window)}
    layout, source = layouts.get_layout(db, user, "overview")
    products = [p for t in ctx.active for p in t.products]
    agent_rows = agent_service.list_agents(db, ctx.tenant_ids)
    statuses = [agent_service.status(a) for a in agent_rows]
    firing = [a for t in ctx.active for a in alerts.firing(t)] if ctx.active else []
    by_category: dict[str, list] = {c.value: [] for c in KpiCategory}
    for value in values.values():
        by_category.setdefault(value.kpi.category, []).append(value)
    return render(request, "home.html", ctx=ctx, window=window,
                  headline=[values[k] for k in layout["tiles"] if k in values], by_category=by_category,
                  layout=layout, layout_source=source,
                  products=products, agent_counts={s: statuses.count(s) for s in ("active", "inactive")},
                  agents_total=len(agent_rows), firing=firing)


@router.get("/data/overview")
def overview_data(range: str = "24h", group: str = "arc_product_id", ctx: TenantContext = Depends(tenant_context)):
    group = group if group in ("arc_product_id", "arc_service_name", "gen_ai_request_model",
                               "arc_tenant_id") else "arc_product_id"
    return JSONResponse(metrics.overview_charts(ctx.promql_selector(), range, group))
