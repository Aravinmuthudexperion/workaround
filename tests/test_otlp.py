import gzip
import json
from datetime import timedelta

import pytest

from arc_tower.otlp import attributes as attr
from arc_tower.otlp.builders import LogEntry, SpanRecord, logs_request, trace_request
from arc_tower.otlp.codec import JSON, OtlpDecodeError, Signal, decode, encode
from arc_tower.otlp.enrich import TenantStamp, enrich
from arc_tower.utils.timeutil import utcnow

STAMP = TenantStamp("acme", "claims", "prod", "custom")


def _llm_request(extra_resource=None):
    now = utcnow()
    span = SpanRecord("chat gpt-4o", now - timedelta(seconds=1), now, attributes={
        "gen_ai.request.model": "gpt-4o", "gen_ai.usage.input_tokens": 10,
        "llm.token_count.completion": 5, "gen_ai.agent.name": "triage", "arc.tenant.id": "evil-corp",
    })
    return trace_request("claims-svc", [span], resource_attrs=extra_resource)


def test_tenant_is_stamped_and_spoofing_removed():
    request = _llm_request({"arc.tenant.id": "evil-corp", "arc.product.id": "x"})
    stats = enrich(Signal.TRACES, request, STAMP)
    resource = request.resource_spans[0].resource.attributes
    span_attrs = request.resource_spans[0].scope_spans[0].spans[0].attributes
    assert attr.get(resource, "arc.tenant.id") == "acme"
    assert attr.get(span_attrs, "arc.tenant.id") == "acme"
    assert attr.get(span_attrs, "arc.service.name") == "claims-svc"
    assert attr.get(span_attrs, "langfuse.trace.tags") == ["tenant:acme", "product:claims"]
    assert stats.spoofed_attributes_removed == 3  # 2 on the resource + 1 on the span


def test_coverage_accepts_vendor_aliases_and_discovers_agents():
    stats = enrich(Signal.TRACES, _llm_request(), STAMP)
    assert stats.llm_spans == 1
    assert stats.field_counts["gen_ai.usage.output_tokens"] == 1   # via llm.token_count.completion
    assert stats.field_counts.get("session.id", 0) == 0
    assert stats.agents == {("claims-svc", "triage"): {"gpt-4o"}}


def test_coverage_measures_fields_against_the_right_spans():
    now = utcnow()
    root = SpanRecord("invoke_agent triage", now - timedelta(seconds=2), now,
                      attributes={"gen_ai.operation.name": "invoke_agent", "session.id": "s1"})
    tool = SpanRecord("execute_tool lookup", now - timedelta(seconds=1), now, trace_id=root.trace_id,
                      parent_span_id=root.span_id, attributes={"gen_ai.operation.name": "execute_tool"})
    chat = SpanRecord("chat gpt-4o", now - timedelta(seconds=1), now, trace_id=root.trace_id,
                      parent_span_id=root.span_id,
                      attributes={"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4o",
                                  "gen_ai.usage.input_tokens": 3})
    stats = enrich(Signal.TRACES, trace_request("svc", [root, tool, chat]), STAMP)
    assert stats.span_classes == {"genai": 3, "model_call": 1, "root": 1}
    # tokens only expected on the model call; session only on the root span
    assert stats.field_counts["gen_ai.usage.input_tokens"] == 1
    assert stats.field_counts["session.id"] == 1


def test_only_genai_spans_discover_agents():
    now = utcnow()
    http = SpanRecord("GET /api/projects", now - timedelta(seconds=2), now, attributes={"http.method": "GET"})
    step = SpanRecord("pipeline.step", now - timedelta(seconds=1), now, trace_id=http.trace_id,
                      parent_span_id=http.span_id, attributes={"project_id": "p1"})
    chat = SpanRecord("chat qwen", now - timedelta(seconds=1), now, trace_id=http.trace_id,
                      parent_span_id=http.span_id,
                      attributes={"gen_ai.request.model": "qwen2.5:7b", "gen_ai.agent.name": "research-agent"})
    unnamed = SpanRecord("chat unnamed", now - timedelta(seconds=1), now, trace_id=http.trace_id,
                         parent_span_id=http.span_id, attributes={"gen_ai.request.model": "gpt-4o"})
    stats = enrich(Signal.TRACES, trace_request("svc", [http, step, chat]), STAMP)
    assert stats.agents == {("svc", "research-agent"): {"qwen2.5:7b"}}
    # GenAI spans without an agent name are still discovered, under the service
    stats = enrich(Signal.TRACES, trace_request("svc", [http, unnamed]), STAMP)
    assert stats.agents == {("svc", ""): {"gpt-4o"}}


