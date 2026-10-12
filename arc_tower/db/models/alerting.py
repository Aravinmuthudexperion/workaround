"""Where a tenant's alerts are delivered."""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from arc_tower.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from .tenant import Tenant


class ContactPoint(TimestampMixin, Base):
    __tablename__ = "contact_points"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(32))
    settings: Mapped[dict] = mapped_column(JSON, default=dict)   # e.g. {"addresses": "..."} / {"url": "..."}
    grafana_uid: Mapped[str | None] = mapped_column(String(64))

    tenant: Mapped[Tenant] = relationship(back_populates="contact_points")
