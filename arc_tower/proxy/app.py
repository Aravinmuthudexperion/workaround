"""Ingest proxy: authenticate the ingest key, stamp the tenant, forward to the collector.

Clients set OTEL_EXPORTER_OTLP_ENDPOINT to this service (optionally with an /otlp prefix);
their SDK then posts to /v1/traces, /v1/metrics and /v1/logs.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from arc_tower.core.config import COLLECTOR_OTLP_URL
from arc_tower.core.logging import configure_logging
from arc_tower.otlp.codec import PROTOBUF, OtlpDecodeError, Signal, decode, encode
from arc_tower.otlp.enrich import enrich
from arc_tower.otlp.sender import routing_headers

from .keys import KeyResolver
from .stats import StatsAggregator

log = configure_logging("ingest-proxy")

MAX_BODY_BYTES = 16 * 1024 * 1024

resolver = KeyResolver()
aggregator = StatsAggregator()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.collector = httpx.AsyncClient(base_url=COLLECTOR_OTLP_URL, timeout=httpx.Timeout(20.0))
    flusher = asyncio.create_task(aggregator.run())
    yield
    flusher.cancel()
    await asyncio.to_thread(aggregator.flush)
    await app.state.collector.aclose()


app = FastAPI(title="ARC Tower ingest proxy", lifespan=lifespan, docs_url=None, redoc_url=None)


def _ingest_key(request: Request) -> str | None:
    key = request.headers.get("x-arc-key")
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:]
    return key.strip() if key else None


def _error(status: int, message: str) -> JSONResponse:
    # OTLP clients log the body; keep it short and actionable.
    return JSONResponse({"code": status, "message": message}, status_code=status)


async def _handle(signal: Signal, request: Request) -> Response:
    key = _ingest_key(request)
    if not key:
        return _error(401, "missing ingest key: set OTEL_EXPORTER_OTLP_HEADERS=x-arc-key=<key>")
    resolved = await asyncio.to_thread(resolver.resolve, key)
    if resolved is None:
        return _error(401, "unknown or revoked ingest key")

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return _error(413, "payload too large")
    try:
        message = decode(signal, body, request.headers.get("content-type"), request.headers.get("content-encoding"))
    except OtlpDecodeError as exc:
        return _error(400, str(exc))

    stats = enrich(signal, message, resolved.stamp)
    aggregator.add(resolved.product_id, resolved.stamp.platform, key, stats)
    if stats.spoofed_attributes_removed:
        log.info("removed %d client-supplied arc.* attributes for product %s",
                 stats.spoofed_attributes_removed, resolved.product_slug)

    headers = {"Content-Type": PROTOBUF,
               **routing_headers(resolved.tenant_slug, resolved.product_slug, resolved.langfuse_auth)}
    try:
        upstream = await request.app.state.collector.post(f"/v1/{signal.value}", content=encode(message),
                                                          headers=headers)
    except httpx.HTTPError as exc:
        log.warning("collector unavailable: %s", exc)
        return _error(503, "ARC Tower gateway temporarily unavailable, retry later")
    # Pass the collector's (protobuf) response through: the client SDK understands partial-success replies.
    return Response(upstream.content, status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", PROTOBUF))


@app.post("/v1/traces")
@app.post("/otlp/v1/traces")
async def traces(request: Request) -> Response:
    return await _handle(Signal.TRACES, request)


@app.post("/v1/metrics")
@app.post("/otlp/v1/metrics")
async def metrics(request: Request) -> Response:
    return await _handle(Signal.METRICS, request)


@app.post("/v1/logs")
@app.post("/otlp/v1/logs")
async def logs(request: Request) -> Response:
    return await _handle(Signal.LOGS, request)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}
