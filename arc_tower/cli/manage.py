"""One-off management commands."""
from __future__ import annotations

import argparse
import logging

from arc_tower.core.constants import Role
from arc_tower.db.session import session_scope
from arc_tower.services import tenants, users

log = logging.getLogger("arc.cli")


def create_tenant(name: str, slug: str | None) -> None:
    with session_scope() as session:
        tenant = tenants.create_tenant(session, "cli", name, slug)
        log.info("tenant %s created (grafana org %s)", tenant.slug, tenant.grafana_org_id)


def create_user(args: argparse.Namespace) -> None:
    with session_scope() as session:
        user = users.get_by_email(session, args.email) or users.create_user(
            session, "cli", args.email, args.name, args.password,
            is_platform_admin=args.platform_admin, is_executive=args.executive)
        if args.tenant:
            tenant = tenants.get_by_slug(session, args.tenant)
            if not tenant:
                raise SystemExit(f"no tenant '{args.tenant}'")
            users.set_membership(session, "cli", user, tenant, Role(args.role))
        else:
            users.sync_grafana_user(user)
        log.info("user %s ready", user.email)
