"""Azure platform connector: product mapping, ARM client parsing, and a full worker run against a fake Azure.

The scenario: one client resource group holding three products (warranty claims, dealer assist,
parts search). The client names only the warranty function app; ARC must find the rest.
"""
from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from arc_tower.connectors.plugins.azure_platform import AzurePlatformConnector
from arc_tower.integrations.azure import AzureClient, AzureError, MetricValue
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
    _res(rid("Microsoft.Insights/components", "appi-warranty"), "Microsoft.Insights/components",
         {f"hidden-link:{FUNC}": "Resource"}),
    _res(rid("Microsoft.Storage/storageAccounts", "stwarrantyarchive"), "Microsoft.Storage/storageAccounts"),
    _res(COSMOS, "Microsoft.DocumentDB/databaseAccounts", {"product": "dealerassist"}),
    _res(rid("Microsoft.Network/privateEndpoints", "pe-oai"), "Microsoft.Network/privateEndpoints", {},
         subnet={"id": f"{VNET}/subnets/pe"},
         privateLinkServiceConnections=[{"properties": {"privateLinkServiceId": OAI}}]),
    _res(rid("Microsoft.Storage/storageAccounts", "sttemptest01"), "Microsoft.Storage/storageAccounts"),
]

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


def test_client_reports_permission_errors():
    client = _client(lambda r: httpx.Response(403, json={"error": {"message": "AuthorizationFailed"}}))
    with pytest.raises(AzureError) as exc:
        client.resource_graph("resources", [SUB])
    assert exc.value.status_code == 403 and "AuthorizationFailed" in str(exc.value)


# --- full worker run -----------------------------------------------------------------------

class FakeAzure:
    MINUTE = utcnow().replace(second=0, microsecond=0) - timedelta(minutes=2)

    def __init__(self):
        self.metric_calls = 0
        self.graph_calls = 0

    def resource_graph(self, query, subscriptions):
        assert subscriptions == [SUB]
        self.graph_calls += 1
        return ASSIGNMENTS if "authorizationresources" in query else RESOURCES

    def metric(self, resource_id, name, aggregation, start, end, interval="PT1M", split_by=None, latest=False):
        assert interval == "PT1M" and latest
        self.metric_calls += 1
        if name == "Http5xx":
            raise AzureError("metric not supported", 400)
        return [MetricValue("gpt4o-mini" if split_by else "", 10.0, self.MINUTE)]

    def cost_by_resource(self, scope):
        return {DOCS.lower(): (42.0, "USD"), SHARED_ST.lower(): (60.0, "USD"), OAI.lower(): (250.0, "USD"),
                PLAN.lower(): (300.0, "USD")}

    def close(self):
        pass


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
    cost_resources = {kv.value.string_value for p in gauges["arc.azure.cost"] for kv in p.attributes
                      if kv.key == "azure.resource"}
    assert cost_resources == {"func-warranty-processor"}                         # only the confirmed starting point

    graph_calls, metric_calls = fake.graph_calls, fake.metric_calls
    worker.run_connector(connector_id, FakeSender())                             # next minute: metrics only
    assert fake.graph_calls == graph_calls and fake.metric_calls > metric_calls
    with session_scope() as db:
        assert "9 resources tracked (scanned" in db.get(Connector, connector_id).last_message

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
