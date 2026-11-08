import json

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from monitor.portal.app import create_app
from monitor.store import Store

RUN = "20260101T000000Z-test"


def _event(seq, image_ref=None, status="ok"):
    dets = [
        {"class_id": 0, "class_name": "person", "score": 0.81, "bbox_xyxy": [10, 10, 40, 80], "zone": "accepted"},
        {"class_id": 2, "class_name": "car", "score": 0.12, "bbox_xyxy": [50, 50, 90, 70], "zone": "low"},
    ] if status == "ok" else []
    return {
        "event_id": f"evt-{RUN}-f{seq:06d}", "run_id": RUN, "frame_id": f"{RUN}-f{seq:06d}", "seq": seq,
        "timestamp": f"2026-01-01T00:00:0{seq}.100Z", "captured_at": f"2026-01-01T00:00:0{seq}.000Z",
        "source_id": "test-src", "model_ref": "yolo11n@sha256:abc", "config_ref": "cfg-test",
        "thresholds": {"lower": 0.35, "upper": 0.6}, "status": status, "detections": dets,
        "n_detections": len(dets), "zone_counts": {"accepted": 1, "doubt": 0, "low": 1} if dets else {},
        "low_ratio": 0.5 if dets else None, "timings": {"inference_ms": 100.0}, "e2e_ms": 120.0,
        "image_ref": image_ref, "capture_reason": "muestra_normal" if image_ref else None,
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_dir = tmp_path / "datos" / "runs" / RUN
    (run_dir / "imagenes").mkdir(parents=True)
    img_ref = f"datos/runs/{RUN}/imagenes/f1.jpg"
    cv2.imwrite(img_ref, np.full((120, 160, 3), 128, np.uint8))
    lines = [json.dumps(_event(1, img_ref)), json.dumps(_event(2, status="no_detections")),
             json.dumps(_event(1, img_ref)),          # reenvío: duplicado
             '{"event_id": "roto"}']                   # no cumple el contrato: rechazado
    (run_dir / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest = {"run_id": RUN, "label": "test", "mode": "replay", "started_at": "2026-01-01T00:00:00Z",
                "config_ref": "cfg-test", "config": {"policy": {"lower": 0.35, "upper": 0.6}},
                "model_ref": "yolo11n@sha256:abc", "model_sha256": "abc", "class_names": {"0": "person"}}
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    store = Store(tmp_path / "datos" / "monitor.db")
    result = store.ingest_run_dir(run_dir)
    assert result == {"run_id": RUN, "inserted": 2, "duplicates": 1, "rejected": 1}
    store.insert_alert({
        "alert_id": "alert-test-1", "rule_id": "baja-confianza-v1", "run_id": RUN,
        "timestamp": "2026-01-01T00:01:00Z", "severity": "warning",
        "reason": "Condición de prueba", "suggested_action": "Revisar iluminación",
        "config_ref": "cfg-test", "event_ids": [f"evt-{RUN}-f000001"], "metric": {"valor": 0.5},
    })
    store.close()
    return TestClient(create_app(tmp_path / "datos" / "monitor.db", root=tmp_path))


def test_events_list_and_filters(client):
    r = client.get("/eventos")
    assert r.status_code == 200 and f"evt-{RUN}-f000001" in r.text
    assert client.get("/api/eventos").json()["total"] == 2
    assert client.get("/api/eventos", params={"status": "no_detections"}).json()["total"] == 1
    assert client.get("/api/eventos", params={"with_image": "true"}).json()["total"] == 1
    assert client.get("/api/eventos", params={"run_id": "otro"}).json()["total"] == 0


def test_event_detail_links_image_config_and_alert(client):
    eid = f"evt-{RUN}-f000001"
    r = client.get(f"/eventos/{eid}")
    assert r.status_code == 200
    assert f"/imagen/{eid}" in r.text and "/config/cfg-test" in r.text and "alert-test-1" in r.text
    img = client.get(f"/imagen/{eid}")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert client.get(f"/imagen/evt-{RUN}-f000002").status_code == 404   # sin imagen capturada


def test_alerts_views(client):
    assert "alert-test-1" in client.get("/alertas").text
    r = client.get("/alertas/alert-test-1")
    assert r.status_code == 200 and "Revisar iluminación" in r.text and f"evt-{RUN}-f000001" in r.text
    assert client.get("/api/alertas").json()["items"][0]["event_ids"] == [f"evt-{RUN}-f000001"]


def test_runs_config_and_404(client):
    assert RUN in client.get("/runs").text
    assert client.get(f"/runs/{RUN}").status_code == 200
    assert client.get("/config/cfg-test").status_code == 200
    assert client.get("/eventos/no-existe").status_code == 404
    assert client.get("/alertas/no-existe").status_code == 404


def _with_windows(tmp_path):
    """Base con una ejecución con ventanas, una alerta de proporción y una de la fuente."""
    from monitor.portal.app import create_app as _create
    run = "20260102T000000Z-graf"
    cfg = {"policy": {"lower": 0.5, "upper": 0.65, "analysis_floor": 0.25},
           "windows": {"size_frames": 2, "min_valid_frames": 2, "min_eligible": 2},
           "alerts": [{"rule_id": "baja-confianza-v1", "signal": "low_ratio", "op": ">", "threshold": 0.7}]}
    st = Store(tmp_path / "g.db")
    st.upsert_config("cfg-g", cfg)
    st.upsert_run({"run_id": run, "label": "g", "mode": "replay", "started_at": "2026-01-02T00:00:00Z",
                   "config_ref": "cfg-g", "model_ref": "m"})
    states = [("normal", 0.6, 0.5), ("fuera_de_rango", 0.8, 0.4), ("insuficiente", None, None)]
    for i, (state, lr, ms) in enumerate(states, start=1):
        st.insert_window({"window_id": f"w{i}", "run_id": run, "idx": i, "config_ref": "cfg-g",
                          "first_event_id": "a", "last_event_id": "b", "start": "s", "end": "e", "n_frames": 2,
                          "n_valid": 2, "n_eligible": 10, "n_low": 5, "low_ratio": lr, "mean_score": ms,
                          "state": state, "conditions": {}, "event_ids": [], "image_refs": []})
    for aid, rule in ((f"alert-{run}-baja-confianza-v1-w0002", "baja-confianza-v1"),
                      (f"alert-{run}-fuente-interrumpida-v1-f000005", "fuente-interrumpida-v1")):
        st.insert_alert({"alert_id": aid, "rule_id": rule, "run_id": run, "timestamp": "t", "severity": "warning",
                         "reason": "r", "suggested_action": "a", "config_ref": "cfg-g", "event_ids": []})
    st.close()
    return TestClient(_create(tmp_path / "g.db", root=tmp_path)), run


def test_charts_series_gaps_and_alert_marks(tmp_path):
    c, run = _with_windows(tmp_path)
    d = c.get(f"/api/runs/{run}/graficas").json()
    assert [p["value"] for p in d["low_ratio"]["points"]] == [0.6, 0.8, None]       # insuficiente: hueco, no 0
    assert d["low_ratio"]["points"][1]["alerts"] == ["baja-confianza-v1"]
    assert d["low_ratio"]["points"][2]["alerts"] == ["fuente-interrumpida-v1"]       # seq 5 -> ventana 3 (2 frames)
    assert d["w1"]["has_reference"] is False and all(p["value"] is None for p in d["w1"]["points"])
    html = c.get(f"/graficas?run_id={run}").text
    assert html.count("<svg") == 2                         # sin referencia, W1 se muestra como estado vacío
    assert "insuficiente" in html and "umbral &gt; 0.700" in html and "falta config/referencia-distribucion.json" in html


def test_charts_empty_and_error_states(client, tmp_path):
    r = client.get(f"/graficas?run_id={RUN}")              # ejecución sin ventanas
    assert r.status_code == 200 and "no tiene ventanas registradas" in r.text
    assert client.get("/graficas?run_id=no-existe").status_code == 404
    assert client.get("/api/runs/no-existe/graficas").status_code == 404
    empty = Store(tmp_path / "vacia.db")
    empty.close()
    from monitor.portal.app import create_app as _create
    r = TestClient(_create(tmp_path / "vacia.db", root=tmp_path)).get("/graficas")
    assert r.status_code == 200 and "No hay ejecuciones registradas" in r.text
