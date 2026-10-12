# Local test Langfuse (development only)

A self-hosted Langfuse used to test ARC Tower's trace store integration on a developer
machine. It needs a container runtime (Colima or Docker). The PoC and production use
Langfuse Cloud (or your organisation's Langfuse) instead - then this folder is not needed.

    colima start --cpu 4 --memory 8
    docker compose -p langfuse --env-file test.env up -d   # UI: http://localhost:3300
    docker compose -p langfuse --env-file test.env down   # stop (add -v to delete all traces)

Login: admin@arc.local / arc-test-password. Project keys pk-lf-arc-test / sk-lf-arc-test,
which ARC Tower's .env uses with LANGFUSE_HOST=http://localhost:3300.
Ports are remapped (3300, 9190, 9191) so they do not collide with Grafana and Prometheus.

The credentials in test.env are for this localhost-only test instance. Never reuse them anywhere else.
