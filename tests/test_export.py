"""F3 · exportación de eventos: filtros, granularidad, determinismo, procedencia, estados vacíos y errores."""

import csv
import hashlib
import io
import json

import pytest
from fastapi.testclient import TestClient

from monitor import export as ex
from monitor.portal.app import create_app
from monitor.store import Store

RUN = "20260101T000000Z-f3"
ALERT = f"alert-{RUN}-baja-confianza-v1-w0001"


def _det(cls, name, score, zone):
    return {"class_id": cls, "class_name": name, "score": score, "bbox_xyxy": [1.0, 2.0, 3.0, 4.0], "zone": zone}


def _event(seq, status, dets, image=None, low=None):
    return {"event_id": f"evt-{RUN}-f{seq:06d}", "run_id": RUN, "frame_id": f"{RUN}-f{seq:06d}", "seq": seq,
            "timestamp": f"2026-01-01T00:00:0{seq}Z", "captured_at": f"2026-01-01T00:00:0{seq}Z", "source_id": "s",
            "model_ref": "yolo11n@sha256:abc" if seq != 3 else "yolo11s@sha256:def", "config_ref": "cfg",
            "thresholds": {"lower": 0.5, "upper": 0.65}, "status": status, "detections": dets, "n_detections": len(dets),
            "zone_counts": {z: sum(d["zone"] == z for d in dets) for z in ("accepted", "doubt", "low")},
            "low_ratio": low, "timings": {"inference_ms": 100.0}, "e2e_ms": 110.0, "image_ref": image,
            "capture_reason": "baja_confianza" if image else None}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_dir = tmp_path / "datos" / "runs" / RUN
    run_dir.mkdir(parents=True)
    evs = [_event(1, "ok", [_det(0, "person", 0.81, "accepted"), _det(2, "car", 0.12, "low")], image="img1.jpg", low=0.5),
           _event(2, "no_detections", []),
           _event(3, "ok", [_det(0, "person", 0.55, "doubt")], low=0.0)]
    (run_dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in evs), encoding="utf-8")
    (run_dir / "alerts.jsonl").write_text(json.dumps({
        "type": "alert", "alert_id": ALERT, "rule_id": "baja-confianza-v1", "run_id": RUN,
        "timestamp": "2026-01-01T00:01:00Z", "severity": "warning", "reason": "r", "suggested_action": "s",
        "config_ref": "cfg", "event_ids": [evs[0]["event_id"], evs[2]["event_id"]], "metric": {}}) + "\n", encoding="utf-8")
    (run_dir / "revisiones.jsonl").write_text(json.dumps({
        "review_id": "rev-1", "request_id": "q1", "at": "2026-01-02T00:00:00Z", "run_id": RUN, "target_type": "event",
        "target_id": evs[0]["event_id"], "action": "revisar", "operator": "Ana", "note": "", "verdict": None,
        "label": "clima", "state_before": "pendiente", "state_after": "clima"}) + "\n", encoding="utf-8")
    (run_dir / "manifest.json").write_text(json.dumps({
        "run_id": RUN, "label": "f3", "mode": "replay", "started_at": "2026-01-01T00:00:00Z", "config_ref": "cfg",
        "config": {"policy": {"lower": 0.5, "upper": 0.65}}, "model_ref": "yolo11n@sha256:abc", "model_sha256": "abc",
        "class_names": {}}), encoding="utf-8")
    db = tmp_path / "datos" / "monitor.db"
    st = Store(db)
    st.ingest_run_dir(run_dir)
    st.close()
    return tmp_path, db, TestClient(create_app(db, root=tmp_path))


def spec(**kw):
    return ex.ExportSpec.from_params({"run_id": RUN, **kw})


def test_event_level_csv_has_traceability_and_matches_events_api(env):
    _, db, c = env
    data, meta, _ = ex.build(db, spec())
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8"))))
    assert meta["filas"] == len(rows) == c.get(f"/api/eventos?run_id={RUN}").json()["total"] == 3
    assert [r["seq"] for r in rows] == ["1", "2", "3"]                              # orden estable
    assert rows[0]["alert_ids"] == ALERT and rows[1]["alert_ids"] == "" and rows[0]["revision_captura"] == "clima"
    assert rows[1]["low_ratio"] == "" and rows[1]["status"] == "no_detections"   # ausencia de dato: vacío, no 0
    assert meta["sha256"] == hashlib.sha256(data).hexdigest()


def test_detection_level_and_detection_filters(env):
    _, db, _ = env
    _, meta, rows = ex.build(db, spec(nivel="deteccion"))
    assert meta["filas"] == 3 and [r["score"] for r in rows] == [0.81, 0.12, 0.55]
    assert ex.build(db, spec(nivel="deteccion", class_name="person", min_score="0.5"))[1]["filas"] == 2
    assert ex.build(db, spec(nivel="deteccion", zone="low"))[1]["filas"] == 1
    # en nivel evento, un filtro de detección elige los eventos que tengan alguna que cumpla
    assert [r["seq"] for r in ex.build(db, spec(class_name="car"))[2]] == [1]


