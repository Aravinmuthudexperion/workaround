"""HashiCorp Vault KV v2 backend (plain HTTP API, no extra dependency)."""
from __future__ import annotations

from arc_tower.utils.http import client

from .base import SecretStore

_PREFIX = "arc-tower"


class VaultSecretStore(SecretStore):
    def __init__(self, addr: str, token: str, mount: str = "secret"):
        if not addr or not token:
            raise RuntimeError("ARC_SECRETS_BACKEND=vault needs VAULT_ADDR and VAULT_TOKEN")
        self._http = client(addr.rstrip("/"), headers={"X-Vault-Token": token})
        self._mount = mount

    def _path(self, kind: str, name: str) -> str:
        return f"/v1/{self._mount}/{kind}/{_PREFIX}/{name}"

    def get(self, name: str) -> dict | None:
        resp = self._http.get(self._path("data", name))
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()["data"]["data"]

    def put(self, name: str, value: dict) -> None:
        self._http.post(self._path("data", name), json={"data": value}).raise_for_status()

    def delete(self, name: str) -> None:
        resp = self._http.delete(self._path("metadata", name))
        if resp.status_code not in (204, 404):
            resp.raise_for_status()
