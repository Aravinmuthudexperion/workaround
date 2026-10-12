"""Shared HTTP client settings."""
from __future__ import annotations

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


def client(base_url: str = "", **kwargs) -> httpx.Client:
    """A sync client with sane timeouts. Honours HTTPS_PROXY / NO_PROXY from the environment."""
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return httpx.Client(base_url=base_url, **kwargs)


def async_client(base_url: str = "", **kwargs) -> httpx.AsyncClient:
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return httpx.AsyncClient(base_url=base_url, **kwargs)