PRICES = {"gpt-4.1": [2, 8], "gpt-4.1-mini": [0.4, 1.6], "text-embedding-3-large": [0.13, 0]}


def _model_span(model, **attrs):
    now = utcnow()
    return SpanRecord(f"chat {model}", now - timedelta(seconds=1), now,
                      attributes={"gen_ai.request.model": model, **attrs})


def _cost(request, index=0):
    raw = attr.get(request.resource_spans[0].scope_spans[0].spans[index].attributes,
                   "langfuse.observation.cost_details")
    return None if raw is None else json.loads(raw)


def test_cost_is_stamped_from_model_prices():
    request = trace_request("svc", [_model_span("gpt-4.1", **{"gen_ai.usage.input_tokens": 10_000,
                                                              "gen_ai.usage.output_tokens": 1_000})])
    stats = enrich(Signal.TRACES, request, STAMP, PRICES)
    assert _cost(request) == pytest.approx({"input": 0.02, "output": 0.008, "total": 0.028})
    assert stats.priced_spans == 1


def test_cost_accepts_token_aliases_and_input_only_embeddings():
    request = trace_request("svc", [
        _model_span("gpt-4.1", **{"llm.token_count.prompt": 1_000_000, "gen_ai.usage.completion_tokens": 0}),
        _model_span("text-embedding-3-large", **{"gen_ai.usage.input_tokens": 2_000_000}),
    ])
    enrich(Signal.TRACES, request, STAMP, PRICES)
    assert _cost(request, 0)["total"] == pytest.approx(2.0)
    assert _cost(request, 1) == pytest.approx({"input": 0.26, "output": 0.0, "total": 0.26})


@pytest.mark.parametrize("model, expected_input_price", [
    ("GPT-4.1", 2), ("openai/gpt-4.1", 2), ("gpt-4.1-2025-04-14", 2),
    ("gpt-4.1-mini", 0.4), ("gpt-4.1-mini-2025-04-14", 0.4),
])
def test_cost_matches_provider_prefixes_case_and_dated_snapshots(model, expected_input_price):
    request = trace_request("svc", [_model_span(model, **{"gen_ai.usage.input_tokens": 1_000_000})])
    enrich(Signal.TRACES, request, STAMP, PRICES)
    assert _cost(request)["input"] == pytest.approx(expected_input_price)


def test_cost_is_skipped_for_unpriced_models_and_client_supplied_cost():
    request = trace_request("svc", [
        _model_span("mystery-model", **{"gen_ai.usage.input_tokens": 100}),
        _model_span("gpt-4.1", **{"gen_ai.usage.input_tokens": 100, "gen_ai.usage.cost": 9.99}),
        _model_span("gpt-4.1"),  # no token counts at all
    ])
    stats = enrich(Signal.TRACES, request, STAMP, PRICES)
    assert [_cost(request, i) for i in range(3)] == [None, None, None]
    assert stats.priced_spans == 0
    assert stats.unpriced_models == {"mystery-model"}


def test_logs_are_stamped():
    request = logs_request("svc", [LogEntry("hello", utcnow(), "WARN")])
    stats = enrich(Signal.LOGS, request, STAMP)
    assert stats.log_records == 1
    record = request.resource_logs[0].scope_logs[0].log_records[0]
    assert record.severity_number == 13
    assert attr.get(record.attributes, "arc.product.id") == "claims"


def test_codec_roundtrip_protobuf_gzip_and_json():
    request = _llm_request()
    body = encode(request)
    assert decode(Signal.TRACES, gzip.compress(body), None, "gzip") == request
    from google.protobuf import json_format
    as_json = json_format.MessageToJson(request).encode()
    assert decode(Signal.TRACES, as_json, JSON, None) == request


def test_codec_rejects_garbage():
    with pytest.raises(OtlpDecodeError):
        decode(Signal.TRACES, b"\xff\xfe not protobuf", None, None)
