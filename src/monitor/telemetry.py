"""Escenario S4 · pérdida de telemetría: los destinos de escritura del monitor fallan y no se pierde nada.

El monitor escribe en dos destinos:

- **registro primario**: los JSONL de la ejecución (events, windows, alerts, fuente, inferencia, telemetria).
  `BufferedLog` intenta cada escritura; si falla (p. ej. disco lleno), la línea queda en un búfer en memoria
  y se reintenta en cada muestra, en el mismo orden. Si el búfer llega a `max_buffer` líneas, las más
  antiguas se descartan y se cuentan como perdidas: la pérdida es explícita, nunca silenciosa.
- **índice** (SQLite, para el portal): `IndexSink` encola cada operación y la aplica en orden; si la base
  está bloqueada o no responde, la cola espera a la siguiente muestra. Todas las operaciones del índice son
  idempotentes (INSERT OR IGNORE / REPLACE / UPDATE), así que reintentar es seguro. Si al cerrar sigue
  bloqueada, lo pendiente se recupera con `monitor.ingest`, porque el registro primario lo tiene todo.

`TelemetryMonitor` es la regla `telemetria-perdida-v1`: `consecutive_failures` muestras seguidas con un
destino fallando abren UNA alerta por destino; se resuelve cuando ese destino vuelve a escribir y su
pendiente queda en 0 durante `recover_after_ok` muestras. Se alimenta de `telemetria.jsonl` (una línea por
muestra con el estado de cada destino), así que `monitor.evaluate` la reproduce sin depender de los eventos.

Inyección (`--inject-telemetry-loss DESTINO:INICIO:DURACION`, reloj de stream como S1–S3):
- `indice`: otra conexión toma un candado EXCLUSIVO real sobre la base SQLite durante el intervalo;
- `registro`: cada escritura de los JSONL falla con ENOSPC (disco lleno simulado) durante el intervalo.
"""

from __future__ import annotations

import errno
import json
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from monitor.source import utc_now_iso

SINKS = ("indice", "registro")


@dataclass
class TelemetryFault:
    sink: str
    start_s: float
    duration_s: float
    _lock: sqlite3.Connection | None = field(default=None, repr=False)

    @classmethod
    def parse(cls, spec: str) -> "TelemetryFault":
        try:
            sink, start, dur = spec.split(":")
            f = cls(sink, float(start), float(dur))
        except ValueError:
            raise ValueError("--inject-telemetry-loss: usa DESTINO:INICIO:DURACION (indice o registro)")
        if sink not in SINKS:
            raise ValueError(f"--inject-telemetry-loss: destino {sink!r}; válidos: {', '.join(SINKS)}")
        return f

    def active_at(self, t: float) -> bool:
        return self.start_s <= t < self.start_s + self.duration_s

    def as_dict(self) -> dict:
        return {"sink": self.sink, "start_s": self.start_s, "duration_s": self.duration_s}

    # -- índice: candado real tomado por otra conexión -----------------------------
    def drive_index_lock(self, db_path: str, t: float) -> None:
        """Llamar en cada muestra: toma el candado al entrar al intervalo y lo suelta al salir."""
        if self.sink != "indice":
            return
        if self.active_at(t) and self._lock is None:
            self._lock = sqlite3.connect(db_path, timeout=5, isolation_level=None, check_same_thread=False)
            self._lock.execute("BEGIN EXCLUSIVE")
        elif not self.active_at(t) and self._lock is not None:
            self.release()

    def release(self) -> None:
        if self._lock is not None:
            self._lock.execute("ROLLBACK")
            self._lock.close()
            self._lock = None


class BufferedLog:
    """JSONL del registro primario con búfer ante fallos de escritura. `clock()` da el tiempo de stream."""

    def __init__(self, path: Path, status: "SinkStatus", fault: TelemetryFault | None = None,
                 clock=lambda: 0.0, max_buffer: int = 1000):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._f = open(path, "a", encoding="utf-8")
        self.status, self.fault, self.clock, self.max_buffer = status, fault, clock, max_buffer
        self.pending: deque[str] = deque()

    def write(self, rec) -> None:
        # un evento (modelo pydantic) se serializa igual que EventWriter: json.dumps de model_dump()
        line = json.dumps(rec.model_dump() if hasattr(rec, "model_dump") else rec, ensure_ascii=False)
        self.pending.append(line)
        if len(self.pending) > self.max_buffer:
            self.pending.popleft()
            self.status.lost += 1
        if not self.status.failed:     # ya falló en esta muestra: se reintenta en la siguiente
            self.flush()

    def flush(self) -> None:
        while self.pending:
            try:
                if self.fault and self.fault.sink == "registro" and self.fault.active_at(self.clock()):
                    raise OSError(errno.ENOSPC, "No queda espacio en el dispositivo (fallo inyectado, escenario S4)")
                self._f.write(self.pending[0] + "\n")
                self._f.flush()
            except OSError as e:
                self.status.fail(e)
                return
            self.pending.popleft()
            self.status.written += 1

    def close(self) -> None:
        self.flush()
        self._f.close()


@dataclass
class SinkStatus:
    """Estado de un destino durante la muestra actual (lo agrega todo lo que escribe en él)."""
    failed: bool = False
    error: str | None = None
    written: int = 0
    lost: int = 0

    def fail(self, e: Exception) -> None:
        self.failed, self.error = True, f"{type(e).__name__}: {e}"

    def reset(self) -> None:
        self.failed, self.error = False, None


