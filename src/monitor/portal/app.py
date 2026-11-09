"""Portal de consulta: eventos, alertas, runs y configuraciones (FastAPI + Jinja2 sobre SQLite).

Cada vista HTML tiene su equivalente JSON en /api/... para consultas verificables.
Las consultas usan una conexión de solo lectura. La única escritura es la revisión humana (F2,
monitor.review): se agrega a datos/runs/<run_id>/revisiones.jsonl y se indexa en SQLite; los registros
del monitor (eventos, ventanas, alertas) nunca se modifican desde el portal.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlencode

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from monitor import drift
from monitor import export as exporter
from monitor import live as livemod
from monitor.draw import draw_detections
from monitor.portal.charts import window_chart
from monitor.review import (CAPTURE_LABELS, VERDICTS, ReviewError, ReviewLog, alert_state, capture_state,
                            check_alert_action, check_capture_review)
from monitor.source import ANSI
from monitor.store import Store

TEMPLATES = Path(__file__).parent / "templates"
PAGE_SIZE = 50
STATUSES = ["ok", "no_detections", "inference_error", "invalid_input"]


def create_app(db_path: str | Path, root: str | Path = ".", runs_dir: str = "datos/runs") -> FastAPI:
    root = Path(root).resolve()
    Store(db_path).close()                                  # crea/migra el esquema (p. ej. la tabla reviews)
    reviews = ReviewLog(root / runs_dir, lambda: Store(db_path))
    exports_log = root / Path(runs_dir).parent / "exportaciones.jsonl"   # registro de descargas (F3)
    live_dir = root / Path(runs_dir).parent / "vivo"                       # estado publicado por monitor.run (F4)
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
        al["resolution"] = json.loads(al.pop("resolution_json", None) or "{}")
        return al

    def alerts_for_event(event_id: str) -> list[dict]:
        return rows(
            "SELECT alert_id, rule_id, severity, timestamp FROM alerts WHERE event_ids_json LIKE ?",
            (f'%"{event_id}"%',),
        )

    def run_options() -> list[dict]:
        return rows("SELECT run_id, label FROM runs ORDER BY started_at DESC")

    # -- revisión humana (F2) ---------------------------------------------------
    def history(target_type: str, target_id: str) -> list[dict]:
        return [json.loads(r["record_json"]) for r in rows(
            "SELECT record_json FROM reviews WHERE target_type = ? AND target_id = ? ORDER BY at, review_id",
            (target_type, target_id))]

    def histories(target_type: str, run_id: str = "") -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        sql = "SELECT target_id, record_json FROM reviews WHERE target_type = ?"
        params: list = [target_type]
        if run_id:
            sql += " AND run_id = ?"; params.append(run_id)
        for r in rows(sql + " ORDER BY at, review_id", params):
            out.setdefault(r["target_id"], []).append(json.loads(r["record_json"]))
        return out

    async def form_data(request: Request) -> dict:
        if request.headers.get("content-type", "").startswith("application/json"):
            body = await request.json()
            return body if isinstance(body, dict) else {}
        try:   # un navegador envía UTF-8; texto mal codificado se rechaza en vez de guardarse con "�"
            raw = (await request.body()).decode("utf-8")
            return {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True, errors="strict").items()}
        except UnicodeDecodeError:
            raise ReviewError(422, "El formulario no viene en UTF-8: el texto se guardaría alterado.")

    def submit_alert(alert_id: str, form: dict):
        def load(store):
            al = store.alert(alert_id)
            if al is None:
                raise ReviewError(404, f"Alerta no encontrada: {alert_id}")
            return al, store.reviews_for("alert", alert_id)
        return reviews.submit(form.get("request_id"), load, lambda al, h: check_alert_action(al, h, form))

    def submit_capture(event_id: str, form: dict):
        def load(store):
            ev = store.event(event_id)
            if ev is None:
                raise ReviewError(404, f"Evento no encontrado: {event_id}")
            return ev, store.reviews_for("event", event_id)
        return reviews.submit(form.get("request_id"), load, lambda ev, h: check_capture_review(ev, h, form))

    def review_ctx(request: Request, target_type: str, target_id: str, msg: str = "", error: str = "",
                   form: dict | None = None) -> dict:
        h = history(target_type, target_id)
        return {"history": h, "rstate": alert_state(h) if target_type == "alert" else capture_state(h),
                "verdicts": VERDICTS, "labels": CAPTURE_LABELS, "msg": msg, "error": error, "form": form or {},
                "request_id": uuid.uuid4().hex, "operator": (form or {}).get("operator") or request.cookies.get("operador", "")}

    def done(url: str, rec: dict, new: bool) -> RedirectResponse:
        text = (f"Registrado: {rec['action']} por {rec['operator']} ({rec['review_id']})" if new else
                f"Este envío ya estaba registrado ({rec['review_id']}); no se duplicó.")
        resp = RedirectResponse(f"{url}?{urlencode({'msg': text})}#revision", status_code=303)
        resp.set_cookie("operador", rec["operator"], max_age=30 * 86400, samesite="lax")
        return resp

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

    def render_event(request: Request, event_id: str, low: bool = False, status_code: int = 200,
                     **review) -> HTMLResponse:
        ev = event_or_404(event_id)
        return templates.TemplateResponse(request, "event.html", {
            "ev": ev, "alerts": alerts_for_event(event_id), "show_low": low,
            "run": one("SELECT run_id, label FROM runs WHERE run_id = ?", (ev["run_id"],)),
            **review_ctx(request, "event", event_id, **review),
        }, status_code=status_code)

    @app.get("/eventos/{event_id}", response_class=HTMLResponse)
    def event_page(request: Request, event_id: str, low: bool = False, msg: str = ""):
        return render_event(request, event_id, low=low, msg=msg)

    @app.post("/eventos/{event_id}/revision", response_class=HTMLResponse)
    async def capture_review_form(request: Request, event_id: str):
        form: dict = {}
        try:
            form = await form_data(request)
            rec, new = submit_capture(event_id, form)
        except ReviewError as e:
            if e.status == 404:
                raise HTTPException(404, e.message)
            return render_event(request, event_id, status_code=e.status, error=e.message, form=form)
        return done(f"/eventos/{event_id}", rec, new)

    @app.get("/alertas", response_class=HTMLResponse)
    def alerts_page(request: Request, run_id: str = "", severity: str = "", rule_id: str = "", status: str = "",
                    revision: str = ""):
        items = query_alerts(run_id, severity, rule_id, status)
        hs = histories("alert")
        for a in items:
            a["n_events"] = len(json.loads(a["event_ids_json"]))
            a["review"] = alert_state(hs.get(a["alert_id"], []))
        if revision:
            items = [a for a in items if a["review"]["state"] == revision]
        return templates.TemplateResponse(request, "alerts.html", {
            "items": items, "runs": run_options(),
            "rules": [r["rule_id"] for r in rows("SELECT DISTINCT rule_id FROM alerts ORDER BY 1")],
            "f": {"run_id": run_id, "severity": severity, "rule_id": rule_id, "status": status, "revision": revision},
        })

    def render_alert(request: Request, alert_id: str, status_code: int = 200, **review) -> HTMLResponse:
        al = alert_or_404(alert_id)
        ids = al["event_ids"]
        evs = rows(
            f"""SELECT event_id, seq, captured_at, status, n_detections, n_accepted, n_doubt, n_low,
                       low_ratio, image_ref, error FROM events WHERE event_id IN ({",".join("?" * len(ids))})
                ORDER BY captured_at""", ids) if ids else []
        caps = histories("event", al["run_id"] or "")
        for e in evs:
            e["review"] = capture_state(caps.get(e["event_id"], []))
        return templates.TemplateResponse(request, "alert.html", {"al": al, "events": evs,
                                                                  **review_ctx(request, "alert", alert_id, **review)},
                                          status_code=status_code)

    @app.get("/alertas/{alert_id}", response_class=HTMLResponse)
    def alert_page(request: Request, alert_id: str, msg: str = ""):
        return render_alert(request, alert_id, msg=msg)

    @app.post("/alertas/{alert_id}/revision", response_class=HTMLResponse)
    async def alert_review_form(request: Request, alert_id: str):
        form: dict = {}
        try:
            form = await form_data(request)
            rec, new = submit_alert(alert_id, form)
        except ReviewError as e:
            if e.status == 404:
                raise HTTPException(404, e.message)
            return render_alert(request, alert_id, status_code=e.status, error=e.message, form=form)
        return done(f"/alertas/{alert_id}", rec, new)

    @app.get("/revision", response_class=HTMLResponse)
    def review_queue(request: Request, run_id: str = "", estado: str = "", reason: str = "", label: str = ""):
        """Cola de revisión: capturas de una ejecución con su etiqueta vigente y alertas por revisar."""
        runs = rows("""SELECT r.run_id, r.label, COUNT(e.event_id) AS n_images FROM runs r
                       JOIN events e ON e.run_id = r.run_id AND e.image_ref IS NOT NULL
                       GROUP BY r.run_id ORDER BY r.started_at DESC""")
        if not runs:
            return templates.TemplateResponse(request, "review.html", {"runs": [], "f": {}, "items": [], "alerts": []})
        run_id = run_id or runs[0]["run_id"]
        if not any(r["run_id"] == run_id for r in runs):
            raise HTTPException(404, f"Ejecución sin capturas o inexistente: {run_id}")
        evs = rows("""SELECT event_id, seq, captured_at, capture_reason, n_detections, n_low, low_ratio FROM events
                      WHERE run_id = ? AND image_ref IS NOT NULL ORDER BY seq""", (run_id,))
        caps = histories("event", run_id)
        for e in evs:
            e["review"] = capture_state(caps.get(e["event_id"], []))
        counts = {"total": len(evs), "revisadas": sum(e["review"]["state"] == "revisada" for e in evs)}
        counts["por_etiqueta"] = {k: sum(e["review"]["label"] == k for e in evs) for k in CAPTURE_LABELS}
        items = [e for e in evs
                 if (not estado or e["review"]["state"] == estado) and (not reason or e["capture_reason"] == reason)
                 and (not label or e["review"]["label"] == label)]
        hs = histories("alert", run_id)
        alerts = rows("SELECT alert_id, rule_id, severity, status FROM alerts WHERE run_id = ? ORDER BY timestamp",
                      (run_id,))
        for a in alerts:
            a["review"] = alert_state(hs.get(a["alert_id"], []))
        return templates.TemplateResponse(request, "review.html", {
            "runs": runs, "items": items, "counts": counts, "alerts": alerts, "labels": CAPTURE_LABELS,
            "reasons": sorted({e["capture_reason"] for e in evs if e["capture_reason"]}),
            "f": {"run_id": run_id, "estado": estado, "reason": reason, "label": label}})

    # -- vista en vivo (F4) ----------------------------------------------------
    RUN_ID_RE = re.compile(r"^[\w\-]+$")          # ids de ejecución: sin puntos ni separadores de ruta

    def live_or_404(run_id: str) -> dict:
        st = livemod.read_live(live_dir, run_id) if RUN_ID_RE.match(run_id) else None
        if st is None:
            raise HTTPException(404, f"Sin vista en vivo para: {run_id}")
        return st

    @app.get("/vivo", response_class=HTMLResponse)
    def live_page(request: Request, run_id: str = ""):
        runs = livemod.list_live(live_dir)
        if not runs:
            return templates.TemplateResponse(request, "vivo.html", {"runs": []})
        st = live_or_404(run_id) if run_id else runs[0]
        return templates.TemplateResponse(request, "vivo.html", {"runs": runs, "st": st})

    @app.get("/vivo/panel/{run_id}", response_class=HTMLResponse)
    def live_panel(request: Request, run_id: str):
        return templates.TemplateResponse(request, "_vivo_panel.html", {"st": live_or_404(run_id)},
                                          headers={"Cache-Control": "no-store"})

    @app.get("/vivo/frame/{run_id}")
    def live_frame(run_id: str):
        live_or_404(run_id)
        f = live_dir / run_id / "frame.jpg"
        if not f.is_file():
            raise HTTPException(404, "Todavía no hay frame")
        return Response(f.read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/vivo")
    def api_live_list():
        return {"items": [{"run_id": s["run_id"], "label": s.get("label"), "updated_at": s.get("updated_at"),
                           **s["vivo"]} for s in livemod.list_live(live_dir)]}

    @app.get("/api/vivo/{run_id}")
    def api_live(run_id: str):
        return live_or_404(run_id)

    # -- exportación (F3) ------------------------------------------------------
    def export_params(request: Request) -> dict:
        return {k: v for k, v in request.query_params.items() if k != "preview"}

    @app.get("/exportar", response_class=HTMLResponse)
    def export_page(request: Request):
        params = export_params(request)
        ctx = {"runs": run_options(), "q": params, "levels": exporter.LEVELS, "formats": exporter.FORMATS,
               "statuses": exporter.STATUSES, "zones": exporter.ZONES, "log": exporter.read_log(exports_log),
               "reasons": [r["capture_reason"] for r in rows(
                   "SELECT DISTINCT capture_reason FROM events WHERE capture_reason IS NOT NULL ORDER BY 1")],
               "error": "", "meta": None, "preview": [], "qs": urlencode(params)}
        if "preview" not in request.query_params:
            return templates.TemplateResponse(request, "export.html", ctx)
        try:
            spec = exporter.ExportSpec.from_params(params)
            _, meta, out = exporter.build(db_path, spec)
        except exporter.ExportError as e:
            return templates.TemplateResponse(request, "export.html", {**ctx, "error": e.message}, status_code=e.status)
        return templates.TemplateResponse(request, "export.html", {**ctx, "meta": meta,
                                                                   "preview": out[:exporter.PREVIEW_ROWS]})

    def export_file(request: Request, origin: str):
        spec = exporter.ExportSpec.from_params(export_params(request))
        data, meta, _ = exporter.build(db_path, spec)
        exporter.log_export(exports_log, meta, origin)
        media = "text/csv; charset=utf-8" if spec.formato == "csv" else "application/x-ndjson; charset=utf-8"
        return Response(data, media_type=media, headers={
            "Content-Disposition": f'attachment; filename="{meta["filename"]}"',
            "X-Export-Rows": str(meta["filas"]), "X-Export-SHA256": meta["sha256"]})

    @app.get("/exportar/descargar")
    def export_download(request: Request):
        try:
            return export_file(request, "portal")
        except exporter.ExportError as e:
            return templates.TemplateResponse(request, "export.html", {
                "runs": run_options(), "q": export_params(request), "levels": exporter.LEVELS,
                "formats": exporter.FORMATS, "statuses": exporter.STATUSES, "zones": exporter.ZONES,
                "reasons": [], "log": exporter.read_log(exports_log), "error": e.message, "meta": None,
                "preview": [], "qs": ""}, status_code=e.status)

    @app.get("/api/exportar")
    def api_export(request: Request):
        try:
            return export_file(request, "api")
        except exporter.ExportError as e:
            return JSONResponse({"detail": e.message}, status_code=e.status)

    @app.get("/api/exportaciones")
    def api_exports_log(limit: int = Query(20, ge=1, le=500)):
        return {"items": exporter.read_log(exports_log, limit)}

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
        health = rows("SELECT at, type, detail_json FROM source_events WHERE run_id = ? ORDER BY at", (run_id,))
        for h in health:
            # los registros anteriores al filtro pueden traer códigos de color ANSI de yt-dlp
            h["detail"] = {k: ANSI.sub("", v) if isinstance(v, str) else v
                           for k, v in json.loads(h.pop("detail_json")).items()}
        versions = rows("SELECT model_ref, COUNT(*) AS n, MIN(seq) AS first_seq, MAX(seq) AS last_seq FROM events "
                        "WHERE run_id = ? GROUP BY model_ref ORDER BY first_seq", (run_id,))
        return templates.TemplateResponse(request, "run.html", {"r": r, "m": manifest, "s": stats,
                                                                "windows": windows, "alerts": alerts,
                                                                "health": health, "versions": versions})

    def chart_data(run_id: str) -> dict:
        """Series por ventana de una ejecución: proporción baja, media y W1 frente a la referencia."""
        r = one("SELECT run_id, config_ref FROM runs WHERE run_id = ?", (run_id,))
        if not r:
            raise HTTPException(404, f"Run no encontrado: {run_id}")
        wins = rows("SELECT idx, state, low_ratio, mean_score, n_frames FROM windows WHERE run_id = ? ORDER BY idx",
                    (run_id,))
        if not wins:                                  # estado vacío: la ejecución no tiene ventanas
            return {"run_id": run_id, "config_ref": r["config_ref"], "n_windows": 0}
        cfg = json.loads(one("SELECT config_json FROM configs WHERE config_ref = ?", (r["config_ref"],))["config_json"])
        size, wcfg = cfg["windows"]["size_frames"], cfg["windows"]
        floor = cfg["policy"]["analysis_floor"]
        marks: dict[int, list[str]] = {}
        for a in rows("SELECT alert_id, rule_id FROM alerts WHERE run_id = ?", (run_id,)):
            m = re.search(r"-w(\d{4})$", a["alert_id"]) or re.search(r"-f(\d{6})$", a["alert_id"])
            if m:
                idx = int(m.group(1)) if "-w" in m.group(0) else (int(m.group(1)) - 1) // size + 1
                marks.setdefault(idx, []).append(a["rule_id"])
        rules = {x["signal"]: x for x in cfg.get("alerts", [])}
        ref = drift.load_reference(root / drift.REF_FILE)
        evs = rows("SELECT seq, status, detections_json FROM events WHERE run_id = ? ORDER BY seq", (run_id,))
        w1 = {}
        for i in range(0, len(evs), size):
            ch = evs[i:i + size]
            sc = np.array([d["score"] for e in ch if e["status"] in ("ok", "no_detections")
                           for d in json.loads(e["detections_json"]) if d["score"] >= floor])
            n_valid = sum(e["status"] in ("ok", "no_detections") for e in ch)
            ok = len(ch) == size and n_valid >= wcfg["min_valid_frames"] and len(sc) >= wcfg["min_eligible"]
            w1[i // size + 1] = drift.w1(sc, ref["quantiles"]) if (ok and ref) else None

        def pts(key):
            return [{"idx": w["idx"], "value": (w[key] if w["state"] in ("normal", "fuera_de_rango") else None),
                     "state": w["state"], "alerts": marks.get(w["idx"], [])} for w in wins]

        return {
            "run_id": run_id, "config_ref": r["config_ref"], "n_windows": len(wins),
            "low_ratio": {"points": pts("low_ratio"), "rule": rules.get("low_ratio")},
            "mean_score": {"points": pts("mean_score"), "rule": rules.get("mean_score")},
            "w1": {"points": [{"idx": w["idx"], "value": w1.get(w["idx"]), "state": w["state"],
                               "alerts": marks.get(w["idx"], [])} for w in wins],
                   "threshold": ref["threshold"] if ref else None, "has_reference": ref is not None},
        }

    @app.get("/graficas", response_class=HTMLResponse)
    def charts_page(request: Request, run_id: str = ""):
        runs = run_options()
        if not runs:
            return templates.TemplateResponse(request, "charts.html", {"runs": [], "run_id": "", "charts": []})
        run_id = run_id or runs[0]["run_id"]
        d = chart_data(run_id)
        if d["n_windows"] == 0:
            return templates.TemplateResponse(request, "charts.html", {"runs": runs, "run_id": run_id, "charts": [],
                                                                       "d": d})
        lr, ms, w = d["low_ratio"], d["mean_score"], d["w1"]

        def only(points, rule):     # cada gráfica marca las alertas de su regla, de la fuente y de versión
            keep = lambda r: r.startswith(("fuente-", "version-")) or (rule is not None and r == rule["rule_id"])
            return [{**p, "alerts": [r for r in p["alerts"] if keep(r)]} for p in points]

        lr["points"] = only(lr["points"], lr["rule"])
        ms["points"] = only(ms["points"], ms["rule"])
        w["points"] = only(w["points"], None)
        charts = [
            window_chart("Proporción de baja confianza (s < lower entre elegibles)", lr["points"],
                         lr["rule"]["threshold"] if lr["rule"] else None, lr["rule"]["op"] if lr["rule"] else None,
                         note=f"regla {lr['rule']['rule_id']}" if lr["rule"] else ""),
            window_chart("Puntuación media de las detecciones elegibles", ms["points"],
                         ms["rule"]["threshold"] if ms["rule"] else None, ms["rule"]["op"] if ms["rule"] else None,
                         note=f"regla {ms['rule']['rule_id']}" if ms["rule"] else ""),
            window_chart("Desplazamiento frente a la referencia (W1, métrica M1)", w["points"], w["threshold"], ">",
                         fmt="{:.4f}", note="" if w["has_reference"] else "falta config/referencia-distribucion.json",
                         has_rule=False),
        ]
        return templates.TemplateResponse(request, "charts.html", {"runs": runs, "run_id": run_id, "charts": charts,
                                                                   "d": d})

    @app.get("/api/runs/{run_id}/graficas")
    def api_charts(run_id: str):
        return chart_data(run_id)

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
            draw_detections(img, ev["detections"], show_low=low)
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

    @app.get("/api/alertas/{alert_id}/revision")
    def api_alert_review(alert_id: str):
        al = alert_or_404(alert_id)
        h = history("alert", alert_id)
        return {"alert_id": alert_id, "alert_status": al["status"], "review": alert_state(h), "history": h}

    @app.post("/api/alertas/{alert_id}/revision")
    async def api_alert_review_post(request: Request, alert_id: str):
        try:
            rec, new = submit_alert(alert_id, await form_data(request))
        except ReviewError as e:
            return JSONResponse({"detail": e.message}, status_code=e.status)
        return JSONResponse({"record": rec, "duplicate": not new, "review": alert_state(history("alert", alert_id))},
                            status_code=201 if new else 200)

    @app.get("/api/eventos/{event_id}/revision")
    def api_capture_review(event_id: str):
        ev = event_or_404(event_id)
        h = history("event", event_id)
        return {"event_id": event_id, "image_ref": ev["image_ref"], "review": capture_state(h), "history": h}

    @app.post("/api/eventos/{event_id}/revision")
    async def api_capture_review_post(request: Request, event_id: str):
        try:
            rec, new = submit_capture(event_id, await form_data(request))
        except ReviewError as e:
            return JSONResponse({"detail": e.message}, status_code=e.status)
        return JSONResponse({"record": rec, "duplicate": not new, "review": capture_state(history("event", event_id))},
                            status_code=201 if new else 200)

    @app.get("/api/revisiones")
    def api_reviews(run_id: str = "", target_type: str = ""):
        where, params = [], []
        for col, val in (("run_id", run_id), ("target_type", target_type)):
            if val:
                where.append(f"{col} = ?"); params.append(val)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        items = [json.loads(r["record_json"]) for r in rows(f"SELECT record_json FROM reviews {w} ORDER BY at, review_id", params)]
        return {"total": len(items), "items": items}

    @app.get("/api/runs")
    def api_runs():
        return rows("SELECT run_id, label, mode, started_at, finished_at, config_ref, model_ref FROM runs "
                    "ORDER BY started_at DESC")

    return app
