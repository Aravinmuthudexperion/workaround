"""Which Langfuse project a product's traces go to, and the credentials to read them back.

shared   - the project from LANGFUSE_PUBLIC_KEY/SECRET_KEY; traces tagged tenant:<t> + product:<p>
own_keys - keys of a project created by hand for this product (stored in the secrets store)
managed  - project + keys created by ARC through the org API (Langfuse Enterprise only)
"""
from __future__ import annotations

from sqlalchemy.orm import object_session

from arc_tower.core.config import get_settings
from arc_tower.core.constants import LangfuseMode
from arc_tower.core.security import basic_auth
from arc_tower.db.models import Product
from arc_tower.integrations.langfuse import LangfuseAdminClient, LangfuseClient, LangfuseError
from arc_tower.secretstore import get_secret_store
from arc_tower.secretstore.base import langfuse_secret_name

from .errors import ValidationFailed


def credentials(product: Product) -> tuple[str, str] | None:
    settings = get_settings()
    if product.langfuse_mode == LangfuseMode.SHARED.value:
        if settings.langfuse_enabled:
            return settings.langfuse_public_key, settings.langfuse_secret_key
        return None
    secret = get_secret_store().get(product.langfuse_secret_ref) if product.langfuse_secret_ref else None
    return (secret["public_key"], secret["secret_key"]) if secret else None


def auth_header(product: Product) -> str | None:
    creds = credentials(product)
    return basic_auth(*creds) if creds else None


def client_for(product: Product) -> LangfuseClient | None:
    creds = credentials(product)
    host = get_settings().langfuse_host
    return LangfuseClient(host, *creds) if creds and host else None


def trace_tags(product: Product) -> list[str]:
    """Tags that select this product's traces (needed in shared mode, harmless otherwise)."""
    return [f"tenant:{product.tenant.slug}", f"product:{product.slug}"]


def save_own_keys(product: Product, public_key: str, secret_key: str) -> None:
    host = get_settings().langfuse_host
    if not host:
        raise ValidationFailed("LANGFUSE_HOST is not configured")
    try:
        projects = LangfuseClient(host, public_key.strip(), secret_key.strip()).projects()
    except LangfuseError as exc:
        raise ValidationFailed(f"Langfuse rejected these keys: {exc}") from exc
    name = langfuse_secret_name(product.id)
    get_secret_store().put(name, {"public_key": public_key.strip(), "secret_key": secret_key.strip()},
                           session=object_session(product))
    product.langfuse_mode = LangfuseMode.OWN_KEYS.value
    product.langfuse_secret_ref = name
    product.langfuse_project_id = projects[0]["id"] if projects else None


def provision_managed(product: Product) -> None:
    settings = get_settings()
    if not settings.langfuse_managed:
        raise ValidationFailed("managed mode needs LANGFUSE_ORG_PUBLIC_KEY / LANGFUSE_ORG_SECRET_KEY "
                               "(Langfuse Enterprise)")
    admin = LangfuseAdminClient(settings.langfuse_host, settings.langfuse_org_public_key,
                                settings.langfuse_org_secret_key)
    try:
        project = admin.create_project(f"{product.tenant.slug}-{product.slug}")
        key = admin.create_api_key(project["id"], note="ARC Tower ingest")
    except LangfuseError as exc:
        raise ValidationFailed(f"could not create the Langfuse project: {exc}") from exc
    name = langfuse_secret_name(product.id)
    get_secret_store().put(name, {"public_key": key["publicKey"], "secret_key": key["secretKey"]},
                           session=object_session(product))
    product.langfuse_mode = LangfuseMode.MANAGED.value
    product.langfuse_secret_ref = name
    product.langfuse_project_id = project["id"]
