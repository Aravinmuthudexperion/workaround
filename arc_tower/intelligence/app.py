"""Intelligence service: runs the analytics jobs and serves their metrics for Prometheus.

    python -m arc_tower.intelligence.app
"""
from __future__ import annotations

import signal
import threading

from prometheus_client import start_http_server

from arc_tower.core.config import INTELLIGENCE_PORT
from arc_tower.core.logging import configure_logging

from . import metrics, scheduler
from .jobs import anomaly, compliance, drift, finops, inventory, pricing, quality

JOBS = [
    scheduler.Job("pricing", pricing.INTERVAL, pricing.run),
    scheduler.Job("inventory", inventory.INTERVAL, inventory.run),
    scheduler.Job("finops", finops.INTERVAL, finops.run),
    scheduler.Job("quality", quality.INTERVAL, quality.run),
    scheduler.Job("drift", drift.INTERVAL, drift.run),
    scheduler.Job("anomaly", anomaly.INTERVAL, anomaly.run),
    scheduler.Job("compliance", compliance.INTERVAL, compliance.run),
]


def main() -> None:
    log = configure_logging("intelligence")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    start_http_server(INTELLIGENCE_PORT, addr="127.0.0.1", registry=metrics.REGISTRY)
    scheduler.start(JOBS, stop)
    log.info("intelligence jobs running; metrics on 127.0.0.1:%s/metrics", INTELLIGENCE_PORT)
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
