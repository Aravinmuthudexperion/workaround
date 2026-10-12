"""Plugin interface for pull connectors."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from .emitter import Emitter


class ConnectorError(RuntimeError):
    """A failure the user can act on (bad credentials, missing permission, missing package)."""


@dataclass
class RunContext:
    connector_id: int
    config: dict[str, str]
    secrets: dict[str, str]
    cursor: dict
    since: datetime                 # pull records newer than this
    emitter: Emitter
    log: logging.Logger


@dataclass
class RunResult:
    records: int
    cursor: dict = field(default_factory=dict)
    message: str = ""


class PullConnector(ABC):
    type_key: str = ""

    @abstractmethod
    def run(self, ctx: RunContext) -> RunResult:
        """Fetch new records since ctx.since, emit them via ctx.emitter, return the new cursor."""


def require(module: str, extra: str):
    """Import an optional platform SDK, or explain which extra to install."""
    import importlib
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ConnectorError(f"missing package for this connector: pip install -e .[{extra}]") from exc
