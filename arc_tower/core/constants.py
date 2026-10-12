"""Enumerations shared across services."""
from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    """A user's role inside one tenant."""
    VIEWER = "viewer"
    EDITOR = "editor"
    TENANT_ADMIN = "tenant_admin"

    @property
    def can_edit(self) -> bool:
        return self in (Role.EDITOR, Role.TENANT_ADMIN)

    @property
    def grafana_role(self) -> str:
        # Tenant admins get Editor, not Admin, in Grafana: an Admin could edit the
        # data source and remove the tenant filter that isolates their data.
        return "Viewer" if self is Role.VIEWER else "Editor"


class IngestMode(str, Enum):
    PUSH = "push"
    PULL = "pull"


class LangfuseMode(str, Enum):
    SHARED = "shared"      # one Langfuse project, traces tagged tenant:/product:
    OWN_KEYS = "own_keys"  # keys of a Langfuse project created by hand for this product
    MANAGED = "managed"    # project + keys created by ARC (Langfuse Enterprise org key)


class ProductStatus(str, Enum):
    ONBOARDING = "onboarding"
    LIVE = "live"
    PAUSED = "paused"


class KpiCategory(str, Enum):
    ADOPTION = "Adoption & Inventory"
    PERFORMANCE = "Performance & Reliability"
    COST = "Cost & FinOps"
    QUALITY = "Quality & Drift"
    SECURITY = "Security & Compliance"


class ContactPointType(str, Enum):
    EMAIL = "email"
    TEAMS = "teams"
    WEBHOOK = "webhook"
    SLACK = "slack"


# Resource / span attribute keys ARC Tower stamps on every signal.
ATTR_TENANT = "arc.tenant.id"
ATTR_PRODUCT = "arc.product.id"
ATTR_SERVICE = "arc.service.name"
ATTR_PLATFORM = "arc.platform"
ATTR_LANGFUSE_TAGS = "langfuse.trace.tags"

# Metadata headers passed from the ingest proxy to the collector.
HDR_TENANT = "x-arc-tenant"
HDR_PRODUCT = "x-arc-product"
HDR_LANGFUSE_AUTH = "x-langfuse-auth"
