"""Escenario S5 · poco tráfico: diagnóstico, ventana extendida, recuperación, inyección y punta a punta."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from monitor.alerts import WindowEvaluator
from monitor.config import load_config
from monitor.events import Detection, InferenceEvent
from monitor.portal.app import create_app
from monitor.traffic import LowTrafficFault, SceneComposer, TrafficMonitor

REPO = Path(__file__).resolve().parents[1]
RULE = {"rule_id": "trafico-bajo-v1", "consecutive_windows": 1, "max_extension_windows": 3, "severity": "warning",
        "suggested_action": "revisar"}
CFG = {"windows": {"size_frames": 4, "min_valid_frames": 3, "min_eligible": 8},
       "policy": {"analysis_floor": 0.25, "lower": 0.5}, "source": {"sample_interval_s": 2.0},
       "alerts": [{"rule_id": "baja-confianza-v1", "signal": "low_ratio", "op": ">", "threshold": 0.6,
                   "consecutive": 1, "severity": "warning", "suggested_action": "a"}],
       "low_traffic": RULE}


def ev(i, scores, status="ok"):
    dets = [Detection(class_id=0, class_name="person", score=s, bbox_xyxy=[0, 0, 1, 1],
                      zone="low" if s < 0.5 else "accepted") for s in scores]
    return InferenceEvent(event_id=f"evt-r-f{i:06d}", run_id="r", frame_id=f"f{i}", seq=i,
                          timestamp=f"2026-01-01T00:{i // 30:02d}:{2 * i % 60:02d}.000Z",
                          captured_at=f"2026-01-01T00:{i // 30:02d}:{2 * i % 60:02d}.000Z", source_id="s",
                          model_ref="m", config_ref="c", thresholds={}, status=status,
                          detections=dets if status == "ok" else [])


def run(frames, cfg=CFG):
    """Alimenta WindowEvaluator y TrafficMonitor como run.py; devuelve ventanas, alertas, updates y registros."""
    w_ev, tm = WindowEvaluator(cfg, "c", "r"), TrafficMonitor(cfg, "c", "r")
    wins, alerts, updates, logs = [], [], [], []
    for i, f in enumerate(frames, start=1):
        e = ev(i, *f) if isinstance(f, tuple) else ev(i, f)
        tm.observe(e)
        win, _, _ = w_ev.add(e)
        if win:
            wins.append(win)
            a, u, l = tm.add(win)
            alerts += a; updates += u; logs += l
    return wins, alerts, updates, logs


BUSY = [[0.7, 0.7, 0.3]] * 4          # 12 elegibles por ventana: suficiente
SPARSE = [[0.7], [0.3], [0.7], []]     # 3 elegibles: poco tráfico con la fuente sana


def test_low_traffic_opens_extends_and_resolves():
    wins, alerts, updates, logs = run(BUSY + SPARSE * 3 + BUSY)
    assert [w.state for w in wins] == ["normal", "insuficiente", "insuficiente", "insuficiente", "normal"]
    (a,) = alerts
    assert a["alert_id"] == "alert-r-trafico-bajo-v1-w0002" and a["metric"]["diagnostico"]["causa"] == "poco_trafico"
    assert a["metric"]["value"] == 3 and a["metric"]["diagnostico"]["frames_validos"] == "4/4"
    ext = [l for l in logs if l["type"] == "extendida"]
    # ventanas 2+3+4 = 9 elegibles >= 8: una evaluación extendida con las reglas de confianza (3/9 bajas -> normal)
    assert [(x["ventanas"], x["estado"], x["n_eligible"]) for x in ext] == [([2, 3, 4], "normal", 9)]
    assert ext[0]["low_ratio"] == round(3 / 9, 4) and ext[0]["condiciones"] == {"baja-confianza-v1": False}
    (u,) = updates
    assert u["status"] == "resolved" and u["resolved_by_window"] == "win-r-0005" and u["n_low_windows"] == 3
    assert (u["windows_covered"], u["blind_s_with_reaction"], u["blind_s_without_reaction"]) == (3, 0.0, 24.0)
    assert [l["clasificacion"] for l in logs if l["type"] == "ventana"] == ["suficiente"] + ["poco_trafico"] * 3 + ["suficiente"]


def test_extension_can_fail_or_be_interrupted_and_flags_out_of_range():
    empty = [[0.7], [], [], []]                                    # 1 elegible: ni 3 ventanas alcanzan
    _, _, (u,), logs = run(BUSY + empty * 4 + BUSY)
    states = [x["estado"] for x in logs if x["type"] == "extendida"]
    assert states == ["insuficiente_aun_extendida", "sin_completar"]   # 3 ventanas; la 4.ª queda sin completar
    assert u["n_extended"] == 0 and u["blind_s_with_reaction"] == u["blind_s_without_reaction"] == 32.0
    low = [[0.3], [0.3], [0.3], [0.7]]                             # pocas y casi todas bajas
    _, _, _, logs = run(BUSY + low * 2 + BUSY)
    (x,) = [l for l in logs if l["type"] == "extendida"]
    assert x["estado"] == "fuera_de_rango" and x["condiciones"]["baja-confianza-v1"] is True


def test_source_loss_is_not_low_traffic():
    lost = [[], [], [], []]
    frames = BUSY + [(f, "invalid_input") for f in lost] + BUSY
    wins, alerts, updates, logs = run(frames)
    assert wins[1].state == "insuficiente" and alerts == [] and updates == []
    assert [l["clasificacion"] for l in logs][1] == "fuente"                # es S1, no poco tráfico
    assert TrafficMonitor({**CFG, "low_traffic": None}, "c", "r").add(wins[1]) == ([], [], [])


def test_fault_parsing_composer_and_config(tmp_path):
    assert LowTrafficFault.parse("zona:120:480").as_dict() == {"mode": "zona", "start_s": 120.0, "duration_s": 480.0}
    assert LowTrafficFault.parse("vacia:0:10").active_at(9.9) and not LowTrafficFault.parse("vacia:0:10").active_at(10)
    for bad in ("zona", "otra:1:2", "zona:a:b"):
        with pytest.raises(ValueError):
            LowTrafficFault.parse(bad)
    plate = np.full((100, 200, 3), 50, np.uint8)
    cv2.imwrite(str(tmp_path / "p.png"), plate)
    img = np.full((100, 200, 3), 200, np.uint8)
    z = SceneComposer({"plate": tmp_path / "p.png", "box": [80, 30, 120, 70], "feather": 2}, "zona", (100, 200)).apply(img)
    assert z[50, 100].tolist() == [200] * 3 and z[5, 5].tolist() == [50] * 3   # zona real; fuera, la escena vacía
    v = SceneComposer({"plate": tmp_path / "p.png", "box": [80, 30, 120, 70]}, "vacia", (100, 200)).apply(img)
    assert (v == 50).all()
    p = tmp_path / "c.yaml"
    p.write_text(f"base: {(REPO / 'config' / 'escenario-trafico.yaml').as_posix()}\nlow_traffic:\n  max_extension_windows: 1\n",
                 encoding="utf-8")
    with pytest.raises(ValueError, match="low_traffic"):
        load_config(p)


WEIGHTS = REPO / "modelo" / "yolo11n.pt"


@pytest.mark.skipif(not WEIGHTS.exists(), reason="faltan los pesos (monitor.download_model)")
def test_run_end_to_end_with_empty_scene(tmp_path):
    """monitor.run real: 12 frames, ventanas de 3; escena vacía en las muestras 4-9 -> alerta, extensión que no
    alcanza (insuficiente_aun_extendida no llega: la ventana 4 vuelve antes) y resolución; evaluate coincide."""
    replay = tmp_path / "replay"
    replay.mkdir()
    for f in sorted((REPO / "evidencias" / "entradas" / "c1-normal").glob("*.jpg"))[:12]:
        shutil.copy(f, replay / f.name)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f"base: {(REPO / 'config' / 'escenario-trafico.yaml').as_posix()}\n"
                   "windows:\n  size_frames: 3\n  min_valid_frames: 3\n  min_eligible: 30\n"
                   f"low_traffic:\n  injection:\n    plate: {(REPO / 'evidencias' / 'entradas' / 's5-poco-trafico' / 'escena-vacia.jpg').as_posix()}\n"
                   f"output:\n  runs_dir: {(tmp_path / 'runs').as_posix()}\n  db: {(tmp_path / 'm.db').as_posix()}\n",
                   encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "monitor.run", "--config", str(cfg), "--replay", str(replay), "--no-pace",
                        "--sin-vivo", "--inject-low-traffic", "vacia:6:12", "--label", "s5"],
                       cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    (d,) = list((tmp_path / "runs").iterdir())
    wins = [json.loads(l) for l in open(d / "windows.jsonl", encoding="utf-8")]
    st = [w["state"] for w in wins]                              # con 3 frames, C1 puede quedar fuera de rango
    assert st[1:3] == ["insuficiente"] * 2 and {st[0], st[3]} <= {"normal", "fuera_de_rango"}
    alog = [json.loads(l) for l in open(d / "alerts.jsonl", encoding="utf-8")]
    (al,) = [x for x in alog if x.get("rule_id") == "trafico-bajo-v1"]
    (up,) = [x for x in alog if x["type"] == "update" and x["alert_id"] == al["alert_id"]]
    lines = [al, up]
    assert up["status"] == "resolved" and up["n_low_windows"] == 2
    assert up["blind_s_with_reaction"] == up["blind_s_without_reaction"]
    traf = [json.loads(l) for l in open(d / "trafico.jsonl", encoding="utf-8")]
    assert [x["estado"] for x in traf if x["type"] == "extendida"] == ["sin_completar"]
    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    assert m["low_traffic_fault"] == {"mode": "vacia", "start_s": 6.0, "duration_s": 12.0}
    e = subprocess.run([sys.executable, "-m", "monitor.evaluate", str(d)], cwd=REPO, capture_output=True, text=True,
                       encoding="utf-8")
    assert e.returncode == 0 and "coinciden=True" in e.stdout
    page = TestClient(create_app(tmp_path / "m.db", root=tmp_path, runs_dir="runs")).get(f"/alertas/{lines[0]['alert_id']}")
    assert "poco tráfico durante" in page.text and "con la reacción" in page.text
