"""F2 · revisión humana de alertas y capturas: estados, validación, idempotencia, persistencia y vistas."""

import json
import threading

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from monitor.portal.app import create_app
from monitor.review import ReviewError, ReviewLog, alert_state, check_alert_action, check_capture_review
from monitor.store import Store

RUN = "20260101T000000Z-f2"
OPEN = f"alert-{RUN}-baja-confianza-v1-w0001"
RESOLVED = f"alert-{RUN}-fuente-interrumpida-v1-f000001"
WITH_IMG, NO_IMG = f"evt-{RUN}-f000001", f"evt-{RUN}-f000002"


# -- reglas de transición (sin portal) -------------------------------------------
ALERT = {"alert_id": "a", "run_id": "r", "rule_id": "x", "status": "open", "config_ref": "c"}


def act(alert, history, **form):
    rec = check_alert_action(alert, history, {"operator": "Ana", **form})
    rec["at"] = f"t{len(history)}"
    return history + [rec]


def test_alert_state_machine_and_rejections():
    h = act(ALERT, [], action="reconocer")
    assert alert_state(h)["state"] == "reconocida" and alert_state(h)["acknowledged_by"] == "Ana"
    h = act(ALERT, h, action="comentar", note="sigue lloviendo")
    assert alert_state(h)["state"] == "reconocida"
    cases = [
        (ALERT, h, {"action": "reconocer"}, 409),                                    # solo una vez
        (ALERT, [], {"action": "cerrar", "verdict": "esperada", "note": "x"}, 409),  # sin reconocer
        (ALERT, h, {"action": "cerrar", "verdict": "esperada", "note": "x"}, 409),   # la regla no la resolvió
        ({**ALERT, "status": "resolved"}, h, {"action": "cerrar", "verdict": "quizá", "note": "x"}, 422),
        ({**ALERT, "status": "resolved"}, h, {"action": "cerrar", "verdict": "esperada"}, 422),   # sin nota
        (ALERT, [], {"action": "borrar"}, 422),
        (ALERT, [], {"action": "comentar"}, 422),                                    # comentario vacío
        (ALERT, [], {"action": "reconocer", "operator": "  "}, 422),
        (ALERT, [], {"action": "reconocer", "operator": "x" * 61}, 422),
    ]
    for alert, hist, form, code in cases:
        with pytest.raises(ReviewError) as e:
            check_alert_action(alert, hist, {"operator": "Ana", **form})
        assert e.value.status == code, form
    h = act({**ALERT, "status": "resolved"}, h, action="cerrar", verdict="esperada", note="escenario provocado")
    st = alert_state(h)
    assert st["state"] == "revisada" and st["verdict"] == "esperada"
    with pytest.raises(ReviewError) as e:
        check_alert_action({**ALERT, "status": "resolved"}, h, {"operator": "Ana", "action": "cerrar",
                                                                 "verdict": "confirmada", "note": "otra vez"})
    assert e.value.status == 409


def test_capture_review_rules():
    ev = {"event_id": "e", "run_id": "r", "image_ref": "img.jpg", "config_ref": "c"}
    rec = check_capture_review(ev, [], {"operator": "Ana", "label": "clima"})
    assert rec["state_before"] == "pendiente" and rec["state_after"] == "clima"
    for form, code in (({"label": "clima"}, 422),                         # sin operador
                       ({"operator": "Ana", "label": "bonita"}, 422),
                       ({"operator": "Ana", "label": "otro"}, 422)):      # "otro" sin nota
        with pytest.raises(ReviewError) as e:
            check_capture_review(ev, [], form)
        assert e.value.status == code
    with pytest.raises(ReviewError) as e:
        check_capture_review({**ev, "image_ref": None}, [], {"operator": "Ana", "label": "clima"})
    assert e.value.status == 409


# -- portal: flujo completo sobre una base y una carpeta de ejecución reales --------
def _event(seq, image_ref):
    return {"event_id": f"evt-{RUN}-f{seq:06d}", "run_id": RUN, "frame_id": f"{RUN}-f{seq:06d}", "seq": seq,
            "timestamp": f"2026-01-01T00:00:0{seq}Z", "captured_at": f"2026-01-01T00:00:0{seq}Z",
            "source_id": "s", "model_ref": "m@sha256:abc", "config_ref": "cfg", "thresholds": {"lower": 0.5, "upper": 0.65},
            "status": "ok", "detections": [], "n_detections": 0, "image_ref": image_ref,
            "capture_reason": "muestra_normal" if image_ref else None, "timings": {"inference_ms": 1.0}}


