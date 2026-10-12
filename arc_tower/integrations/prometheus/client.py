"""Minimal Prometheus HTTP API client."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import httpx

from arc_tower.core.config import PROMETHEUS_URL
from arc_tower.utils.http import client


@dataclass
class Sample:
    labels: dict[str, str]
    value: float


@dataclass
class Series:
    labels: dict[str, str]
    points: list[tuple[float, float]]  # (unix seconds, value)


class PrometheusError(RuntimeError):
    pass


def _float(value: str) -> float:
    number = float(value)
    return 0.0 if math.isnan(number) or math.isinf(number) else number


class PrometheusClient:
    def __init__(self, base_url: str = PROMETHEUS_URL):
        self._http = client(base_url)

    def _get(self, path: str, params: dict) -> dict:
        try:
            resp = self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise PrometheusError(f"Prometheus unreachable: {exc}") from exc
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code >= 400 or body.get("status") != "success":
            raise PrometheusError(body.get("error") or f"HTTP {resp.status_code}")
        return body["data"]

    def query(self, expr: str) -> list[Sample]:
        data = self._get("/api/v1/query", {"query": expr})
        if data["resultType"] == "scalar":
            return [Sample({}, _float(data["result"][1]))]
        return [Sample(r["metric"], _float(r["value"][1])) for r in data["result"]]

    def scalar(self, expr: str, default: float | None = None) -> float | None:
        samples = self.query(expr)
        return samples[0].value if samples else default

    def query_range(self, expr: str, start: datetime, end: datetime, step_seconds: int) -> list[Series]:
        data = self._get("/api/v1/query_range", {
            "query": expr, "start": start.timestamp(), "end": end.timestamp(), "step": step_seconds,
        })
        return [Series(r["metric"], [(float(t), _float(v)) for t, v in r["values"]]) for r in data["result"]]

    def label_values(self, label: str, match: str | None = None) -> list[str]:
        params = {"match[]": match} if match else {}
        return self._get(f"/api/v1/label/{label}/values", params)

    def healthy(self) -> bool:
        try:
            return self._http.get("/-/ready").status_code == 200
        except httpx.HTTPError:
            return False
