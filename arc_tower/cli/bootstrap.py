"""Initialisation and idempotent provisioning, run by `start.py setup` / `start.py up`."""
from __future__ import annotations

import logging
import time

from sqlalchemy import select

from arc_tower.core.config import get_settings
from arc_tower.db.models import Product, Tenant, User
from arc_tower.db.session import init_db as create_tables
from arc_tower.db.session import session_scope
from arc_tower.infra.render import render_all
from arc_tower.integrations.grafana import GrafanaClient, GrafanaError
from arc_tower.services import alerts, grafana_provisioning, kpis, tenants, users
from arc_tower.services.errors import ValidationFailed

log = logging.getLogger("arc.bootstrap")


def init_db() -> None:
    settings = get_settings()
    create_tables()
    with session_scope() as session:
        kpis.seed_catalog(session)
        if settings.admin_password:
            users.ensure_admin(session, settings.admin_email, settings.admin_password)
    log.info("database ready at %s", settings.database_url)


def render_configs() -> None:
    for path in render_all():
        log.info("rendered %s", path)


def _wait_for_grafana(timeout: float = 120) -> bool:
    grafana = GrafanaClient()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if grafana.healthy():
            return True
        time.sleep(2)
    return False


def bootstrap() -> None:
    """Bring Grafana in line with the registry. Safe to run repeatedly."""
    init_db()
    if not _wait_for_grafana():
        log.warning("Grafana is not reachable - skipping provisioning (re-run `start.py up` later)")
        return
    with session_scope() as session:
        try:
            grafana_provisioning.provision_platform(session)
        except GrafanaError as exc:
            log.error("platform org provisioning failed: %s", exc)
            return
        for tenant in session.scalars(select(Tenant)):
            tenants.provision(session, tenant)
        for user in session.scalars(select(User).where(User.active.is_(True))):
            users.sync_grafana_user(user)
        for product in session.scalars(select(Product)):
            if product.targets:
                try:
                    alerts.sync_product_rules(session, product)
                except ValidationFailed as exc:
                    log.warning("alert rules for %s: %s", product.slug, exc)
    log.info("Grafana provisioning complete")
