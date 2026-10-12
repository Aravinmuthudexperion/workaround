"""Chart data for ARC's native pages (overview, product, executive). Tenant scope enforced via `sel`."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from arc_tower.integrations.grafana import queries as q
from arc_tower.integrations.prometheus import PrometheusClient
from arc_tower.integrations.prometheus.client import PrometheusError
from arc_tower.utils.timeutil import utcnow

from .layouts import GROUPS, METRICS

RANGES = {"6h": (timedelta(hours=6), 300), "24h": (timedelta(hours=24), 900), "7d": (timedelta(days=7), 3600)}


@dataclass
class Chart:
    labels: list[str]                       # x axis (ISO timestamps)
    datasets: list[dict]                    # [{"label": str, "data": [float|None]}]
    unit: str = ""

    def as_dict(self) -> dict:
        return {"labels": self.labels, "datasets": self.datasets, "unit": self.unit}


def _range_chart(prom: PrometheusClient, expr: str, legend_label: str, range_key: str, unit: str) -> Chart:
    span, step = RANGES.get(range_key, RANGES["24h"])
    end = utcnow()
    start = end - span
    try:
        series = prom.query_range(expr, start, end, step)
    except PrometheusError:
        series = []
    stamps = [start.timestamp() + i * step for i in range(int(span.total_seconds() // step) + 1)]
    datasets = []
    for s in sorted(series, key=lambda s: -sum(v for _, v in s.points)):
        by_ts = {round(t): v for t, v in s.points}
        datasets.append({"label": s.labels.get(legend_label, "") or "(none)" if legend_label else "Total",
                         "data": [by_ts.get(round(t)) for t in stamps]})
    labels = [datetime.fromtimestamp(t, tz=timezone.utc).isoformat() for t in stamps]
    return Chart(labels, datasets[:8], unit)


def overview_charts(sel: str, range_key: str = "24h", group: str = "arc_product_id") -> dict[str, dict]:
    prom = PrometheusClient()
    step = f"{RANGES.get(range_key, RANGES['24h'])[1]}s"
    return {
        "calls": _range_chart(prom, q.calls_by(group, sel, step), group, range_key, "calls").as_dict(),
        "cost": _range_chart(prom, q.cost(sel, step, by=group), group, range_key, "USD").as_dict(),
        "latency": _range_chart(prom, q.p95_latency(sel, step, by=group), group, range_key, "ms").as_dict(),
        "errors": _range_chart(prom, q.error_rate(sel, step, by=group), group, range_key, "%").as_dict(),
    }


def breakdown(sel: str, by: str, measure: str = "cost", window: str = "24h") -> list[tuple[str, float]]:
    """Totals grouped by one label, or by several ("a, b") in which case keys are tuples."""
    expr = {
        "cost": q.cost(sel, window, by=by),
        "calls": q.calls_by(by, sel, window),
        "tokens": q.tokens(sel, window, by=by),
    }[measure]
    try:
        samples = PrometheusClient().query(expr)
    except PrometheusError:
        return []
    labels = [label.strip() for label in by.split(",")]
    if len(labels) == 1:
        rows = [(s.labels.get(labels[0], "unknown"), s.value) for s in samples]
    else:
        rows = [(tuple(s.labels.get(label, "") for label in labels), s.value) for s in samples]
    return sorted(rows, key=lambda r: -r[1])


def tenant_rollup(sel: str, window: str = "24h") -> dict[str, dict[str, float]]:
    """Per-tenant totals for the executive view."""
    prom = PrometheusClient()
    exprs = {
        "calls": q.calls_by("arc_tenant_id", sel, window),
        "cost": q.cost(sel, window, by="arc_tenant_id"),
        "error_rate": q.error_rate(sel, window, by="arc_tenant_id"),
        "tokens": q.tokens(sel, window, by="arc_tenant_id"),
    }
    table: dict[str, dict[str, float]] = {}
    for name, expr in exprs.items():
        try:
            for sample in prom.query(expr):
                table.setdefault(sample.labels.get("arc_tenant_id", "?"), {})[name] = sample.value
        except PrometheusError:
            continue
    return table


# --- configurable charts (Overview / agent pages) ----------------------------------------------

RANGE_WINDOWS = {"6h": "6h", "24h": "24h", "7d": "7d"}


def _quantile(q_value: float, sel: str, window: str, by: str | None) -> str:
    grouping = f"le, {by}" if by else "le"
    return (f"histogram_quantile({q_value}, sum by ({grouping}) "
            f'(rate(arc_duration_milliseconds_bucket{{{sel},gen_ai_request_model!=""}}[{window}])))')


def chart_expr(metric: str, sel: str, window: str, by: str | None) -> str:
    grouping = f" by ({by})" if by else ""
    builders = {
        "calls": lambda: f'sum{grouping} (increase(arc_calls_total{{{sel},gen_ai_request_model!=""}}[{window}]))',
        "cost": lambda: q.cost(sel, window, by=by),
        "tokens": lambda: q.tokens(sel, window, by=by),
        "input_tokens": lambda: q.tokens(sel, window, by=by, direction="input"),
        "output_tokens": lambda: q.tokens(sel, window, by=by, direction="output"),
        "p95_latency": lambda: _quantile(0.95, sel, window, by),
        "p50_latency": lambda: _quantile(0.5, sel, window, by),
        "error_rate": lambda: q.error_rate(sel, window, by=by),
        "pii_masked": lambda: f"sum{grouping} (increase(arc_pii_masked{{{sel}}}[{window}]))",
    }
    return builders[metric]()


def chart_data(sel: str, chart: dict, range_key: str = "24h") -> dict:
    """Data for one configured chart: time series for line/bar, totals over the range for pie."""
    metric = METRICS[chart["metric"]]
    by = GROUPS[chart["group"]].prom_label
    span, step = RANGES.get(range_key, RANGES["24h"])
    if chart["kind"] == "pie":
        try:
            samples = PrometheusClient().query(chart_expr(metric.key, sel, RANGE_WINDOWS.get(range_key, "24h"), by))
        except PrometheusError:
            samples = []
        rows = sorted(((s.labels.get(by, "") or "(none)" if by else "Total", s.value) for s in samples
                       if s.value > 0), key=lambda r: -r[1])[:8]
        return {"kind": "pie", "labels": [r[0] for r in rows], "values": [r[1] for r in rows], "unit": metric.unit}
    data = _range_chart(PrometheusClient(), chart_expr(metric.key, sel, f"{step}s", by), by, range_key,
                        metric.unit).as_dict()
    return {**data, "kind": chart["kind"]}
