"""Persistencia en SQLite: runs, configuraciones, eventos y alertas.

Los eventos se insertan por event_id (idempotente): un reenvío se cuenta como duplicado,
no como evento nuevo. Las filas que no cumplen el contrato se rechazan y se cuentan.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from monitor.events import InferenceEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    label TEXT,
    mode TEXT,
    started_at TEXT,
    finished_at TEXT,
    config_ref TEXT,
    model_ref TEXT,
    manifest_json TEXT
);
CREATE TABLE IF NOT EXISTS configs (
    config_ref TEXT PRIMARY KEY,
    config_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    frame_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    source_id TEXT NOT NULL,
    origin TEXT,
    task TEXT NOT NULL,
    model_ref TEXT NOT NULL,
    config_ref TEXT NOT NULL,
    status TEXT NOT NULL,
    n_detections INTEGER NOT NULL,
    n_accepted INTEGER NOT NULL,
    n_doubt INTEGER NOT NULL,
    n_low INTEGER NOT NULL,
    low_ratio REAL,
    inference_ms REAL,
    e2e_ms REAL,
    image_ref TEXT,
    capture_reason TEXT,
    error TEXT,
    thresholds_json TEXT NOT NULL,
    timings_json TEXT NOT NULL,
    detections_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(captured_at);
CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    run_id TEXT,
    timestamp TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    reason TEXT NOT NULL,
    suggested_action TEXT NOT NULL,
    config_ref TEXT NOT NULL,
    window_start TEXT,
    window_end TEXT,
    metric_json TEXT,
    event_ids_json TEXT NOT NULL,
    image_refs_json TEXT,
    n_windows INTEGER,
    suppressed_repeats INTEGER DEFAULT 0,
    resolved_at TEXT,
    resolved_by_window TEXT
);
CREATE INDEX IF NOT EXISTS ix_alerts_ts ON alerts(timestamp);
CREATE TABLE IF NOT EXISTS windows (
    window_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    config_ref TEXT NOT NULL,
    first_event_id TEXT NOT NULL,
    last_event_id TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    n_frames INTEGER NOT NULL,
    n_valid INTEGER NOT NULL,
    n_eligible INTEGER NOT NULL,
    n_low INTEGER NOT NULL,
    low_ratio REAL,
    mean_score REAL,
    state TEXT NOT NULL,
    conditions_json TEXT NOT NULL,
    event_ids_json TEXT NOT NULL,
    image_refs_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_windows_run ON windows(run_id, idx);
CREATE TABLE IF NOT EXISTS source_events (
    run_id TEXT NOT NULL,
    at TEXT NOT NULL,
    type TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    PRIMARY KEY (run_id, at, type)
);
CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    at TEXT NOT NULL,
    run_id TEXT NOT NULL,
    target_type TEXT NOT NULL,          -- alert | event
    target_id TEXT NOT NULL,
    action TEXT NOT NULL,
    operator TEXT NOT NULL,
    note TEXT,
    verdict TEXT,
    label TEXT,
    state_before TEXT,
    state_after TEXT,
    record_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reviews_target ON reviews(target_type, target_id, at);
CREATE TABLE IF NOT EXISTS ingest_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    run_id TEXT,
    inserted INTEGER NOT NULL,
    duplicates INTEGER NOT NULL,
    rejected INTEGER NOT NULL,
    rejected_detail TEXT
);
"""


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.con = sqlite3.connect(self.path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        # migración: bases creadas antes del escenario S1 no tienen esta columna
        if "resolution_json" not in {r["name"] for r in self.con.execute("PRAGMA table_info(alerts)")}:
            self.con.execute("ALTER TABLE alerts ADD COLUMN resolution_json TEXT")
            self.con.commit()

    def close(self) -> None:
        self.con.close()

    # -- escritura ------------------------------------------------------------
    def upsert_config(self, config_ref: str, cfg: dict) -> None:
        self.con.execute(
            "INSERT OR IGNORE INTO configs(config_ref, config_json) VALUES (?, ?)",
            (config_ref, json.dumps(cfg, ensure_ascii=False, sort_keys=True)),
        )
        self.con.commit()

    def upsert_run(self, manifest: dict) -> None:
        self.con.execute(
            """INSERT INTO runs(run_id,label,mode,started_at,finished_at,config_ref,model_ref,manifest_json)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET finished_at=excluded.finished_at,
                   manifest_json=excluded.manifest_json""",
            (
                manifest["run_id"], manifest.get("label"), manifest.get("mode"),
                manifest.get("started_at"), manifest.get("finished_at"),
                manifest["config_ref"], manifest["model_ref"],
                json.dumps(manifest, ensure_ascii=False),
            ),
        )
        self.con.commit()

    def insert_event(self, ev: InferenceEvent, commit: bool = True) -> bool:
        """Devuelve False si el event_id ya existía (duplicado)."""
        zc = ev.zone_counts or {}
        cur = self.con.execute(
            """INSERT OR IGNORE INTO events VALUES
               (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ev.event_id, ev.run_id, ev.frame_id, ev.seq, ev.timestamp, ev.captured_at,
                ev.source_id, ev.origin, ev.task, ev.model_ref, ev.config_ref, ev.status,
                ev.n_detections, zc.get("accepted", 0), zc.get("doubt", 0), zc.get("low", 0),
                ev.low_ratio, ev.timings.get("inference_ms"), ev.e2e_ms, ev.image_ref,
                ev.capture_reason, ev.error,
                json.dumps(ev.thresholds), json.dumps(ev.timings),
                json.dumps([d.model_dump() for d in ev.detections]),
            ),
        )
        if commit:
            self.con.commit()
        return cur.rowcount == 1

    def insert_alert(self, alert: dict) -> bool:
        cur = self.con.execute(
            """INSERT OR IGNORE INTO alerts(alert_id,rule_id,run_id,timestamp,severity,status,reason,
                   suggested_action,config_ref,window_start,window_end,metric_json,event_ids_json,image_refs_json,
                   n_windows,suppressed_repeats)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                alert["alert_id"], alert["rule_id"], alert.get("run_id"), alert["timestamp"],
                alert["severity"], alert.get("status", "open"), alert["reason"],
                alert["suggested_action"], alert["config_ref"], alert.get("window_start"),
                alert.get("window_end"), json.dumps(alert.get("metric", {}), ensure_ascii=False),
                json.dumps(alert["event_ids"]), json.dumps(alert.get("image_refs", [])),
                alert.get("n_windows"), alert.get("suppressed_repeats", 0),
            ),
        )
        self.con.commit()
        return cur.rowcount == 1

    UPDATABLE = ("status", "resolved_at", "resolved_by_window", "n_windows", "window_end", "suppressed_repeats",
                 "resolution_json")
    # detalle de la resolución: regla de la fuente (S1) y regla de versión (S2)
    RESOLUTION = ("n_invalid", "downtime_s", "n_mismatch", "duration_s", "reported_model_refs", "first_ok_event_id",
                  "n_errors", "n_reloads", "reload_effective", "recovered_by")   # ... y de inferencia (S3)

    def update_alert(self, upd: dict) -> None:
        res = {k: upd[k] for k in self.RESOLUTION if k in upd}
        if res:
            upd = {**upd, "resolution_json": json.dumps(res, ensure_ascii=False)}
        cols = [c for c in self.UPDATABLE if c in upd]
        if cols:
            self.con.execute(
                f"UPDATE alerts SET {', '.join(f'{c} = ?' for c in cols)} WHERE alert_id = ?",
                [upd[c] for c in cols] + [upd["alert_id"]],
            )
            self.con.commit()

    def insert_window(self, w: dict) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO windows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                w["window_id"], w["run_id"], w["idx"], w["config_ref"], w["first_event_id"], w["last_event_id"],
                w["start"], w["end"], w["n_frames"], w["n_valid"], w["n_eligible"], w["n_low"],
                w["low_ratio"], w["mean_score"], w["state"], json.dumps(w["conditions"]),
                json.dumps(w["event_ids"]), json.dumps(w["image_refs"]),
            ),
        )
        self.con.commit()

    def insert_source_event(self, rec: dict) -> None:
        detail = {k: v for k, v in rec.items() if k not in ("run_id", "at", "type")}
        self.con.execute("INSERT OR IGNORE INTO source_events VALUES (?,?,?,?)",
                         (rec["run_id"], rec["at"], rec["type"], json.dumps(detail, ensure_ascii=False)))
        self.con.commit()

    def insert_review(self, rec: dict) -> bool:
        """Revisión humana (F2). Idempotente por review_id / request_id."""
        cur = self.con.execute(
            "INSERT OR IGNORE INTO reviews VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["review_id"], rec["request_id"], rec["at"], rec["run_id"], rec["target_type"], rec["target_id"],
             rec["action"], rec["operator"], rec.get("note"), rec.get("verdict"), rec.get("label"),
             rec.get("state_before"), rec.get("state_after"), json.dumps(rec, ensure_ascii=False)))
        self.con.commit()
        return cur.rowcount == 1

    def review_by_request(self, request_id: str) -> dict | None:
        r = self.con.execute("SELECT record_json FROM reviews WHERE request_id = ?", (request_id,)).fetchone()
        return json.loads(r["record_json"]) if r else None

    def reviews_for(self, target_type: str, target_id: str) -> list[dict]:
        return [json.loads(r["record_json"]) for r in self.con.execute(
            "SELECT record_json FROM reviews WHERE target_type = ? AND target_id = ? ORDER BY at, review_id",
            (target_type, target_id))]

    def alert(self, alert_id: str) -> dict | None:
        r = self.con.execute("SELECT alert_id, run_id, rule_id, status, config_ref FROM alerts WHERE alert_id = ?",
                             (alert_id,)).fetchone()
        return dict(r) if r else None

    def event(self, event_id: str) -> dict | None:
        r = self.con.execute("SELECT event_id, run_id, image_ref, config_ref FROM events WHERE event_id = ?",
                             (event_id,)).fetchone()
        return dict(r) if r else None

    def apply_alert_log(self, record: dict) -> None:
        """Aplica una línea de alerts.jsonl: {"type": "alert"|"update", ...}."""
        body = {k: v for k, v in record.items() if k != "type"}
        if record["type"] == "alert":
            self.insert_alert(body)
        else:
            self.update_alert(body)

    # -- ingesta de un run desde disco ----------------------------------------
    def ingest_run_dir(self, run_dir: str | Path) -> dict:
        run_dir = Path(run_dir)
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.upsert_config(manifest["config_ref"], manifest["config"])
        self.upsert_run(manifest)
        inserted = duplicates = rejected = 0
        detail = []
        with open(run_dir / "events.jsonl", encoding="utf-8") as f:
            for n, line in enumerate(f, start=1):
                try:
                    ev = InferenceEvent.model_validate_json(line)
                except ValidationError as e:
                    rejected += 1
                    detail.append({"line": n, "error": str(e).splitlines()[0]})
                    continue
                if self.insert_event(ev, commit=False):
                    inserted += 1
                else:
                    duplicates += 1
        self.con.commit()
        if (run_dir / "windows.jsonl").exists():
            for line in open(run_dir / "windows.jsonl", encoding="utf-8"):
                self.insert_window(json.loads(line))
        if (run_dir / "alerts.jsonl").exists():
            for line in open(run_dir / "alerts.jsonl", encoding="utf-8"):
                self.apply_alert_log(json.loads(line))
        if (run_dir / "fuente.jsonl").exists():
            for line in open(run_dir / "fuente.jsonl", encoding="utf-8"):
                self.insert_source_event(json.loads(line))
        if (run_dir / "revisiones.jsonl").exists():          # revisiones humanas (F2)
            for line in open(run_dir / "revisiones.jsonl", encoding="utf-8"):
                self.insert_review(json.loads(line))
        self.con.execute(
            "INSERT INTO ingest_log(at,run_id,inserted,duplicates,rejected,rejected_detail) "
            "VALUES (datetime('now'),?,?,?,?,?)",
            (manifest["run_id"], inserted, duplicates, rejected, json.dumps(detail[:50])),
        )
        self.con.commit()
        return {"run_id": manifest["run_id"], "inserted": inserted, "duplicates": duplicates, "rejected": rejected}
