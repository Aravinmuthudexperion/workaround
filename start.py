#!/usr/bin/env python3
"""ARC Tower launcher - native, no Docker. Works on Windows, macOS and Linux.

    python start.py setup      create .venv, install packages, download binaries, create .env, init DB
    python start.py up         start every component (logs in run/logs)
    python start.py status     show what is running
    python start.py restart    restart everything (or --only arc-app,ingest-proxy)
    python start.py down       stop everything
    python start.py logs <svc> show the end of one service's log
    python start.py demo       create a demo tenant/product and run the sample agent
    python start.py bundle     build an offline zip for another OS (default: windows-amd64)
"""
from arc_tower.launcher.cli import main

if __name__ == "__main__":
    main()
