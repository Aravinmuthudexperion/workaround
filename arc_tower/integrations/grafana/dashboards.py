"""Standard ARC dashboards and the library-panel catalog.

The same dashboard UIDs exist in every tenant org, so ARC can deep-link to
/grafana/d/<uid>?orgId=<tenant org>&var-product=<slug>.
Tenant isolation comes from the org's data source (prom-label-proxy), not from these queries.
"""
from __future__ import annotations

from . import panels as p
from . import queries as q

OVERVIEW_UID = "arc-overview"
FINOPS_UID = "arc-finops"
QUALITY_UID = "arc-quality"
EXEC_UID = "arc-exec"
LIBRARY_UID = "arc-library"

STANDARD_FOLDER_UID = "arc-standard"
MINE_FOLDER_UID = "arc-mine"
ALERTS_FOLDER_UID = "arc-alerts"

_TENANT_FILTER = 'arc_product_id=~"$product",arc_service_name=~"$service"'
_TENANT_VARS = [
    p.query_var("product", "Product", "label_values(arc_calls_total, arc_product_id)"),
    p.query_var("service", "Agent service",
                'label_values(arc_calls_total{arc_product_id=~"$product"}, arc_service_name)'),
]


def overview() -> dict:
    f = _TENANT_FILTER
    panels = [
        p.stat("LLM calls", q.llm_calls(f, "$__range"), grid=(0, 0, 4, 4)),
        p.stat("Active agents", q.active_agents(f), grid=(4, 0, 4, 4)),
        p.stat("Error rate", q.error_rate(f), unit="percent", decimals=1, grid=(8, 0, 4, 4),
               thresholds=[("green", None), ("orange", 2), ("red", 5)]),
        p.stat("p95 latency", q.p95_latency(f, "$__range"), unit="ms", grid=(12, 0, 4, 4),
               thresholds=[("green", None), ("orange", 3000), ("red", 8000)]),
        p.stat("Tokens", q.tokens(f), grid=(16, 0, 4, 4)),
        p.stat("Est. cost", q.cost(f), unit="currencyUSD", decimals=2, grid=(20, 0, 4, 4)),
        p.timeseries("LLM calls per minute by agent",
                     [(f'sum by (arc_service_name) (rate(arc_calls_total{{{f},gen_ai_request_model!=""}}'
                       f'[$__rate_interval])) * 60', "{{arc_service_name}}")],
                     unit="short", grid=(0, 4, 12, 8)),
        p.timeseries("p95 latency by agent", [(q.p95_latency(f, by="arc_service_name"), "{{arc_service_name}}")],
                     unit="ms", grid=(12, 4, 12, 8)),
        p.timeseries("Error rate by agent", [(q.error_rate(f, "$__rate_interval", by="arc_service_name"),
                                              "{{arc_service_name}}")],
                     unit="percent", grid=(0, 12, 12, 8)),
        p.bars("Tokens by model", q.tokens(f, by="gen_ai_request_model"), "{{gen_ai_request_model}}",
               grid=(12, 12, 12, 8)),
        p.logs("Errors and guardrail events",
               '{arc_product_id=~"$product"} | severity_text=~"ERROR|WARN|error|warn"', grid=(0, 20, 24, 9)),
    ]
    return p.dashboard(OVERVIEW_UID, "ARC · Agent Overview", panels, _TENANT_VARS, ["arc", "standard"],
                       "Adoption, performance and reliability of your AI agents.")


