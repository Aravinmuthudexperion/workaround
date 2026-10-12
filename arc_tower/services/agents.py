"""Agent registry: auto-discovered from telemetry, enriched by owners."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from arc_tower.db.models import Agent, Product
from arc_tower.utils.timeutil import as_utc, utcnow

from . import audit

ACTIVE_WINDOW = timedelta(hours=24)
RISK_CLASSES = ("unclassified", "minimal", "limited", "high")


def status(agent: Agent, now: datetime | None = None) -> str:
    seen = as_utc(agent.last_seen_at)
    if seen is None:
        return "registered"
    return "active" if (now or utcnow()) - seen <= ACTIVE_WINDOW else "inactive"


def upsert_discovered(session: Session, product: Product, service_name: str, agent_name: str,
                      models: set[str], seen_at: datetime, platform: str) -> Agent:
    agent = session.scalar(select(Agent).where(Agent.product_id == product.id,
                                               Agent.service_name == service_name,
                                               Agent.agent_name == agent_name))
    if agent is None:
        agent = Agent(product_id=product.id, service_name=service_name, agent_name=agent_name,
                      platform=platform, discovered=True, models=[], first_seen_at=seen_at)
        session.add(agent)
    merged = sorted(set(agent.models or []) | models)
    if merged != (agent.models or []):
        agent.models = merged
    agent.last_seen_at = seen_at
    return agent


def list_agents(session: Session, tenant_ids: list[int] | None, product_id: int | None = None) -> list[Agent]:
    stmt = (select(Agent).join(Product).options(selectinload(Agent.product).selectinload(Product.tenant))
            .order_by(Product.name, Agent.service_name, Agent.agent_name))
    if tenant_ids is not None:
        stmt = stmt.where(Product.tenant_id.in_(tenant_ids or [-1]))
    if product_id:
        stmt = stmt.where(Agent.product_id == product_id)
    return list(session.scalars(stmt))


def register_manual(session: Session, actor: str, product: Product, service_name: str, **metadata) -> Agent:
    agent = Agent(product_id=product.id, service_name=service_name.strip(), agent_name="", discovered=False,
                  platform=product.connector_type, models=[])
    session.add(agent)
    session.flush()
    update_metadata(session, actor, agent, **metadata)
    return agent


def update_metadata(session: Session, actor: str, agent: Agent, display_name: str = "", owner: str = "",
                    purpose: str = "", risk_class: str = "unclassified") -> None:
    agent.display_name = display_name.strip()
    agent.owner = owner.strip()
    agent.purpose = purpose.strip()
    agent.risk_class = risk_class if risk_class in RISK_CLASSES else "unclassified"
    audit.record(session, actor, "agent.update", "agent", agent.id, tenant_id=agent.product.tenant_id,
                 risk_class=agent.risk_class)
