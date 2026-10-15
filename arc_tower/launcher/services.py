"""The ARC Tower processes, in start order."""
from __future__ import annotations

from arc_tower.core.ports import PORTS

from .binaries import find_binary, grafana_home
from .envfile import read_env
from .paths import CONFIG_OUT, DATA, venv_python
from .process import Service


def _binary(component: str) -> str:
    path = find_binary(component)
    if not path:
        raise SystemExit(f"{component} binary missing - run: python start.py setup")
    return str(path)


def infrastructure() -> list[Service]:
    cfg = CONFIG_OUT
    return [
        Service("loki", [_binary("loki"), f"-config.file={cfg / 'loki.yaml'}"],
                health_url=f"http://127.0.0.1:{PORTS['loki']}/ready"),
        Service("prometheus", [_binary("prometheus"), f"--config.file={cfg / 'prometheus.yml'}",
                               f"--storage.tsdb.path={DATA / 'prometheus'}",
                               f"--web.listen-address=127.0.0.1:{PORTS['prometheus']}",
                               "--web.enable-otlp-receiver", "--web.enable-lifecycle",
                               # `increase(x[w] anchored)`: exact counts for dashboards (no extrapolation)
                               "--enable-feature=promql-extended-range-selectors,promql-experimental-functions",
                               "--storage.tsdb.retention.time=90d"],
                health_url=f"http://127.0.0.1:{PORTS['prometheus']}/-/ready"),
        Service("prom-label-proxy", [_binary("prom-label-proxy"), "-label=arc_tenant_id",
                                     f"-upstream=http://127.0.0.1:{PORTS['prometheus']}",
                                     f"-insecure-listen-address=127.0.0.1:{PORTS['prom_label_proxy']}",
                                     "-enable-label-apis",
                                     "-enable-promql-extended-range-selectors",    # same PromQL as Prometheus
                                     "-enable-promql-experimental-functions"],
                port=PORTS["prom_label_proxy"]),
        Service("otel-collector", [_binary("otelcol"), f"--config={cfg / 'otel-collector.yaml'}"],
                health_url=f"http://127.0.0.1:{PORTS['collector_health']}/"),
        Service("grafana", [_binary("grafana"), "server", f"--homepath={grafana_home()}",
                            f"--config={cfg / 'grafana.ini'}"],
                health_url=f"http://127.0.0.1:{PORTS['grafana']}/grafana/api/health",
                cwd=grafana_home()),
    ]


def applications() -> list[Service]:
    env = read_env()
    py = str(venv_python())
    arc_port = env.get("ARC_PORT", "8000")
    proxy_port = env.get("PROXY_PORT", "4318")
    return [
        Service("arc-app", [py, "-m", "uvicorn", "arc_tower.web.app:app", "--host", "127.0.0.1",
                            "--port", arc_port, "--proxy-headers"],
                health_url=f"http://127.0.0.1:{arc_port}/healthz"),
        Service("ingest-proxy", [py, "-m", "uvicorn", "arc_tower.proxy.app:app",
                                 "--host", env.get("PROXY_HOST", "127.0.0.1"), "--port", proxy_port],
                health_url=f"http://127.0.0.1:{proxy_port}/healthz"),
        Service("connectors", [py, "-m", "arc_tower.connectors.worker"]),
        Service("intelligence", [py, "-m", "arc_tower.intelligence.app"],
                health_url=f"http://127.0.0.1:{PORTS['intelligence']}/metrics"),
    ]


def all_services() -> list[Service]:
    return infrastructure() + applications()
