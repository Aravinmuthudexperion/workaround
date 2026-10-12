"""A tenant is a client team / business unit that owns products."""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from arc_tower.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from .alerting import ContactPoint
    from .product import Product
    from .user import Membership


class Tenant(TimestampMixin, Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(1000), default="")
    grafana_org_id: Mapped[int | None] = mapped_column(Integer)

    products: Mapped[list[Product]] = relationship(back_populates="tenant", cascade="all, delete-orphan",
                                                   order_by="Product.name")
    memberships: Mapped[list[Membership]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    contact_points: Mapped[list[ContactPoint]] = relationship(back_populates="tenant",
                                                              cascade="all, delete-orphan")