def _alert(alert_id, rule):
    return {"alert_id": alert_id, "rule_id": rule, "run_id": RUN, "timestamp": "2026-01-01T00:01:00Z",
            "severity": "warning", "reason": "r", "suggested_action": "s", "config_ref": "cfg",
            "event_ids": [WITH_IMG, NO_IMG], "metric": {}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_dir = tmp_path / "datos" / "runs" / RUN
    (run_dir / "imagenes").mkdir(parents=True)
    img = f"datos/runs/{RUN}/imagenes/f1.jpg"
    cv2.imwrite(img, np.full((60, 80, 3), 90, np.uint8))
    (run_dir / "events.jsonl").write_text("\n".join(json.dumps(_event(i, img if i == 1 else None)) for i in (1, 2)) + "\n",
                                          encoding="utf-8")
    (run_dir / "alerts.jsonl").write_text("\n".join(json.dumps(x) for x in (
        {"type": "alert", **_alert(OPEN, "baja-confianza-v1")},
        {"type": "alert", **_alert(RESOLVED, "fuente-interrumpida-v1")},
        {"type": "update", "alert_id": RESOLVED, "status": "resolved", "resolved_at": "2026-01-01T00:02:00Z"})) + "\n",
        encoding="utf-8")
    (run_dir / "manifest.json").write_text(json.dumps({
        "run_id": RUN, "label": "f2", "mode": "replay", "started_at": "2026-01-01T00:00:00Z", "config_ref": "cfg",
        "config": {"policy": {"lower": 0.5, "upper": 0.65}}, "model_ref": "m@sha256:abc", "model_sha256": "abc",
        "class_names": {}}), encoding="utf-8")
    db = tmp_path / "datos" / "monitor.db"
    st = Store(db)
    st.ingest_run_dir(run_dir)
    st.close()
    return tmp_path, run_dir, db, TestClient(create_app(db, root=tmp_path))


def post(c, path, **body):
    return c.post(path, json={"operator": "Luis", "request_id": body.pop("rid", None) or body["action"] + path, **body})


def test_alert_review_api_flow_idempotency_and_conflicts(env):
    _, run_dir, _, c = env
    r = post(c, f"/api/alertas/{RESOLVED}/revision", action="reconocer", rid="r1")
    assert r.status_code == 201 and r.json()["review"]["state"] == "reconocida"
    again = post(c, f"/api/alertas/{RESOLVED}/revision", action="reconocer", rid="r1")      # mismo envío
    assert again.status_code == 200 and again.json()["duplicate"] and again.json()["record"] == r.json()["record"]
    assert post(c, f"/api/alertas/{RESOLVED}/revision", action="reconocer", rid="r2").status_code == 409
    closed = post(c, f"/api/alertas/{RESOLVED}/revision", action="cerrar", verdict="esperada",
                  note="corte inyectado en S1", rid="r3")
    assert closed.status_code == 201 and closed.json()["review"]["verdict"] == "esperada"
    # la alerta abierta no se puede cerrar aunque esté reconocida
    assert post(c, f"/api/alertas/{OPEN}/revision", action="reconocer", rid="r4").status_code == 201
    blocked = post(c, f"/api/alertas/{OPEN}/revision", action="cerrar", verdict="confirmada", note="x", rid="r5")
    assert blocked.status_code == 409 and "todavía no resolvió" in blocked.json()["detail"]
    assert post(c, "/api/alertas/alert-no-existe/revision", action="reconocer", rid="r6").status_code == 404
    assert c.post(f"/api/alertas/{OPEN}/revision", json={"operator": "Luis", "action": "comentar",
                                                          "note": "x"}).status_code == 422   # sin request_id
    # registro primario: solo las acciones aceptadas, una vez cada una
    lines = [json.loads(l) for l in (run_dir / "revisiones.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [l["request_id"] for l in lines] == ["r1", "r3", "r4"]
    assert lines[1]["state_before"] == "reconocida" and lines[1]["alert_status"] == "resolved"
    h = c.get(f"/api/alertas/{RESOLVED}/revision").json()
    assert h["alert_status"] == "resolved" and [x["action"] for x in h["history"]] == ["reconocer", "cerrar"]
    # la revisión no toca la alerta
    assert c.get(f"/api/alertas/{OPEN}").json()["status"] == "open"


def test_capture_review_history_and_errors(env):
    _, _, _, c = env
    assert post(c, f"/api/eventos/{WITH_IMG}/revision", action="revisar", label="clima", rid="c1").status_code == 201
    r = post(c, f"/api/eventos/{WITH_IMG}/revision", action="revisar", label="oclusion", note="multitud", rid="c2")
    assert r.status_code == 201 and r.json()["review"] == {**r.json()["review"], "label": "oclusion", "n_reviews": 2}
    assert post(c, f"/api/eventos/{NO_IMG}/revision", action="revisar", label="clima", rid="c3").status_code == 409
    assert post(c, f"/api/eventos/{WITH_IMG}/revision", action="revisar", label="x", rid="c4").status_code == 422
    assert post(c, "/api/eventos/evt-nada/revision", action="revisar", label="clima", rid="c5").status_code == 404
    assert c.get(f"/api/revisiones?run_id={RUN}&target_type=event").json()["total"] == 2


def test_state_is_rebuilt_from_the_jsonl(env):
    root, run_dir, db, c = env
    post(c, f"/api/alertas/{RESOLVED}/revision", action="reconocer", rid="a")
    post(c, f"/api/alertas/{RESOLVED}/revision", action="cerrar", verdict="esperada", note="ok", rid="b")
    post(c, f"/api/eventos/{WITH_IMG}/revision", action="revisar", label="clima", rid="c")
    before = (c.get(f"/api/alertas/{RESOLVED}/revision").json(), c.get(f"/api/eventos/{WITH_IMG}/revision").json())
    fresh = root / "datos" / "nueva.db"                 # base nueva, solo desde los registros de la ejecución
    st = Store(fresh)
    st.ingest_run_dir(run_dir)
    st.ingest_run_dir(run_dir)                          # reingesta idempotente: no duplica revisiones
    st.close()
    c2 = TestClient(create_app(fresh, root=root))
    after = (c2.get(f"/api/alertas/{RESOLVED}/revision").json(), c2.get(f"/api/eventos/{WITH_IMG}/revision").json())
    assert after == before and len(after[0]["history"]) == 2


def test_html_forms_redirect_or_show_error(env):
    _, _, _, c = env
    ok = c.post(f"/alertas/{RESOLVED}/revision", data={"operator": "Luis", "action": "reconocer", "note": "",
                                                       "request_id": "h1"}, follow_redirects=False)
    assert ok.status_code == 303 and "msg=" in ok.headers["location"] and ok.cookies.get("operador") == "Luis"
    page = c.get(ok.headers["location"]).text
    assert "Registrado: reconocer por Luis" in page and "reconocida" in page and "Cerrar revisión" in page
    bad = c.post(f"/alertas/{OPEN}/revision", data={"operator": "Luis", "action": "cerrar", "verdict": "confirmada",
                                                    "note": "x", "request_id": "h2"})
    assert bad.status_code == 409 and "No se registró" in bad.text
    assert "Cerrar revisión" not in c.get(f"/alertas/{OPEN}").text        # alerta abierta: no se ofrece cerrar
    ev = c.post(f"/eventos/{NO_IMG}/revision", data={"operator": "Luis", "label": "clima", "request_id": "h3"})
    assert ev.status_code == 409 and "Sin imagen capturada" in ev.text
    assert c.post("/alertas/nada/revision", data={"operator": "L", "action": "reconocer",
                                                  "request_id": "h4"}).status_code == 404


def test_review_queue_and_alert_filter(env):
    _, _, _, c = env
    q = c.get("/revision").text
    assert "0 de 1" in q and "pendiente" in q
    post(c, f"/api/eventos/{WITH_IMG}/revision", action="revisar", label="clima", rid="q1")
    post(c, f"/api/alertas/{OPEN}/revision", action="reconocer", rid="q2")
    assert "1 de 1" in c.get(f"/revision?run_id={RUN}").text
    assert "Ninguna captura con estos filtros" in c.get(f"/revision?run_id={RUN}&estado=pendiente").text
    assert c.get("/revision?run_id=no-existe").status_code == 404
    lst = c.get("/alertas?revision=reconocida").text
    assert OPEN in lst and RESOLVED not in lst


def test_review_queue_empty_state(tmp_path):
    db = tmp_path / "vacia.db"
    Store(db).close()
    assert "No hay ejecuciones con capturas" in TestClient(create_app(db, root=tmp_path)).get("/revision").text


def test_missing_run_folder_is_rejected(env):
    _, run_dir, _, c = env
    (run_dir / "events.jsonl").rename(run_dir.parent / "events.bak")
    for f in run_dir.rglob("*"):
        if f.is_file():
            f.unlink()
    (run_dir / "imagenes").rmdir()
    run_dir.rmdir()
    r = post(c, f"/api/alertas/{OPEN}/revision", action="reconocer", rid="m1")
    assert r.status_code == 409 and "registro primario" in r.json()["detail"]


def test_concurrent_acknowledgements_only_one_wins(env):
    root, run_dir, db, _ = env
    log = ReviewLog(run_dir.parent, lambda: Store(db))

    def load(store):
        return store.alert(OPEN), store.reviews_for("alert", OPEN)

    results = []

    def worker(i):
        try:
            log.submit(f"conc-{i}", load, lambda al, h: check_alert_action(al, h, {"operator": f"op{i}",
                                                                                   "action": "reconocer"}))
            results.append("ok")
        except ReviewError as e:
            results.append(e.status)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results, key=str) == [409] * 7 + ["ok"]


def test_badly_encoded_form_is_rejected_not_stored(env):
    _, run_dir, _, c = env
    body = b"operator=Luis&action=comentar&request_id=enc1&note=prop%F3sito"     # latin-1, no UTF-8
    r = c.post(f"/api/alertas/{OPEN}/revision", content=body,
               headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 422 and "UTF-8" in r.json()["detail"]
    ok = c.post(f"/alertas/{OPEN}/revision", data={"operator": "Luis", "action": "comentar", "request_id": "enc2",
                                                   "note": "propósito y reconexión"}, follow_redirects=False)
    assert ok.status_code == 303
    (rec,) = [json.loads(l) for l in (run_dir / "revisiones.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rec["note"] == "propósito y reconexión"
