"""Consistent log format for every ARC Tower process."""
from __future__ import annotations

import logging
import os


def configure_logging(component: str) -> logging.Logger:
    level = os.environ.get("ARC_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format=f"%(asctime)s %(levelname)-7s [{component}] %(name)s: %(message)s",
    )
    # Third-party request logs are noisy at INFO.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(f"arc.{component}")
