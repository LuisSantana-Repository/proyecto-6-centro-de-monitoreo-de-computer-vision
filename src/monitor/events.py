"""Contrato del evento de inferencia y escritura en JSONL."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel


class Detection(BaseModel):
    class_id: int
    class_name: str
    score: float
    bbox_xyxy: list[float]
    zone: Literal["accepted", "doubt", "low"]


class InferenceEvent(BaseModel):
    event_id: str
    run_id: str
    frame_id: str
    seq: int
    timestamp: str                 # cuándo se emitió el evento (UTC)
    captured_at: str               # cuándo se obtuvo el frame (UTC)
    source_id: str
    origin: str | None = None      # archivo de origen en modo replay
    task: Literal["detection"] = "detection"
    model_ref: str
    config_ref: str
    thresholds: dict               # {"lower":..,"upper":..} usados en este evento
    status: Literal["ok", "no_detections", "inference_error", "invalid_input"]
    detections: list[Detection] = []
    n_detections: int = 0
    zone_counts: dict[str, int] = {}
    low_ratio: float | None = None     # detecciones low / detecciones puntuadas (None si no hay)
    timings: dict = {}
    e2e_ms: float | None = None        # captura -> evento listo
    image_ref: str | None = None
    capture_reason: str | None = None
    error: str | None = None


class EventWriter:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(path, "a", encoding="utf-8")

    def write(self, ev: InferenceEvent) -> None:
        self._f.write(json.dumps(ev.model_dump(), ensure_ascii=False) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()
