"""Read-only Azure Resource Manager calls used by the Azure platform connector:
Resource Graph (inventory, role assignments), Azure Monitor metrics and Cost Management.

Signs in with a client secret (app registration) when one is configured, otherwise with
DefaultAzureCredential: Managed Identity when ARC runs in Azure, `az login` on a developer machine.
Requires: pip install -e .[azure]
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import httpx

from arc_tower.utils.http import client as http_client

ARM = "https://management.azure.com"
_TOKEN_SCOPE = f"{ARM}/.default"
_GRAPH_API = "2022-10-01"
_METRICS_API = "2023-10-01"
_COST_API = "2023-03-01"
_GRAPH_PAGE = 1000


class AzureError(RuntimeError):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class MetricValue:
    dimension: str      # e.g. the model deployment name when split by ModelDeploymentName; "" otherwise
    value: float
    at: datetime | None = None    # timestamp of the data point (latest=True only)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def credential_token_provider(tenant_id: str, client_id: str, client_secret: str) -> Callable[[], str]:
    """Token provider with caching. Client secret if given, else DefaultAzureCredential (Managed Identity)."""
    try:
        from azure import identity
    except ImportError as exc:
        raise AzureError("missing package for the Azure connector: pip install -e .[azure]") from exc
    if client_secret:
        if not (tenant_id and client_id):
            raise AzureError("tenant ID and client ID are required with a client secret")
        credential = identity.ClientSecretCredential(tenant_id, client_id, client_secret)
    else:
        credential = identity.DefaultAzureCredential(managed_identity_client_id=client_id or None)
    cache: dict = {}

    def token() -> str:
        if cache.get("expires_on", 0) - 120 < time.time():
            try:
                access = credential.get_token(_TOKEN_SCOPE)
            except Exception as exc:  # azure-identity raises its own hierarchy
                raise AzureError(f"Azure sign-in failed: {exc}", 401) from exc
            cache.update(token=access.token, expires_on=access.expires_on)
        return cache["token"]

    return token


class AzureClient:
    def __init__(self, token_provider: Callable[[], str], http: httpx.Client | None = None):
        self._token = token_provider
        self._http = http or http_client(ARM, timeout=httpx.Timeout(60.0, connect=10.0))

    @classmethod
    def from_credentials(cls, tenant_id: str, client_id: str, client_secret: str) -> AzureClient:
        return cls(credential_token_provider(tenant_id, client_id, client_secret))

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, url: str, **kwargs) -> dict:
        for attempt in range(3):
            resp = self._http.request(method, url, headers={"Authorization": f"Bearer {self._token()}"}, **kwargs)
            if resp.status_code == 429 and attempt < 2:      # ARM throttling: honour Retry-After, briefly
                time.sleep(min(float(resp.headers.get("Retry-After") or 2), 10))
                continue
            break
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except ValueError:
                detail = resp.text[:200]
            raise AzureError(f"HTTP {resp.status_code}: {detail or resp.reason_phrase}", resp.status_code)
        return resp.json() if resp.content else {}

    # --- Resource Graph ---------------------------------------------------------------

    def resource_graph(self, query: str, subscriptions: list[str]) -> list[dict]:
        rows: list[dict] = []
        skip_token = None
        while True:
            options: dict = {"resultFormat": "objectArray", "$top": _GRAPH_PAGE}
            if skip_token:
                options["$skipToken"] = skip_token
            body = self._request("POST", f"/providers/Microsoft.ResourceGraph/resources?api-version={_GRAPH_API}",
                                 json={"subscriptions": subscriptions, "query": query, "options": options})
            rows.extend(body.get("data") or [])
            skip_token = body.get("$skipToken")
            if not skip_token:
                return rows

    # --- Azure Monitor metrics --------------------------------------------------------

    def metric(self, resource_id: str, name: str, aggregation: str, start: datetime, end: datetime,
               interval: str = "PT1M", split_by: str | None = None, latest: bool = False) -> list[MetricValue]:
        """One value per series. latest=True: the most recent point that has data (with its time);
        otherwise the whole span: summed for Total/Count, averaged for Average."""
        params = {"api-version": _METRICS_API, "metricnames": name, "aggregation": aggregation,
                  "timespan": f"{_iso(start)}/{_iso(end)}", "interval": interval}
        if split_by:
            params["$filter"] = f"{split_by} eq '*'"
        body = self._request("GET", f"{resource_id}/providers/Microsoft.Insights/metrics", params=params)
        key = aggregation.lower()
        values: list[MetricValue] = []
        for metric in body.get("value") or []:
            for series in metric.get("timeseries") or []:
                points = [p for p in series.get("data") or [] if p.get(key) is not None]
                if not points:
                    continue
                dims = series.get("metadatavalues") or []
                dimension = str(dims[0].get("value", "")) if dims else ""
                if latest:
                    point = max(points, key=lambda p: p.get("timeStamp", ""))
                    values.append(MetricValue(dimension, float(point[key]), _parse_ts(point.get("timeStamp"))))
                    continue
                numbers = [p[key] for p in points]
                total = sum(numbers) / len(numbers) if key == "average" else sum(numbers)
                values.append(MetricValue(dimension, float(total)))
        return values

    # --- Cost Management --------------------------------------------------------------

    def cost_by_resource(self, scope: str, timeframe: str = "MonthToDate") -> dict[str, tuple[float, str]]:
        """{lower-case resource id: (cost, currency)} for a subscription or resource-group scope."""
        body = {"type": "ActualCost", "timeframe": timeframe,
                "dataset": {"granularity": "None",
                            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                            "grouping": [{"type": "Dimension", "name": "ResourceId"}]}}
        url = f"{scope}/providers/Microsoft.CostManagement/query?api-version={_COST_API}"
        costs: dict[str, tuple[float, str]] = {}
        while url:
            props = self._request("POST", url, json=body).get("properties") or {}
            names = [c.get("name", "") for c in props.get("columns") or []]
            lowered = [n.lower() for n in names]
            if "resourceid" not in lowered:
                break
            rid_i = lowered.index("resourceid")
            cost_i = next((i for i, n in enumerate(lowered) if n in ("cost", "pretaxcost", "costusd")), None)
            cur_i = next((i for i, n in enumerate(lowered) if n == "currency"), None)
            if cost_i is None:
                break
            for row in props.get("rows") or []:
                rid = str(row[rid_i]).lower()
                previous = costs.get(rid, (0.0, ""))[0]
                costs[rid] = (previous + float(row[cost_i] or 0), str(row[cur_i]) if cur_i is not None else "")
            url = props.get("nextLink")
        return costs
