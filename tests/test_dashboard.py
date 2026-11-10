"""F5 · panel de operación: indicadores, reglas de atención, estado coordinado, filtros, verificación y vistas."""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from monitor import dashboard as panel
from monitor.portal.app import create_app
from monitor.store import Store

NOW = datetime(2026, 1, 3, 12, 0, 0, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parents[1]


def iso(dt):
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def event(run, seq, status="ok", reason=None):
    dets = [{"class_id": 0, "class_name": "person", "score": 0.8, "bbox_xyxy": [1, 1, 9, 9], "zone": "accepted"},
            {"class_id": 0, "class_name": "person", "score": 0.3, "bbox_xyxy": [2, 2, 8, 8], "zone": "low"}] \
        if status == "ok" else []
    return {"event_id": f"evt-{run}-f{seq:06d}", "run_id": run, "frame_id": f"{run}-f{seq:06d}", "seq": seq,
            "timestamp": f"2026-01-01T00:00:{seq:02d}.100Z", "captured_at": f"2026-01-01T00:00:{seq:02d}.000Z",
            "source_id": "src", "model_ref": "yolo11n@sha256:abc", "config_ref": "cfg-p", "status": status,
            "thresholds": {"lower": 0.5, "upper": 0.65}, "detections": dets, "n_detections": len(dets),
            "zone_counts": {"accepted": 1, "doubt": 0, "low": 1} if dets else {},
            "low_ratio": 0.5 if dets else None, "timings": {"inference_ms": 100.0 + seq} if dets else {},
            "image_ref": f"datos/runs/{run}/imagenes/{seq}.jpg" if reason else None, "capture_reason": reason,
            "error": None if dets else "la fuente no entregó frame"}


def window(run, idx, state):
    return {"window_id": f"win-{run}-{idx:04d}", "run_id": run, "idx": idx, "config_ref": "cfg-p",
            "first_event_id": "a", "last_event_id": "b", "start": "s", "end": "e", "n_frames": 2, "n_valid": 2,
            "n_eligible": 4, "n_low": 2, "low_ratio": 0.5 if state in ("normal", "fuera_de_rango") else None,
            "mean_score": 0.55, "state": state, "conditions": {}, "event_ids": [], "image_refs": []}


def alert(run, rule, sev, ts="2026-01-01T00:01:00.000Z"):
    return {"type": "alert", "alert_id": f"alert-{run}-{rule}", "rule_id": rule, "run_id": run, "timestamp": ts,
            "severity": sev, "status": "open", "reason": "r", "suggested_action": "a", "config_ref": "cfg-p",
            "event_ids": []}


def review(run, target_type, target_id, action, n, **kw):
    return {"review_id": f"rev-{run}-{n}", "request_id": f"req-{run}-{n}", "at": f"2026-01-02T00:00:0{n}Z",
            "run_id": run, "target_type": target_type, "target_id": target_id, "action": action,
            "operator": "Prueba", "note": "", **kw}


def write_run(runs_dir, run, mode, started, finished, events, windows=(), alerts=(), reviews=()):
    d = runs_dir / run
    d.mkdir(parents=True)
    w = lambda name, recs: (d / name).write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    w("events.jsonl", events)
    w("windows.jsonl", windows)
    w("alerts.jsonl", alerts)
    if reviews:
        w("revisiones.jsonl", reviews)
    (d / "manifest.json").write_text(json.dumps({
        "run_id": run, "label": run.split("-", 1)[1], "mode": mode, "started_at": started, "finished_at": finished,
        "config_ref": "cfg-p", "config": {"policy": {"lower": 0.5, "upper": 0.65}}, "model_ref": "yolo11n@sha256:abc"}),
        encoding="utf-8")
    return d


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Cinco ejecuciones que activan cada regla de atención, más su vista en vivo."""
    monkeypatch.chdir(tmp_path)
    runs = tmp_path / "datos" / "runs"
    a, b, c, e, f = ("20260101T000000Z-vivo-ok", "20260101T060000Z-replay", "20260102T000000Z-sin-cerrar",
                     "20260102T060000Z-estancada", "20260103T000000Z-caida")
    write_run(runs, a, "live", "2026-01-01T00:00:00.000Z", "2026-01-01T00:10:00.000Z",
              [event(a, 1, reason="baja_confianza"), event(a, 2), event(a, 3, reason="muestra_normal"),
               event(a, 4, "invalid_input"), event(a, 1, reason="baja_confianza")],        # reenvío duplicado
              [window(a, 1, "normal"), window(a, 2, "fuera_de_rango")],
              [alert(a, "baja-confianza-v1", "warning"), alert(a, "fuente-interrumpida-v1", "critical"),
               {"type": "update", "alert_id": f"alert-{a}-fuente-interrumpida-v1", "status": "resolved",
                "resolved_at": "2026-01-01T00:05:00.000Z"}],
              [review(a, "event", f"evt-{a}-f000003", "revisar", 1, label="sin_problema")])
    write_run(runs, b, "replay", "2026-01-01T06:00:00.000Z", "2026-01-01T06:10:00.000Z",
              [event(b, 1, reason="baja_confianza"), event(b, 2)], [window(b, 1, "insuficiente")],
              [alert(b, "cambio-sostenido-v1", "critical", "2026-01-01T06:05:00.000Z")],
              [review(b, "alert", f"alert-{b}-cambio-sostenido-v1", "reconocer", 1),
               review(b, "event", f"evt-{b}-f000001", "revisar", 2, label="clima")])
    write_run(runs, c, "replay", "2026-01-02T00:00:00.000Z", None, [event(c, 1)])
    write_run(runs, e, "live", "2026-01-02T06:00:00.000Z", None, [event(e, 1)])
    write_run(runs, f, "live", "2026-01-03T00:00:00.000Z", None, [event(f, 1), event(f, 2, "invalid_input")])
    db = tmp_path / "datos" / "monitor.db"
    st = Store(db)
    for d in sorted(runs.iterdir()):
        st.ingest_run_dir(d)
    st.close()
    vivo = tmp_path / "datos" / "vivo"
    base = {"interval_s": 2.0, "terminada": False, "mode": "live", "model_ref_configurado": "m",
            "thresholds": {"lower": 0.5, "upper": 0.65}, "contadores": {}}
    for rid, st_ in ((e, {"updated_at": iso(NOW - timedelta(minutes=5)), "evento": {"status": "ok"},
                          "frame": {"captured_at": iso(NOW - timedelta(minutes=5))}}),
                     (f, {"updated_at": iso(NOW - timedelta(seconds=1)), "evento": {"status": "invalid_input", "seq": 2},
                          "frame": {"captured_at": iso(NOW - timedelta(seconds=30)), "seq": 1}})):
        (vivo / rid).mkdir(parents=True)
        (vivo / rid / "estado.json").write_text(json.dumps({**base, **st_, "run_id": rid}), encoding="utf-8")
    return {"root": tmp_path, "db": db, "runs": runs, "vivo": vivo, "ids": (a, b, c, e, f)}


def build(w, **params):
    con = panel.connect(w["db"])
    try:
        return panel.build(con, panel.live_states(w["vivo"]), panel.PanelFilter.from_params(params), NOW,
                           runs_dir=w["runs"])
    finally:
        con.close()


def test_kpis_and_rows_from_real_records(world):
    d = build(world)
    k = d["kpis"]
    assert k["ejecuciones"] == {"n": 5, "live": 3, "replay": 2, "por_estado": {
        "en_curso": 0, "fuente_caida": 1, "estancada": 1, "sin_cerrar": 1, "terminada": 2}}
    assert k["eventos"]["n"] == 10 and k["eventos"]["validos"] == 8 and k["eventos"]["sin_frame"] == 2
    assert k["eventos"]["pct_validos"] == 0.8 and k["eventos"]["capturas"] == 3
    assert k["ventanas"]["evaluadas"] == 2 and k["ventanas"]["pct_fuera_de_rango"] == 0.5
    assert k["alertas"] == {"n": 3, "abiertas": 2, "resueltas": 1,
                            "revision": {"pendiente": 2, "reconocida": 1, "revisada": 0}}
    assert k["capturas"] == {"n": 3, "revisadas": 2, "sin_revisar_baja_confianza": 1}
    # mediana con interpolación (101..103 ms en los eventos válidos con tiempo), igual que numpy
    assert k["inferencia_ms"]["n"] == 8 and k["inferencia_ms"]["p50"] == 101.0
    row = next(r for r in d["ejecuciones"] if r["run_id"] == world["ids"][0])
    assert row["ventanas"]["fuera_de_rango"] == 1 and row["pct_fuera_de_rango"] == 0.5
    assert row["alertas_abiertas"] == 1 and row["capturas_sin_revisar"] == 1 and row["registro_primario"] is True
    sin_ventanas = next(r for r in d["ejecuciones"] if r["run_id"] == world["ids"][2])
    assert sin_ventanas["pct_fuera_de_rango"] is None and sin_ventanas["n_ventanas"] == 0     # ausencia, no 0 %


def test_attention_rules_coordinate_alert_review_and_run_states(world):
    a, b, c, e, f = world["ids"]
    d = build(world)
    got = {(x["regla"], x["run_id"]) for x in d["atencion"]}
    # la captura de baja confianza de b ya está revisada (F2): solo a genera C1
    assert got == {("A1", a), ("A2", a), ("C1", a), ("A3", b), ("E3", c), ("E2", e), ("E1", f)}
    levels = [panel.LEVELS[x["nivel"]] for x in d["atencion"]]
    assert levels == sorted(levels) and d["atencion"][0]["nivel"] == "critico"
    a1 = next(x for x in d["atencion"] if x["regla"] == "A1")
    assert a1["nivel"] == "aviso" and a1["href"] == f"/alertas/alert-{a}-baja-confianza-v1"   # warning -> aviso
    assert next(x for x in d["atencion"] if x["regla"] == "E2")["href"] == f"/vivo?run_id={e}"
    assert d["atencion_por_nivel"] == {"critico": 2, "aviso": 3, "seguimiento": 2}
    # cerrar la revisión de la alerta resuelta y reconocer la abierta saca A2 y convierte A1 en A3
    with open(world["runs"] / a / "revisiones.jsonl", "a", encoding="utf-8") as fh:
        for n, (tid, act, kw) in enumerate(((f"alert-{a}-fuente-interrumpida-v1", "cerrar", {"verdict": "esperada"}),
                                            (f"alert-{a}-baja-confianza-v1", "reconocer", {})), start=5):
            fh.write(json.dumps(review(a, "alert", tid, act, n, **kw)) + "\n")
    st = Store(world["db"])
    st.ingest_run_dir(world["runs"] / a)
    st.close()
    rules = {(x["regla"], x["run_id"]) for x in build(world)["atencion"]}
    assert ("A2", a) not in rules and ("A1", a) not in rules and ("A3", a) in rules


def test_run_state_prefers_persisted_summary():
    run = {"finished_at": None}
    assert panel.run_state({"finished_at": "t"}, {"terminada": False}, NOW)["state"] == "terminada"
    assert panel.run_state(run, None, NOW)["state"] == "sin_cerrar"
    done_live = {"terminada": True, "updated_at": iso(NOW)}
    assert "falta el resumen final" in panel.run_state(run, done_live, NOW)["motivo"]
    fresh = {"updated_at": iso(NOW), "evento": {"status": "ok"}, "frame": {"captured_at": iso(NOW)}}
    assert panel.run_state(run, fresh, NOW)["state"] == "en_curso"


def test_filters_and_validation(world):
    a, b, c, e, f = world["ids"]
    assert {r["run_id"] for r in build(world, mode="live")["ejecuciones"]} == {a, e, f}
    # una fecha sola cubre el día completo en until
    assert {r["run_id"] for r in build(world, since="2026-01-01", until="2026-01-01")["ejecuciones"]} == {a, b}
    assert {r["run_id"] for r in build(world, since="2026-01-02T03:00:00Z")["ejecuciones"]} == {e, f}
    empty = build(world, since="2030-01-01")
    assert empty["ejecuciones"] == [] and empty["kpis"]["eventos"]["pct_validos"] is None and empty["atencion"] == []
    assert build(world, mode="live")["filtros"] == {"mode": "live"}
    for bad, msg in (({"mode": "x"}, "Modo inválido"), ({"since": "ayer"}, "ISO 8601"),
                     ({"since": "2026-01-02", "until": "2026-01-01"}, "invertido"), ({"run": "x"}, "desconocidos")):
        with pytest.raises(panel.PanelError) as ei:
            panel.PanelFilter.from_params(bad)
        assert ei.value.status == 422 and msg in ei.value.message


def test_verify_against_primary_records(world):
    d = build(world)
    assert panel.verify(d, world["runs"]) == []
    a = world["ids"][0]
    # un registro primario que cambia sin reindexar: la verificación lo señala con la ejecución y el campo
    with open(world["runs"] / a / "events.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event(a, 9)) + "\n")
    diffs = panel.verify(d, world["runs"])
    assert diffs == [f"{a} · eventos: panel 4 ≠ JSONL 5", f"{a} · validos: panel 3 ≠ JSONL 4"]
    # una ejecución indexada cuya carpeta ya no existe: regla E4 y verificación fallida
    shutil.rmtree(world["runs"] / world["ids"][1])
    d2 = build(world)
    assert any(x["regla"] == "E4" and x["run_id"] == world["ids"][1] for x in d2["atencion"])
    assert any("sin registro primario" in x for x in panel.verify(d2, world["runs"]))


def test_portal_panel_views_and_errors(world):
    f = world["vivo"] / world["ids"][4] / "estado.json"          # el portal usa el reloj real
    now = datetime.now(timezone.utc)
    f.write_text(json.dumps({**json.loads(f.read_text(encoding="utf-8")), "updated_at": iso(now)}), encoding="utf-8")
    c = TestClient(create_app(world["db"], root=world["root"]))
    assert c.get("/", follow_redirects=False).headers["location"] == "/panel"
    page = c.get("/panel")
    assert page.status_code == 200 and "Requiere atención" in page.text and "fuente caída" in page.text
    assert f'href="/vivo?run_id={world["ids"][4]}"' in page.text and "Alertas por regla" in page.text
    assert 'http-equiv="refresh"' in page.text          # hay una ejecución con la fuente caída: se actualiza sola
    api = c.get("/api/panel?mode=replay").json()
    assert api["filtros"] == {"mode": "replay"} and len(api["ejecuciones"]) == 2
    assert 'http-equiv="refresh"' not in c.get("/panel?mode=replay").text
    bad = c.get("/panel?since=ayer&mode=live")
    assert bad.status_code == 422 and "ISO 8601" in bad.text and 'value="ayer"' in bad.text   # conserva el filtro
    assert c.get("/api/panel?mode=x").status_code == 422
    assert "Ninguna ejecución coincide" in c.get("/panel?since=2030-01-01").text


def test_portal_panel_without_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Store(tmp_path / "datos" / "monitor.db").close()
    c = TestClient(create_app(tmp_path / "datos" / "monitor.db", root=tmp_path))
    assert "No hay ejecuciones registradas" in c.get("/panel").text
    assert c.get("/api/panel").json()["kpis"]["ejecuciones"]["n"] == 0


def test_cli_verificar(world):
    cmd = [sys.executable, "-m", "monitor.dashboard", "--db", str(world["db"]), "--runs-dir", str(world["runs"]),
           "--verificar"]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}     # en Windows, un pipe usa cp1252 por defecto
    ok = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=REPO, env=env)
    assert ok.returncode == 0 and "coincide" in ok.stdout, ok.stderr
    (world["runs"] / world["ids"][2] / "events.jsonl").write_text("", encoding="utf-8")
    bad = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=REPO, env=env)
    assert bad.returncode == 1 and "NO coincide" in bad.stdout
