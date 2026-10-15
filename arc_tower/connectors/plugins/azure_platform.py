"""Azure platform: map a product's Azure resources, then pull their metrics and cost.

The connector runs every minute; each part has its own pace:
  - every run (1 min): the latest complete 1-minute Azure Monitor value of each metric, for every
    resource not rejected (Reader / Monitoring Reader), sent to Prometheus as arc_azure_metric.
    One minute is Azure Monitor's finest grain; values appear in Azure 1-3 minutes after the fact.
  - every 30 min (or on "Run now" / connector save): scan the subscriptions / resource groups with
    Resource Graph (Reader) and propose which resources belong to the product, starting from the one
    the client named (services/azure_mapping.py), keeping earlier review decisions.
  - every 1 h: month-to-date cost per resource (Cost Management Reader); the product's attributable
    part is re-sent every run as arc_azure_cost.
Requires: pip install -e .[azure]
"""
from __future__ import annotations

from datetime import datetime, timedelta

from arc_tower.integrations.azure import AzureClient, AzureError
from arc_tower.otlp.builders import GaugePoint
from arc_tower.services import azure_mapping, azure_resources
from arc_tower.services.azure_mapping import Inventory, MappingError
from arc_tower.utils.timeutil import utcnow

from ..base import ConnectorError, PullConnector, RunContext, RunResult

SERVICE_NAME = "azure-platform"
METRIC_LOOKBACK = timedelta(minutes=5)     # fetch a few 1-minute points, keep the newest with data
SCAN_REFRESH = timedelta(minutes=30)
COST_REFRESH = timedelta(hours=1)        # Azure publishes cost hours late; the API is rate-limited
SCAN_CURSOR = "scan_at"                    # cleared to force a re-scan (see services/connectors.py)

# Metrics pulled per resource type: (metric name, aggregation, split-by dimension).
METRIC_CATALOG: dict[str, list[tuple[str, str, str | None]]] = {
    "microsoft.cognitiveservices/accounts": [("ProcessedPromptTokens", "Total", "ModelDeploymentName"),
                                             ("GeneratedTokens", "Total", "ModelDeploymentName"),
                                             ("AzureOpenAIRequests", "Total", "ModelDeploymentName")],
    "microsoft.documentdb/databaseaccounts": [("TotalRequestUnits", "Total", None), ("TotalRequests", "Count", None)],
    "microsoft.storage/storageaccounts": [("Transactions", "Total", None), ("Egress", "Total", None)],
    "microsoft.dbforpostgresql/flexibleservers": [("cpu_percent", "Average", None),
                                                  ("active_connections", "Average", None)],
    "microsoft.web/sites": [("Requests", "Total", None), ("Http5xx", "Total", None)],
    "microsoft.app/containerapps": [("Requests", "Total", None)],
    "microsoft.search/searchservices": [("SearchQueriesPerSecond", "Average", None)],
}


def _hint(exc: AzureError, what: str) -> str:
    if exc.status_code == 401:
        return f"{what}: Azure sign-in failed - check tenant ID, client ID and client secret ({exc})"
    if exc.status_code == 403:
        return f"{what}: permission denied - grant the ARC identity the read-only role for this ({exc})"
    return f"{what}: {exc}"


