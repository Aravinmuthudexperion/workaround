"""Azure platform connector: product mapping, ARM client parsing, and a full worker run against a fake Azure.

The scenario: one client resource group holding three products (warranty claims, dealer assist,
parts search). The client names only the warranty function app; ARC must find the rest.
"""
from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from arc_tower.connectors.plugins.app_insights import span_from_row
from arc_tower.connectors.plugins.azure_platform import METRIC_CATALOG, AzurePlatformConnector
from arc_tower.integrations.azure import AzureClient, AzureError, MetricValue
from arc_tower.integrations.azure.pricing import Meter
from arc_tower.otlp.codec import Signal
from arc_tower.services import azure_mapping, azure_resources
from arc_tower.services.azure_mapping import Inventory, MappingError, build_product_map
from arc_tower.utils.timeutil import utcnow

SUB = "11111111-2222-3333-4444-555555555555"
RG = f"/subscriptions/{SUB}/resourceGroups/hd-ai-prod-rg/providers"
VNET = f"/subscriptions/{SUB}/resourceGroups/hd-net-rg/providers/Microsoft.Network/virtualNetworks/hd-vnet"


def rid(provider: str, name: str) -> str:
    return f"{RG}/{provider}/{name}"


FUNC = rid("Microsoft.Web/sites", "func-warranty-processor")
PLAN = rid("Microsoft.Web/serverfarms", "asp-shared-plan")
OAI = rid("Microsoft.CognitiveServices/accounts", "oai-hd-shared")
DOCS = rid("Microsoft.Storage/storageAccounts", "sthdwarrantydocs")
SHARED_ST = rid("Microsoft.Storage/storageAccounts", "sthdshared")
COSMOS = rid("Microsoft.DocumentDB/databaseAccounts", "cosmos-hd-chat")
APPI = rid("Microsoft.Insights/components", "appi-warranty")
LAW = f"/subscriptions/{SUB}/resourceGroups/hd-ops-rg/providers/Microsoft.OperationalInsights/workspaces/law-hd-central"
WORKSPACE_GUID = "9f1b2c3d-0000-4000-8000-00000000abcd"


def _res(id_, type_, tags=None, principal=None, **props):
    return {"id": id_, "name": id_.rsplit("/", 1)[-1], "type": type_, "resourceGroup": "hd-ai-prod-rg",
            "subscriptionId": SUB, "location": "eastus", "tags": tags or {},
            "identity": {"principalId": principal} if principal else None, "properties": props}


RESOURCES = [
    _res(FUNC, "Microsoft.Web/sites", {"product": "warranty"}, "p-warranty", serverFarmId=PLAN,
         virtualNetworkSubnetId=f"{VNET}/subnets/apps", httpsOnly=True),
    _res(rid("Microsoft.Web/sites", "app-dealerassist-web"), "Microsoft.Web/sites", {"product": "dealerassist"},
         "p-dealer", serverFarmId=PLAN),
    _res(rid("Microsoft.App/containerApps", "ca-partsearch-api"), "Microsoft.App/containerApps",
         {"product": "parts"}, "p-parts"),
    _res(PLAN, "Microsoft.Web/serverfarms"),
    _res(OAI, "Microsoft.CognitiveServices/accounts", {"env": "prod"}, publicNetworkAccess="Enabled",
         disableLocalAuth=False),
    _res(DOCS, "Microsoft.Storage/storageAccounts", {"product": "warranty"}, allowBlobPublicAccess=True),
    _res(SHARED_ST, "Microsoft.Storage/storageAccounts", {"env": "prod"}, allowSharedKeyAccess=False),
    _res(rid("Microsoft.KeyVault/vaults", "kv-hd-ai"), "Microsoft.KeyVault/vaults", {"env": "prod"},
         accessPolicies=[{"objectId": "p-warranty"}, {"objectId": "p-parts"}]),
    _res(APPI, "Microsoft.Insights/components", {f"hidden-link:{FUNC}": "Resource"}, WorkspaceResourceId=LAW),
    _res(LAW, "Microsoft.OperationalInsights/workspaces", {"env": "prod"}, customerId=WORKSPACE_GUID),
    _res(rid("Microsoft.Storage/storageAccounts", "stwarrantyarchive"), "Microsoft.Storage/storageAccounts"),
    _res(COSMOS, "Microsoft.DocumentDB/databaseAccounts", {"product": "dealerassist"}),
    _res(rid("Microsoft.Network/privateEndpoints", "pe-oai"), "Microsoft.Network/privateEndpoints", {},
         subnet={"id": f"{VNET}/subnets/pe"},
         privateLinkServiceConnections=[{"properties": {"privateLinkServiceId": OAI}}]),
    _res(rid("Microsoft.Storage/storageAccounts", "sttemptest01"), "Microsoft.Storage/storageAccounts"),
]

