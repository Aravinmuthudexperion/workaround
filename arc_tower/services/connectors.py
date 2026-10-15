"""Pull-connector configuration: non-secret settings in the DB, credentials in the secrets store."""
from __future__ import annotations

from sqlalchemy.orm import Session

from arc_tower.connectors import catalog
from arc_tower.db.models import Connector, Product
from arc_tower.secretstore import get_secret_store
from arc_tower.secretstore.base import connector_secret_name

from . import audit
from .errors import ValidationFailed


def configure(session: Session, actor: str, product: Product, form: dict[str, str],
              interval_seconds: int | None = None) -> Connector:
    spec = catalog.get(product.connector_type)
    if not spec or spec.is_push:
        raise ValidationFailed("this product uses a push connector - nothing to configure here")
    connector = next((c for c in product.connectors if c.type == spec.key), None)
    if connector is None:
        connector = Connector(product_id=product.id, type=spec.key, config={}, cursor={},
                              interval_seconds=spec.default_interval_seconds)
        session.add(connector)
        session.flush()
    config: dict[str, str] = {}
    secret: dict[str, str] = dict(get_secret_store().get(connector.secret_ref) or {}) if connector.secret_ref else {}
    for field in spec.fields:
        value = (form.get(field.name) or "").strip()
        if field.secret:
            if value:                      # blank = keep the stored value
                secret[field.name] = value
            elif field.required and field.name not in secret:
                raise ValidationFailed(f"{field.label} is required")
        else:
            value = value or field.default
            if field.required and not value:
                raise ValidationFailed(f"{field.label} is required")
            config[field.name] = value
    connector.config = config
    if secret:
        connector.secret_ref = connector_secret_name(connector.id)
        get_secret_store().put(connector.secret_ref, secret, session=session)
    if interval_seconds:
        connector.interval_seconds = max(30, int(interval_seconds))
    connector.enabled = True
    request_run(connector)                 # new settings (e.g. another starting point) apply at once
    audit.record(session, actor, "connector.configure", "connector", connector.id, tenant_id=product.tenant_id,
                 type=spec.key, fields=sorted(config))
    return connector


def request_run(connector: Connector) -> None:
    """Ask the worker to run this connector on its next tick, including any slower periodic work
    (e.g. the Azure connector's resource scan, normally every 30 minutes)."""
    connector.last_run_at = None
    connector.cursor = {k: v for k, v in (connector.cursor or {}).items() if k != "scan_at"}


def secrets_for(connector: Connector) -> dict[str, str]:
    return get_secret_store().get(connector.secret_ref) or {} if connector.secret_ref else {}
