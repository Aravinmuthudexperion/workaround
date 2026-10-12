"""Argument parsing for `python -m arc_tower.cli`."""
from __future__ import annotations

import argparse

from arc_tower.core.logging import configure_logging

from . import bootstrap, demo, manage


def main(argv: list[str] | None = None) -> None:
    configure_logging("cli")
    parser = argparse.ArgumentParser(prog="arc_tower.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create tables, seed KPIs and the admin user").set_defaults(
        func=lambda a: bootstrap.init_db())
    sub.add_parser("render-configs", help="render component configs into run/config").set_defaults(
        func=lambda a: bootstrap.render_configs())
    sub.add_parser("bootstrap", help="provision Grafana orgs, users and alert rules").set_defaults(
        func=lambda a: bootstrap.bootstrap())

    p = sub.add_parser("demo", help="create demo tenant/products and run the sample agent")
    p.add_argument("--minutes", type=float, default=5)
    p.add_argument("--no-agent", action="store_true", help="only create the demo data")
    p.set_defaults(func=lambda a: demo.run(a.minutes, run_agent=not a.no_agent))

    p = sub.add_parser("create-tenant")
    p.add_argument("name")
    p.add_argument("--slug")
    p.set_defaults(func=lambda a: manage.create_tenant(a.name, a.slug))

    p = sub.add_parser("create-user")
    p.add_argument("email")
    p.add_argument("--name", default="")
    p.add_argument("--password")
    p.add_argument("--tenant", help="tenant slug to add the user to")
    p.add_argument("--role", default="viewer", choices=["viewer", "editor", "tenant_admin"])
    p.add_argument("--platform-admin", action="store_true")
    p.add_argument("--executive", action="store_true")
    p.set_defaults(func=lambda a: manage.create_user(a))

    args = parser.parse_args(argv)
    args.func(args)
