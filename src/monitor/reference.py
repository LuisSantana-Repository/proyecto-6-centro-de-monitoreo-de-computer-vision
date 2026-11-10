"""Estadísticas de referencia y umbrales derivados.

Lee los events.jsonl de las ejecuciones de referencia, calcula las mismas ventanas que el monitor
y escribe config/referencia.json con: distribución de puntuaciones elegibles, estadísticas por
ventana (low_ratio y mean_score) y los umbrales derivados (media ± 2 sd) y de captura (p5 por frame).

Uso:
  uv run python -m monitor.reference datos/runs/<ref1> datos/runs/<ref2> [--out config/referencia.json]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from monitor.alerts import window_stats
from monitor.config import config_ref, load_config
from monitor.events import InferenceEvent
from monitor.source import utc_now_iso


def summary(values: list[float]) -> dict:
    a = np.array(values, dtype=float)
    return {
        "n": int(len(a)), "mean": round(float(a.mean()), 4), "sd": round(float(a.std(ddof=1)), 4),
        "min": round(float(a.min()), 4), "p2_5": round(float(np.percentile(a, 2.5)), 4),
        "p5": round(float(np.percentile(a, 5)), 4), "p50": round(float(np.percentile(a, 50)), 4),
        "p95": round(float(np.percentile(a, 95)), 4), "p97_5": round(float(np.percentile(a, 97.5)), 4),
        "max": round(float(a.max()), 4),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--config", default="config/monitor.yaml")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    cfg = load_config(args.config)
    floor, lower = cfg["policy"]["analysis_floor"], cfg["policy"]["lower"]
    size = cfg["windows"]["size_frames"]

    # F7: una referencia vale para una sola fuente; se comprueba antes de calcular nada
    from monitor.sources import base_source
    sources = {base_source(json.loads(l)["source_id"]) for r in args.runs
               for l in open(Path(r) / "events.jsonl", encoding="utf-8")}
    if len(sources) != 1:
        raise SystemExit(f"Las ejecuciones de referencia mezclan fuentes: {sorted(sources)}")
    all_scores, eligible, frame_means = [], [], []
    win_low, win_mean, per_run = [], [], []
    for r in args.runs:
        r = Path(r)
        manifest = json.loads((r / "manifest.json").read_text(encoding="utf-8"))
        events = [InferenceEvent.model_validate_json(l) for l in open(r / "events.jsonl", encoding="utf-8")]
        for e in events:
            s = [d.score for d in e.detections]
            all_scores += s
            el = [x for x in s if x >= floor]
            eligible += el
            if len(el) >= cfg["capture"]["frame_min_eligible"]:
                frame_means.append(sum(el) / len(el))
        wins = [window_stats(events[i:i + size], floor, lower) for i in range(0, len(events) - size + 1, size)]
        wins = [w for w in wins if w["n_valid"] >= cfg["windows"]["min_valid_frames"]
                and w["n_eligible"] >= cfg["windows"]["min_eligible"]]
        win_low += [w["low_ratio"] for w in wins]
        win_mean += [w["mean_score"] for w in wins]
        per_run.append({
            "run_id": manifest["run_id"], "config_ref": manifest["config_ref"],
            "replay_of": manifest.get("replay_of"), "n_events": len(events), "n_windows": len(wins),
            "first_captured_at": events[0].captured_at, "det_per_frame": round(np.mean([e.n_detections for e in events]), 2),
            "low_ratio": summary([w["low_ratio"] for w in wins]), "mean_score": summary([w["mean_score"] for w in wins]),
        })

    low_s, mean_s = summary(win_low), summary(win_mean)
    el = np.array(eligible)
    out = {
        "generated_at": utc_now_iso(),
        "config_ref_at_generation": config_ref(cfg),
        "source_id": next(iter(sources)),   # F7: la referencia solo vale para esta fuente
        "analysis_floor": floor, "lower": lower, "window_size_frames": size,
        "runs": per_run,
        "candidates_total": len(all_scores),
        "eligible_total": len(eligible),
        "eligible_fraction": round(len(eligible) / len(all_scores), 4),
        "eligible_score_quantiles": {f"p{p}": round(float(np.percentile(el, p)), 4) for p in (25, 50, 60, 75, 80, 90)},
        "windows": {"low_ratio": low_s, "mean_score": mean_s},
        "frame_mean_eligible": summary(frame_means),
        "derived_thresholds": {
            "low_ratio_gt (mean + 2 sd)": round(low_s["mean"] + 2 * low_s["sd"], 3),
            "mean_score_lt (mean - 2 sd)": round(mean_s["mean"] - 2 * mean_s["sd"], 3),
            "capture_frame_mean_below (p5 frame)": round(summary(frame_means)["p5"], 2),
        },
    }
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"Escrito {args.out}")
    print(json.dumps({k: out[k] for k in ("eligible_fraction", "eligible_score_quantiles", "windows",
                                          "derived_thresholds")}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
