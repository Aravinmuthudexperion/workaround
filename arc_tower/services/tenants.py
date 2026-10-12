"""Tenant lifecycle."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from arc_tower.db.models import Tenant
from arc_tower.integrations.grafana import GrafanaError
from arc_tower.utils.text import slugify

from . import audit, grafana_provisioning, users
from .errors import ValidationFailed

log = logging.getLogger(__name__)


def get_by_slug(session: Session, slug: str) -> Tenant | None:
    return session.scalar(select(Tenant).where(Tenant.slug == slug))


def create_tenant(session: Session, actor: str, name: str, slug: str | None = None,
                  description: str = "") -> Tenant:
    name = name.strip()
    if not name:
        raise ValidationFailed("tenant name is required")
    slug = slugify(slug or name)
    if get_by_slug(session, slug):
        raise ValidationFailed(f"a tenant with id '{slug}' already exists")
    tenant = Tenant(slug=slug, name=name, description=description)
    session.add(tenant)
    session.flush()
    audit.record(session, actor, "tenant.create", "tenant", tenant.id, tenant_id=tenant.id, slug=slug)
    provision(session, tenant)
    return tenant


def provision(session: Session, tenant: Tenant) -> bool:
    """Best effort: a tenant is usable even if Grafana is down; `start.py up` retries."""
    try:
        grafana_provisioning.provision_tenant(session, tenant)
        grafana_provisioning.refresh_platform_logs(session)
        users.grant_all_tenant_users(session, tenant)
        return True
    except GrafanaError as exc:
        log.warning("grafana provisioning for tenant %s deferred: %s", tenant.slug, exc)
        return False
