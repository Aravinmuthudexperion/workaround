"""Agent registry entry: discovered from telemetry, enriched by owners."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from arc_tower.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from .product import Product


class Agent(TimestampMixin, Base):
    __tablename__ = "agents"
    __table_args__ = (UniqueConstraint("product_id", "service_name", "agent_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    service_name: Mapped[str] = mapped_column(String(200))
    agent_name: Mapped[str] = mapped_column(String(200), default="")
    platform: Mapped[str] = mapped_column(String(64), default="custom")
    models: Mapped[list] = mapped_column(JSON, default=list)
    discovered: Mapped[bool] = mapped_column(Boolean, default=True)

    # Owner-maintained metadata.
    display_name: Mapped[str] = mapped_column(String(200), default="")
    owner: Mapped[str] = mapped_column(String(200), default="")
    purpose: Mapped[str] = mapped_column(String(1000), default="")
    risk_class: Mapped[str] = mapped_column(String(32), default="unclassified")

    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    product: Mapped[Product] = relationship(back_populates="agents")

    @property
    def label(self) -> str:
        return self.display_name or self.agent_name or self.service_name
