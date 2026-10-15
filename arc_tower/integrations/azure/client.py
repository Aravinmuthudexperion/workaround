"""Read-only Azure calls used by the Azure platform connector: Resource Graph (inventory, role
assignments), Azure Monitor metrics, Cost Management (all through Resource Manager) and Log Analytics
queries (App Insights traces, through the Log Analytics API).

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
LOGS = "https://api.loganalytics.io"
_TOKEN_SCOPE = f"{ARM}/.default"
_LOGS_SCOPE = f"{LOGS}/.default"
_GRAPH_API = "2022-10-01"
_METRICS_API = "2023-10-01"
_METRIC_DEFINITIONS_API = "2018-01-01"
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


def _credential(tenant_id: str, client_id: str, client_secret: str):
    """Client secret if given, else DefaultAzureCredential (Managed Identity, or `az login` locally)."""
    try:
        from azure import identity
    except ImportError as exc:
        raise AzureError("missing package for the Azure connector: pip install -e .[azure]") from exc
    if client_secret:
        if not (tenant_id and client_id):
            raise AzureError("tenant ID and client ID are required with a client secret")
        return identity.ClientSecretCredential(tenant_id, client_id, client_secret)
    return identity.DefaultAzureCredential(managed_identity_client_id=client_id or None)


def token_provider(credential, scope: str = _TOKEN_SCOPE) -> Callable[[], str]:
    """Token provider for one audience, with caching."""
    cache: dict = {}

    def token() -> str:
        if cache.get("expires_on", 0) - 120 < time.time():
            try:
                access = credential.get_token(scope)
            except Exception as exc:  # azure-identity raises its own hierarchy
                raise AzureError(f"Azure sign-in failed: {exc}", 401) from exc
            cache.update(token=access.token, expires_on=access.expires_on)
        return cache["token"]

    return token


class AzureClient:
    def __init__(self, token_provider: Callable[[], str], http: httpx.Client | None = None,
                 logs_token_provider: Callable[[], str] | None = None, logs_http: httpx.Client | None = None):
        self._token = token_provider
        self._http = http or http_client(ARM, timeout=httpx.Timeout(60.0, connect=10.0))
        self._logs_token = logs_token_provider or token_provider
        self._logs_http = logs_http
        self._own_logs_http = logs_http is None

    @classmethod
    def from_credentials(cls, tenant_id: str, client_id: str, client_secret: str) -> AzureClient:
        credential = _credential(tenant_id, client_id, client_secret)
        return cls(token_provider(credential), logs_token_provider=token_provider(credential, _LOGS_SCOPE))

    def close(self) -> None:
        self._http.close()
        if self._logs_http is not None and self._own_logs_http:
            self._logs_http.close()

    def _request(self, method: str, url: str, http: httpx.Client | None = None,
                 token: Callable[[], str] | None = None, **kwargs) -> dict:
        http, token = http or self._http, token or self._token
        for attempt in range(3):
            resp = http.request(method, url, headers={"Authorization": f"Bearer {token()}"}, **kwargs)
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

    def resource(self, resource_id: str, api_version: str) -> dict:
        """One resource as Resource Manager returns it (e.g. a Log Analytics workspace outside the scan)."""
        return self._request("GET", resource_id, params={"api-version": api_version})

    def openai_deployments(self, account_id: str) -> dict[str, dict]:
        """{deployment name: {"model", "version", "sku"}} of an Azure OpenAI / AI Services account."""
        body = self._request("GET", f"{account_id}/deployments", params={"api-version": "2024-10-01"})
        return {d.get("name", ""): {"model": ((d.get("properties") or {}).get("model") or {}).get("name", ""),
                                    "version": ((d.get("properties") or {}).get("model") or {}).get("version", ""),
                                    "sku": (d.get("sku") or {}).get("name", "")}
                for d in body.get("value") or []}

    def created_times(self, scope: str) -> dict[str, str]:
        """{lower-case resource id: creation time (ISO)} for a resource group or subscription."""
        found: dict[str, str] = {}
        url, params = f"{scope}/resources", {"api-version": "2021-04-01", "$expand": "createdTime"}
        while url:
            body = self._request("GET", url, params=params)
            for res in body.get("value") or []:
                if res.get("id") and res.get("createdTime"):
                    found[res["id"].lower()] = res["createdTime"]
            url, params = body.get("nextLink"), None
        return found

    # --- Azure Monitor metrics --------------------------------------------------------

    def metric_names(self, resource_id: str) -> set[str]:
        """Names of the metrics a resource publishes. They vary within one type: a Flex Consumption
        function app has execution counts but no Requests / Http5xx, unlike an App Service web app."""
        body = self._request("GET", f"{resource_id}/providers/Microsoft.Insights/metricDefinitions",
                             params={"api-version": _METRIC_DEFINITIONS_API})
        return {str((d.get("name") or {}).get("value", "")) for d in body.get("value") or []} - {""}

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

    # --- Log Analytics ----------------------------------------------------------------

    def logs_query(self, workspace_id: str, query: str, start: datetime, end: datetime) -> list[dict]:
        """Rows of the first result table, as {column: value}. workspace_id is the workspace GUID
        (customerId). Needs Log Analytics Reader on the workspace."""
        if self._logs_http is None:
            self._logs_http = http_client(LOGS, timeout=httpx.Timeout(60.0, connect=10.0))
        body = self._request("POST", f"/v1/workspaces/{workspace_id}/query", http=self._logs_http,
                             token=self._logs_token, json={"query": query, "timespan": f"{_iso(start)}/{_iso(end)}"})
        tables = body.get("tables") or []
        if not tables:
            return []
        names = [c.get("name", "") for c in tables[0].get("columns") or []]
        return [dict(zip(names, row)) for row in tables[0].get("rows") or []]
