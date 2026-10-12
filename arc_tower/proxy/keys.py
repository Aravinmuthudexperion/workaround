"""Resolve an ingest key to the product it belongs to (cached, so the DB is not hit per request)."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from arc_tower.connectors import catalog
from arc_tower.db.session import session_scope
from arc_tower.otlp.enrich import TenantStamp
from arc_tower.services import langfuse_routing, products

_TTL_SECONDS = 60
_NEGATIVE_TTL_SECONDS = 10


@dataclass(frozen=True)
class ResolvedKey:
    product_id: int
    tenant_slug: str
    product_slug: str
    stamp: TenantStamp
    langfuse_auth: str | None


class KeyResolver:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, ResolvedKey | None]] = {}
        self._lock = threading.Lock()

    def resolve(self, key: str) -> ResolvedKey | None:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        resolved = self._load(key)
        ttl = _TTL_SECONDS if resolved else _NEGATIVE_TTL_SECONDS
        with self._lock:
            self._cache[key] = (now + ttl, resolved)
        return resolved

    @staticmethod
    def _load(key: str) -> ResolvedKey | None:
        with session_scope() as session:
            product = products.product_for_key(session, key)
            if product is None:
                return None
            spec = catalog.get(product.connector_type)
            return ResolvedKey(
                product_id=product.id,
                tenant_slug=product.tenant.slug,
                product_slug=product.slug,
                stamp=TenantStamp(product.tenant.slug, product.slug, product.environment,
                                  spec.platform if spec else "custom"),
                langfuse_auth=langfuse_routing.auth_header(product),
            )
