"""A product's Azure resource map: store scan proposals, record review decisions, attribute cost."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from arc_tower.db.models import Product, ProductResource
from arc_tower.utils.timeutil import utcnow

from . import audit
from .azure_mapping import Candidate
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
