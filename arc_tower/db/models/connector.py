"""A configured pull connector instance for one product."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from arc_tower.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from .product import Product


class Connector(TimestampMixin, Base):
    __tablename__ = "connectors"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSON, default=dict)       # non-secret settings
    secret_ref: Mapped[str | None] = mapped_column(String(128))    # secrets-store name for credentials
    interval_seconds: Mapped[int] = mapped_column(Integer, default=300)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cursor: Mapped[dict] = mapped_column(JSON, default=dict)       # incremental-sync state
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str] = mapped_column(String(16), default="never")  # ok | error | never
    last_message: Mapped[str] = mapped_column(Text, default="")
    last_record_count: Mapped[int] = mapped_column(Integer, default=0)

    product: Mapped[Product] = relationship(back_populates="connectors")
