"""Funcionalidad adicional F4 · vista en vivo del stream con resultados.

El proceso del monitor publica, en cada muestra, su estado actual en `datos/vivo/<run_id>/`:
  estado.json  -> último evento (estado, conteos por zona, prop. baja, tiempos, modelo, error), progreso de la
                  ventana en curso, última ventana cerrada, alertas abiertas, estado de la fuente y contadores.
  frame.jpg    -> último frame recibido con sus cajas (aceptada / duda).
El portal (/vivo) los lee y clasifica el estado de la ejecución:
  en_curso      -> el monitor reporta dentro del plazo esperado
  fuente_caida  -> el monitor reporta, pero las últimas muestras no traen frame (se muestra el último frame
                   recibido y su antigüedad)
  estancada     -> el monitor dejó de reportar sin terminar (proceso detenido, colgado o equipo suspendido)
  terminada     -> la ejecución terminó y dejó su resumen final

Escrituras atómicas (archivo temporal + os.replace): el portal nunca lee un JSON a medias. La vista en vivo
es de mejor esfuerzo: si una escritura falla se cuenta y se sigue; nunca detiene el monitor. Estos archivos no
son registro primario (no se versionan): todo lo que muestran apunta a eventos, ventanas y alertas persistidos.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2

from monitor.alerts import window_stats
from monitor.draw import draw_detections
from monitor.source import utc_now_iso

LIVE_DIR = "datos/vivo"


def _atomic_write(path: Path, data: bytes, retries: int = 3) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    for i in range(retries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:          # Windows: el lector tiene el archivo abierto en ese instante
            time.sleep(0.05 * (i + 1))
    raise PermissionError(f"no se pudo reemplazar {path}")


class LiveWriter:
    def __init__(self, live_dir: str | Path, manifest: dict, frames_target: int | None):
        self.dir = Path(live_dir) / manifest["run_id"]
        self.dir.mkdir(parents=True, exist_ok=True)
        cfg = manifest["config"]
        self.floor, self.lower = cfg["policy"]["analysis_floor"], cfg["policy"]["lower"]
        self.size = cfg["windows"]["size_frames"]
        self.base = {
            "run_id": manifest["run_id"], "label": manifest.get("label"), "mode": manifest.get("mode"),
            "started_at": manifest["started_at"], "config_ref": manifest["config_ref"],
            "model_ref_configurado": manifest["model_ref"], "interval_s": cfg["source"]["sample_interval_s"],
            "frames_objetivo": frames_target, "thresholds": {"lower": cfg["policy"]["lower"], "upper": cfg["policy"]["upper"]},
        }
        self.write_errors = 0
        self.last_frame_at: str | None = None
        self.last_frame_seq: int | None = None
        self.state: dict = {}

    def update(self, ev, image, window_buf: list, last_window: dict | None, open_alerts: dict,
               source_state: dict, counts: dict) -> None:
        try:
            if image is not None:
                img = draw_detections(image.copy(), [d.model_dump() for d in ev.detections])
                ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok:
                    _atomic_write(self.dir / "frame.jpg", buf.tobytes())
                    self.last_frame_at, self.last_frame_seq = ev.captured_at, ev.seq
            partial = window_stats(window_buf, self.floor, self.lower) if window_buf else None
            self.state = {
                **self.base, "updated_at": utc_now_iso(), "terminada": False,
                "evento": {"event_id": ev.event_id, "seq": ev.seq, "captured_at": ev.captured_at, "status": ev.status,
                           "n_detections": ev.n_detections, "zone_counts": ev.zone_counts, "low_ratio": ev.low_ratio,
                           "inference_ms": ev.timings.get("inference_ms"), "e2e_ms": ev.e2e_ms,
                           "model_ref": ev.model_ref, "image_ref": ev.image_ref, "error": ev.error},
                "frame": {"seq": self.last_frame_seq, "captured_at": self.last_frame_at},
                "ventana_en_curso": {"idx": (ev.seq - 1) // self.size + 1, "n": len(window_buf), "de": self.size,
                                     **({k: partial[k] for k in ("n_valid", "n_eligible", "low_ratio", "mean_score")}
                                        if partial else {})},
                "ultima_ventana": last_window,
                "alertas_abiertas": sorted(open_alerts.values(), key=lambda a: a["timestamp"]),
                "fuente": source_state, "contadores": dict(counts), "errores_escritura_vivo": self.write_errors,
            }
            _atomic_write(self.dir / "estado.json", json.dumps(self.state, ensure_ascii=False).encode("utf-8"))
        except Exception:                 # mejor esfuerzo: la vista en vivo nunca detiene el monitor
            self.write_errors += 1

    def finish(self, counts: dict, finished_at: str) -> None:
        try:
            self.state = {**(self.state or self.base), "updated_at": utc_now_iso(), "terminada": True,
                          "finished_at": finished_at, "contadores": dict(counts),
                          "errores_escritura_vivo": self.write_errors}
            _atomic_write(self.dir / "estado.json", json.dumps(self.state, ensure_ascii=False).encode("utf-8"))
        except Exception:
            self.write_errors += 1


# -- lectura (portal) -------------------------------------------------------------
def _age_s(ts: str | None, now: datetime) -> float | None:
    if not ts:
        return None
    return round((now - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds(), 1)


def classify(state: dict, now: datetime | None = None) -> dict:
    """Estado de la ejecución para la vista en vivo, con las antigüedades que lo justifican."""
    now = now or datetime.now(timezone.utc)
    interval = state.get("interval_s") or 2.0
    stale_after = max(10.0, 5 * interval)
    age = _age_s(state.get("updated_at"), now)
    frame_age = _age_s((state.get("frame") or {}).get("captured_at"), now)
    ev = state.get("evento") or {}
    if state.get("terminada"):
        status, why = "terminada", f"terminó {state.get('finished_at')}"
    elif age is None or age > stale_after:
        status, why = "estancada", f"el monitor no reporta desde hace {age} s (plazo {stale_after:g} s)"
    elif ev.get("status") == "invalid_input":
        status, why = "fuente_caida", (f"las últimas muestras no traen frame; último frame recibido hace {frame_age} s"
                                       if frame_age is not None else "todavía no se ha recibido ningún frame")
    else:
        status, why = "en_curso", f"último reporte hace {age} s"
    return {"status": status, "motivo": why, "edad_s": age, "edad_frame_s": frame_age, "plazo_s": stale_after}


def list_live(live_dir: Path) -> list[dict]:
    out = []
    if not live_dir.is_dir():
        return out
    for d in live_dir.iterdir():
        f = d / "estado.json"
        if f.is_file():
            try:
                st = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out.append({**st, "vivo": classify(st)})
    return sorted(out, key=lambda s: s.get("updated_at") or "", reverse=True)


def read_live(live_dir: Path, run_id: str) -> dict | None:
    f = live_dir / run_id / "estado.json"
    if not f.is_file():
        return None
    st = json.loads(f.read_text(encoding="utf-8"))
    return {**st, "vivo": classify(st)}
