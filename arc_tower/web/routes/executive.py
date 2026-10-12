"""Executive view: enterprise-wide rollups across all tenants (platform admins and executives)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from arc_tower.integrations.grafana import dashboards as std
from arc_tower.services import access, metrics
from arc_tower.services import agents as agent_service
from arc_tower.services.access import Scope

from ..deps import TenantContext, current_scope, get_db, tenant_context
from ..grafana_proxy import grafana_url
from ..templating import render

router = APIRouter()


@router.get("/executive")
def executive(request: Request, window: str = "7d", db: Session = Depends(get_db),
              scope: Scope = Depends(current_scope), ctx: TenantContext = Depends(tenant_context)):
    if not scope.all_tenants:
        raise HTTPException(status_code=403, detail="The executive view is for executives and platform admins.")
    window = window if window in ("24h", "7d", "30d") else "7d"
    tenants = access.visible_tenants(db, scope)
    rollup = metrics.tenant_rollup('arc_tenant_id!=""', window)
    agents = agent_service.list_agents(db, None)
    active_by_tenant: dict[int, int] = {}
    risk: dict[str, int] = {}
    for agent in agents:
        if agent_service.status(agent) == "active":
            active_by_tenant[agent.product.tenant_id] = active_by_tenant.get(agent.product.tenant_id, 0) + 1
        risk[agent.risk_class] = risk.get(agent.risk_class, 0) + 1
    totals = {k: sum(r.get(k, 0) for r in rollup.values()) for k in ("calls", "cost", "tokens")}
    return render(request, "executive.html", ctx=ctx, window=window, tenants=tenants, rollup=rollup, totals=totals,
                  active_by_tenant=active_by_tenant, agents_total=len(agents), risk=risk,
                  by_model=metrics.breakdown('arc_tenant_id!=""', "gen_ai_request_model", "cost", window),
                  grafana_exec=grafana_url(f"d/{std.EXEC_UID}", 1))
