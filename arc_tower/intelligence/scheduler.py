"""A tiny interval scheduler: each job runs in its own thread, never overlapping itself."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import metrics

log = logging.getLogger("arc.intelligence")


@dataclass
class Job:
    name: str
    interval_seconds: int
    run: Callable[[], None]


def _loop(job: Job, stop: threading.Event) -> None:
    while not stop.is_set():
        started = time.time()
        try:
            job.run()
            metrics.job_success.labels(job.name).set(1)
        except Exception:
            metrics.job_success.labels(job.name).set(0)
            log.exception("job %s failed", job.name)
        metrics.job_last_run.labels(job.name).set(time.time())
        stop.wait(max(job.interval_seconds - (time.time() - started), 1))


def start(jobs: list[Job], stop: threading.Event) -> list[threading.Thread]:
    threads = []
    for job in jobs:
        thread = threading.Thread(target=_loop, args=(job, stop), name=f"job-{job.name}", daemon=True)
        thread.start()
        threads.append(thread)
    return threads
