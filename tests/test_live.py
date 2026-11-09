"""F4 · vista en vivo: estado publicado por el monitor, clasificación, vistas del portal e integración con run."""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from monitor import live
from monitor.events import Detection, InferenceEvent
from monitor.portal.app import create_app
from monitor.store import Store

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc)
MANIFEST = {"run_id": "20260101T000000Z-vivo", "label": "vivo", "mode": "replay", "started_at": "2026-01-01T00:00:00Z",
            "config_ref": "cfg", "model_ref": "yolo11n@sha256:abc",
            "config": {"policy": {"analysis_floor": 0.25, "lower": 0.5, "upper": 0.65},
                       "windows": {"size_frames": 60}, "source": {"sample_interval_s": 2.0}}}


def iso(dt):
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ev(seq, status="ok", model="yolo11n@sha256:abc"):
    dets = [Detection(class_id=0, class_name="person", score=s, bbox_xyxy=[10, 10, 40, 60],
                      zone="accepted" if s >= 0.65 else "doubt" if s >= 0.5 else "low") for s in (0.8, 0.55, 0.3)] \
        if status == "ok" else []
    return InferenceEvent(event_id=f"evt-{MANIFEST['run_id']}-f{seq:06d}", run_id=MANIFEST["run_id"], frame_id="f",
                          seq=seq, timestamp="t", captured_at=iso(NOW - timedelta(seconds=2 * (10 - seq))), source_id="s",
                          model_ref=model, config_ref="cfg", thresholds={"lower": 0.5, "upper": 0.65}, status=status,
                          detections=dets, n_detections=len(dets),
                          zone_counts={"accepted": 1, "doubt": 1, "low": 1} if dets else {},
                          low_ratio=1 / 3 if dets else None, timings={"inference_ms": 100.0} if dets else {},
                          error=None if dets else "la fuente no entregó frame en 3 s")


def st(**kw):
    return {"interval_s": 2.0, "updated_at": iso(NOW - timedelta(seconds=1)), "terminada": False,
            "evento": {"status": "ok"}, "frame": {"captured_at": iso(NOW - timedelta(seconds=1))}, **kw}


def test_classify_states():
    assert live.classify(st(), NOW)["status"] == "en_curso"
    stale = live.classify(st(updated_at=iso(NOW - timedelta(seconds=30))), NOW)
    assert stale["status"] == "estancada" and stale["edad_s"] == 30.0 and stale["plazo_s"] == 10.0
    down = live.classify(st(evento={"status": "invalid_input"}, frame={"captured_at": iso(NOW - timedelta(seconds=40))}), NOW)
    assert down["status"] == "fuente_caida" and down["edad_frame_s"] == 40.0
    assert "ningún frame" in live.classify(st(evento={"status": "invalid_input"}, frame={}), NOW)["motivo"]
    assert live.classify(st(terminada=True, updated_at=iso(NOW - timedelta(hours=1))), NOW)["status"] == "terminada"


def test_writer_publishes_state_frame_and_finish(tmp_path):
    w = live.LiveWriter(tmp_path, MANIFEST, 240)
    img = np.zeros((80, 100, 3), np.uint8)
    w.update(ev(1), img, [ev(1)], None, {}, {"estado": "conectada"}, {"ok": 1})
    d = tmp_path / MANIFEST["run_id"]
    s1 = json.loads((d / "estado.json").read_text(encoding="utf-8"))
    assert (d / "frame.jpg").stat().st_size > 0 and s1["frame"]["seq"] == 1 and s1["ventana_en_curso"]["n"] == 1
    assert s1["ventana_en_curso"]["low_ratio"] == round(1 / 3, 4) and s1["frames_objetivo"] == 240
    w.update(ev(2, "invalid_input"), None, [ev(1), ev(2, "invalid_input")], None,
             {"a": {"alert_id": "a", "rule_id": "fuente-interrumpida-v1", "severity": "critical", "timestamp": "t",
                    "reason": "r"}}, {"estado": "con_fallas", "ultimo": "desconectado"}, {"ok": 1, "invalid_input": 1})
    s2 = json.loads((d / "estado.json").read_text(encoding="utf-8"))
    assert s2["evento"]["status"] == "invalid_input" and s2["frame"]["seq"] == 1      # conserva el último frame
    assert s2["alertas_abiertas"][0]["rule_id"] == "fuente-interrumpida-v1" and s2["fuente"]["estado"] == "con_fallas"
    w.finish({"ok": 1, "invalid_input": 1}, "2026-01-01T00:08:00Z")
    s3 = json.loads((d / "estado.json").read_text(encoding="utf-8"))
    assert s3["terminada"] and live.classify(s3)["status"] == "terminada" and not list(d.glob("*.tmp"))


def test_write_failures_never_stop_the_monitor(tmp_path, monkeypatch):
    w = live.LiveWriter(tmp_path, MANIFEST, None)

    def boom(*a, **k):
        raise PermissionError("bloqueado")

    monkeypatch.setattr(live, "_atomic_write", boom)
    w.update(ev(1), np.zeros((8, 8, 3), np.uint8), [ev(1)], None, {}, {}, {})
    w.finish({}, "t")
    assert w.write_errors == 2


