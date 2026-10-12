"""Liveness endpoint and component health."""
from __future__ import annotations

import httpx
from fastapi import APIRouter

from arc_tower.core.config import COLLECTOR_OTLP_URL, get_settings
from arc_tower.core.ports import PORTS
from arc_tower.integrations.grafana import GrafanaClient
from arc_tower.integrations.loki import LokiClient
from arc_tower.integrations.prometheus import PrometheusClient

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"status": "ok"}


def _http_ok(url: str) -> bool:
    try:
        return httpx.get(url, timeout=2).status_code < 500
    except httpx.HTTPError:
        return False


def component_health() -> list[dict]:
    settings = get_settings()
    langfuse = None
    if settings.langfuse_enabled:
        from arc_tower.integrations.langfuse import LangfuseClient
        langfuse = LangfuseClient(settings.langfuse_host, settings.langfuse_public_key,
                                  settings.langfuse_secret_key).health()
    return [
        {"name": "Ingest proxy", "ok": _http_ok(f"http://127.0.0.1:{settings.proxy_port}/healthz"),
         "detail": settings.public_ingest_url},
        {"name": "OTel Collector", "ok": _http_ok(f"http://127.0.0.1:{PORTS['collector_health']}/"),
         "detail": COLLECTOR_OTLP_URL},
        {"name": "Prometheus", "ok": PrometheusClient().healthy(), "detail": "KPI time-series"},
        {"name": "Loki", "ok": LokiClient().healthy(), "detail": "logs & events"},
        {"name": "Grafana", "ok": GrafanaClient().healthy(), "detail": "dashboards & alerting"},
        {"name": "Intelligence jobs", "ok": _http_ok(f"http://127.0.0.1:{PORTS['intelligence']}/metrics"),
         "detail": "FinOps, quality, drift, anomaly, compliance"},
        {"name": "Langfuse", "ok": langfuse,
         "detail": settings.langfuse_host if settings.langfuse_enabled else "not configured (optional)"},
    ]
