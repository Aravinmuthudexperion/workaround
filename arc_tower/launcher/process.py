"""Cross-platform background process management with pid files and health checks."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .output import log
from .paths import IS_WINDOWS, LOGS, PIDS, ROOT

_CREATE_NO_WINDOW = 0x08000000


@dataclass
class Service:
    name: str
    cmd: list[str]
    health_url: str | None = None
    port: int | None = None
    cwd: Path = field(default=ROOT)

    @property
    def pid_file(self) -> Path:
        return PIDS / f"{self.name}.pid"

    @property
    def log_file(self) -> Path:
        return LOGS / f"{self.name}.log"


def pid_alive(pid: int) -> bool:
    try:
        import psutil  # optional: more reliable, available once the venv exists
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except ImportError:
        pass
    except Exception:
        return False
    if IS_WINDOWS:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def running_pid(svc: Service) -> int | None:
    try:
        pid = int(svc.pid_file.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def is_healthy(svc: Service) -> bool:
    if svc.health_url:
        try:
            with urllib.request.urlopen(svc.health_url, timeout=2) as resp:
                return resp.status < 500
        except urllib.error.HTTPError as exc:
            return exc.code < 500
        except Exception:
            return False
    if svc.port:
        with socket.socket() as sock:
            sock.settimeout(1)
            return sock.connect_ex(("127.0.0.1", svc.port)) == 0
    return running_pid(svc) is not None


def start(svc: Service, env: dict[str, str], timeout: float = 90) -> None:
    if running_pid(svc):
        log(f"{svc.name}: already running")
        return
    LOGS.mkdir(parents=True, exist_ok=True)
    PIDS.mkdir(parents=True, exist_ok=True)
    log_handle = open(svc.log_file, "ab")
    kwargs: dict = {"cwd": svc.cwd, "env": env, "stdout": log_handle, "stderr": subprocess.STDOUT,
                    "stdin": subprocess.DEVNULL}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(svc.cmd, **kwargs)
    svc.pid_file.write_text(str(proc.pid))
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"{svc.name} exited with code {proc.returncode} - see {svc.log_file}")
        if is_healthy(svc):
            log(f"{svc.name}: up (pid {proc.pid})")
            return
        time.sleep(1)
    log(f"{svc.name}: started (pid {proc.pid}) but not healthy yet - see {svc.log_file}")


def stop(svc: Service) -> bool:
    pid = running_pid(svc)
    svc.pid_file.unlink(missing_ok=True)
    if not pid:
        return False
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return True
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        return True
    for _ in range(50):
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
    return True
