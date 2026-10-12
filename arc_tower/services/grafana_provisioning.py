"""Create each tenant's Grafana org: isolated data sources, folders, dashboards, library panels.

Isolation: a tenant org's Prometheus data source points at prom-label-proxy with
?arc_tenant_id=<slug>, and its Loki data source sends X-Scope-OrgID: <slug>.
Tenant users are Viewer/Editor in that org, so they cannot change the data sources.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from arc_tower.core.config import LOKI_URL, PROM_LABEL_PROXY_URL, PROMETHEUS_URL
from arc_tower.db.models import Tenant
from arc_tower.integrations.grafana import GrafanaClient
from arc_tower.integrations.grafana import dashboards as std

log = logging.getLogger(__name__)

PLATFORM_ORG_ID = 1
PLATFORM_ORG_NAME = "ARC Platform"

_VIEW, _EDIT = 1, 2


def _prometheus_ds(url: str, tenant_slug: str | None) -> dict:
    json_data = {"httpMethod": "GET", "prometheusType": "Prometheus", "timeInterval": "15s"}
    if tenant_slug:
        json_data["customQueryParameters"] = f"arc_tenant_id={tenant_slug}"
    return {"uid": "arc-prom", "name": "ARC Metrics", "type": "prometheus", "access": "proxy", "url": url,
            "isDefault": True, "jsonData": json_data}


def _loki_ds(org_header: str) -> dict:
    return {"uid": "arc-loki", "name": "ARC Logs", "type": "loki", "access": "proxy", "url": LOKI_URL,
            "jsonData": {"httpHeaderName1": "X-Scope-OrgID", "maxLines": 1000},
            "secureJsonData": {"httpHeaderValue1": org_header}}


def _folders(grafana: GrafanaClient, org_id: int) -> None:
    grafana.ensure_folder(org_id, std.STANDARD_FOLDER_UID, "ARC Standard")
    grafana.ensure_folder(org_id, std.MINE_FOLDER_UID, "My Dashboards")
    grafana.ensure_folder(org_id, std.ALERTS_FOLDER_UID, "ARC Alerts")
    # Standard dashboards are read-only for everyone but org admins; "My Dashboards" is editable.
    grafana.set_folder_permissions(org_id, std.STANDARD_FOLDER_UID,
                                   [{"role": "Viewer", "permission": _VIEW}, {"role": "Editor", "permission": _VIEW}])
    grafana.set_folder_permissions(org_id, std.MINE_FOLDER_UID,
                                   [{"role": "Viewer", "permission": _VIEW}, {"role": "Editor", "permission": _EDIT}])
    grafana.set_folder_permissions(org_id, std.ALERTS_FOLDER_UID,
                                   [{"role": "Viewer", "permission": _VIEW}, {"role": "Editor", "permission": _VIEW}])


def _content(grafana: GrafanaClient, org_id: int, dashboards: list[dict]) -> None:
    for dashboard in dashboards:
        grafana.upsert_dashboard(org_id, dashboard, std.STANDARD_FOLDER_UID)
    for uid, name, model in std.library_panels():
        grafana.upsert_library_panel(org_id, uid, name, model, std.STANDARD_FOLDER_UID)
    # Created after the library panels it references.
    grafana.upsert_dashboard(org_id, std.library_showcase(), std.STANDARD_FOLDER_UID)


def provision_tenant(session: Session, tenant: Tenant, grafana: GrafanaClient | None = None) -> int:
    grafana = grafana or GrafanaClient()
    org_id = tenant.grafana_org_id or grafana.ensure_org(f"ARC · {tenant.name} ({tenant.slug})")
    tenant.grafana_org_id = org_id
    grafana.upsert_datasource(org_id, _prometheus_ds(PROM_LABEL_PROXY_URL, tenant.slug))
    grafana.upsert_datasource(org_id, _loki_ds(tenant.slug))
    _folders(grafana, org_id)
    _content(grafana, org_id, std.tenant_dashboards())
    session.flush()
    log.info("grafana: tenant %s provisioned in org %s", tenant.slug, org_id)
    return org_id


def provision_platform(session: Session, grafana: GrafanaClient | None = None) -> None:
    """Org 1 = platform org for admins and executives: unfiltered data, executive dashboard."""
    grafana = grafana or GrafanaClient()
    grafana.rename_org(PLATFORM_ORG_ID, PLATFORM_ORG_NAME)
    grafana.upsert_datasource(PLATFORM_ORG_ID, _prometheus_ds(PROMETHEUS_URL, None))
    refresh_platform_logs(session, grafana)
    _folders(grafana, PLATFORM_ORG_ID)
    _content(grafana, PLATFORM_ORG_ID, [std.executive(), *std.tenant_dashboards()])


def refresh_platform_logs(session: Session, grafana: GrafanaClient | None = None) -> None:
    """Loki needs the explicit tenant list for cross-tenant queries ("a|b|c")."""
    grafana = grafana or GrafanaClient()
    slugs = list(session.scalars(select(Tenant.slug).order_by(Tenant.slug)))
    grafana.upsert_datasource(PLATFORM_ORG_ID, _loki_ds("|".join(["arc-platform", *slugs])))
