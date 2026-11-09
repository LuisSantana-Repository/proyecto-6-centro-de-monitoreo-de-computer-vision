"""Escenario S3: error de inferencia: detección, diagnóstico, reacción (recarga del modelo), recuperación."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from monitor.alerts import InferenceHealthMonitor, WindowEvaluator
from monitor.config import load_config
from monitor.detector import InferenceFault
from monitor.events import Detection, InferenceEvent
from monitor.portal.app import create_app
from monitor.store import Store

REPO = Path(__file__).resolve().parents[1]
RULE = {"rule_id": "inferencia-fallida-v1", "consecutive_errors": 3, "recover_after_ok": 3, "severity": "critical",
        "reload_after": 3, "reload_every": 5, "max_reloads": 3, "suggested_action": "revisar el modelo"}
CFG = {"inference_health": RULE, "policy": {"lower": 0.5, "upper": 0.65, "analysis_floor": 0.25},
       "windows": {"size_frames": 6, "min_valid_frames": 5, "min_eligible": 4}, "alerts": []}
ERR = "RuntimeError: Given groups=1, weight of size [16, 1, 3, 3]"


def ev(i, status="ok"):
    dets = [Detection(class_id=0, class_name="person", score=s, bbox_xyxy=[0, 0, 1, 1], zone="accepted")
            for s in (0.7, 0.8)] if status == "ok" else []
    t = f"2026-01-01T00:{(2 * i) // 60:02d}:{(2 * i) % 60:02d}Z"
    return InferenceEvent(event_id=f"e{i}", run_id="r", frame_id=f"f{i}", seq=i, timestamp=t, captured_at=t,
                          source_id="s", model_ref="m", config_ref="c", thresholds={"lower": 0.5, "upper": 0.65},
                          status=status, detections=dets, n_detections=len(dets),
                          image_ref=f"img{i}.jpg" if status == "inference_error" else None,
                          error=ERR if status == "inference_error" else ("sin frame" if status == "invalid_input" else None))


def drive(pattern, fixes_on_reload=None):
    """'o' inferencia válida, 'x' inference_error, '-' sin frame. fixes_on_reload: tras la recarga N, las 'x'
    siguientes se vuelven 'o' (simula que la recarga arregla el estado del modelo)."""
    mon = InferenceHealthMonitor(CFG, "c", "r")
    out = {"alerts": [], "updates": [], "logs": [], "reloads": []}
    fixed = False
    for i, c in enumerate(pattern, start=1):
        st = {"o": "ok", "x": "inference_error", "-": "invalid_input"}[c]
        if fixed and st == "inference_error":
            st = "ok"
        a, u, logs, want = mon.add(ev(i, st))
        out["alerts"] += a; out["updates"] += u; out["logs"] += logs
        if want:
            mon.note_reload({"type": "recarga_modelo", "seq": i, "resultado": "cargado"})
            out["reloads"].append(i)
            fixed = fixes_on_reload is not None and len(out["reloads"]) >= fixes_on_reload
    return out


def test_reload_fixes_corrupted_model():
    r = drive("ooo" + "x" * 10 + "oooo", fixes_on_reload=1)
    (a,) = r["alerts"]
    assert a["event_ids"] == ["e4", "e5", "e6"] and a["alert_id"].endswith("-f000004")
    dg = a["metric"]["diagnostico"]
    assert dg["etapa"] == "inferencia" and dg["fuente_entrego_frame"] and dg["tipos_de_error"] == {"RuntimeError": 3}
    assert dg["capturas_del_fallo"] == 3 and a["image_refs"] == ["img4.jpg", "img5.jpg", "img6.jpg"]
    assert r["reloads"] == [6]                                   # al tercer error seguido
    assert [(l["seq"], l["efecto"]) for l in r["logs"]] == [(7, "efectiva")]
    (u,) = r["updates"]
    assert u["status"] == "resolved" and u["n_errors"] == 3 and u["n_reloads"] == 1
    assert u["reload_effective"] and u["recovered_by"] == "recarga_del_modelo"
    assert u["first_ok_event_id"] == "e7" and u["resolved_by_window"] == "e9" and u["duration_s"] == 6.0


def test_persistent_failure_reloads_are_bounded_and_ineffective():
    r = drive("oo" + "x" * 30 + "ooo")
    assert len(r["alerts"]) == 1                                 # un episodio, sin alertas repetidas
    assert r["reloads"] == [5, 10, 15]                           # errores 3, 8 y 13; tope max_reloads = 3
    assert [l["efecto"] for l in r["logs"]] == ["sin_efecto"] * 3
    (u,) = r["updates"]
    assert u["n_errors"] == 30 and u["n_reloads"] == 3 and not u["reload_effective"]
    assert u["recovered_by"] == "fallo_ceso_sin_efecto_de_las_recargas" and u["duration_s"] == 60.0


def test_streak_rules_and_source_gaps():
    assert drive("xxoxxo")["alerts"] == []                       # dos errores y una válida: la racha se corta
    assert drive("xxoxxo")["reloads"] == []
    # sin frame (S1) no cuenta como error de inferencia ni corta la racha
    r = drive("x-x-x" + "ooo")
    assert [e for e in r["alerts"][0]["event_ids"]] == ["e1", "e3", "e5"] and len(r["updates"]) == 1
    assert drive("-----")["alerts"] == []
    # sin regla configurada no hace nada
    mon = InferenceHealthMonitor({}, "c", "r")
    assert mon.add(ev(1, "inference_error")) == ([], [], [], False)


def test_windows_with_errors_are_insufficient_not_normal():
    w = WindowEvaluator(CFG, "c", "r")
    res = [w.add(ev(i, "inference_error" if i in (2, 3) else "ok")) for i in range(1, 7)]
    win = res[-1][0]
    assert win.n_valid == 4 and win.state == "insuficiente"      # 4 válidos < min_valid 5


def test_fault_plan_parsing_and_timing():
    f = InferenceFault.parse("persistente:300:60")
    assert f.as_dict() == {"mode": "persistente", "start_s": 300.0, "duration_s": 60.0}
    f.apply(None, 299.9)                                         # antes del intervalo: no falla
    with pytest.raises(RuntimeError, match="fallo inyectado"):
        f.apply(None, 300.0)
    f.apply(None, 360.0)                                         # fin exclusivo
    assert InferenceFault.parse("corrupcion:150").duration_s is None
    for bad in ("corrupcion", "persistente:1", "otro:1:2"):
        with pytest.raises(ValueError):
            InferenceFault.parse(bad)


def test_config_validation(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(f"base: {(REPO / 'config' / 'escenario-inferencia.yaml').as_posix()}\n"
                 "inference_health:\n  reload_every: 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="inference_health"):
        load_config(p)
    assert load_config(REPO / "config" / "escenario-inferencia.yaml")["inference_health"]["consecutive_errors"] == 3


WEIGHTS = REPO / "modelo" / "yolo11n.pt"


@pytest.mark.skipif(not WEIGHTS.exists(), reason="faltan los pesos (monitor.download_model)")
def test_corruption_is_real_and_reload_restores_identical_detections():
    """La corrupción produce el error real de torch y recargar desde los pesos da las mismas detecciones."""
    from monitor.detector import Detector

    cfg = load_config(REPO / "config" / "monitor.yaml")
    img = __import__("cv2").imread(str(sorted((REPO / "evidencias" / "entradas" / "c1-normal").glob("*.jpg"))[0]))
    d = Detector(cfg, cfg["model"]["sha256"])
    before = d.predict(img)[0]
    InferenceFault.parse("corrupcion:0").apply(d, 0.0)
    with pytest.raises(RuntimeError, match="channels"):
        d.predict(img)
    with pytest.raises(RuntimeError):                            # persiste: no se cura sola
        d.predict(img)
    assert Detector(cfg, cfg["model"]["sha256"]).predict(img)[0] == before


@pytest.mark.skipif(not WEIGHTS.exists(), reason="faltan los pesos (monitor.download_model)")
def test_run_end_to_end_with_reaction(tmp_path):
    """monitor.run real sobre 10 frames versionados: alerta, recarga efectiva, resolución y evaluate coincide."""
    replay = tmp_path / "replay"
    replay.mkdir()
    for f in sorted((REPO / "evidencias" / "entradas" / "c1-normal").glob("*.jpg"))[:10]:
        shutil.copy(f, replay / f.name)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f"base: {(REPO / 'config' / 'escenario-inferencia.yaml').as_posix()}\noutput:\n"
                   f"  runs_dir: {(tmp_path / 'runs').as_posix()}\n  db: {(tmp_path / 'm.db').as_posix()}\n",
                   encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "monitor.run", "--config", str(cfg), "--replay", str(replay), "--no-pace",
                        "--sin-vivo", "--inject-inference-error", "corrupcion:4", "--label", "s3-test"],
                       cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    (d,) = list((tmp_path / "runs").iterdir())
    evs = [json.loads(l) for l in open(d / "events.jsonl", encoding="utf-8")]
    assert [e["status"] for e in evs] == ["ok"] * 2 + ["inference_error"] * 3 + ["ok"] * 5
    assert all(e["image_ref"] for e in evs[2:5])                 # capturas del fallo (capture.on_error)
    inf = [json.loads(l) for l in open(d / "inferencia.jsonl", encoding="utf-8")]
    assert [x["type"] for x in inf] == ["recarga_modelo", "recarga_verificada"]
    assert inf[0]["sha256_verificado"] and inf[0]["resultado"] == "cargado" and inf[1]["efecto"] == "efectiva"
    upd = [json.loads(l) for l in open(d / "alerts.jsonl", encoding="utf-8")][-1]
    assert upd["status"] == "resolved" and upd["recovered_by"] == "recarga_del_modelo"
    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    assert m["inference_fault"]["mode"] == "corrupcion" and m["counts"]["reloads"] == 1
    ev_ = subprocess.run([sys.executable, "-m", "monitor.evaluate", str(d)], cwd=REPO, capture_output=True, text=True,
                         encoding="utf-8")
    assert ev_.returncode == 0 and "coinciden=True" in ev_.stdout
    # portal: resolución, diagnóstico y la reacción leída de inferencia.jsonl
    st = Store(tmp_path / "m.db")
    st.close()
    c = TestClient(create_app(tmp_path / "m.db", root=tmp_path, runs_dir="runs"))
    page = c.get(f"/alertas/{upd['alert_id']}").text
    assert "la recarga del modelo" in page and "Diagnóstico" in page and "Reacción: recargas del modelo" in page
    assert "efectiva" in page and "SHA-256 verificado" in page
