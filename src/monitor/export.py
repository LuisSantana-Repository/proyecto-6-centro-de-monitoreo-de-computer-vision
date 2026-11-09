"""Funcionalidad adicional F3 · exportación de eventos (CSV / JSONL) con filtros, procedencia y verificación.

Dos granularidades:
  evento     -> una fila por evento: estado, conteos por zona, tiempos, imagen, motivo de captura, error, más
                las alertas que lo citan y la etiqueta vigente de la revisión de su captura (F2).
  deteccion  -> una fila por detección: evento, clase, puntuación, zona y caja.

El archivo es determinista: orden fijo (run_id, seq, puntuación) y sin marcas de tiempo dentro, así que
los mismos filtros sobre los mismos datos dan los mismos bytes y el mismo SHA-256. La procedencia
(filtros, filas, SHA-256, momento) viaja en cabeceras HTTP y en el registro `datos/exportaciones.jsonl`,
no dentro del archivo, para que el CSV/JSONL siga siendo datos puros.

Uso (mismos bytes que el portal):
  uv run python -m monitor.export --run-id <id> [--nivel evento|deteccion] [--formato csv|jsonl] --out f.csv
  uv run python -m monitor.export --run-id <id> ... --verificar f.csv      # recalcula y compara el SHA-256
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sqlite3
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path

from monitor.review import capture_state
from monitor.source import utc_now_iso

FORMATS = ("csv", "jsonl")
LEVELS = ("evento", "deteccion")
STATUSES = ("ok", "no_detections", "inference_error", "invalid_input")
ZONES = ("accepted", "doubt", "low")
EVENT_COLUMNS = ["event_id", "run_id", "seq", "captured_at", "timestamp", "source_id", "model_ref", "config_ref",
                 "status", "n_detections", "n_accepted", "n_doubt", "n_low", "low_ratio", "inference_ms", "e2e_ms",
                 "image_ref", "capture_reason", "error", "alert_ids", "revision_captura"]
DETECTION_COLUMNS = ["event_id", "run_id", "seq", "captured_at", "model_ref", "config_ref", "class_id", "class_name",
                     "score", "zone", "x1", "y1", "x2", "y2"]
PREVIEW_ROWS = 10


class ExportError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class ExportSpec:
    run_id: str = ""
    status: str = ""
    reason: str = ""
    with_image: bool = False
    since: str = ""
    until: str = ""
    model_ref: str = ""
    alert_id: str = ""
    class_name: str = ""
    zone: str = ""
    min_score: float | None = None
    min_low_ratio: float | None = None
    max_low_ratio: float | None = None
    nivel: str = "evento"
    formato: str = "csv"

    @classmethod
    def from_params(cls, params: dict) -> "ExportSpec":
        """Valida parámetros de texto (query string o CLI). Errores de forma -> 422."""
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(params) - known)
        if unknown:
            raise ExportError(422, f"Parámetros desconocidos: {', '.join(unknown)}.")
        p = {k: (v.strip() if isinstance(v, str) else v) for k, v in params.items()}
        spec = cls(**{k: v for k, v in p.items() if k not in ("with_image", "min_score", "min_low_ratio", "max_low_ratio")})
        spec.with_image = str(p.get("with_image", "")).lower() in ("1", "true", "on", "si", "sí")
        for k in ("min_score", "min_low_ratio", "max_low_ratio"):
            v = p.get(k)
            if v not in (None, ""):
                try:
                    x = float(v)
                except (TypeError, ValueError):
                    raise ExportError(422, f"{k} debe ser un número entre 0 y 1.")
                if not 0 <= x <= 1:
                    raise ExportError(422, f"{k} debe estar entre 0 y 1.")
                setattr(spec, k, x)
        spec.validate()
        return spec

    def validate(self) -> None:
        if self.formato not in FORMATS:
            raise ExportError(422, f"Formato inválido: {self.formato!r}. Válidos: {', '.join(FORMATS)}.")
        if self.nivel not in LEVELS:
            raise ExportError(422, f"Nivel inválido: {self.nivel!r}. Válidos: {', '.join(LEVELS)}.")
        if self.status and self.status not in STATUSES:
            raise ExportError(422, f"Estado inválido: {self.status!r}.")
        if self.zone and self.zone not in ZONES:
            raise ExportError(422, f"Zona inválida: {self.zone!r}. Válidas: {', '.join(ZONES)}.")
        for k in ("since", "until"):
            v = getattr(self, k)
            if v:
                try:
                    datetime.fromisoformat(v.replace("Z", "+00:00"))
                except ValueError:
                    raise ExportError(422, f"{k} no es una fecha ISO 8601 (p. ej. 2026-11-08T16:33:00Z).")
        if self.since and self.until and self.since > self.until:
            raise ExportError(422, "El rango de fechas está invertido (since > until).")
        if None not in (self.min_low_ratio, self.max_low_ratio) and self.min_low_ratio > self.max_low_ratio:
            raise ExportError(422, "min_low_ratio no puede ser mayor que max_low_ratio.")

    def filters(self) -> dict:
        """Solo los filtros activos (para la procedencia y el nombre del archivo)."""
        d = asdict(self)
        return {k: v for k, v in d.items() if k not in ("nivel", "formato") and v not in ("", None, False)}

    def filename(self) -> str:
        tag = self.run_id or "todas"
        h = hashlib.sha1(json.dumps(self.filters(), sort_keys=True).encode()).hexdigest()[:8]
        return f"eventos-{tag}-{self.nivel}-{h}.{self.formato}"


def _connect(db_path) -> sqlite3.Connection:
    con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _rows(con: sqlite3.Connection, spec: ExportSpec) -> list[dict]:
    if spec.run_id and not con.execute("SELECT 1 FROM runs WHERE run_id = ?", (spec.run_id,)).fetchone():
        raise ExportError(404, f"Ejecución no encontrada: {spec.run_id}")
    where, params = [], []
    for col, val in (("run_id", spec.run_id), ("status", spec.status), ("capture_reason", spec.reason),
                     ("model_ref", spec.model_ref)):
        if val:
            where.append(f"{col} = ?"); params.append(val)
    if spec.with_image:
        where.append("image_ref IS NOT NULL")
    if spec.since:
        where.append("captured_at >= ?"); params.append(spec.since)
    if spec.until:
        where.append("captured_at <= ?"); params.append(spec.until)
    if spec.min_low_ratio is not None:
        where.append("low_ratio >= ?"); params.append(spec.min_low_ratio)
    if spec.max_low_ratio is not None:
        where.append("low_ratio <= ?"); params.append(spec.max_low_ratio)
    alerts = [dict(r) for r in con.execute("SELECT alert_id, event_ids_json FROM alerts")]
    if spec.alert_id:
        a = next((a for a in alerts if a["alert_id"] == spec.alert_id), None)
        if a is None:
            raise ExportError(404, f"Alerta no encontrada: {spec.alert_id}")
        ids = json.loads(a["event_ids_json"])
        where.append(f"event_id IN ({','.join('?' * len(ids))})" if ids else "0")
        params += ids
    w = ("WHERE " + " AND ".join(where)) if where else ""
    events = [dict(r) for r in con.execute(f"SELECT * FROM events {w} ORDER BY run_id, seq", params)]
    by_event: dict[str, list[str]] = {}
    for a in alerts:
        for eid in json.loads(a["event_ids_json"]):
            by_event.setdefault(eid, []).append(a["alert_id"])
    reviews: dict[str, list[dict]] = {}
    has_reviews = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviews'").fetchone()
    if has_reviews:
        for r in con.execute("SELECT target_id, record_json FROM reviews WHERE target_type = 'event' ORDER BY at, review_id"):
            reviews.setdefault(r["target_id"], []).append(json.loads(r["record_json"]))

    def det_ok(d: dict) -> bool:
        return ((not spec.class_name or d["class_name"] == spec.class_name) and (not spec.zone or d["zone"] == spec.zone)
                and (spec.min_score is None or d["score"] >= spec.min_score))

    det_filter = bool(spec.class_name or spec.zone or spec.min_score is not None)
    out = []
    for e in events:
        dets = json.loads(e["detections_json"])
        if spec.nivel == "evento":
            if det_filter and not any(det_ok(d) for d in dets):
                continue           # en nivel evento, los filtros de detección eligen eventos que tengan alguna
            row = {c: e.get(c) for c in EVENT_COLUMNS if c in e}
            row["alert_ids"] = ";".join(sorted(by_event.get(e["event_id"], [])))
            row["revision_captura"] = capture_state(reviews.get(e["event_id"], []))["label"] or ""
            out.append({c: row.get(c) for c in EVENT_COLUMNS})
        else:
            for d in sorted(dets, key=lambda d: (-d["score"], d["class_id"], d["bbox_xyxy"])):
                if not det_ok(d):
                    continue
                x1, y1, x2, y2 = d["bbox_xyxy"]
                out.append({"event_id": e["event_id"], "run_id": e["run_id"], "seq": e["seq"],
                            "captured_at": e["captured_at"], "model_ref": e["model_ref"], "config_ref": e["config_ref"],
                            "class_id": d["class_id"], "class_name": d["class_name"], "score": d["score"],
                            "zone": d["zone"], "x1": x1, "y1": y1, "x2": x2, "y2": y2})
    return out


def render(rows: list[dict], spec: ExportSpec) -> bytes:
    cols = EVENT_COLUMNS if spec.nivel == "evento" else DETECTION_COLUMNS
    if spec.formato == "jsonl":
        return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
    buf = io.StringIO()
    wr = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    wr.writeheader()
    for r in rows:
        wr.writerow({k: ("" if v is None else v) for k, v in r.items()})
    return buf.getvalue().encode("utf-8")


def build(db_path, spec: ExportSpec) -> tuple[bytes, dict, list[dict]]:
    """Devuelve (contenido, procedencia, filas). La procedencia no forma parte del contenido."""
    con = _connect(db_path)
    try:
        rows = _rows(con, spec)
    finally:
        con.close()
    data = render(rows, spec)
    meta = {"filename": spec.filename(), "nivel": spec.nivel, "formato": spec.formato, "filtros": spec.filters(),
            "filas": len(rows), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "columnas": EVENT_COLUMNS if spec.nivel == "evento" else DETECTION_COLUMNS}
    return data, meta, rows


def log_export(path: Path, meta: dict, origin: str) -> dict:
    rec = {"at": utc_now_iso(), "origen": origin, **{k: v for k, v in meta.items() if k != "columnas"}}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def read_log(path: Path, limit: int = 20) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()][-limit:][::-1]


def main() -> None:
    ap = argparse.ArgumentParser(description="Exporta eventos o detecciones (mismos bytes que /exportar del portal)")
    ap.add_argument("--db", default="datos/monitor.db")
    for f in fields(ExportSpec):
        if f.name == "with_image":
            ap.add_argument("--with-image", action="store_true")
        else:
            ap.add_argument("--" + f.name.replace("_", "-"), default=None)
    ap.add_argument("--out", default=None, help="archivo de salida (por defecto, el nombre sugerido)")
    ap.add_argument("--verificar", default=None, help="archivo descargado a comparar con lo que se recalcula")
    args = ap.parse_args()
    params = {f.name: getattr(args, f.name) for f in fields(ExportSpec) if getattr(args, f.name) not in (None, False)}
    try:
        spec = ExportSpec.from_params(params)
        data, meta, _ = build(args.db, spec)
    except ExportError as e:
        raise SystemExit(f"Error ({e.status}): {e.message}")
    if args.verificar:
        got = hashlib.sha256(Path(args.verificar).read_bytes()).hexdigest()
        ok = got == meta["sha256"]
        print(f"{args.verificar}: sha256={got} | recalculado={meta['sha256']} ({meta['filas']} filas) -> "
              f"{'coincide' if ok else 'NO coincide'}")
        raise SystemExit(0 if ok else 1)
    out = Path(args.out or meta["filename"])
    out.write_bytes(data)
    print(f"Escrito {out}: {meta['filas']} filas, {meta['bytes']} bytes, sha256={meta['sha256']}")


if __name__ == "__main__":
    main()
