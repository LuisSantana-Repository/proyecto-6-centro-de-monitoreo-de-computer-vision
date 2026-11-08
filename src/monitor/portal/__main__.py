"""Arranca el portal.

Uso: uv run python -m monitor.portal [--config config/monitor.yaml] [--port 8000]
"""

from __future__ import annotations

import argparse

import uvicorn

from monitor.config import load_config
from monitor.portal.app import create_app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/monitor.yaml")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    cfg = load_config(args.config)
    app = create_app(cfg["output"]["db"], root=".")
    print(f"Portal en http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
