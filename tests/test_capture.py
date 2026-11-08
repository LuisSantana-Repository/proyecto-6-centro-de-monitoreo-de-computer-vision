from monitor.events import Detection
from monitor.run import capture_reason

CFG = {"policy": {"analysis_floor": 0.25},
       "capture": {"normal_every_n_frames": 15, "frame_mean_below": 0.38, "frame_min_eligible": 5,
                   "on_no_detections": True, "on_error": True}}


def dets(*scores):
    return [Detection(class_id=0, class_name="person", score=s, bbox_xyxy=[0, 0, 1, 1], zone="low") for s in scores]


def test_frames_without_detections_are_captured_separately():
    assert capture_reason(CFG, 7, "no_detections", []) == "sin_detecciones"


def test_errors_are_captured():
    assert capture_reason(CFG, 7, "inference_error", []) == "error"
    assert capture_reason(CFG, 7, "invalid_input", []) == "error"


def test_low_confidence_frame_needs_enough_eligible_detections():
    assert capture_reason(CFG, 7, "ok", dets(0.3, 0.3, 0.3, 0.3, 0.3)) == "baja_confianza"
    assert capture_reason(CFG, 7, "ok", dets(0.3, 0.3, 0.3, 0.3)) is None          # solo 4 elegibles
    assert capture_reason(CFG, 7, "ok", dets(0.3, 0.3, 0.3, 0.3, 0.3, 0.1, 0.1)) == "baja_confianza"  # < 0.25 no cuenta


def test_periodic_normal_sample():
    assert capture_reason(CFG, 30, "ok", dets(0.9)) == "muestra_normal"
    assert capture_reason(CFG, 31, "ok", dets(0.9)) is None
