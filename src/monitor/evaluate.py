"""Recalcula ventanas y alertas de una ejecución a partir de su events.jsonl y las compara con lo registrado.

Sirve para verificar (M5 / R7.2) que las métricas por ventana y las alertas se reproducen desde los
datos primarios con la configuración guardada en el manifest.

Uso: uv run python -m monitor.evaluate datos/runs/<run_id> [...]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from monitor.alerts import WindowEvaluator
from monitor.events import InferenceEvent


def recompute(run_dir: Path) -> tuple[list[dict], list[dict]]:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    ev = WindowEvaluator(manifest["config"], manifest["config_ref"], manifest["run_id"])
    windows, alerts = [], []
    results = []
    for line in open(run_dir / "events.jsonl", encoding="utf-8"):
        results.append(ev.add(InferenceEvent.model_validate_json(line)))
    results.append(ev.flush())
    for win, new, _ in results:
        if win is not None:
            windows.append(win.record())
        alerts += new
    return windows, alerts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    args = ap.parse_args()
    ok_all = True
    for r in args.runs:
        r = Path(r)
        windows, alerts = recompute(r)
        logged_w = [json.loads(l) for l in open(r / "windows.jsonl", encoding="utf-8")] if (r / "windows.jsonl").exists() else []
        logged_a = [json.loads(l) for l in open(r / "alerts.jsonl", encoding="utf-8")] if (r / "alerts.jsonl").exists() else []
        logged_a = [a for a in logged_a if a["type"] == "alert"]
        keys = ("window_id", "n_valid", "n_eligible", "n_low", "low_ratio", "mean_score", "state")
        w_ok = [{k: w[k] for k in keys} for w in windows] == [{k: w[k] for k in keys} for w in logged_w]
        a_ok = [(a["alert_id"], a["rule_id"], a["event_ids"]) for a in alerts] == \
               [(a["alert_id"], a["rule_id"], a["event_ids"]) for a in logged_a]
        ok_all &= w_ok and a_ok
        print(f"{r.name}: ventanas recalculadas={len(windows)} registradas={len(logged_w)} coinciden={w_ok} | "
              f"alertas recalculadas={len(alerts)} registradas={len(logged_a)} coinciden={a_ok}")
        for w in windows:
            lr = f"{w['low_ratio']:.4f}" if w["low_ratio"] is not None else "   -  "
            ms = f"{w['mean_score']:.4f}" if w["mean_score"] is not None else "   -  "
            print(f"   ventana {w['idx']:3d} {w['state']:15s} low_ratio={lr} mean_score={ms} elegibles={w['n_eligible']}")
        for a in alerts:
            print(f"   alerta {a['alert_id']} ({a['severity']})")
    raise SystemExit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
