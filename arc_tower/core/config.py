"""Settings, read once from the environment (start.py loads .env into it)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .ports import PORTS


def _home() -> Path:
    return Path(os.environ.get("ARC_HOME") or Path(__file__).resolve().parents[2])


def _load_dotenv(path: Path) -> None:
    """Fill os.environ from .env for processes started outside start.py (tests, CLI)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# Internal component URLs. Overridable when a component runs on another host (and in tests).
LOKI_URL = os.environ.get("ARC_LOKI_URL", f"http://127.0.0.1:{PORTS['loki']}")
PROMETHEUS_URL = os.environ.get("ARC_PROMETHEUS_URL", f"http://127.0.0.1:{PORTS['prometheus']}")
PROM_LABEL_PROXY_URL = os.environ.get("ARC_PROM_LABEL_PROXY_URL", f"http://127.0.0.1:{PORTS['prom_label_proxy']}")
COLLECTOR_OTLP_URL = os.environ.get("ARC_COLLECTOR_URL", f"http://127.0.0.1:{PORTS['collector_otlp']}")
GRAFANA_URL = os.environ.get("ARC_GRAFANA_URL", f"http://127.0.0.1:{PORTS['grafana']}")
INTELLIGENCE_PORT = PORTS["intelligence"]


@dataclass(frozen=True)
class Settings:
    home: Path
    port: int
    base_url: str
    secret_key: str
    session_secret: str
    admin_email: str
    admin_password: str
    database_url: str
    proxy_host: str
    proxy_port: int
    public_ingest_url: str
    langfuse_host: str
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_org_public_key: str
    langfuse_org_secret_key: str
    grafana_admin_user: str
    grafana_admin_password: str
    secrets_backend: str
    vault_addr: str
    vault_token: str
    vault_mount: str
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str
    model_prices: dict = field(default_factory=dict)

    @property
    def run_dir(self) -> Path:
        return self.home / "run"

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_host and self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def langfuse_managed(self) -> bool:
        return bool(self.langfuse_host and self.langfuse_org_public_key and self.langfuse_org_secret_key)

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id)


@lru_cache
def get_settings() -> Settings:
    home = _home()
    _load_dotenv(home / ".env")
    env = os.environ.get
    run_dir = home / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    db_url = env("ARC_DATABASE_URL") or f"sqlite:///{(run_dir / 'arc.db').as_posix()}"
    try:
        prices = json.loads(env("ARC_MODEL_PRICES") or "{}")
    except json.JSONDecodeError:
        prices = {}
    port = int(env("ARC_PORT") or 8000)
    return Settings(
        home=home,
        port=port,
        base_url=(env("ARC_BASE_URL") or f"http://localhost:{port}").rstrip("/"),
        secret_key=env("ARC_SECRET_KEY") or "",
        session_secret=env("ARC_SESSION_SECRET") or "dev-only-session-secret",
        admin_email=env("ARC_ADMIN_EMAIL") or "admin@arc.local",
        admin_password=env("ARC_ADMIN_PASSWORD") or "",
        database_url=db_url,
        proxy_host=env("PROXY_HOST") or "127.0.0.1",
        proxy_port=int(env("PROXY_PORT") or 4318),
        public_ingest_url=(env("ARC_PUBLIC_INGEST_URL") or "http://localhost:4318/otlp").rstrip("/"),
        langfuse_host=(env("LANGFUSE_HOST") or "").rstrip("/"),
        langfuse_public_key=env("LANGFUSE_PUBLIC_KEY") or "",
        langfuse_secret_key=env("LANGFUSE_SECRET_KEY") or "",
        langfuse_org_public_key=env("LANGFUSE_ORG_PUBLIC_KEY") or "",
        langfuse_org_secret_key=env("LANGFUSE_ORG_SECRET_KEY") or "",
        grafana_admin_user=env("GRAFANA_ADMIN_USER") or "admin",
        grafana_admin_password=env("GRAFANA_ADMIN_PASSWORD") or "admin",
        secrets_backend=env("ARC_SECRETS_BACKEND") or "local",
        vault_addr=env("VAULT_ADDR") or "",
        vault_token=env("VAULT_TOKEN") or "",
        vault_mount=env("VAULT_MOUNT") or "secret",
        oidc_issuer=env("OIDC_ISSUER") or "",
        oidc_client_id=env("OIDC_CLIENT_ID") or "",
        oidc_client_secret=env("OIDC_CLIENT_SECRET") or "",
        model_prices=prices,
    )
