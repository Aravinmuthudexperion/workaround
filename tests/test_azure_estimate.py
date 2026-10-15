"""List-price cost estimates: Retail Prices parsing, tiers (free grants) and the per-service rules."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from arc_tower.integrations.azure.pricing import Meter, RetailPrices, unit_size
from arc_tower.services import azure_estimate as est

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
PERIOD = est.Period(datetime(2026, 10, 1, tzinfo=timezone.utc), NOW)


def meter(name, unit, *tiers, product="p", sku="s", service="svc"):
    return Meter(service, product, sku, name, "centralindia", unit, tuple(tiers))


def lookup(*meters):
    def find(service="", product="", region="", sku=""):
        return [m for m in meters if (not service or m.service == service) and (not product or m.product == product)
                and (not sku or m.sku == sku)]
    return find


def test_units_and_tiers_apply_free_grants():
    assert [unit_size(u) for u in ("1K", "10K", "1M", "10", "1 GB Second", "1 Hour", "100")] == [
        1e3, 1e4, 1e6, 10, 1, 1, 100]
    ingestion = meter("Analytics Logs Data Ingestion", "1 GB", (0.0, 0.0), (5.0, 309.0859))
    assert ingestion.cost(3) == 0 and ingestion.cost(7) == pytest.approx(2 * 309.0859)
    assert ingestion.free_units == 5
    executions = meter("On Demand Total Executions", "10", (0.0, 0.0), (25000.0, 0.0004))
    assert executions.free_units == 250_000                                  # 25,000 blocks of 10
    assert executions.cost(250_010) == pytest.approx(0.0004)


def test_flex_function_uses_gb_seconds_and_free_grant():
    flex = lookup(meter("On Demand Execution Time", "1 GB Second", (0.0, 0.0), (100000.0, 0.0036),
                        service="Functions", product="Flex Consumption", sku="On Demand"),
                  meter("On Demand Total Executions", "10", (0.0, 0.0), (25000.0, 0.0004),
                        service="Functions", product="Flex Consumption", sku="On Demand"))
    facts = {"type": est.SITE, "location": "centralindia", "plan": {"tier": "FlexConsumption"}}
    assert [n.metric for n in est.needs(facts)] == ["OnDemandFunctionExecutionUnits", "OnDemandFunctionExecutionCount"]
    live = est.estimate(facts, {"OnDemandFunctionExecutionUnits": {"": 277_094_400},     # ResumeX, 15 Oct
                                "OnDemandFunctionExecutionCount": {"": 44}}, flex, PERIOD)
    assert live.amount == 0 and "44 executions, 270.6 GB-s" in live.detail
    busy = est.estimate(facts, {"OnDemandFunctionExecutionUnits": {"": 200_000 * 1_024_000},
                                "OnDemandFunctionExecutionCount": {"": 0}}, flex, PERIOD)
    assert busy.amount == pytest.approx(100_000 * 0.0036)
    web = est.estimate({"type": est.SITE, "plan": {"tier": "Basic"}}, {}, flex, PERIOD)
    assert web.amount == 0 and web.method == "billed elsewhere"


def test_openai_picks_standard_token_meters_for_the_deployment_type():
    catalog = [meter(n, "1K", (0.0, p), product="Azure OpenAI") for n, p in (
        ("gpt 4.1 mini Batch Inp glbl Tokens", 0.0192), ("gpt 4.1 mini cached Inp regnl Tokens", 0.0106),
        ("gpt-4.1-mini-ft input regional Tokens", 0.0422), ("gpt 4.1 mini Inp regnl Tokens", 0.0422),
        ("gpt 4.1 mini Inp glbl Tokens", 0.0384), ("gpt 4.1 mini Outp glbl Tokens", 0.1536),
        ("gpt 4.1 Inp glbl Tokens", 0.192), ("gpt 4.1 mini Inp Data Zone Tokens", 0.0422))]
    inp, outp = est.openai_meters(catalog, "gpt-4.1-mini", "GlobalStandard")
    assert (inp.name, outp.name) == ("gpt 4.1 mini Inp glbl Tokens", "gpt 4.1 mini Outp glbl Tokens")
    assert est.openai_meters(catalog, "gpt-4.1-mini", "Standard")[0].name == "gpt 4.1 mini Inp regnl Tokens"
    assert est.openai_meters(catalog, "gpt-4.1-mini", "DataZoneStandard")[0].name == "gpt 4.1 mini Inp Data Zone Tokens"
    facts = {"type": est.OPENAI, "location": "eastus2",
             "deployments": {"resumex-chat": {"model": "gpt-4.1-mini", "sku": "GlobalStandard"},
                             "legacy": {"model": "text-davinci-003", "sku": "Standard"}}}
    result = est.estimate(facts, {"ProcessedPromptTokens": {"resumex-chat": 9178, "legacy": 10},
                                  "GeneratedTokens": {"resumex-chat": 6488}}, lambda **_: catalog, PERIOD)
    assert result.amount == pytest.approx(9.178 * 0.0384 + 6.488 * 0.1536)
    assert "resumex-chat (gpt-4.1-mini): 9,178 in / 6,488 out tokens" in result.detail
    assert "no list price for text-davinci-003" in result.detail


def test_storage_prices_operations_by_class_and_capacity():
    assert [est.storage_operation_class(a) for a in (
        "PutBlob", "PutBlockList", "GetBlob", "GetBlobProperties", "ListBlobs", "CreateContainer", "DeleteBlob",
        "BlobPreflightRequest")] == ["write", "write", "read", "other", "list", "list", "free", "other"]
    catalog = lookup(*[meter(n, u, (0.0, p), service="Storage", product="General Block Blob v2", sku="Hot LRS")
                       for n, u, p in (("Hot LRS Write Operations", "10K", 5.2794), ("Hot Read Operations", "10K", 0.4224),
                                       ("All Other Operations", "10K", 0.4224),
                                       ("LRS List and Create Container Operations", "10K", 5.2794),
                                       ("Hot LRS Data Stored", "1 GB/Month", 1.9198))])
    facts = {"type": est.STORAGE, "kind": "StorageV2", "sku": {"name": "Standard_LRS"}, "access_tier": "Hot"}
    result = est.estimate(facts, {"Transactions": {"PutBlob": 10_000, "GetBlob": 20_000, "ListBlobs": 10_000,
                                                   "DeleteBlob": 99_999},
                                  "UsedCapacity": {"": 31 * 1024 ** 3}}, catalog, PERIOD)
    stored = 31 * 1.9198 * PERIOD.month_fraction                              # 31 GB for half a month
    assert result.amount == pytest.approx(5.2794 + 2 * 0.4224 + 5.2794 + stored)
    assert "10,000 write, 20,000 read, 10,000 list, 0 other operations; 31.00 GB stored" in result.detail
    premium = est.estimate({"type": est.STORAGE, "kind": "BlockBlobStorage", "sku": {"name": "Premium_LRS"}},
                           {}, catalog, PERIOD)
    assert premium.amount is None


def test_app_service_plan_by_hours_and_free_or_execution_plans():
    plans = lookup(meter("B1", "1 Hour", (0.0, 1.7278), service="Azure App Service", sku="B1",
                         product="Azure App Service Basic Plan - Linux"),
                   meter("B1", "1 Hour", (0.0, 2.1), service="Azure App Service", sku="B1",
                         product="Azure App Service Basic Plan"))
    created = (NOW - timedelta(hours=10)).isoformat()
    linux = est.estimate({"type": est.PLAN, "sku": {"name": "B1", "tier": "Basic", "capacity": 2}, "reserved": True,
                          "created": created}, {}, plans, PERIOD)
    assert linux.amount == pytest.approx(10 * 2 * 1.7278) and linux.method == "hours"
    windows = est.estimate({"type": est.PLAN, "sku": {"name": "B1", "tier": "Basic"}, "created": created},
                           {}, plans, PERIOD)
    assert windows.amount == pytest.approx(10 * 2.1)
    assert est.estimate({"type": est.PLAN, "sku": {"name": "F1", "tier": "Free"}}, {}, plans, PERIOD).amount == 0
    assert est.estimate({"type": est.PLAN, "sku": {"name": "FC1", "tier": "FlexConsumption"}}, {}, plans,
                        PERIOD).method == "billed elsewhere"


def test_workspace_ingestion_vault_cosmos_and_unknown_types():
    la = lookup(meter("Analytics Logs Data Ingestion", "1 GB", (0.0, 0.0), (5.0, 309.0859), service="Log Analytics"))
    workspace = {"type": est.WORKSPACE, "pricing_tier": "PerGB2018"}
    assert est.estimate(workspace, {est.INGESTION_GB: {"": 0.42}}, la, PERIOD).amount == 0
    assert est.estimate(workspace, {}, la, PERIOD).amount is None                 # volume unreadable
    assert "Billed on Log Analytics workspace law-x" in est.estimate(
        {"type": est.COMPONENT, "workspace": "law-x"}, {}, la, PERIOD).detail
    vault = lookup(meter("Operations", "10K", (0.0, 2.8797), service="Key Vault", product="Key Vault", sku="Standard"))
    assert est.estimate({"type": est.VAULT, "vault_sku": "standard"}, {"ServiceApiHit": {"": 30_000}}, vault,
                        PERIOD).amount == pytest.approx(3 * 2.8797)
    cosmos = lookup(meter("1M RUs", "1M", (0.0, 27.357), service="Azure Cosmos DB", product="Azure Cosmos DB serverless"))
    assert est.estimate({"type": est.COSMOS, "serverless": True}, {"TotalRequestUnits": {"": 2_000_000}}, cosmos,
                        PERIOD).amount == pytest.approx(2 * 27.357)
    assert est.estimate({"type": est.COSMOS}, {}, cosmos, PERIOD).amount is None   # provisioned throughput
    assert est.estimate({"type": "microsoft.network/virtualnetworks"}, {}, cosmos, PERIOD).amount is None


def test_retail_prices_pages_groups_tiers_and_caches():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        row = {"serviceName": "Log Analytics", "productName": "Log Analytics", "skuName": "Analytics Logs",
               "meterName": "Analytics Logs Data Ingestion", "armRegionName": "centralindia", "unitOfMeasure": "1 GB",
               "type": "Consumption"}
        if "page2" in str(request.url):
            return httpx.Response(200, json={"Items": [{**row, "tierMinimumUnits": 5.0, "retailPrice": 309.0859}]})
        assert request.url.params["currencyCode"] == "INR"
        assert request.url.params["$filter"] == ("type eq 'Consumption' and serviceName eq 'Log Analytics' and "
                                                 "armRegionName eq 'centralindia'")
        return httpx.Response(200, json={"Items": [{**row, "tierMinimumUnits": 0.0, "retailPrice": 0.0}],
                                         "NextPageLink": "https://prices.azure.com/api/retail/prices?page2"})

    prices = RetailPrices(httpx.Client(base_url="https://prices.azure.com", transport=httpx.MockTransport(handler)))
    found = prices.meters("inr", service="Log Analytics", region="centralindia")
    assert len(found) == 1 and found[0].tiers == ((0.0, 0.0), (5.0, 309.0859))
    prices.meters("INR", service="Log Analytics", region="centralindia")
    assert len(calls) == 2                                                   # second call served from the cache
