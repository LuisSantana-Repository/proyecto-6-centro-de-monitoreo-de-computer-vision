"""Métrica adicional M1 (W1 frente a la referencia): caso controlado, cero observaciones y umbral."""

import numpy as np

from monitor.drift import QS, quantiles, run_drift, w1
from monitor.events import Detection, InferenceEvent

CFG = {"policy": {"analysis_floor": 0.25}, "windows": {"size_frames": 4, "min_valid_frames": 3, "min_eligible": 8}}
RNG = np.random.default_rng(0)
REF = np.clip(RNG.beta(4, 4, 20000) * 0.75 + 0.25, 0.25, 1.0)


def test_identical_distribution_is_zero_and_controlled_shift_equals_delta():
    rq = quantiles(REF)
    assert w1(REF, rq) == 0.0
    for d in (0.02, 0.05, 0.1):
        assert abs(w1(REF - d, rq) - d) < 1e-3       # corrimiento conocido -> W1 = δ


def test_w1_is_symmetric_in_magnitude():
    rq = quantiles(REF)
    assert abs(w1(REF + 0.03, rq) - w1(REF - 0.03, rq)) < 1e-3
    assert len(QS) == 1000


def ev(i, scores, status="ok"):
    dets = [Detection(class_id=0, class_name="person", score=s, bbox_xyxy=[0, 0, 1, 1], zone="low") for s in scores]
    return InferenceEvent(event_id=f"e{i}", run_id="r", frame_id=f"f{i}", seq=i, timestamp="t", captured_at="t",
                          source_id="s", model_ref="m", config_ref="c", thresholds={}, status=status,
                          detections=dets if status == "ok" else [])


def test_run_drift_windows_threshold_and_insufficient():
    ref = {"threshold": 0.05, "quantiles": list(quantiles(REF)), "runs": ["ref"]}
    same = list(RNG.choice(REF, 12))
    low = list(np.clip(RNG.choice(REF, 12) - 0.15, 0.25, 1))
    events = [ev(i, same[3 * i:3 * i + 3]) for i in range(4)]                      # ventana 1: como la referencia
    events += [ev(4 + i, low[3 * i:3 * i + 3]) for i in range(4)]                  # ventana 2: desplazada
    events += [ev(8, [0.6] * 3), ev(9, [], "invalid_input"), ev(10, [], "invalid_input"), ev(11, [0.6] * 3)]  # insuficiente
    events += [ev(12, [0.6] * 3)]                                                  # incompleta
    d = run_drift(events, CFG, ref)
    w = d["ventanas"]
    assert w[1]["supera_umbral"] is True
    assert w[2]["w1"] is None and w[3]["w1"] is None           # sin evidencia suficiente: null, no 0
    assert d["n"] == 2 and d["ventanas_sobre_umbral"] == sum(x["supera_umbral"] is True for x in w)


def test_no_sufficient_windows_is_sin_datos():
    ref = {"threshold": 0.05, "quantiles": list(quantiles(REF)), "runs": ["ref"]}
    d = run_drift([ev(1, [0.5])], CFG, ref)
    assert d["valor"] is None and d["estado"] == "sin_datos"