@pytest.mark.parametrize("kw,expected", [
    ({"status": "no_detections"}, [2]), ({"reason": "baja_confianza"}, [1]), ({"with_image": "true"}, [1]),
    ({"model_ref": "yolo11s@sha256:def"}, [3]), ({"alert_id": ALERT}, [1, 3]),
    ({"min_low_ratio": "0.4"}, [1]), ({"max_low_ratio": "0.1"}, [3]),
    ({"since": "2026-01-01T00:00:02Z", "until": "2026-01-01T00:00:03Z"}, [2, 3]),
])
def test_filters(env, kw, expected):
    _, db, _ = env
    assert [r["seq"] for r in ex.build(db, spec(**kw))[2]] == expected


def test_deterministic_and_jsonl(env):
    _, db, _ = env
    a, b = ex.build(db, spec())[0], ex.build(db, spec())[0]
    assert a == b                                                                    # mismos bytes
    j, meta, _ = ex.build(db, spec(formato="jsonl"))
    lines = [json.loads(l) for l in j.decode("utf-8").splitlines()]
    assert len(lines) == 3 and lines[1]["low_ratio"] is None and meta["filename"].endswith(".jsonl")


@pytest.mark.parametrize("kw,code", [
    ({"formato": "xlsx"}, 422), ({"nivel": "frame"}, 422), ({"status": "raro"}, 422), ({"zone": "media"}, 422),
    ({"since": "ayer"}, 422), ({"since": "2026-01-02T00:00:00Z", "until": "2026-01-01T00:00:00Z"}, 422),
    ({"min_score": "alto"}, 422), ({"min_low_ratio": "1.5"}, 422), ({"min_low_ratio": "0.8", "max_low_ratio": "0.2"}, 422),
    ({"columna": "x"}, 422), ({"run_id": "no-existe"}, 404), ({"alert_id": "alert-no-existe"}, 404),
])
def test_errors(env, kw, code):
    _, db, c = env
    with pytest.raises(ex.ExportError) as e:
        ex.build(db, ex.ExportSpec.from_params({"run_id": RUN, **kw}))
    assert e.value.status == code
    q = "&".join(f"{k}={v}" for k, v in {"run_id": RUN, **kw}.items())
    r = c.get(f"/api/exportar?{q}")
    assert r.status_code == code and "detail" in r.json()


def test_download_headers_log_and_empty_export(env):
    root, db, c = env
    r = c.get(f"/api/exportar?run_id={RUN}&nivel=deteccion")
    assert r.status_code == 200 and r.headers["x-export-rows"] == "3"
    assert r.headers["x-export-sha256"] == hashlib.sha256(r.content).hexdigest() == ex.build(db, spec(nivel="deteccion"))[1]["sha256"]
    assert "attachment" in r.headers["content-disposition"]
    empty = c.get(f"/api/exportar?run_id={RUN}&status=inference_error")
    assert empty.status_code == 200 and empty.headers["x-export-rows"] == "0"
    assert empty.text == ",".join(ex.EVENT_COLUMNS) + "\n"                          # solo encabezado
    log = c.get("/api/exportaciones").json()["items"]
    assert [l["filas"] for l in log] == [0, 3] and log[1]["sha256"] == r.headers["x-export-sha256"]
    assert (root / "datos" / "exportaciones.jsonl").exists()


def test_html_preview_empty_and_error_states(env):
    _, _, c = env
    page = c.get(f"/exportar?preview=1&run_id={RUN}&class_name=person").text
    assert "2 eventos" in page and "Descargar" in page and "Primeras 2 filas" in page
    empty = c.get(f"/exportar?preview=1&run_id={RUN}&status=inference_error").text
    assert "no hay nada que descargar" in empty and "Descargar eventos" not in empty
    bad = c.get(f"/exportar?preview=1&run_id={RUN}&since=ayer")
    assert bad.status_code == 422 and "No se puede exportar" in bad.text
    assert c.get("/exportar/descargar?run_id=no-existe").status_code == 404
    assert "Exportar estos eventos" in c.get(f"/eventos?run_id={RUN}").text


def test_cli_writes_same_bytes_and_verifies(env, tmp_path, monkeypatch):
    _, db, c = env
    import sys
    out = tmp_path / "x.csv"
    monkeypatch.setattr(sys, "argv", ["export", "--db", str(db), "--run-id", RUN, "--out", str(out)])
    ex.main()
    assert out.read_bytes() == c.get(f"/api/exportar?run_id={RUN}").content
    monkeypatch.setattr(sys, "argv", ["export", "--db", str(db), "--run-id", RUN, "--verificar", str(out)])
    with pytest.raises(SystemExit) as e:
        ex.main()
    assert e.value.code == 0
    out.write_bytes(out.read_bytes() + b"x")                                         # archivo alterado
    with pytest.raises(SystemExit) as e:
        ex.main()
    assert e.value.code == 1


def test_preview_form_keeps_selected_filters(env):
    _, _, c = env
    page = c.get(f"/exportar?preview=1&run_id={RUN}&nivel=deteccion&formato=jsonl&class_name=person").text
    assert f'<option value="{RUN}" selected' in page and "<option selected>deteccion" in page
    assert "<option selected>jsonl" in page and 'name="class_name" value="person"' in page
