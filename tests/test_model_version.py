"""Escenario S2: cambio de versión del modelo no declarado, detección, reacción en ventanas y reversión."""

import json

from fastapi.testclient import TestClient

from monitor.alerts import ModelVersionMonitor, WindowEvaluator
from monitor.config import expected_model_ref, load_config
from monitor.detector import SwapPlan
from monitor.events import Detection, InferenceEvent
from monitor.metrics import trace_problems
from monitor.portal.app import create_app
from monitor.store import Store

EXPECTED = "yolo11n@sha256:0ebbc80d4a76"
OTHER = "yolo11s@sha256:85a76fe86dd8"
RULE = {"rule_id": "version-modelo-v1", "consecutive_mismatch": 3, "recover_after_match": 3,
        "severity": "critical", "suggested_action": "revisar el despliegue", "exclude_windows": True}
MODEL = {"name": "yolo11n", "sha256": "0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1"}
CFG = {
    "model": MODEL,
    "policy": {"lower": 0.5, "upper": 0.65, "analysis_floor": 0.25},
    "windows": {"size_frames": 2, "min_valid_frames": 2, "min_eligible": 4},
    "alerts": [{"rule_id": "baja", "signal": "low_ratio", "op": ">", "threshold": 0.7, "consecutive": 2,
                "severity": "warning", "suggested_action": "revisar"}],
}
LOW = [0.3, 0.3, 0.3, 0.6]       # low_ratio 0.75 -> cumple la regla "baja"


def ev(i, model_ref=EXPECTED, status="ok", scores=LOW):
    dets = [Detection(class_id=0, class_name="person", score=s, bbox_xyxy=[0, 0, 1, 1],
                      zone="low" if s < 0.5 else "doubt") for s in scores] if status == "ok" else []
    t = f"2026-01-01T00:{(2 * i) // 60:02d}:{(2 * i) % 60:02d}Z"
    return InferenceEvent(event_id=f"e{i}", run_id="r", frame_id=f"f{i}", seq=i, timestamp=t, captured_at=t,
                          source_id="s", model_ref=model_ref, config_ref="c",
                          thresholds={"lower": 0.5, "upper": 0.65}, status=status, detections=dets,
                          n_detections=len(dets))


def run_monitor(pattern):
    """pattern: 'o' = versión esperada, 'x' = otra versión, '-' = sin frame (invalid_input)."""
    mon = ModelVersionMonitor({**CFG, "model_version": RULE}, "c", "r")
    alerts, updates = [], []
    for i, c in enumerate(pattern, start=1):
        e = ev(i, EXPECTED if c != "x" else OTHER, "invalid_input" if c == "-" else "ok")
        a, u = mon.add(e)
        alerts += a
        updates += u
    return alerts, updates


def test_version_alert_opens_once_and_resolves_after_rollback():
    alerts, updates = run_monitor("ooo" + "x" * 8 + "ooo")
    (a,) = alerts
    assert a["event_ids"] == ["e4", "e5", "e6"] and a["alert_id"].endswith("-f000004")
    assert a["metric"]["expected_model_ref"] == EXPECTED and a["metric"]["reported_model_ref"] == OTHER
    (u,) = updates
    assert u["status"] == "resolved" and u["n_mismatch"] == 8 and u["reported_model_refs"] == [OTHER]
    assert u["duration_s"] == 16.0              # de e4 (t=8 s) a e12 (t=24 s), primer evento con la versión esperada
    assert u["first_ok_event_id"] == "e12" and u["resolved_by_window"] == "e14"


def test_needs_consecutive_mismatches_and_ignores_events_without_inference():
    assert run_monitor("oxxoxxo") == ([], [])
    alerts, updates = run_monitor("x-x-x" + "o-oo")       # los invalid_input ni cuentan ni cortan la racha
    assert len(alerts) == 1 and alerts[0]["event_ids"] == ["e1", "e3", "e5"] and len(updates) == 1


def test_disabled_without_rule():
    assert ModelVersionMonitor(CFG, "c", "r").add(ev(1, OTHER)) == ([], [])


def feed(cfg, events):
    w = WindowEvaluator(cfg, "c", "r")
    out = [w.add(e) for e in events]
    return [x for x in out if x[0] is not None]


