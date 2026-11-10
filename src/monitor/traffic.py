"""Escenario S5 · Poco tráfico: la escena casi vacía deja a las reglas de confianza sin evidencia.

Problema. Una ventana necesita >= min_eligible detecciones elegibles para evaluarse. Con poco tráfico (de
madrugada, una calle cerrada) la fuente y el modelo funcionan, pero las ventanas quedan `insuficiente`: las
reglas de confianza no se evalúan y el monitor queda ciego sin decir por qué. `insuficiente` también es lo que
deja una caída de la fuente (S1), así que el estado solo no distingue las dos causas.

Diagnóstico (regla `trafico-bajo-v1`). Una ventana es de poco tráfico si la fuente está sana (n_valid >=
min_valid_frames) y aun así tiene menos de min_eligible elegibles. `consecutive_windows` ventanas así seguidas
abren UNA alerta, con las detecciones por frame frente a la referencia (≈ 25 por frame en config/referencia.json).

Reacción (ventana extendida). Las ventanas de poco tráfico seguidas se acumulan hasta reunir min_eligible
elegibles, como mucho `max_extension_windows` ventanas, y las reglas de confianza se evalúan sobre ese conjunto
(mismas señales, umbrales y población). Así el monitor sigue diciendo si la confianza es normal, con menos
resolución temporal. Si ni así se reúne la evidencia, queda `insuficiente_aun_extendida`. La evaluación
extendida es un diagnóstico: no abre ni resuelve las alertas de confianza, que siguen definidas por ventana (R3).

Recuperación. La primera ventana suficiente resuelve la alerta con la duración, las ventanas de poco tráfico,
las evaluaciones extendidas y el tiempo ciego con y sin la reacción.

Inyección (`--inject-low-traffic MODO:INICIO:DURACION`, mismo reloj de stream que S1–S4). Sobre una escena
vacía (mediana temporal de una grabación: quita lo que se mueve) se deja ver solo una zona del frame real con
borde suavizado (`zona`), o nada (`vacia`). Determinista; las capturas guardan el frame inyectado.

    uv run python -m monitor.traffic escena-vacia <grabación> <salida.jpg>   # mediana temporal
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from monitor.alerts import VALID, Window, _check, _secs, window_stats
from monitor.events import InferenceEvent
from monitor.source import utc_now_iso

MODES = ("zona", "vacia")


@dataclass
class LowTrafficFault:
    mode: str
    start_s: float
    duration_s: float

    @classmethod
    def parse(cls, spec: str) -> "LowTrafficFault":
        parts = spec.split(":")
        if len(parts) != 3 or parts[0] not in MODES:
            raise ValueError("--inject-low-traffic: usa zona:INICIO:DURACION o vacia:INICIO:DURACION")
        try:
            return cls(parts[0], float(parts[1]), float(parts[2]))
        except ValueError:
            raise ValueError("--inject-low-traffic: INICIO y DURACION son segundos de stream")

    def active_at(self, t: float) -> bool:
        return self.start_s <= t < self.start_s + self.duration_s

    def as_dict(self) -> dict:
        return {"mode": self.mode, "start_s": self.start_s, "duration_s": self.duration_s}


class SceneComposer:
    """Escena vacía + (en modo zona) una zona del frame real con borde suavizado."""

    def __init__(self, inj: dict, mode: str, shape: tuple[int, int]):
        import cv2

        plate = cv2.imread(str(inj["plate"]))
        if plate is None:
            raise FileNotFoundError(f"low_traffic.injection.plate: no se puede leer {inj['plate']}")
        h, w = shape
        self.plate = cv2.resize(plate, (w, h)).astype(np.float32) if plate.shape[:2] != (h, w) else plate.astype(np.float32)
        m = np.zeros((h, w), np.float32)
        if mode == "zona":
            x0, y0, x1, y1 = inj["box"]
            m[y0:y1, x0:x1] = 1.0
            m = cv2.GaussianBlur(m, (0, 0), inj.get("feather", 25))
        self.mask = m[..., None]

    def apply(self, image: np.ndarray) -> np.ndarray:
        return (image.astype(np.float32) * self.mask + self.plate * (1 - self.mask)).round().astype(np.uint8)


def build_plate(recording: Path, out: Path, every: int = 3) -> dict:
    import cv2

    files = sorted(recording.glob("*.jpg"))[::every]
    plate = np.median(np.stack([cv2.imread(str(f)) for f in files]), axis=0).astype(np.uint8)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), plate, [cv2.IMWRITE_JPEG_QUALITY, 95])
    meta = {"generated_at": utc_now_iso(), "grabacion": recording.as_posix(), "frames_usados": len(files),
            "cada": every, "metodo": "mediana temporal por píxel (quita lo que se mueve)"}
    out.with_suffix(".json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return meta


def reference_eligible_per_frame(cfg: dict) -> float | None:
    p = Path((cfg.get("reference") or {}).get("stats_file") or "")
    if not p.is_file():
        return None
    ref = json.loads(p.read_text(encoding="utf-8"))
    n = (ref.get("frame_mean_eligible") or {}).get("n")
    return round(ref["eligible_total"] / n, 2) if n and ref.get("eligible_total") else None


class TrafficMonitor:
    """Regla trafico-bajo-v1 con reacción de ventana extendida.

    observe(ev) con cada evento; add(win) con cada ventana cerrada -> (alertas, actualizaciones, registros
    para trafico.jsonl)."""

    def __init__(self, cfg: dict, config_ref: str, run_id: str):
        self.rule = cfg.get("low_traffic")
        w, p = cfg["windows"], cfg["policy"]
        self.size, self.min_valid, self.min_eligible = w["size_frames"], w["min_valid_frames"], w["min_eligible"]
        self.floor, self.lower = p["analysis_floor"], p["lower"]
        self.rules = cfg.get("alerts", [])
        self.interval = cfg["source"]["sample_interval_s"]
        self.ref_epf = reference_eligible_per_frame(cfg) if self.rule else None
        self.config_ref, self.run_id = config_ref, run_id
        self.events: list[InferenceEvent] = []
        self.pool: list[tuple[Window, list[InferenceEvent]]] = []
        self.streak: list[Window] = []
        self.open: dict | None = None

    def observe(self, ev: InferenceEvent) -> None:
        if self.rule:
            self.events.append(ev)

    def classify(self, win: Window) -> str:
        if win.state in ("normal", "fuera_de_rango"):
            return "suficiente"
        if win.state == "insuficiente" and win.n_valid >= self.min_valid and win.n_eligible < self.min_eligible:
            return "poco_trafico"
        if win.state == "insuficiente":
            return "fuente"                         # pocas muestras válidas: es S1, no poco tráfico
        return win.state                            # incompleta, otra_version: no aplican

    def add(self, win: Window) -> tuple[list[dict], list[dict], list[dict]]:
        if not self.rule:
            return [], [], []
        events, self.events = self.events, []
        kind = self.classify(win)
        epf = round(win.n_eligible / win.n_valid, 2) if win.n_valid else None
        logs = [{"run_id": self.run_id, "type": "ventana", "idx": win.idx, "window_id": win.window_id,
                 "clasificacion": kind, "n_valid": win.n_valid, "n_eligible": win.n_eligible,
                 "elegibles_por_frame": epf, "referencia_por_frame": self.ref_epf}]
        alerts, updates = [], []
        if kind == "poco_trafico":
            self.streak.append(win)
            if self.open is None and len(self.streak) >= self.rule["consecutive_windows"]:
                a = self._make_alert(self.streak)
                alerts.append(a)
                self.open = {"alert_id": a["alert_id"], "first": self.streak[0], "low": list(self.streak),
                             "ext": []}
            elif self.open is not None:
                self.open["low"].append(win)
            self.pool.append((win, events))
            logs += self._try_extend()
            return alerts, updates, logs
        self.streak = []
        logs += self._drop_pool("interrumpida: terminó el poco tráfico" if kind == "suficiente"
                                else f"interrumpida: ventana {kind}")
        if kind == "suficiente" and self.open is not None:
            updates.append(self._resolve(win))
        return alerts, updates, logs

    # -- reacción -----------------------------------------------------------------------
    def _extended(self, state_override: str | None = None, note: str | None = None) -> dict:
        wins = [w for w, _ in self.pool]
        evs = [e for _, es in self.pool for e in es]
        st = window_stats(evs, self.floor, self.lower)
        ok = state_override is None
        cond = {r["rule_id"]: (_check(st[r["signal"]], r["op"], r["threshold"]) if ok else None) for r in self.rules}
        state = state_override or ("fuera_de_rango" if any(cond.values()) else "normal")
        rec = {"run_id": self.run_id, "type": "extendida", "ventanas": [w.idx for w in wins],
               "window_ids": [w.window_id for w in wins], "first_event_id": wins[0].first_event_id,
               "last_event_id": wins[-1].last_event_id, "start": wins[0].start, "end": wins[-1].end,
               **st, "estado": state, "condiciones": cond, "nota": note}
        if self.open is not None:
            self.open["ext"].append(rec)
        self.pool = []
        return rec

    def _try_extend(self) -> list[dict]:
        n = sum(w.n_eligible for w, _ in self.pool)
        if n >= self.min_eligible:
            return [self._extended()]
        if len(self.pool) >= self.rule["max_extension_windows"]:
            return [self._extended("insuficiente_aun_extendida",
                                   f"{n} elegibles en {len(self.pool)} ventanas (< {self.min_eligible})")]
        return []

    def _drop_pool(self, why: str) -> list[dict]:
        if not self.pool:
            return []
        return [self._extended("sin_completar", why)]

    def _resolve(self, win: Window) -> dict:
        ep, self.open = self.open, None
        low = ep["low"]
        covered = sum(len(x["ventanas"]) for x in ep["ext"] if x["estado"] in ("normal", "fuera_de_rango"))
        span = self.size * self.interval
        return {"alert_id": ep["alert_id"], "status": "resolved", "resolved_at": win.end,
                "resolved_by_window": win.window_id, "window_end": low[-1].end,
                "duration_s": round(_secs(ep["first"].start, win.start), 1), "n_low_windows": len(low),
                "n_extended": sum(1 for x in ep["ext"] if x["estado"] in ("normal", "fuera_de_rango")),
                "extended_states": [x["estado"] for x in ep["ext"]], "windows_covered": covered,
                "blind_s_without_reaction": round(len(low) * span, 1),
                "blind_s_with_reaction": round((len(low) - covered) * span, 1),
                "recovered_by": "evidencia_suficiente"}

    def _make_alert(self, wins: list[Window]) -> dict:
        r, w = self.rule, wins[-1]
        epf = round(w.n_eligible / w.n_valid, 2)
        frac = round(epf / self.ref_epf, 3) if self.ref_epf else None
        return {
            "alert_id": f"alert-{self.run_id}-{r['rule_id']}-w{wins[0].idx:04d}",
            "rule_id": r["rule_id"], "run_id": self.run_id, "timestamp": utc_now_iso(),
            "severity": r["severity"], "status": "open",
            "reason": (f"Poco tráfico: {len(wins)} ventana(s) seguidas con la fuente sana ({w.n_valid}/{w.n_frames} "
                       f"frames válidos) pero solo {w.n_eligible} detecciones elegibles (< {self.min_eligible}): "
                       f"{epf} por frame frente a {self.ref_epf} en la referencia. Las reglas de confianza no se "
                       f"pueden evaluar por ventana; se evalúan sobre ventanas extendidas (hasta "
                       f"{r['max_extension_windows']})."),
            "suggested_action": r["suggested_action"], "config_ref": self.config_ref,
            "window_start": wins[0].start, "window_end": w.end, "n_windows": len(wins), "suppressed_repeats": 0,
            "metric": {"signal": "elegibles_por_ventana", "op": "<", "threshold": self.min_eligible,
                       "value": w.n_eligible,
                       "diagnostico": {"causa": "poco_trafico", "fuente_sana": True,
                                       "frames_validos": f"{w.n_valid}/{w.n_frames}",
                                       "elegibles_por_frame": epf, "referencia_por_frame": self.ref_epf,
                                       "fraccion_de_la_referencia": frac},
                       "reaccion": {"max_extension_windows": r["max_extension_windows"]}},
            "event_ids": [x for v in wins for x in v.event_ids], "image_refs": [x for v in wins for x in v.image_refs],
        }


def main() -> None:
    ap = argparse.ArgumentParser(description="Escenario S5 · poco tráfico")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("escena-vacia", help="mediana temporal de una grabación (quita lo que se mueve)")
    p.add_argument("grabacion")
    p.add_argument("salida")
    p.add_argument("--cada", type=int, default=3)
    args = ap.parse_args()
    meta = build_plate(Path(args.grabacion), Path(args.salida), args.cada)
    print(f"Escrito {args.salida}: mediana de {meta['frames_usados']} frames de {meta['grabacion']}")


if __name__ == "__main__":
    main()
