"""Recalcula ventanas y alertas de una ejecución a partir de su events.jsonl y las compara con lo registrado.

Sirve para verificar (M5 / R7.2) que las métricas por ventana y las alertas se reproducen desde los
datos primarios con la configuración guardada en el manifest.

Uso: uv run python -m monitor.evaluate datos/runs/<run_id> [...]
     uv run python -m monitor.evaluate datos/runs/<run_s2> --sin-reaccion-version   # contrafactual de S2
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from monitor.alerts import InferenceHealthMonitor, ModelVersionMonitor, SourceHealthMonitor, WindowEvaluator
from monitor.events import InferenceEvent
from monitor.telemetry import TelemetryMonitor


def recompute(run_dir: Path, without_version_reaction: bool = False) -> tuple[list[dict], list[dict]]:
    """Ventanas y alertas (reglas de confianza por ventana + reglas de la fuente, de versión y de inferencia).

    `without_version_reaction` (contrafactual del escenario S2): recalcula como si las ventanas con eventos
    de otra versión del modelo se evaluaran contra la referencia (`model_version.exclude_windows: false`)."""
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    cfg = manifest["config"]
    if without_version_reaction and cfg.get("model_version"):
        cfg = copy.deepcopy(cfg)
        cfg["model_version"]["exclude_windows"] = False
    args = (cfg, manifest["config_ref"], manifest["run_id"])
    ev, health, version = WindowEvaluator(*args), SourceHealthMonitor(*args), ModelVersionMonitor(*args)
    inference = InferenceHealthMonitor(*args)       # S3: la apertura depende solo de los eventos
    windows, alerts = [], []
    results = []
    for line in open(run_dir / "events.jsonl", encoding="utf-8"):
        e = InferenceEvent.model_validate_json(line)
        alerts += health.add(e)[0]        # mismo orden que run.py: fuente, versión, luego ventana
        alerts += version.add(e)[0]
        alerts += inference.add(e)[0]
        results.append(ev.add(e))
    results.append(ev.flush())
    # S4: la regla de telemetría no se deriva de los eventos sino del estado de los destinos por muestra
    if (run_dir / "telemetria.jsonl").exists():
        tel = TelemetryMonitor(*args)
        for line in open(run_dir / "telemetria.jsonl", encoding="utf-8"):
            alerts += tel.add(json.loads(line))[0]
    window_alerts = []
    for win, new, _ in results:
        if win is not None:
            windows.append(win.record())
        window_alerts += new
    return windows, alerts + window_alerts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--sin-reaccion-version", action="store_true",
                    help="contrafactual de S2: evalúa también las ventanas con otra versión del modelo (no compara)")
    args = ap.parse_args()
    ok_all = True
    for r in args.runs:
        r = Path(r)
        if args.sin_reaccion_version:
            windows, alerts = recompute(r, without_version_reaction=True)
            print(f"{r.name} (contrafactual: sin excluir ventanas de otra versión; no se compara con lo registrado)")
            for w in windows:
                print(f"   ventana {w['idx']:3d} {w['state']:15s} low_ratio={w['low_ratio']} mean_score={w['mean_score']} "
                      f"condiciones={w['conditions']}")
            print(f"   alertas de confianza: {[a['rule_id'] for a in alerts if a['rule_id'] != 'version-modelo-v1'] or 'ninguna'}")
            continue
        windows, alerts = recompute(r)
        logged_w = [json.loads(l) for l in open(r / "windows.jsonl", encoding="utf-8")] if (r / "windows.jsonl").exists() else []
        logged_a = [json.loads(l) for l in open(r / "alerts.jsonl", encoding="utf-8")] if (r / "alerts.jsonl").exists() else []
        logged_a = [a for a in logged_a if a["type"] == "alert"]
        keys = ("window_id", "n_valid", "n_eligible", "n_low", "low_ratio", "mean_score", "state")
        w_ok = [{k: w[k] for k in keys} for w in windows] == [{k: w[k] for k in keys} for w in logged_w]
        key = lambda a: (a["alert_id"], a["rule_id"], a["event_ids"])
        a_ok = sorted(map(key, alerts)) == sorted(map(key, logged_a))
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
