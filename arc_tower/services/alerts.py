"""Turn KPI targets into Grafana alert rules, and manage where alerts are delivered."""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from arc_tower.core.constants import ContactPointType
from arc_tower.db.models import ContactPoint, Product, Target, Tenant
from arc_tower.integrations.grafana import GrafanaClient, GrafanaError
from arc_tower.integrations.grafana.dashboards import ALERTS_FOLDER_UID
from arc_tower.integrations.prometheus.promql import render, selector

from . import audit
from .errors import ValidationFailed

log = logging.getLogger(__name__)

ALERT_WINDOW = "15m"

_CP_SETTINGS = {
    ContactPointType.EMAIL.value: lambda s: {"addresses": s["target"]},
    ContactPointType.TEAMS.value: lambda s: {"url": s["target"]},
    ContactPointType.SLACK.value: lambda s: {"url": s["target"]},
    ContactPointType.WEBHOOK.value: lambda s: {"url": s["target"], "httpMethod": "POST"},
}


def rule_uid(target: Target) -> str:
    return f"arc-t{target.product_id}-{target.kpi_key}"[:40]


def _rule(target: Target, product: Product) -> dict:
    sel = selector([product.tenant.slug], [product.slug])
    expr = render(target.kpi.promql, sel, ALERT_WINDOW)
    evaluator = "gt" if target.kpi.breach == "above" else "lt"
    return {
        "uid": rule_uid(target),
        "title": f"{product.name}: {target.kpi.name} {'>' if evaluator == 'gt' else '<'} {target.threshold:g}",
        "ruleGroup": f"product-{product.slug}",
        "folderUID": ALERTS_FOLDER_UID,
        "condition": "C",
        "for": "5m",
        "noDataState": "OK",
        "execErrState": "Error",
        "labels": {"severity": target.severity, "arc_product": product.slug, "arc_kpi": target.kpi_key},
        "annotations": {"summary": f"{target.kpi.name} for {product.name} breached its target "
                                   f"({target.threshold:g} {target.kpi.unit})",
                        "description": target.kpi.description},
        "isPaused": not target.alert_enabled,
        "data": [
            {"refId": "A", "datasourceUid": "arc-prom", "relativeTimeRange": {"from": 900, "to": 0},
             "model": {"refId": "A", "expr": expr, "instant": True, "intervalMs": 60000, "maxDataPoints": 43200}},
            {"refId": "C", "datasourceUid": "__expr__", "relativeTimeRange": {"from": 0, "to": 0},
             "model": {"refId": "C", "type": "threshold", "expression": "A",
                       "conditions": [{"evaluator": {"type": evaluator, "params": [target.threshold]}}]}},
        ],
    }


def sync_product_rules(session: Session, product: Product, grafana: GrafanaClient | None = None) -> int:
    """Create/update a Grafana rule per alertable target; remove rules for deleted targets."""
    org_id = product.tenant.grafana_org_id
    if not org_id:
        return 0
    grafana = grafana or GrafanaClient()
    synced = 0
    try:
        for target in product.targets:
            if not target.kpi.alertable:
                continue
            grafana.upsert_alert_rule(org_id, _rule(target, product))
            target.grafana_rule_uid = rule_uid(target)
            synced += 1
    except GrafanaError as exc:
        log.warning("alert rule sync for %s failed: %s", product.slug, exc)
        raise ValidationFailed(f"Grafana rejected the alert rules: {exc}") from exc
    return synced


def delete_rule_for(target: Target) -> None:
    org_id = target.product.tenant.grafana_org_id
    if org_id and target.grafana_rule_uid:
        try:
            GrafanaClient().delete_alert_rule(org_id, target.grafana_rule_uid)
        except GrafanaError as exc:
            log.warning("could not delete alert rule %s: %s", target.grafana_rule_uid, exc)


def save_contact_point(session: Session, actor: str, tenant: Tenant, name: str, cp_type: str,
                       target: str) -> ContactPoint:
    if cp_type not in _CP_SETTINGS:
        raise ValidationFailed("unsupported channel type")
    if not target.strip():
        raise ValidationFailed("an address or webhook URL is required")
    contact = ContactPoint(tenant_id=tenant.id, name=name.strip() or cp_type, type=cp_type,
                           settings={"target": target.strip()})
    session.add(contact)
    session.flush()
    contact.grafana_uid = f"arc-cp-{contact.id}"
    if tenant.grafana_org_id:
        grafana = GrafanaClient()
        try:
            grafana.upsert_contact_point(tenant.grafana_org_id, contact.grafana_uid, contact.name, cp_type,
                                         _CP_SETTINGS[cp_type](contact.settings))
            grafana.set_default_receiver(tenant.grafana_org_id, contact.name)
        except GrafanaError as exc:
            raise ValidationFailed(f"Grafana rejected the contact point: {exc}") from exc
    audit.record(session, actor, "contact_point.create", "tenant", tenant.id, tenant_id=tenant.id,
                 type=cp_type, name=contact.name)
    return contact


def firing(tenant: Tenant) -> list[dict]:
    if not tenant.grafana_org_id:
        return []
    try:
        alerts = GrafanaClient().firing_alerts(tenant.grafana_org_id)
    except GrafanaError as exc:
        log.warning("could not read alerts for %s: %s", tenant.slug, exc)
        return []
    return [a for a in alerts if a.get("state", "").lower() in ("alerting", "firing", "pending")]
