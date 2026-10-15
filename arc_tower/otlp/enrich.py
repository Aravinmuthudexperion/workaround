"""Stamp tenant identity onto incoming telemetry and collect onboarding statistics.

The tenant is decided by ARC Tower (from the ingest key), never by the client:
any arc.* attribute the client sent is removed and replaced.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

from google.protobuf.message import Message

from arc_tower.core.constants import ATTR_LANGFUSE_TAGS, ATTR_PLATFORM, ATTR_PRODUCT, ATTR_SERVICE, ATTR_TENANT

from . import attributes as attr
from .codec import Signal

# Standard fields ARC wants, and the alternative keys that satisfy each one.
# Alternatives are normalised to the gen_ai.* name by the collector's transform processor.
COVERAGE_FIELDS: dict[str, tuple[str, ...]] = {
    "gen_ai.request.model": ("gen_ai.request.model", "gen_ai.response.model", "llm.model_name",
                             "langfuse.observation.model.name"),
    "gen_ai.usage.input_tokens": ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens",
                                  "llm.token_count.prompt"),
    "gen_ai.usage.output_tokens": ("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens",
                                   "llm.token_count.completion"),
    "gen_ai.operation.name": ("gen_ai.operation.name", "openinference.span.kind"),
    "gen_ai.agent.name": ("gen_ai.agent.name",),
    "session.id": ("session.id", "langfuse.session.id"),
    "user.id": ("user.id", "langfuse.user.id", "enduser.id"),
    "input": ("gen_ai.prompt", "gen_ai.input.messages", "input.value", "langfuse.observation.input"),
    "output": ("gen_ai.completion", "gen_ai.output.messages", "output.value", "langfuse.observation.output"),
}

# Each field is measured against the spans where it belongs: token counts only on model calls,
# session/user ids on the root span of a request, agent/operation names on any GenAI span.
FIELD_CLASS: dict[str, str] = {
    "gen_ai.request.model": "model_call", "gen_ai.usage.input_tokens": "model_call",
    "gen_ai.usage.output_tokens": "model_call", "input": "model_call", "output": "model_call",
    "gen_ai.operation.name": "genai", "gen_ai.agent.name": "genai",
    "session.id": "root", "user.id": "root",
}

_MODEL_KEYS = COVERAGE_FIELDS["gen_ai.request.model"]
_INPUT_TOKEN_KEYS = COVERAGE_FIELDS["gen_ai.usage.input_tokens"]
_OUTPUT_TOKEN_KEYS = COVERAGE_FIELDS["gen_ai.usage.output_tokens"]
_TOKEN_KEYS = _INPUT_TOKEN_KEYS + _OUTPUT_TOKEN_KEYS
_MODEL_OPERATIONS = {"chat", "text_completion", "generate_content", "embeddings", "llm"}

# Cost stamped onto model-call spans from ARC_MODEL_PRICES, so Langfuse shows
# cost without every client registering model prices in Langfuse by hand.
# Langfuse's OTel ingestion stores this as the observation's cost breakdown
# even for models it has no price for (verified against Langfuse v4).
ATTR_LANGFUSE_COST = "langfuse.observation.cost_details"
# A cost the client computed itself always wins over ours.
_CLIENT_COST_KEYS = (ATTR_LANGFUSE_COST, "gen_ai.usage.cost")


@dataclass(frozen=True)
class TenantStamp:
    tenant_slug: str
    product_slug: str
    environment: str = "prod"
    platform: str = "custom"


@dataclass
class EnrichStats:
    spans: int = 0
    llm_spans: int = 0
    log_records: int = 0
    data_points: int = 0
    spoofed_attributes_removed: int = 0
    priced_spans: int = 0
    unpriced_models: set[str] = field(default_factory=set)  # model calls with no ARC_MODEL_PRICES entry
    field_counts: Counter = field(default_factory=Counter)
    span_classes: Counter = field(default_factory=Counter)   # model_call / genai / root
    # (service.name, gen_ai.agent.name) -> set of models seen
    agents: dict[tuple[str, str], set[str]] = field(default_factory=dict)

    def merge(self, other: EnrichStats) -> None:
        self.spans += other.spans
        self.llm_spans += other.llm_spans
        self.log_records += other.log_records
        self.data_points += other.data_points
        self.spoofed_attributes_removed += other.spoofed_attributes_removed
        self.priced_spans += other.priced_spans
        self.unpriced_models |= other.unpriced_models
        self.field_counts.update(other.field_counts)
        self.span_classes.update(other.span_classes)
        for key, models in other.agents.items():
            self.agents.setdefault(key, set()).update(models)


def _stamp_resource(resource_attrs, stamp: TenantStamp, stats: EnrichStats) -> str:
    stats.spoofed_attributes_removed += attr.remove_prefix(resource_attrs, "arc.")
    service = attr.get(resource_attrs, "service.name") or "unknown_service"
    attr.upsert(resource_attrs, "service.name", service)
    attr.upsert(resource_attrs, ATTR_TENANT, stamp.tenant_slug)
    attr.upsert(resource_attrs, ATTR_PRODUCT, stamp.product_slug)
    attr.upsert(resource_attrs, ATTR_PLATFORM, stamp.platform)
    if attr.get(resource_attrs, "deployment.environment") is None:
        attr.upsert(resource_attrs, "deployment.environment", stamp.environment)
    return service


def _stamp_record_attrs(record_attrs, stamp: TenantStamp, service: str, stats: EnrichStats) -> None:
    # Copied onto each span/log/point too, so collector connectors that only read
    # record attributes (spanmetrics dimensions, sum) can group by tenant/product.
    stats.spoofed_attributes_removed += attr.remove_prefix(record_attrs, "arc.")
    attr.upsert(record_attrs, ATTR_TENANT, stamp.tenant_slug)
    attr.upsert(record_attrs, ATTR_PRODUCT, stamp.product_slug)
    attr.upsert(record_attrs, ATTR_SERVICE, service)


def _span_classes(span, values: dict) -> set[str]:
    classes = set()
    if any(k.startswith(("gen_ai.", "llm.")) for k in values):
        classes.add("genai")
        operation = str(values.get("gen_ai.operation.name") or "").lower()
        if operation in _MODEL_OPERATIONS or any(values.get(k) for k in _MODEL_KEYS + _TOKEN_KEYS):
            classes.add("model_call")
    if not span.parent_span_id:
        classes.add("root")
    return classes


def _count_coverage(span, values: dict, stats: EnrichStats) -> set[str]:
    classes = _span_classes(span, values)
    if "genai" in classes:
        stats.llm_spans += 1
    stats.span_classes.update(classes)
    for name, keys in COVERAGE_FIELDS.items():
        if FIELD_CLASS[name] in classes and any(values.get(k) not in (None, "") for k in keys):
            stats.field_counts[name] += 1
    return classes


def price_for(model: str, prices: dict) -> tuple[float, float] | None:
    """ARC_MODEL_PRICES entry for a model name as clients actually send it:
    exact key first, then case-insensitive with any "provider/" prefix
    dropped, then the longest key the name extends with "-" - so a dated
    snapshot like gpt-4.1-2025-04-14 uses gpt-4.1's price, while
    gpt-4.1-mini still matches gpt-4.1-mini rather than gpt-4.1."""
    if model in prices:
        return tuple(prices[model])
    name = model.lower().rsplit("/", 1)[-1]
    lowered = {k.lower(): v for k, v in prices.items()}
    if name in lowered:
        return tuple(lowered[name])
    prefixes = [k for k in lowered if name.startswith(k + "-")]
    return tuple(lowered[max(prefixes, key=len)]) if prefixes else None


def _first_number(values: dict, keys: tuple[str, ...]) -> float:
    for key in keys:
        value = values.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return 0.0


def _stamp_cost(span, values: dict, prices: dict, stats: EnrichStats) -> None:
    if not prices or any(values.get(k) not in (None, "") for k in _CLIENT_COST_KEYS):
        return
    model = next((str(values[k]) for k in _MODEL_KEYS if values.get(k)), None)
    price = price_for(model, prices) if model else None
    if price is None:
        if model:
            stats.unpriced_models.add(model)
        return
    input_tokens = _first_number(values, _INPUT_TOKEN_KEYS)
    output_tokens = _first_number(values, _OUTPUT_TOKEN_KEYS)
    if not (input_tokens or output_tokens):
        return
    price_in, price_out = price  # USD per 1M tokens
    cost_in = input_tokens * price_in / 1_000_000
    cost_out = output_tokens * price_out / 1_000_000
    attr.upsert(span.attributes, ATTR_LANGFUSE_COST,
                json.dumps({"input": cost_in, "output": cost_out, "total": cost_in + cost_out}))
    stats.priced_spans += 1


def _enrich_traces(request: Message, stamp: TenantStamp, stats: EnrichStats, prices: dict) -> None:
    tags = [f"tenant:{stamp.tenant_slug}", f"product:{stamp.product_slug}"]
    for resource_spans in request.resource_spans:
        service = _stamp_resource(resource_spans.resource.attributes, stamp, stats)
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                stats.spans += 1
                values = attr.as_dict(span.attributes)
                classes = _count_coverage(span, values, stats)
                if "model_call" in classes:
                    _stamp_cost(span, values, prices, stats)
                # Only GenAI spans discover agents: an app's plain spans (HTTP
                # requests, DB calls, pipeline steps) carry no gen_ai.agent.name
                # and would otherwise register a phantom "(service, no agent)"
                # agent next to the real one.
                if "genai" in classes:
                    agent_name = str(values.get("gen_ai.agent.name") or "")
                    models = stats.agents.setdefault((service, agent_name), set())
                    model = next((values[k] for k in _MODEL_KEYS if values.get(k)), None)
                    if model:
                        models.add(str(model))
                _stamp_record_attrs(span.attributes, stamp, service, stats)
                attr.upsert(span.attributes, ATTR_LANGFUSE_TAGS, tags)


def _enrich_logs(request: Message, stamp: TenantStamp, stats: EnrichStats, prices: dict) -> None:
    for resource_logs in request.resource_logs:
        service = _stamp_resource(resource_logs.resource.attributes, stamp, stats)
        for scope_logs in resource_logs.scope_logs:
            for record in scope_logs.log_records:
                stats.log_records += 1
                _stamp_record_attrs(record.attributes, stamp, service, stats)


def _metric_points(metric):
    kind = metric.WhichOneof("data")
    if kind is None:
        return []
    return getattr(metric, kind).data_points


def _enrich_metrics(request: Message, stamp: TenantStamp, stats: EnrichStats, prices: dict) -> None:
    for resource_metrics in request.resource_metrics:
        service = _stamp_resource(resource_metrics.resource.attributes, stamp, stats)
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                for point in _metric_points(metric):
                    stats.data_points += 1
                    _stamp_record_attrs(point.attributes, stamp, service, stats)


_ENRICHERS = {Signal.TRACES: _enrich_traces, Signal.LOGS: _enrich_logs, Signal.METRICS: _enrich_metrics}


def enrich(signal: Signal, request: Message, stamp: TenantStamp, prices: dict | None = None) -> EnrichStats:
    """Mutates `request` in place and returns what was seen.

    prices: {"model": [input_usd_per_1M, output_usd_per_1M]}; defaults to
    ARC_MODEL_PRICES, the same table the dashboards' cost panels use."""
    if prices is None:
        from arc_tower.core.config import get_settings

        prices = get_settings().model_prices
    stats = EnrichStats()
    _ENRICHERS[signal](request, stamp, stats, prices)
    return stats
