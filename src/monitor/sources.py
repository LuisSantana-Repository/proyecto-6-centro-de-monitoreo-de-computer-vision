"""Extra F7 · varias fuentes: cada cámara con su referencia, sus umbrales y su vista.

Dos cámaras del mismo cruce (desde arriba y desde la calle) ven escenas con distribuciones de confianza muy
distintas: con los umbrales de una, la otra parece estar siempre en "baja confianza". Por eso cada fuente tiene
su propia configuración (`config/monitor.yaml` para la 01, `config/fuente-02.yaml` para la 02), su referencia
(`reference.stats_file`, que guarda el `source_id` con el que se generó) y sus umbrales derivados de ella.

- `check_reference(cfg)`: monitor.run no arranca si la referencia de la configuración es de otra fuente.
- `summary(con, live)`: estado por fuente para el portal (`/fuentes`) y la API.
- `verify(con, runs_dir)`: para cada ejecución, comprueba con sus registros primarios que su referencia es de
  su fuente, que sus umbrales son los derivados de esa referencia y que todos sus eventos son de esa fuente.
- `cross(run_dir, cfg)`: contrafactual; reevalúa las ventanas de una ejecución con los umbrales de otra
  configuración (p. ej. la fuente 02 con los de la 01).

    uv run python -m monitor.sources                      # resumen por fuente (SQLite, solo lectura)
    uv run python -m monitor.sources --verificar          # umbrales y referencia por fuente, desde los JSONL
    uv run python -m monitor.sources --cruzado datos/runs/<run> --config config/monitor.yaml
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from monitor.alerts import WindowEvaluator
from monitor.events import InferenceEvent

THRESHOLD_KEYS = {"low_ratio": "low_ratio_gt (mean + 2 sd)", "mean_score": "mean_score_lt (mean - 2 sd)"}


def base_source(source_id: str | None) -> str | None:
    """La fuente sin el sufijo de reproducción: shibuya-scramble-02-replay -> shibuya-scramble-02."""
    return source_id[: -len("-replay")] if source_id and source_id.endswith("-replay") else source_id


def load_reference(cfg: dict, root: Path = Path(".")) -> dict | None:
    p = root / ((cfg.get("reference") or {}).get("stats_file") or "")
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def check_reference(cfg: dict, root: Path = Path(".")) -> None:
    ref = load_reference(cfg, root)
    if ref is None or not ref.get("source_id"):
        return
    if ref["source_id"] != cfg["source"]["id"]:
        raise SystemExit(f"La referencia {cfg['reference']['stats_file']} es de la fuente {ref['source_id']}, pero esta "
                         f"configuración monitorea {cfg['source']['id']}: sus umbrales no aplican. Usa la configuración "
                         f"de esa fuente o genera su referencia (monitor.reference).")


def thresholds(cfg: dict) -> dict:
    return {r["signal"]: r["threshold"] for r in cfg.get("alerts", [])}


def expected_thresholds(ref: dict) -> dict:
    d = ref.get("derived_thresholds") or {}
    return {sig: d[k] for sig, k in THRESHOLD_KEYS.items() if k in d}


# -- resumen por fuente (índice) ------------------------------------------------------
def summary(con: sqlite3.Connection, live: dict[str, dict] | None = None, root: Path = Path("."),
            now=None) -> list[dict]:
    from datetime import datetime, timezone

    from monitor.dashboard import run_state

    now = now or datetime.now(timezone.utc)
    con.row_factory = sqlite3.Row
    configs = {r["config_ref"]: json.loads(r["config_json"]) for r in con.execute("SELECT * FROM configs")}
    out: dict[str, dict] = {}
    for r in con.execute("SELECT run_id, label, mode, started_at, finished_at, config_ref, manifest_json FROM runs "
                         "ORDER BY started_at"):
        m = json.loads(r["manifest_json"] or "{}")
        cfg = m.get("config") or configs.get(r["config_ref"]) or {}
        sid = (cfg.get("source") or {}).get("id") or "desconocida"
        s = out.setdefault(sid, {"source_id": sid, "url": (cfg.get("source") or {}).get("url"), "configs": {},
                                 "runs": [], "live": 0, "replay": 0, "en_curso": []})
        ref = load_reference(cfg, root)
        s["configs"].setdefault(r["config_ref"], {
            "config_ref": r["config_ref"], "reference": (cfg.get("reference") or {}).get("stats_file"),
            "reference_source": (ref or {}).get("source_id"), "thresholds": thresholds(cfg)})
        s["runs"].append(r["run_id"])
        s[r["mode"] if r["mode"] in ("live", "replay") else "replay"] += 1
        # mismo criterio que el panel (F5): en curso solo si la vista en vivo reporta hace poco (no estancada)
        state = run_state(dict(r), (live or {}).get(r["run_id"]), now)["state"]
        if state in ("en_curso", "fuente_caida"):
            s["en_curso"].append(r["run_id"])
    for s in out.values():
        q = ",".join("?" * len(s["runs"]))
        e = con.execute(f"SELECT COUNT(*) n, SUM(status IN ('ok','no_detections')) v, AVG(n_detections) d, "
                        f"MAX(captured_at) last FROM events WHERE run_id IN ({q})", s["runs"]).fetchone()
        w = {x["state"]: x["n"] for x in con.execute(
            f"SELECT state, COUNT(*) n FROM windows WHERE run_id IN ({q}) GROUP BY state", s["runs"])}
        a = con.execute(f"SELECT COUNT(*) n, SUM(status = 'open') o FROM alerts WHERE run_id IN ({q})", s["runs"]).fetchone()
        evaluated = w.get("normal", 0) + w.get("fuera_de_rango", 0)
        s.update({"n_runs": len(s["runs"]), "events": e["n"], "valid": e["v"] or 0,
                  "det_per_frame": round(e["d"], 1) if e["d"] is not None else None, "last_event": e["last"],
                  "windows": w, "evaluated": evaluated,
                  "pct_out_of_range": round(w.get("fuera_de_rango", 0) / evaluated, 4) if evaluated else None,
                  "alerts": a["n"], "alerts_open": a["o"] or 0, "configs": list(s["configs"].values())})
    return sorted(out.values(), key=lambda s: s["source_id"])


# -- verificación (registros primarios) ------------------------------------------------
def verify_run(run_dir: Path, root: Path = Path(".")) -> list[str]:
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    cfg, name, problems = m["config"], run_dir.name, []
    sid = cfg["source"]["id"]
    ids = {base_source(json.loads(l)["source_id"]) for l in open(run_dir / "events.jsonl", encoding="utf-8")}
    if ids - {sid}:
        problems.append(f"{name}: eventos de otra fuente {sorted(ids - {sid})} (configurada {sid})")
    ref = load_reference(cfg, root)
    if ref is None:
        return problems
    if ref.get("source_id") and ref["source_id"] != sid:
        problems.append(f"{name}: la referencia es de {ref['source_id']} y la ejecución de {sid}")
    exp, got = expected_thresholds(ref), thresholds(cfg)
    for sig, v in exp.items():
        if sig in got and got[sig] != v:
            problems.append(f"{name}: umbral {sig} = {got[sig]}, pero la referencia de su fuente deriva {v}")
    return problems


def verify(runs_dir: Path, run_ids: list[str] | None = None, root: Path = Path(".")) -> tuple[dict, list[str]]:
    dirs = [runs_dir / r for r in run_ids] if run_ids is not None else \
        sorted(p for p in runs_dir.iterdir() if (p / "manifest.json").exists() and (p / "events.jsonl").exists())
    per_source: dict[str, int] = {}
    problems = []
    for d in dirs:
        m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        per_source[m["config"]["source"]["id"]] = per_source.get(m["config"]["source"]["id"], 0) + 1
        problems += verify_run(d, root)
    return per_source, problems


# -- contrafactual -----------------------------------------------------------------------
def cross(run_dir: Path, other_cfg: dict) -> dict:
    """Reevalúa las ventanas de una ejecución con las reglas (umbrales) de otra configuración."""
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    cfg = {**m["config"], "alerts": other_cfg.get("alerts", [])}
    ev = WindowEvaluator(cfg, m["config_ref"], m["run_id"])
    wins, alerts = [], []
    for line in open(run_dir / "events.jsonl", encoding="utf-8"):
        w, a, _ = ev.add(InferenceEvent.model_validate_json(line))
        if w:
            wins.append(w)
        alerts += a
    w, a, _ = ev.flush()
    alerts += a
    states = [x.state for x in wins]
    own = [json.loads(l)["state"] for l in open(run_dir / "windows.jsonl", encoding="utf-8")] \
        if (run_dir / "windows.jsonl").exists() else []
    return {"run_id": m["run_id"], "source_id": m["config"]["source"]["id"], "thresholds_used": thresholds(cfg),
            "own_thresholds": thresholds(m["config"]), "windows": states,
            "fuera_de_rango": states.count("fuera_de_rango"), "evaluadas": sum(s in ("normal", "fuera_de_rango") for s in states),
            "alertas": [a["rule_id"] for a in alerts],
            "propias": {"fuera_de_rango": own.count("fuera_de_rango"),
                        "alertas": sum(1 for l in open(run_dir / "alerts.jsonl", encoding="utf-8")
                                       if json.loads(l).get("type") == "alert") if (run_dir / "alerts.jsonl").exists() else 0}}


def main() -> None:
    from monitor.config import load_config
    from monitor.dashboard import connect, live_states

    ap = argparse.ArgumentParser(description="Varias fuentes (F7)")
    ap.add_argument("--db", default="datos/monitor.db")
    ap.add_argument("--runs-dir", default="datos/runs")
    ap.add_argument("--vivo-dir", default="datos/vivo")
    ap.add_argument("--verificar", action="store_true")
    ap.add_argument("--cruzado", default=None, metavar="RUN_DIR")
    ap.add_argument("--config", default="config/monitor.yaml", help="con --cruzado: umbrales a aplicar")
    args = ap.parse_args()
    if args.cruzado:
        c = cross(Path(args.cruzado), load_config(args.config))
        print(f"{c['run_id']} ({c['source_id']}) con los umbrales de {args.config} {c['thresholds_used']}:")
        print(f"  fuera de rango {c['fuera_de_rango']}/{c['evaluadas']} ventanas evaluadas · alertas {c['alertas']}")
        print(f"  con sus propios umbrales {c['own_thresholds']}: fuera de rango {c['propias']['fuera_de_rango']} · "
              f"alertas {c['propias']['alertas']}")
        return
    if args.verificar:
        per, problems = verify(Path(args.runs_dir))
        for s, n in sorted(per.items()):
            print(f"  {s}: {n} ejecuciones")
        if problems:
            print("NO coincide:")
            print("\n".join(f"  {p}" for p in problems))
            sys.exit(1)
        print("Cada ejecución usa la referencia de su fuente y los umbrales derivados de ella; sus eventos son de "
              "esa fuente.")
        return
    con = connect(args.db)
    for s in summary(con, live_states(Path(args.vivo_dir))):
        print(f"{s['source_id']}: {s['n_runs']} ejecuciones ({s['live']} en vivo, {s['replay']} replay) · "
              f"{s['events']} eventos · {s['det_per_frame']} det/frame · ventanas {s['windows']} · "
              f"alertas {s['alerts']} ({s['alerts_open']} abiertas)")
        for c in s["configs"]:
            print(f"   {c['config_ref']} · referencia {c['reference']} ({c['reference_source']}) · umbrales {c['thresholds']}")
    con.close()


if __name__ == "__main__":
    main()
