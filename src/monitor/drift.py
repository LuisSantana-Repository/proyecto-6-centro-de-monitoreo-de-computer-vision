"""Métrica adicional M1 · desplazamiento de la distribución de puntuaciones frente a la referencia.

Métrica: distancia de Wasserstein-1 (W1) entre la distribución de puntuaciones elegibles de una
ventana y la de la referencia. En 1-D, W1 = ∫ |Q_ventana(u) − Q_ref(u)| du, con Q la función cuantil:
es el desplazamiento medio de las puntuaciones, en las mismas unidades que la confianza. Un corrimiento
uniforme de δ da W1 = δ, lo que permite verificarla con un caso controlado.

Población: detecciones con s ≥ analysis_floor en frames con inferencia válida (la misma de G4).
Por ventana: solo ventanas completas y suficientes (n_valid ≥ min_valid_frames, n_eligible ≥ min_eligible);
si no, el valor es null (sin_datos), nunca 0.
Referencia: las mismas ejecuciones de referencia que los umbrales. Se guarda su función cuantil
(1000 puntos) y el umbral = máximo de W1 de cada ventana de referencia contra el resto de la
referencia (leave-one-out), en config/referencia-distribucion.json.

Uso:
  uv run python -m monitor.drift --build datos/runs/<ref1> datos/runs/<ref2>   # genera la referencia
  uv run python -m monitor.drift datos/runs/<run_id> [...]                      # W1 por ventana
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from monitor.config import load_config
from monitor.events import InferenceEvent
from monitor.source import utc_now_iso

QS = np.linspace(0.0005, 0.9995, 1000)      # puntos de la función cuantil (punto medio de 1000 tramos)
REF_FILE = "config/referencia-distribucion.json"


def eligible_scores(events: list[InferenceEvent], floor: float) -> np.ndarray:
    return np.array([d.score for e in events if e.status in ("ok", "no_detections")
                     for d in e.detections if d.score >= floor], dtype=float)


def quantiles(scores: np.ndarray) -> np.ndarray:
    return np.quantile(scores, QS)


def w1(scores: np.ndarray, ref_quantiles: np.ndarray) -> float:
    """Wasserstein-1 aproximada por la diferencia media de funciones cuantil en 1000 puntos."""
    return round(float(np.mean(np.abs(quantiles(scores) - np.asarray(ref_quantiles)))), 4)


def sufficient(events: list[InferenceEvent], scores: np.ndarray, cfg: dict) -> bool:
    w = cfg["windows"]
    n_valid = sum(e.status in ("ok", "no_detections") for e in events)
    return len(events) == w["size_frames"] and n_valid >= w["min_valid_frames"] and len(scores) >= w["min_eligible"]


def load_events(run_dir: Path) -> list[InferenceEvent]:
    return [InferenceEvent.model_validate_json(l) for l in open(run_dir / "events.jsonl", encoding="utf-8")]


def windows_of(events: list, size: int) -> list[list]:
    return [events[i:i + size] for i in range(0, len(events), size)]


def build_reference(runs: list[Path], cfg: dict) -> dict:
    floor, size = cfg["policy"]["analysis_floor"], cfg["windows"]["size_frames"]
    wins = []
    for r in runs:
        for ch in windows_of(load_events(r), size):
            s = eligible_scores(ch, floor)
            if sufficient(ch, s, cfg):
                wins.append(s)
    pooled = np.concatenate(wins)
    loo = [w1(s, quantiles(np.concatenate(wins[:i] + wins[i + 1:]))) for i, s in enumerate(wins)]
    return {
        "generated_at": utc_now_iso(),
        "metric": "wasserstein_1",
        "runs": [r.name for r in runs],
        "analysis_floor": floor,
        "n_windows": len(wins),
        "n_scores": int(len(pooled)),
        "leave_one_out_w1": {"mean": round(float(np.mean(loo)), 4), "sd": round(float(np.std(loo, ddof=1)), 4),
                             "max": round(float(np.max(loo)), 4), "values": loo},
        "threshold": round(float(np.max(loo)), 3),
        "threshold_rule": "máximo de W1 leave-one-out de las ventanas de referencia",
        "quantile_points": len(QS),
        "quantiles": [round(float(x), 5) for x in quantiles(pooled)],
    }


def ref_path(cfg: dict) -> str:
    """F7: cada fuente puede declarar su propia referencia de distribución (reference.distribution_file)."""
    return (cfg.get("reference") or {}).get("distribution_file") or REF_FILE


def load_reference(path: str | Path = REF_FILE) -> dict | None:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def run_drift(events: list[InferenceEvent], cfg: dict, ref: dict) -> dict:
    """W1 por ventana y de toda la ejecución (ventanas suficientes) frente a la referencia."""
    floor, size = cfg["policy"]["analysis_floor"], cfg["windows"]["size_frames"]
    th, rq = ref["threshold"], ref["quantiles"]
    per_window, used = [], []
    for i, ch in enumerate(windows_of(events, size), start=1):
        s = eligible_scores(ch, floor)
        ok = sufficient(ch, s, cfg)
        v = w1(s, rq) if ok else None
        if ok:
            used.append(s)
        per_window.append({"idx": i, "first_event_id": ch[0].event_id, "last_event_id": ch[-1].event_id,
                           "n_eligible": int(len(s)), "w1": v, "supera_umbral": (v > th) if v is not None else None})
    vals = [w["w1"] for w in per_window if w["w1"] is not None]
    run_value = w1(np.concatenate(used), rq) if used else None
    return {
        "valor": run_value, "unidad": "puntos de confianza (W1)", "n": len(vals),
        "estado": "ok" if run_value is not None else "sin_datos",
        "umbral": th, "ventanas_sobre_umbral": sum(v > th for v in vals),
        "max_ventana": max(vals) if vals else None,
        "definicion": f"W1 entre puntuaciones elegibles (s >= {floor}) y la referencia ({', '.join(ref['runs'])})",
        "ventanas": per_window,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--build", action="store_true", help="generar la referencia a partir de estas ejecuciones")
    ap.add_argument("--config", default="config/monitor.yaml")
    ap.add_argument("--ref", default=REF_FILE)
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.build:
        ref = build_reference([Path(r) for r in args.runs], cfg)
        Path(args.ref).write_text(json.dumps(ref, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Escrito {args.ref}: {ref['n_windows']} ventanas, {ref['n_scores']} puntuaciones, "
              f"W1 leave-one-out {ref['leave_one_out_w1']['mean']} ± {ref['leave_one_out_w1']['sd']} "
              f"(máx {ref['leave_one_out_w1']['max']}) -> umbral {ref['threshold']}")
        return
    ref = load_reference(args.ref)
    for r in args.runs:
        r = Path(r)
        m = json.loads((r / "manifest.json").read_text(encoding="utf-8"))
        d = run_drift(load_events(r), m["config"], ref)
        print(f"== {m['run_id']}: W1 ejecución={d['valor']} | ventanas sobre {d['umbral']}: "
              f"{d['ventanas_sobre_umbral']}/{d['n']}")
        print("   " + " ".join(f"{w['idx']}:{w['w1'] if w['w1'] is not None else '-'}" for w in d["ventanas"]))


if __name__ == "__main__":
    main()
