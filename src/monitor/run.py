"""Recorrido principal: fuente -> inferencia -> zonas de confianza -> evento (+ captura).

Uso:
  uv run python -m monitor.run --frames 30                       # cámara en vivo
  uv run python -m monitor.run --frames 30 --record              # además guarda cada frame para replay
  uv run python -m monitor.run --replay datos/grabaciones/<run>  # reproduce una secuencia grabada
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

from monitor.config import config_ref, load_config, sha256_file
from monitor.detector import Detector
from monitor.events import Detection, EventWriter, InferenceEvent
from monitor.policy import LOW, zone, zone_counts
from monitor.source import LiveYouTubeSource, ReplaySource, utc_now_iso


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


def capture_reason(cfg: dict, seq: int, status: str, n: int, low_ratio: float | None) -> str | None:
    c = cfg["capture"]
    if status in ("inference_error", "invalid_input"):
        return "error" if c["on_error"] else None
    if status == "no_detections" and c["on_no_detections"]:
        return "sin_detecciones"
    if low_ratio is not None and n >= c["low_min_detections"] and low_ratio >= c["low_ratio_min"]:
        return "baja_confianza"
    if seq % c["normal_every_n_frames"] == 0:
        return "muestra_normal"
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/monitor.yaml")
    ap.add_argument("--frames", type=int, default=None, help="número de frames a procesar (default: sin límite)")
    ap.add_argument("--replay", default=None, help="carpeta de frames .jpg a reproducir como stream")
    ap.add_argument("--record", action="store_true", help="guardar cada frame muestreado para replay")
    ap.add_argument("--label", default="run", help="etiqueta del run (p. ej. referencia, normal, baja)")
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

    source = ReplaySource(cfg, args.replay) if args.replay else LiveYouTubeSource(cfg)
    detector = Detector(cfg, model_sha)
    print(f"[{run_id}] modelo={detector.model_ref} config={cref} umbrales=({lower}, {upper})")
    detector.warmup()

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
        "recording_dir": str(rec_dir) if args.record else None,
    }
    writer = EventWriter(run_dir / "events.jsonl")
    counts = {"ok": 0, "no_detections": 0, "inference_error": 0, "invalid_input": 0, "images": 0}

    source.start()
    manifest["source_meta"] = source.stats.stream_meta
    try:
        for fr in source.frames(args.frames):
            frame_id = f"{run_id}-f{fr.seq:06d}"
            dets, timings, err = [], {}, None
            if fr.image is None:
                status = "invalid_input"
                err = "frame no disponible o no decodificable"
            else:
                if args.record:
                    cv2.imwrite(str(rec_dir / f"{fr.seq:06d}.jpg"), fr.image)
                try:
                    raw, timings = detector.predict(fr.image)
                    dets = [Detection(**d, zone=zone(d["score"], lower, upper)) for d in raw]
                    status = "ok" if dets else "no_detections"
                except Exception as e:  # el fallo técnico queda registrado como evento
                    status, err = "inference_error", f"{type(e).__name__}: {e}"

            zc = zone_counts([d.zone for d in dets])
            low_ratio = round(zc[LOW] / len(dets), 4) if dets else None
            reason = capture_reason(cfg, fr.seq, status, len(dets), low_ratio)
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
                model_ref=detector.model_ref,
                config_ref=cref,
                thresholds={"lower": lower, "upper": upper},
                status=status,
                detections=dets,
                n_detections=len(dets),
                zone_counts=zc,
                low_ratio=low_ratio,
                timings=timings,
                e2e_ms=round((time.perf_counter() - fr.t_capture) * 1000, 2),
                image_ref=image_ref,
                capture_reason=reason,
                error=err,
            )
            writer.write(ev)
            counts[status] += 1
            print(
                f"  #{fr.seq:4d} {status:14s} det={len(dets):3d} "
                f"acc/dud/baja={zc['accepted']}/{zc['doubt']}/{zc['low']} "
                f"inf={timings.get('inference_ms', '-')}ms img={reason or '-'}"
            )
    except KeyboardInterrupt:
        print("Interrumpido por el usuario.")
    finally:
        source.stop()
        writer.close()
        manifest["finished_at"] = utc_now_iso()
        manifest["counts"] = counts
        manifest["source_stats"] = source.stats.as_dict()
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Listo: {run_dir}")


if __name__ == "__main__":
    main()
