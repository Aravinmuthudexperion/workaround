"""Customise which KPI tiles and charts the Overview and agent pages show."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from arc_tower.db.models import Agent, User
from arc_tower.integrations.prometheus.promql import selector
from arc_tower.services import access, kpis, layouts, metrics
from arc_tower.services.access import Scope
from arc_tower.services.errors import NotFound, ValidationFailed

from .. import session as web_session
from ..deps import TenantContext, current_scope, current_user, get_db, tenant_context
from ..templating import render

router = APIRouter()

_RETURN = {"overview": "/", "agent": "/agents"}


def agent_selector(db: Session, scope: Scope, agent_id: int) -> tuple[Agent, str]:
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise NotFound("agent not found")
    product = access.get_product(db, scope, agent.product_id)
    labels = {"arc_service_name": agent.service_name}
    if agent.agent_name:
        labels["gen_ai_agent_name"] = agent.agent_name
    return agent, selector([product.tenant.slug], [product.slug], **labels)


@router.get("/customise/{page}")
def customise(request: Request, page: str, back: str = "", db: Session = Depends(get_db),
              user: User = Depends(current_user), ctx: TenantContext = Depends(tenant_context)):
    layout, source = layouts.get_layout(db, user, page)
    return render(request, "customise.html", ctx=ctx, page=page, layout=layout, source=source,
                  catalog=kpis.catalog(db), metrics=layouts.METRICS, groups=layouts.GROUPS, kinds=layouts.KINDS,
                  max_charts=layouts.MAX_CHARTS, back=back if back.startswith("/") else _RETURN.get(page, "/"),
                  is_admin=user.is_platform_admin)


@router.post("/customise/{page}", dependencies=[Depends(web_session.verify_csrf)])
async def save(request: Request, page: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    back = str(form.get("back") or _RETURN.get(page, "/"))
    action = str(form.get("action", "save"))
    kpi_keys = {k.key for k in kpis.catalog(db)}
    try:
        if action in ("reset", "reset_everyone"):
            layouts.reset_layout(db, user, page, for_everyone=action == "reset_everyone")
            web_session.flash(request, "Layout reset.")
        else:
            order = {key: int(form.get(f"tile_order_{key}") or 99) for key in kpi_keys}
            tiles = sorted([str(t) for t in form.getlist("tiles")], key=lambda k: order.get(k, 99))
            charts = []
            for i in range(layouts.MAX_CHARTS):
                if form.get(f"chart_{i}_metric") and not form.get(f"chart_{i}_remove"):
                    charts.append({"metric": form.get(f"chart_{i}_metric"), "kind": form.get(f"chart_{i}_kind"),
                                   "group": form.get(f"chart_{i}_group"), "title": form.get(f"chart_{i}_title"),
                                   "order": int(form.get(f"chart_{i}_order") or i)})
            charts.sort(key=lambda c: c["order"])
            layouts.save_layout(db, user, page, {"tiles": tiles, "charts": charts}, kpi_keys,
                                for_everyone=action == "save_everyone")
            web_session.flash(request, "Layout saved for everyone." if action == "save_everyone" else "Layout saved.")
    except ValidationFailed as exc:
        web_session.flash(request, str(exc), "error")
        return RedirectResponse(f"/customise/{page}?back={back}", status_code=303)
    return RedirectResponse(back if back.startswith("/") else "/", status_code=303)


@router.get("/data/layout/{page}")
def layout_data(page: str, range: str = "24h", agent: int | None = None, db: Session = Depends(get_db),
                user: User = Depends(current_user), scope: Scope = Depends(current_scope),
                ctx: TenantContext = Depends(tenant_context)):
    """Chart data for every chart in the user's layout of this page."""
    layout, _ = layouts.get_layout(db, user, page)
    sel = agent_selector(db, scope, agent)[1] if page == "agent" and agent else ctx.promql_selector()
    return JSONResponse({c["id"]: metrics.chart_data(sel, c, range) for c in layout["charts"]})
