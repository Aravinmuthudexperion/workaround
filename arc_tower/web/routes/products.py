"""Products: list, detail (KPIs, keys, coverage), key rotation, Langfuse keys, delete."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from arc_tower.connectors import catalog
from arc_tower.integrations.grafana import dashboards as std
from arc_tower.integrations.prometheus.promql import selector
from arc_tower.services import access, coverage, kpis, langfuse_routing, products
from arc_tower.services.access import Scope

from .. import session as web_session
from ..deps import TenantContext, current_scope, get_db, tenant_context
from ..grafana_proxy import grafana_url
from ..templating import render

router = APIRouter()
_NEW_KEY = "new_ingest_key"


def remember_new_key(request: Request, product_id: int, key: str) -> None:
    """Keep a freshly issued key for exactly one page view (it is never stored in plain text)."""
    request.session[_NEW_KEY] = {"product": product_id, "key": key}


def pop_new_key(request: Request, product_id: int) -> str | None:
    data = request.session.get(_NEW_KEY)
    if data and data.get("product") == product_id:
        request.session.pop(_NEW_KEY)
        return data["key"]
    return None


@router.get("/products")
def list_products(request: Request, ctx: TenantContext = Depends(tenant_context)):
    rows = [(t, p) for t in ctx.active for p in t.products]
    return render(request, "products/list.html", ctx=ctx, rows=rows, catalog=catalog.CATALOG)


@router.get("/products/{product_id}")
def product_detail(request: Request, product_id: int, window: str = "24h", db: Session = Depends(get_db),
                   scope: Scope = Depends(current_scope), ctx: TenantContext = Depends(tenant_context)):
    product = access.get_product(db, scope, product_id)
    sel = selector([product.tenant.slug], [product.slug])
    values = kpis.evaluate(db, sel, window, kpis.targets_for(product))
    rows, score = coverage.report(product)
    org = product.tenant.grafana_org_id
    return render(
        request, "products/detail.html", ctx=ctx, product=product, spec=catalog.get(product.connector_type),
        values=values, window=window, coverage_rows=rows, coverage_score=score,
        can_edit=scope.can_edit(product.tenant_id), new_key=pop_new_key(request, product.id),
        env_block=products.env_block(product, None), langfuse_ready=langfuse_routing.credentials(product) is not None,
        links={
            "overview": grafana_url(f"d/{std.OVERVIEW_UID}", org, **{"var-product": product.slug}),
            "finops": grafana_url(f"d/{std.FINOPS_UID}", org, **{"var-product": product.slug}),
            "quality": grafana_url(f"d/{std.QUALITY_UID}", org, **{"var-product": product.slug}),
        },
    )


@router.get("/products/{product_id}/status")
def product_status(request: Request, product_id: int, db: Session = Depends(get_db),
                   scope: Scope = Depends(current_scope)):
    """HTMX partial: live "waiting for first data" indicator."""
    product = access.get_product(db, scope, product_id)
    rows, score = coverage.report(product)
    return render(request, "partials/product_status.html", product=product, coverage_score=score)


@router.post("/products/{product_id}/keys", dependencies=[Depends(web_session.verify_csrf)])
def rotate_key(request: Request, product_id: int, back: str = Form(""), db: Session = Depends(get_db),
               scope: Scope = Depends(current_scope)):
    product = access.get_product(db, scope, product_id, edit=True)
    issued = products.issue_ingest_key(db, scope.email, product, revoke_existing=True)
    remember_new_key(request, product.id, issued.key)
    web_session.flash(request, "New ingest key issued. Previous keys were revoked - update the client's env.")
    return RedirectResponse(back or f"/products/{product.id}", status_code=303)


@router.post("/products/{product_id}/keys/{key_id}/revoke", dependencies=[Depends(web_session.verify_csrf)])
def revoke_key(request: Request, product_id: int, key_id: int, db: Session = Depends(get_db),
               scope: Scope = Depends(current_scope)):
    product = access.get_product(db, scope, product_id, edit=True)
    products.revoke_ingest_key(db, scope.email, product, key_id)
    web_session.flash(request, "Key revoked. Clients using it are rejected within a minute.", "warning")
    return RedirectResponse(f"/products/{product.id}", status_code=303)


@router.post("/products/{product_id}/langfuse", dependencies=[Depends(web_session.verify_csrf)])
def save_langfuse_keys(request: Request, product_id: int, public_key: str = Form(...), secret_key: str = Form(...),
                       db: Session = Depends(get_db), scope: Scope = Depends(current_scope)):
    product = access.get_product(db, scope, product_id, edit=True)
    langfuse_routing.save_own_keys(product, public_key, secret_key)
    web_session.flash(request, "Langfuse project keys saved. New traces go to that project.")
    return RedirectResponse(f"/products/{product.id}", status_code=303)


@router.post("/products/{product_id}/delete", dependencies=[Depends(web_session.verify_csrf)])
def delete_product(request: Request, product_id: int, confirm: str = Form(""), db: Session = Depends(get_db),
                   scope: Scope = Depends(current_scope)):
    product = access.get_product(db, scope, product_id, edit=True)
    if confirm != product.slug:
        web_session.flash(request, f"Type '{product.slug}' to confirm deletion.", "error")
        return RedirectResponse(f"/products/{product.id}", status_code=303)
    products.delete_product(db, scope.email, product)
    web_session.flash(request, f"{product.name} deleted. Its data stays until retention expires.", "warning")
    return RedirectResponse("/products", status_code=303)
