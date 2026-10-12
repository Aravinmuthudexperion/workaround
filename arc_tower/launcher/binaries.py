"""Download, extract and locate the native component binaries."""
from __future__ import annotations

import os
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

from .output import log
from .paths import DOWNLOAD_CACHE, IS_WINDOWS, ROOT, VENDOR
from .platforms import BINARY_NAMES, current_platform, download_urls


def vendor_dir(os_name: str, arch: str) -> Path:
    return VENDOR / f"{os_name}-{arch}"


def _matches(component: str, path: Path) -> bool:
    name = path.name.lower()
    stem = name[:-4] if name.endswith(".exe") else name
    if component == "grafana":
        # Grafana ships bin/grafana plus grafana-server / grafana-cli shims; we want bin/grafana.
        return stem == "grafana" and path.parent.name == "bin"
    if component == "loki":
        return stem.startswith("loki")
    return stem == BINARY_NAMES[component]


def find_binary(component: str, os_name: str | None = None, arch: str | None = None) -> Path | None:
    if os_name is None or arch is None:
        os_name, arch = current_platform()
    base = vendor_dir(os_name, arch) / component
    if not base.exists():
        return None
    for path in sorted(base.rglob("*")):
        if path.is_file() and _matches(component, path):
            return path
    return None


def grafana_home() -> Path | None:
    binary = find_binary("grafana")
    return binary.parent.parent if binary else None


def _fetch(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    log(f"downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "arc-tower-setup"})
    with urllib.request.urlopen(request, timeout=120) as resp, open(partial, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done, last_report = 0, 0.0
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if total and time.time() - last_report > 2:
                last_report = time.time()
                log(f"  {done * 100 // total:3d}%  {done >> 20}/{total >> 20} MB")
    partial.replace(dest)


def _extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    else:
        with tarfile.open(archive) as tf:
            try:
                tf.extractall(dest, filter="data")
            except TypeError:  # Python < 3.12 has no extraction filters
                tf.extractall(dest)
    if not IS_WINDOWS:
        for path in dest.rglob("*"):
            if path.is_file() and (path.suffix == "" or path.parent.name == "bin"):
                path.chmod(path.stat().st_mode | 0o111)


def download_binaries(os_name: str, arch: str) -> None:
    for component, url in download_urls(os_name, arch).items():
        if find_binary(component, os_name, arch):
            log(f"{component}: already present")
            continue
        archive = DOWNLOAD_CACHE / url.rsplit("/", 1)[-1]
        if not archive.exists():
            _fetch(url, archive)
        _extract(archive, vendor_dir(os_name, arch) / component)
        found = find_binary(component, os_name, arch)
        if not found:
            raise SystemExit(f"could not find the {component} binary after extracting {archive}")
        log(f"{component}: {os.path.relpath(found, ROOT)}")
