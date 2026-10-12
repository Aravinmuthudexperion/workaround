"""Who can see and change what.

Tenant users see only their own tenants. Platform admins and executives see every tenant;
only platform admins and tenant admins/editors can change things.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from arc_tower.core.constants import Role
from arc_tower.db.models import Product, Tenant, User

from .errors import NotFound, PermissionDenied


@dataclass(frozen=True)
class Scope:
    user_id: int
    email: str
    is_platform_admin: bool
    is_executive: bool
    roles: dict[int, Role]           # tenant_id -> role (explicit memberships)

    @property
    def all_tenants(self) -> bool:
        return self.is_platform_admin or self.is_executive

    def can_view(self, tenant_id: int) -> bool:
        return self.all_tenants or tenant_id in self.roles

    def can_edit(self, tenant_id: int) -> bool:
        return self.is_platform_admin or (tenant_id in self.roles and self.roles[tenant_id].can_edit)

    def can_admin(self, tenant_id: int) -> bool:
        return self.is_platform_admin or self.roles.get(tenant_id) is Role.TENANT_ADMIN


def scope_for(user: User) -> Scope:
    return Scope(
        user_id=user.id, email=user.email, is_platform_admin=user.is_platform_admin,
        is_executive=user.is_executive,
        roles={m.tenant_id: m.role_enum for m in user.memberships},
    )


def visible_tenants(session: Session, scope: Scope) -> list[Tenant]:
    stmt = select(Tenant).order_by(Tenant.name)
    if not scope.all_tenants:
        stmt = stmt.where(Tenant.id.in_(list(scope.roles) or [-1]))
    return list(session.scalars(stmt))


def get_tenant(session: Session, scope: Scope, tenant_id: int, edit: bool = False) -> Tenant:
    tenant = session.get(Tenant, tenant_id)
    if not tenant or not scope.can_view(tenant_id):
        raise NotFound("tenant not found")
    if edit and not scope.can_edit(tenant_id):
        raise PermissionDenied("you need editor rights on this tenant")
    return tenant


def get_product(session: Session, scope: Scope, product_id: int, edit: bool = False) -> Product:
    product = session.get(Product, product_id)
    if not product or not scope.can_view(product.tenant_id):
        raise NotFound("product not found")
    if edit and not scope.can_edit(product.tenant_id):
        raise PermissionDenied("you need editor rights on this product's tenant")
    return product


def require_platform_admin(scope: Scope) -> None:
    if not scope.is_platform_admin:
        raise PermissionDenied("platform admins only")