def test_window_with_other_version_is_not_evaluated_and_keeps_streak():
    events = [ev(1), ev(2), ev(3, OTHER), ev(4), ev(5), ev(6)]   # ventanas: [n n] [s n] [n n]
    res = feed({**CFG, "model_version": RULE}, events)
    states = [w.state for w, _, _ in res]
    assert states == ["fuera_de_rango", "otra_version", "fuera_de_rango"]
    assert res[1][0].n_other_version == 1 and res[1][0].conditions == {"baja": None}
    # la ventana excluida no rompe la racha: la regla (2 ventanas seguidas) se abre en la ventana 3
    assert [len(new) for _, new, _ in res] == [0, 0, 1]
    # sin la regla de versión (configuración base) la misma ventana se evalúa como siempre
    base = feed(CFG, events)
    assert [w.state for w, _, _ in base] == ["fuera_de_rango"] * 3 and len(base[1][1]) == 1


def test_scenario_config_inherits_base_and_declares_alternative():
    base = load_config("config/monitor.yaml")
    s2 = load_config("config/escenario-version.yaml")
    assert s2["model"] == base["model"] and s2["policy"] == base["policy"] and s2["alerts"] == base["alerts"]
    assert s2["windows"] == base["windows"] and "model_version" in s2 and "model_version" not in base
    assert expected_model_ref(s2) == EXPECTED
    assert s2["model_alternatives"]["yolo11s"]["sha256"].startswith("85a76fe86dd8")


def test_swap_plan_uses_stream_time():
    plan = SwapPlan.parse("yolo11s:150:210")
    active = [seq for seq in range(1, 241) if plan.active_at((seq - 1) * 2.0)]
    assert active[0] == 76 and active[-1] == 180 and len(active) == 105


def test_g5_accepts_the_verified_swapped_artifact_only():
    manifest = {"config_ref": "c", "model_sha256": MODEL["sha256"], "config": {"policy": CFG["policy"]},
                "model_swap": {"sha256": "85a76fe86dd8afe384648546b56a7a78580c7cb7b404fc595f97969322d502d5"}}
    ok = ev(1, OTHER)
    ok.timings = {"inference_ms": 1.0}
    assert trace_problems(ok, manifest, root=None) == []
    unknown = ev(2, "yolo11x@sha256:ffffffffffff")
    unknown.timings = {"inference_ms": 1.0}
    assert "artefacto verificado" in " ".join(trace_problems(unknown, manifest, root=None))


def test_portal_shows_versions_and_version_resolution(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run = "20260101T000000Z-s2"
    store = Store(tmp_path / "monitor.db")
    store.upsert_config("c", {**CFG, "model_version": RULE})
    store.upsert_run({"run_id": run, "label": "s2", "mode": "replay", "started_at": "2026-01-01T00:00:00Z",
                      "config_ref": "c", "config": CFG, "model_ref": EXPECTED, "model_sha256": MODEL["sha256"],
                      "class_names": {}, "model_swap": {"name": "yolo11s", "start_s": 2.0, "duration_s": 4.0,
                                                        "model_ref": OTHER}})
    for i, m in enumerate([EXPECTED, OTHER, OTHER, EXPECTED], start=1):
        e = ev(i, m)
        e.run_id, e.event_id = run, f"evt-{run}-f{i:06d}"
        store.insert_event(e)
    aid = f"alert-{run}-version-modelo-v1-f000002"
    store.insert_alert({"alert_id": aid, "rule_id": "version-modelo-v1", "run_id": run,
                        "timestamp": "2026-01-01T00:00:06Z", "severity": "critical", "reason": "otra versión",
                        "suggested_action": "revisar", "config_ref": "c", "event_ids": [f"evt-{run}-f000002"],
                        "metric": {}})
    store.update_alert({"alert_id": aid, "status": "resolved", "resolved_at": "2026-01-01T00:00:08Z",
                        "n_mismatch": 2, "duration_s": 4.0, "reported_model_refs": [OTHER],
                        "first_ok_event_id": f"evt-{run}-f000004"})
    store.close()
    c = TestClient(create_app(tmp_path / "monitor.db", root=tmp_path))
    page = c.get(f"/runs/{run}").text
    assert "Versiones reportadas" in page and OTHER in page and "distinta de la configurada" in page
    alert = c.get(f"/alertas/{aid}").text
    assert "otra versión durante 4.0 s" in alert and f"evt-{run}-f000004" in alert
    assert json.loads(c.get(f"/api/alertas/{aid}").text)["status"] == "resolved"