class AzurePlatformConnector(PullConnector):
    type_key = "azure_platform"

    @staticmethod
    def client_factory(tenant_id: str, client_id: str, client_secret: str) -> AzureClient:
        return AzureClient.from_credentials(tenant_id, client_id, client_secret)

    def run(self, ctx: RunContext) -> RunResult:
        if ctx.session is None or ctx.product is None:
            raise ConnectorError("the Azure platform connector must run inside the connector worker")
        cfg = ctx.config
        try:
            subscriptions = azure_mapping.parse_subscriptions(cfg.get("subscription_ids", ""))
            groups = azure_mapping.parse_resource_groups(cfg.get("resource_groups", ""))
        except MappingError as exc:
            raise ConnectorError(str(exc)) from exc
        try:
            client = self.client_factory(cfg.get("tenant_id", ""), cfg.get("client_id", ""),
                                         ctx.secrets.get("client_secret", ""))
        except AzureError as exc:
            raise ConnectorError(str(exc)) from exc
        now = utcnow()
        cursor = dict(ctx.cursor)
        try:
            scan_note = self._scan_if_due(ctx, client, subscriptions, groups, now, cursor)
            metric_count, metric_errors = self._pull_metrics(ctx, client, now)
            cost_note = self._pull_cost(ctx, client, subscriptions, now, cursor)
        finally:
            client.close()
        self._emit_cost(ctx, now)
        metrics_note = f"{metric_count} metric series (1 min)"
        if metric_errors:
            metrics_note += f", {metric_errors} unavailable"
        return RunResult(records=metric_count, cursor=cursor,
                         message=f"{scan_note} · {metrics_note} · {cost_note}")

    # --- steps --------------------------------------------------------------------------

    def _scan_if_due(self, ctx: RunContext, client: AzureClient, subscriptions: list[str], groups: list[str],
                     now: datetime, cursor: dict) -> str:
        last = cursor.get(SCAN_CURSOR)
        if last and now - datetime.fromisoformat(last) < SCAN_REFRESH and ctx.product.azure_resources:
            mapped = len(azure_resources.tracked(ctx.product))
            return f"{mapped} resources tracked (scanned {last[11:16]} UTC)"
        s = self._scan(ctx, client, subscriptions, groups, now)
        cursor[SCAN_CURSOR] = now.isoformat()
        note = f"scanned: {s.total} resources mapped ({s.high} high, {s.medium} medium, {s.low} low; {s.new} new)"
        return note + (f", {s.missing} no longer found" if s.missing else "")

    def _scan(self, ctx: RunContext, client: AzureClient, subscriptions: list[str], groups: list[str],
              now: datetime) -> azure_resources.ScanSummary:
        try:
            rows = client.resource_graph(azure_mapping.resources_query(groups), subscriptions)
            assignments = client.resource_graph(azure_mapping.ROLE_ASSIGNMENTS_QUERY, subscriptions)
        except AzureError as exc:
            raise ConnectorError(_hint(exc, "resource scan")) from exc
        try:
            candidates = azure_mapping.build_product_map(Inventory.from_graph(rows, assignments),
                                                         ctx.config.get("starting_point", ""),
                                                         ctx.config.get("product_tag") or None)
        except MappingError as exc:
            raise ConnectorError(str(exc)) from exc
        return azure_resources.apply_scan(ctx.session, ctx.product, candidates, now)

    def _pull_metrics(self, ctx: RunContext, client: AzureClient, now: datetime) -> tuple[int, int]:
        count = errors = 0
        start = now - METRIC_LOOKBACK
        for row in azure_resources.tracked(ctx.product):
            catalog = METRIC_CATALOG.get(row.type)
            if not catalog:
                continue
            latest: dict[str, dict[str, float]] = {}
            for name, aggregation, split_by in catalog:
                try:
                    values = client.metric(row.resource_id, name, aggregation, start, now, interval="PT1M",
                                           split_by=split_by, latest=True)
                except AzureError as exc:
                    errors += 1
                    ctx.log.info("%s %s: %s", row.name, name, exc)
                    continue
                latest[name] = {v.dimension: v.value for v in values}
                for v in values:
                    # Stamped with Azure's own minute, so a value fetched twice is the same sample.
                    ctx.emitter.metric(SERVICE_NAME, GaugePoint(
                        "arc.azure.metric", v.value, v.at or now,
                        {"azure.resource": row.name, "azure.resource_type": row.short_type, "azure.metric": name,
                         "azure.dimension": v.dimension, "azure.window": "1m"},
                        description="Azure Monitor metric for a resource mapped to the product (per minute)"))
                    count += 1
            row.metrics = latest
        return count, errors

    def _pull_cost(self, ctx: RunContext, client: AzureClient, subscriptions: list[str], now: datetime,
                   cursor: dict) -> str:
        last = cursor.get("cost_at")
        if last and now - datetime.fromisoformat(last) < COST_REFRESH:
            return f"cost refreshed {last[:16].replace('T', ' ')} UTC"
        subscription_of = {s.lower(): s for s in subscriptions}
        scopes = [f"/subscriptions/{subscription_of[r.resource_id.split('/')[2]]}/resourceGroups/{r.resource_group}"
                  for r in azure_resources.tracked(ctx.product)
                  if r.resource_id.split("/")[2] in subscription_of and r.resource_group]
        costs: dict[str, tuple[float, str]] = {}
        try:
            for scope in dict.fromkeys(scopes):
                costs.update(client.cost_by_resource(scope))
        except AzureError as exc:
            ctx.log.warning("cost: %s", exc)
            return _hint(exc, "cost not updated")
        for row in azure_resources.tracked(ctx.product):
            cost, currency = costs.get(row.resource_id, (0.0, row.currency or ""))
            row.cost_mtd, row.currency = round(cost, 4), currency
        cursor["cost_at"] = now.isoformat()
        return "cost updated"

    def _emit_cost(self, ctx: RunContext, now: datetime) -> None:
        """Re-send the attributable cost every run so the Prometheus series stays current between pulls."""
        for row in azure_resources.tracked(ctx.product):
            value, basis = azure_resources.attributed_cost(row)
            if value is None:
                continue
            ctx.emitter.metric(SERVICE_NAME, GaugePoint(
                "arc.azure.cost", value, now,
                {"azure.resource": row.name, "azure.resource_type": row.short_type, "currency": row.currency,
                 "azure.cost_basis": "dedicated" if basis == "dedicated" else "allocated", "azure.period": "mtd"},
                description="Month-to-date Azure cost attributed to the product"))
