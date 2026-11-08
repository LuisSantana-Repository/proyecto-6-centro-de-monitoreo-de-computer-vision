from pathlib import Path

from monitor.events import Detection, InferenceEvent
from monitor.metrics import (alert_episodes, g1_inference, g2_fps, g3_ram, g4_low_ratio, g5_traceability,
                             g6_version, g7_active, pct)

MANIFEST = {"config_ref": "cfg-x", "model_sha256": "abcdef1234567890",
            "config": {"policy": {"lower": 0.5, "upper": 0.65, "analysis_floor": 0.25}}}


def ev(i, scores=(0.7, 0.3), status="ok", t="2026-01-01T00:00:{:02d}Z", **kw):
    dets = [Detection(class_id=0, class_name="person", score=s, bbox_xyxy=[0, 0, 1, 1],
                      zone="accepted" if s >= 0.65 else ("doubt" if s >= 0.5 else "low")) for s in scores]
    base = dict(event_id=f"e{i}", run_id="r", frame_id=f"f{i}", seq=i, timestamp=t.format(2 * i),
                captured_at=t.format(2 * i), source_id="s", model_ref="yolo11n@sha256:abcdef123456",
                config_ref="cfg-x", thresholds={"lower": 0.5, "upper": 0.65}, status=status,
                detections=dets if status == "ok" else [], timings={"inference_ms": 100.0 + i} if status in ("ok", "no_detections") else {},
                resources={"rss_mb": 400.0 + i, "cpu_percent": 50.0})
    base.update(kw)
    return InferenceEvent(**base)


def test_nearest_rank_percentile_and_empty():
    assert pct([1, 2, 3, 4], 50) == 2 and pct([1, 2, 3, 4], 95) == 4
    assert pct([], 50) is None


def test_zero_observations_are_null_not_zero():
    assert g1_inference([])["estado"] == "sin_datos" and g1_inference([])["valor"] is None
    assert g2_fps([ev(1)])["valor"] is None                       # un solo frame: no hay intervalo
    assert g3_ram([ev(1, resources={})])["estado"] == "sin_datos"
    assert g4_low_ratio([ev(1, scores=(0.1,))], 0.25, 0.5)["valor"] is None   # sin elegibles


def test_g1_excludes_failed_inference_and_g2_rate():
    evs = [ev(1), ev(2), ev(3, status="inference_error")]
    assert g1_inference(evs)["n"] == 2
    assert g2_fps([ev(1), ev(2), ev(3)])["valor"] == 0.5          # un frame cada 2 s


def test_g4_low_ratio_over_eligible():
    r = g4_low_ratio([ev(1, scores=(0.7, 0.3, 0.1)), ev(2, scores=(0.4, 0.6))], 0.25, 0.5)
    assert r["n"] == 4 and r["n_low"] == 2 and r["valor"] == 0.5


def test_g5_detects_broken_links_and_inconsistent_zones(tmp_path):
    img = tmp_path / "ok.jpg"
    img.write_bytes(b"x")
    good = ev(1, image_ref="ok.jpg", capture_reason="muestra_normal")
    missing = ev(2, image_ref="no-existe.jpg", capture_reason="muestra_normal")
    wrong_zone = ev(3)
    wrong_zone.detections[0].zone = "low"
    r = g5_traceability([good, missing, wrong_zone], MANIFEST, tmp_path)
    assert r["incompletos"] == 2 and r["valor"] == round(1 / 3, 4)


def test_g6_version_distribution():
    r = g6_version([ev(1), ev(2, model_ref="yolo11n@sha256:000000000000")], "yolo11n@sha256:abcdef123456")
    assert r["valor"] == 0.5 and len(r["reportadas"]) == 2


def test_g7_active_alerts_over_time():
    log = [{"type": "alert", "alert_id": "a1", "rule_id": "x", "severity": "warning", "window_end": "2026-01-01T00:02:00Z"},
           {"type": "update", "alert_id": "a1", "status": "resolved", "resolved_at": "2026-01-01T00:06:00Z"}]
    eps = alert_episodes(log)
    assert g7_active(eps, "2026-01-01T00:01:00Z") == 0
    assert g7_active(eps, "2026-01-01T00:04:00Z") == 1
    assert g7_active(eps, "2026-01-01T00:06:00Z") == 0


def test_m2_e2e_latency_excludes_invalid_and_empty_is_null():
    from monitor.metrics import m2_e2e_latency
    evs = [ev(1, e2e_ms=300.0), ev(2, e2e_ms=500.0), ev(3, status="invalid_input", e2e_ms=3000.0)]
    r = m2_e2e_latency(evs)
    assert r["n"] == 2 and r["valor"] == 300.0 and r["p95_ms"] == 500.0
    assert m2_e2e_latency([])["estado"] == "sin_datos"


def test_m3_recovery_from_source_incidents():
    from monitor.metrics import m3_recovery
    log = [{"type": "alert", "alert_id": "a1", "rule_id": "fuente-interrumpida-v1"},
           {"type": "update", "alert_id": "a1", "status": "resolved", "downtime_s": 90.0, "n_invalid": 45,
            "first_ok_event_id": "e9"},
           {"type": "alert", "alert_id": "a2", "rule_id": "fuente-interrumpida-v1"},          # sigue abierto
           {"type": "alert", "alert_id": "b1", "rule_id": "baja-confianza-v1"}]               # otra regla: no cuenta
    evs = [ev(1), ev(2, status="invalid_input"), ev(3), ev(4)]
    r = m3_recovery(log, evs)
    assert r["valor"] == 90.0 and r["n"] == 1 and r["incidentes_abiertos"] == 1 and r["disponibilidad"] == 0.75
    none = m3_recovery([], evs[:1])
    assert none["valor"] is None and none["estado"] == "sin_datos" and none["disponibilidad"] == 1.0
