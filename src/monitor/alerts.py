"""Ventanas de confianza y reglas de alerta.

Ventana = `size_frames` eventos consecutivos de una ejecución (60 = 2 min a 0.5 FPS).
Población elegible = detecciones con s >= analysis_floor en frames con inferencia válida.

Señales por ventana:
  low_ratio  = elegibles con s < lower / elegibles
  mean_score = media de las puntuaciones elegibles

Estados de ventana:
  insuficiente   -> menos de min_valid_frames válidos o menos de min_eligible elegibles
                    (evidencia insuficiente: no cuenta como normal ni como anómala; no rompe ni suma rachas)
  incompleta     -> ventana final con menos de size_frames eventos (no se evalúa)
  normal         -> suficiente y ninguna regla cumple su condición
  fuera_de_rango -> suficiente y al menos una regla cumple su condición

Regla: condición (señal op umbral) en `consecutive` ventanas suficientes seguidas -> se abre UNA alerta
(episodio). Mientras la condición siga, el episodio continúa sin alertas repetidas. La primera ventana
suficiente que ya no cumple la condición resuelve el episodio.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from monitor.events import InferenceEvent
from monitor.source import utc_now_iso

VALID = ("ok", "no_detections")


@dataclass
class Window:
    window_id: str
    run_id: str
    idx: int
    config_ref: str
    first_event_id: str
    last_event_id: str
    start: str                  # captured_at del primer frame
    end: str                    # captured_at del último frame
    n_frames: int
    n_valid: int
    n_eligible: int
    n_low: int
    low_ratio: float | None
    mean_score: float | None
    state: str
    conditions: dict = field(default_factory=dict)   # rule_id -> bool | None (None = no evaluada)
    event_ids: list[str] = field(default_factory=list)
    image_refs: list[str] = field(default_factory=list)

    def record(self) -> dict:
        return asdict(self)


def window_stats(events: list[InferenceEvent], floor: float, lower: float) -> dict:
    valid = [e for e in events if e.status in VALID]
    eligible = [d.score for e in valid for d in e.detections if d.score >= floor]
    n_low = sum(s < lower for s in eligible)
    return {
        "n_frames": len(events),
        "n_valid": len(valid),
        "n_eligible": len(eligible),
        "n_low": n_low,
        "low_ratio": round(n_low / len(eligible), 4) if eligible else None,
        "mean_score": round(sum(eligible) / len(eligible), 4) if eligible else None,
    }


def _check(value: float | None, op: str, threshold: float) -> bool | None:
    if value is None:
        return None
    return value > threshold if op == ">" else value < threshold


SIGNAL_TEXT = {
    "low_ratio": "proporción de baja confianza (s < {lower}) entre detecciones elegibles (s ≥ {floor})",
    "mean_score": "puntuación media de las detecciones elegibles (s ≥ {floor})",
}


class WindowEvaluator:
    def __init__(self, cfg: dict, config_ref: str, run_id: str):
        w, p = cfg["windows"], cfg["policy"]
        self.size = w["size_frames"]
        self.min_valid = w["min_valid_frames"]
        self.min_eligible = w["min_eligible"]
        self.floor = p["analysis_floor"]
        self.lower = p["lower"]
        self.rules = cfg.get("alerts", [])
        self.config_ref = config_ref
        self.run_id = run_id
        self.buf: list[InferenceEvent] = []
        self.idx = 0
        self.streak: dict[str, list[Window]] = {r["rule_id"]: [] for r in self.rules}
        self.open: dict[str, dict | None] = {r["rule_id"]: None for r in self.rules}

    # -- API ----------------------------------------------------------------------
    def add(self, ev: InferenceEvent) -> tuple[Window | None, list[dict], list[dict]]:
        """Agrega un evento. Devuelve (ventana cerrada | None, alertas nuevas, actualizaciones de alertas)."""
        self.buf.append(ev)
        if len(self.buf) < self.size:
            return None, [], []
        events, self.buf = self.buf, []
        return self._close(events, partial=False)

    def flush(self) -> tuple[Window | None, list[dict], list[dict]]:
        if not self.buf:
            return None, [], []
        events, self.buf = self.buf, []
        return self._close(events, partial=True)

    # -- interno ------------------------------------------------------------------
    def _close(self, events: list[InferenceEvent], partial: bool):
        self.idx += 1
        st = window_stats(events, self.floor, self.lower)
        sufficient = (not partial) and st["n_valid"] >= self.min_valid and st["n_eligible"] >= self.min_eligible
        conditions = {
            r["rule_id"]: (_check(st[r["signal"]], r["op"], r["threshold"]) if sufficient else None)
            for r in self.rules
        }
        if partial:
            state = "incompleta"
        elif not sufficient:
            state = "insuficiente"
        else:
            state = "fuera_de_rango" if any(conditions.values()) else "normal"
        win = Window(
            window_id=f"win-{self.run_id}-{self.idx:04d}", run_id=self.run_id, idx=self.idx,
            config_ref=self.config_ref, first_event_id=events[0].event_id, last_event_id=events[-1].event_id,
            start=events[0].captured_at, end=events[-1].captured_at, state=state, conditions=conditions,
            event_ids=[e.event_id for e in events], image_refs=[e.image_ref for e in events if e.image_ref], **st,
        )
        new_alerts, updates = [], []
        if not sufficient:
            return win, new_alerts, updates     # sin evidencia suficiente: no suma ni rompe rachas
        for rule in self.rules:
            rid = rule["rule_id"]
            if conditions[rid]:
                self.streak[rid].append(win)
                if self.open[rid] is not None:
                    ep = self.open[rid]
                    ep["n_windows"] += 1
                    ep["window_end"] = win.end
                    ep["suppressed_repeats"] += 1   # la condición sigue: no se emite alerta repetida
                    updates.append({"alert_id": ep["alert_id"], "n_windows": ep["n_windows"],
                                    "window_end": win.end, "suppressed_repeats": ep["suppressed_repeats"]})
                elif len(self.streak[rid]) >= rule["consecutive"]:
                    alert = self._make_alert(rule, self.streak[rid][-rule["consecutive"]:])
                    # estado del episodio separado del registro de la alerta (que no se modifica)
                    self.open[rid] = {"alert_id": alert["alert_id"], "n_windows": alert["n_windows"],
                                      "window_end": alert["window_end"], "suppressed_repeats": 0}
                    new_alerts.append(alert)
            else:
                self.streak[rid] = []
                if self.open[rid] is not None:
                    ep = self.open[rid]
                    updates.append({"alert_id": ep["alert_id"], "status": "resolved",
                                    "resolved_at": win.end, "resolved_by_window": win.window_id})
                    self.open[rid] = None
        return win, new_alerts, updates

    def _make_alert(self, rule: dict, windows: list[Window]) -> dict:
        sig = rule["signal"]
        values = [getattr(w, sig) for w in windows]
        text = SIGNAL_TEXT[sig].format(lower=self.lower, floor=self.floor)
        if rule["consecutive"] == 1:
            reason = (f"La {text} fue {values[0]:.3f} {rule['op']} {rule['threshold']} en la ventana "
                      f"{windows[0].idx} ({windows[0].n_eligible} detecciones elegibles en {windows[0].n_valid} frames).")
        else:
            reason = (f"La {text} cumplió {rule['op']} {rule['threshold']} en {len(windows)} ventanas suficientes "
                      f"seguidas ({', '.join(f'{v:.3f}' for v in values)}): posible cambio sostenido respecto de la referencia.")
        return {
            "alert_id": f"alert-{self.run_id}-{rule['rule_id']}-w{windows[-1].idx:04d}",
            "rule_id": rule["rule_id"],
            "run_id": self.run_id,
            "timestamp": utc_now_iso(),
            "severity": rule["severity"],
            "status": "open",
            "reason": reason,
            "suggested_action": rule["suggested_action"],
            "config_ref": self.config_ref,
            "window_start": windows[0].start,
            "window_end": windows[-1].end,
            "n_windows": len(windows),
            "suppressed_repeats": 0,
            "metric": {
                "signal": sig, "op": rule["op"], "threshold": rule["threshold"],
                "consecutive": rule["consecutive"], "analysis_floor": self.floor, "lower": self.lower,
                "windows": [{"window_id": w.window_id, "idx": w.idx, "value": getattr(w, sig),
                             "n_valid": w.n_valid, "n_eligible": w.n_eligible} for w in windows],
            },
            "event_ids": [eid for w in windows for eid in w.event_ids],
            "image_refs": [ref for w in windows for ref in w.image_refs],
        }
