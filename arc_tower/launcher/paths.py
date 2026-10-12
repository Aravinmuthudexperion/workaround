"""Filesystem layout of an ARC Tower installation."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENV = ROOT / ".venv"
VENDOR = ROOT / "vendor"
DOWNLOAD_CACHE = VENDOR / "_downloads"
WHEELS = ROOT / "wheels"
DIST = ROOT / "dist"
RUN = ROOT / "run"
CONFIG_OUT = RUN / "config"
DATA = RUN / "data"
LOGS = RUN / "logs"
PIDS = RUN / "pids"
ENV_FILE = ROOT / ".env"
ENV_TEMPLATE = ROOT / ".env.example"
DEPLOY_TEMPLATES = ROOT / "deploy" / "templates"

IS_WINDOWS = os.name == "nt"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def ensure_run_dirs() -> None:
    for directory in (CONFIG_OUT, DATA, LOGS, PIDS):
        directory.mkdir(parents=True, exist_ok=True)
