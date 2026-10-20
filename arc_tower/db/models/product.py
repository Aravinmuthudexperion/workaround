"""A product is one client AI application onboarded to ARC Tower, with its ingest keys."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from arc_tower.core.constants import IngestMode, LangfuseMode, ProductStatus
from arc_tower.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from .agent import Agent
    from .azure import AzureInventoryItem, ProductResource
    from .connector import Connector
    from .kpi import Target
    from .tenant import Tenant


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("tenant_id", "slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(1000), default="")
    environment: Mapped[str] = mapped_column(String(32), default="prod")
    connector_type: Mapped[str] = mapped_column(String(64))
    ingest_mode: Mapped[str] = mapped_column(String(16), default=IngestMode.PUSH.value)
    status: Mapped[str] = mapped_column(String(16), default=ProductStatus.ONBOARDING.value)
    onboarding_step: Mapped[int] = mapped_column(Integer, default=1)

    langfuse_mode: Mapped[str] = mapped_column(String(16), default=LangfuseMode.SHARED.value)
    langfuse_project_id: Mapped[str | None] = mapped_column(String(64))
    # Name of the secret (in the secrets store) holding "public_key:secret_key" for own/managed modes.
    langfuse_secret_ref: Mapped[str | None] = mapped_column(String(128))

    grafana_folder_uid: Mapped[str | None] = mapped_column(String(64))
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Field coverage counters from the ingest proxy: {"spans": n, "fields": {"gen_ai.request.model": n, ...}}
    coverage: Mapped[dict] = mapped_column(JSON, default=dict)

    tenant: Mapped[Tenant] = relationship(back_populates="products")
    ingest_keys: Mapped[list[IngestKey]] = relationship(back_populates="product", cascade="all, delete-orphan",
                                                        order_by="IngestKey.created_at.desc()")
    agents: Mapped[list[Agent]] = relationship(back_populates="product", cascade="all, delete-orphan")
    connectors: Mapped[list[Connector]] = relationship(back_populates="product", cascade="all, delete-orphan")
    targets: Mapped[list[Target]] = relationship(back_populates="product", cascade="all, delete-orphan")
    azure_resources: Mapped[list[ProductResource]] = relationship(
        back_populates="product", cascade="all, delete-orphan",
        order_by="(ProductResource.is_anchor.desc(), ProductResource.score.desc(), ProductResource.name)")
    azure_inventory: Mapped[list[AzureInventoryItem]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="AzureInventoryItem.name")

    @property
    def is_push(self) -> bool:
        return self.ingest_mode == IngestMode.PUSH.value


class IngestKey(TimestampMixin, Base):
    """OTLP ingest key. Only the SHA-256 is stored; the key itself is shown once."""
    __tablename__ = "ingest_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    hint: Mapped[str] = mapped_column(String(64))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    product: Mapped[Product] = relationship(back_populates="ingest_keys")

    @property
    def active(self) -> bool:
        return self.revoked_at is None
