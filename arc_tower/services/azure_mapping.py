"""Work out which Azure resources belong to one product, starting from a single resource the client names.

Clients rarely know (or share) every service a product uses, and one resource group often holds
several products. From the starting point (the product's web app, function, container app...) we
follow evidence that only needs Reader access:

  managed identity   -> role assignments and Key Vault access policies of that identity
  hosting            -> App Service plan / Container Apps environment running the starting point
  App Insights       -> the component Azure links to the app (hidden-link tag)
  tags               -> resources carrying the same product tag
  network            -> private endpoints in the starting point's virtual network
  naming             -> resources whose name contains the product's name token

Two more kinds come from outside this module (`extra`): calls the product's App Insights recorded
to a resource's endpoint (Log Analytics Reader; finds services used with API keys, which leave no
role assignment), and resources a reviewer added by hand.

Each piece of evidence has a weight; they combine as independent signals (noisy-or) into a
confidence score. A person then confirms or rejects the proposal (see services/azure_resources.py).
Pure functions: no Azure calls, no database.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

HIGH, MEDIUM, LOW = "high", "medium", "low"

PRODUCT_TAG_KEYS = ("product", "app", "application", "project", "workload", "service", "system", "solution")

# Generic words in Azure resource names that say nothing about the product.
_STOP_TOKENS = {
    "app", "apps", "func", "function", "functions", "web", "webapp", "api", "aca", "prod", "production", "dev",
    "test", "uat", "stage", "staging", "plan", "service", "server", "backend", "frontend", "processor",
    "worker", "openai", "cosmos", "storage", "vault", "search", "insights", "shared", "common", "core",
    "east", "west", "north", "south", "central", "india", "europe", "eastus", "westus", "westeurope",
    "northeurope", "centralindia", "southindia", "primary", "secondary", "main", "data", "docs",
}

_WEIGHTS = {
    "anchor": 1.0, "identity": 0.9, "tag": 0.9, "app_insights": 0.9, "role": 0.8, "access_policy": 0.8,
    "hosting": 0.8, "network": 0.5, "name": 0.3,
}
CALLS_WEIGHT = 0.95     # calls App Insights recorded to the resource's own endpoint

# Well-known built-in role definition ids, to explain the evidence in words.
_ROLE_NAMES = {
    "acdd72a7-3385-48ef-bd42-f606fba81ae7": "Reader",
    "b24988ac-6180-42a0-ab88-20f7382dd24c": "Contributor",
    "ba92f5b4-2d11-453d-a403-e96b0029c9fe": "Storage Blob Data Contributor",
    "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1": "Storage Blob Data Reader",
    "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd": "Cognitive Services OpenAI User",
    "a97b65f3-24c7-4388-baec-2e87135dc908": "Cognitive Services User",
    "4633458b-17de-408a-b874-0445c86b69e6": "Key Vault Secrets User",
}

_COMPUTE_TYPES = {"microsoft.web/sites", "microsoft.app/containerapps", "microsoft.app/jobs",
                  "microsoft.containerinstance/containergroups", "microsoft.compute/virtualmachines",
                  "microsoft.machinelearningservices/workspaces/onlineendpoints"}
_UAMI_TYPE = "microsoft.managedidentity/userassignedidentities"
_PRIVATE_ENDPOINT_TYPE = "microsoft.network/privateendpoints"
_APP_INSIGHTS_TYPE = "microsoft.insights/components"


class MappingError(ValueError):
    """The scan cannot be mapped (e.g. the starting point is not in the scanned scope)."""


@dataclass
class AzResource:
    id: str                                  # lower-case ARM id
    name: str
    type: str                                # lower-case, e.g. microsoft.web/sites
    resource_group: str = ""
    subscription_id: str = ""
    location: str = ""
    kind: str = ""
    sku: dict = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    principal_ids: set[str] = field(default_factory=set)        # this resource's own identities
    user_identity_ids: set[str] = field(default_factory=set)    # attached user-assigned identity resources
    properties: dict = field(default_factory=dict)

    @classmethod
    def from_graph(cls, row: dict) -> AzResource:
        identity = row.get("identity") or {}
        principals = {str(identity["principalId"]).lower()} if identity.get("principalId") else set()
        uamis = {str(k).lower() for k in (identity.get("userAssignedIdentities") or {})}
        for value in (identity.get("userAssignedIdentities") or {}).values():
            if isinstance(value, dict) and value.get("principalId"):
                principals.add(str(value["principalId"]).lower())
        props = row.get("properties") or {}
        rtype = str(row.get("type", "")).lower()
        if rtype == _UAMI_TYPE and props.get("principalId"):
            principals.add(str(props["principalId"]).lower())
        return cls(id=str(row.get("id", "")).lower(), name=str(row.get("name", "")), type=rtype,
                   resource_group=str(row.get("resourceGroup", "")), subscription_id=str(row.get("subscriptionId", "")),
                   location=str(row.get("location", "")), kind=str(row.get("kind") or ""),
                   sku=row.get("sku") if isinstance(row.get("sku"), dict) else {},
                   tags={str(k): str(v) for k, v in (row.get("tags") or {}).items()},
                   principal_ids=principals, user_identity_ids=uamis, properties=props)

    @property
    def visible_tags(self) -> dict[str, str]:
        return {k: v for k, v in self.tags.items() if not k.lower().startswith("hidden-")}

    @property
    def short_type(self) -> str:
        return self.type.split("/", 1)[-1]


@dataclass(frozen=True)
class RoleAssignment:
    principal_id: str
    scope: str            # lower-case ARM id the role is granted on
    role_definition_id: str = ""

    @property
    def role_name(self) -> str:
        guid = self.role_definition_id.rsplit("/", 1)[-1].lower()
        return _ROLE_NAMES.get(guid, "a role")


@dataclass
class Inventory:
    resources: list[AzResource]
    role_assignments: list[RoleAssignment] = field(default_factory=list)

    @classmethod
    def from_graph(cls, resource_rows: list[dict], assignment_rows: list[dict]) -> Inventory:
        assignments = [RoleAssignment(str(r.get("principalId", "")).lower(), str(r.get("scope", "")).lower(),
                                      str(r.get("roleDefinitionId", "")).lower())
                       for r in assignment_rows if r.get("principalId") and r.get("scope")]
        return cls([AzResource.from_graph(r) for r in resource_rows if r.get("id")], assignments)


@dataclass
class Candidate:
    resource: AzResource
    score: float
    level: str
    evidence: list[str]
    is_anchor: bool = False
    shared_with: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    @property
    def shared(self) -> bool:
        return bool(self.shared_with)


# --- helpers ---------------------------------------------------------------------------

def level_for(score: float) -> str:
    if score >= 0.75:
        return HIGH
    if score >= 0.45:
        return MEDIUM
    return LOW


def _combine(weights: list[float]) -> float:
    miss = 1.0
    for w in weights:
        miss *= 1.0 - w
    return round(1.0 - miss, 3)


def _vnet_of(subnet_id: str) -> str:
    return subnet_id.lower().split("/subnets/", 1)[0] if subnet_id else ""


def _name_tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z]+", name.lower()) if len(t) >= 4 and t not in _STOP_TOKENS}


def _compact(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _find(resources: list[AzResource], ref: str) -> AzResource | None:
    ref = ref.strip().lower()
    by_id = next((r for r in resources if r.id == ref), None)
    if by_id:
        return by_id
    matches = [r for r in resources if r.name.lower() == ref]
    compute = [r for r in matches if r.type in _COMPUTE_TYPES]
    return (compute or matches or [None])[0]


def _parse_tag(product_tag: str | None) -> tuple[str, str] | None:
    if product_tag and "=" in product_tag:
        key, value = product_tag.split("=", 1)
        if key.strip() and value.strip():
            return key.strip().lower(), value.strip().lower()
    return None


def _anchor_tag(anchor: AzResource) -> tuple[str, str] | None:
    tags = {k.lower(): v for k, v in anchor.visible_tags.items()}
    return next(((k, tags[k].lower()) for k in PRODUCT_TAG_KEYS if tags.get(k)), None)


def _target_of(scope: str, by_id: dict[str, AzResource]) -> AzResource | None:
    """The resource a role-assignment scope points at (the resource itself or one of its children)."""
    probe = scope
    while "/providers/" in probe:
        if probe in by_id:
            return by_id[probe]
        probe = probe.rsplit("/", 2)[0]
    return None


# --- risks -------------------------------------------------------------------------------

def risks_for(res: AzResource) -> list[str]:
    p = {str(k).lower(): v for k, v in res.properties.items()}
    found = []
    network = p.get("network") if isinstance(p.get("network"), dict) else {}
    public = str(p.get("publicnetworkaccess") or network.get("publicNetworkAccess") or "").lower()
    if public == "enabled":
        found.append("Public network access enabled")
    if res.type in ("microsoft.cognitiveservices/accounts", "microsoft.documentdb/databaseaccounts") \
            and p.get("disablelocalauth") is not True:
        found.append("Key-based access allowed (Entra ID not enforced)")
    if res.type == "microsoft.storage/storageaccounts":
        if p.get("allowsharedkeyaccess") is not False:
            found.append("Shared-key access allowed (Entra ID not enforced)")
        if p.get("allowblobpublicaccess") is True:
            found.append("Anonymous blob access allowed")
    if res.type == "microsoft.web/sites" and p.get("httpsonly") is False:
        found.append("HTTPS not enforced")
    if not res.visible_tags and res.type not in (_PRIVATE_ENDPOINT_TYPE,):
        found.append("No tags: owner and product unknown")
    return found


# --- observed calls ----------------------------------------------------------------------

# Hosts an AI Services / Cognitive Services account answers on, besides the URLs in its properties.
_SUBDOMAIN_SUFFIXES = (".openai.azure.com", ".cognitiveservices.azure.com", ".services.ai.azure.com")
_HOST_KEYS = {"defaulthostname", "hostname", "hostnames", "fullyqualifieddomainname", "fqdn"}
_COGNITIVE_TYPE = "microsoft.cognitiveservices/accounts"


def _hosts_in(value, found: set[str], depth: int = 0) -> None:
    """Host names in a resource's properties: every http(s) URL, plus host-name fields."""
    if depth > 5:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in _HOST_KEYS:
                for host in [item] if isinstance(item, str) else item if isinstance(item, list) else []:
                    if isinstance(host, str) and "." in host and "/" not in host:
                        found.add(host.lower())
            else:
                _hosts_in(item, found, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _hosts_in(item, found, depth + 1)
    elif isinstance(value, str) and value.lower().startswith(("https://", "http://")):
        host = urlsplit(value).hostname
        if host:
            found.add(host.lower())


def endpoint_hosts(res: AzResource) -> set[str]:
    found: set[str] = set()
    _hosts_in(res.properties, found)
    subdomain = str(res.properties.get("customSubDomainName") or "").lower()
    if res.type == _COGNITIVE_TYPE and subdomain:
        found |= {subdomain + suffix for suffix in _SUBDOMAIN_SUFFIXES}
    if res.type == "microsoft.search/searchservices":
        found.add(f"{res.name.lower()}.search.windows.net")
    return found


def call_host(target: str) -> str:
    """The host in an App Insights dependency Target: 'host', 'host:443', 'host | db' or a URL."""
    text = str(target or "").split(" | ", 1)[0].strip().lower()
    if "://" in text:
        text = urlsplit(text).hostname or ""
    return text.split(":", 1)[0] if text.count(":") == 1 else text


def match_calls(resources: list[AzResource], calls: dict[str, int],
                skip: set[str] = frozenset()) -> dict[str, tuple[int, list[str]]]:
    """{resource id: (calls, hosts)} for the call targets that are one resource's endpoint. A host
    several resources answer on (a shared regional endpoint) says nothing about which one, so it is skipped."""
    owners: dict[str, set[str]] = {}
    for res in resources:
        for host in endpoint_hosts(res):
            owners.setdefault(host, set()).add(res.id)
    found: dict[str, tuple[int, list[str]]] = {}
    for target, count in calls.items():
        host = call_host(target)
        ids = owners.get(host, set())
        if len(ids) == 1 and (rid := next(iter(ids))) not in skip:
            total, hosts = found.get(rid, (0, []))
            found[rid] = (total + int(count), hosts if host in hosts else [*hosts, host])
    return found


# --- the mapping -------------------------------------------------------------------------

def build_product_map(inventory: Inventory, starting_point: str, product_tag: str | None = None,
                      extra: dict[str, list[tuple[str, float, str]]] | None = None) -> list[Candidate]:
    """`extra`: more evidence per resource id, as (kind, weight, text): observed calls, manual picks."""
    resources = inventory.resources
    by_id = {r.id: r for r in resources}
    anchor = _find(resources, starting_point)
    if anchor is None:
        raise MappingError(f"starting point '{starting_point}' was not found in the scanned subscriptions / "
                           f"resource groups (check the name, or that ARC has Reader on its resource group)")

    signals: dict[str, list[tuple[str, float, str]]] = {}

    def add(res: AzResource, kind: str, text: str, weight: float | None = None) -> None:
        signals.setdefault(res.id, [])
        if not any(t == text for _, _, t in signals[res.id]):
            signals[res.id].append((kind, _WEIGHTS[kind] if weight is None else weight, text))

    add(anchor, "anchor", "Starting point named by the client")

    # Identities that act for the product: the anchor's own, plus attached user-assigned identities.
    identities = set(anchor.principal_ids)
    for uami_id in anchor.user_identity_ids:
        uami = by_id.get(uami_id)
        if uami:
            add(uami, "identity", "Managed identity used by the starting point")
            identities |= uami.principal_ids

    # Who owns each identity, to name the other users of a shared resource.
    owners: dict[str, list[str]] = {}
    for res in resources:
        if res.type == _UAMI_TYPE:
            continue
        for pid in res.principal_ids:
            owners.setdefault(pid, []).append(res.name)
    users: dict[str, set[str]] = {}       # resource id -> names of other identities' owners using it

    for ra in inventory.role_assignments:
        target = _target_of(ra.scope, by_id)
        if target is None or target.id == anchor.id:
            continue
        if ra.principal_id in identities:
            add(target, "role", f"Starting point's identity has {ra.role_name} on it")
        elif ra.principal_id in owners:
            users.setdefault(target.id, set()).update(owners[ra.principal_id])

    for res in resources:
        if res.type != "microsoft.keyvault/vaults":
            continue
        for policy in res.properties.get("accessPolicies") or []:
            object_id = str(policy.get("objectId", "")).lower()
            if object_id in identities:
                add(res, "access_policy", "Access policy grants the starting point's identity")
            elif object_id in owners:
                users.setdefault(res.id, set()).update(owners[object_id])

    # Hosting: App Service plan / Container Apps environment.
    hosts = {str(anchor.properties.get(k) or "").lower()
             for k in ("serverFarmId", "managedEnvironmentId", "environmentId")} - {""}
    for host_id in hosts:
        host = by_id.get(host_id)
        if host:
            add(host, "hosting", "Hosts the starting point")
            others = [r.name for r in resources if r.id != anchor.id and host_id in
                      {str(r.properties.get(k) or "").lower() for k in ("serverFarmId", "managedEnvironmentId",
                                                                        "environmentId")}]
            if others:
                users.setdefault(host.id, set()).update(others)

    # Application Insights: Azure tags the component with hidden-link:<app id>.
    for res in resources:
        if res.type == _APP_INSIGHTS_TYPE and any(k.lower() == f"hidden-link:{anchor.id}" for k in res.tags):
            add(res, "app_insights", "Application Insights linked to the starting point")

    # Tags.
    tag = _parse_tag(product_tag) or _anchor_tag(anchor)
    if tag:
        key, value = tag
        for res in resources:
            tags = {k.lower(): v.lower() for k, v in res.visible_tags.items()}
            if tags.get(key) == value and res.id != anchor.id:
                add(res, "tag", f"Tagged {key}={value}")

    # Network: private endpoints in the starting point's virtual network, and what they connect to.
    subnet = str(anchor.properties.get("virtualNetworkSubnetId") or "")
    if not subnet:
        env = by_id.get(str(anchor.properties.get("managedEnvironmentId") or "").lower())
        subnet = str(((env.properties.get("vnetConfiguration") or {}) if env else {}).get("infrastructureSubnetId")
                     or "")
    vnet = _vnet_of(subnet)
    if vnet:
        for pe in (r for r in resources if r.type == _PRIVATE_ENDPOINT_TYPE):
            if _vnet_of(str((pe.properties.get("subnet") or {}).get("id") or "")) != vnet:
                continue
            add(pe, "network", "Private endpoint in the starting point's virtual network")
            for conn in (pe.properties.get("privateLinkServiceConnections") or []) + \
                        (pe.properties.get("manualPrivateLinkServiceConnections") or []):
                target = by_id.get(str((conn.get("properties") or {}).get("privateLinkServiceId") or "").lower())
                if target:
                    add(target, "network", "Reached through a private endpoint in the starting point's network")

    # Naming: the product's distinctive word(s) from the starting point's name.
    for token in _name_tokens(anchor.name):
        for res in resources:
            if res.id != anchor.id and token in _compact(res.name):
                add(res, "name", f"Name contains '{token}'")

    for rid, found in (extra or {}).items():
        res = by_id.get(rid)
        if res is not None and res.id != anchor.id:      # gone from the scope: the row shows "not found"
            for kind, weight, text in found:
                add(res, kind, text, weight)

    candidates = []
    for rid, found in signals.items():
        res = by_id[rid]
        score = _combine([w for _, w, _ in found])
        shared_with = sorted(users.get(rid, set()) - {anchor.name})
        if tag:
            other = {k.lower(): v for k, v in res.visible_tags.items()}.get(tag[0])
            if other and other.lower() != tag[1]:
                shared_with.append(f"tagged {tag[0]}={other}")
        candidates.append(Candidate(res, score, level_for(score), [t for _, _, t in found],
                                    is_anchor=res.id == anchor.id, shared_with=shared_with, risks=risks_for(res)))
    candidates.sort(key=lambda c: (not c.is_anchor, -c.score, c.resource.name.lower()))
    return candidates


# --- Resource Graph queries --------------------------------------------------------------

_NAME_RE = re.compile(r"^[A-Za-z0-9._()\-]{1,90}$")
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def parse_subscriptions(raw: str) -> list[str]:
    subs = [s.strip() for s in re.split(r"[,\s]+", raw or "") if s.strip()]
    bad = [s for s in subs if not _GUID_RE.match(s)]
    if not subs or bad:
        raise MappingError(f"subscription IDs must be GUIDs (got: {', '.join(bad) or 'none'})")
    return subs


def parse_resource_groups(raw: str) -> list[str]:
    groups = [g.strip() for g in re.split(r"[,\s]+", raw or "") if g.strip()]
    bad = [g for g in groups if not _NAME_RE.match(g)]
    if bad:
        raise MappingError(f"invalid resource group name(s): {', '.join(bad)}")
    return groups


def resources_query(resource_groups: list[str]) -> str:
    where = ""
    if resource_groups:      # names are validated by parse_resource_groups: safe to quote
        where = "| where resourceGroup in~ (" + ", ".join(f"'{g}'" for g in resource_groups) + ")\n"
    return ("resources\n" + where +
            "| project id, name, type, kind, sku, location, resourceGroup, subscriptionId, tags, identity, properties")


ROLE_ASSIGNMENTS_QUERY = (
    "authorizationresources\n"
    "| where type =~ 'microsoft.authorization/roleassignments'\n"
    "| project principalId = tostring(properties.principalId), scope = tolower(tostring(properties.scope)), "
    "roleDefinitionId = tolower(tostring(properties.roleDefinitionId))"
)
