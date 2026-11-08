"""Portal de consulta: eventos, alertas, runs y configuraciones (FastAPI + Jinja2 sobre SQLite).

Cada vista HTML tiene su equivalente JSON en /api/... para consultas verificables.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

import cv2
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

TEMPLATES = Path(__file__).parent / "templates"
PAGE_SIZE = 50
ZONE_BGR = {"accepted": (60, 170, 40), "doubt": (0, 150, 255), "low": (40, 40, 220)}
STATUSES = ["ok", "no_detections", "inference_error", "invalid_input"]


def create_app(db_path: str | Path, root: str | Path = ".") -> FastAPI:
    root = Path(root).resolve()
    db_uri = Path(db_path).resolve().as_uri() + "?mode=ro"  # solo lectura; la ruta puede tener espacios
    app = FastAPI(title="Proyecto 6: Centro de monitoreo de computer vision")
    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters["fromjson"] = json.loads
    templates.env.filters["pretty"] = lambda v: json.dumps(v, indent=2, ensure_ascii=False)  # autoescapado por Jinja

    def rows(sql: str, params=()) -> list[dict]:
        con = sqlite3.connect(db_uri, uri=True)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, params).fetchall()]
        finally:
            con.close()

    def one(sql: str, params=()) -> dict | None:
        r = rows(sql, params)
        return r[0] if r else None

    # -- consultas --------------------------------------------------------------
    def query_events(run_id, status, reason, with_image, since, until, page):
        where, params = [], []
        if run_id:
            where.append("run_id = ?"); params.append(run_id)
        if status:
            where.append("status = ?"); params.append(status)
        if reason:
            where.append("capture_reason = ?"); params.append(reason)
        if with_image:
            where.append("image_ref IS NOT NULL")
        if since:
            where.append("captured_at >= ?"); params.append(since)
        if until:
            where.append("captured_at <= ?"); params.append(until)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        total = one(f"SELECT COUNT(*) AS n FROM events {w}", params)["n"]
        items = rows(
            f"""SELECT event_id, run_id, seq, captured_at, source_id, status, n_detections, n_accepted,
                       n_doubt, n_low, low_ratio, inference_ms, e2e_ms, image_ref, capture_reason, config_ref
                FROM events {w} ORDER BY captured_at DESC, seq DESC LIMIT ? OFFSET ?""",
            params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
        )
        return total, items

    def query_alerts(run_id, severity, rule_id, status):
        where, params = [], []
        for col, val in (("run_id", run_id), ("severity", severity), ("rule_id", rule_id), ("status", status)):
            if val:
                where.append(f"{col} = ?"); params.append(val)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        return rows(f"SELECT * FROM alerts {w} ORDER BY timestamp DESC", params)

    def event_or_404(event_id: str) -> dict:
        ev = one("SELECT * FROM events WHERE event_id = ?", (event_id,))
        if not ev:
            raise HTTPException(404, f"Evento no encontrado: {event_id}")
        ev["detections"] = json.loads(ev.pop("detections_json"))
        ev["thresholds"] = json.loads(ev.pop("thresholds_json"))
        ev["timings"] = json.loads(ev.pop("timings_json"))
        return ev

    def alert_or_404(alert_id: str) -> dict:
        al = one("SELECT * FROM alerts WHERE alert_id = ?", (alert_id,))
        if not al:
            raise HTTPException(404, f"Alerta no encontrada: {alert_id}")
        al["event_ids"] = json.loads(al.pop("event_ids_json"))
        al["metric"] = json.loads(al.pop("metric_json") or "{}")
        al["image_refs"] = json.loads(al.pop("image_refs_json") or "[]")
        return al

    def alerts_for_event(event_id: str) -> list[dict]:
        return rows(
            "SELECT alert_id, rule_id, severity, timestamp FROM alerts WHERE event_ids_json LIKE ?",
            (f'%"{event_id}"%',),
        )

    def run_options() -> list[dict]:
        return rows("SELECT run_id, label FROM runs ORDER BY started_at DESC")

    # -- vistas HTML ------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def home():
        return RedirectResponse("/eventos")

    @app.get("/eventos", response_class=HTMLResponse)
    def events_page(
        request: Request,
        run_id: str = "", status: str = "", reason: str = "", with_image: bool = False,
        since: str = "", until: str = "", page: int = Query(1, ge=1),
    ):
        total, items = query_events(run_id, status, reason, with_image, since, until, page)
        reasons = [r["capture_reason"] for r in rows(
            "SELECT DISTINCT capture_reason FROM events WHERE capture_reason IS NOT NULL ORDER BY 1")]
        qs = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "page"])
        return templates.TemplateResponse(request, "events.html", {
            "items": items, "total": total, "page": page, "pages": max(1, math.ceil(total / PAGE_SIZE)), "qs": qs,
            "f": {"run_id": run_id, "status": status, "reason": reason, "with_image": with_image,
                  "since": since, "until": until},
            "runs": run_options(), "statuses": STATUSES, "reasons": reasons,
        })

    @app.get("/eventos/{event_id}", response_class=HTMLResponse)
    def event_page(request: Request, event_id: str, low: bool = False):
        ev = event_or_404(event_id)
        return templates.TemplateResponse(request, "event.html", {
            "ev": ev, "alerts": alerts_for_event(event_id), "show_low": low,
            "run": one("SELECT run_id, label FROM runs WHERE run_id = ?", (ev["run_id"],)),
        })

    @app.get("/alertas", response_class=HTMLResponse)
    def alerts_page(request: Request, run_id: str = "", severity: str = "", rule_id: str = "", status: str = ""):
        items = query_alerts(run_id, severity, rule_id, status)
        for a in items:
            a["n_events"] = len(json.loads(a["event_ids_json"]))
        return templates.TemplateResponse(request, "alerts.html", {
            "items": items, "runs": run_options(),
            "rules": [r["rule_id"] for r in rows("SELECT DISTINCT rule_id FROM alerts ORDER BY 1")],
            "f": {"run_id": run_id, "severity": severity, "rule_id": rule_id, "status": status},
        })

    @app.get("/alertas/{alert_id}", response_class=HTMLResponse)
    def alert_page(request: Request, alert_id: str):
        al = alert_or_404(alert_id)
        ids = al["event_ids"]
        evs = rows(
            f"""SELECT event_id, seq, captured_at, status, n_detections, n_accepted, n_doubt, n_low,
                       low_ratio, image_ref FROM events WHERE event_id IN ({",".join("?" * len(ids))})
                ORDER BY captured_at""", ids) if ids else []
        return templates.TemplateResponse(request, "alert.html", {"al": al, "events": evs})

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request):
        items = rows(
            """SELECT r.run_id, r.label, r.mode, r.started_at, r.finished_at, r.config_ref, r.model_ref,
                      COUNT(e.event_id) AS n_events, SUM(e.image_ref IS NOT NULL) AS n_images
               FROM runs r LEFT JOIN events e ON e.run_id = r.run_id
               GROUP BY r.run_id ORDER BY r.started_at DESC""")
        return templates.TemplateResponse(request, "runs.html", {"items": items})

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str):
        r = one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        if not r:
            raise HTTPException(404, f"Run no encontrado: {run_id}")
        manifest = json.loads(r["manifest_json"])
        stats = one(
            """SELECT COUNT(*) AS n, SUM(status='ok') AS ok, SUM(status='no_detections') AS no_det,
                      SUM(status='inference_error') AS err, SUM(status='invalid_input') AS invalid,
                      SUM(image_ref IS NOT NULL) AS images, AVG(n_detections) AS det_mean,
                      AVG(inference_ms) AS inf_mean, MIN(captured_at) AS first, MAX(captured_at) AS last
               FROM events WHERE run_id = ?""", (run_id,))
        windows = rows(
            """SELECT window_id, idx, start, end, n_frames, n_valid, n_eligible, low_ratio, mean_score, state,
                      conditions_json, first_event_id FROM windows WHERE run_id = ? ORDER BY idx""", (run_id,))
        for w in windows:
            w["conditions"] = json.loads(w.pop("conditions_json"))
        alerts = rows("SELECT alert_id, rule_id, severity, status, timestamp FROM alerts WHERE run_id = ? "
                      "ORDER BY timestamp", (run_id,))
        return templates.TemplateResponse(request, "run.html", {"r": r, "m": manifest, "s": stats,
                                                                "windows": windows, "alerts": alerts})

    @app.get("/api/runs/{run_id}/ventanas")
    def api_windows(run_id: str):
        items = rows("SELECT * FROM windows WHERE run_id = ? ORDER BY idx", (run_id,))
        for w in items:
            w["conditions"] = json.loads(w.pop("conditions_json"))
            w["event_ids"] = json.loads(w.pop("event_ids_json"))
            w["image_refs"] = json.loads(w.pop("image_refs_json"))
        return items

    @app.get("/config/{config_ref}", response_class=HTMLResponse)
    def config_page(request: Request, config_ref: str):
        c = one("SELECT * FROM configs WHERE config_ref = ?", (config_ref,))
        if not c:
            raise HTTPException(404, f"Configuración no encontrada: {config_ref}")
        pretty = json.dumps(json.loads(c["config_json"]), indent=2, ensure_ascii=False)
        used_by = rows("SELECT run_id, label FROM runs WHERE config_ref = ? ORDER BY started_at", (config_ref,))
        return templates.TemplateResponse(request, "config.html", {"ref": config_ref, "pretty": pretty, "runs": used_by})

    # -- imagen con cajas -------------------------------------------------------
    @app.get("/imagen/{event_id}")
    def event_image(event_id: str, boxes: bool = True, low: bool = False):
        ev = event_or_404(event_id)
        if not ev["image_ref"]:
            raise HTTPException(404, "El evento no tiene imagen capturada")
        path = (root / ev["image_ref"]).resolve()
        if root not in path.parents or not path.is_file():
            raise HTTPException(404, f"Imagen no disponible: {ev['image_ref']}")
        img = cv2.imread(str(path))
        if img is None:
            raise HTTPException(500, "No se pudo leer la imagen")
        if boxes:
            for d in sorted(ev["detections"], key=lambda d: d["score"]):
                if d["zone"] == "low" and not low:
                    continue
                x1, y1, x2, y2 = (int(v) for v in d["bbox_xyxy"])
                color = ZONE_BGR[d["zone"]]
                cv2.rectangle(img, (x1, y1), (x2, y2), color, 1 if d["zone"] == "low" else 2)
                if d["zone"] != "low":
                    cv2.putText(img, f"{d['class_name']} {d['score']:.2f}", (x1, max(10, y1 - 3)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
        return Response(buf.tobytes(), media_type="image/jpeg")

    # -- API JSON ---------------------------------------------------------------
    @app.get("/api/eventos")
    def api_events(run_id: str = "", status: str = "", reason: str = "", with_image: bool = False,
                   since: str = "", until: str = "", page: int = Query(1, ge=1)):
        total, items = query_events(run_id, status, reason, with_image, since, until, page)
        return {"total": total, "page": page, "page_size": PAGE_SIZE, "items": items}

    @app.get("/api/eventos/{event_id}")
    def api_event(event_id: str):
        return event_or_404(event_id)

    @app.get("/api/alertas")
    def api_alerts(run_id: str = "", severity: str = "", rule_id: str = "", status: str = ""):
        items = query_alerts(run_id, severity, rule_id, status)
        for a in items:
            a["event_ids"] = json.loads(a.pop("event_ids_json"))
            a["metric"] = json.loads(a.pop("metric_json") or "{}")
            a["image_refs"] = json.loads(a.pop("image_refs_json") or "[]")
        return {"total": len(items), "items": items}

    @app.get("/api/alertas/{alert_id}")
    def api_alert(alert_id: str):
        return alert_or_404(alert_id)

    @app.get("/api/runs")
    def api_runs():
        return rows("SELECT run_id, label, mode, started_at, finished_at, config_ref, model_ref FROM runs "
                    "ORDER BY started_at DESC")

    return app
