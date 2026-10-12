"""Test setup: an isolated ARC_HOME with its own SQLite registry and secret key."""
from __future__ import annotations

import base64
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Never touch a running ARC stack from tests: point every component at a closed port.
_CLOSED = "http://127.0.0.1:9"
for _name in ("ARC_GRAFANA_URL", "ARC_PROMETHEUS_URL", "ARC_LOKI_URL", "ARC_PROM_LABEL_PROXY_URL",
              "ARC_COLLECTOR_URL"):
    os.environ[_name] = _CLOSED


@pytest.fixture(scope="session", autouse=True)
def arc_home(tmp_path_factory):
    home = tmp_path_factory.mktemp("arc_home")
    shutil.copytree(ROOT / "deploy", home / "deploy")
    os.environ.update({
        "ARC_HOME": str(home),
        "ARC_SECRET_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        "ARC_DATABASE_URL": f"sqlite:///{(home / 'test.db').as_posix()}",
        "ARC_ADMIN_EMAIL": "admin@test.local",
        "ARC_ADMIN_PASSWORD": "test-password",
        "LANGFUSE_PUBLIC_KEY": "",
        "LANGFUSE_SECRET_KEY": "",
    })
    from arc_tower.db.session import init_db
    init_db()
    return home
