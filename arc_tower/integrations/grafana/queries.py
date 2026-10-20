"""PromQL used by the standard Grafana dashboards, alert rules and library panels.

`f` is the label filter for the current dashboard variables, e.g.
arc_product_id=~"$product",arc_service_name=~"$service".
Cost joins token counters with per-model prices exported by the intelligence service.
"""
from __future__ import annotations


def counted(selector: str, window: str) -> str:
    """Exact rise of each counter series over the window, from the stored samples.
    Plain increase() extrapolates to the window edges, so a stat showed 38.4, 39.2, 38.8 for the same
    38 calls on successive refreshes. `anchored` (Prometheus extended range selectors) does not
    extrapolate and handles counter resets, but measures from the first sample it finds: a series born
    inside the window with a non-zero first sample (a connector's first batch of 30 calls arrives as
    one sample of 30) would lose that batch, so its first value is added back. Needs the
    extended-range-selector and experimental-function flags on Prometheus and prom-label-proxy (set by
    the launcher)."""
    rise = f"increase({selector}[{window}] anchored)"
    born_inside = f"(first_over_time({selector}[{window}]) unless ({selector} offset {window}))"
    return f"({rise} + ({born_inside} or {rise} * 0))"


def llm_calls(f: str, window: str = "$__rate_interval") -> str:
    selector = f'arc_calls_total{{{f},gen_ai_request_model!=""}}'
    return f"sum({counted(selector, window)})"


def calls_by(group: str, f: str, window: str = "$__range") -> str:
    selector = f'arc_calls_total{{{f},gen_ai_request_model!=""}}'
    return f"sum by ({group}) ({counted(selector, window)})"


def error_rate(f: str, window: str = "$__range", by: str | None = None) -> str:
    grouping = f" by ({by})" if by else ""
    error_selector = f'arc_calls_total{{{f},status_code="STATUS_CODE_ERROR"}}'
    errors = f"sum{grouping} ({counted(error_selector, window)})"
    total = f"sum{grouping} ({counted(f'arc_calls_total{{{f}}}', window)})"
    return f"100 * ({errors} or {total} * 0) / clamp_min({total}, 1)"     # 0 %, not "no data", without errors


def p95_latency(f: str, window: str = "$__rate_interval", by: str | None = None, quantile: float = 0.95) -> str:
    grouping = f"le, {by}" if by else "le"
    buckets = counted(f'arc_duration_milliseconds_bucket{{{f},gen_ai_request_model!=""}}', window)
    return f"histogram_quantile({quantile}, sum by ({grouping}) ({buckets}))"


def tokens(f: str, window: str = "$__range", by: str | None = None, direction: str | None = None) -> str:
    grouping = f" by ({by})" if by else ""
    parts = [f"sum{grouping} ({counted(f'arc_gen_ai_{d}_tokens{{{f}}}', window)})"
             for d in ([direction] if direction else ["input", "output"])]
    return " + ".join(parts)


def cost(f: str, window: str = "$__range", by: str | None = None) -> str:
    """Estimated USD cost = tokens x model price (per tenant, so it works through the label proxy)."""
    grouping = f" by ({by})" if by else ""
    parts = []
    for direction in ("input", "output"):
        parts.append(
            f"sum{grouping} ({counted(f'arc_gen_ai_{direction}_tokens{{{f}}}', window)}"
            f" * on (arc_tenant_id, gen_ai_request_model) group_left() "
            f'arc_model_price_usd_per_token{{direction="{direction}"}})'
        )
    return "(" + " + ".join(parts) + ")"


def active_agents(f: str, window: str = "$__range") -> str:
    return (f"count(count by (arc_product_id, arc_service_name, gen_ai_agent_name) "
            f"({counted(f'arc_calls_total{{{f}}}', window)} > 0))")


def pii_masked(f: str, window: str = "$__range") -> str:
    return f"sum({counted(f'arc_pii_masked{{{f}}}', window)})"