@pytest.fixture
def portal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "datos" / "monitor.db"
    Store(db).close()
    return tmp_path, TestClient(create_app(db, root=tmp_path))


def test_portal_empty_state(portal):
    _, c = portal
    assert "No hay ejecuciones con vista en vivo" in c.get("/vivo").text
    assert c.get("/api/vivo").json() == {"items": []}


def test_portal_live_views_and_errors(portal):
    root, c = portal
    w = live.LiveWriter(root / "datos" / "vivo", MANIFEST, 240)
    w.update(ev(9), np.full((80, 100, 3), 90, np.uint8), [ev(9)], {"window_id": "w", "idx": 1, "state": "normal",
             "low_ratio": 0.6, "mean_score": 0.49, "n_valid": 60, "n_eligible": 900}, {}, {"estado": "conectada"}, {"ok": 9})
    run = MANIFEST["run_id"]
    page = c.get("/vivo").text
    assert "En curso" in page and f"/vivo/frame/{run}?v=9" in page and f"/eventos/evt-{run}-f000009" in page
    panel = c.get(f"/vivo/panel/{run}")
    assert panel.status_code == 200 and 'data-frame-seq="9"' in panel.text and "Última ventana" in panel.text
    assert panel.headers["cache-control"] == "no-store"
    f = c.get(f"/vivo/frame/{run}")
    assert f.status_code == 200 and f.headers["content-type"] == "image/jpeg"
    assert c.get("/api/vivo").json()["items"][0]["status"] == "en_curso"
    assert c.get(f"/api/vivo/{run}").json()["evento"]["seq"] == 9
    # modelo distinto del configurado (S2) se señala en vivo
    w.update(ev(10, model="yolo11s@sha256:def"), None, [ev(9)], None, {}, {}, {})
    assert "distinto del configurado" in c.get(f"/vivo/panel/{run}").text
    for bad in ("no-existe", "%2E%2E", "a.b"):          # ".." codificado: el cliente no lo normaliza
        assert c.get(f"/api/vivo/{bad}").status_code == 404
        assert c.get(f"/vivo/frame/{bad}").status_code == 404
    assert c.get("/vivo?run_id=no-existe").status_code == 404


def test_portal_shows_stalled_and_source_down(portal):
    root, c = portal
    d = root / "datos" / "vivo" / MANIFEST["run_id"]
    d.mkdir(parents=True)
    old = {**st(updated_at="2020-01-01T00:00:00Z"), "run_id": MANIFEST["run_id"], "mode": "live",
           "model_ref_configurado": "m", "thresholds": {"lower": 0.5, "upper": 0.65}, "contadores": {}}
    (d / "estado.json").write_text(json.dumps(old), encoding="utf-8")
    assert "Sin reportes del monitor" in c.get("/vivo").text
    down = {**old, "updated_at": iso(datetime.now(timezone.utc)), "evento": {"status": "invalid_input", "seq": 5,
            "event_id": "e", "captured_at": "t", "model_ref": "m", "error": "sin frame"}}
    (d / "estado.json").write_text(json.dumps(down), encoding="utf-8")
    assert "Fuente caída" in c.get(f"/vivo/panel/{MANIFEST['run_id']}").text


@pytest.mark.skipif(not (REPO / "modelo" / "yolo11n.pt").exists(), reason="faltan los pesos (monitor.download_model)")
def test_run_publishes_live_state_end_to_end(tmp_path):
    """monitor.run real (3 frames de la muestra versionada) -> estado en vivo 'terminada' con su último frame."""
    import shutil

    replay = tmp_path / "replay"
    replay.mkdir()
    for f in sorted((REPO / "evidencias" / "entradas" / "c1-normal").glob("*.jpg"))[:3]:
        shutil.copy(f, replay / f.name)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f"base: {(REPO / 'config' / 'monitor.yaml').as_posix()}\noutput:\n"
                   f"  runs_dir: {(tmp_path / 'datos' / 'runs').as_posix()}\n  db: {(tmp_path / 'datos' / 'm.db').as_posix()}\n",
                   encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "monitor.run", "--config", str(cfg), "--replay", str(replay),
                        "--no-pace", "--label", "vivo-test"], cwd=REPO, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    (d,) = list((tmp_path / "datos" / "vivo").iterdir())
    s = json.loads((d / "estado.json").read_text(encoding="utf-8"))
    assert s["terminada"] and s["evento"]["seq"] == 3 and s["frame"]["seq"] == 3 and (d / "frame.jpg").exists()
    assert s["contadores"]["ok"] == 3 and s["errores_escritura_vivo"] == 0
    m = json.loads((tmp_path / "datos" / "runs" / d.name / "manifest.json").read_text(encoding="utf-8"))
    assert m["live_view"]["write_errors"] == 0