class IndexSink:
    """Cola ordenada de operaciones sobre el Store; se aplica mientras la base responda."""

    def __init__(self, store, status: SinkStatus):
        self.store, self.status = store, status
        self.queue: deque[tuple[str, tuple]] = deque()

    def do(self, op: str, *args) -> None:
        self.queue.append((op, args))
        if not self.status.failed:     # ya falló en esta muestra: no esperar otra vez al candado hasta la siguiente
            self.flush()

    def flush(self) -> None:
        while self.queue:
            op, args = self.queue[0]
            try:
                getattr(self.store, op)(*args)
            except sqlite3.OperationalError as e:
                try:
                    self.store.con.rollback()          # deja la conexión limpia para el reintento
                except sqlite3.Error:
                    pass
                self.status.fail(e)
                return
            self.queue.popleft()
            self.status.written += 1


class TelemetryMonitor:
    """Regla telemetria-perdida-v1, evaluada con una observación por muestra (ver telemetria.jsonl)."""

    def __init__(self, cfg: dict, config_ref: str, run_id: str):
        self.rule = cfg.get("telemetry_health")
        self.config_ref, self.run_id = config_ref, run_id
        self.state = {s: {"bad": [], "good": 0, "open": None, "first_ok": None} for s in SINKS}

    def add(self, obs: dict) -> tuple[list[dict], list[dict]]:
        """obs: {"seq", "at", "indice": {"ok", "pendientes", "error", "perdidos"}, "registro": {...}}."""
        if not self.rule:
            return [], []
        r, new, upd = self.rule, [], []
        for sink in SINKS:
            o, st = obs[sink], self.state[sink]
            if not o["ok"]:
                st["good"] = 0
                st["bad"].append(obs)
                ep = st["open"]
                if ep is None and len(st["bad"]) >= r["consecutive_failures"]:
                    a = self._make_alert(sink, st["bad"])
                    st["open"] = {"alert_id": a["alert_id"], "first": st["bad"][0], "n_failed": len(st["bad"]),
                                  "max_pending": max(b[sink]["pendientes"] for b in st["bad"]), "last_bad": obs}
                    new.append(a)
                elif ep is not None:
                    ep["n_failed"] += 1
                    ep["max_pending"] = max(ep["max_pending"], o["pendientes"])
                    ep["last_bad"] = obs
                continue
            if st["open"] is None:
                st["bad"] = []
                continue
            if o["pendientes"] > 0:                     # escribe otra vez, pero aún vacía lo atrasado
                st["good"] = 0
                continue
            st["good"] += 1
            if st["good"] == 1:
                st["first_ok"] = obs                    # primera muestra escribiendo y sin atrasos
            if st["good"] < r["recover_after_ok"]:
                continue
            ep = st["open"]
            first_ok = st["first_ok"]
            upd.append({"alert_id": ep["alert_id"], "status": "resolved", "resolved_at": obs["at"],
                        "resolved_by_window": f"telemetria-f{obs['seq']:06d}", "window_end": ep["last_bad"]["at"],
                        "sink": sink, "n_failed": ep["n_failed"], "max_pending": ep["max_pending"],
                        "lost": o.get("perdidos", 0), "recovered_at_seq": first_ok["seq"],
                        "duration_s": round(_secs(ep["first"]["at"], first_ok["at"]), 1)})
            st.update(bad=[], good=0, open=None, first_ok=None)
        return new, upd

    def _make_alert(self, sink: str, bad: list[dict]) -> dict:
        r, last = self.rule, bad[-1][sink]
        what = {"indice": "el índice (SQLite) no acepta escrituras: el portal no ve lo nuevo, pero el registro "
                          "primario sigue completo y la cola se aplicará al recuperarse",
                "registro": "el registro primario (JSONL) no acepta escrituras: las líneas esperan en un búfer en "
                            "memoria y se escribirán en orden al recuperarse"}[sink]
        return {
            "alert_id": f"alert-{self.run_id}-{r['rule_id']}-{sink}-f{bad[0]['seq']:06d}",
            "rule_id": r["rule_id"],
            "run_id": self.run_id,
            "timestamp": utc_now_iso(),
            "severity": r["severity"],
            "status": "open",
            "reason": (f"El destino '{sink}' falló en {len(bad)} muestras seguidas desde {bad[0]['at']} "
                       f"(umbral {r['consecutive_failures']}): {what}. Pendientes: {last['pendientes']}. "
                       f"Error: {last['error']}"),
            "suggested_action": r["suggested_action"],
            "config_ref": self.config_ref,
            "window_start": bad[0]["at"],
            "window_end": bad[-1]["at"],
            "n_windows": None,
            "suppressed_repeats": 0,
            "metric": {"signal": "muestras_con_destino_fallando", "op": ">=", "threshold": r["consecutive_failures"],
                       "value": len(bad), "sink": sink, "pendientes": last["pendientes"], "error": last["error"],
                       "recover_after_ok": r["recover_after_ok"], "max_buffer": r.get("max_buffer")},
            "event_ids": [f"evt-{self.run_id}-f{b['seq']:06d}" for b in bad],
            "image_refs": [],
        }


def _secs(a: str, b: str) -> float:
    from datetime import datetime

    f = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
    return (f(b) - f(a)).total_seconds()
