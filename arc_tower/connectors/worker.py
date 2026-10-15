"""Connector worker: runs due pull connectors and records their status.

    python -m arc_tower.connectors.worker
"""
from __future__ import annotations

import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from sqlalchemy import select

from arc_tower.connectors import catalog
from arc_tower.core.logging import configure_logging
from arc_tower.db.models import Connector
from arc_tower.db.session import session_scope
from arc_tower.otlp.enrich import TenantStamp
from arc_tower.otlp.sender import CollectorSender
from arc_tower.services import agents, connectors, coverage, langfuse_routing, products
from arc_tower.utils.timeutil import as_utc, utcnow

from .base import ConnectorError, RunContext, RunResult
from .emitter import Emitter
from .registry import plugin_for

log = configure_logging("connectors")

TICK_SECONDS = 5
FIRST_RUN_LOOKBACK = timedelta(hours=1)
_stop = threading.Event()


def _due(connector: Connector, now: datetime) -> bool:
    last = as_utc(connector.last_run_at)
    return last is None or (now - last).total_seconds() >= connector.interval_seconds


def _since(connector: Connector, now: datetime) -> datetime:
    cursor = (connector.cursor or {}).get("since")
    if cursor:
        try:
            return as_utc(datetime.fromisoformat(cursor))
        except ValueError:
            pass
    return as_utc(connector.last_run_at) or now - FIRST_RUN_LOOKBACK


def run_connector(connector_id: int, sender: CollectorSender) -> None:
    """Run one connector end to end in its own DB session."""
    now = utcnow()
    with session_scope() as session:
        connector = session.get(Connector, connector_id)
        if connector is None:
            return
        product = connector.product
        spec = catalog.get(connector.type)
        plugin = plugin_for(connector.type)
        stamp = TenantStamp(product.tenant.slug, product.slug, product.environment,
                            spec.platform if spec else connector.type)
        emitter = Emitter(stamp, langfuse_routing.auth_header(product), sender)
        ctx = RunContext(connector.id, dict(connector.config or {}), connectors.secrets_for(connector),
                         dict(connector.cursor or {}), _since(connector, now), emitter, log,
                         session=session, product=product)
        connector.last_run_at = now
        try:
            if plugin is None:
                raise ConnectorError(f"no plugin for connector type '{connector.type}'")
            result: RunResult = plugin.run(ctx)
            emitter.flush()
            connector.last_status = "ok"
            connector.last_message = result.message
            connector.last_record_count = result.records
            connector.cursor = result.cursor or {"since": now.isoformat()}
            if emitter.sent_spans or emitter.sent_logs or emitter.sent_metrics:
                products.mark_seen(product, now)
                coverage.merge(product, emitter.stats)
                for (service, agent_name), models in emitter.stats.agents.items():
                    agents.upsert_discovered(session, product, service, agent_name, models, now,
                                             stamp.platform)
            log.info("%s/%s: %s", product.slug, connector.type, result.message)
        except ConnectorError as exc:
            connector.last_status, connector.last_message = "error", str(exc)
            log.warning("%s/%s: %s", product.slug, connector.type, exc)
        except Exception as exc:  # platform SDK / network errors: keep the worker alive
            connector.last_status, connector.last_message = "error", f"{type(exc).__name__}: {exc}"[:500]
            log.exception("%s/%s failed", product.slug, connector.type)


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    sender = CollectorSender()
    running: set[int] = set()
    lock = threading.Lock()
    log.info("connector worker started")

    def _run(connector_id: int) -> None:
        try:
            run_connector(connector_id, sender)
        finally:
            with lock:
                running.discard(connector_id)

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="connector") as pool:
        while not _stop.is_set():
            now = utcnow()
            try:
                with session_scope() as session:
                    due = [c.id for c in session.scalars(select(Connector).where(Connector.enabled.is_(True)))
                           if _due(c, now)]
            except Exception:
                log.exception("could not read connectors")
                due = []
            for connector_id in due:
                with lock:
                    if connector_id in running:
                        continue
                    running.add(connector_id)
                pool.submit(_run, connector_id)
            _stop.wait(TICK_SECONDS)
    sender.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