# Facts the cost estimate reads: storage SKU / tier and the OpenAI account kind.
for _row in RESOURCES:
    if _row["id"] in (DOCS, SHARED_ST):
        _row.update(kind="StorageV2", sku={"name": "Standard_LRS", "tier": "Standard"})
        _row["properties"]["accessTier"] = "Hot"
    if _row["id"] == OAI:
        _row["kind"] = "OpenAI"

BLOB_CONTRIB = "/providers/Microsoft.Authorization/roleDefinitions/ba92f5b4-2d11-453d-a403-e96b0029c9fe"
OAI_USER = "/providers/Microsoft.Authorization/roleDefinitions/5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
ASSIGNMENTS = [
    {"principalId": "p-warranty", "scope": DOCS.lower(), "roleDefinitionId": BLOB_CONTRIB},
    {"principalId": "p-warranty", "scope": f"{SHARED_ST}/blobServices/default/containers/claims".lower(),
     "roleDefinitionId": BLOB_CONTRIB},
    {"principalId": "p-warranty", "scope": OAI.lower(), "roleDefinitionId": OAI_USER},
    {"principalId": "p-warranty", "scope": f"/subscriptions/{SUB}/resourcegroups/hd-ai-prod-rg",
     "roleDefinitionId": "reader"},                                      # resource-group wide: ignored
    {"principalId": "p-parts", "scope": SHARED_ST.lower(), "roleDefinitionId": BLOB_CONTRIB},
    {"principalId": "p-parts", "scope": OAI.lower(), "roleDefinitionId": OAI_USER},
    {"principalId": "p-dealer", "scope": COSMOS.lower(), "roleDefinitionId": "x"},
    {"principalId": "p-dealer", "scope": OAI.lower(), "roleDefinitionId": OAI_USER},
]


def _map(starting_point="func-warranty-processor", tag=None):
    return {c.resource.name: c for c in build_product_map(Inventory.from_graph(RESOURCES, ASSIGNMENTS),
                                                          starting_point, tag)}


# --- mapping -------------------------------------------------------------------------------

def test_map_finds_the_products_resources_from_one_starting_point():
    found = _map()
    assert next(iter(found)) == "func-warranty-processor" and found["func-warranty-processor"].is_anchor
    assert {n for n, c in found.items() if c.level == "high"} == {
        "func-warranty-processor", "sthdwarrantydocs", "sthdshared", "oai-hd-shared", "asp-shared-plan",
        "kv-hd-ai", "appi-warranty"}
    assert found["pe-oai"].level == "medium"                 # same VNet only
    assert found["stwarrantyarchive"].level == "low"         # name only: left for the reviewer
    for other in ("app-dealerassist-web", "ca-partsearch-api", "cosmos-hd-chat", "sttemptest01"):
        assert other not in found


def test_map_detects_shared_resources_and_explains_evidence():
    found = _map()
    assert found["sthdshared"].shared_with == ["ca-partsearch-api"]           # child-scope role assignment
    assert found["oai-hd-shared"].shared_with == ["app-dealerassist-web", "ca-partsearch-api"]
    assert found["asp-shared-plan"].shared_with == ["app-dealerassist-web"]
    assert found["kv-hd-ai"].shared_with == ["ca-partsearch-api"]
    assert not found["sthdwarrantydocs"].shared
    docs = " ".join(found["sthdwarrantydocs"].evidence)
    assert "Tagged product=warranty" in docs and "Storage Blob Data Contributor" in docs
    assert any("private endpoint" in e for e in found["oai-hd-shared"].evidence)


