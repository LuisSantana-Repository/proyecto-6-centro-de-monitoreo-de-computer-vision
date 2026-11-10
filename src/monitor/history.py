"""Extra F6 · gráficas históricas: una señal por ventana a través de todas las ejecuciones.

F1 (`/graficas`) muestra las ventanas de **una** ejecución; F5 (`/panel`) cuenta estados. Aquí se compara la
misma señal entre ejecuciones, con una consulta parametrizada:

- `senal`: low_ratio (G4), mean_score, inferencia_p95 (G1) o validos (proporción de frames con inferencia válida);
- `eje`: `ejecucion` (una columna por ejecución, en orden de inicio) u `hora` (hora del día **de la escena** en
  Shibuya, JST, para ver la señal frente a la hora sin importar cuándo se procesó);
- filtros: `modo` (live/replay), `desde`/`hasta` (fecha de inicio de la ejecución) y `etiqueta` (texto en la
  etiqueta o el run_id).

Hora de la escena: en vivo es exacta (`captured_at`). En un replay se reconstruye desde la grabación: el nombre
de la carpeta reproducida empieza con su hora de inicio (UTC) y cada muestra está a `sample_interval_s` de la
anterior, así que la ventana cae en inicio + (muestra media − 1) × intervalo (aproximada: supone que la
grabación no perdió frames). Una grabación sin hora en el nombre no tiene hora de escena: sus ventanas se
excluyen del eje `hora` y se cuentan.

Valor por ventana: low_ratio y mean_score solo en ventanas evaluadas (normal o fuera_de_rango), igual que F1 y
las reglas; inferencia_p95 y validos en toda ventana completa. Lo demás se excluye y se cuenta con su motivo,
nunca se grafica como 0.

`verify()` recalcula todo desde los registros primarios (manifest.json, windows.jsonl, events.jsonl,
alerts.jsonl) sin SQLite y lo compara con lo que muestra el portal.

    uv run python -m monitor.history [--senal low_ratio] [--eje hora] [--modo live] [--verificar]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from monitor.dashboard import MODES, PanelError, _parse, _ts, connect

SIGNALS = {
    "low_ratio": {"titulo": "Proporción de baja confianza (G4)", "unidad": "proporción", "fmt": "{:.3f}",
                  "regla": "low_ratio"},
    "mean_score": {"titulo": "Puntuación media de las detecciones elegibles", "unidad": "puntuación",
                   "fmt": "{:.3f}", "regla": "mean_score"},
    "inferencia_p95": {"titulo": "Tiempo de inferencia p95 por ventana (G1)", "unidad": "ms", "fmt": "{:.0f}",
                       "regla": None},
    "validos": {"titulo": "Proporción de frames con inferencia válida", "unidad": "proporción", "fmt": "{:.2f}",
                "regla": None},
}
AXES = ("ejecucion", "hora")
EVALUATED = ("normal", "fuera_de_rango")
VALID = ("ok", "no_detections")
JST = timezone(timedelta(hours=9))
REC_TS = re.compile(r"^(\d{8}T\d{6}Z)")
PARAMS = ("senal", "eje", "modo", "desde", "hasta", "etiqueta")


@dataclass
class HistoryFilter:
    senal: str = "low_ratio"
    eje: str = "ejecucion"
    modo: str = ""
    desde: str = ""
    hasta: str = ""
    etiqueta: str = ""

    @classmethod
    def from_params(cls, params: dict) -> "HistoryFilter":
        unknown = sorted(set(params) - set(PARAMS))
        if unknown:
            raise PanelError(422, f"Parámetros desconocidos: {', '.join(unknown)}.")
        f = cls(**{k: (v or "").strip() for k, v in params.items() if (v or "").strip()})
        if f.senal not in SIGNALS:
            raise PanelError(422, f"Señal inválida: {f.senal!r}. Válidas: {', '.join(SIGNALS)}.")
        if f.eje not in AXES:
            raise PanelError(422, f"Eje inválido: {f.eje!r}. Válidos: {', '.join(AXES)}.")
        if f.modo and f.modo not in MODES:
            raise PanelError(422, f"Modo inválido: {f.modo!r}. Válidos: {', '.join(MODES)}.")
        lo, hi = (cls._date(f.desde, "desde"), cls._date(f.hasta, "hasta", end=True))
        if lo and hi and lo > hi:
            raise PanelError(422, "El rango de fechas está invertido (desde > hasta).")
        return f

    @staticmethod
    def _date(v: str, name: str, end: bool = False) -> datetime | None:
        if not v:
            return None
        try:
            return _parse(v, end)
        except PanelError:
            raise PanelError(422, f"{name} no es una fecha ISO 8601 (p. ej. 2026-11-08 o 2026-11-08T16:00:00Z).")

    def match(self, run: dict) -> bool:
        if self.modo and run["mode"] != self.modo:
            return False
        start = _ts(run["started_at"])
        if self.desde and (start is None or start < self._date(self.desde, "desde")):
            return False
        if self.hasta and (start is None or start > self._date(self.hasta, "hasta", end=True)):
            return False
        if self.etiqueta and self.etiqueta.lower() not in f"{run['run_id']} {run['label'] or ''}".lower():
            return False
        return True

    def active(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v}


# -- datos normalizados (iguales desde SQLite o desde los JSONL) ---------------------
def _seq(event_id: str) -> int:
    return int(event_id.rsplit("-f", 1)[1])


def _run(run_id: str, label, mode, started_at, manifest: dict) -> dict:
    cfg = manifest.get("config") or {}
    rules = {r["signal"]: {"rule_id": r["rule_id"], "threshold": r["threshold"], "op": r["op"]}
             for r in cfg.get("alerts", [])}
    return {"run_id": run_id, "label": label, "mode": mode, "started_at": started_at,
            "replay_of": manifest.get("replay_of"), "interval": cfg.get("source", {}).get("sample_interval_s"),
            "size": cfg.get("windows", {}).get("size_frames"), "rules": rules}


def _win(w: dict) -> dict:
    return {k: w[k] for k in ("run_id", "idx", "state", "n_frames", "n_valid", "low_ratio", "mean_score",
                              "start", "end")} | {"first_seq": _seq(w["first_event_id"]),
                                                  "last_seq": _seq(w["last_event_id"])}


def from_index(con: sqlite3.Connection) -> dict:
    con.row_factory = sqlite3.Row
    runs = [_run(r["run_id"], r["label"], r["mode"], r["started_at"], json.loads(r["manifest_json"] or "{}"))
            for r in con.execute("SELECT run_id, label, mode, started_at, manifest_json FROM runs")]
    wins = [_win(dict(w)) for w in con.execute(
        "SELECT run_id, idx, state, n_frames, n_valid, low_ratio, mean_score, start, end, first_event_id, "
        "last_event_id FROM windows ORDER BY run_id, idx")]
    inf: dict[str, list] = {}
    for e in con.execute("SELECT run_id, seq, status, inference_ms FROM events ORDER BY run_id, seq"):
        if e["status"] in VALID and e["inference_ms"] is not None:
            inf.setdefault(e["run_id"], []).append((e["seq"], e["inference_ms"]))
    alerts: dict[str, list] = {}
    for a in con.execute("SELECT run_id, rule_id FROM alerts"):
        alerts.setdefault(a["run_id"], []).append(a["rule_id"])
    return {"runs": runs, "windows": wins, "inference": inf, "alerts": alerts}


def from_jsonl(runs_dir: Path, run_ids: list[str] | None = None) -> dict:
    dirs = [runs_dir / r for r in run_ids] if run_ids is not None else \
        sorted(p for p in runs_dir.iterdir() if (p / "manifest.json").exists())
    out = {"runs": [], "windows": [], "inference": {}, "alerts": {}, "missing": []}
    for d in dirs:
        if not (d / "manifest.json").exists() or not (d / "events.jsonl").exists():
            out["missing"].append(d.name)
            continue
        m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        out["runs"].append(_run(m["run_id"], m.get("label"), m.get("mode"), m.get("started_at"), m))
        if (d / "windows.jsonl").exists():
            out["windows"] += [_win(json.loads(l)) for l in open(d / "windows.jsonl", encoding="utf-8")]
        for l in open(d / "events.jsonl", encoding="utf-8"):
            e = json.loads(l)
            ms = (e.get("timings") or {}).get("inference_ms")
            if e["status"] in VALID and ms is not None:
                out["inference"].setdefault(m["run_id"], []).append((e["seq"], ms))
        if (d / "alerts.jsonl").exists():
            for l in open(d / "alerts.jsonl", encoding="utf-8"):
                a = json.loads(l)
                if a.get("type") == "alert":
                    out["alerts"].setdefault(m["run_id"], []).append(a["rule_id"])
    return out


# -- cálculo ------------------------------------------------------------------------
def p95(values: list[float]) -> float | None:
    """Percentil por rango más cercano, como G1 (monitor.metrics): siempre un valor observado."""
    if not values:
        return None
    a = sorted(values)
    return round(float(a[max(0, math.ceil(0.95 * len(a)) - 1)]), 3)


def scene_time(run: dict, w: dict) -> tuple[datetime | None, str]:
    """(instante de la escena a mitad de la ventana, 'exacta' | 'aproximada' | 'sin_hora')."""
    if run["mode"] == "live":
        a, b = _ts(w["start"]), _ts(w["end"])
        return (a + (b - a) / 2, "exacta") if a and b else (None, "sin_hora")
    m = REC_TS.match(run["replay_of"] or "")
    if not m or not run["interval"]:
        return None, "sin_hora"
    rec = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    mid = (w["first_seq"] + w["last_seq"]) / 2
    return rec + timedelta(seconds=(mid - 1) * run["interval"]), "aproximada"


def window_value(w: dict, senal: str, inf: list[tuple]) -> tuple[float | None, str | None]:
    """(valor, motivo de exclusión)."""
    if w["state"] == "incompleta":
        return None, "incompleta"
    if senal in ("low_ratio", "mean_score"):
        if w["state"] not in EVALUATED:
            return None, w["state"]
        return w[senal], None
    if senal == "validos":
        return round(w["n_valid"] / w["n_frames"], 4), None
    v = p95([ms for s, ms in inf if w["first_seq"] <= s <= w["last_seq"]])
    return (v, None) if v is not None else (None, "sin_inferencias")


def build(data: dict, f: HistoryFilter) -> dict:
    spec = SIGNALS[f.senal]
    runs = sorted((r for r in data["runs"] if f.match(r)), key=lambda r: (r["started_at"] or "", r["run_id"]))
    num = {r["run_id"]: i for i, r in enumerate(runs, start=1)}
    by_run: dict[str, list] = {}
    for w in data["windows"]:
        if w["run_id"] in num:
            by_run.setdefault(w["run_id"], []).append(w)
    points, excluded, table = [], {}, []
    thresholds = set()
    for r in runs:
        rule = r["rules"].get(spec["regla"]) if spec["regla"] else None
        if rule:
            thresholds.add((rule["threshold"], rule["op"]))
        vals, scenes, kinds = [], [], set()
        for w in sorted(by_run.get(r["run_id"], []), key=lambda x: x["idx"]):
            v, why = window_value(w, f.senal, data["inference"].get(r["run_id"], []))
            t, kind = scene_time(r, w)
            if v is None:
                excluded[why] = excluded.get(why, 0) + 1
                continue
            if f.eje == "hora" and t is None:
                excluded["sin_hora_de_escena"] = excluded.get("sin_hora_de_escena", 0) + 1
                continue
            jst = t.astimezone(JST) if t else None
            over = None if rule is None else (v > rule["threshold"] if rule["op"] == ">" else v < rule["threshold"])
            points.append({"n": num[r["run_id"]], "run_id": r["run_id"], "label": r["label"], "idx": w["idx"],
                           "value": v, "state": w["state"], "cruza_umbral": over,
                           "escena_jst": jst.strftime("%Y-%m-%d %H:%M") if jst else None,
                           "hora_escena": round(jst.hour + jst.minute / 60 + jst.second / 3600, 4) if jst else None,
                           "hora_tipo": kind})
            vals.append(v)
            if jst:
                scenes.append(jst)
                kinds.add(kind)
        rule_alerts = sum(1 for a in data["alerts"].get(r["run_id"], []) if rule and a == rule["rule_id"])
        table.append({"n": num[r["run_id"]], "run_id": r["run_id"], "label": r["label"], "mode": r["mode"],
                      "started_at": r["started_at"], "ventanas": len(by_run.get(r["run_id"], [])),
                      "con_valor": len(vals), "min": min(vals) if vals else None,
                      "media": round(sum(vals) / len(vals), 4) if vals else None, "max": max(vals) if vals else None,
                      "alertas_de_la_regla": rule_alerts if rule else None,
                      "escena_jst": (f"{min(scenes):%m-%d %H:%M}–{max(scenes):%H:%M}" if scenes else None),
                      "hora_tipo": "/".join(sorted(kinds)) or "sin_hora"})
    umbral = None
    if len(thresholds) == 1:
        th, op = next(iter(thresholds))
        umbral = {"valor": th, "op": op, "rule_id": next(r["rules"][spec["regla"]]["rule_id"] for r in runs
                                                         if spec["regla"] in r["rules"])}
    return {"filtros": f.active(), "senal": f.senal, "eje": f.eje, "titulo": spec["titulo"], "unidad": spec["unidad"],
            "fmt": spec["fmt"], "umbral": umbral, "umbrales_distintos": len(thresholds) > 1,
            "ejecuciones": table, "puntos": points, "n_puntos": len(points), "excluidas": excluded}


def verify(index_data: dict, runs_dir: Path, f: HistoryFilter) -> list[str]:
    """Diferencias entre lo que muestra el portal (SQLite) y lo recalculado desde los registros primarios."""
    a = build(index_data, f)
    ids = [r["run_id"] for r in a["ejecuciones"]]
    j = from_jsonl(runs_dir, ids)
    b = build(j, f)
    out = [f"{r}: sin registro primario en {(runs_dir / r).as_posix()}" for r in j["missing"]]
    key = lambda p: (p["run_id"], p["idx"])
    pa, pb = {key(p): p for p in a["puntos"]}, {key(p): p for p in b["puntos"]}
    for k in sorted(set(pa) | set(pb)):
        if k[0] in j["missing"]:
            continue
        x, y = pa.get(k), pb.get(k)
        if x is None or y is None:
            out.append(f"{k[0]} ventana {k[1]}: {'solo en el índice' if y is None else 'solo en los JSONL'}")
        elif (x["value"], x["hora_escena"], x["cruza_umbral"]) != (y["value"], y["hora_escena"], y["cruza_umbral"]):
            out.append(f"{k[0]} ventana {k[1]}: índice {x['value']} / JSONL {y['value']}")
    ra = {r["run_id"]: (r["con_valor"], r["alertas_de_la_regla"]) for r in a["ejecuciones"]}
    rb = {r["run_id"]: (r["con_valor"], r["alertas_de_la_regla"]) for r in b["ejecuciones"]}
    out += [f"{r}: índice {ra[r]} / JSONL {rb.get(r)}" for r in ra if r in rb and ra[r] != rb[r]]
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Gráficas históricas (F6): series por ventana de todas las ejecuciones")
    ap.add_argument("--db", default="datos/monitor.db")
    ap.add_argument("--runs-dir", default="datos/runs")
    for p in PARAMS:
        ap.add_argument(f"--{p}", default="")
    ap.add_argument("--verificar", action="store_true",
                    help="recalcular cada señal y eje desde los JSONL y compararlos con el índice")
    args = ap.parse_args()
    if not Path(args.db).exists():
        raise SystemExit(f"No existe {args.db}: ejecuta monitor.run o monitor.ingest")
    params = {p: getattr(args, p) for p in PARAMS if getattr(args, p)}
    try:
        con = connect(args.db)
        data = from_index(con)
        con.close()
        if args.verificar:
            bad = []
            for s in SIGNALS:
                for e in AXES:
                    f = HistoryFilter.from_params({**params, "senal": s, "eje": e})
                    d = verify(data, Path(args.runs_dir), f)
                    n = build(data, f)["n_puntos"]
                    print(f"  {s:15s} {e:10s} {n:4d} puntos · " + ("coincide" if not d else f"{len(d)} diferencias"))
                    bad += [f"{s}/{e}: {x}" for x in d]
            if bad:
                print("NO coincide con los JSONL:")
                print("\n".join(f"  {x}" for x in sorted(set(bad))[:40]))
                sys.exit(1)
            print("Las series del histórico coinciden con los registros primarios.")
            return
        d = build(data, HistoryFilter.from_params(params))
    except PanelError as e:
        raise SystemExit(f"Error {e.status}: {e.message}")
    print(f"{d['titulo']} · eje {d['eje']} · {d['n_puntos']} ventanas con valor · excluidas {d['excluidas']}")
    for r in d["ejecuciones"]:
        print(f"  {r['n']:2d} {r['run_id']:45s} {r['con_valor']:3d}/{r['ventanas']:<3d} "
              f"min {r['min']} media {r['media']} max {r['max']} · escena {r['escena_jst']} ({r['hora_tipo']})")


if __name__ == "__main__":
    main()
