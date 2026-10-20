"""A product's Azure resource map: store scan proposals, record review decisions, attribute cost."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from arc_tower.db.models import AzureInventoryItem, Product, ProductResource
from arc_tower.utils.timeutil import utcnow

from . import audit
from .azure_mapping import AzResource, Candidate
from .errors import NotFound, ValidationFailed

PROPOSED, CONFIRMED, REJECTED = "proposed", "confirmed", "rejected"


@dataclass
class ScanSummary:
    total: int
    new: int
    high: int
    medium: int
    low: int
    missing: int


def apply_scan(session: Session, product: Product, candidates: list[Candidate], now: datetime) -> ScanSummary:
    """Upsert the proposals. Scan facts are refreshed; the reviewer's decisions are kept."""
    existing = {r.resource_id: r for r in product.azure_resources}
    seen, new = set(), 0
    for c in candidates:
        res = c.resource
        row = existing.get(res.id)
        if row is None:
            row = ProductResource(product_id=product.id, resource_id=res.id, status=PROPOSED, metrics={})
            product.azure_resources.append(row)
            new += 1
        row.name, row.type, row.resource_group, row.location = res.name, res.type, res.resource_group, res.location
        row.score, row.level, row.evidence = c.score, c.level, c.evidence
        row.shared_with, row.risks, row.is_anchor = c.shared_with, c.risks, c.is_anchor
        row.present, row.last_seen_at = True, now
        if c.is_anchor and row.status == PROPOSED:
            row.status = CONFIRMED            # the client named it themselves
        seen.add(res.id)
    missing = 0
    for rid, row in existing.items():
        if rid not in seen and row.present:
            row.present = False
            missing += 1
    session.flush()
    levels = [c.level for c in candidates]
    return ScanSummary(len(candidates), new, levels.count("high"), levels.count("medium"), levels.count("low"),
                       missing)


def tracked(product: Product) -> list[ProductResource]:
    """Resources ARC pulls metrics and cost for: everything still present that was not rejected."""
    return [r for r in product.azure_resources if r.present and r.status != REJECTED]


# --- the scanned scope, and resources added by hand ---------------------------------------

def record_inventory(session: Session, product: Product, resources: list[AzResource], now: datetime) -> None:
    """Keep what the scan saw, for the "add resources" picker. Picks survive even when a resource is
    gone (it may be back, or moved); everything else follows the scan."""
    existing = {i.resource_id: i for i in product.azure_inventory}
    seen = set()
    for res in resources:
        item = existing.get(res.id)
        if item is None:
            item = AzureInventoryItem(product_id=product.id, resource_id=res.id)
            product.azure_inventory.append(item)
        item.name, item.type, item.kind = res.name, res.type, res.kind
        item.resource_group, item.location, item.last_seen_at = res.resource_group, res.location, now
        seen.add(res.id)
    for rid, item in existing.items():
        if rid not in seen and not item.picked_by:
            product.azure_inventory.remove(item)
    session.flush()


def picked_evidence(product: Product) -> dict[str, list[tuple[str, float, str]]]:
    """Manual picks as mapping evidence, so every scan keeps them (see azure_mapping.build_product_map)."""
    return {i.resource_id: [("picked", 1.0, f"Added by {i.picked_by}")]
            for i in product.azure_inventory if i.picked_by}


def choices(product: Product) -> list[AzureInventoryItem]:
    """Scanned resources not in the review list yet: what a reviewer can add."""
    listed = {r.resource_id for r in product.azure_resources if r.present}
    return [i for i in product.azure_inventory if i.resource_id not in listed and i.last_seen_at]


