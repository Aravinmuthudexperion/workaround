"""Langfuse public API clients.

`LangfuseClient` uses project keys (read traces, metrics, scores).
`LangfuseAdminClient` uses organization-scoped keys (Langfuse Enterprise) to create projects and keys.
"""
from __future__ import annotations

import json
from datetime import datetime

import httpx

from arc_tower.utils.http import client


class LangfuseError(RuntimeError):
    pass


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class _Base:
    def __init__(self, host: str, public_key: str, secret_key: str):
        self.host = host.rstrip("/")
        self._http = client(self.host, auth=(public_key, secret_key))

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise LangfuseError(f"Langfuse unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise LangfuseError(f"Langfuse {method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else {}


class LangfuseClient(_Base):
    def projects(self) -> list[dict]:
        return self._request("GET", "/api/public/projects").get("data", [])

    def health(self) -> bool:
        try:
            self._request("GET", "/api/public/health")
            return True
        except LangfuseError:
            return False

    def root_observations(self, tags: list[str] | None = None, limit: int = 25, cursor: str | None = None,
                          user_id: str | None = None, session_id: str | None = None,
                          from_ts: datetime | None = None) -> tuple[list[dict], str | None]:
        """One row per request (the root observation of each trace), newest first.

        Uses Observations API v2, the read path that works on Langfuse v4 (Cloud is v4-only from
        16 Nov 2026; the classic /api/public/traces endpoint is gone in v4 events-only mode).
        """
        filters: list[dict] = [{"type": "boolean", "column": "isRootObservation", "operator": "=", "value": True}]
        if tags:
            filters.append({"type": "arrayOptions", "column": "tags", "operator": "all of", "value": tags})
        if user_id:
            filters.append({"type": "string", "column": "userId", "operator": "=", "value": user_id})
        if session_id:
            filters.append({"type": "string", "column": "sessionId", "operator": "=", "value": session_id})
        if from_ts:
            filters.append({"type": "datetime", "column": "startTime", "operator": ">=", "value": _iso(from_ts)})
        params: dict = {"filter": json.dumps(filters), "limit": limit, "fields": "core,basic,metrics,trace_context"}
        if cursor:
            params["cursor"] = cursor
        body = self._request("GET", "/api/public/v2/observations", params=params)
        return body.get("data", []), (body.get("meta") or {}).get("cursor")

    def trace_costs(self, trace_ids: list[str]) -> dict[str, float]:
        """Total cost per trace (sum over all of its observations)."""
        if not trace_ids:
            return {}
        filters = [{"type": "stringOptions", "column": "traceId", "operator": "any of", "value": trace_ids}]
        body = self._request("GET", "/api/public/v2/observations",
                             params={"filter": json.dumps(filters), "limit": 1000, "fields": "core,usage"})
        costs: dict[str, float] = {}
        for obs in body.get("data", []):
            costs[obs["traceId"]] = costs.get(obs["traceId"], 0.0) + float(obs.get("totalCost") or 0)
        return costs

    def trace_url(self, project_id: str, trace_id: str) -> str:
        return f"{self.host}/project/{project_id}/traces/{trace_id}"

    def metrics(self, query: dict) -> list[dict]:
        return self._request("GET", "/api/public/v2/metrics", params={"query": json.dumps(query)}).get("data", [])

    def cost_by_model(self, tags: list[str], start: datetime, end: datetime) -> list[dict]:
        """[{"model": ..., "cost": usd, "input_tokens": n, "output_tokens": n, "count": n}] for traces with these tags."""
        rows = self.metrics({
            "view": "observations",
            "dimensions": [{"field": "providedModelName"}],
            "metrics": [{"measure": "totalCost", "aggregation": "sum"},
                        {"measure": "inputTokens", "aggregation": "sum"},
                        {"measure": "outputTokens", "aggregation": "sum"},
                        {"measure": "count", "aggregation": "count"}],
            "filters": _tag_filter(tags),
            "fromTimestamp": _iso(start),
            "toTimestamp": _iso(end),
        })
        return [{
            "model": row.get("providedModelName") or "unknown",
            "cost": float(_pick(row, "totalCost") or 0),
            "input_tokens": float(_pick(row, "inputTokens") or 0),
            "output_tokens": float(_pick(row, "outputTokens") or 0),
            "count": float(_pick(row, "count") or 0),
        } for row in rows if row.get("providedModelName")]

    def average_scores(self, tags: list[str], start: datetime, end: datetime) -> dict[str, float]:
        """Average numeric score per score name (LLM-as-judge, annotations, feedback), via Metrics API v2."""
        rows = self.metrics({
            "view": "scores-numeric",
            "dimensions": [{"field": "name"}],
            "metrics": [{"measure": "value", "aggregation": "avg"}],
            "filters": _tag_filter(tags),
            "fromTimestamp": _iso(start),
            "toTimestamp": _iso(end),
        })
        return {row["name"]: float(_pick(row, "value")) for row in rows
                if row.get("name") and _pick(row, "value") is not None}


class LangfuseAdminClient(_Base):
    """Organization-scoped API (Langfuse Enterprise / self-hosted EE only)."""

    def create_project(self, name: str) -> dict:
        return self._request("POST", "/api/public/projects", json={"name": name, "retention": 0})

    def create_api_key(self, project_id: str, note: str) -> dict:
        return self._request("POST", f"/api/public/projects/{project_id}/apiKeys", json={"note": note})


def _tag_filter(tags: list[str] | None) -> list[dict]:
    """Traces carrying ALL of the tags (tenant:<t> + product:<p> selects exactly one product)."""
    return [{"column": "tags", "operator": "all of", "value": tags, "type": "arrayOptions"}] if tags else []


def _pick(row: dict, measure: str):
    """Metric column names look like "sum_totalCost" / "count_count"; match on the measure name."""
    for key, value in row.items():
        if key == measure or key.endswith("_" + measure) or key.startswith(measure):
            return value
    return None
