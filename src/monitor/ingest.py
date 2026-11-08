"""Importa runs existentes (manifest.json + events.jsonl) a la base SQLite. Idempotente.

Uso:
  uv run python -m monitor.ingest                     # todos los runs de datos/runs
  uv run python -m monitor.ingest datos/runs/<run_id> # uno o varios runs
"""

from __future__ import annotations

import argparse
from pathlib import Path

from monitor.config import load_config
from monitor.store import Store


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="*", help="carpetas de runs (default: todas las de output.runs_dir)")
    ap.add_argument("--config", default="config/monitor.yaml")
    args = ap.parse_args()
    cfg = load_config(args.config)
    runs = [Path(r) for r in args.runs] or sorted(
        p for p in Path(cfg["output"]["runs_dir"]).iterdir() if (p / "manifest.json").exists()
    )
    store = Store(cfg["output"]["db"])
    for r in runs:
        print(store.ingest_run_dir(r))
    store.close()


if __name__ == "__main__":
    main()
