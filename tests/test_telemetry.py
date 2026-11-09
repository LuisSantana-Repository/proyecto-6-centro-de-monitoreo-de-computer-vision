"""Escenario S4: pérdida de telemetría (índice bloqueado o registro primario sin espacio) sin pérdida de datos."""

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from monitor.config import load_config
from monitor.events import InferenceEvent
from monitor.portal.app import create_app
from monitor.store import Store
from monitor.telemetry import BufferedLog, IndexSink, SinkStatus, TelemetryFault, TelemetryMonitor

REPO = Path(__file__).resolve().parents[1]
RULE = {"rule_id": "telemetria-perdida-v1", "consecutive_failures": 2, "recover_after_ok": 3, "severity": "critical",
        "max_buffer": 1000, "suggested_action": "revisar"}


def ev(i, run="r"):
    t = f"2026-01-01T00:00:{i:02d}.000Z"
    return InferenceEvent(event_id=f"evt-{run}-f{i:06d}", run_id=run, frame_id=f"f{i}", seq=i, timestamp=t,
                          captured_at=t, source_id="s", model_ref="m", config_ref="c",
                          thresholds={"lower": 0.5, "upper": 0.65}, status="no_detections", detections=[],
                          n_detections=0)


def test_buffered_log_keeps_order_and_counts_losses(tmp_path):
    clock = {"t": 0.0}
    st = SinkStatus()
    log = BufferedLog(tmp_path / "e.jsonl", st, TelemetryFault("registro", 10, 10), lambda: clock["t"], max_buffer=3)
    log.write({"n": 1})
    clock["t"] = 10.0                                            # disco lleno
    for n in (2, 3, 4):
        st.reset()
        log.write({"n": n})
    assert st.failed and "ENOSPC" not in st.error and "espacio" in st.error and len(log.pending) == 3
    st.reset()
    log.write({"n": 5})                                          # excede max_buffer: se pierde la más antigua (2)
    assert st.lost == 1 and [json.loads(x)["n"] for x in log.pending] == [3, 4, 5]
    clock["t"] = 20.0                                            # se recupera: escribe lo atrasado en orden
    st.reset()
    log.flush()
    log.close()
    assert [json.loads(l)["n"] for l in open(tmp_path / "e.jsonl", encoding="utf-8")] == [1, 3, 4, 5]
    assert not log.pending and st.written == 4


def test_index_sink_survives_a_real_exclusive_lock(tmp_path):
    db = tmp_path / "m.db"
    store = Store(db, timeout=0.05)
    store.upsert_config("c", {})
    store.upsert_run({"run_id": "r", "config_ref": "c", "model_ref": "m"})
    st = SinkStatus()
    sink = IndexSink(store, st)
    fault = TelemetryFault("indice", 2, 4)
    for i in range(1, 9):
        st.reset()
        fault.drive_index_lock(str(db), float(i))               # candado real de otra conexión en t = 2..5
        sink.flush()
        sink.do("insert_event", ev(i))
        if 2 <= i < 6:
            assert st.failed and "locked" in st.error and len(sink.queue) == i - 1
    assert not sink.queue                                        # al liberarse, la cola se aplicó en orden
    rows = [r[0] for r in sqlite3.connect(db).execute("SELECT seq FROM events ORDER BY seq")]
    assert rows == list(range(1, 9))
    sink.do("insert_event", ev(3))                               # reintentar es seguro: INSERT OR IGNORE
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM events").fetchone()[0] == 8
    store.close()


def obs(i, idx=(True, 0), reg=(True, 0)):
    mk = lambda ok, p: {"ok": ok, "pendientes": p, "error": None if ok else "OperationalError: database is locked",
                        "perdidos": 0}
    return {"seq": i, "at": f"2026-01-01T00:00:{2 * i:02d}.000Z", "indice": mk(*idx), "registro": mk(*reg)}


def test_rule_opens_per_sink_waits_for_backlog_and_resolves():
    mon = TelemetryMonitor({"telemetry_health": RULE}, "c", "r")
    seq = [obs(1), obs(2, idx=(False, 1)), obs(3, idx=(False, 2)), obs(4, idx=(False, 3)),
           obs(5, idx=(True, 2)),                                # escribe otra vez, pero aún vacía atrasos
           obs(6), obs(7), obs(8), obs(9)]
    alerts, updates = [], []
    for o in seq:
        a, u = mon.add(o)
        alerts += a; updates += u
    (a,) = alerts
    assert a["alert_id"] == "alert-r-telemetria-perdida-v1-indice-f000002" and a["metric"]["sink"] == "indice"
    assert a["event_ids"] == ["evt-r-f000002", "evt-r-f000003"]
    (u,) = updates
    assert u["sink"] == "indice" and u["n_failed"] == 3 and u["max_pending"] == 3 and u["lost"] == 0
    assert u["recovered_at_seq"] == 6 and u["duration_s"] == 8.0 and u["resolved_by_window"] == "telemetria-f000008"
    # un fallo aislado no abre alerta; los dos destinos son independientes
    mon2 = TelemetryMonitor({"telemetry_health": RULE}, "c", "r")
    out = [mon2.add(o) for o in (obs(1, reg=(False, 1)), obs(2), obs(3, reg=(False, 1)), obs(4, idx=(False, 1)))]
    assert all(a == [] for a, _ in out)
    assert TelemetryMonitor({}, "c", "r").add(obs(1, idx=(False, 1))) == ([], [])


