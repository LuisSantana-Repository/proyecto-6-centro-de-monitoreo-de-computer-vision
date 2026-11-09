"""Envoltura del detector YOLO (Ultralytics) con parámetros fijados por la configuración."""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np


@dataclass
class SwapPlan:
    """Escenario S2: un "despliegue" reemplaza el modelo por `name` (de `model_alternatives`) de INICIO a
    INICIO+DURACION segundos de stream, sin cambiar la configuración, y luego se revierte.

    El tiempo es el del stream: (seq - 1) × sample_interval_s, igual en vivo y en replay, así que el
    cambio cae siempre en las mismas muestras (determinista)."""
    name: str
    start_s: float
    duration_s: float

    @classmethod
    def parse(cls, spec: str) -> "SwapPlan":
        name, start, dur = spec.split(":")
        return cls(name, float(start), float(dur))

    def active_at(self, t: float) -> bool:
        return self.start_s <= t < self.start_s + self.duration_s

    def as_dict(self) -> dict:
        return {"name": self.name, "start_s": self.start_s, "duration_s": self.duration_s}


class Detector:
    def __init__(self, cfg: dict, model_sha256: str, weights: str | None = None, name: str | None = None):
        """Por defecto carga el modelo de `cfg.model`. `weights`/`name` cargan otro artefacto con los mismos
        parámetros de inferencia (escenario S2); model_ref siempre refleja lo que realmente se cargó."""
        from ultralytics import YOLO

        m = cfg["model"]
        self.model = YOLO(weights or m["weights"])
        self.names: dict[int, str] = self.model.names
        self.params = dict(
            imgsz=m["imgsz"],
            conf=m["conf_floor"],
            iou=m["nms_iou"],
            classes=m["classes"],
            max_det=m["max_det"],
            device=m["device"],
            verbose=False,
        )
        self.model_ref = f"{name or m['name']}@sha256:{model_sha256[:12]}"

    def warmup(self) -> None:
        self.model.predict(np.zeros((720, 1280, 3), dtype=np.uint8), **self.params)

    def predict(self, image: np.ndarray) -> tuple[list[dict], dict]:
        t0 = time.perf_counter()
        r = self.model.predict(image, **self.params)[0]
        total_ms = (time.perf_counter() - t0) * 1000
        dets = []
        if r.boxes is not None and len(r.boxes):
            xyxy = r.boxes.xyxy.cpu().numpy()
            conf = r.boxes.conf.cpu().numpy()
            cls = r.boxes.cls.cpu().numpy().astype(int)
            for b, s, c in zip(xyxy, conf, cls):
                dets.append({
                    "class_id": int(c),
                    "class_name": self.names[int(c)],
                    "score": round(float(s), 4),
                    "bbox_xyxy": [round(float(v), 1) for v in b],
                })
        timings = {
            "preprocess_ms": round(r.speed["preprocess"], 2),
            "inference_ms": round(r.speed["inference"], 2),   # solo el modelo
            "postprocess_ms": round(r.speed["postprocess"], 2),
            "predict_total_ms": round(total_ms, 2),
        }
        return dets, timings
