"""Customisable page layouts: which KPI tiles and which charts the Overview and agent pages show.

Resolution order for a page: the user's own layout -> the organisation default (saved by a
platform admin) -> the built-in default below.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from arc_tower.db.models import DashboardLayout, User

from . import audit
from .errors import ValidationFailed

PAGES = ("overview", "agent")


@dataclass(frozen=True)
class ChartMetric:
    key: str
    label: str
    unit: str
    groups: tuple[str, ...]          # group-by options the underlying metric supports


@dataclass(frozen=True)
class Group:
    key: str
    label: str
    prom_label: str | None           # None = one "total" series


GROUPS: dict[str, Group] = {g.key: g for g in [
    Group("none", "Total", None),
    Group("product", "Product", "arc_product_id"),
    Group("agent", "Agent", "gen_ai_agent_name"),
    Group("service", "Service", "arc_service_name"),
    Group("model", "Model", "gen_ai_request_model"),
    Group("tenant", "Tenant", "arc_tenant_id"),
]}

_ALL = ("none", "product", "agent", "service", "model", "tenant")

METRICS: dict[str, ChartMetric] = {m.key: m for m in [
    ChartMetric("calls", "LLM calls", "calls", _ALL),
    ChartMetric("cost", "Estimated cost", "USD", _ALL),
    ChartMetric("tokens", "Tokens (input + output)", "tokens", _ALL),
    ChartMetric("input_tokens", "Input tokens", "tokens", _ALL),
    ChartMetric("output_tokens", "Output tokens", "tokens", _ALL),
    ChartMetric("p95_latency", "p95 latency", "ms", _ALL),
    ChartMetric("p50_latency", "Median latency", "ms", _ALL),
    ChartMetric("error_rate", "Error rate", "%", _ALL),
    ChartMetric("pii_masked", "PII values masked", "values", ("none", "product", "service", "tenant")),
]}

KINDS = {"line": "Line", "bar": "Stacked bars", "pie": "Pie (totals)"}

MAX_TILES = 12
MAX_CHARTS = 12

DEFAULTS: dict[str, dict] = {
    "overview": {
        "tiles": ["llm_calls", "active_agents", "error_rate", "p95_latency_ms", "cost_usd", "pii_masked"],
        "charts": [
            {"metric": "calls", "kind": "bar", "group": "product", "title": "LLM calls by product"},
            {"metric": "cost", "kind": "bar", "group": "product", "title": "Estimated cost by product"},
            {"metric": "p95_latency", "kind": "line", "group": "product", "title": "p95 LLM latency"},
            {"metric": "error_rate", "kind": "line", "group": "product", "title": "Error rate"},
        ],
    },
    "agent": {
        "tiles": ["llm_calls", "error_rate", "p95_latency_ms", "tokens", "cost_usd", "cost_per_1k_calls"],
        "charts": [
            {"metric": "calls", "kind": "bar", "group": "model", "title": "LLM calls by model"},
            {"metric": "p95_latency", "kind": "line", "group": "model", "title": "p95 latency by model"},
            {"metric": "tokens", "kind": "pie", "group": "model", "title": "Token share by model"},
            {"metric": "cost", "kind": "bar", "group": "model", "title": "Estimated cost by model"},
        ],
    },
}


def _with_ids(config: dict) -> dict:
    charts = [{**c, "id": c.get("id") or f"c{i}"} for i, c in enumerate(config.get("charts", []))]
    return {"tiles": list(config.get("tiles", [])), "charts": charts}


def _row(session: Session, user_id: int | None, page: str) -> DashboardLayout | None:
    stmt = select(DashboardLayout).where(DashboardLayout.page == page)
    stmt = stmt.where(DashboardLayout.user_id.is_(None) if user_id is None else DashboardLayout.user_id == user_id)
    return session.scalar(stmt)


def get_layout(session: Session, user: User, page: str) -> tuple[dict, str]:
    """(layout, source) where source is "personal", "organisation" or "built-in"."""
    if page not in PAGES:
        raise ValidationFailed("unknown page")
    for owner, source in ((user.id, "personal"), (None, "organisation")):
        row = _row(session, owner, page)
        if row and row.config:
            return _with_ids(row.config), source
    return _with_ids(DEFAULTS[page]), "built-in"


def validate(config: dict, kpi_keys: set[str]) -> dict:
    tiles = [t for t in dict.fromkeys(config.get("tiles", [])) if t in kpi_keys][:MAX_TILES]
    charts = []
    for chart in config.get("charts", [])[:MAX_CHARTS]:
        metric = METRICS.get(chart.get("metric", ""))
        if metric is None:
            continue
        group = chart.get("group") if chart.get("group") in metric.groups else "none"
        kind = chart.get("kind") if chart.get("kind") in KINDS else "line"
        if kind == "pie" and group == "none":
            group = metric.groups[1] if len(metric.groups) > 1 else "none"
        auto = metric.label if group == "none" else f"{metric.label} by {GROUPS[group].label.lower()}"
        title = (chart.get("title") or "").strip()[:80] or auto
        charts.append({"id": "c" + secrets.token_hex(4), "metric": metric.key, "kind": kind, "group": group,
                       "title": title})
    if not tiles and not charts:
        raise ValidationFailed("choose at least one KPI tile or chart")
    return {"tiles": tiles, "charts": charts}


def save_layout(session: Session, user: User, page: str, config: dict, kpi_keys: set[str],
                for_everyone: bool = False) -> None:
    if page not in PAGES:
        raise ValidationFailed("unknown page")
    if for_everyone and not user.is_platform_admin:
        raise ValidationFailed("only platform admins can change the default layout")
    clean = validate(config, kpi_keys)
    owner = None if for_everyone else user.id
    row = _row(session, owner, page)
    if row is None:
        row = DashboardLayout(user_id=owner, page=page)
        session.add(row)
    row.config = clean
    audit.record(session, user.email, "layout.save", "layout", page, scope="everyone" if for_everyone else "personal",
                 tiles=len(clean["tiles"]), charts=len(clean["charts"]))


def reset_layout(session: Session, user: User, page: str, for_everyone: bool = False) -> None:
    if for_everyone and not user.is_platform_admin:
        raise ValidationFailed("only platform admins can change the default layout")
    row = _row(session, None if for_everyone else user.id, page)
    if row:
        session.delete(row)
        audit.record(session, user.email, "layout.reset", "layout", page,
                     scope="everyone" if for_everyone else "personal")
