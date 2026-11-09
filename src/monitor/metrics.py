"""Métricas base por grupo (G1–G7), calculadas desde los registros primarios de una ejecución.

Entradas: datos/runs/<run>/events.jsonl, alerts.jsonl y manifest.json (configuración guardada).
Salida: un JSON por ejecución con cada métrica por ejecución y, cuando aplica, por ventana de
`windows.size_frames` eventos (las mismas ventanas que usan las alertas).

Regla común para cero observaciones: si la población de una métrica está vacía, el valor es null y
`estado` = "sin_datos" (nunca 0, que se confundiría con un valor medido).

Uso:
  uv run python -m monitor.metrics datos/runs/<run_id> [...] [--out-dir evidencias/metricas]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from monitor.alerts import window_stats
from monitor.config import load_config
from monitor import drift
from monitor.events import InferenceEvent
from monitor.policy import zone

VALID = ("ok", "no_detections")
REQUIRED = ("event_id", "run_id", "frame_id", "timestamp", "captured_at", "source_id", "task",
            "model_ref", "config_ref", "status")


def pct(values: list[float], p: float) -> float | None:
    """Percentil por rango más cercano (nearest-rank): siempre es un valor observado."""
    if not values:
        return None
    a = sorted(values)
    k = max(0, int(np.ceil(p / 100 * len(a))) - 1)
    return round(float(a[k]), 3)


def ts(iso: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def chunks(events: list, size: int) -> list[list]:
    return [events[i:i + size] for i in range(0, len(events), size)]


def result(value, unidad: str, n: int, **extra) -> dict:
    return {"valor": value, "unidad": unidad, "n": n, "estado": "ok" if value is not None else "sin_datos", **extra}


# -- G1 · tiempo de inferencia -----------------------------------------------------
def g1_inference(events: list[InferenceEvent]) -> dict:
    v = [e.timings["inference_ms"] for e in events if e.status in VALID and "inference_ms" in e.timings]
    return result(pct(v, 50), "ms", len(v), p95_ms=pct(v, 95), max_ms=max(v) if v else None)


# -- G2 · FPS de procesamiento -----------------------------------------------------
def g2_fps(events: list[InferenceEvent]) -> dict:
    v = [e for e in events if e.status in VALID]
    if len(v) < 2:
        return result(None, "frames/s", len(v))
    span = ts(v[-1].timestamp) - ts(v[0].timestamp)
    return result(round((len(v) - 1) / span, 4) if span > 0 else None, "frames/s", len(v), span_s=round(span, 1))


# -- G3 · RAM del proceso ----------------------------------------------------------
def g3_ram(events: list[InferenceEvent]) -> dict:
    v = [e.resources["rss_mb"] for e in events if e.resources.get("rss_mb") is not None]
    cpu = [e.resources["cpu_percent"] for e in events if e.resources.get("cpu_percent") is not None]
    return result(pct(v, 95), "MB (RSS, p95)", len(v), p50_mb=pct(v, 50), max_mb=max(v) if v else None,
                  cpu_percent_p50=pct(cpu, 50))


# -- G4 · proporción de baja confianza ---------------------------------------------
def g4_low_ratio(events: list[InferenceEvent], floor: float, lower: float) -> dict:
    st = window_stats(events, floor, lower)
    return result(st["low_ratio"], "proporción", st["n_eligible"], n_low=st["n_low"], n_valid_frames=st["n_valid"])


# -- G5 · cobertura de trazabilidad ------------------------------------------------
def trace_problems(e: InferenceEvent, manifest: dict, root: Path) -> list[str]:
    p = [f"falta {f}" for f in REQUIRED if not getattr(e, f)]
    if e.config_ref != manifest["config_ref"]:
        p.append("config_ref no coincide con el manifest")
    # el model_ref debe resolver a un artefacto verificado de la ejecución: el configurado o, en el
    # escenario S2, el que el despliegue cargó (manifest.model_swap, también verificado por SHA-256).
    # Que sea otra versión no lo vuelve intrazable: eso lo mide G6 y lo vigila la regla de versión.
    known = {manifest["model_sha256"][:12]} | ({manifest["model_swap"]["sha256"][:12]}
                                               if manifest.get("model_swap") else set())
    if e.model_ref.split("@sha256:")[-1] not in known:
        p.append("model_ref no corresponde a un artefacto verificado de la ejecución")
    lower, upper = e.thresholds.get("lower"), e.thresholds.get("upper")
    pol = manifest["config"]["policy"]
    if (lower, upper) != (pol["lower"], pol["upper"]):
        p.append("umbrales distintos de la configuración")
    if any(d.zone != zone(d.score, pol["lower"], pol["upper"]) for d in e.detections):
        p.append("zona inconsistente con los umbrales")
    if e.capture_reason and not e.image_ref:
        p.append("captura sin image_ref")
    if e.image_ref:
        f = root / e.image_ref
        if not f.is_file() or f.stat().st_size == 0:
            p.append("image_ref no resuelve a un archivo")
    if e.status in VALID and "inference_ms" not in e.timings:
        p.append("sin tiempo de inferencia")
    return p


def g5_traceability(events: list[InferenceEvent], manifest: dict, root: Path) -> dict:
    problems = {e.event_id: trace_problems(e, manifest, root) for e in events}
    bad = {k: v for k, v in problems.items() if v}
    n = len(events)
    return result(round((n - len(bad)) / n, 4) if n else None, "proporción de eventos completos", n,
                  incompletos=len(bad), ejemplos=dict(list(bad.items())[:5]),
                  imagenes_referenciadas=sum(1 for e in events if e.image_ref))


# -- G6 · versión esperada frente a reportada --------------------------------------
def g6_version(events: list[InferenceEvent], expected_model_ref: str) -> dict:
    reported: dict[str, int] = {}
    for e in events:
        reported[e.model_ref] = reported.get(e.model_ref, 0) + 1
    n = len(events)
    match = reported.get(expected_model_ref, 0)
    return result(round(match / n, 4) if n else None, "proporción de eventos con la versión esperada", n,
                  esperada=expected_model_ref, reportadas=reported)


# -- G7 · alertas activas ----------------------------------------------------------
def alert_episodes(alert_log: list[dict]) -> dict[str, dict]:
    eps: dict[str, dict] = {}
    for r in alert_log:
        if r["type"] == "alert":
            eps[r["alert_id"]] = {"rule_id": r["rule_id"], "severity": r["severity"],
                                  "opened_at": r["window_end"], "resolved_at": None}
        elif r.get("status") == "resolved":
            eps[r["alert_id"]]["resolved_at"] = r["resolved_at"]
    return eps


def g7_active(eps: dict[str, dict], instant: str) -> int:
    """Alertas abiertas en un instante: abiertas en o antes del instante y no resueltas antes."""
    t = ts(instant)
    return sum(1 for e in eps.values()
               if ts(e["opened_at"]) <= t and (e["resolved_at"] is None or ts(e["resolved_at"]) > t))


# -- métricas adicionales -----------------------------------------------------------
def m2_e2e_latency(events: list[InferenceEvent]) -> dict:
    """M2 · latencia de punta a punta: del frame obtenido de la fuente al evento creado (inferencia,
    pre/postproceso, zonas y captura incluidas). Etapa distinta de G1, que mide solo el modelo."""
    v = [e.e2e_ms for e in events if e.status in VALID and e.e2e_ms is not None]
    return result(pct(v, 50), "ms", len(v), p95_ms=pct(v, 95), max_ms=max(v) if v else None)


def availability(events: list[InferenceEvent]) -> float | None:
    """Proporción de muestras en las que la fuente entregó frame (status distinto de invalid_input)."""
    return round(sum(e.status != "invalid_input" for e in events) / len(events), 4) if events else None


def m3_recovery(alert_log: list[dict], events: list[InferenceEvent]) -> dict:
    """M3 · tiempo de recuperación de la fuente: del primer frame perdido al primer frame recibido,
    por incidente resuelto de la regla de salud de la fuente. Sin incidentes -> null (no hay qué medir)."""
    opened = {r["alert_id"] for r in alert_log if r["type"] == "alert" and r["rule_id"].startswith("fuente-")}
    inc = [{"alert_id": r["alert_id"], "downtime_s": r["downtime_s"], "n_invalid": r["n_invalid"],
            "first_ok_event_id": r["first_ok_event_id"]}
           for r in alert_log if r["type"] == "update" and r.get("alert_id") in opened and "downtime_s" in r]
    d = [i["downtime_s"] for i in inc]
    return result(round(sum(d) / len(d), 1) if d else None, "s (media por incidente)", len(d),
                  max_s=max(d) if d else None, incidentes_abiertos=len(opened) - len(inc),
                  disponibilidad=availability(events), incidentes=inc)


# -- ejecución -------------------------------------------------------------------
def compute(run_dir: Path, expected_cfg: dict | None = None, root: Path = Path("."),
            drift_ref: dict | None = None) -> dict:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    cfg = manifest["config"]
    floor, lower = cfg["policy"]["analysis_floor"], cfg["policy"]["lower"]
    size = cfg["windows"]["size_frames"]
    events = [InferenceEvent.model_validate_json(l) for l in open(run_dir / "events.jsonl", encoding="utf-8")]
    alert_log = [json.loads(l) for l in open(run_dir / "alerts.jsonl", encoding="utf-8")] \
        if (run_dir / "alerts.jsonl").exists() else []
    eps = alert_episodes(alert_log)
    exp = expected_cfg or cfg
    expected_model_ref = f"{exp['model']['name']}@sha256:{exp['model']['sha256'][:12]}"

    windows = []
    for i, ch in enumerate(chunks(events, size), start=1):
        windows.append({
            "idx": i, "first_event_id": ch[0].event_id, "last_event_id": ch[-1].event_id,
            "start": ch[0].captured_at, "end": ch[-1].captured_at, "n_events": len(ch),
            "completa": len(ch) == size,
            "G1_inferencia_p50_ms": g1_inference(ch)["valor"],
            "G1_inferencia_p95_ms": g1_inference(ch)["p95_ms"],
            "G2_fps_procesamiento": g2_fps(ch)["valor"],
            "G3_rss_p95_mb": g3_ram(ch)["valor"],
            "G4_prop_baja_confianza": g4_low_ratio(ch, floor, lower)["valor"],
            "G4_n_elegibles": g4_low_ratio(ch, floor, lower)["n"],
            "G7_alertas_activas": g7_active(eps, ch[-1].captured_at) if eps else 0,
            "M2_e2e_p95_ms": m2_e2e_latency(ch)["p95_ms"],
            "M3_disponibilidad_fuente": availability(ch),
            "G6_prop_version_esperada": g6_version(ch, expected_model_ref)["valor"],
        })

    paced = manifest.get("paced", True)
    g2 = g2_fps(events)
    if not paced:
        g2["estado"] = "no_aplica"
        g2["nota"] = "reprocesamiento sin pausa (--no-pace): el ritmo no representa el stream"
    g7_end = g7_active(eps, events[-1].captured_at) if events and eps else 0
    return {
        "run_id": manifest["run_id"], "label": manifest.get("label"), "mode": manifest.get("mode"),
        "replay_of": manifest.get("replay_of"), "paced": paced, "config_ref": manifest["config_ref"],
        "n_events": len(events), "window_size_frames": size,
        "metricas_base": {
            "G1_tiempo_inferencia": {**g1_inference(events), "nota": "solo el modelo; excluye pre/postproceso y captura"},
            "G2_fps_procesamiento": {**g2, "declarado_fps": round(1 / cfg["source"]["sample_interval_s"], 3)},
            "G3_ram_proceso": g3_ram(events),
            "G4_proporcion_baja_confianza": {**g4_low_ratio(events, floor, lower),
                                             "definicion": f"elegibles (s >= {floor}) con s < {lower} / elegibles"},
            "G5_cobertura_trazabilidad": g5_traceability(events, manifest, root),
            "G6_version_esperada_vs_reportada": g6_version(events, expected_model_ref),
            "G7_alertas_activas": result(g7_end, "alertas abiertas al final", len(eps),
                                         max_por_ventana=max((w["G7_alertas_activas"] for w in windows), default=0),
                                         emitidas=len(eps),
                                         resueltas=sum(1 for e in eps.values() if e["resolved_at"])),
        },
        "metricas_adicionales": {
            # M1 · desplazamiento de la distribución de puntuaciones frente a la referencia (monitor.drift)
            "M1_desplazamiento_distribucion_W1": drift.run_drift(events, cfg, drift_ref) if drift_ref else
            {"valor": None, "estado": "sin_datos", "nota": "falta config/referencia-distribucion.json"},
            "M2_latencia_e2e": {**m2_e2e_latency(events),
                                "nota": "del frame obtenido al evento creado; excluye la escritura del evento"},
            "M3_recuperacion_fuente": m3_recovery(alert_log, events),
        },
        "contexto_fuente": manifest.get("source_stats"),
        "ventanas": windows,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--config", default="config/monitor.yaml", help="configuración desplegada (versión esperada)")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--drift-ref", default=drift.REF_FILE, help="referencia de la métrica adicional M1")
    args = ap.parse_args()
    expected = load_config(args.config)
    ref = drift.load_reference(args.drift_ref)
    for r in args.runs:
        m = compute(Path(r), expected, drift_ref=ref)
        if args.out_dir:
            Path(args.out_dir).mkdir(parents=True, exist_ok=True)
            out = Path(args.out_dir) / f"{m['run_id']}.json"
            out.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"== {m['run_id']} ({m['n_events']} eventos)")
        for k, v in m["metricas_base"].items():
            extra = {kk: vv for kk, vv in v.items() if kk not in ("valor", "unidad", "n", "estado", "ejemplos",
                                                                   "nota", "definicion", "reportadas")}
            print(f"   {k:34s} {str(v['valor']):>8} {v['unidad']:<40} n={v['n']:<6} {v['estado']:9s} {extra}")
        ad = m["metricas_adicionales"]
        d = ad["M1_desplazamiento_distribucion_W1"]
        print(f"   {'M1_desplazamiento_distribucion_W1':34s} {str(d['valor']):>8} "
              f"ventanas sobre {d.get('umbral')}: {d.get('ventanas_sobre_umbral')}/{d.get('n')}")
        e = ad["M2_latencia_e2e"]
        print(f"   {'M2_latencia_e2e':34s} {str(e['valor']):>8} ms p50 · p95 {e['p95_ms']} · n={e['n']}")
        rc = ad["M3_recuperacion_fuente"]
        print(f"   {'M3_recuperacion_fuente':34s} {str(rc['valor']):>8} s · incidentes={rc['n']} "
              f"abiertos={rc['incidentes_abiertos']} disponibilidad={rc['disponibilidad']}")


if __name__ == "__main__":
    main()
