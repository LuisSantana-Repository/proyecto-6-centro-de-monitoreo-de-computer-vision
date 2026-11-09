"""Recorrido principal: fuente -> inferencia -> zonas -> evento (+ captura) -> ventana -> alerta.

Uso:
  uv run python -m monitor.run --frames 450 --label c1                       # cámara en vivo
  uv run python -m monitor.run --frames 450 --record --label c1              # + guarda cada frame para replay
  uv run python -m monitor.run --replay datos/grabaciones/<run> --label x    # reproduce a 1 frame / 2 s
  uv run python -m monitor.run --replay ... --no-pace --label x              # reprocesa sin esperar (solo análisis)
  uv run python -m monitor.run --config config/escenario-interrupcion.yaml --frames 240       --inject-outage 180:90 --label s1                                      # escenario S1: corte de la fuente
  uv run python -m monitor.run --config config/escenario-version.yaml --replay ... --frames 240       --swap-model yolo11s:150:210 --label s2                                # escenario S2: cambio de versión
  uv run python -m monitor.run --config config/escenario-inferencia.yaml --replay ... --frames 240 --inject-inference-error corrupcion:150 --label s3a   # escenario S3: error de inferencia
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import psutil

from monitor.alerts import InferenceHealthMonitor, ModelVersionMonitor, SourceHealthMonitor, WindowEvaluator
from monitor.config import config_ref, load_config, sha256_file
from monitor.detector import Detector, InferenceFault, SwapPlan
from monitor.events import Detection, EventWriter, InferenceEvent
from monitor.live import LiveWriter
from monitor.policy import LOW, zone, zone_counts
from monitor.source import FaultPlan, LiveYouTubeSource, ReplaySource, utc_now_iso
from monitor.store import Store


def environment() -> dict:
    import torch
    import ultralytics

    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "processor": platform.processor(),
        "torch": torch.__version__,
        "ultralytics": ultralytics.__version__,
        "opencv": cv2.__version__,
        "cuda_available": torch.cuda.is_available(),
    }


def capture_reason(cfg: dict, seq: int, status: str, dets: list[Detection]) -> str | None:
    """Política de captura (qué frames se conservan como imagen)."""
    c, floor = cfg["capture"], cfg["policy"]["analysis_floor"]
    if status in ("inference_error", "invalid_input"):
        return "error" if c["on_error"] else None
    if status == "no_detections" and c["on_no_detections"]:
        return "sin_detecciones"
    eligible = [d.score for d in dets if d.score >= floor]
    if len(eligible) >= c["frame_min_eligible"] and sum(eligible) / len(eligible) < c["frame_mean_below"]:
        return "baja_confianza"
    if seq % c["normal_every_n_frames"] == 0:
        return "muestra_normal"
    return None


class ResourceProbe:
    """Uso de recursos del proceso del monitor (G3), muestreado una vez por evento."""

    def __init__(self, disk_path: Path):
        self.proc = psutil.Process()
        self.proc.cpu_percent(None)   # inicializa el contador; la siguiente lectura cubre el intervalo
        self.disk_path = disk_path

    def sample(self) -> dict:
        return {
            "rss_mb": round(self.proc.memory_info().rss / 2**20, 1),
            "cpu_percent": round(self.proc.cpu_percent(None), 1),   # % de un núcleo desde la muestra anterior
            "disk_free_gb": round(psutil.disk_usage(str(self.disk_path)).free / 2**30, 2),
        }


class JsonlLog:
    def __init__(self, path: Path):
        self._f = open(path, "a", encoding="utf-8")

    def write(self, rec: dict) -> None:
        self._f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/monitor.yaml")
    ap.add_argument("--frames", type=int, default=None, help="número de frames a procesar (default: sin límite)")
    ap.add_argument("--replay", default=None, help="carpeta de frames .jpg a reproducir como stream")
    ap.add_argument("--no-pace", action="store_true", help="replay sin esperar el intervalo (reprocesamiento)")
    ap.add_argument("--record", action="store_true", help="guardar cada frame muestreado para replay")
    ap.add_argument("--label", default="run", help="etiqueta del run (p. ej. referencia, c1, c2)")
    ap.add_argument("--inject-outage", default=None, metavar="INICIO:DURACION",
                    help="escenario S1: corta la fuente de INICIO a INICIO+DURACION segundos de stream")
    ap.add_argument("--sin-vivo", action="store_true",
                    help="no publicar el estado en vivo (datos/vivo/<run_id>/) para el portal /vivo")
    ap.add_argument("--swap-model", default=None, metavar="NOMBRE:INICIO:DURACION",
                    help="escenario S2: usa el modelo NOMBRE (model_alternatives) de INICIO a INICIO+DURACION "
                         "segundos de stream, sin cambiar la configuración, y luego vuelve al configurado")
    ap.add_argument("--inject-inference-error", default=None, metavar="MODO:INICIO[:DURACION]",
                    help="escenario S3: corrupcion:INICIO (estado del modelo corrupto; la recarga lo arregla) o "
                         "persistente:INICIO:DURACION (la inferencia falla durante el intervalo)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    cref = config_ref(cfg)
    lower, upper = cfg["policy"]["lower"], cfg["policy"]["upper"]

    weights = Path(cfg["model"]["weights"])
    if not weights.exists():
        raise SystemExit("Faltan los pesos. Ejecuta: uv run python -m monitor.download_model")
    model_sha = sha256_file(weights)
    if model_sha != cfg["model"]["sha256"]:
        raise SystemExit(f"Los pesos no coinciden con el SHA-256 declarado ({model_sha})")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{args.label}"
    run_dir = Path(cfg["output"]["runs_dir"]) / run_id
    img_dir = run_dir / "imagenes"
    img_dir.mkdir(parents=True, exist_ok=True)
    rec_dir = Path("datos/grabaciones") / run_id
    if args.record:
        rec_dir.mkdir(parents=True, exist_ok=True)

    fault = FaultPlan.parse(args.inject_outage) if args.inject_outage else None
    ifault = InferenceFault.parse(args.inject_inference_error) if args.inject_inference_error else None
    if args.replay:
        source = ReplaySource(cfg, args.replay, pace=not args.no_pace, fault=fault)
    else:
        source = LiveYouTubeSource(cfg, fault=fault)
    detector = Detector(cfg, model_sha)
    print(f"[{run_id}] modelo={detector.model_ref} config={cref} umbrales=({lower}, {upper})")
    detector.warmup()

    # escenario S2: el artefacto que el "despliegue" pone en lugar del configurado. Se verifica igual que
    # el principal (identidad resoluble), se carga antes de empezar y solo se usa durante la ventana del plan.
    swap, swap_info, alt_detector = None, None, None
    if args.swap_model:
        swap = SwapPlan.parse(args.swap_model)
        alt = (cfg.get("model_alternatives") or {}).get(swap.name)
        if alt is None:
            raise SystemExit(f"--swap-model: {swap.name} no está en model_alternatives de {args.config}")
        alt_sha = sha256_file(alt["weights"]) if Path(alt["weights"]).exists() else None
        if alt_sha != alt["sha256"]:
            raise SystemExit(f"Faltan o no coinciden los pesos de {swap.name}. Ejecuta: "
                             f"uv run python -m monitor.download_model --config {args.config}")
        alt_detector = Detector(cfg, alt_sha, weights=alt["weights"], name=swap.name)
        alt_detector.warmup()
        swap_info = {**swap.as_dict(), "model_ref": alt_detector.model_ref, "sha256": alt_sha,
                     "weights": alt["weights"]}
        print(f"  S2: {alt_detector.model_ref} reemplaza al modelo de t={swap.start_s} a "
              f"t={swap.start_s + swap.duration_s} s de stream")
    interval = cfg["source"]["sample_interval_s"]

    manifest = {
        "run_id": run_id,
        "label": args.label,
        "started_at": utc_now_iso(),
        "config_path": args.config,
        "config_ref": cref,
        "config": cfg,
        "model_ref": detector.model_ref,
        "model_sha256": model_sha,
        "class_names": {c: detector.names[c] for c in cfg["model"]["classes"]},
        "environment": environment(),
        "mode": "replay" if args.replay else "live",
        "replay_of": Path(args.replay).name if args.replay else None,
        "paced": not (args.replay and args.no_pace),
        "recording_dir": str(rec_dir) if args.record else None,
        "fault_injection": fault.as_dict() if fault else None,
        "model_swap": swap_info,
        "inference_fault": ifault.as_dict() if ifault else None,
    }
    writer = EventWriter(run_dir / "events.jsonl")
    win_log = JsonlLog(run_dir / "windows.jsonl")
    alert_log = JsonlLog(run_dir / "alerts.jsonl")
    health_log = JsonlLog(run_dir / "fuente.jsonl")
    evaluator = WindowEvaluator(cfg, cref, run_id)
    health = SourceHealthMonitor(cfg, cref, run_id)
    version = ModelVersionMonitor(cfg, cref, run_id)
    inference = InferenceHealthMonitor(cfg, cref, run_id)
    # S3: registro de la reacción (recargas del modelo y su efecto); solo si la regla o el fallo existen
    inf_log = JsonlLog(run_dir / "inferencia.jsonl") if (cfg.get("inference_health") or ifault) else None
    probe = ResourceProbe(run_dir)
    manifest["environment"]["cpu_count_logical"] = psutil.cpu_count()
    manifest["environment"]["ram_total_gb"] = round(psutil.virtual_memory().total / 2**30, 1)
    counts = {"ok": 0, "no_detections": 0, "inference_error": 0, "invalid_input": 0, "images": 0,
              "windows": 0, "alerts": 0, "reloads": 0}
    # F4 · vista en vivo: estado actual para el portal (/vivo); no es registro primario
    live_dir = Path(cfg["output"]["runs_dir"]).parent / "vivo"
    live = None if args.sin_vivo else LiveWriter(live_dir, manifest, args.frames)
    live_ctx = {"last_window": None, "open_alerts": {},
                "source": {"estado": "iniciando", "ultimo": None, "at": None, "detalle": {}}}
    if live:
        manifest["live_view"] = {"dir": str(live.dir)}

    source.start()
    manifest["source_meta"] = source.stats.stream_meta
    store = Store(cfg["output"]["db"])
    store.upsert_config(cref, cfg)
    store.upsert_run(manifest)

    def drain_source_health() -> None:
        for h in source.drain_health():
            rec = {"run_id": run_id, **h}
            health_log.write(rec)
            store.insert_source_event(rec)
            ok_types = ("conectado", "reconectado", "lectura_recuperada")
            live_ctx["source"] = {"estado": "conectada" if h["type"] in ok_types else "con_fallas",
                                  "ultimo": h["type"], "at": h["at"],
                                  "detalle": {k: v for k, v in h.items() if k not in ("at", "type")}}
            print(f"  ~~ fuente: {h['type']} " + " ".join(f"{k}={v}" for k, v in h.items() if k not in ("at", "type")))

    def handle_alerts(new_alerts, updates) -> None:
        for a in new_alerts:
            alert_log.write({"type": "alert", **a})
            store.insert_alert(a)
            live_ctx["open_alerts"][a["alert_id"]] = {k: a[k] for k in ("alert_id", "rule_id", "severity",
                                                                         "timestamp", "reason")}
            counts["alerts"] += 1
            print(f"  !! ALERTA {a['severity']} {a['rule_id']}: {a['reason']}")
        for u in updates:
            alert_log.write({"type": "update", **u})
            store.update_alert(u)
            if u.get("status") == "resolved":
                live_ctx["open_alerts"].pop(u["alert_id"], None)
                print(f"  -- alerta resuelta: {u['alert_id']}")

    def handle_window(result) -> None:
        win, new_alerts, updates = result
        if win is None:
            return
        rec = win.record()
        win_log.write(rec)
        store.insert_window(rec)
        counts["windows"] += 1
        live_ctx["last_window"] = {k: rec[k] for k in ("window_id", "idx", "state", "low_ratio", "mean_score",
                                                        "n_valid", "n_eligible")}
        lr = f"{win.low_ratio:.3f}" if win.low_ratio is not None else "-"
        ms = f"{win.mean_score:.3f}" if win.mean_score is not None else "-"
        print(f"  == ventana {win.idx}: {win.state} (prop_baja={lr} media={ms} elegibles={win.n_eligible})")
        handle_alerts(new_alerts, updates)

    def reload_model(ev: InferenceEvent) -> Detector | None:
        """Reacción de S3: recarga el modelo desde los pesos, verificando antes su SHA-256."""
        t0, new, err = time.perf_counter(), None, None
        sha = sha256_file(weights)
        try:
            if sha != cfg["model"]["sha256"]:
                raise RuntimeError(f"los pesos en disco no coinciden con el SHA-256 declarado ({sha[:12]})")
            new = Detector(cfg, sha)
            new.warmup()
        except Exception as e:
            new, err = None, f"{type(e).__name__}: {e}"
        rec = {"run_id": run_id, "at": utc_now_iso(), "type": "recarga_modelo", "seq": ev.seq, "event_id": ev.event_id,
               "n_errores_seguidos": len(inference.bad), "sha256_verificado": sha == cfg["model"]["sha256"],
               "resultado": "cargado" if new else "error", "error": err,
               "duracion_ms": round((time.perf_counter() - t0) * 1000, 1)}
        inf_log.write(rec)
        inference.note_reload(rec)
        counts["reloads"] += 1
        print(f"  >> reacción: recarga del modelo ({rec['resultado']}, {rec['duracion_ms']} ms)")
        return new

    try:
        for fr in source.frames(args.frames):
            frame_id = f"{run_id}-f{fr.seq:06d}"
            dets, timings, err = [], {}, None
            det = alt_detector if (swap and swap.active_at((fr.seq - 1) * interval)) else detector
            if fr.image is None:
                status = "invalid_input"
                err = fr.error or "frame no disponible o no decodificable"
            else:
                if args.record:
                    cv2.imwrite(str(rec_dir / f"{fr.seq:06d}.jpg"), fr.image)
                try:
                    if ifault:
                        ifault.apply(det, (fr.seq - 1) * interval)
                    raw, timings = det.predict(fr.image)
                    dets = [Detection(**d, zone=zone(d["score"], lower, upper)) for d in raw]
                    status = "ok" if dets else "no_detections"
                except Exception as e:  # el fallo técnico queda registrado como evento
                    status, err = "inference_error", f"{type(e).__name__}: {e}"

            zc = zone_counts([d.zone for d in dets])
            low_ratio = round(zc[LOW] / len(dets), 4) if dets else None
            # sin imagen (la fuente no entregó frame) no hay nada que capturar; el error queda en el evento
            reason = capture_reason(cfg, fr.seq, status, dets) if fr.image is not None else None
            image_ref = None
            if reason and fr.image is not None:
                p = img_dir / f"{frame_id}.jpg"
                cv2.imwrite(str(p), fr.image)
                image_ref = p.as_posix()
                counts["images"] += 1

            ev = InferenceEvent(
                event_id=f"evt-{frame_id}",
                run_id=run_id,
                frame_id=frame_id,
                seq=fr.seq,
                timestamp=utc_now_iso(),
                captured_at=fr.captured_at,
                source_id=fr.source_id,
                origin=fr.origin,
                model_ref=det.model_ref,           # lo que realmente se ejecutó (S2: puede no ser lo configurado)
                config_ref=cref,
                thresholds={"lower": lower, "upper": upper},
                status=status,
                detections=dets,
                n_detections=len(dets),
                zone_counts=zc,
                low_ratio=low_ratio,
                timings=timings,
                e2e_ms=round((time.perf_counter() - fr.t_capture) * 1000, 2),
                resources=probe.sample(),
                image_ref=image_ref,
                capture_reason=reason,
                error=err,
            )
            writer.write(ev)        # registro primario (JSONL, recalculable)
            store.insert_event(ev)  # consulta en el portal
            counts[status] += 1
            print(
                f"  #{fr.seq:4d} {status:14s} det={len(dets):3d} "
                f"acc/dud/baja={zc['accepted']}/{zc['doubt']}/{zc['low']} "
                f"inf={timings.get('inference_ms', '-')}ms img={reason or '-'}"
            )
            drain_source_health()
            handle_alerts(*health.add(ev))
            handle_alerts(*version.add(ev))
            new_a, upd_a, inf_recs, want_reload = inference.add(ev)
            for rec in inf_recs:
                inf_log.write(rec)
                print(f"  >> recarga {rec['efecto']}: la muestra #{rec['seq']} quedó {rec['status_siguiente']}")
            handle_alerts(new_a, upd_a)
            if want_reload:
                detector = reload_model(ev) or detector
            handle_window(evaluator.add(ev))
            if live:
                live.update(ev, fr.image, evaluator.buf, live_ctx["last_window"], live_ctx["open_alerts"],
                            live_ctx["source"], counts)
    except KeyboardInterrupt:
        print("Interrumpido por el usuario.")
    finally:
        handle_window(evaluator.flush())
        source.stop()
        drain_source_health()
        health_log.close()
        writer.close()
        win_log.close()
        alert_log.close()
        if inf_log:
            inf_log.close()
        manifest["finished_at"] = utc_now_iso()
        manifest["counts"] = counts
        if live:
            live.finish(counts, manifest["finished_at"])
            manifest["live_view"]["write_errors"] = live.write_errors
        manifest["source_stats"] = source.stats.as_dict()
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        store.upsert_run(manifest)
        store.close()
        print(f"Listo: {run_dir}  ({counts['windows']} ventanas, {counts['alerts']} alertas)")


if __name__ == "__main__":
    main()
