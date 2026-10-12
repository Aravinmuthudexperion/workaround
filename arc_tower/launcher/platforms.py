"""Pinned component versions and their per-platform download URLs."""
from __future__ import annotations

import platform

VERSIONS = {
    "otelcol": "0.161.0",
    "prometheus": "3.15.0",
    "loki": "3.7.8",
    "grafana": "13.2.2",
    "prom-label-proxy": "0.15.1",
}

BINARY_NAMES = {
    "otelcol": "otelcol-contrib",
    "prometheus": "prometheus",
    "loki": "loki",
    "grafana": "grafana",
    "prom-label-proxy": "prom-label-proxy",
}

# pip --platform tags used when bundling wheels for another machine.
WHEEL_PLATFORMS = {
    "windows-amd64": "win_amd64",
    "windows-arm64": "win_arm64",
    "linux-amd64": "manylinux2014_x86_64",
    "darwin-arm64": "macosx_11_0_arm64",
    "darwin-amd64": "macosx_10_12_x86_64",
}


def current_platform() -> tuple[str, str]:
    """(os, arch) in the naming the upstream releases use."""
    os_name = {"darwin": "darwin", "windows": "windows", "linux": "linux"}[platform.system().lower()]
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "amd64"
    return os_name, arch


def parse_platform(spec: str | None) -> tuple[str, str]:
    if not spec:
        return current_platform()
    os_name, arch = spec.split("-", 1)
    return os_name, arch


def download_urls(os_name: str, arch: str) -> dict[str, str]:
    v = VERSIONS
    win = os_name == "windows"
    gh = "https://github.com"
    return {
        "otelcol": f"{gh}/open-telemetry/opentelemetry-collector-releases/releases/download/"
                   f"v{v['otelcol']}/otelcol-contrib_{v['otelcol']}_{os_name}_{arch}.tar.gz",
        "prometheus": f"{gh}/prometheus/prometheus/releases/download/"
                      f"v{v['prometheus']}/prometheus-{v['prometheus']}.{os_name}-{arch}.tar.gz",
        "loki": f"{gh}/grafana/loki/releases/download/"
                f"v{v['loki']}/loki-{os_name}-{arch}{'.exe' if win else ''}.zip",
        "grafana": f"https://dl.grafana.com/oss/release/grafana-{v['grafana']}.{os_name}-{arch}."
                   f"{'zip' if win else 'tar.gz'}",
        "prom-label-proxy": f"{gh}/prometheus-community/prom-label-proxy/releases/download/"
                            f"v{v['prom-label-proxy']}/prom-label-proxy-{v['prom-label-proxy']}.{os_name}-{arch}.tar.gz",
    }
