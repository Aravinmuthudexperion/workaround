"""Argument parsing for start.py."""
from __future__ import annotations

import argparse

from . import commands


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="start.py", description="ARC Tower launcher (native, no Docker)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="create .venv, install packages, download binaries, init DB")
    p.add_argument("--extras", default="", help="connector extras, e.g. azure,aws,oidc")
    p.set_defaults(func=commands.setup)

    p = sub.add_parser("up", help="start all components")
    p.add_argument("--only", help="comma-separated service names")
    p.set_defaults(func=commands.up)

    p = sub.add_parser("restart", help="restart all (or selected) components")
    p.add_argument("--only", help="comma-separated service names")
    p.set_defaults(func=commands.restart)

    sub.add_parser("down", help="stop all components").set_defaults(func=commands.down)
    sub.add_parser("status", help="show component status").set_defaults(func=commands.status)

    p = sub.add_parser("logs", help="show the end of a service log")
    p.add_argument("service")
    p.add_argument("-n", "--lines", type=int, default=60)
    p.set_defaults(func=commands.logs)

    p = sub.add_parser("demo", help="create a demo tenant/product and run the sample agent")
    p.add_argument("--minutes", type=float, default=5)
    p.set_defaults(func=commands.demo)

    p = sub.add_parser("bundle", help="build an offline zip for another OS")
    p.add_argument("--target", default="windows-amd64")
    p.add_argument("--python", default="3.12", help="Python version on the target machine")
    p.add_argument("--extras", default="", help="extra connector deps to include, e.g. azure,aws,oidc")
    p.set_defaults(func=commands.bundle)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)
