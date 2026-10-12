"""Drift: how far each agent's last hour departs from its 7-day baseline (latency, output length)."""
from __future__ import annotations

from arc_tower.integrations.prometheus import PrometheusClient

from .. import metrics

INTERVAL = 300
_BY = "arc_tenant_id, arc_product_id, arc_service_name"

SIGNALS = {
    "p95_latency": ("histogram_quantile(0.95, sum by (le, {by}) "
                    '(rate(arc_duration_milliseconds_bucket{{gen_ai_request_model!=""}}[{w}])))'),
    "output_tokens_per_call": ("sum by ({by}) (increase(arc_gen_ai_output_tokens[{w}])) / "
                               'clamp_min(sum by ({by}) (increase(arc_calls_total{{gen_ai_request_model!=""}}[{w}])), 1)'),
}


def _key(labels: dict[str, str]) -> tuple[str, str, str]:
    return labels.get("arc_tenant_id", ""), labels.get("arc_product_id", ""), labels.get("arc_service_name", "")


def run() -> None:
    prom = PrometheusClient()
    rows = []
    for signal, template in SIGNALS.items():
        current = {_key(s.labels): s.value for s in prom.query(template.format(by=_BY, w="1h"))}
        baseline = {_key(s.labels): s.value for s in prom.query(template.format(by=_BY, w="7d"))}
        for key, value in current.items():
            base = baseline.get(key)
            if base and base > 0 and all(key):
                rows.append(((*key, signal), abs(value - base) / base))
    metrics.replace(metrics.drift_index, rows)
