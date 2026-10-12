"""Cost as calculated by Langfuse (its model price table), per product and model."""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select

from arc_tower.db.models import Product
from arc_tower.db.session import session_scope
from arc_tower.integrations.langfuse import LangfuseError
from arc_tower.services import langfuse_routing
from arc_tower.utils.timeutil import utcnow

from .. import metrics

INTERVAL = 300
WINDOWS = {"24h": timedelta(hours=24), "30d": timedelta(days=30)}
log = logging.getLogger("arc.intelligence.finops")


def run() -> None:
    rows = []
    now = utcnow()
    with session_scope() as session:
        product_list = list(session.scalars(select(Product)))
        targets = [(p, langfuse_routing.client_for(p), langfuse_routing.trace_tags(p), p.tenant.slug)
                   for p in product_list]
    for product, client, tags, tenant in targets:
        if client is None:
            continue
        for window, span in WINDOWS.items():
            try:
                for row in client.cost_by_model(tags, now - span, now):
                    rows.append(((tenant, product.slug, row["model"], window), row["cost"]))
            except LangfuseError as exc:
                log.warning("langfuse cost for %s: %s", product.slug, exc)
                break
    metrics.replace(metrics.langfuse_cost, rows)
