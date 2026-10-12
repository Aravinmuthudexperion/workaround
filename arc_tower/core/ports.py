"""Fixed local ports of the internal components. Standard library only (the launcher imports it).

Everything here binds to 127.0.0.1; only the ARC app and the ingest proxy are user-facing,
and their ports come from .env (ARC_PORT, PROXY_PORT).
"""

PORTS = {
    "loki": 3100,
    "loki_grpc": 19095,
    "prometheus": 9090,
    "prom_label_proxy": 9091,
    "collector_otlp": 14318,
    "collector_health": 13133,
    "grafana": 3000,
    "intelligence": 9464,
}
