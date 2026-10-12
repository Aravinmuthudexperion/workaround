"""Registry state as metrics: agents by status, KPI targets, pull-connector health."""
from __future__ import annotations

from collections import Counter

from sqlalchemy import select

from arc_tower.db.models import Agent, Connector, Product, Target
from arc_tower.db.session import session_scope
from arc_tower.services import agents as agent_service

from .. import metrics

INTERVAL = 60


def run() -> None:
    with session_scope() as session:
        counts: Counter = Counter()
        for agent in session.scalars(select(Agent)):
            counts[(agent.product.tenant.slug, agent.product.slug, agent_service.status(agent))] += 1
        targets = [((t.product.tenant.slug, t.product.slug, t.kpi_key), t.threshold)
                   for t in session.scalars(select(Target))]
        connectors = [((c.product.tenant.slug, c.product.slug, c.type), 1.0 if c.last_status == "ok" else 0.0)
                      for c in session.scalars(select(Connector).join(Product).where(Connector.enabled.is_(True)))]
    metrics.replace(metrics.agents, [(k, float(v)) for k, v in counts.items()])
    metrics.replace(metrics.target_threshold, targets)
    metrics.replace(metrics.connector_up, connectors)
