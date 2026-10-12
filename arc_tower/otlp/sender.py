"""Send OTLP requests to the internal collector with ARC routing headers."""
from __future__ import annotations

from google.protobuf.message import Message

from arc_tower.core.config import COLLECTOR_OTLP_URL
from arc_tower.core.constants import HDR_LANGFUSE_AUTH, HDR_PRODUCT, HDR_TENANT
from arc_tower.utils.http import client

from .codec import PROTOBUF, Signal, encode


def routing_headers(tenant_slug: str, product_slug: str, langfuse_auth: str | None) -> dict[str, str]:
    headers = {HDR_TENANT: tenant_slug, HDR_PRODUCT: product_slug}
    if langfuse_auth:
        headers[HDR_LANGFUSE_AUTH] = langfuse_auth
    return headers


class CollectorSender:
    """Posts already-enriched requests to the collector's OTLP/HTTP receiver."""

    def __init__(self, base_url: str = COLLECTOR_OTLP_URL):
        self._http = client(base_url)

    def send(self, signal: Signal, request: Message, headers: dict[str, str]) -> None:
        resp = self._http.post(f"/v1/{signal.value}", content=encode(request),
                               headers={"Content-Type": PROTOBUF, **headers})
        resp.raise_for_status()

    def close(self) -> None:
        self._http.close()
