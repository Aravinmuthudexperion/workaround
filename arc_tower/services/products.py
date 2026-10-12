"""Product lifecycle: create, ingest keys, the client env block, status."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from arc_tower.connectors import catalog
from arc_tower.core.config import get_settings
from arc_tower.core.constants import LangfuseMode, ProductStatus
from arc_tower.core.security import INGEST_KEY_PREFIX, hash_key, key_hint, new_opaque_key
from arc_tower.db.models import IngestKey, Product, Tenant
from arc_tower.utils.text import slugify
from arc_tower.utils.timeutil import utcnow

from . import audit, langfuse_routing
from .errors import ValidationFailed


@dataclass(frozen=True)
class IssuedKey:
    key: str
    record: IngestKey


def create_product(session: Session, actor: str, tenant: Tenant, name: str, connector_type: str,
                   description: str = "", environment: str = "prod",
                   langfuse_mode: str = LangfuseMode.SHARED.value) -> Product:
    connector = catalog.get(connector_type)
    if not connector or connector.maturity == "planned":
        raise ValidationFailed("choose an available connector")
    name = name.strip()
    if not name:
        raise ValidationFailed("product name is required")
    slug = slugify(name)
    if session.scalar(select(Product).where(Product.tenant_id == tenant.id, Product.slug == slug)):
        raise ValidationFailed(f"{tenant.name} already has a product '{slug}'")
    product = Product(tenant_id=tenant.id, tenant=tenant, slug=slug, name=name, description=description,
                      environment=slugify(environment or "prod", 16), connector_type=connector.key,
                      ingest_mode=connector.mode.value, langfuse_mode=LangfuseMode.SHARED.value, coverage={})
    session.add(product)
    session.flush()
    if langfuse_mode == LangfuseMode.MANAGED.value:
        langfuse_routing.provision_managed(product)
    audit.record(session, actor, "product.create", "product", product.id, tenant_id=tenant.id,
                 slug=slug, connector=connector.key)
    return product


def issue_ingest_key(session: Session, actor: str, product: Product, revoke_existing: bool = False) -> IssuedKey:
    if revoke_existing:
        for existing in product.ingest_keys:
            if existing.active:
                existing.revoked_at = utcnow()
    plain = new_opaque_key(INGEST_KEY_PREFIX)
    record = IngestKey(product_id=product.id, key_hash=hash_key(plain), hint=key_hint(plain))
    session.add(record)
    session.flush()
    audit.record(session, actor, "ingest_key.issue", "product", product.id, tenant_id=product.tenant_id,
                 hint=record.hint, rotated=revoke_existing)
    return IssuedKey(plain, record)


def revoke_ingest_key(session: Session, actor: str, product: Product, key_id: int) -> None:
    record = session.get(IngestKey, key_id)
    if not record or record.product_id != product.id:
        raise ValidationFailed("unknown key")
    record.revoked_at = utcnow()
    audit.record(session, actor, "ingest_key.revoke", "product", product.id, tenant_id=product.tenant_id,
                 hint=record.hint)


def product_for_key(session: Session, key: str) -> Product | None:
    record = session.scalar(select(IngestKey).where(IngestKey.key_hash == hash_key(key)))
    return record.product if record and record.active else None


def env_block(product: Product, key: str | None) -> str:
    """The env variables the client adds to their app's environment."""
    settings = get_settings()
    connector = catalog.get(product.connector_type)
    service = f"{product.slug}-agent"
    lines = [
        f"OTEL_EXPORTER_OTLP_ENDPOINT={settings.public_ingest_url}",
        "OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf",
        f"OTEL_EXPORTER_OTLP_HEADERS=x-arc-key={key or '<your ingest key>'}",
        f"OTEL_SERVICE_NAME={service}",
        f"OTEL_RESOURCE_ATTRIBUTES=deployment.environment={product.environment}",
        "OTEL_TRACES_EXPORTER=otlp",
        "OTEL_LOGS_EXPORTER=otlp",
        "OTEL_METRICS_EXPORTER=none",
    ]
    if connector:
        lines += [f"{k}={v}" for k, v in connector.push_extra_env]
    return "\n".join(lines)


def advance_onboarding(product: Product, step: int) -> None:
    product.onboarding_step = max(product.onboarding_step, min(step, 6))


def mark_seen(product: Product, when=None) -> None:
    when = when or utcnow()
    if product.first_seen_at is None:
        product.first_seen_at = when
        if product.status == ProductStatus.ONBOARDING.value:
            product.status = ProductStatus.LIVE.value
    product.last_seen_at = when


def delete_product(session: Session, actor: str, product: Product) -> None:
    audit.record(session, actor, "product.delete", "product", product.id, tenant_id=product.tenant_id,
                 slug=product.slug)
    session.delete(product)
