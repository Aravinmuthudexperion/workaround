"""ARC Tower REST API v1. Authenticate with `Authorization: Bearer <token>` (create one under Account)
or an ARC browser session. Results are always limited to the caller's tenants."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from arc_tower.db.models import User
from arc_tower.integrations.prometheus.promql import selector
from arc_tower.services import access, api_tokens, kpis, traces
from arc_tower.services import agents as agent_service
from arc_tower.services.access import Scope

from ... import session as web_session
from ...deps import get_db
from .schemas import AgentOut, KpiOut, ProductOut, TenantOut, TraceOut

router = APIRouter(tags=["v1"])


def api_scope(request: Request, db: Session = Depends(get_db)) -> Scope:
    auth = request.headers.get("authorization", "")
    user: User | None = None
    if auth.lower().startswith("bearer "):
        user = api_tokens.user_for_token(db, auth[7:].strip())
    elif web_session.user_id(request):
        user = db.get(User, web_session.user_id(request))
    if user is None or not user.active:
        raise HTTPException(status_code=401, detail="missing or invalid API token")
    return access.scope_for(user)


@router.get("/tenants", response_model=list[TenantOut])
def list_tenants(db: Session = Depends(get_db), scope: Scope = Depends(api_scope)):
    return [TenantOut(id=t.id, slug=t.slug, name=t.name) for t in access.visible_tenants(db, scope)]


@router.get("/products", response_model=list[ProductOut])
def list_products(tenant: str | None = None, db: Session = Depends(get_db), scope: Scope = Depends(api_scope)):
    return [ProductOut(id=p.id, tenant=t.slug, slug=p.slug, name=p.name, connector_type=p.connector_type,
                       ingest_mode=p.ingest_mode, status=p.status, environment=p.environment,
                       first_seen_at=p.first_seen_at, last_seen_at=p.last_seen_at)
            for t in access.visible_tenants(db, scope) if not tenant or t.slug == tenant for p in t.products]


@router.get("/agents", response_model=list[AgentOut])
def list_agents(db: Session = Depends(get_db), scope: Scope = Depends(api_scope)):
    tenant_ids = None if scope.all_tenants else list(scope.roles)
    return [AgentOut(id=a.id, tenant=a.product.tenant.slug, product=a.product.slug, service_name=a.service_name,
                     agent_name=a.agent_name, display_name=a.display_name, owner=a.owner, risk_class=a.risk_class,
                     platform=a.platform, models=a.models or [], status=agent_service.status(a),
                     last_seen_at=a.last_seen_at)
            for a in agent_service.list_agents(db, tenant_ids)]


@router.get("/products/{product_id}/kpis", response_model=list[KpiOut])
def product_kpis(product_id: int, window: str = "24h", db: Session = Depends(get_db),
                 scope: Scope = Depends(api_scope)):
    product = access.get_product(db, scope, product_id)
    values = kpis.evaluate(db, selector([product.tenant.slug], [product.slug]), window, kpis.targets_for(product))
    return [KpiOut(key=v.kpi.key, name=v.kpi.name, category=v.kpi.category, unit=v.kpi.unit, value=v.value,
                   target=v.target.threshold if v.target else None, breached=v.breached) for v in values]


@router.get("/products/{product_id}/traces", response_model=list[TraceOut])
def product_traces(product_id: int, response: Response, cursor: str | None = None, limit: int = 25,
                   db: Session = Depends(get_db), scope: Scope = Depends(api_scope)):
    """Newest requests first. Pass the `X-Next-Cursor` response header back as `cursor` for older ones."""
    product = access.get_product(db, scope, product_id)
    result = traces.list_traces(product, cursor=cursor, limit=min(limit, 100))
    if result.error:
        raise HTTPException(status_code=502, detail=result.error)
    if result.next_cursor:
        response.headers["X-Next-Cursor"] = result.next_cursor
    return [TraceOut(**{k: getattr(r, k) for k in TraceOut.model_fields}) for r in result.rows]
