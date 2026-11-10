"""Extra F7 · varias fuentes: referencia por fuente, protección, verificación, contrafactual y portal."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from monitor import history as hist
from monitor import sources as src
from monitor.dashboard import connect
from monitor.portal.app import create_app
from monitor.store import Store

REPO = Path(__file__).resolve().parents[1]
S1, S2 = "cam-01", "cam-02"


def rule(signal, op, th, rid):
    return {"rule_id": rid, "signal": signal, "op": op, "threshold": th, "consecutive": 1, "severity": "warning",
            "suggested_action": "a"}


def cfg(source, ref, low=0.9, mean=0.2):
    return {"source": {"id": source, "url": f"https://example.invalid/{source}", "sample_interval_s": 2.0},
            "reference": {"stats_file": ref}, "policy": {"lower": 0.5, "upper": 0.65, "analysis_floor": 0.25},
            "windows": {"size_frames": 4, "min_valid_frames": 3, "min_eligible": 8},
            "alerts": [rule("low_ratio", ">", low, "baja-confianza-v1"), rule("mean_score", "<", mean, "cambio-v1")]}


def event(run, seq, source, scores):
    dets = [{"class_id": 0, "class_name": "person", "score": s, "bbox_xyxy": [0, 0, 1, 1],
             "zone": "low" if s < 0.5 else "accepted"} for s in scores]
    t = f"2026-01-01T00:00:{seq:02d}.000Z"
    return {"event_id": f"evt-{run}-f{seq:06d}", "run_id": run, "frame_id": f"{run}-f{seq:06d}", "seq": seq,
            "timestamp": t, "captured_at": t, "source_id": source, "model_ref": "m", "config_ref": "cfg-x",
            "status": "ok", "thresholds": {"lower": 0.5, "upper": 0.65}, "detections": dets, "n_detections": len(dets),
            "timings": {"inference_ms": 100.0}}


def write_run(runs, run, conf, mode="live", source_suffix="", scores=(0.3, 0.3, 0.3, 0.7)):
    from monitor.alerts import WindowEvaluator
    from monitor.events import InferenceEvent

    d = runs / run
    d.mkdir(parents=True)
    evs = [event(run, s, conf["source"]["id"] + source_suffix, list(scores)) for s in range(1, 9)]
    ev = WindowEvaluator(conf, "cfg-x", run)
    wins, alerts = [], []
    for e in evs:
        w, a, _ = ev.add(InferenceEvent.model_validate(e))
        wins += [w.record()] if w else []
        alerts += [{"type": "alert", **x} for x in a]
    w = lambda name, recs: (d / name).write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    w("events.jsonl", evs); w("windows.jsonl", wins); w("alerts.jsonl", alerts)
    (d / "manifest.json").write_text(json.dumps({"run_id": run, "label": run.split("-", 1)[1], "mode": mode,
                                                 "started_at": "2026-01-01T00:00:00.000Z", "config_ref": "cfg-x",
                                                 "model_ref": "m", "config": conf}), encoding="utf-8")
    return d


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    for name, sid, low in (("ref-01.json", S1, 0.9), ("ref-02.json", S2, 0.95)):
        (tmp_path / "config" / name).write_text(json.dumps({"source_id": sid, "derived_thresholds": {
            "low_ratio_gt (mean + 2 sd)": low, "mean_score_lt (mean - 2 sd)": 0.2}}), encoding="utf-8")
    c1, c2 = cfg(S1, "config/ref-01.json", 0.9), cfg(S2, "config/ref-02.json", 0.95)
    runs = tmp_path / "datos" / "runs"
    write_run(runs, "20260101T000000Z-cam1", c1)
    write_run(runs, "20260101T010000Z-cam2", c2, mode="replay", source_suffix="-replay", scores=(0.3, 0.3, 0.3, 0.3))
    db = tmp_path / "datos" / "monitor.db"
    st = Store(db)
    for d in sorted(runs.iterdir()):
        st.ingest_run_dir(d)
    st.close()
    return {"root": tmp_path, "runs": runs, "db": db, "c1": c1, "c2": c2}


def test_base_source_and_reference_guard(world):
    assert src.base_source("x-02-replay") == "x-02" and src.base_source("x-02") == "x-02"
    src.check_reference(world["c2"])                                          # misma fuente: arranca
    with pytest.raises(SystemExit, match="es de la fuente cam-01"):
        src.check_reference({**world["c2"], "reference": {"stats_file": "config/ref-01.json"}})
    src.check_reference({**world["c2"], "reference": {"stats_file": "config/no-existe.json"}})   # sin referencia aún


def test_verify_detects_each_inconsistency(world):
    per, problems = src.verify(world["runs"])
    assert per == {S1: 1, S2: 1} and problems == []
    bad = world["runs"] / "20260101T000000Z-cam1"
    m = json.loads((bad / "manifest.json").read_text(encoding="utf-8"))
    m["config"]["alerts"][0]["threshold"] = 0.95                              # umbral que no sale de su referencia
    m["config"]["reference"]["stats_file"] = "config/ref-02.json"            # y referencia de otra fuente
    (bad / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    msgs = src.verify_run(bad)
    assert any("la referencia es de cam-02" in x for x in msgs)
    m["config"]["reference"]["stats_file"] = "config/ref-01.json"
    (bad / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    assert src.verify_run(bad) == [f"{bad.name}: umbral low_ratio = 0.95, pero la referencia de su fuente deriva 0.9"]
    f = bad / "events.jsonl"
    f.write_text(f.read_text(encoding="utf-8").replace('"source_id": "cam-01"', '"source_id": "cam-03"', 1), encoding="utf-8")
    assert any("eventos de otra fuente ['cam-03']" in x for x in src.verify_run(bad))


def test_cross_thresholds_counterfactual(world):
    run = world["runs"] / "20260101T010000Z-cam2"                           # prop. baja 1.0 en sus ventanas
    own = src.cross(run, world["c2"])
    assert own["fuera_de_rango"] == own["propias"]["fuera_de_rango"] == 2   # con los suyos, igual que lo registrado
    loose = src.cross(run, {**world["c1"], "alerts": [rule("low_ratio", ">", 1.0, "baja-confianza-v1")]})
    assert loose["fuera_de_rango"] == 0 and loose["alertas"] == [] and loose["evaluadas"] == 2


def test_summary_and_portal_filters(world):
    con = connect(world["db"])
    s = {x["source_id"]: x for x in src.summary(con, {}, world["root"])}
    con.close()
    assert set(s) == {S1, S2} and s[S2]["replay"] == 1 and s[S1]["live"] == 1
    assert s[S2]["configs"][0]["reference_source"] == S2 and s[S2]["configs"][0]["thresholds"]["low_ratio"] == 0.95
    c = TestClient(create_app(world["db"], root=world["root"]))
    page = c.get("/fuentes")
    assert page.status_code == 200 and "cam-01" in page.text and "cam-02" in page.text and "/historico?fuente=cam-02" in page.text
    assert c.get("/api/eventos?fuente=cam-02").json()["total"] == 8               # replay: source_id con -replay
    assert c.get("/api/eventos?fuente=cam-01").json()["total"] == 8 and c.get("/api/eventos").json()["total"] == 16
    assert c.get("/api/eventos?fuente=nada").json()["total"] == 0
    al = c.get("/api/alertas?fuente=cam-02").json()["items"]
    assert al and all(a["run_id"].endswith("cam2") for a in al)
    assert 'option value="cam-02"' in c.get("/eventos").text and 'option value="cam-01"' in c.get("/alertas").text
    h = hist.build(hist.from_index(connect(world["db"])), hist.HistoryFilter(fuente=S2))
    assert [r["run_id"] for r in h["ejecuciones"]] == ["20260101T010000Z-cam2"]
    assert "Ninguna ejecución coincide" not in c.get("/historico?fuente=cam-02").text


def test_reference_records_its_source_and_rejects_mixtures(world, tmp_path):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    cmd = [sys.executable, "-m", "monitor.reference", "--config", str(REPO / "config" / "monitor.yaml")]
    # ejecuciones con ventanas de 60: se escriben de nuevo con más eventos para que haya ventanas suficientes
    out = tmp_path / "ref.json"
    one = world["runs"] / "20260101T010000Z-cam2"
    ev = [json.loads(l) for l in open(one / "events.jsonl", encoding="utf-8")]
    big = tmp_path / "big"
    big.mkdir()
    (big / "manifest.json").write_text((one / "manifest.json").read_text(encoding="utf-8"), encoding="utf-8")
    (big / "events.jsonl").write_text("".join(json.dumps({**ev[0], "seq": i, "event_id": f"e{i}",
                                                          "detections": ev[0]["detections"] * 5}) + "\n"
                                              for i in range(1, 61)), encoding="utf-8")
    r = subprocess.run(cmd + [str(big), "--out", str(out)], cwd=REPO, capture_output=True, text=True, encoding="utf-8", env=env)
    assert r.returncode == 0, r.stderr[-1500:]
    assert json.loads(out.read_text(encoding="utf-8"))["source_id"] == S2      # el sufijo -replay se quita
    mixed = subprocess.run(cmd + [str(big), str(world["runs"] / "20260101T000000Z-cam1")], cwd=REPO,
                           capture_output=True, text=True, encoding="utf-8", env=env)
    assert mixed.returncode != 0 and "mezclan fuentes" in mixed.stderr


def test_run_refuses_a_reference_of_another_source(tmp_path):
    c = tmp_path / "c.yaml"
    c.write_text(f"base: {(REPO / 'config' / 'fuente-02.yaml').as_posix()}\n"
                 "reference:\n  stats_file: config/referencia.json\n", encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "monitor.run", "--config", str(c), "--frames", "1"], cwd=REPO,
                       capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                       timeout=120)
    assert r.returncode != 0 and "es de la fuente shibuya-scramble-01" in r.stderr
