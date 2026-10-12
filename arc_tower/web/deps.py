"""FastAPI dependencies: DB session, signed-in user, access scope and tenant context."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from arc_tower.db.models import Tenant, User
from arc_tower.db.session import new_session
from arc_tower.integrations.prometheus.promql import selector
from arc_tower.services import access
from arc_tower.services.access import Scope

from . import session as web_session


class LoginRequired(Exception):
    """Raised for unauthenticated page requests; handled by redirecting to /login."""


def get_db() -> Iterator[Session]:
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = web_session.user_id(request)
    user = db.get(User, uid) if uid else None
    if user is None or not user.active:
        raise LoginRequired()
    return user


def current_scope(user: User = Depends(current_user)) -> Scope:
    return access.scope_for(user)


@dataclass
class TenantContext:
    """Which tenants the current page shows: one selected tenant, or all visible ones."""
    scope: Scope
    tenants: list[Tenant]            # every tenant the user may see (for the switcher)
    current: Tenant | None           # None = "all tenants" view

    @property
    def active(self) -> list[Tenant]:
        return [self.current] if self.current else self.tenants

    @property
    def tenant_ids(self) -> list[int]:
        return [t.id for t in self.active]

    @property
    def slugs(self) -> list[str]:
        return [t.slug for t in self.active]

    def promql_selector(self, product_slugs: list[str] | None = None) -> str:
        sel = selector(self.slugs, product_slugs)
        return sel or 'arc_tenant_id!=""'

    @property
    def can_edit(self) -> bool:
        return bool(self.current) and self.scope.can_edit(self.current.id)


def tenant_context(request: Request, db: Session = Depends(get_db),
                   scope: Scope = Depends(current_scope)) -> TenantContext:
    tenants = access.visible_tenants(db, scope)
    wanted = web_session.selected_tenant(request)
    current = None
    if wanted != web_session.ALL_TENANTS:
        current = next((t for t in tenants if t.id == wanted), None)
        if current is None and tenants and not scope.all_tenants:
            current = tenants[0]
    return TenantContext(scope, tenants, current)


def require_platform_admin(scope: Scope = Depends(current_scope)) -> Scope:
    if not scope.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform administrators only.")
    return scope
