"""Envoltura del detector YOLO (Ultralytics) con parámetros fijados por la configuración."""

from __future__ import annotations

import time

import numpy as np


class Detector:
    def __init__(self, cfg: dict, model_sha256: str):
        from ultralytics import YOLO

        m = cfg["model"]
        self.model = YOLO(m["weights"])
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
        self.model_ref = f"{m['name']}@sha256:{model_sha256[:12]}"

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
