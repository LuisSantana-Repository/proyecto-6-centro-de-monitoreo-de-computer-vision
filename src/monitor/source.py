"""Fuentes visuales: cámara en vivo de YouTube (HLS) y reproducción de una secuencia grabada.

Ambas entregan frames a un ritmo fijo (sample_interval_s). La fuente en vivo lee el stream
continuamente en un hilo (grab) y solo decodifica el frame más reciente cuando toca muestrear;
así la inferencia no se queda atrás del video.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Frame:
    seq: int
    source_id: str
    captured_at: str            # UTC ISO del momento en que se obtuvo el frame
    t_capture: float            # time.perf_counter() del mismo momento (para latencia e2e)
    image: np.ndarray | None    # None = lectura/decodificación fallida
    origin: str | None = None   # replay: archivo de origen


@dataclass
class SourceStats:
    frames_received: int = 0        # frames leídos del stream (30 fps en vivo)
    frames_sampled: int = 0         # frames entregados a inferencia
    dropped_sampling: int = 0       # descartados por la política de muestreo
    dropped_saturation: int = 0     # ticks de muestreo perdidos porque la inferencia tardó más que el intervalo
    read_failures: int = 0          # grab() fallidos del stream
    decode_failures: int = 0        # frames muestreados que no se pudieron obtener/decodificar
    reconnections: int = 0
    last_frame_at: str | None = None
    stream_meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["dropped_sampling"] = max(0, self.frames_received - self.frames_sampled)
        return d


class LiveYouTubeSource:
    def __init__(self, cfg: dict):
        s = cfg["source"]
        self.source_id = s["id"]
        self.url = s["url"]
        self.fmt = s["format"]
        self.interval = float(s["sample_interval_s"])
        self.fail_limit = int(s["reconnect_after_failures"])
        self.backoff = float(s["reconnect_backoff_s"])
        self.stats = SourceStats()
        self._cap: cv2.VideoCapture | None = None
        self._lock = threading.Lock()
        self._want = threading.Event()
        self._ready = threading.Event()
        self._handoff: tuple | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- conexión -------------------------------------------------------------
    def _resolve(self) -> str:
        import yt_dlp

        opts = {"quiet": True, "no_warnings": True, "format": self.fmt, "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(self.url, download=False)
        self.stats.stream_meta = {
            "title": info.get("title"),
            "channel": info.get("channel"),
            "live_status": info.get("live_status"),
            "format_id": info.get("format_id"),
            "width": info.get("width"),
            "height": info.get("height"),
            "fps": info.get("fps"),
            "resolved_at": utc_now_iso(),
        }
        return info["url"]

    def _connect(self) -> None:
        if self._cap is not None:
            self._cap.release()
        stream_url = self._resolve()  # la URL HLS caduca: se resuelve en cada conexión
        self._cap = cv2.VideoCapture(stream_url)
        if not self._cap.isOpened():
            raise ConnectionError("OpenCV no pudo abrir el stream")

    def _reader(self) -> None:
        fails = 0
        while not self._stop.is_set():
            try:
                if self._cap is None or not self._cap.isOpened():
                    self._connect()
                ok = self._cap.grab()
            except Exception:
                ok = False
            if not ok:
                self.stats.read_failures += 1
                fails += 1
                if fails >= self.fail_limit:
                    self.stats.reconnections += 1
                    time.sleep(self.backoff)
                    try:
                        self._connect()
                        fails = 0
                    except Exception:
                        pass
                continue
            fails = 0
            self.stats.frames_received += 1
            if self._want.is_set():
                self._want.clear()
                ok, img = self._cap.retrieve()
                self._handoff = (utc_now_iso(), time.perf_counter(), img if ok else None)
                self._ready.set()

    def start(self) -> None:
        self._connect()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._want.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._cap is not None:
            self._cap.release()

    # -- muestreo -------------------------------------------------------------
    def frames(self, max_frames: int | None = None):
        seq = 0
        next_t = time.perf_counter()
        while max_frames is None or seq < max_frames:
            now = time.perf_counter()
            if now < next_t:
                time.sleep(next_t - now)
            else:
                missed = int((now - next_t) // self.interval)
                self.stats.dropped_saturation += missed
                next_t += missed * self.interval
            next_t += self.interval

            self._ready.clear()
            self._want.set()
            if not self._ready.wait(timeout=30):
                captured_at, t_cap, img = utc_now_iso(), time.perf_counter(), None
            else:
                captured_at, t_cap, img = self._handoff
            if img is None:
                self.stats.decode_failures += 1
            else:
                self.stats.last_frame_at = captured_at
            seq += 1
            self.stats.frames_sampled += 1
            yield Frame(seq, self.source_id, captured_at, t_cap, img)


class ReplaySource:
    """Reproduce una carpeta de frames .jpg como stream, al mismo ritmo declarado."""

    def __init__(self, cfg: dict, replay_dir: str | Path):
        self.source_id = cfg["source"]["id"] + "-replay"
        self.interval = float(cfg["source"]["sample_interval_s"])
        self.files = sorted(Path(replay_dir).glob("*.jpg"))
        if not self.files:
            raise FileNotFoundError(f"No hay .jpg en {replay_dir}")
        self.stats = SourceStats(stream_meta={"replay_dir": str(replay_dir), "n_files": len(self.files)})

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def frames(self, max_frames: int | None = None):
        next_t = time.perf_counter()
        for seq, path in enumerate(self.files, start=1):
            if max_frames is not None and seq > max_frames:
                break
            now = time.perf_counter()
            if now < next_t:
                time.sleep(next_t - now)
            next_t = max(next_t, now) + self.interval
            img = cv2.imread(str(path), cv2.IMREAD_COLOR)
            self.stats.frames_received += 1
            self.stats.frames_sampled += 1
            captured_at = utc_now_iso()
            if img is None:
                self.stats.decode_failures += 1
            else:
                self.stats.last_frame_at = captured_at
            yield Frame(seq, self.source_id, captured_at, time.perf_counter(), img, origin=path.name)
