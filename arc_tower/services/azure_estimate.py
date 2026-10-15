"""List-price cost estimate per Azure resource: month-to-date usage x Azure Retail Prices.

Live, unlike the bill (Cost Management publishes 8-24 h late), and the bill stays the reference:
- list prices: negotiated discounts, reservations and savings plans are not known here;
- free grants (the price tiers at 0, e.g. 5 GB of Log Analytics ingestion a month) are applied per
  resource, while Azure applies most of them once per subscription or billing account;
- each service is estimated from its main meters only (noted per service below).
Pure: facts, usage and prices are passed in (see connectors/plugins/azure_platform.py).
"""
from __future__ import annotations

import calendar
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from arc_tower.integrations.azure.pricing import Meter

MeterLookup = Callable[..., list[Meter]]          # meters(service=, product=, region=, sku=)
Usage = dict[str, dict[str, float]]               # metric -> {dimension value or "": month-to-date amount}

OPENAI = "microsoft.cognitiveservices/accounts"
SITE = "microsoft.web/sites"
PLAN = "microsoft.web/serverfarms"
STORAGE = "microsoft.storage/storageaccounts"
VAULT = "microsoft.keyvault/vaults"
WORKSPACE = "microsoft.operationalinsights/workspaces"
COMPONENT = "microsoft.insights/components"
COSMOS = "microsoft.documentdb/databaseaccounts"

INGESTION_GB = "ingestion_gb"                     # usage key filled from the workspace's Usage table


@dataclass(frozen=True)
class UsageNeed:
    """A month-to-date Azure Monitor figure the estimate needs."""
    metric: str
    aggregation: str = "Total"
    split_by: str | None = None
    latest: bool = False          # the latest hourly value (e.g. stored capacity) instead of the month's total


@dataclass
class Estimate:
    amount: float | None          # None: not estimated (detail says why)
    method: str                   # short category for dashboards
    detail: str                   # one sentence for people


@dataclass(frozen=True)
class Period:
    start: datetime               # first instant of the month (UTC)
    now: datetime

    @property
    def month_fraction(self) -> float:
        days = calendar.monthrange(self.start.year, self.start.month)[1]
        return (self.now - self.start).total_seconds() / (days * 86400)

    def hours_since(self, created: datetime | None) -> float:
        begin = max(self.start, created) if created else self.start
        return max((self.now - begin).total_seconds() / 3600, 0.0)


def _none(reason: str) -> Estimate:
    return Estimate(None, "not estimated", reason)


def _find(meters: list[Meter], name: str) -> Meter | None:
    return next((m for m in meters if m.name.lower() == name.lower()), None)


def _total(usage: Usage, metric: str) -> float:
    return sum((usage.get(metric) or {}).values())


# --- what each resource needs ----------------------------------------------------------------

def needs(facts: dict) -> list[UsageNeed]:
    rtype = facts.get("type", "")
    if rtype == OPENAI:
        return [UsageNeed("ProcessedPromptTokens", split_by="ModelDeploymentName"),
                UsageNeed("GeneratedTokens", split_by="ModelDeploymentName")]
    if rtype == SITE:
        tier = (facts.get("plan") or {}).get("tier", "").lower()
        if tier == "flexconsumption":
            return [UsageNeed("OnDemandFunctionExecutionUnits"), UsageNeed("OnDemandFunctionExecutionCount")]
        if tier == "dynamic":
            return [UsageNeed("FunctionExecutionUnits"), UsageNeed("FunctionExecutionCount")]
        return []
    if rtype == STORAGE:
        return [UsageNeed("Transactions", split_by="ApiName"), UsageNeed("UsedCapacity", "Average", latest=True)]
    if rtype == VAULT:
        return [UsageNeed("ServiceApiHit", "Count")]
    if rtype == COSMOS and facts.get("serverless"):
        return [UsageNeed("TotalRequestUnits")]
    return []


# --- the estimates ----------------------------------------------------------------------------

def estimate(facts: dict, usage: Usage, meters: MeterLookup, period: Period) -> Estimate:
    recipe = _RECIPES.get(facts.get("type", ""))
    if recipe is None:
        return _none("No price rules for this service type yet")
    return recipe(facts, usage, meters, period)


_DEPLOYMENT_TYPES = {"globalstandard": ("glbl", "global"), "standard": ("regnl", "regional"),
                     "datazonestandard": ("data zone", "dzone")}
_NOT_STANDARD_CALLS = ("batch", "cached", "cchd", "-ft", " ft", "training", "grdr")


def openai_meters(catalog: list[Meter], model: str, sku: str) -> tuple[Meter | None, Meter | None]:
    """Input and output token meters of a model for a deployment type,
    e.g. gpt-4.1-mini + GlobalStandard -> "gpt 4.1 mini Inp glbl Tokens" / "gpt 4.1 mini Outp glbl Tokens"."""
    words = model.lower().replace("-", " ")
    types = _DEPLOYMENT_TYPES.get(sku.lower(), _DEPLOYMENT_TYPES["globalstandard"])

    def pick(direction: str) -> Meter | None:
        for meter in catalog:
            name = meter.name.lower()
            if (name.startswith(f"{words} {direction} ") and any(t in name for t in types)
                    and not any(x in name for x in _NOT_STANDARD_CALLS)):
                return meter
        return None

    return pick("inp"), pick("outp")


