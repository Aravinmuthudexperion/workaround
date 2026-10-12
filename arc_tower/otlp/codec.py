"""Decode and encode OTLP/HTTP request bodies (protobuf or JSON, optionally gzip)."""
from __future__ import annotations

import gzip
from enum import Enum

from google.protobuf import json_format
from google.protobuf.message import DecodeError, Message
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

PROTOBUF = "application/x-protobuf"
JSON = "application/json"


class Signal(str, Enum):
    TRACES = "traces"
    METRICS = "metrics"
    LOGS = "logs"


_REQUEST_TYPES: dict[Signal, type[Message]] = {
    Signal.TRACES: ExportTraceServiceRequest,
    Signal.METRICS: ExportMetricsServiceRequest,
    Signal.LOGS: ExportLogsServiceRequest,
}


class OtlpDecodeError(ValueError):
    pass


def decode(signal: Signal, body: bytes, content_type: str | None, content_encoding: str | None) -> Message:
    if (content_encoding or "").lower() == "gzip":
        try:
            body = gzip.decompress(body)
        except OSError as exc:
            raise OtlpDecodeError("invalid gzip body") from exc
    message = _REQUEST_TYPES[signal]()
    try:
        if (content_type or PROTOBUF).split(";")[0].strip().lower() == JSON:
            json_format.Parse(body.decode("utf-8"), message, ignore_unknown_fields=True)
        else:
            message.ParseFromString(body)
    except (DecodeError, json_format.ParseError, UnicodeDecodeError) as exc:
        raise OtlpDecodeError(f"cannot decode OTLP {signal.value} payload: {exc}") from exc
    return message


def encode(message: Message) -> bytes:
    """Always forward as protobuf - the collector accepts both, protobuf is smaller."""
    return message.SerializeToString()
