"""Collects spans/logs/metrics produced by a connector run and sends them to the collector for one product."""
from __future__ import annotations

from collections import defaultdict

from arc_tower.otlp.builders import GaugePoint, LogEntry, SpanRecord, logs_request, metrics_request, trace_request
from arc_tower.otlp.codec import Signal
from arc_tower.otlp.enrich import EnrichStats, TenantStamp, enrich
from arc_tower.otlp.sender import CollectorSender, routing_headers

_BATCH = 500


class Emitter:
    def __init__(self, stamp: TenantStamp, langfuse_auth: str | None, sender: CollectorSender):
        self._stamp = stamp
        self._headers = routing_headers(stamp.tenant_slug, stamp.product_slug, langfuse_auth)
        self._sender = sender
        self._spans: dict[str, list[SpanRecord]] = defaultdict(list)
        self._logs: dict[str, list[LogEntry]] = defaultdict(list)
        self._metrics: dict[str, list[GaugePoint]] = defaultdict(list)
        self.stats = EnrichStats()   # coverage + discovered agents across the whole run
        self.sent_spans = 0
        self.sent_logs = 0
        self.sent_metrics = 0

    def span(self, service_name: str, record: SpanRecord) -> None:
        self._spans[service_name].append(record)
        if len(self._spans[service_name]) >= _BATCH:
            self._flush_spans(service_name)

    def log(self, service_name: str, entry: LogEntry) -> None:
        self._logs[service_name].append(entry)

    def metric(self, service_name: str, point: GaugePoint) -> None:
        self._metrics[service_name].append(point)

    def _flush_spans(self, service: str) -> None:
        spans = self._spans.pop(service, [])
        if not spans:
            return
        request = trace_request(service, spans)
        self.stats.merge(enrich(Signal.TRACES, request, self._stamp))
        self._sender.send(Signal.TRACES, request, self._headers)
        self.sent_spans += len(spans)

    def flush(self) -> None:
        for service in list(self._spans):
            self._flush_spans(service)
        for service, entries in list(self._logs.items()):
            request = logs_request(service, entries)
            self.stats.merge(enrich(Signal.LOGS, request, self._stamp))
            self._sender.send(Signal.LOGS, request, self._headers)
            self.sent_logs += len(entries)
        self._logs.clear()
        for service, points in list(self._metrics.items()):
            request = metrics_request(service, points)
            enrich(Signal.METRICS, request, self._stamp)
            self._sender.send(Signal.METRICS, request, self._headers)
            self.sent_metrics += len(points)
        self._metrics.clear()