def _openai(facts: dict, usage: Usage, meters: MeterLookup, period: Period) -> Estimate:
    """Tokens per deployment x the model's token prices for the deployment type. Cached input,
    batch and fine-tuning are priced differently and not split out here."""
    prompt, generated = usage.get("ProcessedPromptTokens") or {}, usage.get("GeneratedTokens") or {}
    deployments = facts.get("deployments") or {}
    names = sorted(set(prompt) | set(generated))
    if not names:
        return Estimate(0.0, "tokens", "No tokens this month")
    catalog = meters(product="Azure OpenAI", region=facts.get("location", ""))
    total, parts, missing = 0.0, [], []
    for name in names:
        deployment = deployments.get(name) or {}
        model = deployment.get("model") or name
        inp, outp = openai_meters(catalog, model, deployment.get("sku", ""))
        if not (inp and outp):
            missing.append(f"{model} ({deployment.get('sku') or 'unknown type'})")
            continue
        tokens_in, tokens_out = prompt.get(name, 0.0), generated.get(name, 0.0)
        total += inp.cost(tokens_in) + outp.cost(tokens_out)
        parts.append(f"{name} ({model}): {tokens_in:,.0f} in / {tokens_out:,.0f} out tokens")
    if not parts:
        return _none(f"No list price found for {', '.join(missing)}")
    note = f"; no list price for {', '.join(missing)}" if missing else ""
    return Estimate(total, "tokens", "; ".join(parts) + note)


def _site(facts: dict, usage: Usage, meters: MeterLookup, period: Period) -> Estimate:
    """Function apps on Flex Consumption / Consumption: GB-seconds and executions. Web apps and
    functions on App Service plans are billed on the plan."""
    tier = (facts.get("plan") or {}).get("tier", "").lower()
    if tier == "flexconsumption":
        product, sku, prefix, metric = "Flex Consumption", "On Demand", "On Demand", "OnDemandFunctionExecution"
    elif tier == "dynamic":
        product, sku, prefix, metric = "Functions", "Standard", "Standard", "FunctionExecution"
    else:
        return Estimate(0.0, "billed elsewhere", "Billed on its App Service plan")
    catalog = meters(service="Functions", product=product, region=facts.get("location", ""), sku=sku)
    time_meter, exec_meter = _find(catalog, f"{prefix} Execution Time"), _find(catalog, f"{prefix} Total Executions")
    if not (time_meter and exec_meter):
        return _none(f"No list price found for {product} in {facts.get('location')}")
    gb_seconds = _total(usage, f"{metric}Units") / 1_024_000           # Azure reports MB-milliseconds
    executions = _total(usage, f"{metric}Count")
    amount = time_meter.cost(gb_seconds) + exec_meter.cost(executions)
    return Estimate(amount, "executions",
                    f"{executions:,.0f} executions, {gb_seconds:,.1f} GB-s (free each month: "
                    f"{time_meter.free_units:,.0f} GB-s and {exec_meter.free_units:,.0f} executions)")


def _plan(facts: dict, usage: Usage, meters: MeterLookup, period: Period) -> Estimate:
    """App Service plans: instance-hours since the plan was created (or the month began) at its current size."""
    sku = facts.get("sku") or {}
    tier, name = str(sku.get("tier", "")).lower(), str(sku.get("name", ""))
    if tier in ("flexconsumption", "dynamic"):
        return Estimate(0.0, "billed elsewhere", "Billed per execution on its function apps")
    if tier == "free":
        return Estimate(0.0, "free tier", f"{name}: free tier")
    linux = bool(facts.get("reserved"))
    catalog = [m for m in meters(service="Azure App Service", region=facts.get("location", ""), sku=name)
               if m.unit == "1 Hour" and ("linux" in m.product.lower()) == linux]
    if not catalog:
        return _none(f"No list price found for {name} ({'Linux' if linux else 'Windows'})")
    instances = max(int(sku.get("capacity") or 1), 1)
    created = datetime.fromisoformat(facts["created"].replace("Z", "+00:00")) if facts.get("created") else None
    hours = period.hours_since(created)
    return Estimate(catalog[0].cost(hours * instances), "hours",
                    f"{name} x {instances} instance(s) for {hours:,.0f} h this month (at its current size)")


def storage_operation_class(api_name: str) -> str:
    """Blob operation price class of an Azure Storage ApiName."""
    name = api_name.lower()
    if name.startswith(("delete", "undelete")):
        return "free"
    if name.startswith("list") or name == "createcontainer":
        return "list"
    if name.startswith(("put", "create", "set", "append", "copy", "upload", "snapshot", "seal")):
        return "write"
    if name.startswith("get") and not any(x in name for x in ("properties", "metadata", "tags", "acl", "stats")):
        return "read"
    return "other"


