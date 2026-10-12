"""Metrics exported by the intelligence service (scraped by Prometheus on 127.0.0.1:9464).

Every tenant-level metric carries arc_tenant_id so it is visible through the tenant's
label-enforcing data source in Grafana.
"""
from __future__ import annotations

from prometheus_client import CollectorRegistry, Gauge

REGISTRY = CollectorRegistry(auto_describe=True)

T, P, S, M = "arc_tenant_id", "arc_product_id", "arc_service_name", "gen_ai_request_model"

model_price = Gauge("arc_model_price_usd_per_token", "Model price per token (from ARC_MODEL_PRICES)",
                    [T, M, "direction"], registry=REGISTRY)
langfuse_cost = Gauge("arc_langfuse_cost_usd", "Cost reported by Langfuse per model",
                      [T, P, M, "window"], registry=REGISTRY)
quality_score = Gauge("arc_quality_score", "Average evaluation score over the last 24h (Langfuse scores)",
                      [T, P, "score_name"], registry=REGISTRY)
drift_index = Gauge("arc_drift_index", "Relative change of the last hour vs the 7-day baseline",
                    [T, P, S, "signal"], registry=REGISTRY)
anomaly_score = Gauge("arc_anomaly_score", "Absolute z-score of the last 15 minutes vs the last 24h",
                      [T, P, "signal"], registry=REGISTRY)
guardrail_violations = Gauge("arc_guardrail_violations_24h", "Guardrail/policy events in the last 24h (Loki)",
                             [T, P], registry=REGISTRY)
target_threshold = Gauge("arc_target_threshold", "KPI target set in ARC Tower", [T, P, "kpi"], registry=REGISTRY)
agents = Gauge("arc_agents", "Registered agents by status", [T, P, "status"], registry=REGISTRY)
connector_up = Gauge("arc_connector_up", "1 if the last pull-connector run succeeded", [T, P, "connector"],
                     registry=REGISTRY)
job_success = Gauge("arc_intelligence_job_success", "1 if the job's last run succeeded", ["job"], registry=REGISTRY)
job_last_run = Gauge("arc_intelligence_job_last_run_timestamp", "Unix time of the job's last run", ["job"],
                     registry=REGISTRY)


def replace(gauge: Gauge, rows: list[tuple[tuple[str, ...], float]]) -> None:
    """Swap all series of a gauge at once, so series that disappeared do not linger."""
    gauge.clear()
    for labels, value in rows:
        gauge.labels(*labels).set(value)
