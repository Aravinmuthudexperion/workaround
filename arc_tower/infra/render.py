"""Render the Collector, Prometheus, Loki and Grafana configs into run/config.

Paths are written with forward slashes, which every component accepts on Windows too.
"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from arc_tower.core.config import get_settings
from arc_tower.core.ports import PORTS
from arc_tower.core.security import basic_auth

TEMPLATES = {
    "otel-collector.yaml.j2": "otel-collector.yaml",
    "prometheus.yml.j2": "prometheus.yml",
    "loki.yaml.j2": "loki.yaml",
    "grafana.ini.j2": "grafana.ini",
}


def _context() -> dict:
    settings = get_settings()
    data = settings.run_dir / "data"
    langfuse_auth = (basic_auth(settings.langfuse_public_key, settings.langfuse_secret_key)
                     if settings.langfuse_enabled else "")
    return {
        "ports": PORTS,
        "arc_base_url": settings.base_url,
        "paths": {
            "loki": (data / "loki").as_posix(),
            "grafana": (data / "grafana").as_posix(),
            "grafana_provisioning": (settings.run_dir / "config" / "grafana-provisioning").as_posix(),
        },
        "langfuse": {
            # A Langfuse host alone is enough to export: per-product keys arrive with each request.
            "enabled": bool(settings.langfuse_host) and (settings.langfuse_enabled or settings.langfuse_managed),
            "host": settings.langfuse_host,
            "default_auth": langfuse_auth,
        },
        "grafana": {
            "admin_user": settings.grafana_admin_user,
            "admin_password": settings.grafana_admin_password,
        },
    }


def render_all(out_dir: Path | None = None) -> list[Path]:
    settings = get_settings()
    out_dir = out_dir or settings.run_dir / "config"
    out_dir.mkdir(parents=True, exist_ok=True)
    context = _context()
    for key in ("loki", "grafana"):
        Path(context["paths"][key]).mkdir(parents=True, exist_ok=True)
    # Grafana logs an error for each missing provisioning folder; ARC provisions through the API instead.
    for sub in ("dashboards", "datasources", "alerting", "plugins"):
        (Path(context["paths"]["grafana_provisioning"]) / sub).mkdir(parents=True, exist_ok=True)
    env = Environment(loader=FileSystemLoader(settings.home / "deploy" / "templates"),
                      undefined=StrictUndefined, keep_trailing_newline=True)
    written = []
    for template_name, output_name in TEMPLATES.items():
        target = out_dir / output_name
        target.write_text(env.get_template(template_name).render(**context), encoding="utf-8")
        written.append(target)
    return written
