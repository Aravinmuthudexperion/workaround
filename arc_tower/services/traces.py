"""Trace explorer: list a product's requests from Langfuse and deep-link into its UI."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from arc_tower.db.models import Product
from arc_tower.integrations.langfuse import LangfuseError

from . import langfuse_routing


@dataclass
class TraceRow:
    id: str
    name: str
    timestamp: str
    user_id: str | None
    session_id: str | None
    latency_s: float | None
    cost_usd: float | None
    level: str
    url: str

    @property
    def failed(self) -> bool:
        return self.level == "ERROR"


@dataclass
class TracePage:
    rows: list[TraceRow]
    next_cursor: str | None = None
    error: str | None = None


def list_traces(product: Product, cursor: str | None = None, limit: int = 25, user_id: str | None = None,
                session_id: str | None = None, since: datetime | None = None) -> TracePage:
    client = langfuse_routing.client_for(product)
    if client is None:
        return TracePage([], error="Langfuse is not configured for this product. Add LANGFUSE_* keys in .env "
                                   "or the product's own Langfuse keys.")
    try:
        roots, next_cursor = client.root_observations(langfuse_routing.trace_tags(product), limit=limit,
                                                      cursor=cursor, user_id=user_id, session_id=session_id,
                                                      from_ts=since)
        costs = client.trace_costs([r["traceId"] for r in roots])
    except LangfuseError as exc:
        return TracePage([], error=str(exc))
    rows = [TraceRow(
        id=r["traceId"], name=r.get("traceName") or r.get("name") or "(unnamed)", timestamp=r.get("startTime", ""),
        user_id=r.get("userId"), session_id=r.get("sessionId"), latency_s=r.get("latency"),
        cost_usd=costs.get(r["traceId"]), level=r.get("level") or "DEFAULT",
        url=client.trace_url(r.get("projectId", ""), r["traceId"]),
    ) for r in roots]
    return TracePage(rows, next_cursor if len(roots) == limit else None)
