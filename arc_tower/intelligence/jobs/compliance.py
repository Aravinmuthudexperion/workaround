"""Compliance: guardrail / policy events reported by agents (from Loki), per product.

PII masking is counted in real time by the gateway (arc_pii_masked); this job adds the
events agents themselves log, e.g. event.name = guardrail.violation.
"""
from __future__ import annotations

import logging

import httpx
from sqlalchemy import select

from arc_tower.db.models import Tenant
from arc_tower.db.session import session_scope
from arc_tower.integrations.loki import LokiClient

from .. import metrics

INTERVAL = 120
log = logging.getLogger("arc.intelligence.compliance")

QUERY = ('sum by (arc_product_id) (count_over_time({{arc_tenant_id="{tenant}"}} '
         '| event_name=~"guardrail.*|policy.*" [24h]))')


def run() -> None:
    with session_scope() as session:
        tenants = list(session.scalars(select(Tenant.slug)))
    loki = LokiClient()
    rows = []
    for tenant in tenants:
        try:
            results = loki.instant([tenant], QUERY.format(tenant=tenant))
        except httpx.HTTPError as exc:
            log.warning("loki query for %s failed: %s", tenant, exc)
            continue
        rows += [((tenant, labels["arc_product_id"]), value) for labels, value in results
                 if labels.get("arc_product_id")]
    metrics.replace(metrics.guardrail_violations, rows)
