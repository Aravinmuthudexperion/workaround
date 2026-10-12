"""Accumulate per-product ingest statistics in memory and flush them to the registry periodically:
last seen, field coverage, discovered agents, key usage."""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field

from sqlalchemy import select

from arc_tower.core.security import hash_key
from arc_tower.db.models import IngestKey, Product
from arc_tower.db.session import session_scope
from arc_tower.otlp.enrich import EnrichStats
from arc_tower.services import agents, coverage, products
from arc_tower.utils.timeutil import utcnow

log = logging.getLogger(__name__)

FLUSH_SECONDS = 10


@dataclass
class _Pending:
    stats: EnrichStats = field(default_factory=EnrichStats)
    platform: str = "custom"
    key_hashes: set[str] = field(default_factory=set)


class StatsAggregator:
    def __init__(self) -> None:
        self._pending: dict[int, _Pending] = {}
        self._lock = threading.Lock()

    def add(self, product_id: int, platform: str, key: str, stats: EnrichStats) -> None:
        with self._lock:
            pending = self._pending.setdefault(product_id, _Pending(platform=platform))
            pending.stats.merge(stats)
            pending.key_hashes.add(hash_key(key))

    def flush(self) -> None:
        with self._lock:
            batch, self._pending = self._pending, {}
        if not batch:
            return
        now = utcnow()
        with session_scope() as session:
            for product_id, pending in batch.items():
                product = session.get(Product, product_id)
                if product is None:
                    continue
                products.mark_seen(product, now)
                coverage.merge(product, pending.stats)
                for (service, agent_name), models in pending.stats.agents.items():
                    agents.upsert_discovered(session, product, service, agent_name, models, now, pending.platform)
                for key in session.scalars(select(IngestKey).where(IngestKey.key_hash.in_(pending.key_hashes))):
                    key.last_used_at = now
        log.debug("flushed ingest stats for %d products", len(batch))

    async def run(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            try:
                await asyncio.to_thread(self.flush)
            except Exception:  # keep ingesting even if the registry is briefly unavailable
                log.exception("stats flush failed")
