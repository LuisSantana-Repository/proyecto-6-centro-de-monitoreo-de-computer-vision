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


@dataclass
class InferenceFault:
    """Escenario S3: fallo de la etapa de inferencia mientras la fuente sigue entregando frames.

    - `corrupcion:INICIO`: en INICIO (segundos de stream) se corrompe el estado del modelo en memoria (los
      pesos de la primera convolución quedan con una forma inválida). Cada inferencia falla con el error real
      de torch hasta que el modelo se recarga desde los pesos verificados: la reacción lo arregla.
    - `persistente:INICIO:DURACION`: durante ese intervalo la inferencia falla siempre (simula un dispositivo
      o una dependencia que no responde, externa al modelo): recargar el modelo no lo arregla.

    Mismo reloj que S1 y S2: (seq - 1) × sample_interval_s, determinista en vivo y en replay."""
    mode: str
    start_s: float
    duration_s: float | None = None
    fired: bool = False

    MODES = ("corrupcion", "persistente")

    @classmethod
    def parse(cls, spec: str) -> "InferenceFault":
        parts = spec.split(":")
        mode = parts[0]
        if mode == "corrupcion" and len(parts) == 2:
            return cls(mode, float(parts[1]))
        if mode == "persistente" and len(parts) == 3:
            return cls(mode, float(parts[1]), float(parts[2]))
        raise ValueError("--inject-inference-error: usa corrupcion:INICIO o persistente:INICIO:DURACION")

    def apply(self, detector: "Detector", t: float) -> None:
        """Se llama justo antes de cada inferencia, con el tiempo de stream de la muestra."""
        if self.mode == "corrupcion" and not self.fired and t >= self.start_s:
            self.fired = True               # una sola vez, aunque la corrupción misma fallara
            corrupt_weights(detector)
        elif self.mode == "persistente" and self.start_s <= t < self.start_s + self.duration_s:
            raise RuntimeError("dispositivo de inferencia no disponible (fallo inyectado, escenario S3)")

    def as_dict(self) -> dict:
        return {"mode": self.mode, "start_s": self.start_s, "duration_s": self.duration_s}


def corrupt_weights(detector: "Detector") -> None:
    """Deja la primera convolución con 1 canal de entrada en lugar de 3: el siguiente forward falla con el
    RuntimeError de torch, y sigue fallando hasta recargar los pesos desde el archivo."""
    import torch

    # el predictor envuelve el modelo en un AutoBackend que no lo registra como submódulo: se toma el interno
    backend = detector.model.predictor.model if detector.model.predictor else None
    model = getattr(backend, "model", None) or detector.model.model
    conv = next(m for m in model.modules() if isinstance(m, torch.nn.Conv2d))
    conv.weight.data = conv.weight.data[:, :1].clone()


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
