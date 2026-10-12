"""Secrets store (connector credentials, Langfuse project keys).

`get_secret_store()` returns the backend chosen by ARC_SECRETS_BACKEND:
"local" (encrypted in the ARC database) or "vault" (HashiCorp Vault KV v2).
"""
from __future__ import annotations

from functools import lru_cache

from arc_tower.core.config import get_settings

from .base import SecretStore


@lru_cache
def get_secret_store() -> SecretStore:
    settings = get_settings()
    if settings.secrets_backend == "vault":
        from .vault import VaultSecretStore
        return VaultSecretStore(settings.vault_addr, settings.vault_token, settings.vault_mount)
    from .local import LocalSecretStore
    return LocalSecretStore()


__all__ = ["SecretStore", "get_secret_store"]
