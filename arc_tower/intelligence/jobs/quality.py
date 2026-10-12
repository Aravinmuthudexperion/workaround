"""Quality: average Langfuse evaluation scores (LLM-as-judge, annotations, user feedback) per product.

Configure evaluators in Langfuse (e.g. managed LLM-as-judge for helpfulness, hallucination,
toxicity); their scores appear here and on the Quality & Compliance dashboard.
"""
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
log = logging.getLogger("arc.intelligence.quality")


def run() -> None:
    end = utcnow()
    start = end - timedelta(hours=24)
    rows = []
    with session_scope() as session:
        products = [(p.slug, p.tenant.slug, langfuse_routing.client_for(p), langfuse_routing.trace_tags(p))
                    for p in session.scalars(select(Product))]
    for product, tenant, client, tags in products:
        if client is None:
            continue
        try:
            averages = client.average_scores(tags, start, end)
        except LangfuseError as exc:
            log.warning("langfuse scores for %s: %s", product, exc)
            continue
        rows += [((tenant, product, name), value) for name, value in averages.items()]
    metrics.replace(metrics.quality_score, rows)