def finops() -> dict:
    f = _TENANT_FILTER
    panels = [
        p.stat("Est. cost (range)", q.cost(f), unit="currencyUSD", decimals=2, grid=(0, 0, 6, 4)),
        p.stat("Cost per 1k LLM calls", f"1000 * {q.cost(f)} / clamp_min({q.llm_calls(f, '$__range')}, 1)",
               unit="currencyUSD", decimals=3, grid=(6, 0, 6, 4)),
        p.stat("Input tokens", q.tokens(f, direction="input"), grid=(12, 0, 6, 4)),
        p.stat("Output tokens", q.tokens(f, direction="output"), grid=(18, 0, 6, 4)),
        p.timeseries("Est. cost per hour by product", [(q.cost(f, "1h", by="arc_product_id"), "{{arc_product_id}}")],
                     unit="currencyUSD", grid=(0, 4, 12, 8), stack=True, draw="bars"),
        p.pie("Cost share by model", q.cost(f, by="gen_ai_request_model"), "{{gen_ai_request_model}}",
              unit="currencyUSD", grid=(12, 4, 12, 8)),
        p.bars("Cost by agent", q.cost(f, by="arc_service_name"), "{{arc_service_name}}", unit="currencyUSD",
               grid=(0, 12, 12, 8)),
        p.timeseries("Tokens per minute", [
            (f"sum(rate(arc_gen_ai_input_tokens{{{f}}}[$__rate_interval])) * 60", "input"),
            (f"sum(rate(arc_gen_ai_output_tokens{{{f}}}[$__rate_interval])) * 60", "output"),
        ], grid=(12, 12, 12, 8)),
        p.bars("Langfuse-reported cost (24h)", 'sum by (gen_ai_request_model) '
               '(arc_langfuse_cost_usd{arc_product_id=~"$product",window="24h"})', "{{gen_ai_request_model}}",
               unit="currencyUSD", grid=(0, 20, 24, 7),
               description="Cost calculated by Langfuse from its model price table (refreshed every 5 min)."),
    ]
    return p.dashboard(FINOPS_UID, "ARC · FinOps", panels, _TENANT_VARS, ["arc", "standard", "finops"],
                       "AI spend by product, agent and model.", time_from="now-24h")


def quality() -> dict:
    f = _TENANT_FILTER
    prod = 'arc_product_id=~"$product"'
    panels = [
        p.stat("Avg quality score (24h)", f"avg(arc_quality_score{{{prod}}})", decimals=2, grid=(0, 0, 6, 4),
               thresholds=[("red", None), ("orange", 0.6), ("green", 0.8)]),
        p.stat("Max drift index", f"max(arc_drift_index{{{prod}}})", decimals=2, grid=(6, 0, 6, 4),
               thresholds=[("green", None), ("orange", 0.3), ("red", 0.6)]),
        p.stat("PII values masked", q.pii_masked(f), grid=(12, 0, 6, 4),
               thresholds=[("green", None), ("orange", 1)]),
        p.stat("Guardrail violations (24h)", f"sum(arc_guardrail_violations_24h{{{prod}}})", grid=(18, 0, 6, 4),
               thresholds=[("green", None), ("red", 1)]),
        p.timeseries("Quality score by metric", [(f"avg by (score_name) (arc_quality_score{{{prod}}})",
                                                  "{{score_name}}")], grid=(0, 4, 12, 8)),
        p.timeseries("Drift index by agent and signal",
                     [(f"max by (arc_service_name, signal) (arc_drift_index{{{prod}}})",
                       "{{arc_service_name}} · {{signal}}")], grid=(12, 4, 12, 8)),
        p.timeseries("Anomaly score", [(f"max by (arc_product_id, signal) (arc_anomaly_score{{{prod}}})",
                                        "{{arc_product_id}} · {{signal}}")], grid=(0, 12, 12, 8)),
        p.timeseries("PII values masked per hour",
                     [(f"sum by (arc_service_name) (increase(arc_pii_masked{{{f}}}[1h]))", "{{arc_service_name}}")],
                     grid=(12, 12, 12, 8), draw="bars"),
        p.logs("Guardrail and policy events",
               '{arc_product_id=~"$product"} | event_name=~"guardrail.*|policy.*"', grid=(0, 20, 24, 9)),
    ]
    return p.dashboard(QUALITY_UID, "ARC · Quality & Compliance", panels, _TENANT_VARS,
                       ["arc", "standard", "quality"], "Evaluation scores, drift, PII and guardrails.",
                       time_from="now-24h")


