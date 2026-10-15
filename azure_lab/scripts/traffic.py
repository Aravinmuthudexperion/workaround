#!/usr/bin/env python3
"""Generate realistic usage on both lab products so Azure has metrics, traces and cost to report.

    python3 scripts/traffic.py                       # 20 requests per product, 3 s apart
    python3 scripts/traffic.py --count 100 --interval 1
    python3 scripts/traffic.py --only warranty

Standard library only. Reads URLs and keys from azure_lab/.lab.env (written by scripts/deploy.sh).
"""
from __future__ import annotations

import argparse
import json
import random
import shlex
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / ".lab.env"

ROLES = ["Backend Engineer", "Data Analyst", "Product Manager", "DevOps Engineer"]
RESUMES = [
    "6 years Python/Go, built payment APIs handling 2k rps at a fintech, led migration to Kubernetes, AWS certified.",
    "Fresh graduate, B.Tech CSE 2025, internship building dashboards in Power BI, SQL and pandas projects.",
    "8 years product management in e-commerce, launched checkout redesign (+12% conversion), gap 2021-2023.",
    "4 years SRE, Terraform, Azure, on-call lead, cut incident MTTR by 40%, wrote runbooks and SLOs.",
    "Teacher for 10 years, completed a 6-month data analytics bootcamp, Excel and Tableau portfolio.",
]
PRODUCTS = ["Aero X1 laptop", "Pulse earbuds", "Nimbus 55\" TV", "Vector smartphone", "Orbit smartwatch"]
ISSUES = [
    "Screen flickers after the latest update.",
    "Battery drains from full to empty in two hours.",
    "Dropped it in water, now it won't turn on.",
    "Left earbud has no sound since last week.",
    "Device restarts randomly several times a day.",
    "Hinge cracked while opening the lid normally.",
]


def load_state() -> dict[str, str]:
    if not STATE.exists():
        raise SystemExit("azure_lab/.lab.env not found: run scripts/deploy.sh first")
    state = {}
    for line in STATE.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            state[key] = shlex.split(value)[0] if value else ""
    return state


def post(url: str, body: dict, headers: dict[str, str]) -> tuple[int, str]:
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.status, resp.read().decode()[:160]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:160]
    except (urllib.error.URLError, TimeoutError) as exc:
        return 0, str(exc)


def resumex(state: dict[str, str]) -> tuple[int, str]:
    return post(f"{state['RESUMEX_URL']}/api/screen?code={state.get('RESUMEX_FUNCTION_KEY', '')}",
                {"candidate": f"cand-{random.randint(1000, 9999)}", "role": random.choice(ROLES),
                 "resume": random.choice(RESUMES)}, {})


def warranty(state: dict[str, str]) -> tuple[int, str]:
    bought = date.today() - timedelta(days=random.randint(20, 600))
    return post(f"{state['WARRANTY_URL']}/claims",
                {"customer": f"cust-{random.randint(1000, 9999)}", "product": random.choice(PRODUCTS),
                 "purchase_date": bought.isoformat(), "issue": random.choice(ISSUES)},
                {"x-api-key": state["WARRANTY_API_KEY"]})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=20, help="requests per product")
    parser.add_argument("--interval", type=float, default=3.0, help="seconds between rounds")
    parser.add_argument("--only", choices=["resumex", "warranty"])
    args = parser.parse_args()
    state = load_state()
    targets = {"resumex": resumex, "warranty": warranty}
    if args.only:
        targets = {args.only: targets[args.only]}
    ok = {name: 0 for name in targets}
    for i in range(1, args.count + 1):
        for name, call in targets.items():
            status, text = call(state)
            ok[name] += status == 200
            print(f"{i:>3} {name:<8} {status} {text}")
        time.sleep(args.interval)
    print("succeeded:", ", ".join(f"{k} {v}/{args.count}" for k, v in ok.items()))


if __name__ == "__main__":
    main()
