"""Reading and creating the .env file, and building child-process environments."""
from __future__ import annotations

import base64
import os
import secrets

from .output import log
from .paths import ENV_FILE, ENV_TEMPLATE, ROOT

# Keys whose empty value in .env.example is replaced by a generated secret.
_GENERATED = {
    "ARC_SECRET_KEY": lambda: base64.urlsafe_b64encode(os.urandom(32)).decode(),
    "ARC_SESSION_SECRET": lambda: secrets.token_hex(32),
    "ARC_ADMIN_PASSWORD": lambda: secrets.token_urlsafe(12),
    "GRAFANA_ADMIN_PASSWORD": lambda: secrets.token_urlsafe(16),
}


def parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def read_env() -> dict[str, str]:
    return parse_env_text(ENV_FILE.read_text(encoding="utf-8")) if ENV_FILE.exists() else {}


def write_env_if_missing() -> None:
    if ENV_FILE.exists():
        log(".env already exists - keeping it")
        return
    lines = []
    for line in ENV_TEMPLATE.read_text(encoding="utf-8").splitlines():
        key = line.rstrip("=")
        if line.endswith("=") and key in _GENERATED:
            line = f"{key}={_GENERATED[key]()}"
        lines.append(line)
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log("created .env with fresh secrets")


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(read_env())
    env["ARC_HOME"] = str(ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    # Windows defaults redirected output to the ANSI code page; force UTF-8 so logs never crash on "·" or "→".
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Components talk to each other on 127.0.0.1; never send that through a corporate proxy.
    no_proxy = ",".join(filter(None, [env.get("NO_PROXY"), "127.0.0.1,localhost"]))
    env["NO_PROXY"] = env["no_proxy"] = no_proxy
    return env