def executive() -> dict:
    """Platform org only: cross-tenant rollups for CTO / CFO views."""
    f = 'arc_tenant_id=~"$tenant"'
    variables = [p.query_var("tenant", "Tenant", "label_values(arc_calls_total, arc_tenant_id)")]
    panels = [
        p.stat("Tenants live", 'count(count by (arc_tenant_id) (increase(arc_calls_total{arc_tenant_id=~"$tenant"}'
               '[$__range]) > 0))', grid=(0, 0, 4, 4)),
        p.stat("Active agents", q.active_agents(f), grid=(4, 0, 4, 4)),
        p.stat("LLM calls", q.llm_calls(f, "$__range"), grid=(8, 0, 4, 4)),
        p.stat("Est. AI spend", q.cost(f), unit="currencyUSD", decimals=2, grid=(12, 0, 4, 4)),
        p.stat("Error rate", q.error_rate(f), unit="percent", decimals=1, grid=(16, 0, 4, 4),
               thresholds=[("green", None), ("orange", 2), ("red", 5)]),
        p.stat("PII masked", q.pii_masked(f), grid=(20, 0, 4, 4)),
        p.bars("AI spend by tenant", q.cost(f, by="arc_tenant_id"), "{{arc_tenant_id}}", unit="currencyUSD",
               grid=(0, 4, 12, 8)),
        p.bars("LLM calls by tenant", q.calls_by("arc_tenant_id", f), "{{arc_tenant_id}}", grid=(12, 4, 12, 8)),
        p.timeseries("Est. spend per hour by tenant", [(q.cost(f, "1h", by="arc_tenant_id"), "{{arc_tenant_id}}")],
                     unit="currencyUSD", grid=(0, 12, 12, 8), stack=True, draw="bars"),
        p.timeseries("Error rate by tenant", [(q.error_rate(f, "$__rate_interval", by="arc_tenant_id"),
                                               "{{arc_tenant_id}}")], unit="percent", grid=(12, 12, 12, 8)),
        p.bars("Spend by model (all tenants)", q.cost(f, by="gen_ai_request_model"), "{{gen_ai_request_model}}",
               unit="currencyUSD", grid=(0, 20, 12, 8)),
        p.bars("Agents by tenant", 'sum by (arc_tenant_id) (arc_agents{arc_tenant_id=~"$tenant",status="active"})',
               "{{arc_tenant_id}}", grid=(12, 20, 12, 8)),
    ]
    return p.dashboard(EXEC_UID, "ARC · Executive Overview", panels, variables, ["arc", "executive"],
                       "Enterprise-wide adoption, spend and reliability across all tenants.", time_from="now-7d")


def tenant_dashboards() -> list[dict]:
    return [overview(), finops(), quality()]


def library_panels() -> list[tuple[str, str, dict]]:
    """(uid, name, panel model) - ready-made panels users can add to their own dashboards."""
    f = _TENANT_FILTER
    items = [
        ("arc-lib-cost-agent", "Cost by agent",
         p.bars("Cost by agent", q.cost(f, by="arc_service_name"), "{{arc_service_name}}", unit="currencyUSD")),
        ("arc-lib-cost-model", "Cost share by model",
         p.pie("Cost share by model", q.cost(f, by="gen_ai_request_model"), "{{gen_ai_request_model}}",
               unit="currencyUSD")),
        ("arc-lib-p95-agent", "p95 latency by agent",
         p.timeseries("p95 latency by agent", [(q.p95_latency(f, by="arc_service_name"), "{{arc_service_name}}")],
                      unit="ms")),
        ("arc-lib-errors-agent", "Error rate by agent",
         p.timeseries("Error rate by agent", [(q.error_rate(f, "$__rate_interval", by="arc_service_name"),
                                               "{{arc_service_name}}")], unit="percent")),
        ("arc-lib-tokens-model", "Tokens by model",
         p.bars("Tokens by model", q.tokens(f, by="gen_ai_request_model"), "{{gen_ai_request_model}}")),
        ("arc-lib-calls-product", "LLM calls by product",
         p.timeseries("LLM calls by product", [(f'sum by (arc_product_id) (increase(arc_calls_total{{{f},'
                                                f'gen_ai_request_model!=""}}[$__rate_interval]))',
                                                "{{arc_product_id}}")])),
        ("arc-lib-pii", "PII values masked",
         p.stat("PII values masked", q.pii_masked(f))),
        ("arc-lib-quality", "Quality score",
         p.timeseries("Quality score", [('avg by (score_name) (arc_quality_score{arc_product_id=~"$product"})',
                                         "{{score_name}}")])),
    ]
    for _, _, model in items:
        model.pop("gridPos", None)
        model["gridPos"] = {"x": 0, "y": 0, "w": 12, "h": 8}
    return items


def library_showcase() -> dict:
    """A dashboard that uses every library panel. Library panels must be used by at least one
    dashboard, otherwise Grafana's panel library shows "Panel is not linked to a dashboard"."""
    panels = []
    for i, (uid, name, _) in enumerate(library_panels()):
        panels.append({"gridPos": {"x": (i % 2) * 12, "y": (i // 2) * 8, "w": 12, "h": 8},
                       "libraryPanel": {"uid": uid, "name": name}})
    return p.dashboard(LIBRARY_UID, "ARC · Panel Library", panels, _TENANT_VARS, ["arc", "standard", "library"],
                       "Every ready-made panel. In your own dashboard: Add > Import from library.")
