"""Azure platform: map a product's Azure resources, then pull their metrics, traces and cost.

The connector runs every minute; each part has its own pace:
  - every run (1 min): the latest complete 1-minute Azure Monitor value of each metric, for every
    resource not rejected (Reader / Monitoring Reader), sent to Prometheus as arc_azure_metric.
    One minute is Azure Monitor's finest grain; values appear in Azure 1-3 minutes after the fact.
    Only the metrics each resource actually publishes are asked for (they differ within a type).
  - every run: new gen_ai.* spans from each App Insights resource mapped to the product, read from its
    Log Analytics workspace (Log Analytics Reader). No workspace ID to enter: the scan finds it.
  - every 30 min (or on "Run now" / connector save): scan the subscriptions / resource groups with
    Resource Graph (Reader) and propose which resources belong to the product, starting from the one
    the client named (services/azure_mapping.py), keeping earlier review decisions. The scan also
    records which metrics each resource publishes and which workspace backs each App Insights resource.
  - every 1 h: month-to-date cost per resource (Cost Management Reader); the product's attributable
    part is re-sent every run as arc_azure_cost.
  - every 1 h: a list-price estimate per resource (services/azure_estimate.py): month-to-date usage
    from Azure Monitor and Log Analytics x Azure Retail Prices, in the billing currency; live while the
    bill lags 8-24 h. Re-sent every run as arc_azure_cost_estimate, attributed like the bill.
Requires: pip install -e .[azure]
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from arc_tower.core.config import get_settings
from arc_tower.integrations.azure import AzureClient, AzureError
from arc_tower.integrations.azure.pricing import RetailPrices
from arc_tower.otlp.builders import GaugePoint
from arc_tower.services import azure_estimate, azure_mapping, azure_resources
from arc_tower.services.azure_mapping import Inventory, MappingError
from arc_tower.utils.timeutil import utcnow

from ..base import ConnectorError, PullConnector, RunContext, RunResult
from .app_insights import COLUMNS, GEN_AI_FILTER, call_log, span_from_row
from .common import parse_time

SERVICE_NAME = "azure-platform"
METRIC_LOOKBACK = timedelta(minutes=5)     # fetch a few 1-minute points, keep the newest with data
SCAN_REFRESH = timedelta(minutes=30)
COST_REFRESH = timedelta(hours=1)        # Azure publishes cost hours late; the API is rate-limited
TRACE_BACKFILL = timedelta(hours=1)        # first pull of a newly mapped App Insights resource
SCAN_CURSOR = "scan_at"                    # cleared to force a re-scan (see services/connectors.py)
METRIC_NAMES = "metric_names"              # {resource id: [metrics it publishes]}, refreshed by each scan
TRACE_SOURCES = "trace_sources"            # {App Insights resource id: workspace GUID}, refreshed by each scan
TRACES_AT = "traces_at"                    # {App Insights resource id: ingestion time of the last row read}
ESTIMATE_REFRESH = timedelta(hours=1)
FACTS = "facts"                            # {resource id: what the estimate needs (SKU, plan, deployments...)}
ESTIMATES = "estimates"                    # {resource id: {amount, currency, method, detail}}
_USAGE_KQL = ("Usage | where TimeGenerated >= startofmonth(now()) | where IsBillable == true "
              "| summarize gb = sum(Quantity) / 1000.0")   # Quantity is in MB
_COMPONENT = "microsoft.insights/components"
_WORKSPACE = "microsoft.operationalinsights/workspaces"
_WORKSPACE_API = "2023-09-01"
_SAFE_ID = re.compile(r"^[a-z0-9/._()\-]+$")
_KQL_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,7})?Z$")   # as Log Analytics returns it

# Metrics pulled per resource type: (metric name, aggregation, split-by dimension).
METRIC_CATALOG: dict[str, list[tuple[str, str, str | None]]] = {
    "microsoft.cognitiveservices/accounts": [("ProcessedPromptTokens", "Total", "ModelDeploymentName"),
                                             ("GeneratedTokens", "Total", "ModelDeploymentName"),
                                             ("AzureOpenAIRequests", "Total", "ModelDeploymentName")],
    "microsoft.documentdb/databaseaccounts": [("TotalRequestUnits", "Total", None), ("TotalRequests", "Count", None)],
    "microsoft.storage/storageaccounts": [("Transactions", "Total", None), ("Egress", "Total", None)],
    "microsoft.dbforpostgresql/flexibleservers": [("cpu_percent", "Average", None),
                                                  ("active_connections", "Average", None)],
    "microsoft.web/sites": [("Requests", "Total", None), ("Http5xx", "Total", None),           # App Service
                            ("FunctionExecutionCount", "Total", None),                          # Consumption
                            ("OnDemandFunctionExecutionCount", "Total", None),                  # Flex Consumption
                            ("AlwaysReadyFunctionExecutionCount", "Total", None)],
    "microsoft.app/containerapps": [("Requests", "Total", None)],
    "microsoft.search/searchservices": [("SearchQueriesPerSecond", "Average", None)],
}


# Readable service names for dashboards (resource types are lower-case ARM types).
SERVICE_NAMES = {
    "microsoft.web/sites": "App Service / Functions",
    "microsoft.web/serverfarms": "App Service plan",
    "microsoft.app/containerapps": "Container Apps",
    "microsoft.app/managedenvironments": "Container Apps environment",
    "microsoft.cognitiveservices/accounts": "Azure OpenAI / AI Services",
    "microsoft.storage/storageaccounts": "Storage",
    "microsoft.keyvault/vaults": "Key Vault",
    "microsoft.documentdb/databaseaccounts": "Cosmos DB",
    "microsoft.dbforpostgresql/flexibleservers": "PostgreSQL",
    "microsoft.search/searchservices": "AI Search",
    "microsoft.insights/components": "Application Insights",
    "microsoft.operationalinsights/workspaces": "Log Analytics",
    "microsoft.managedidentity/userassignedidentities": "Managed identity",
    "microsoft.network/privateendpoints": "Private endpoint",
}


def service_name(resource_type: str) -> str:
    return SERVICE_NAMES.get(resource_type, resource_type.split("/", 1)[-1])


def _hint(exc: AzureError, what: str) -> str:
    if exc.status_code == 401:
        return f"{what}: Azure sign-in failed - check tenant ID, client ID and client secret ({exc})"
    if exc.status_code == 403:
        return f"{what}: permission denied - grant the ARC identity the read-only role for this ({exc})"
    return f"{what}: {exc}"


class AzurePlatformConnector(PullConnector):
    type_key = "azure_platform"

    _prices: RetailPrices | None = None        # shared by runs: the price list is cached for a day

    @staticmethod
    def client_factory(tenant_id: str, client_id: str, client_secret: str) -> AzureClient:
        return AzureClient.from_credentials(tenant_id, client_id, client_secret)

    @classmethod
    def prices(cls) -> RetailPrices:
        if cls._prices is None:
            cls._prices = RetailPrices()
        return cls._prices

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
            metric_count, metric_errors = self._pull_metrics(ctx, client, now, cursor)
            span_count, traces_note = self._pull_traces(ctx, client, now, cursor)
            cost_note = self._pull_cost(ctx, client, subscriptions, now, cursor)
            estimate_note = self._estimate_if_due(ctx, client, now, cursor)
        finally:
            client.close()
        self._emit_cost(ctx, now)
        self._emit_estimates(ctx, now, cursor)
        metrics_note = f"{metric_count} metric series (1 min)"
        if metric_errors:
            metrics_note += f", {metric_errors} unavailable"
        return RunResult(records=metric_count + span_count, cursor=cursor,
                         message=f"{scan_note} · {metrics_note} · {traces_note} · {cost_note} · {estimate_note}")

    # --- steps --------------------------------------------------------------------------

    def _scan_if_due(self, ctx: RunContext, client: AzureClient, subscriptions: list[str], groups: list[str],
                     now: datetime, cursor: dict) -> str:
        last = cursor.get(SCAN_CURSOR)
        if last and now - datetime.fromisoformat(last) < SCAN_REFRESH and ctx.product.azure_resources \
                and TRACE_SOURCES in cursor and FACTS in cursor:
            mapped = len(azure_resources.tracked(ctx.product))
            return f"{mapped} resources tracked (scanned {last[11:16]} UTC)"
        s = self._scan(ctx, client, subscriptions, groups, now, cursor)
        cursor[SCAN_CURSOR] = now.isoformat()
        note = f"scanned: {s.total} resources mapped ({s.high} high, {s.medium} medium, {s.low} low; {s.new} new)"
        return note + (f", {s.missing} no longer found" if s.missing else "")

    def _scan(self, ctx: RunContext, client: AzureClient, subscriptions: list[str], groups: list[str],
              now: datetime, cursor: dict) -> azure_resources.ScanSummary:
        try:
            rows = client.resource_graph(azure_mapping.resources_query(groups), subscriptions)
            assignments = client.resource_graph(azure_mapping.ROLE_ASSIGNMENTS_QUERY, subscriptions)
        except AzureError as exc:
            raise ConnectorError(_hint(exc, "resource scan")) from exc
        inventory = Inventory.from_graph(rows, assignments)
        try:
            candidates = azure_mapping.build_product_map(inventory, ctx.config.get("starting_point", ""),
                                                         ctx.config.get("product_tag") or None)
        except MappingError as exc:
            raise ConnectorError(str(exc)) from exc
        summary = azure_resources.apply_scan(ctx.session, ctx.product, candidates, now)
        cursor[METRIC_NAMES] = self._metric_names(ctx, client)
        cursor[TRACE_SOURCES] = self._trace_sources(ctx, client, inventory, [c.resource for c in candidates])
        cursor[FACTS] = self._facts(ctx, client, inventory, [c.resource for c in candidates])
        return summary

    def _facts(self, ctx: RunContext, client: AzureClient, inventory: Inventory, mapped: list) -> dict[str, dict]:
        """What the cost estimate needs to know about each mapped resource, from the scan plus a few reads."""
        by_id = {r.id: r for r in inventory.resources}
        created: dict[str, str] = {}
        for scope in sorted({res.id.split("/providers/", 1)[0] for res in mapped}):
            try:
                created.update(client.created_times(scope))
            except AzureError as exc:
                ctx.log.info("creation times of %s: %s", scope, exc)
        facts = {}
        for res in mapped:
            props = res.properties
            fact = {"type": res.type, "name": res.name, "kind": res.kind, "location": res.location, "sku": res.sku,
                    "created": created.get(res.id, ""), "reserved": bool(props.get("reserved")),
                    "access_tier": props.get("accessTier") or "",
                    "vault_sku": ((props.get("sku") or {}).get("name") or "") if res.type == azure_estimate.VAULT else "",
                    "pricing_tier": ((props.get("sku") or {}).get("name") or "") if res.type == _WORKSPACE else "",
                    "workspace_guid": str(props.get("customerId") or "") if res.type == _WORKSPACE else "",
                    "serverless": any(str(c.get("name", "")).lower() == "enableserverless"
                                      for c in props.get("capabilities") or [] if isinstance(c, dict))}
            if res.type == azure_estimate.SITE:
                plan = by_id.get(str(props.get("serverFarmId") or "").lower())
                fact["plan"] = (plan.sku if plan else {}) or {}
            if res.type == _COMPONENT:
                workspace = by_id.get(str(props.get("WorkspaceResourceId") or "").lower())
                fact["workspace"] = workspace.name if workspace else str(props.get("WorkspaceResourceId") or "") \
                    .rsplit("/", 1)[-1]
            if res.type == azure_estimate.OPENAI:
                try:
                    fact["deployments"] = client.openai_deployments(res.id)
                except AzureError as exc:
                    ctx.log.info("%s deployments: %s", res.name, exc)
            facts[res.id] = fact
        return facts

    def _metric_names(self, ctx: RunContext, client: AzureClient) -> dict[str, list[str]]:
        """What each tracked resource publishes; resources whose definitions cannot be read get the full catalog."""
        found = {}
        for row in azure_resources.tracked(ctx.product):
            if row.type not in METRIC_CATALOG:
                continue
            try:
                found[row.resource_id] = sorted(client.metric_names(row.resource_id))
            except AzureError as exc:
                ctx.log.info("%s metric definitions: %s", row.name, exc)
        return found

    def _trace_sources(self, ctx: RunContext, client: AzureClient, inventory: Inventory,
                       mapped: list) -> dict[str, str]:
        """{App Insights resource id: GUID of the Log Analytics workspace holding its telemetry}.
        The workspace may sit outside the scanned resource groups (a central one); then it is read directly."""
        by_id = {r.id: r for r in inventory.resources}
        sources = {}
        for res in mapped:
            if res.type != _COMPONENT:
                continue
            workspace_id = str(res.properties.get("WorkspaceResourceId") or "").lower()
            if not workspace_id:
                ctx.log.info("%s: classic App Insights (no workspace) - traces not read", res.name)
                continue
            workspace = by_id.get(workspace_id)
            guid = workspace.properties.get("customerId") if workspace else None
            if not guid:
                try:
                    guid = (client.resource(workspace_id, _WORKSPACE_API).get("properties") or {}).get("customerId")
                except AzureError as exc:
                    ctx.log.info("%s workspace: %s", res.name, exc)
            if guid:
                sources[res.id] = str(guid)
        return sources

    def _pull_metrics(self, ctx: RunContext, client: AzureClient, now: datetime, cursor: dict) -> tuple[int, int]:
        count = errors = 0
        start = now - METRIC_LOOKBACK
        published = cursor.get(METRIC_NAMES) or {}
        for row in azure_resources.tracked(ctx.product):
            catalog = METRIC_CATALOG.get(row.type)
            if not catalog:
                continue
            if row.resource_id in published:
                catalog = [m for m in catalog if m[0] in published[row.resource_id]]
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
                        {"azure.resource": row.name, "azure.resource_type": row.short_type,
                         "azure.service": service_name(row.type), "azure.metric": name,
                         "azure.dimension": v.dimension, "azure.window": "1m"},
                        description="Azure Monitor metric for a resource mapped to the product (per minute)"))
                    count += 1
            row.metrics = latest
        return count, errors

    def _pull_traces(self, ctx: RunContext, client: AzureClient, now: datetime, cursor: dict) -> tuple[int, str]:
        """New model-call spans from each tracked App Insights resource, read by ingestion time so rows
        that arrive late (App Insights takes 2-5 minutes) are neither missed nor read twice."""
        sources = cursor.get(TRACE_SOURCES) or {}
        components = [r for r in azure_resources.tracked(ctx.product) if r.resource_id in sources]
        if not components:
            return 0, "traces: no App Insights mapped"
        read_up_to = dict(cursor.get(TRACES_AT) or {})
        prices = get_settings().model_prices
        count, problems = 0, []
        for row in components:
            if not _SAFE_ID.match(row.resource_id):        # it goes into the KQL text
                ctx.log.warning("traces from %s: unexpected characters in resource ID, skipped", row.name)
                continue
            # The mark is Azure's own text: ingestion times have 100 ns precision, and a Python datetime
            # (microseconds) rounds them down, which made "> mark" match the last row again on every run.
            mark = read_up_to.get(row.resource_id, "")
            if not _KQL_TIME.match(mark):
                # No mark yet: backfill. A mark saved by an older version (microseconds, rounded down):
                # step past it, so its last row is not read again.
                old = parse_time(mark) if mark else None
                start = old + timedelta(microseconds=1) if old else now - TRACE_BACKFILL
                mark = start.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            since = parse_time(mark) or now - TRACE_BACKFILL
            query = (f"AppDependencies\n| where _ResourceId =~ '{row.resource_id}'\n"
                     f"| where ingestion_time() > datetime({mark})\n| where {GEN_AI_FILTER}\n"
                     f"| extend Ingested = ingestion_time()\n| project Ingested, {COLUMNS}\n"
                     "| order by Ingested asc\n| take 5000")
            try:
                rows = client.logs_query(sources[row.resource_id], query, since - TRACE_BACKFILL, now)
            except AzureError as exc:
                ctx.log.warning("traces from %s: %s", row.name, exc)
                problems.append(_hint(exc, f"traces from {row.name}"))
                continue
            for record in rows:
                converted = span_from_row(record, row.name)
                if converted:
                    service, span = converted
                    ctx.emitter.span(service, span)
                    entry = call_log(service, span, prices)
                    if entry:
                        ctx.emitter.log(service, entry)
                    count += 1
            newest = str(rows[-1].get("Ingested") or "") if rows else ""     # rows are in ingestion order
            if _KQL_TIME.match(newest):
                read_up_to[row.resource_id] = newest
        cursor[TRACES_AT] = read_up_to
        note = f"{count} spans from {len(components)} App Insights"
        return count, note + (f" ({'; '.join(problems)})" if problems else "")

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

    def _estimate_if_due(self, ctx: RunContext, client: AzureClient, now: datetime, cursor: dict) -> str:
        """Month-to-date list-price estimate per tracked resource, refreshed hourly."""
        last = cursor.get("estimate_at")
        if last and now - datetime.fromisoformat(last) < ESTIMATE_REFRESH and ESTIMATES in cursor:
            return f"estimate refreshed {last[11:16]} UTC"
        currency = (ctx.config.get("price_currency") or next(
            (r.currency for r in azure_resources.tracked(ctx.product) if r.currency), "") or "USD").upper()
        period = azure_estimate.Period(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), now)
        prices = self.prices()

        def meters(**fields):
            return prices.meters(currency, **fields)

        facts, estimates, problems = cursor.get(FACTS) or {}, {}, []
        for row in azure_resources.tracked(ctx.product):
            fact = facts.get(row.resource_id)
            if not fact:
                continue
            usage = self._usage(ctx, client, row, fact, period)
            try:
                result = azure_estimate.estimate(fact, usage, meters, period)
            except AzureError as exc:                      # the price list could not be read
                problems.append(str(exc))
                result = azure_estimate.Estimate(None, "not estimated", "Price list unavailable")
            estimates[row.resource_id] = {"amount": None if result.amount is None else round(result.amount, 4),
                                          "currency": currency, "method": result.method, "detail": result.detail}
        if problems and all(e["amount"] is None for e in estimates.values()):
            ctx.log.warning("estimate: %s", problems[0])
            return f"estimate not updated ({problems[0][:80]})"
        cursor[ESTIMATES] = estimates
        cursor["estimate_at"] = now.isoformat()
        total = sum(e["amount"] or 0 for e in estimates.values())
        return f"estimate {total:,.2f} {currency} month to date ({sum(e['amount'] is not None for e in estimates.values())} resources)"

    def _usage(self, ctx: RunContext, client: AzureClient, row, fact: dict,
               period: azure_estimate.Period) -> azure_estimate.Usage:
        usage: azure_estimate.Usage = {}
        for need in azure_estimate.needs(fact):
            try:
                values = client.metric(row.resource_id, need.metric, need.aggregation, period.start, period.now,
                                       interval="PT1H", split_by=need.split_by, latest=need.latest)
            except AzureError as exc:
                ctx.log.info("%s %s (month to date): %s", row.name, need.metric, exc)
                continue
            usage[need.metric] = {v.dimension: v.value for v in values}
        if fact.get("type") == _WORKSPACE and fact.get("workspace_guid"):
            try:
                rows = client.logs_query(fact["workspace_guid"], _USAGE_KQL, period.start, period.now)
                usage[azure_estimate.INGESTION_GB] = {"": float((rows[0].get("gb") if rows else 0) or 0)}
            except AzureError as exc:
                ctx.log.info("%s ingestion volume: %s", row.name, exc)
        return usage

    def _emit_estimates(self, ctx: RunContext, now: datetime, cursor: dict) -> None:
        """Re-send the attributable estimate every run, like the bill (arc_azure_cost)."""
        estimates = cursor.get(ESTIMATES) or {}
        for row in azure_resources.tracked(ctx.product):
            found = estimates.get(row.resource_id) or {}
            if found.get("amount") is None:
                continue
            value, basis = azure_resources.attribute(row, found["amount"])
            if value is None:
                continue
            ctx.emitter.metric(SERVICE_NAME, GaugePoint(
                "arc.azure.cost_estimate", value, now,
                {"azure.resource": row.name, "azure.resource_type": row.short_type,
                 "azure.service": service_name(row.type), "currency": found["currency"],
                 "azure.cost_basis": "dedicated" if basis == "dedicated" else "allocated",
                 "azure.estimate_method": found["method"]},
                description="Month-to-date list-price estimate attributed to the product (usage x Azure Retail Prices)"))

    def _emit_cost(self, ctx: RunContext, now: datetime) -> None:
        """Re-send the attributable cost every run so the Prometheus series stays current between pulls."""
        for row in azure_resources.tracked(ctx.product):
            value, basis = azure_resources.attributed_cost(row)
            if value is None:
                continue
            ctx.emitter.metric(SERVICE_NAME, GaugePoint(
                "arc.azure.cost", value, now,
                {"azure.resource": row.name, "azure.resource_type": row.short_type,
                 "azure.service": service_name(row.type), "currency": row.currency,
                 "azure.cost_basis": "dedicated" if basis == "dedicated" else "allocated", "azure.period": "mtd"},
                description="Month-to-date Azure cost attributed to the product"))