def test_fault_parsing_and_config_validation(tmp_path):
    assert TelemetryFault.parse("indice:150:60").as_dict() == {"sink": "indice", "start_s": 150.0, "duration_s": 60.0}
    for bad in ("indice", "otro:1:2", "registro:a:b"):
        with pytest.raises(ValueError):
            TelemetryFault.parse(bad)
    p = tmp_path / "c.yaml"
    p.write_text(f"base: {(REPO / 'config' / 'escenario-telemetria.yaml').as_posix()}\n"
                 "telemetry_health:\n  index_timeout_s: 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="telemetry_health"):
        load_config(p)


WEIGHTS = REPO / "modelo" / "yolo11n.pt"


@pytest.mark.skipif(not WEIGHTS.exists(), reason="faltan los pesos (monitor.download_model)")
@pytest.mark.parametrize("sink", ["indice", "registro"])
def test_run_end_to_end_loses_nothing(tmp_path, sink):
    """monitor.run real (10 frames, fallo en las muestras 3-5): registro e índice completos, alerta resuelta."""
    replay = tmp_path / "replay"
    replay.mkdir()
    for f in sorted((REPO / "evidencias" / "entradas" / "c1-normal").glob("*.jpg"))[:10]:
        shutil.copy(f, replay / f.name)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f"base: {(REPO / 'config' / 'escenario-telemetria.yaml').as_posix()}\noutput:\n"
                   f"  runs_dir: {(tmp_path / 'runs').as_posix()}\n  db: {(tmp_path / 'm.db').as_posix()}\n",
                   encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "monitor.run", "--config", str(cfg), "--replay", str(replay), "--no-pace",
                        "--sin-vivo", "--inject-telemetry-loss", f"{sink}:4:6", "--label", f"s4-{sink}"],
                       cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    (d,) = list((tmp_path / "runs").iterdir())
    assert [json.loads(l)["seq"] for l in open(d / "events.jsonl", encoding="utf-8")] == list(range(1, 11))
    tel = [json.loads(l) for l in open(d / "telemetria.jsonl", encoding="utf-8")]
    assert [t["seq"] for t in tel] == list(range(1, 11))
    assert [t["seq"] for t in tel if not t[sink]["ok"]] == [3, 4, 5]
    lines = [json.loads(l) for l in open(d / "alerts.jsonl", encoding="utf-8")]
    assert [x["type"] for x in lines] == ["alert", "update"] and lines[1]["sink"] == sink and lines[1]["lost"] == 0
    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    assert m["telemetry"]["registro_perdidas"] == 0 and m["telemetry"]["indice_pendientes_al_cerrar"] == 0
    con = sqlite3.connect(tmp_path / "m.db")
    assert [x[0] for x in con.execute("SELECT seq FROM events WHERE run_id = ? ORDER BY seq", (d.name,))] == list(range(1, 11))
    assert con.execute("SELECT status FROM alerts WHERE run_id = ?", (d.name,)).fetchone()[0] == "resolved"
    con.close()
    ev_ = subprocess.run([sys.executable, "-m", "monitor.evaluate", str(d)], cwd=REPO, capture_output=True, text=True,
                         encoding="utf-8")
    assert ev_.returncode == 0 and "coinciden=True" in ev_.stdout
    page = TestClient(create_app(tmp_path / "m.db", root=tmp_path, runs_dir="runs")).get(f"/alertas/{lines[0]['alert_id']}")
    assert f"destino <b>{sink}</b>" in page.text and "0 perdidos" in page.text


def test_portal_answers_503_while_the_index_is_locked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "datos" / "monitor.db"
    Store(db).close()
    c = TestClient(create_app(db, root=tmp_path))
    lock = sqlite3.connect(db, isolation_level=None)
    lock.execute("BEGIN EXCLUSIVE")                              # otro proceso tiene la base
    try:
        page = c.get("/eventos")
        assert page.status_code == 503 and "bloqueado por otro proceso" in page.text and "/vivo" in page.text
        api = c.get("/api/eventos")
        assert api.status_code == 503 and api.headers["retry-after"] == "5" and "bloqueado" in api.json()["detail"]
        assert c.get("/vivo").status_code == 200                 # la vista en vivo no lee el índice
    finally:
        lock.execute("ROLLBACK")
        lock.close()
    assert c.get("/eventos").status_code == 200
