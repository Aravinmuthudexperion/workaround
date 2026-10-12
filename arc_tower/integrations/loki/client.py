"""Loki query client. Every call is scoped to tenants via X-Scope-OrgID."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from arc_tower.core.config import LOKI_URL
from arc_tower.utils.http import client


@dataclass
class LogLine:
    at: datetime
    line: str
    labels: dict[str, str]


class LokiClient:
    def __init__(self, base_url: str = LOKI_URL):
        self._http = client(base_url)

    @staticmethod
    def _org_header(tenants: list[str]) -> dict[str, str]:
        # Loki multi-tenant queries: "a|b|c" (enabled in loki.yaml).
        return {"X-Scope-OrgID": "|".join(tenants) if tenants else "__none__"}

    def query_range(self, tenants: list[str], logql: str, start: datetime, end: datetime,
                    limit: int = 100) -> list[LogLine]:
        resp = self._http.get("/loki/api/v1/query_range", headers=self._org_header(tenants), params={
            "query": logql, "start": int(start.timestamp() * 1e9), "end": int(end.timestamp() * 1e9),
            "limit": limit, "direction": "backward",
        })
        resp.raise_for_status()
        lines = []
        for stream in resp.json()["data"]["result"]:
            for ts, line in stream["values"]:
                lines.append(LogLine(datetime.fromtimestamp(int(ts) / 1e9, tz=timezone.utc), line, stream["stream"]))
        return sorted(lines, key=lambda log: log.at, reverse=True)[:limit]

    def instant(self, tenants: list[str], logql: str) -> list[tuple[dict[str, str], float]]:
        """Instant metric query (e.g. sum by (...) (count_over_time(...))) -> [(labels, value)]."""
        resp = self._http.get("/loki/api/v1/query", headers=self._org_header(tenants), params={"query": logql})
        resp.raise_for_status()
        return [(r["metric"], float(r["value"][1])) for r in resp.json()["data"]["result"]]

    def count(self, tenants: list[str], logql_selector: str, window: str = "24h") -> float:
        resp = self._http.get("/loki/api/v1/query", headers=self._org_header(tenants),
                              params={"query": f"sum(count_over_time({logql_selector}[{window}]))"})
        resp.raise_for_status()
        result = resp.json()["data"]["result"]
        return float(result[0]["value"][1]) if result else 0.0

    def healthy(self) -> bool:
        try:
            return self._http.get("/ready").status_code == 200
        except httpx.HTTPError:
            return False
