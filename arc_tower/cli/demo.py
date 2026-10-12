"""`start.py demo`: a demo tenant with a push product (sample agent) and a pull product (demo platform)."""
from __future__ import annotations

import logging
import os
import subprocess
import sys

from arc_tower.core.config import get_settings
from arc_tower.core.constants import Role
from arc_tower.db.session import session_scope
from arc_tower.services import alerts, connectors, kpis, products, tenants, users
from arc_tower.services.errors import ValidationFailed

log = logging.getLogger("arc.demo")

TENANT = ("Demo Insurance", "demo-insurance")
PUSH_PRODUCT = "Claims Assistant"
PULL_PRODUCT = "IT Service Desk"
DEMO_USERS = [
    ("analyst@demo.local", "Dana Analyst", Role.VIEWER),
    ("lead@demo.local", "Lee Lead", Role.TENANT_ADMIN),
]
DEMO_PASSWORD = "demo1234"
DEFAULT_TARGETS = {
    "error_rate": {"threshold": 8.0, "alert": True, "severity": "critical"},
    "p95_latency_ms": {"threshold": 12000.0, "alert": True, "severity": "warning"},
    "cost_usd": {"threshold": 25.0, "alert": True, "severity": "warning"},
    "pii_masked": {"threshold": 50.0, "alert": True, "severity": "warning"},
}


def _setup() -> str:
    """Create (or reuse) the demo data; returns the agent's env block with a fresh ingest key."""
    with session_scope() as session:
        tenant = tenants.get_by_slug(session, TENANT[1]) or tenants.create_tenant(session, "demo", *TENANT)
        for email, name, role in DEMO_USERS:
            user = users.get_by_email(session, email) or users.create_user(session, "demo", email, name,
                                                                             DEMO_PASSWORD)
            users.set_membership(session, "demo", user, tenant, role)

        push = next((p for p in tenant.products if p.name == PUSH_PRODUCT), None)
        if push is None:
            push = products.create_product(session, "demo", tenant, PUSH_PRODUCT, "otel_custom",
                                           "Customer-facing claims chatbot (LangGraph).")
            kpis.save_targets(session, "demo", push, DEFAULT_TARGETS)
        products.advance_onboarding(push, 3)
        issued = products.issue_ingest_key(session, "demo", push, revoke_existing=True)

        pull = next((p for p in tenant.products if p.name == PULL_PRODUCT), None)
        if pull is None:
            pull = products.create_product(session, "demo", tenant, PULL_PRODUCT, "demo_platform",
                                           "Now Assist-style IT help desk agents, pulled from the platform.")
            connectors.configure(session, "demo", pull, {"agents": "it-helpdesk-agent,hr-policy-agent"},
                                 interval_seconds=60)
            products.advance_onboarding(pull, 3)
        try:
            alerts.sync_product_rules(session, push)
        except ValidationFailed as exc:
            log.warning("alert rules not synced: %s", exc)
        env = products.env_block(push, issued.key).replace(f"OTEL_SERVICE_NAME={push.slug}-agent",
                                                           "OTEL_SERVICE_NAME=claims-assistant")
    return env


def run(minutes: float, run_agent: bool = True) -> None:
    settings = get_settings()
    env_block = _setup()
    env_file = settings.home / "sample_agent" / "agent.env"
    env_file.write_text(env_block + "\n", encoding="utf-8")
    log.info("demo tenant ready - users %s (password %s)", ", ".join(u[0] for u in DEMO_USERS), DEMO_PASSWORD)
    log.info("agent env written to %s", env_file)
    if not run_agent:
        return
    agent_env = dict(os.environ)
    for line in env_block.splitlines():
        key, _, value = line.partition("=")
        agent_env[key] = value
    log.info("running the sample agent for %.1f minutes (Ctrl+C to stop)", minutes)
    try:
        subprocess.run([sys.executable, str(settings.home / "sample_agent" / "claims_agent.py"),
                        "--minutes", str(minutes), "--rate", "2"], env=agent_env, check=False,
                       stdin=subprocess.DEVNULL)
    except KeyboardInterrupt:
        log.info("stopped")
