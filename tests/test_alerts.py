from monitor.alerts import WindowEvaluator, window_stats
from monitor.events import Detection, InferenceEvent

CFG = {
    "policy": {"lower": 0.5, "upper": 0.65, "analysis_floor": 0.25},
    "windows": {"size_frames": 2, "min_valid_frames": 2, "min_eligible": 4},
    "alerts": [
        {"rule_id": "baja", "signal": "low_ratio", "op": ">", "threshold": 0.7, "consecutive": 1,
         "severity": "warning", "suggested_action": "revisar"},
        {"rule_id": "sostenido", "signal": "mean_score", "op": "<", "threshold": 0.45, "consecutive": 2,
         "severity": "critical", "suggested_action": "investigar"},
    ],
}
_seq = 0


def ev(scores, status="ok"):
    global _seq
    _seq += 1
    dets = [Detection(class_id=0, class_name="person", score=s, bbox_xyxy=[0, 0, 1, 1],
                      zone="low" if s < 0.5 else "doubt") for s in scores]
    return InferenceEvent(
        event_id=f"e{_seq}", run_id="r", frame_id=f"f{_seq}", seq=_seq, timestamp="t", captured_at=f"c{_seq}",
        source_id="s", model_ref="m", config_ref="cfg", thresholds={"lower": 0.5, "upper": 0.65},
        status=status, detections=dets, n_detections=len(dets),
    )


NORMAL = [0.6, 0.6, 0.55, 0.3, 0.1]        # elegibles: 0.6,0.6,0.55,0.3 -> low 1/4, media 0.5125
LOWCONF = [0.3, 0.3, 0.3, 0.6, 0.05]       # elegibles: 0.3,0.3,0.3,0.6 -> low 3/4, media 0.375


def many(scores, n):
    return [ev(scores) for _ in range(n)]


def feed(evaluator, frames):
    out = []
    for f in frames:
        win, new, upd = evaluator.add(f)
        if win:
            out.append((win, new, upd))
    return out


def test_window_stats_excludes_below_floor_and_invalid():
    st = window_stats([ev(NORMAL), ev([0.9], status="inference_error")], floor=0.25, lower=0.5)
    assert (st["n_valid"], st["n_eligible"], st["n_low"]) == (1, 4, 1)
    assert st["low_ratio"] == 0.25 and st["mean_score"] == 0.5125


def test_normal_windows_raise_nothing():
    e = WindowEvaluator(CFG, "cfg", "r")
    res = feed(e, many(NORMAL, 6))
    assert [w.state for w, _, _ in res] == ["normal"] * 3
    assert all(not new for _, new, _ in res)


def test_low_window_alerts_once_per_episode_then_resolves():
    e = WindowEvaluator(CFG, "cfg", "r")
    res = feed(e, many(LOWCONF, 6) + many(NORMAL, 2))
    alerts = [a for _, new, _ in res for a in new]
    # baja (k=1) en la 1.ª ventana; sostenido (k=2) en la 2.ª; ninguna repetida en la 3.ª
    assert [a["rule_id"] for a in alerts] == ["baja", "sostenido"]
    assert len(alerts[1]["event_ids"]) == 4 and alerts[1]["n_windows"] == 2
    upd = [u for _, _, ups in res for u in ups]
    assert any(u.get("suppressed_repeats") for u in upd)
    resolved = [u["alert_id"] for u in upd if u.get("status") == "resolved"]
    assert sorted(resolved) == sorted(a["alert_id"] for a in alerts)


def test_insufficient_window_neither_normal_nor_breaks_streak():
    e = WindowEvaluator(CFG, "cfg", "r")
    few = [0.3]                                   # 1 elegible por frame -> ventana insuficiente
    res = feed(e, many(LOWCONF, 2) + many(few, 2) + many(LOWCONF, 2))
    assert [w.state for w, _, _ in res] == ["fuera_de_rango", "insuficiente", "fuera_de_rango"]
    assert res[1][0].conditions == {"baja": None, "sostenido": None}
    # la racha de 'sostenido' continúa a través de la ventana insuficiente
    assert [a["rule_id"] for a in res[2][1]] == ["sostenido"]


def test_partial_final_window_is_incomplete():
    e = WindowEvaluator(CFG, "cfg", "r")
    feed(e, [ev(LOWCONF)])
    win, new, _ = e.flush()
    assert win.state == "incompleta" and new == []
