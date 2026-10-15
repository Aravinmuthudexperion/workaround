"""Secrets-store interface. Values are small JSON objects."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class SecretStore(ABC):
    @abstractmethod
    def get(self, name: str) -> dict | None: ...

    @abstractmethod
    def put(self, name: str, value: dict, session: Session | None = None) -> None:
        """`session`: the caller's open DB session. Pass it when the caller has already written in this
        transaction - a database-backed store writing through a second session would wait on SQLite's
        write lock until it times out ("database is locked")."""

    @abstractmethod
    def delete(self, name: str) -> None: ...


def connector_secret_name(connector_id: int) -> str:
    return f"connectors/{connector_id}"


def langfuse_secret_name(product_id: int) -> str:
    return f"langfuse/products/{product_id}"
