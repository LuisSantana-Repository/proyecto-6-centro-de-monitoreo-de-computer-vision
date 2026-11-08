"""Fuentes visuales: cámara en vivo de YouTube (HLS) y reproducción de una secuencia grabada.

Ambas entregan frames a un ritmo fijo (sample_interval_s). La fuente en vivo lee el stream
continuamente en un hilo (grab) y solo decodifica el frame más reciente cuando toca muestrear;
así la inferencia no se queda atrás del video.

Salud de la fuente: cada transición de conexión (conectado, lectura fallida, desconectado, intento
de reconexión fallido, reconectado) se registra con su hora y su error en un log de diagnóstico que
run.py escribe en fuente.jsonl. Una interrupción se puede inyectar (FaultPlan) para el escenario S1.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def error_text(e: BaseException) -> str:
    """Texto del error para los registros: tipo + mensaje, sin códigos de color ANSI (yt-dlp los usa)."""
    return ANSI.sub("", f"{type(e).__name__}: {e}")

@dataclass
class Frame:
    seq: int
    source_id: str
    captured_at: str            # UTC ISO del momento en que se obtuvo el frame
    t_capture: float            # time.perf_counter() del mismo momento (para latencia e2e)
    image: np.ndarray | None    # None = la fuente no entregó frame o no se pudo decodificar
    origin: str | None = None   # replay: archivo de origen
    error: str | None = None    # motivo cuando image es None


@dataclass
class SourceStats:
    frames_received: int = 0        # frames leídos del stream (30 fps en vivo)
    frames_sampled: int = 0         # frames entregados a inferencia
    dropped_sampling: int = 0       # descartados por la política de muestreo
    # Ticks de muestreo perdidos (el ciclo anterior duró más que sample_interval_s), separados por causa:
    dropped_saturation: int = 0     # ... porque el procesamiento (inferencia + evento) tardó más
    dropped_source_wait: int = 0    # ... porque la fuente tardó en entregar el frame (p. ej. segmento HLS)
    read_failures: int = 0          # grab() fallidos del stream
    decode_failures: int = 0        # muestras sin frame (fuente caída, timeout o decodificación fallida)
    reconnect_attempts: int = 0     # intentos de reconexión (exitosos o no)
    reconnections: int = 0          # reconexiones exitosas
    last_frame_at: str | None = None
    stream_meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["dropped_sampling"] = max(0, self.frames_received - self.frames_sampled)
        return d


@dataclass
class FaultPlan:
    """Interrupción inyectada de la fuente (escenario S1), en segundos desde el inicio del stream.

    En vivo cuenta tiempo real desde que arranca la fuente: mientras está activa, grab() falla y cada
    intento de reconexión se rechaza como si no hubiera red. En replay cuenta tiempo de stream
    ((seq - 1) x sample_interval_s), así que es determinista aunque se reprocese sin pausa.
    """
    start_s: float
    duration_s: float

    @classmethod
    def parse(cls, text: str) -> "FaultPlan":
        start, dur = text.split(":")
        return cls(float(start), float(dur))

    def active_at(self, t: float) -> bool:
        return self.start_s <= t < self.start_s + self.duration_s

    def as_dict(self) -> dict:
        return {"start_s": self.start_s, "duration_s": self.duration_s}


class HealthLog:
    """Registro de diagnóstico de la fuente (fuente.jsonl): transiciones de conexión con su hora y error."""

    def _init_health(self) -> None:
        self._health: list[dict] = []
        self._hlock = threading.Lock()

    def _log(self, type_: str, **detail) -> None:
        with self._hlock:
            self._health.append({"at": utc_now_iso(), "type": type_, **detail})

    def drain_health(self) -> list[dict]:
        with self._hlock:
            out, self._health = self._health, []
        return out


class LiveYouTubeSource(HealthLog):
    def __init__(self, cfg: dict, fault: FaultPlan | None = None):
        s = cfg["source"]
        self.source_id = s["id"]
        self.url = s["url"]
        self.fmt = s["format"]
        self.interval = float(s["sample_interval_s"])
        self.fail_limit = int(s["reconnect_after_failures"])
        self.backoff = float(s["reconnect_backoff_s"])
        self.backoff_max = float(s.get("reconnect_backoff_max_s", self.backoff))  # = backoff: espera constante
        self.stall_timeout = s.get("stall_timeout_s")          # None: desconecta solo por conteo de fallos
        self.frame_timeout = float(s.get("frame_timeout_s", 30.0))
        self.open_timeout_ms = int(1000 * s.get("open_timeout_s", 15))
        self.read_timeout_ms = int(1000 * s.get("read_timeout_s", 10))
        self.fault = fault
        self.stats = SourceStats()
        self.state = "desconectado"
        self._last_error: str | None = None
        self._down_since: float | None = None
        self._t0: float | None = None
        self._cap: cv2.VideoCapture | None = None
        self._want = threading.Event()
        self._ready = threading.Event()
        self._handoff: tuple | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._init_health()

    # -- conexión -------------------------------------------------------------
    def _fault_active(self) -> bool:
        return self.fault is not None and self._t0 is not None and \
            self.fault.active_at(time.perf_counter() - self._t0)

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

    def _open_capture(self, stream_url: str) -> cv2.VideoCapture:
        return cv2.VideoCapture(stream_url, cv2.CAP_FFMPEG, [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.open_timeout_ms,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.read_timeout_ms,
        ])

    def _connect(self) -> None:
        if self._fault_active():
            raise ConnectionError("interrupción inyectada: sin acceso a la red")
        stream_url = self._resolve()  # la URL HLS caduca: se resuelve en cada conexión
        cap = self._open_capture(stream_url)
        if not cap.isOpened():
            cap.release()
            raise ConnectionError("OpenCV no pudo abrir el stream")
        self._cap = cap

    def _grab(self) -> bool:
        if self._fault_active():
            self._last_error = "interrupción inyectada: el stream no entrega datos"
            return False
        return self._cap.grab()

    def _disconnect(self, reason: str) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        self.state = "desconectado"
        self._down_since = time.perf_counter()
        self._log("desconectado", motivo=reason, ultimo_error=self._last_error)

    def _reader(self) -> None:
        fails, attempt = 0, 0
        last_ok = time.perf_counter()
        while not self._stop.is_set():
            if self._cap is None:                      # desconectado: reintentar con espera creciente
                attempt += 1
                self.stats.reconnect_attempts += 1
                self.state = "reconectando"
                try:
                    self._connect()
                except Exception as e:
                    self._last_error = error_text(e)
                    wait = min(self.backoff * 2 ** (attempt - 1), self.backoff_max)
                    self._log("reconexion_fallida", intento=attempt, error=self._last_error, espera_s=wait)
                    self.state = "desconectado"
                    self._stop.wait(wait)
                    continue
                self.stats.reconnections += 1
                self.state = "conectado"
                self._log("reconectado", intento=attempt,
                          caida_s=round(time.perf_counter() - self._down_since, 1),
                          formato=self.stats.stream_meta.get("format_id"))
                fails, attempt, last_ok = 0, 0, time.perf_counter()
                continue
            try:
                ok = self._grab()
            except Exception as e:
                ok, self._last_error = False, error_text(e)
            if not ok:
                self.stats.read_failures += 1
                fails += 1
                if fails == 1:
                    self._log("lectura_fallida", error=self._last_error or "grab() devolvió False")
                stalled_s = time.perf_counter() - last_ok
                if fails >= self.fail_limit or (self.stall_timeout is not None and stalled_s >= self.stall_timeout):
                    self._disconnect(f"{fails} lecturas fallidas seguidas, {stalled_s:.1f} s sin frames")
                    fails = 0
                else:
                    self._stop.wait(0.1)
                continue
            if fails:
                self._log("lectura_recuperada", lecturas_fallidas=fails)
            fails, last_ok = 0, time.perf_counter()
            self._last_error = None
            self.stats.frames_received += 1
            if self._want.is_set():
                self._want.clear()
                ok, img = self._cap.retrieve()
                self._handoff = (utc_now_iso(), time.perf_counter(), img if ok else None)
                self._ready.set()

    def start(self) -> None:
        self._t0 = time.perf_counter()
        self._connect()
        self.state = "conectado"
        meta = self.stats.stream_meta
        self._log("conectado", formato=meta.get("format_id"), resolucion=f"{meta.get('width')}x{meta.get('height')}")
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
        last_wait = last_proc = 0.0
        t_yield = None
        while max_frames is None or seq < max_frames:
            now = time.perf_counter()
            if t_yield is not None:
                last_proc = now - t_yield
            if now < next_t:
                time.sleep(next_t - now)
            else:
                missed = int((now - next_t) // self.interval)
                if last_wait > last_proc:
                    self.stats.dropped_source_wait += missed
                else:
                    self.stats.dropped_saturation += missed
                next_t += missed * self.interval
            next_t += self.interval

            self._ready.clear()
            self._want.set()
            t_req = time.perf_counter()
            got = self._ready.wait(timeout=self.frame_timeout)
            last_wait = time.perf_counter() - t_req
            err = None
            if not got:
                captured_at, t_cap, img = utc_now_iso(), time.perf_counter(), None
                err = (f"la fuente no entregó frame en {self.frame_timeout:g} s (fuente {self.state}; "
                       f"último error: {self._last_error or 'ninguno'})")
            else:
                captured_at, t_cap, img = self._handoff
                if img is None:
                    err = "retrieve() no pudo decodificar el frame"
            if img is None:
                self.stats.decode_failures += 1
            else:
                self.stats.last_frame_at = captured_at
            seq += 1
            self.stats.frames_sampled += 1
            yield Frame(seq, self.source_id, captured_at, t_cap, img, error=err)
            t_yield = time.perf_counter()


class ReplaySource(HealthLog):
    """Reproduce una carpeta de frames .jpg como stream, al mismo ritmo declarado."""

    def __init__(self, cfg: dict, replay_dir: str | Path, pace: bool = True, fault: FaultPlan | None = None):
        self.source_id = cfg["source"]["id"] + "-replay"
        self.stream_interval = float(cfg["source"]["sample_interval_s"])
        self.interval = self.stream_interval if pace else 0.0
        self.fault = fault
        self.files = sorted(Path(replay_dir).glob("*.jpg"))
        if not self.files:
            raise FileNotFoundError(f"No hay .jpg en {replay_dir}")
        self.stats = SourceStats(stream_meta={"replay_dir": Path(replay_dir).as_posix(),
                                              "n_files": len(self.files), "paced": pace,
                                              "interval_s": self.interval})
        self._init_health()

    def start(self) -> None:
        self._log("conectado", replay_dir=self.stats.stream_meta["replay_dir"])

    def stop(self) -> None: ...

    def frames(self, max_frames: int | None = None):
        next_t = time.perf_counter()
        down_at = None
        for seq, path in enumerate(self.files, start=1):
            if max_frames is not None and seq > max_frames:
                break
            now = time.perf_counter()
            if now < next_t:
                time.sleep(next_t - now)
            next_t = max(next_t, now) + self.interval
            t_stream = (seq - 1) * self.stream_interval
            self.stats.frames_sampled += 1
            captured_at = utc_now_iso()
            if self.fault is not None and self.fault.active_at(t_stream):
                # interrupción inyectada: el frame grabado de este instante se pierde
                if down_at is None:
                    down_at = t_stream
                    self._log("desconectado", motivo="interrupción inyectada (replay)", t_stream_s=t_stream)
                self.stats.decode_failures += 1
                yield Frame(seq, self.source_id, captured_at, time.perf_counter(), None, origin=path.name,
                            error=f"interrupción inyectada: la fuente no entregó el frame de t={t_stream:g} s")
                continue
            if down_at is not None:
                self.stats.reconnect_attempts += 1
                self.stats.reconnections += 1
                self._log("reconectado", caida_s=t_stream - down_at, t_stream_s=t_stream)
                down_at = None
            img = cv2.imread(str(path), cv2.IMREAD_COLOR)
            self.stats.frames_received += 1
            err = None
            if img is None:
                self.stats.decode_failures += 1
                err = f"no se pudo decodificar {path.name}"
            else:
                self.stats.last_frame_at = captured_at
            yield Frame(seq, self.source_id, captured_at, time.perf_counter(), img, origin=path.name, error=err)