def pick(session: Session, actor: str, product: Product, item_ids: list[int]) -> list[ProductResource]:
    """Add scanned resources by hand: confirmed at once; the next scan fills in sharing, risks, cost and metrics."""
    items = {i.id: i for i in product.azure_inventory}
    unknown = [i for i in item_ids if i not in items]
    if unknown or not item_ids:
        raise ValidationFailed("choose at least one resource from the scanned scope")
    rows = {r.resource_id: r for r in product.azure_resources}
    now, added = utcnow(), []
    for item_id in dict.fromkeys(item_ids):
        item = items[item_id]
        item.picked_by, item.picked_at = actor, now
        row = rows.get(item.resource_id)
        if row is None:
            row = ProductResource(product_id=product.id, resource_id=item.resource_id, metrics={}, evidence=[],
                                  shared_with=[], risks=[])
            product.azure_resources.append(row)
        row.name, row.type, row.resource_group, row.location = item.name, item.type, item.resource_group, \
            item.location
        row.evidence = [*(e for e in row.evidence or [] if not e.startswith("Added by ")), f"Added by {actor}"]
        row.score, row.level, row.present, row.last_seen_at = 1.0, "high", True, item.last_seen_at
        row.status, row.decided_by, row.decided_at = CONFIRMED, actor, now
        added.append(row)
    session.flush()
    for row in added:
        audit.record(session, actor, "azure_resource.add", "product_resource", row.id, tenant_id=product.tenant_id,
                     product=product.slug, resource=row.name, resource_id=row.resource_id)
    return added


def decide(session: Session, actor: str, product: Product, row_id: int, status: str,
           share_pct: float | None = None) -> ProductResource:
    row = next((r for r in product.azure_resources if r.id == row_id), None)
    if row is None:
        raise NotFound("resource not found for this product")
    if status not in (CONFIRMED, REJECTED, PROPOSED):
        raise ValidationFailed("decision must be confirmed, rejected or proposed")
    if share_pct is not None and not 0 <= share_pct <= 100:
        raise ValidationFailed("share must be between 0 and 100 %")
    if row.is_anchor and status == REJECTED:
        raise ValidationFailed("the starting point cannot be rejected - change the connector's starting point instead")
    row.status, row.decided_by, row.decided_at = status, actor, utcnow()
    row.share_pct = share_pct if row.shared else None
    if status == REJECTED:              # a rejected pick stops being forced into every scan
        for item in product.azure_inventory:
            if item.resource_id == row.resource_id:
                item.picked_by = item.picked_at = None
    audit.record(session, actor, "azure_resource.decide", "product_resource", row.id, tenant_id=product.tenant_id,
                 product=product.slug, resource=row.name, status=status, share_pct=row.share_pct)
    return row


def attributed_cost(row: ProductResource) -> tuple[float | None, str]:
    """(this product's part of the resource's cost, basis). None when it cannot be attributed yet."""
    if row.cost_mtd is None:
        return None, "no cost data"
    return attribute(row, row.cost_mtd)


def attribute(row: ProductResource, amount: float) -> tuple[float | None, str]:
    """This product's part of an amount of the resource's cost (billed or estimated), and the basis."""
    if row.status == PROPOSED:
        return None, "awaiting review"
    if not row.shared:
        return amount, "dedicated"
    if row.share_pct is None:
        return None, "shared - set this product's share"
    return amount * row.share_pct / 100.0, f"shared - {row.share_pct:g}% allocated"


@dataclass
class CostSummary:
    currency: str
    dedicated: float
    allocated: float
    unallocated_shared: float      # full cost of shared resources with no share set yet
    awaiting_review: float         # full cost of proposals nobody has confirmed

    @property
    def total(self) -> float:
        return self.dedicated + self.allocated


def cost_summary(product: Product) -> CostSummary:
    summary = CostSummary("", 0.0, 0.0, 0.0, 0.0)
    for row in tracked(product):
        if row.cost_mtd is None:
            continue
        summary.currency = summary.currency or row.currency
        value, basis = attributed_cost(row)
        if basis == "dedicated":
            summary.dedicated += value
        elif value is not None:
            summary.allocated += value
        elif basis == "awaiting review":
            summary.awaiting_review += row.cost_mtd
        else:
            summary.unallocated_shared += row.cost_mtd
    return summary