def _storage(facts: dict, usage: Usage, meters: MeterLookup, period: Period) -> Estimate:
    """Standard general-purpose v2 / blob accounts: blob operations by class and stored GB. Queue,
    table and file operations are priced as blob ones; geo-replication transfer is not included."""
    kind = str(facts.get("kind", "")).lower()
    sku_name = str((facts.get("sku") or {}).get("name", ""))
    if not sku_name.startswith("Standard_") or kind not in ("storagev2", "blobstorage"):
        return _none(f"No price rules for {facts.get('kind')} {sku_name} storage yet")
    redundancy = {"RAGRS": "RA-GRS", "RAGZRS": "RA-GZRS"}.get(sku_name.split("_", 1)[1], sku_name.split("_", 1)[1])
    tier = str(facts.get("access_tier") or "Hot").capitalize()
    product = "General Block Blob v2" if kind == "storagev2" else "Blob Storage"
    catalog = meters(service="Storage", product=product, region=facts.get("location", ""),
                     sku=f"{tier} {redundancy}")
    write = _find(catalog, f"{tier} {redundancy} Write Operations")
    read = _find(catalog, f"{tier} Read Operations")
    other = _find(catalog, "All Other Operations")
    listing = _find(catalog, f"{redundancy} List and Create Container Operations") or write
    stored = _find(catalog, f"{tier} {redundancy} Data Stored")
    if not (write and read and other and stored):
        return _none(f"No list price found for {product} {tier} {redundancy}")
    counts = {"write": 0.0, "read": 0.0, "list": 0.0, "other": 0.0, "free": 0.0}
    for api, n in (usage.get("Transactions") or {}).items():
        counts[storage_operation_class(api)] += n
    gb = _total(usage, "UsedCapacity") / 1024 ** 3
    amount = (write.cost(counts["write"]) + read.cost(counts["read"]) + listing.cost(counts["list"])
              + other.cost(counts["other"]) + stored.cost(gb) * period.month_fraction)
    return Estimate(amount, "operations",
                    f"{counts['write']:,.0f} write, {counts['read']:,.0f} read, {counts['list']:,.0f} list, "
                    f"{counts['other']:,.0f} other operations; {gb:,.2f} GB stored")


def _vault(facts: dict, usage: Usage, meters: MeterLookup, period: Period) -> Estimate:
    """Secret / key operations (advanced key operations and HSM keys are not split out)."""
    sku = str(facts.get("vault_sku") or "standard").capitalize()
    meter = _find(meters(service="Key Vault", product="Key Vault", region=facts.get("location", ""), sku=sku),
                  "Operations")
    if not meter:
        return _none(f"No list price found for Key Vault {sku}")
    hits = _total(usage, "ServiceApiHit")
    return Estimate(meter.cost(hits), "operations", f"{hits:,.0f} operations")


def _workspace(facts: dict, usage: Usage, meters: MeterLookup, period: Period) -> Estimate:
    """Pay-as-you-go ingestion (Analytics logs). Retention beyond the included days is not included."""
    if str(facts.get("pricing_tier") or "pergb2018").lower() != "pergb2018":
        return _none(f"Pricing tier {facts.get('pricing_tier')} not estimated yet")
    if INGESTION_GB not in usage:
        return _none("Ingestion volume unavailable (ARC needs Log Analytics Reader on the workspace)")
    meter = _find(meters(service="Log Analytics", region=facts.get("location", "")), "Analytics Logs Data Ingestion")
    if not meter:
        return _none("No list price found for Log Analytics ingestion")
    gb = _total(usage, INGESTION_GB)
    return Estimate(meter.cost(gb), "ingestion",
                    f"{gb:,.3f} GB ingested (first {meter.free_units:,.0f} GB free each month)")


def _component(facts: dict, usage: Usage, meters: MeterLookup, period: Period) -> Estimate:
    if facts.get("workspace"):
        return Estimate(0.0, "billed elsewhere", f"Billed on Log Analytics workspace {facts['workspace']}")
    return _none("Classic Application Insights: not estimated")


def _cosmos(facts: dict, usage: Usage, meters: MeterLookup, period: Period) -> Estimate:
    """Serverless request units (storage not included). Provisioned throughput is not estimated yet."""
    if not facts.get("serverless"):
        return _none("Provisioned throughput: not estimated yet")
    meter = _find(meters(service="Azure Cosmos DB", product="Azure Cosmos DB serverless",
                         region=facts.get("location", "")), "1M RUs")
    if not meter:
        return _none("No list price found for Cosmos DB serverless")
    rus = _total(usage, "TotalRequestUnits")
    return Estimate(meter.cost(rus), "request units", f"{rus:,.0f} request units (storage not included)")


_RECIPES: dict[str, Callable[[dict, Usage, MeterLookup, Period], Estimate]] = {
    OPENAI: _openai, SITE: _site, PLAN: _plan, STORAGE: _storage, VAULT: _vault, WORKSPACE: _workspace,
    COMPONENT: _component, COSMOS: _cosmos,
}
