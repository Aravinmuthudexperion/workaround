"""Application Insights rows -> ARC spans. Shared by the Azure platform and Azure AI Foundry connectors.

Apps instrumented with OpenTelemetry (Azure Monitor distro, Foundry agents, Azure AI inference SDK)
write model calls to the AppDependencies table with gen_ai.* keys in Properties.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta

from arc_tower.otlp.builders import LogEntry, SpanRecord
from arc_tower.otlp.enrich import price_for

from .common import as_int, id_bytes, parse_time

GEN_AI_FILTER = ('isnotempty(tostring(Properties["gen_ai.operation.name"])) '
                 'or isnotempty(tostring(Properties["gen_ai.request.model"]))')
COLUMNS = "TimeGenerated, OperationId, Id, ParentId, Name, DurationMs, Success, AppRoleName, Properties"
_KEPT_PREFIXES = ("gen_ai.", "session.", "user.")
_TOKEN_KEYS = ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens")
_SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")      # gpt-4.1-mini-2025-04-14 -> gpt-4.1-mini


def _attributes(properties) -> dict:
    if isinstance(properties, str):           # the Logs REST API returns dynamic columns as JSON text
        try:
            properties = json.loads(properties)
        except ValueError:
            properties = {}
    attrs = {k: v for k, v in (properties or {}).items() if k.startswith(_KEPT_PREFIXES)}
    for key in _TOKEN_KEYS:
        if key in attrs:
            attrs[key] = as_int(attrs[key])
    # Azure OpenAI: the request names a deployment (e.g. "claims-chat"); the response names the model.
    # Report the model so prices and the per-model dashboards line up, and keep the deployment.
    response_model = str(attrs.get("gen_ai.response.model") or "")
    if response_model:
        model = _SNAPSHOT.sub("", response_model)
        deployment = attrs.get("gen_ai.request.model")
        if deployment and deployment != model:
            attrs["azure.openai.deployment"] = deployment
        attrs["gen_ai.request.model"] = model
    return attrs


def span_from_row(row: dict, default_service: str) -> tuple[str, SpanRecord] | None:
    """(service name, span) for one AppDependencies row; None if the row has no usable time."""
    start = parse_time(row.get("TimeGenerated"))
    if start is None:
        return None
    service = row.get("AppRoleName") or default_service
    return service, SpanRecord(
        name=row.get("Name") or "gen_ai", start=start,
        end=start + timedelta(milliseconds=float(row.get("DurationMs") or 0)),
        trace_id=id_bytes(row.get("OperationId", ""), 16), span_id=id_bytes(row.get("Id", ""), 8),
        parent_span_id=id_bytes(row["ParentId"], 8) if row.get("ParentId") else b"",
        attributes=_attributes(row.get("Properties")),
        error=None if row.get("Success") in (True, "True", "true", 1) else "failed")


def call_log(service: str, span: SpanRecord, prices: dict) -> LogEntry | None:
    """One log line per model call, so a dashboard can list every call (traces without a trace store).
    None for spans that are not model calls."""
    attrs = span.attributes
    model = attrs.get("gen_ai.request.model")
    if not model:
        return None
    tokens_in = attrs.get("gen_ai.usage.input_tokens") or 0
    tokens_out = attrs.get("gen_ai.usage.output_tokens") or 0
    seconds = (span.end - span.start).total_seconds()
    price = price_for(str(model), prices)
    cost = (tokens_in * price[0] + tokens_out * price[1]) / 1e6 if price else None
    deployment = attrs.get("azure.openai.deployment")
    body = (f"{model}{f' ({deployment})' if deployment else ''} · {tokens_in} in / {tokens_out} out tokens"
            f" · {seconds:.2f} s{f' · ${cost:.5f}' if cost is not None else ''} · "
            f"{'failed' if span.error else 'ok'} · {service} · trace {span.trace_id.hex()}")
    fields = {"event.name": "gen_ai.call", "gen_ai.request.model": model, "gen_ai.usage.input_tokens": tokens_in,
              "gen_ai.usage.output_tokens": tokens_out, "duration_ms": round(seconds * 1000),
              "status": "error" if span.error else "ok"}
    if deployment:
        fields["azure.openai.deployment"] = deployment
    if cost is not None:
        fields["gen_ai.usage.cost_usd"] = round(cost, 6)
    return LogEntry(body=body, at=span.start, severity="ERROR" if span.error else "INFO", attributes=fields,
                    trace_id=span.trace_id, span_id=span.span_id)
