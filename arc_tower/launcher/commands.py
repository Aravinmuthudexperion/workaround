"""Implementations of the start.py sub-commands."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

from . import process
from .binaries import download_binaries, vendor_dir
from .envfile import child_env, read_env, write_env_if_missing
from .output import log
from .paths import DIST, ENV_FILE, ROOT, VENV, WHEELS, ensure_run_dirs, venv_python
from .platforms import WHEEL_PLATFORMS, current_platform, parse_platform
from arc_tower.core.ports import PORTS

from .services import all_services

# Files and folders that never go into an offline bundle.
_BUNDLE_SKIP = {".venv", "run", "dist", "vendor", ".env", "wheels", ".pytest_cache", ".git"}
# agent.env holds a real ingest key written by `start.py demo` - never ship it.
_BUNDLE_SKIP_PATTERNS = ("__pycache__", "*.pyc", "*.egg-info", "*.env")


def _run_app_cli(*args: str, check: bool = True) -> int:
    cmd = [str(venv_python()), "-m", "arc_tower.cli", *args]
    return subprocess.run(cmd, cwd=ROOT, env=child_env(), check=check).returncode


def _ensure_pip() -> None:
    """venvs created by other tools (e.g. uv) may not include pip."""
    if subprocess.run([str(venv_python()), "-m", "pip", "--version"], capture_output=True).returncode != 0:
        subprocess.run([str(venv_python()), "-m", "ensurepip", "--upgrade"], check=True)


def setup(args: argparse.Namespace) -> None:
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 or newer is required")
    if not venv_python().exists():
        log("creating .venv")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    _ensure_pip()
    pip = [str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check"]
    if WHEELS.exists():
        log("installing Python packages from the offline wheels/ folder")
        pip += ["--no-index", "--find-links", str(WHEELS)]
    else:
        log("installing Python packages")
        subprocess.run([*pip, "--upgrade", "pip"], check=False)
    extras = ",".join(filter(None, ["agent", "dev", args.extras]))
    subprocess.run([*pip, "-e", f".[{extras}]"], cwd=ROOT, check=True)

    download_binaries(*current_platform())
    write_env_if_missing()
    ensure_run_dirs()
    _run_app_cli("init-db")
    env = read_env()
    log("setup complete")
    log(f"ARC admin login: {env.get('ARC_ADMIN_EMAIL')} / {env.get('ARC_ADMIN_PASSWORD')}")
    log("next: python start.py up")


def up(args: argparse.Namespace) -> None:
    if not venv_python().exists() or not ENV_FILE.exists():
        raise SystemExit("run `python start.py setup` first")
    ensure_run_dirs()
    log("rendering component configs")
    _run_app_cli("render-configs")
    env = child_env()
    wanted = set(args.only.split(",")) if args.only else None
    for svc in all_services():
        if not wanted or svc.name in wanted:
            process.start(svc, env)
    _run_app_cli("bootstrap", check=False)
    values = read_env()
    port = values.get("ARC_PORT", "8000")
    log("")
    log(f"ARC Tower     http://localhost:{port}   ({values.get('ARC_ADMIN_EMAIL')} / {values.get('ARC_ADMIN_PASSWORD')})")
    log(f"OTLP ingest   {values.get('ARC_PUBLIC_INGEST_URL', 'http://localhost:4318/otlp')}")
    log(f"Grafana       http://localhost:{port}/grafana/  (signed in through ARC)")
    log(f"Prometheus    http://127.0.0.1:{PORTS['prometheus']}")


def down(args: argparse.Namespace) -> None:
    for svc in reversed(all_services()):
        if process.stop(svc):
            log(f"{svc.name}: stopped")


def restart(args: argparse.Namespace) -> None:
    """Stop and start selected services (or all), e.g. after a code or .env change."""
    wanted = set(args.only.split(",")) if args.only else None
    for svc in reversed(all_services()):
        if (not wanted or svc.name in wanted) and process.stop(svc):
            log(f"{svc.name}: stopped")
    up(args)


def status(args: argparse.Namespace) -> None:
    for svc in all_services():
        pid = process.running_pid(svc)
        state = "running" if pid else "stopped"
        health = ("healthy" if process.is_healthy(svc) else "unhealthy") if pid else ""
        print(f"  {svc.name:<18} {state:<8} {health:<10} {f'pid {pid}' if pid else ''}")


def logs(args: argparse.Namespace) -> None:
    svc = next((s for s in all_services() if s.name == args.service), None)
    if not svc or not svc.log_file.exists():
        names = ", ".join(s.name for s in all_services())
        raise SystemExit(f"no log for {args.service}; services: {names}")
    lines = svc.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-args.lines:]))


def demo(args: argparse.Namespace) -> None:
    _run_app_cli("demo", "--minutes", str(args.minutes))


def _bundle_requirements(extras: str) -> list[str]:
    """Runtime dependencies from pyproject.toml plus the agent/dev extras (and any requested ones)."""
    import tomllib  # Python 3.11+; bundling runs on the build machine
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    wanted = ["agent", "dev", *filter(None, extras.split(","))]
    deps = list(project["dependencies"])
    for extra in wanted:
        deps += project.get("optional-dependencies", {}).get(extra, [])
    return ["setuptools>=68", "wheel", "pip", *dict.fromkeys(deps)]


def bundle(args: argparse.Namespace) -> None:
    """Offline zip for another machine: code + that OS's binaries + that OS's Python wheels."""
    os_name, arch = parse_platform(args.target)
    download_binaries(os_name, arch)
    stage = DIST / f"arc-tower-{os_name}-{arch}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    ignore = shutil.ignore_patterns(*_BUNDLE_SKIP, *_BUNDLE_SKIP_PATTERNS)
    skip_top = ignore(str(ROOT), [p.name for p in ROOT.iterdir()])
    for item in ROOT.iterdir():
        if item.name in skip_top or item.suffix in (".pptx", ".png"):
            continue
        if item.is_dir():
            shutil.copytree(item, stage / item.name, ignore=ignore)
        else:
            shutil.copy2(item, stage / item.name)
    shutil.copytree(vendor_dir(os_name, arch), stage / "vendor" / f"{os_name}-{arch}")
    wheel_platform = WHEEL_PLATFORMS[f"{os_name}-{arch}"]
    requirements = stage / "wheels" / "requirements.txt"
    requirements.parent.mkdir()
    requirements.write_text("\n".join(_bundle_requirements(args.extras)) + "\n", encoding="utf-8")
    _ensure_pip()
    log(f"downloading Python {args.python} wheels for {wheel_platform}")
    # Dependencies only: the project itself is installed from source on the target (pip install -e .),
    # which needs setuptools + wheel from this folder for its build.
    subprocess.run([str(venv_python()), "-m", "pip", "download", "--disable-pip-version-check",
                    "--only-binary=:all:", "--platform", wheel_platform, "--python-version", args.python,
                    "-d", str(stage / "wheels"), "-r", str(requirements)], check=True)
    archive = shutil.make_archive(str(stage), "zip", stage.parent, stage.name)
    log(f"bundle ready: {archive}")
