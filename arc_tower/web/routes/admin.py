"""Administration: tenants, users & memberships, audit log, system health, personal API tokens."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from arc_tower.core.constants import Role
from arc_tower.db.models import User
from arc_tower.services import access, api_tokens, audit, tenants, users
from arc_tower.services.access import Scope
from arc_tower.services.errors import NotFound, ValidationFailed

from .. import session as web_session
from ..deps import TenantContext, current_scope, current_user, get_db, require_platform_admin, tenant_context
from ..templating import render
from .system import component_health

router = APIRouter()


@router.get("/admin/tenants")
def tenant_admin(request: Request, ctx: TenantContext = Depends(tenant_context),
                 _: Scope = Depends(require_platform_admin)):
    return render(request, "admin/tenants.html", ctx=ctx)


@router.post("/admin/tenants", dependencies=[Depends(web_session.verify_csrf)])
def create_tenant(request: Request, name: str = Form(...), slug: str = Form(""), description: str = Form(""),
                  db: Session = Depends(get_db), scope: Scope = Depends(require_platform_admin)):
    try:
        tenant = tenants.create_tenant(db, scope.email, name, slug or None, description)
        web_session.flash(request, f"Tenant {tenant.name} created"
                          + ("" if tenant.grafana_org_id else " (Grafana provisioning deferred)") + ".")
    except ValidationFailed as exc:
        web_session.flash(request, str(exc), "error")
    return RedirectResponse("/admin/tenants", status_code=303)


@router.get("/admin/users")
def user_admin(request: Request, db: Session = Depends(get_db), ctx: TenantContext = Depends(tenant_context),
               _: Scope = Depends(require_platform_admin)):
    all_users = list(db.scalars(select(User).order_by(User.email)))
    return render(request, "admin/users.html", ctx=ctx, users=all_users, roles=[r.value for r in Role])


@router.post("/admin/users", dependencies=[Depends(web_session.verify_csrf)])
def create_user(request: Request, email: str = Form(...), name: str = Form(""), password: str = Form(""),
                tenant_id: str = Form(""), role: str = Form("viewer"), platform_admin: str = Form(""),
                executive: str = Form(""), db: Session = Depends(get_db), scope: Scope = Depends(require_platform_admin)):
    try:
        user = users.create_user(db, scope.email, email, name, password or None,
                                 is_platform_admin=platform_admin == "on", is_executive=executive == "on")
        if tenant_id:
            users.set_membership(db, scope.email, user, access.get_tenant(db, scope, int(tenant_id)), Role(role))
        else:
            users.sync_grafana_user(user)
        web_session.flash(request, f"User {user.email} created.")
    except ValidationFailed as exc:
        web_session.flash(request, str(exc), "error")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/membership", dependencies=[Depends(web_session.verify_csrf)])
def set_membership(request: Request, user_id: int, tenant_id: int = Form(...), role: str = Form("viewer"),
                   remove: str = Form(""), db: Session = Depends(get_db), scope: Scope = Depends(require_platform_admin)):
    user = db.get(User, user_id)
    if user is None:
        raise NotFound("user not found")
    tenant = access.get_tenant(db, scope, tenant_id)
    if remove:
        users.remove_membership(db, scope.email, user, tenant)
        web_session.flash(request, f"{user.email} removed from {tenant.name}.", "warning")
    else:
        users.set_membership(db, scope.email, user, tenant, Role(role))
        web_session.flash(request, f"{user.email} is now {role} in {tenant.name}.")
    return RedirectResponse("/admin/users", status_code=303)


@router.get("/admin/audit")
def audit_log(request: Request, db: Session = Depends(get_db), ctx: TenantContext = Depends(tenant_context)):
    tenant_filter = None if ctx.scope.is_platform_admin else [
        t.id for t in ctx.tenants if ctx.scope.can_admin(t.id)]
    if tenant_filter == []:
        raise ValidationFailed("the audit log is available to platform and tenant admins")
    return render(request, "admin/audit.html", ctx=ctx, entries=audit.recent(db, tenant_filter))


@router.get("/admin/system")
def system(request: Request, ctx: TenantContext = Depends(tenant_context), _: Scope = Depends(require_platform_admin)):
    return render(request, "admin/system.html", ctx=ctx, components=component_health())


@router.get("/account")
def account(request: Request, user: User = Depends(current_user), ctx: TenantContext = Depends(tenant_context)):
    return render(request, "account.html", ctx=ctx, user=user, new_token=request.session.pop("new_api_token", None))


@router.post("/account/tokens", dependencies=[Depends(web_session.verify_csrf)])
def create_token(request: Request, name: str = Form("api"), db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    request.session["new_api_token"] = api_tokens.create(db, user, name)
    return RedirectResponse("/account", status_code=303)


@router.post("/account/tokens/{token_id}/revoke", dependencies=[Depends(web_session.verify_csrf)])
def revoke_token(request: Request, token_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    api_tokens.revoke(db, user, token_id)
    web_session.flash(request, "Token revoked.", "warning")
    return RedirectResponse("/account", status_code=303)