def test_map_flags_risks():
    found = _map()
    assert "Public network access enabled" in found["oai-hd-shared"].risks
    assert any("Key-based access" in r for r in found["oai-hd-shared"].risks)
    assert "Anonymous blob access allowed" in found["sthdwarrantydocs"].risks
    assert not any("Shared-key" in r for r in found["sthdshared"].risks)        # disabled on that account
    assert any("No tags" in r for r in found["asp-shared-plan"].risks)


def test_map_accepts_resource_id_and_explicit_tag():
    found = _map(FUNC.upper(), tag="product=dealerassist")
    assert "cosmos-hd-chat" in found and "Tagged product=dealerassist" in found["cosmos-hd-chat"].evidence


def test_map_rejects_unknown_starting_point_and_bad_scope():
    with pytest.raises(MappingError):
        _map("no-such-app")
    with pytest.raises(MappingError):
        azure_mapping.parse_subscriptions("not-a-guid")
    with pytest.raises(MappingError):
        azure_mapping.parse_resource_groups("rg1, x') | project secrets")     # no query injection
    assert "in~ ('rg-a', 'rg-b')" in azure_mapping.resources_query(azure_mapping.parse_resource_groups("rg-a,rg-b"))


# --- ARM client parsing --------------------------------------------------------------------

def _client(handler) -> AzureClient:
    return AzureClient(lambda: "token", httpx.Client(base_url="https://management.azure.com",
                                                     transport=httpx.MockTransport(handler)))


def test_client_pages_resource_graph_and_parses_metrics_and_cost():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"] == "Bearer token"
        if "ResourceGraph" in request.url.path:
            body = json.loads(request.content)
            if "$skipToken" not in body["options"]:
                return httpx.Response(200, json={"data": [{"id": "a"}], "$skipToken": "next"})
            return httpx.Response(200, json={"data": [{"id": "b"}]})
        if request.url.path.endswith("/metrics"):
            assert request.url.params["$filter"] == "ModelDeploymentName eq '*'"
            assert request.url.params["interval"] == "PT1M"
            return httpx.Response(200, json={"value": [{"timeseries": [
                {"metadatavalues": [{"name": {"value": "modeldeploymentname"}, "value": "gpt4o-mini"}],
                 "data": [{"timeStamp": "2026-10-12T10:00:00Z", "total": 100},
                          {"timeStamp": "2026-10-12T10:01:00Z", "total": 50},
                          {"timeStamp": "2026-10-12T10:02:00Z"}]},          # newest minute: not published yet
                {"metadatavalues": [{"name": {"value": "modeldeploymentname"}, "value": "gpt4o"}],
                 "data": [{"timeStamp": "2026-10-12T10:01:00Z", "total": 7}]}]}]})
        if "CostManagement" in request.url.path:
            if "page2" in str(request.url):
                return httpx.Response(200, json={"properties": {"columns": [
                    {"name": "PreTaxCost"}, {"name": "ResourceId"}, {"name": "Currency"}],
                    "rows": [[2.5, DOCS, "USD"]]}})
            return httpx.Response(200, json={"properties": {"columns": [
                {"name": "PreTaxCost"}, {"name": "ResourceId"}, {"name": "Currency"}],
                "rows": [[40, DOCS, "USD"], [12, OAI, "USD"]],
                "nextLink": "https://management.azure.com/x/providers/Microsoft.CostManagement/query?page2"}})
        return httpx.Response(404)

    client = _client(handler)
    assert [r["id"] for r in client.resource_graph("resources", [SUB])] == ["a", "b"]
    now = utcnow()
    values = client.metric(OAI, "GeneratedTokens", "Total", now - timedelta(minutes=5), now,
                           split_by="ModelDeploymentName")
    assert values == [MetricValue("gpt4o-mini", 150.0), MetricValue("gpt4o", 7.0)]          # summed span
    latest = client.metric(OAI, "GeneratedTokens", "Total", now - timedelta(minutes=5), now,
                           split_by="ModelDeploymentName", latest=True)
    assert [(v.dimension, v.value, v.at.minute) for v in latest] == [("gpt4o-mini", 50.0, 1), ("gpt4o", 7.0, 1)]
    costs = client.cost_by_resource(f"/subscriptions/{SUB}/resourceGroups/hd-ai-prod-rg")
    assert costs[DOCS.lower()] == (42.5, "USD") and costs[OAI.lower()] == (12.0, "USD")


