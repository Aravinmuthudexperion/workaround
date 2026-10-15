"""Build OTLP requests from plain records. Used by pull connectors and intelligence jobs,
which create telemetry on behalf of platforms that cannot push it themselves."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import InstrumentationScope
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord as PbLogRecord
from opentelemetry.proto.logs.v1.logs_pb2 import ResourceLogs, ScopeLogs
from opentelemetry.proto.metrics.v1.metrics_pb2 import Gauge, Metric, NumberDataPoint, ResourceMetrics, ScopeMetrics
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span, Status

from arc_tower import __version__
from arc_tower.utils.timeutil import to_unix_nano

from .attributes import key_values, to_any_value

_SCOPE = InstrumentationScope(name="arc-tower", version=__version__)

_SEVERITY = {"DEBUG": 5, "INFO": 9, "WARN": 13, "WARNING": 13, "ERROR": 17, "FATAL": 21}


def new_trace_id() -> bytes:
    return os.urandom(16)


def new_span_id() -> bytes:
    return os.urandom(8)


@dataclass
class SpanRecord:
    name: str
    start: datetime
    end: datetime
    trace_id: bytes = field(default_factory=new_trace_id)
    span_id: bytes = field(default_factory=new_span_id)
    parent_span_id: bytes = b""
    attributes: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    kind: int = Span.SPAN_KIND_INTERNAL


@dataclass
class LogEntry:
    body: str
    at: datetime
    severity: str = "INFO"
    attributes: dict[str, Any] = field(default_factory=dict)
    trace_id: bytes = b""
    span_id: bytes = b""


@dataclass
class GaugePoint:
    name: str
    value: float
    at: datetime
    attributes: dict[str, Any] = field(default_factory=dict)
    description: str = ""


def _resource(service_name: str, resource_attrs: dict[str, Any] | None) -> Resource:
    return Resource(attributes=key_values({"service.name": service_name, **(resource_attrs or {})}))


def trace_request(service_name: str, spans: list[SpanRecord],
                  resource_attrs: dict[str, Any] | None = None) -> ExportTraceServiceRequest:
    pb_spans = []
    for s in spans:
        status = Status(code=Status.STATUS_CODE_ERROR, message=s.error) if s.error else Status(
            code=Status.STATUS_CODE_OK)
        pb_spans.append(Span(
            trace_id=s.trace_id, span_id=s.span_id, parent_span_id=s.parent_span_id, name=s.name, kind=s.kind,
            start_time_unix_nano=to_unix_nano(s.start), end_time_unix_nano=to_unix_nano(s.end),
            attributes=key_values(s.attributes), status=status,
        ))
    return ExportTraceServiceRequest(resource_spans=[ResourceSpans(
        resource=_resource(service_name, resource_attrs),
        scope_spans=[ScopeSpans(scope=_SCOPE, spans=pb_spans)],
    )])


def logs_request(service_name: str, entries: list[LogEntry],
                 resource_attrs: dict[str, Any] | None = None) -> ExportLogsServiceRequest:
    records = []
    for e in entries:
        severity = e.severity.upper()
        records.append(PbLogRecord(
            time_unix_nano=to_unix_nano(e.at), observed_time_unix_nano=to_unix_nano(e.at),
            severity_number=_SEVERITY.get(severity, 9), severity_text=severity,
            body=to_any_value(e.body), attributes=key_values(e.attributes),
            trace_id=e.trace_id, span_id=e.span_id,
        ))
    return ExportLogsServiceRequest(resource_logs=[ResourceLogs(
        resource=_resource(service_name, resource_attrs),
        scope_logs=[ScopeLogs(scope=_SCOPE, log_records=records)],
    )])


def metrics_request(service_name: str, points: list[GaugePoint],
                    resource_attrs: dict[str, Any] | None = None) -> ExportMetricsServiceRequest:
    """One gauge metric per name, one data point per (name, attributes)."""
    by_name: dict[str, list[GaugePoint]] = {}
    for p in points:
        by_name.setdefault(p.name, []).append(p)
    metrics = [
        Metric(name=name, description=group[0].description, gauge=Gauge(data_points=[
            NumberDataPoint(time_unix_nano=to_unix_nano(p.at), as_double=float(p.value),
                            attributes=key_values(p.attributes))
            for p in group]))
        for name, group in by_name.items()
    ]
    return ExportMetricsServiceRequest(resource_metrics=[ResourceMetrics(
        resource=_resource(service_name, resource_attrs),
        scope_metrics=[ScopeMetrics(scope=_SCOPE, metrics=metrics)],
    )])
