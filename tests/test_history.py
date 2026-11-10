"""Extra F6 · gráficas históricas: filtros, valores por ventana, hora de la escena, verificación y portal."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from monitor import history as hist
from monitor.dashboard import PanelError, connect
from monitor.portal.app import create_app
from monitor.store import Store

REPO = Path(__file__).resolve().parents[1]
RULE = {"rule_id": "baja-confianza-v1", "signal": "low_ratio", "op": ">", "threshold": 0.6, "consecutive": 1,
        "severity": "warning", "suggested_action": "a"}
L, R, X = "20260101T000000Z-vivo", "20260102T000000Z-c1-replay", "20260103T000000Z-m4-replay"


def event(run, seq, ms):
    return {"event_id": f"evt-{run}-f{seq:06d}", "run_id": run, "frame_id": f"{run}-f{seq:06d}", "seq": seq,
            "timestamp": f"2026-01-01T00:00:{seq:02d}.100Z", "captured_at": f"2026-01-01T00:00:{seq:02d}.000Z",
            "source_id": "src", "model_ref": "m", "config_ref": "cfg-h", "status": "ok",
            "thresholds": {"lower": 0.5, "upper": 0.65}, "detections": [], "n_detections": 0,
            "timings": {"inference_ms": ms}}


def window(run, idx, state, low, start="s", end="e", n_valid=2):
    return {"window_id": f"win-{run}-{idx:04d}", "run_id": run, "idx": idx, "config_ref": "cfg-h",
            "first_event_id": f"evt-{run}-f{2 * idx - 1:06d}", "last_event_id": f"evt-{run}-f{2 * idx:06d}",
            "start": start, "end": end, "n_frames": 2, "n_valid": n_valid, "n_eligible": 4, "n_low": 2,
            "low_ratio": low, "mean_score": 0.5, "state": state, "conditions": {}, "event_ids": [], "image_refs": []}


def write_run(runs, run, mode, started, replay_of, windows, alerts=()):
    d = runs / run
    d.mkdir(parents=True)
    w = lambda name, recs: (d / name).write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    w("events.jsonl", [event(run, s, 100.0 + 10 * (s - 1)) for s in range(1, 5)])
    w("windows.jsonl", windows)
    w("alerts.jsonl", alerts)
    (d / "manifest.json").write_text(json.dumps({
        "run_id": run, "label": run.split("-", 1)[1], "mode": mode, "started_at": started, "replay_of": replay_of,
        "config_ref": "cfg-h", "model_ref": "m",
        "config": {"source": {"sample_interval_s": 2.0}, "windows": {"size_frames": 2}, "alerts": [RULE],
                   "policy": {"lower": 0.5, "upper": 0.65}}}), encoding="utf-8")


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runs = tmp_path / "datos" / "runs"
    # en vivo: escena exacta (22:00 UTC = 07:00 JST); la ventana 2 cruza el umbral y abre la regla
    write_run(runs, L, "live", "2026-01-01T00:00:00.000Z", None,
              [window(L, 1, "normal", 0.5, "2026-01-01T22:00:00.000Z", "2026-01-01T22:00:02.000Z"),
               window(L, 2, "fuera_de_rango", 0.7, "2026-01-01T22:00:04.000Z", "2026-01-01T22:00:06.000Z")],
              [{"type": "alert", "alert_id": f"alert-{L}-w0002", "rule_id": "baja-confianza-v1", "run_id": L,
                "timestamp": "2026-01-01T22:00:06.000Z", "severity": "warning", "status": "open", "reason": "r",
                "suggested_action": "a", "config_ref": "cfg-h", "event_ids": []}])
    # replay de una grabación que empezó 05:00 UTC (14:00 JST); la ventana 2 es insuficiente
    write_run(runs, R, "replay", "2026-01-02T00:00:00.000Z", "20260101T050000Z-c1",
              [window(R, 1, "normal", 0.55), window(R, 2, "insuficiente", None, n_valid=1)])
    # replay de una grabación sin hora en el nombre; la ventana 2 es incompleta
    write_run(runs, X, "replay", "2026-01-03T00:00:00.000Z", "m4-sin-hora",
              [window(X, 1, "normal", 0.65), window(X, 2, "incompleta", 0.4)])
    db = tmp_path / "datos" / "monitor.db"
    st = Store(db)
    for d in sorted(runs.iterdir()):
        st.ingest_run_dir(d)
    st.close()
    return {"db": db, "runs": runs}


def data(w):
    con = connect(w["db"])
    try:
        return hist.from_index(con)
    finally:
        con.close()


def test_filter_validation_is_422():
    for bad in ({"senal": "fps"}, {"eje": "dia"}, {"modo": "otro"}, {"desde": "ayer"},
                {"desde": "2026-01-03", "hasta": "2026-01-01"}, {"run_id": "x"}):
        with pytest.raises(PanelError) as e:
            hist.HistoryFilter.from_params(bad)
        assert e.value.status == 422
    assert hist.HistoryFilter.from_params({"senal": "", "eje": ""}).senal == "low_ratio"   # vacío = por defecto


def test_values_exclusions_threshold_and_scene_time(world):
    d = hist.build(data(world), hist.HistoryFilter())
    pts = {(p["run_id"], p["idx"]): p for p in d["puntos"]}
    assert set(pts) == {(L, 1), (L, 2), (R, 1), (X, 1)}
    assert d["excluidas"] == {"insuficiente": 1, "incompleta": 1}             # nunca graficadas como 0
    assert [pts[(L, 2)]["cruza_umbral"], pts[(L, 1)]["cruza_umbral"]] == [True, False]
    assert d["umbral"] == {"valor": 0.6, "op": ">", "rule_id": "baja-confianza-v1"}
    assert pts[(L, 1)]["escena_jst"] == "2026-01-02 07:00" and pts[(L, 1)]["hora_tipo"] == "exacta"
    assert pts[(R, 1)]["escena_jst"] == "2026-01-01 14:00" and pts[(R, 1)]["hora_tipo"] == "aproximada"
    assert pts[(R, 1)]["hora_escena"] == round(14 + 1 / 3600, 4)              # muestra media 1.5 -> +1 s
    assert pts[(X, 1)]["hora_escena"] is None
    rows = {r["run_id"]: r for r in d["ejecuciones"]}
    assert [r["n"] for r in d["ejecuciones"]] == [1, 2, 3] and rows[L]["alertas_de_la_regla"] == 1
    assert (rows[L]["con_valor"], rows[L]["min"], rows[L]["max"], rows[L]["media"]) == (2, 0.5, 0.7, 0.6)
    # eje hora: la grabación sin hora queda fuera y se cuenta
    h = hist.build(data(world), hist.HistoryFilter(eje="hora"))
    assert h["n_puntos"] == 3 and h["excluidas"]["sin_hora_de_escena"] == 1


def test_other_signals_and_filters(world):
    dd = data(world)
    inf = {(p["run_id"], p["idx"]): p["value"] for p in hist.build(dd, hist.HistoryFilter(senal="inferencia_p95"))["puntos"]}
    assert inf[(L, 1)] == 110.0 and inf[(L, 2)] == 130.0 and inf[(R, 2)] == 130.0   # p95 por rango más cercano
    val = hist.build(dd, hist.HistoryFilter(senal="validos"))
    assert {(p["run_id"], p["idx"]): p["value"] for p in val["puntos"]}[(R, 2)] == 0.5
    assert val["umbral"] is None                                                  # señal sin regla
    assert [r["run_id"] for r in hist.build(dd, hist.HistoryFilter(modo="replay"))["ejecuciones"]] == [R, X]
    assert [r["run_id"] for r in hist.build(dd, hist.HistoryFilter(etiqueta="C1"))["ejecuciones"]] == [R]
    assert [r["run_id"] for r in hist.build(dd, hist.HistoryFilter(desde="2026-01-02", hasta="2026-01-02"))
            ["ejecuciones"]] == [R]
    assert hist.build(dd, hist.HistoryFilter(etiqueta="zzz"))["ejecuciones"] == []


def test_verify_against_primary_records(world):
    dd = data(world)
    for s in hist.SIGNALS:
        for e in hist.AXES:
            assert hist.verify(dd, world["runs"], hist.HistoryFilter(senal=s, eje=e)) == []
    f = world["runs"] / R / "windows.jsonl"                                        # el índice dice otra cosa
    f.write_text(f.read_text(encoding="utf-8").replace('"low_ratio": 0.55', '"low_ratio": 0.56'), encoding="utf-8")
    assert hist.verify(dd, world["runs"], hist.HistoryFilter()) == [f"{R} ventana 1: índice 0.55 / JSONL 0.56"]
    (world["runs"] / X / "events.jsonl").unlink()
    assert any("sin registro primario" in x for x in hist.verify(dd, world["runs"], hist.HistoryFilter()))


def test_portal_page_api_errors_and_empty_states(world, tmp_path):
    c = TestClient(create_app(world["db"], root=tmp_path))
    page = c.get("/historico?eje=hora")
    assert page.status_code == 200 and "por hora de la escena (JST)" in page.text and f'href="/runs/{L}"' in page.text
    assert "1 replay sin hora de escena" in page.text and "umbral &gt; 0.600" in page.text
    bad = c.get("/historico?senal=fps")
    assert bad.status_code == 422 and "Filtro no válido" in bad.text and "Señal inválida" in bad.text
    assert c.get("/api/historico?desde=ayer").status_code == 422
    assert c.get("/api/historico?senal=validos").json()["n_puntos"] == 5   # 6 ventanas, 1 incompleta
    assert "Ninguna ejecución coincide" in c.get("/historico?etiqueta=zzz").text
    empty = tmp_path / "vacio"
    empty.mkdir()
    Store(empty / "m.db").close()
    assert "No hay ejecuciones registradas" in TestClient(create_app(empty / "m.db", root=empty)).get("/historico").text


def test_cli_verificar(world):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    cmd = [sys.executable, "-m", "monitor.history", "--db", str(world["db"]), "--runs-dir", str(world["runs"]),
           "--verificar"]
    ok = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=REPO, env=env)
    assert ok.returncode == 0 and "coinciden con los registros primarios" in ok.stdout, ok.stderr
    (world["runs"] / L / "windows.jsonl").write_text("", encoding="utf-8")
    bad = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=REPO, env=env)
    assert bad.returncode == 1 and "solo en el índice" in bad.stdout
