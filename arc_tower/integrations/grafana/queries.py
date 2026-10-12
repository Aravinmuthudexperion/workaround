"""PromQL used by the standard Grafana dashboards, alert rules and library panels.

`f` is the label filter for the current dashboard variables, e.g.
arc_product_id=~"$product",arc_service_name=~"$service".
Cost joins token counters with per-model prices exported by the intelligence service.
"""
from __future__ import annotations


def llm_calls(f: str, window: str = "$__rate_interval") -> str:
    return f'sum(increase(arc_calls_total{{{f},gen_ai_request_model!=""}}[{window}]))'


def calls_by(group: str, f: str, window: str = "$__range") -> str:
    return f'sum by ({group}) (increase(arc_calls_total{{{f},gen_ai_request_model!=""}}[{window}]))'


def error_rate(f: str, window: str = "$__range", by: str | None = None) -> str:
    grouping = f" by ({by})" if by else ""
    return (f'100 * sum{grouping} (increase(arc_calls_total{{{f},status_code="STATUS_CODE_ERROR"}}[{window}]))'
            f' / clamp_min(sum{grouping} (increase(arc_calls_total{{{f}}}[{window}])), 1)')


def p95_latency(f: str, window: str = "$__rate_interval", by: str | None = None) -> str:
    grouping = f"le, {by}" if by else "le"
    return (f"histogram_quantile(0.95, sum by ({grouping}) "
            f'(rate(arc_duration_milliseconds_bucket{{{f},gen_ai_request_model!=""}}[{window}])))')


def tokens(f: str, window: str = "$__range", by: str | None = None, direction: str | None = None) -> str:
    grouping = f" by ({by})" if by else ""
    parts = [f"sum{grouping} (increase(arc_gen_ai_{d}_tokens{{{f}}}[{window}]))"
             for d in ([direction] if direction else ["input", "output"])]
    return " + ".join(parts)


def cost(f: str, window: str = "$__range", by: str | None = None) -> str:
    """Estimated USD cost = tokens x model price (per tenant, so it works through the label proxy)."""
    grouping = f" by ({by})" if by else ""
    parts = []
    for direction in ("input", "output"):
        parts.append(
            f"sum{grouping} (increase(arc_gen_ai_{direction}_tokens{{{f}}}[{window}])"
            f" * on (arc_tenant_id, gen_ai_request_model) group_left() "
            f'arc_model_price_usd_per_token{{direction="{direction}"}})'
        )
    return "(" + " + ".join(parts) + ")"


def active_agents(f: str, window: str = "$__range") -> str:
    return (f"count(count by (arc_product_id, arc_service_name, gen_ai_agent_name) "
            f"(increase(arc_calls_total{{{f}}}[{window}]) > 0))")


def pii_masked(f: str, window: str = "$__range") -> str:
    return f"sum(increase(arc_pii_masked{{{f}}}[{window}]))"
