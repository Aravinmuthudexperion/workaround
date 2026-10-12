"""KPI catalog and per-product targets (the PPT's "Configure KPIs & Business Targets")."""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from arc_tower.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from .product import Product


class KpiDefinition(TimestampMixin, Base):
    __tablename__ = "kpi_definitions"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(64))
    unit: Mapped[str] = mapped_column(String(32), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    # PromQL with a {sel} placeholder that ARC fills with the enforced tenant/product selector.
    promql: Mapped[str] = mapped_column(Text)
    # "above" = alert when the value goes above the threshold, "below" = when it drops below.
    breach: Mapped[str] = mapped_column(String(8), default="above")
    default_threshold: Mapped[float | None] = mapped_column(Float)
    alertable: Mapped[bool] = mapped_column(Boolean, default=True)


class Target(TimestampMixin, Base):
    __tablename__ = "targets"
    __table_args__ = (UniqueConstraint("product_id", "kpi_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    kpi_key: Mapped[str] = mapped_column(ForeignKey("kpi_definitions.key"))
    threshold: Mapped[float] = mapped_column(Float)
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    alert_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    grafana_rule_uid: Mapped[str | None] = mapped_column(String(64))

    product: Mapped[Product] = relationship(back_populates="targets")
    kpi: Mapped[KpiDefinition] = relationship()
