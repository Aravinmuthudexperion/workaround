"""Secrets-store interface. Values are small JSON objects."""
from __future__ import annotations

from abc import ABC, abstractmethod


class SecretStore(ABC):
    @abstractmethod
    def get(self, name: str) -> dict | None: ...

    @abstractmethod
    def put(self, name: str, value: dict) -> None: ...

    @abstractmethod
    def delete(self, name: str) -> None: ...


def connector_secret_name(connector_id: int) -> str:
    return f"connectors/{connector_id}"


def langfuse_secret_name(product_id: int) -> str:
    return f"langfuse/products/{product_id}"
