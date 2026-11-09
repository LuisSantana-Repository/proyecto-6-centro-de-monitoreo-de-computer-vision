"""Extra F5 · panel de operación: estado agregado de todas las ejecuciones en una vista.

Reúne en una consulta parametrizada (modo y rango de fechas de inicio) lo que hoy está repartido entre
las vistas de eventos, alertas, ejecuciones, revisión (F2) y en vivo (F4):

- indicadores agregados de las ejecuciones filtradas;
- "requiere atención": una lista derivada con reglas explícitas (`ATTENTION_RULES`) que cruza el estado
  de cada alerta según su regla, su estado de revisión humana y el estado de cada ejecución;
- una fila por ejecución con su estado coordinado (`run_state`) y sus ventanas por estado;
- un resumen por regla de alerta con su avance de revisión.

Solo lee: SQLite en modo de solo lectura y los `estado.json` de la vista en vivo. `verify()` recalcula
los conteos desde los registros primarios (los JSONL de cada ejecución) sin pasar por SQLite, para
comprobar que el panel dice lo mismo que lo persistido.

    uv run python -m monitor.dashboard [--mode live] [--since 2026-11-08] [--until ...] [--verificar]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, time, timezone
from pathlib import Path

from monitor import live as livemod
from monitor.review import alert_state, capture_state

MODES = ("live", "replay")
WINDOW_STATES = ("normal", "fuera_de_rango", "otra_version", "insuficiente", "incompleta")
VALID = ("ok", "no_detections")
LEVELS = {"critico": 0, "aviso": 1, "seguimiento": 2}

# Reglas de la lista "requiere atención": (id, nivel, condición). El orden de la lista es nivel y luego fecha.
ATTENTION_RULES = {
    "A1": ("critico|aviso", "Alerta abierta sin reconocer (crítico si la alerta es critical)."),
    "A2": ("aviso", "La regla ya resolvió la alerta, pero su revisión no se ha cerrado con un veredicto."),
    "A3": ("seguimiento", "Alerta reconocida por una persona; la regla sigue abierta."),
    "E1": ("critico", "Ejecución en curso con la fuente caída (F4: fuente_caida)."),
    "E2": ("critico", "Ejecución sin terminar cuyo monitor dejó de reportar (F4: estancada)."),
    "E3": ("aviso", "Ejecución sin resumen final y sin estado en vivo: se interrumpió sin cerrar."),
    "E4": ("aviso", "Ejecución indexada en SQLite cuya carpeta de registros primarios (events.jsonl) no existe."),
    "C1": ("seguimiento", "Capturas por baja confianza de la ejecución todavía sin revisar (F2)."),
}


class PanelError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _parse(v: str, end: bool = False) -> datetime:
    """ISO 8601 con o sin hora. Una fecha sola cubre el día completo (until incluye todo ese día)."""
    s = v.strip().replace("Z", "+00:00")
    try:
        if len(s) == 10:
            d = datetime.combine(datetime.fromisoformat(s).date(), time.max if end else time.min, timezone.utc)
        else:
            d = datetime.fromisoformat(s)
    except ValueError:
        raise PanelError(422, f"{'until' if end else 'since'} no es una fecha ISO 8601 (p. ej. 2026-11-08 o "
                              "2026-11-08T16:00:00Z).")
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _ts(v: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None
    except ValueError:
        return None


@dataclass
class PanelFilter:
    mode: str = ""
    since: str = ""
    until: str = ""

    @classmethod
    def from_params(cls, params: dict) -> "PanelFilter":
        unknown = sorted(set(params) - {"mode", "since", "until"})
        if unknown:
            raise PanelError(422, f"Parámetros desconocidos: {', '.join(unknown)}.")
        f = cls(**{k: (v or "").strip() for k, v in params.items()})
        if f.mode and f.mode not in MODES:
            raise PanelError(422, f"Modo inválido: {f.mode!r}. Válidos: {', '.join(MODES)}.")
        lo = _parse(f.since) if f.since else None
        hi = _parse(f.until, end=True) if f.until else None
        if lo and hi and lo > hi:
            raise PanelError(422, "El rango de fechas está invertido (since > until).")
        return f

    def match(self, run: dict) -> bool:
        if self.mode and run["mode"] != self.mode:
            return False
        start = _ts(run["started_at"])
        if self.since and (start is None or start < _parse(self.since)):
            return False
        if self.until and (start is None or start > _parse(self.until, end=True)):
            return False
        return True

    def active(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v}


def _pct(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def _quantile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    pos = (len(xs) - 1) * q                       # interpolación lineal, igual que numpy por defecto
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo), 1)


def run_state(run: dict, live_state: dict | None, now: datetime) -> dict:
    """Estado coordinado de una ejecución: el registro persistido manda; F4 explica las que no terminaron."""
    if run["finished_at"]:
        return {"state": "terminada", "motivo": f"terminó {run['finished_at']}"}
    if live_state is not None:
        v = livemod.classify(live_state, now)
        if v["status"] == "terminada":            # la vista en vivo terminó pero el resumen no llegó a SQLite
            return {"state": "sin_cerrar", "motivo": "la vista en vivo terminó, pero falta el resumen final en la base"}
        return {"state": v["status"], "motivo": v["motivo"], **{k: v[k] for k in ("edad_s",) if k in v}}
    return {"state": "sin_cerrar", "motivo": "sin resumen final ni estado en vivo: la ejecución se interrumpió"}


def build(con: sqlite3.Connection, live_states: dict[str, dict], f: PanelFilter, now: datetime | None = None,
          runs_dir: Path | None = None) -> dict:
    """runs_dir: si se da, cada ejecución indica si existe su registro primario (regla E4)."""
    now = now or datetime.now(timezone.utc)
    q = lambda sql, p=(): [dict(r) for r in con.execute(sql, p).fetchall()]
    runs = [r for r in q("SELECT run_id, label, mode, started_at, finished_at, config_ref, model_ref FROM runs "
                         "ORDER BY started_at DESC") if f.match(r)]
    ids = [r["run_id"] for r in runs]
    marks = ",".join("?" * len(ids)) or "NULL"
    ev = {r["run_id"]: r for r in q(
        f"""SELECT run_id, COUNT(*) AS n, SUM(status IN ('ok','no_detections')) AS valid,
                   SUM(status='invalid_input') AS invalid, SUM(status='inference_error') AS errors,
                   SUM(image_ref IS NOT NULL) AS images, COUNT(DISTINCT model_ref) AS n_models
            FROM events WHERE run_id IN ({marks}) GROUP BY run_id""", ids)}
    inf = [r["inference_ms"] for r in q(
        f"SELECT inference_ms FROM events WHERE run_id IN ({marks}) AND status IN ('ok','no_detections') "
        "AND inference_ms IS NOT NULL", ids)]
    wins: dict[str, dict[str, int]] = {}
    for r in q(f"SELECT run_id, state, COUNT(*) AS n FROM windows WHERE run_id IN ({marks}) GROUP BY 1, 2", ids):
        wins.setdefault(r["run_id"], {})[r["state"]] = r["n"]
    alerts = q(f"SELECT alert_id, rule_id, run_id, timestamp, severity, status, resolved_at FROM alerts "
               f"WHERE run_id IN ({marks}) ORDER BY timestamp", ids)
    hist: dict[tuple, list] = {}
    for r in q(f"SELECT target_type, target_id, record_json FROM reviews WHERE run_id IN ({marks}) "
               "ORDER BY at, review_id", ids):
        hist.setdefault((r["target_type"], r["target_id"]), []).append(json.loads(r["record_json"]))
    for a in alerts:
        a["review"] = alert_state(hist.get(("alert", a["alert_id"]), []))
    caps = q(f"SELECT event_id, run_id, capture_reason FROM events WHERE run_id IN ({marks}) "
             "AND image_ref IS NOT NULL", ids)
    for c in caps:
        c["review"] = capture_state(hist.get(("event", c["event_id"]), []))

    attention: list[dict] = []

    def need(rule, level, run_id, at, text, href):
        attention.append({"regla": rule, "nivel": level, "run_id": run_id, "at": at, "texto": text, "href": href})

    by_run = []
    for r in runs:
        rid = r["run_id"]
        e = ev.get(rid, {})
        st = run_state(r, live_states.get(rid), now)
        ra = [a for a in alerts if a["run_id"] == rid]
        rc = [c for c in caps if c["run_id"] == rid]
        w = wins.get(rid, {})
        evaluated = w.get("normal", 0) + w.get("fuera_de_rango", 0)
        by_run.append({
            **r, "estado": st, "eventos": e.get("n", 0), "validos": e.get("valid", 0),
            "pct_validos": _pct(e.get("valid", 0), e.get("n", 0)), "sin_frame": e.get("invalid", 0),
            "errores_inferencia": e.get("errors", 0), "capturas": e.get("images", 0),
            "modelos": e.get("n_models", 0),
            "ventanas": {s: w.get(s, 0) for s in WINDOW_STATES}, "n_ventanas": sum(w.values()),
            "pct_fuera_de_rango": _pct(w.get("fuera_de_rango", 0), evaluated),
            "alertas": len(ra), "alertas_abiertas": sum(a["status"] == "open" for a in ra),
            "revision_alertas": {s: sum(a["review"]["state"] == s for a in ra) for s in ("pendiente", "reconocida", "revisada")},
            "revision_alertas_pendiente": sum(a["review"]["state"] != "revisada" for a in ra),
            "capturas_revisadas": sum(c["review"]["state"] == "revisada" for c in rc),
            "capturas_sin_revisar": sum(c["review"]["state"] == "pendiente" for c in rc),
            "registro_primario": (runs_dir / rid / "events.jsonl").is_file() if runs_dir else None,
        })
        if by_run[-1]["registro_primario"] is False:
            need("E4", "aviso", rid, r["started_at"],
                 f"{r['label']}: está en la base, pero falta {(Path(runs_dir.name) / rid / 'events.jsonl').as_posix()}; sus "
                 "cifras no se pueden comprobar (reconstruir el índice con monitor.ingest)", "/runs/" + rid)
        if st["state"] == "fuente_caida":
            need("E1", "critico", rid, r["started_at"], f"{r['label']}: fuente caída · {st['motivo']}", "/vivo?run_id=" + rid)
        elif st["state"] == "estancada":
            need("E2", "critico", rid, r["started_at"], f"{r['label']}: {st['motivo']}", "/vivo?run_id=" + rid)
        elif st["state"] == "sin_cerrar":
            need("E3", "aviso", rid, r["started_at"], f"{r['label']}: {st['motivo']}", "/runs/" + rid)
        low = [c for c in rc if c["capture_reason"] == "baja_confianza" and c["review"]["state"] == "pendiente"]
        if low:
            need("C1", "seguimiento", rid, r["started_at"],
                 f"{r['label']}: {len(low)} captura(s) por baja confianza sin revisar",
                 f"/revision?run_id={rid}&estado=pendiente&reason=baja_confianza")

    for a in alerts:
        rv = a["review"]["state"]
        if a["status"] == "open" and rv == "pendiente":
            need("A1", "critico" if a["severity"] == "critical" else "aviso", a["run_id"], a["timestamp"],
                 f"{a['rule_id']} ({a['severity']}) abierta desde {a['timestamp']} y sin reconocer", "/alertas/" + a["alert_id"])
        elif a["status"] == "open" and rv == "reconocida":
            need("A3", "seguimiento", a["run_id"], a["timestamp"],
                 f"{a['rule_id']} reconocida por {a['review']['acknowledged_by']}; la regla sigue abierta",
                 "/alertas/" + a["alert_id"])
        elif a["status"] == "resolved" and rv != "revisada":
            need("A2", "aviso", a["run_id"], a["resolved_at"] or a["timestamp"],
                 f"{a['rule_id']} resuelta {a['resolved_at']}; falta cerrar su revisión con un veredicto",
                 "/alertas/" + a["alert_id"] + "#revision")
    attention.sort(key=lambda x: x["at"] or "", reverse=True)
    attention.sort(key=lambda x: LEVELS[x["nivel"]])          # estable: nivel y, dentro, lo más reciente primero

    rules: dict[str, dict] = {}
    for a in alerts:
        g = rules.setdefault(a["rule_id"], {"rule_id": a["rule_id"], "severity": a["severity"], "n": 0, "abiertas": 0,
                                            "resueltas": 0, "pendiente": 0, "reconocida": 0, "revisada": 0,
                                            "veredictos": {}})
        g["n"] += 1
        g["abiertas" if a["status"] == "open" else "resueltas"] += 1
        g[a["review"]["state"]] += 1
        if a["review"]["verdict"]:
            g["veredictos"][a["review"]["verdict"]] = g["veredictos"].get(a["review"]["verdict"], 0) + 1

    n_ev = sum(x["eventos"] for x in by_run)
    n_valid = sum(x["validos"] for x in by_run)
    wtot = {s: sum(x["ventanas"][s] for x in by_run) for s in WINDOW_STATES}
    evaluated = wtot["normal"] + wtot["fuera_de_rango"]
    states = [x["estado"]["state"] for x in by_run]
    kpis = {
        "ejecuciones": {"n": len(by_run), "live": sum(x["mode"] == "live" for x in by_run),
                        "replay": sum(x["mode"] == "replay" for x in by_run),
                        "por_estado": {s: states.count(s) for s in ("en_curso", "fuente_caida", "estancada",
                                                                    "sin_cerrar", "terminada")}},
        "eventos": {"n": n_ev, "validos": n_valid, "pct_validos": _pct(n_valid, n_ev),
                    "sin_frame": sum(x["sin_frame"] for x in by_run),
                    "errores_inferencia": sum(x["errores_inferencia"] for x in by_run),
                    "capturas": sum(x["capturas"] for x in by_run)},
        "ventanas": {"n": sum(wtot.values()), "evaluadas": evaluated, "por_estado": wtot,
                     "pct_fuera_de_rango": _pct(wtot["fuera_de_rango"], evaluated)},
        "alertas": {"n": len(alerts), "abiertas": sum(a["status"] == "open" for a in alerts),
                    "resueltas": sum(a["status"] == "resolved" for a in alerts),
                    "revision": {s: sum(a["review"]["state"] == s for a in alerts)
                                 for s in ("pendiente", "reconocida", "revisada")}},
        "capturas": {"n": len(caps), "revisadas": sum(c["review"]["state"] == "revisada" for c in caps),
                     "sin_revisar_baja_confianza": sum(c["review"]["state"] == "pendiente"
                                                       and c["capture_reason"] == "baja_confianza" for c in caps)},
        "inferencia_ms": {"n": len(inf), "p50": _quantile(inf, 0.5), "p95": _quantile(inf, 0.95)},
    }
    return {"generado": now.isoformat(timespec="seconds").replace("+00:00", "Z"), "filtros": f.active(),
            "kpis": kpis, "atencion": attention, "atencion_por_nivel": {k: sum(x["nivel"] == k for x in attention)
                                                                         for k in LEVELS},
            "ejecuciones": by_run, "reglas": sorted(rules.values(), key=lambda g: (-g["abiertas"], g["rule_id"]))}


# -- recálculo independiente desde los registros primarios -----------------------------------------
def _jsonl(p: Path) -> list[dict]:
    if not p.is_file():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def recount(run_dir: Path) -> dict | None:
    """Los conteos del panel para una ejecución, leídos de sus JSONL (sin SQLite). None si no hay registro."""
    if not (run_dir / "events.jsonl").is_file():
        return None
    seen: dict[str, dict] = {}
    for e in _jsonl(run_dir / "events.jsonl"):
        if e.get("event_id"):
            seen.setdefault(e["event_id"], e)              # el índice descarta reenvíos con el mismo event_id
    evs = list(seen.values())
    wins = {s: 0 for s in WINDOW_STATES}
    for w in _jsonl(run_dir / "windows.jsonl"):
        wins[w["state"]] = wins.get(w["state"], 0) + 1
    status: dict[str, str] = {}
    for a in _jsonl(run_dir / "alerts.jsonl"):             # apertura y después actualizaciones con su estado
        if "status" in a:
            status[a["alert_id"]] = a["status"]
    revs = sorted(_jsonl(run_dir / "revisiones.jsonl"), key=lambda r: (r["at"], r["review_id"]))
    rv = {"pendiente": 0, "reconocida": 0, "revisada": 0}
    for aid in status:
        rv[alert_state([r for r in revs if r["target_type"] == "alert" and r["target_id"] == aid])["state"]] += 1
    return {"eventos": len(evs), "validos": sum(e["status"] in VALID for e in evs),
            "sin_frame": sum(e["status"] == "invalid_input" for e in evs),
            "capturas": sum(bool(e.get("image_ref")) for e in evs), "ventanas": wins,
            "alertas": len(status), "alertas_abiertas": sum(v == "open" for v in status.values()),
            "revision_alertas": rv,
            "capturas_revisadas": len({r["target_id"] for r in revs if r["target_type"] == "event"})}


CHECKED = ("eventos", "validos", "sin_frame", "capturas", "ventanas", "alertas", "alertas_abiertas",
           "revision_alertas", "capturas_revisadas")


def verify(panel: dict, runs_dir: Path) -> list[str]:
    """Diferencias entre el panel y el recálculo desde los JSONL, por ejecución; vacía si todo coincide.

    Los indicadores agregados son sumas de las filas por ejecución (se comprueba también)."""
    diffs = []
    for r in panel["ejecuciones"]:
        got = recount(runs_dir / r["run_id"])
        if got is None:
            diffs.append(f"{r['run_id']}: sin registro primario en {(runs_dir / r['run_id']).as_posix()}")
            continue
        diffs += [f"{r['run_id']} · {k}: panel {r[k]} ≠ JSONL {got[k]}" for k in CHECKED if r[k] != got[k]]
    k, rows = panel["kpis"], panel["ejecuciones"]
    sums = {"eventos": (k["eventos"]["n"], sum(r["eventos"] for r in rows)),
            "alertas": (k["alertas"]["n"], sum(r["alertas"] for r in rows)),
            "ventanas": (k["ventanas"]["n"], sum(r["n_ventanas"] for r in rows))}
    diffs += [f"total {n}: {a} ≠ suma por ejecución {b}" for n, (a, b) in sums.items() if a != b]
    return diffs


def connect(db_path) -> sqlite3.Connection:
    con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    con.row_factory = sqlite3.Row
    return con


def live_states(live_dir: Path) -> dict[str, dict]:
    return {s["run_id"]: s for s in livemod.list_live(live_dir) if s.get("run_id")}


def main() -> None:
    ap = argparse.ArgumentParser(description="Panel de operación (F5) en JSON, con verificación contra los JSONL.")
    ap.add_argument("--db", default="datos/monitor.db")
    ap.add_argument("--runs-dir", default="datos/runs")
    ap.add_argument("--mode", default="")
    ap.add_argument("--since", default="")
    ap.add_argument("--until", default="")
    ap.add_argument("--verificar", action="store_true", help="recalcula los conteos desde los JSONL y los compara")
    a = ap.parse_args()
    try:
        f = PanelFilter.from_params({"mode": a.mode, "since": a.since, "until": a.until})
    except PanelError as e:
        sys.exit(f"error {e.status}: {e.message}")
    if not Path(a.db).is_file():
        sys.exit(f"No existe la base {a.db}: créala con monitor.ingest o con una ejecución de monitor.run.")
    con = connect(a.db)
    try:
        panel = build(con, live_states(Path(a.runs_dir).parent / "vivo"), f, runs_dir=Path(a.runs_dir))
    finally:
        con.close()
    if not a.verificar:
        print(json.dumps(panel, indent=2, ensure_ascii=False))
        return
    diffs = verify(panel, Path(a.runs_dir))
    k = panel["kpis"]
    print(f"{k['ejecuciones']['n']} ejecuciones · {k['eventos']['n']} eventos · {k['ventanas']['n']} ventanas · "
          f"{k['alertas']['n']} alertas · {len(panel['atencion'])} puntos de atención")
    if diffs:
        print("NO coincide con los JSONL:\n  " + "\n  ".join(diffs))
        sys.exit(1)
    print("coincide con los registros primarios (events, windows, alerts y revisiones.jsonl)")


if __name__ == "__main__":
    main()