def test_client_lists_metric_definitions_and_queries_log_analytics():
    def arm(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/providers/Microsoft.Insights/metricDefinitions")
        return httpx.Response(200, json={"value": [{"name": {"value": "OnDemandFunctionExecutionCount"}},
                                                   {"name": {"value": "CpuPercentage"}}]})

    def logs(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/workspaces/{WORKSPACE_GUID}/query"
        assert request.headers["authorization"] == "Bearer logs-token"
        body = json.loads(request.content)
        assert body["query"] == "AppDependencies | take 1" and "/" in body["timespan"]
        return httpx.Response(200, json={"tables": [{"name": "PrimaryResult",
                                                     "columns": [{"name": "Name"}, {"name": "DurationMs"}],
                                                     "rows": [["chat x", 812.5]]}]})

    client = AzureClient(lambda: "token", httpx.Client(base_url="https://management.azure.com",
                                                       transport=httpx.MockTransport(arm)),
                         logs_token_provider=lambda: "logs-token",
                         logs_http=httpx.Client(base_url="https://api.loganalytics.io",
                                                transport=httpx.MockTransport(logs)))
    assert client.metric_names(FUNC) == {"OnDemandFunctionExecutionCount", "CpuPercentage"}
    now = utcnow()
    assert client.logs_query(WORKSPACE_GUID, "AppDependencies | take 1", now - timedelta(hours=1), now) == [
        {"Name": "chat x", "DurationMs": 812.5}]


def test_app_insights_rows_become_spans_named_by_model():
    service, span = span_from_row({
        "TimeGenerated": "2026-10-15T06:00:00Z", "OperationId": "4bf92f3577b34da6a3ce929d0e0e4736",
        "Id": "00f067aa0ba902b7", "ParentId": "", "Name": "chat warranty-chat", "DurationMs": 640,
        "Success": "True", "AppRoleName": "warranty",
        "Properties": json.dumps({"gen_ai.operation.name": "chat", "gen_ai.request.model": "warranty-chat",
                                  "gen_ai.response.model": "gpt-4.1-mini-2025-04-14",
                                  "gen_ai.usage.input_tokens": "120", "gen_ai.usage.output_tokens": "30",
                                  "_MS.ProcessedByMetricExtractors": "x"})}, "fallback")
    assert service == "warranty" and span.error is None
    assert span.attributes == {"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4.1-mini",
                               "gen_ai.response.model": "gpt-4.1-mini-2025-04-14",
                               "azure.openai.deployment": "warranty-chat",
                               "gen_ai.usage.input_tokens": 120, "gen_ai.usage.output_tokens": 30}
    assert (span.end - span.start).total_seconds() == 0.64 and span.trace_id.hex().startswith("4bf92f35")
    assert span_from_row({"TimeGenerated": None}, "x") is None


def test_client_reports_permission_errors():
    client = _client(lambda r: httpx.Response(403, json={"error": {"message": "AuthorizationFailed"}}))
    with pytest.raises(AzureError) as exc:
        client.resource_graph("resources", [SUB])
    assert exc.value.status_code == 403 and "AuthorizationFailed" in str(exc.value)


# --- full worker run -----------------------------------------------------------------------

class FakeAzure:
    MINUTE = utcnow().replace(second=0, microsecond=0) - timedelta(minutes=2)

    # Log Analytics returns ingestion times with 100 ns precision (7 digits); Python keeps only 6.
    INGESTED = (utcnow() - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "7Z"
    SPAN_ROW = {"Ingested": INGESTED, "TimeGenerated": INGESTED[:19] + "Z",
                "OperationId": "4bf92f3577b34da6a3ce929d0e0e4736", "Id": "00f067aa0ba902b7", "ParentId": "",
                "Name": "chat claims-chat", "DurationMs": 900, "Success": True, "AppRoleName": "warranty-api",
                "Properties": json.dumps({"gen_ai.operation.name": "chat", "gen_ai.request.model": "claims-chat",
                                          "gen_ai.response.model": "gpt-4o-mini-2024-07-18",
                                          "gen_ai.usage.input_tokens": "200", "gen_ai.usage.output_tokens": "50"})}

    def __init__(self):
        self.metric_calls = 0
        self.graph_calls = 0
        self.metric_requests: list[tuple[str, str]] = []
        self.month_to_date: list[tuple[str, str]] = []
        self.log_queries: list[str] = []

    def metric_names(self, resource_id):
        return {name for metrics in METRIC_CATALOG.values() for name, _, _ in metrics}

    def resource(self, resource_id, api_version):
        raise AssertionError("the workspace is in the scanned inventory")

    def openai_deployments(self, account_id):
        assert account_id == OAI.lower()
        return {"gpt4o-mini": {"model": "gpt-4o-mini", "version": "2024-07-18", "sku": "GlobalStandard"}}

    def created_times(self, scope):
        return {}

    def logs_query(self, workspace_id, query, start, end):
        """Like KQL: rows ingested strictly after the datetime(...) in the query."""
        assert workspace_id == WORKSPACE_GUID and f"_ResourceId =~ '{APPI.lower()}'" in query
        self.log_queries.append(query)
        after = query.split("ingestion_time() > datetime(", 1)[1].split(")", 1)[0]
        return [self.SPAN_ROW] if self.INGESTED > after else []      # same format: text order is time order

    def resource_graph(self, query, subscriptions):
        assert subscriptions == [SUB]
        self.graph_calls += 1
        return ASSIGNMENTS if "authorizationresources" in query else RESOURCES

    def metric(self, resource_id, name, aggregation, start, end, interval="PT1M", split_by=None, latest=False):
        if interval == "PT1H":                       # month-to-date usage for the cost estimate
            self.month_to_date.append((resource_id, name))
            return {"Transactions": [MetricValue("PutBlob", 20_000), MetricValue("GetBlob", 50_000)],
                    "UsedCapacity": [MetricValue("", 0)],
                    "ProcessedPromptTokens": [MetricValue("gpt4o-mini", 2_000_000)],
                    "GeneratedTokens": [MetricValue("gpt4o-mini", 500_000)]}.get(name, [])
        assert interval == "PT1M" and latest
        self.metric_calls += 1
        self.metric_requests.append((resource_id, name))
        if name == "Http5xx":
            raise AzureError("metric not supported", 400)
        return [MetricValue("gpt4o-mini" if split_by else "", 10.0, self.MINUTE)]

    def cost_by_resource(self, scope):
        return {DOCS.lower(): (42.0, "USD"), SHARED_ST.lower(): (60.0, "USD"), OAI.lower(): (250.0, "USD"),
                PLAN.lower(): (300.0, "USD")}

    def close(self):
        pass


class FakePrices:
    """A tiny price list in USD: storage operations and GPT-4o mini tokens (per 1K)."""
    METERS = [
        Meter("Storage", "General Block Blob v2", "Hot LRS", "Hot LRS Write Operations", "eastus", "10K", ((0.0, 0.05),)),
        Meter("Storage", "General Block Blob v2", "Hot LRS", "Hot Read Operations", "eastus", "10K", ((0.0, 0.004),)),
        Meter("Storage", "General Block Blob v2", "Hot LRS", "All Other Operations", "eastus", "10K", ((0.0, 0.004),)),
        Meter("Storage", "General Block Blob v2", "Hot LRS", "Hot LRS Data Stored", "eastus", "1 GB/Month",
              ((0.0, 0.02),)),
        Meter("Foundry Models", "Azure OpenAI", "gpt 4o mini Inp glbl", "gpt 4o mini Inp glbl Tokens", "eastus", "1K",
              ((0.0, 0.00015),)),
        Meter("Foundry Models", "Azure OpenAI", "gpt 4o mini Outp glbl", "gpt 4o mini Outp glbl Tokens", "eastus",
              "1K", ((0.0, 0.0006),)),
    ]

    def __init__(self):
        self.currencies = set()

    def meters(self, currency, service="", product="", region="", sku=""):
        self.currencies.add(currency)
        return [m for m in self.METERS if (not service or m.service == service) and
                (not product or m.product == product) and (not region or m.region == region) and
                (not sku or m.sku == sku)]


@pytest.fixture(autouse=True)
def _no_real_price_list(monkeypatch):
    monkeypatch.setattr(AzurePlatformConnector, "_prices", FakePrices())


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, signal, request, headers):
        self.sent.append((signal, request, headers))


def _gauges(sender: FakeSender) -> dict[str, list]:
    out: dict[str, list] = {}
    for signal, request, _ in sender.sent:
        if signal is Signal.METRICS:
            for rm in request.resource_metrics:
                for sm in rm.scope_metrics:
                    for metric in sm.metrics:
                        out.setdefault(metric.name, []).extend(metric.gauge.data_points)
    return out


def test_worker_run_maps_pulls_metrics_cost_and_keeps_decisions(monkeypatch):
    from arc_tower.connectors import worker
    from arc_tower.db.models import Connector, Product
    from arc_tower.db.session import session_scope
    from arc_tower.services import connectors, products, tenants

    fake = FakeAzure()
    monkeypatch.setattr(AzurePlatformConnector, "client_factory", staticmethod(lambda *a: fake))
    with session_scope() as db:
        tenant = tenants.create_tenant(db, "test", "Client Test", "client-test")
        product = products.create_product(db, "test", tenant, "Warranty Claims AI", "azure_platform")
        connector = connectors.configure(db, "test", product, {
            "subscription_ids": SUB, "resource_groups": "hd-ai-prod-rg",
            "starting_point": "func-warranty-processor", "client_id": "app", "tenant_id": "t",
            "client_secret": "s3cret"})
        product_id, connector_id = product.id, connector.id

    sender = FakeSender()
    worker.run_connector(connector_id, sender)
    with session_scope() as db:
        connector = db.get(Connector, connector_id)
        assert connector.last_status == "ok", connector.last_message
        assert connector.interval_seconds == 60                                 # metrics every minute
        assert "scanned: 9 resources mapped (7 high, 1 medium, 1 low" in connector.last_message
        assert "(1 min), 1 unavailable" in connector.last_message and "cost updated" in connector.last_message
        assert "1 spans from 1 App Insights" in connector.last_message
        assert "estimate 0.84 USD month to date" in connector.last_message   # 0.60 OpenAI + 2 x 0.12 storage
        product = db.get(Product, product_id)
        rows = {r.name: r for r in product.azure_resources}
        assert rows["func-warranty-processor"].status == "confirmed"           # the client's own pick
        assert rows["sthdshared"].status == "proposed" and rows["sthdshared"].cost_mtd == 60.0
        assert rows["oai-hd-shared"].metrics["GeneratedTokens"] == {"gpt4o-mini": 10.0}
        assert product.status == "live"
    gauges = _gauges(sender)
    assert {p.time_unix_nano for p in gauges["arc.azure.metric"]} == {int(FakeAzure.MINUTE.timestamp()) * 10**9}
    labels = [{kv.key: kv.value.string_value for kv in p.attributes} for p in gauges["arc.azure.metric"]]
    assert {"azure.resource": "oai-hd-shared", "azure.metric": "GeneratedTokens"}.items() <= next(
        lb for lb in labels if lb["azure.metric"] == "GeneratedTokens").items()
    assert all(lb["arc.tenant.id"] == "client-test" for lb in labels)            # tenant stamped
    spans = [(rs.resource, span) for signal, request, _ in sender.sent if signal is Signal.TRACES
             for rs in request.resource_spans for ss in rs.scope_spans for span in ss.spans]
    assert len(spans) == 1
    resource, span = spans[0]
    attrs = {kv.key: kv.value for kv in span.attributes}
    assert attrs["gen_ai.request.model"].string_value == "gpt-4o-mini"            # priced model, not deployment
    assert attrs["azure.openai.deployment"].string_value == "claims-chat"
    assert attrs["gen_ai.usage.input_tokens"].int_value == 200
    assert {kv.key: kv.value.string_value for kv in resource.attributes}["service.name"] == "warranty-api"
    call_logs = [record for signal, request, _ in sender.sent if signal is Signal.LOGS
                 for rl in request.resource_logs for sl in rl.scope_logs for record in sl.log_records]
    assert len(call_logs) == 1                                                    # one line per model call
    log_attrs = {kv.key: kv.value for kv in call_logs[0].attributes}
    assert log_attrs["event.name"].string_value == "gen_ai.call"
    assert log_attrs["gen_ai.usage.output_tokens"].int_value == 50
    assert call_logs[0].trace_id == span.trace_id and "gpt-4o-mini (claims-chat)" in call_logs[0].body.string_value
    service_labels = {kv.value.string_value for p in gauges["arc.azure.metric"] for kv in p.attributes
                      if kv.key == "azure.service"}
    assert "Azure OpenAI / AI Services" in service_labels and "Storage" in service_labels
    cost_resources = {kv.value.string_value for p in gauges["arc.azure.cost"] for kv in p.attributes
                      if kv.key == "azure.resource"}
    assert cost_resources == {"func-warranty-processor"}                         # only the confirmed starting point

    graph_calls, metric_calls = fake.graph_calls, fake.metric_calls
    worker.run_connector(connector_id, FakeSender())                             # next minute: metrics only
    assert fake.graph_calls == graph_calls and fake.metric_calls > metric_calls
    with session_scope() as db:
        message = db.get(Connector, connector_id).last_message
        assert "9 resources tracked (scanned" in message and "0 spans from 1 App Insights" in message
    assert f"datetime({FakeAzure.INGESTED})" in fake.log_queries[-1]    # read on from the exact last row, not again

    with session_scope() as db:
        product = db.get(Product, product_id)
        rows = {r.name: r for r in product.azure_resources}
        azure_resources.decide(db, "lead", product, rows["sthdwarrantydocs"].id, "confirmed")
        azure_resources.decide(db, "lead", product, rows["sthdshared"].id, "confirmed", 60)
        azure_resources.decide(db, "lead", product, rows["stwarrantyarchive"].id, "rejected")
        connectors.request_run(db.get(Connector, connector_id))

    sender = FakeSender()
    worker.run_connector(connector_id, sender)                 # "Run now" forced a rescan: decisions survive
    assert fake.graph_calls == graph_calls + 2
    with session_scope() as db:
        product = db.get(Product, product_id)
        rows = {r.name: r for r in product.azure_resources}
        assert rows["sthdshared"].status == "confirmed" and rows["sthdshared"].share_pct == 60
        assert rows["stwarrantyarchive"].status == "rejected"
        cost = azure_resources.cost_summary(product)
        assert cost.dedicated == 42.0 and cost.allocated == 36.0 and cost.total == 78.0
        assert cost.awaiting_review == 550.0                                     # plan + OpenAI still proposed
        assert "cost refreshed" in db.get(Connector, connector_id).last_message  # no second cost pull
    costs = {(kv := {a.key: a.value.string_value for a in p.attributes})["azure.resource"]: (p.as_double, kv)
             for p in _gauges(sender)["arc.azure.cost"]}
    assert costs["sthdshared"][0] == 36.0 and costs["sthdshared"][1]["azure.cost_basis"] == "allocated"
    # The live estimate is attributed like the bill: 2M in x $0.15/M + 0.5M out x $0.6/M = $0.60 for OpenAI
    # (still proposed: not sent); storage 2 x $0.05 write + 5 x $0.004 read = $0.12, 60 % for the shared one.
    estimates = {(kv := {a.key: a.value.string_value for a in p.attributes})["azure.resource"]: (p.as_double, kv)
                 for p in _gauges(sender)["arc.azure.cost_estimate"]}
    assert estimates["sthdwarrantydocs"][0] == pytest.approx(0.12) and estimates["sthdshared"][0] == pytest.approx(0.072)
    assert estimates["sthdwarrantydocs"][1]["azure.estimate_method"] == "operations"
    assert estimates["func-warranty-processor"] == (0.0, estimates["func-warranty-processor"][1])  # billed on its plan
    assert "oai-hd-shared" not in estimates
    with session_scope() as db:
        saved = db.get(Connector, connector_id).cursor["estimates"][OAI.lower()]
        assert saved["amount"] == pytest.approx(0.6) and saved["currency"] == "USD"
        assert "gpt4o-mini (gpt-4o-mini): 2,000,000 in / 500,000 out tokens" in saved["detail"]


def test_review_screen_renders_and_records_decisions(monkeypatch):
    from fastapi.testclient import TestClient

    from arc_tower.connectors import worker
    from arc_tower.db.session import session_scope
    from arc_tower.services import connectors, products, tenants, users
    from arc_tower.web.app import app

    monkeypatch.setattr(AzurePlatformConnector, "client_factory", staticmethod(lambda *a: FakeAzure()))
    with session_scope() as db:
        tenant = tenants.create_tenant(db, "test", "Review Co", "review-co")
        product = products.create_product(db, "test", tenant, "Parts Search", "azure_platform")
        connector = connectors.configure(db, "test", product, {
            "subscription_ids": SUB, "starting_point": "ca-partsearch-api"})
        users.create_user(db, "test", "lead@review.test", password="pw-123456", is_platform_admin=True)
        product_id, connector_id = product.id, connector.id
    worker.run_connector(connector_id, FakeSender())

    web = TestClient(app)
    assert web.post("/login", data={"email": "lead@review.test", "password": "pw-123456"},
                    follow_redirects=False).status_code == 303
    for url in (f"/products/{product_id}", f"/products/{product_id}/onboard/3"):
        page = web.get(url)
        assert page.status_code == 200 and "Azure resources" in page.text and "sthdshared" in page.text
    with session_scope() as db:
        from arc_tower.db.models import Product
        row = next(r for r in db.get(Product, product_id).azure_resources if r.name == "sthdshared")
    csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    resp = web.post(f"/products/{product_id}/azure-resources/{row.id}",
                    data={"csrf_token": csrf, "status": "confirmed", "share_pct": "40",
                          "back": f"/products/{product_id}/onboard/3"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].endswith("/onboard/3")
    with session_scope() as db:
        from arc_tower.db.models import ProductResource
        saved = db.get(ProductResource, row.id)
        assert saved.status == "confirmed" and saved.share_pct == 40


def test_worker_asks_each_resource_only_for_metrics_it_publishes(monkeypatch):
    """A Flex Consumption function app publishes execution counts, not Requests / Http5xx."""
    from arc_tower.connectors import worker
    from arc_tower.db.models import Connector
    from arc_tower.db.session import session_scope
    from arc_tower.services import connectors, products, tenants

    class FlexAzure(FakeAzure):
        def metric_names(self, resource_id):
            if resource_id == FUNC.lower():
                return {"OnDemandFunctionExecutionCount", "CpuPercentage", "MemoryWorkingSet"}
            if resource_id == OAI.lower():
                raise AzureError("forbidden", 403)                  # unreadable: falls back to the catalog
            return super().metric_names(resource_id)

    fake = FlexAzure()
    monkeypatch.setattr(AzurePlatformConnector, "client_factory", staticmethod(lambda *a: fake))
    with session_scope() as db:
        tenant = tenants.create_tenant(db, "test", "Flex Co", "flex-co")
        product = products.create_product(db, "test", tenant, "Warranty Flex", "azure_platform")
        connector_id = connectors.configure(db, "test", product, {
            "subscription_ids": SUB, "starting_point": "func-warranty-processor"}).id
    worker.run_connector(connector_id, FakeSender())
    asked = [name for rid_, name in fake.metric_requests if rid_ == FUNC.lower()]
    assert asked == ["OnDemandFunctionExecutionCount"]
    assert {name for rid_, name in fake.metric_requests if rid_ == OAI.lower()} == {
        "ProcessedPromptTokens", "GeneratedTokens", "AzureOpenAIRequests"}
    with session_scope() as db:
        message = db.get(Connector, connector_id).last_message
        assert "unavailable" not in message, message
