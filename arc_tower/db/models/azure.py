"""Azure resources mapped to a product by the Azure platform connector, and the reviewer's decision on each."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from arc_tower.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from .product import Product


class ProductResource(TimestampMixin, Base):
    __tablename__ = "product_resources"
    __table_args__ = (UniqueConstraint("product_id", "resource_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    resource_id: Mapped[str] = mapped_column(String(512))           # lower-case ARM id
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(200))
    resource_group: Mapped[str] = mapped_column(String(100), default="")
    location: Mapped[str] = mapped_column(String(64), default="")

    # From the latest scan (overwritten on every scan).
    score: Mapped[float] = mapped_column(Float, default=0.0)
    level: Mapped[str] = mapped_column(String(16), default="low")   # high | medium | low
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    shared_with: Mapped[list] = mapped_column(JSON, default=list)
    risks: Mapped[list] = mapped_column(JSON, default=list)
    is_anchor: Mapped[bool] = mapped_column(Boolean, default=False)
    present: Mapped[bool] = mapped_column(Boolean, default=True)   # False = not found in the last scan
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # The reviewer's decision (kept across scans).
    status: Mapped[str] = mapped_column(String(16), default="proposed")  # proposed | confirmed | rejected
    share_pct: Mapped[float | None] = mapped_column(Float)          # this product's share of a shared resource
    decided_by: Mapped[str | None] = mapped_column(String(200))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Latest usage and cost pulled from Azure.
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)        # {metric: {dimension or "": value}}
    cost_mtd: Mapped[float | None] = mapped_column(Float)            # month-to-date cost of the whole resource
    currency: Mapped[str] = mapped_column(String(8), default="")

    product: Mapped[Product] = relationship(back_populates="azure_resources")

    @property
    def shared(self) -> bool:
        return bool(self.shared_with)

    @property
    def short_type(self) -> str:
        return self.type.split("/", 1)[-1]
