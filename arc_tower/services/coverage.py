"""Onboarding step 3 - which standard fields a product's telemetry actually carries."""
from __future__ import annotations

from dataclasses import dataclass

from arc_tower.db.models import Product
from arc_tower.otlp.enrich import FIELD_CLASS, EnrichStats

# (field, label, weight, hint when missing)
FIELDS: list[tuple[str, str, int, str]] = [
    ("gen_ai.request.model", "Model name", 3, "Needed for cost and model breakdowns."),
    ("gen_ai.usage.input_tokens", "Input tokens", 3, "Needed for token usage and cost."),
    ("gen_ai.usage.output_tokens", "Output tokens", 3, "Needed for token usage and cost."),
    ("gen_ai.operation.name", "Operation (chat, tool, agent)", 1, "Separates LLM calls from tool calls."),
    ("gen_ai.agent.name", "Agent name", 2, "Groups spans per agent in the registry and dashboards."),
    ("session.id", "Session id", 1, "Groups multi-turn conversations in Langfuse."),
    ("user.id", "End-user id", 1, "Adoption per user; send a pseudonymous id, not an e-mail."),
    ("input", "Prompt / input", 1, "Needed for quality evaluation (subject to content policy)."),
    ("output", "Completion / output", 1, "Needed for quality evaluation (subject to content policy)."),
]


@dataclass
class FieldCoverage:
    field: str
    label: str
    percent: float
    hint: str

    @property
    def status(self) -> str:
        return "ok" if self.percent >= 90 else "partial" if self.percent > 0 else "missing"


def merge(product: Product, stats: EnrichStats) -> None:
    current = dict(product.coverage or {})
    fields = dict(current.get("fields", {}))
    for name, count in stats.field_counts.items():
        fields[name] = fields.get(name, 0) + count
    classes = dict(current.get("classes", {}))
    for name, count in stats.span_classes.items():
        classes[name] = classes.get(name, 0) + count
    # Assign a new dict so SQLAlchemy notices the JSON change.
    product.coverage = {
        "spans": current.get("spans", 0) + stats.spans,
        "llm_spans": current.get("llm_spans", 0) + stats.llm_spans,
        "logs": current.get("logs", 0) + stats.log_records,
        "fields": fields,
        "classes": classes,
    }


def report(product: Product) -> tuple[list[FieldCoverage], float]:
    data = product.coverage or {}
    classes = data.get("classes", {})
    rows, score, total_weight = [], 0.0, 0
    for name, label, weight, hint in FIELDS:
        # Denominator: the spans this field belongs on (model calls, root spans or any GenAI span).
        denominator = classes.get(FIELD_CLASS[name], 0)
        percent = 100.0 * data.get("fields", {}).get(name, 0) / denominator if denominator else 0.0
        percent = min(percent, 100.0)
        rows.append(FieldCoverage(name, label, round(percent, 1), hint))
        score += weight * percent
        total_weight += weight
    return rows, round(score / total_weight, 1) if total_weight else 0.0
