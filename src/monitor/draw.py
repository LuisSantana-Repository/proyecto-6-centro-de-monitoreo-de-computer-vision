"""Dibujo de detecciones sobre un frame (compartido por el portal y la vista en vivo)."""

from __future__ import annotations

import cv2
import numpy as np

ZONE_BGR = {"accepted": (60, 170, 40), "doubt": (0, 150, 255), "low": (40, 40, 220)}


def draw_detections(img: np.ndarray, detections: list[dict], show_low: bool = False) -> np.ndarray:
    """Cajas por zona (aceptada verde, duda naranja; baja confianza roja solo si show_low). Modifica `img`."""
    for d in sorted(detections, key=lambda d: d["score"]):
        if d["zone"] == "low" and not show_low:
            continue
        x1, y1, x2, y2 = (int(v) for v in d["bbox_xyxy"])
        color = ZONE_BGR[d["zone"]]
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 1 if d["zone"] == "low" else 2)
        if d["zone"] != "low":
            cv2.putText(img, f"{d['class_name']} {d['score']:.2f}", (x1, max(10, y1 - 3)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA)
    return img
