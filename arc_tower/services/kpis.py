"""KPI catalog (PPT slide 10 categories), targets, and current KPI values.

Every KPI query goes through `promql.selector`, so a user only ever sees their own tenants.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from arc_tower.core.constants import KpiCategory
from arc_tower.db.models import KpiDefinition, Product, Target
from arc_tower.integrations.grafana import queries as q
from arc_tower.integrations.prometheus import PrometheusClient
from arc_tower.integrations.prometheus.client import PrometheusError
from arc_tower.integrations.prometheus.promql import render

from . import alerts, audit

S = "{sel}"
W = "{window}"

# key, name, category, unit, breach, default threshold, alertable, promql, description
_CATALOG = [
    ("llm_calls", "LLM calls", KpiCategory.ADOPTION, "calls", "below", None, False,
     q.llm_calls(S, W), "Model calls in the window."),
    ("active_agents", "Active agents", KpiCategory.ADOPTION, "agents", "below", None, False,
     q.active_agents(S, W), "Distinct agents with traffic in the window."),
    ("error_rate", "Error rate", KpiCategory.PERFORMANCE, "%", "above", 5.0, True,
     q.error_rate(S, W), "Share of spans that ended in error."),
    ("p95_latency_ms", "p95 LLM latency", KpiCategory.PERFORMANCE, "ms", "above", 8000.0, True,
     q.p95_latency(S, W), "95th percentile duration of model calls."),
    ("anomaly_score", "Anomaly score", KpiCategory.PERFORMANCE, "z", "above", 3.0, True,
     "max(arc_anomaly_score{" + S + "})", "Largest deviation of traffic/errors from the 24h baseline."),
    ("tokens", "Tokens", KpiCategory.COST, "tokens", "above", None, True,
     q.tokens(S, W), "Input + output tokens in the window."),
    ("cost_usd", "Estimated cost", KpiCategory.COST, "USD", "above", None, True,
     q.cost(S, W), "Tokens x model price. Set a threshold to use it as a budget."),
    ("cost_per_1k_calls", "Cost per 1k LLM calls", KpiCategory.COST, "USD", "above", None, True,
     f"1000 * {q.cost(S, W)} / clamp_min({q.llm_calls(S, W)}, 1)", "Unit cost of the product."),
    ("quality_score", "Quality score", KpiCategory.QUALITY, "0-1", "below", 0.7, True,
     "avg(arc_quality_score{" + S + "})", "Average evaluation score from Langfuse (24h)."),
    ("drift_index", "Drift index", KpiCategory.QUALITY, "ratio", "above", 0.5, True,
     "max(arc_drift_index{" + S + "})", "Relative change of latency/tokens vs the 7-day baseline."),
    ("pii_masked", "PII values masked", KpiCategory.SECURITY, "values", "above", 0.0, True,
     q.pii_masked(S, W), "PII found and masked by the gateway."),
    ("guardrail_violations", "Guardrail violations", KpiCategory.SECURITY, "events", "above", 0.0, True,
     "sum(arc_guardrail_violations_24h{" + S + "})", "Guardrail / policy events reported by agents (24h)."),
]

WINDOWS = {"1h": "1h", "24h": "24h", "7d": "7d", "30d": "30d"}


def seed_catalog(session: Session) -> None:
    for key, name, category, unit, breach, threshold, alertable, promql, description in _CATALOG:
        row = session.get(KpiDefinition, key) or KpiDefinition(key=key)
        row.name, row.category, row.unit, row.breach = name, category.value, unit, breach
        row.default_threshold, row.alertable, row.promql, row.description = threshold, alertable, promql, description
        session.merge(row)


def catalog(session: Session) -> list[KpiDefinition]:
    order = {c.value: i for i, c in enumerate(KpiCategory)}
    rows = list(session.scalars(select(KpiDefinition)))
    return sorted(rows, key=lambda k: (order.get(k.category, 99), k.name))


@dataclass
class KpiValue:
    kpi: KpiDefinition
    value: float | None
    target: Target | None = None

    @property
    def breached(self) -> bool:
        if self.value is None or self.target is None:
            return False
        if self.kpi.breach == "above":
            return self.value > self.target.threshold
        return self.value < self.target.threshold


def evaluate(session: Session, sel: str, window: str = "24h", targets: dict[str, Target] | None = None,
             prom: PrometheusClient | None = None) -> list[KpiValue]:
    prom = prom or PrometheusClient()
    values = []
    for kpi in catalog(session):
        try:
            value = prom.scalar(render(kpi.promql, sel or 'arc_tenant_id!=""', WINDOWS.get(window, "24h")))
        except PrometheusError:
            value = None
        values.append(KpiValue(kpi, value, (targets or {}).get(kpi.key)))
    return values


def targets_for(product: Product) -> dict[str, Target]:
    return {t.kpi_key: t for t in product.targets}


def save_targets(session: Session, actor: str, product: Product, entries: dict[str, dict]) -> None:
    """entries: kpi_key -> {"threshold": float|None, "alert": bool, "severity": str}."""
    existing = targets_for(product)
    for key, entry in entries.items():
        threshold = entry.get("threshold")
        target = existing.get(key)
        if threshold is None:
            if target:
                alerts.delete_rule_for(target)
                product.targets.remove(target)
                session.delete(target)
            continue
        if target is None:
            target = Target(product_id=product.id, kpi_key=key, threshold=threshold)
            session.add(target)
            product.targets.append(target)
        target.threshold = threshold
        target.alert_enabled = bool(entry.get("alert"))
        target.severity = entry.get("severity") or "warning"
    session.flush()
    audit.record(session, actor, "targets.save", "product", product.id, tenant_id=product.tenant_id,
                 kpis=sorted(k for k, e in entries.items() if e.get("threshold") is not None))
