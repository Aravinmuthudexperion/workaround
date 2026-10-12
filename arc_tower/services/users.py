"""Users, authentication and tenant memberships (mirrored into Grafana org roles)."""
from __future__ import annotations

import logging
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from arc_tower.core.constants import Role
from arc_tower.core.security import hash_password, verify_password
from arc_tower.db.models import Membership, Tenant, User
from arc_tower.integrations.grafana import GrafanaClient, GrafanaError
from arc_tower.utils.timeutil import utcnow

from . import audit
from .errors import ValidationFailed

log = logging.getLogger(__name__)


def get_by_email(session: Session, email: str) -> User | None:
    return session.scalar(select(User).where(User.email == email.strip().lower()))


def authenticate(session: Session, email: str, password: str) -> User | None:
    user = get_by_email(session, email)
    if not user or not user.active or not verify_password(password, user.password_hash):
        return None
    user.last_login_at = utcnow()
    return user


def create_user(session: Session, actor: str, email: str, name: str = "", password: str | None = None,
                is_platform_admin: bool = False, is_executive: bool = False) -> User:
    email = email.strip().lower()
    if "@" not in email:
        raise ValidationFailed("a valid e-mail address is required")
    if get_by_email(session, email):
        raise ValidationFailed(f"user {email} already exists")
    user = User(email=email, name=name or email.split("@")[0], is_platform_admin=is_platform_admin,
                is_executive=is_executive, password_hash=hash_password(password) if password else None)
    session.add(user)
    session.flush()
    audit.record(session, actor, "user.create", "user", user.id, email=email,
                 platform_admin=is_platform_admin, executive=is_executive)
    return user


def ensure_admin(session: Session, email: str, password: str) -> User:
    user = get_by_email(session, email)
    if user:
        return user
    return create_user(session, "system", email, "ARC Admin", password, is_platform_admin=True)


def set_membership(session: Session, actor: str, user: User, tenant: Tenant, role: Role) -> Membership:
    membership = session.scalar(select(Membership).where(Membership.user_id == user.id,
                                                         Membership.tenant_id == tenant.id))
    if membership:
        membership.role = role.value
    else:
        membership = Membership(user_id=user.id, tenant_id=tenant.id, role=role.value)
        session.add(membership)
    session.flush()
    session.refresh(user)
    audit.record(session, actor, "membership.set", "user", user.id, tenant_id=tenant.id, role=role.value)
    sync_grafana_user(user)
    return membership


def remove_membership(session: Session, actor: str, user: User, tenant: Tenant) -> None:
    membership = session.scalar(select(Membership).where(Membership.user_id == user.id,
                                                         Membership.tenant_id == tenant.id))
    if membership:
        session.delete(membership)
        audit.record(session, actor, "membership.remove", "user", user.id, tenant_id=tenant.id)
        if tenant.grafana_org_id:
            try:
                GrafanaClient().remove_from_org(tenant.grafana_org_id, user.email)
            except GrafanaError as exc:
                log.warning("grafana: could not remove %s from org: %s", user.email, exc)


def _all_tenants_role(user: User) -> str:
    return "Admin" if user.is_platform_admin else "Viewer"


def _tenant_org_ids(user: User) -> list[int]:
    session = object_session(user)
    if session is None:
        return []
    return [org for org in session.scalars(select(Tenant.grafana_org_id)) if org]


def grant_all_tenant_users(session: Session, tenant: Tenant) -> None:
    """Give existing admins/executives access to a newly provisioned tenant org."""
    if not tenant.grafana_org_id:
        return
    grafana = GrafanaClient()
    for user in session.scalars(select(User).where(User.active.is_(True))):
        if user.sees_all_tenants:
            try:
                grafana.set_org_role(tenant.grafana_org_id, user.email, _all_tenants_role(user))
            except GrafanaError as exc:
                log.warning("grafana: could not add %s to org %s: %s", user.email, tenant.grafana_org_id, exc)


def sync_grafana_user(user: User) -> None:
    """Make the user's Grafana account match ARC: org roles per tenant, platform org for admins/execs."""
    grafana = GrafanaClient()
    try:
        grafana.ensure_user(user.email, user.name, secrets.token_urlsafe(24))
        home_org = None
        member_orgs = set()
        for membership in user.memberships:
            org_id = membership.tenant.grafana_org_id
            if org_id:
                grafana.set_org_role(org_id, user.email, membership.role_enum.grafana_role)
                member_orgs.add(org_id)
                home_org = home_org or org_id
        # Admins and executives see every tenant in ARC without membership rows,
        # and the dashboards page embeds the selected tenant's org - so they
        # need a role in every tenant org too, or Grafana answers 404.
        if user.sees_all_tenants:
            for org_id in _tenant_org_ids(user):
                if org_id not in member_orgs:
                    grafana.set_org_role(org_id, user.email, _all_tenants_role(user))
        # Org 1 is the platform org with unfiltered data. Grafana puts every new user there,
        # so tenant users get the "None" role (no access) instead of the default Viewer.
        if user.is_platform_admin:
            grafana.set_org_role(1, user.email, "Admin")
            grafana.set_server_admin(user.email, True)
        elif user.is_executive:
            grafana.set_org_role(1, user.email, "Viewer")
        else:
            grafana.set_org_role(1, user.email, "None")
        grafana.set_user_home_org(user.email, 1 if user.sees_all_tenants else (home_org or 1))
    except GrafanaError as exc:
        log.warning("grafana: user sync for %s failed: %s", user.email, exc)
