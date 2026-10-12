"""Anomaly: |z-score| of the last 15 minutes against the last 24 hours, per product."""
from __future__ import annotations

from arc_tower.integrations.prometheus import PrometheusClient

from .. import metrics

INTERVAL = 120
_BY = "arc_tenant_id, arc_product_id"

_SERIES = {
    "call_rate": f'sum by ({_BY}) (rate(arc_calls_total{{gen_ai_request_model!=""}}[15m]))',
    "error_ratio": (f'sum by ({_BY}) (rate(arc_calls_total{{status_code="STATUS_CODE_ERROR"}}[15m])) / '
                    f"clamp_min(sum by ({_BY}) (rate(arc_calls_total[15m])), 1e-9)"),
}


# At least one hour of 5-minute baseline samples before a score is meaningful.
MIN_BASELINE_SAMPLES = 12


def _zscore(expr: str) -> str:
    return (f"(abs(({expr}) - avg_over_time(({expr})[24h:5m])) / "
            f"clamp_min(stddev_over_time(({expr})[24h:5m]), 1e-6)) "
            f"and on ({_BY}) (count_over_time(({expr})[24h:5m]) >= {MIN_BASELINE_SAMPLES})")


def run() -> None:
    prom = PrometheusClient()
    rows = []
    for signal, expr in _SERIES.items():
        for sample in prom.query(_zscore(expr)):
            tenant, product = sample.labels.get("arc_tenant_id"), sample.labels.get("arc_product_id")
            if tenant and product:
                rows.append(((tenant, product, signal), min(sample.value, 50.0)))
    metrics.replace(metrics.anomaly_score, rows)
