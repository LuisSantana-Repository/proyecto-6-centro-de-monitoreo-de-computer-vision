"""Escenario S1: interrupción de la fuente, diagnóstico, alerta, reconexión y resolución."""

import time

import numpy as np

from monitor.alerts import SourceHealthMonitor
from monitor.config import load_config
from monitor.events import InferenceEvent
from monitor.source import FaultPlan, LiveYouTubeSource, ReplaySource, error_text

RULE = {"rule_id": "fuente-interrumpida-v1", "consecutive_invalid": 3, "recover_after_valid": 3,
        "severity": "critical", "suggested_action": "revisar"}


def ev(i, valid=True):
    return InferenceEvent(event_id=f"e{i}", run_id="r", frame_id=f"f{i}", seq=i,
                          timestamp=f"2026-01-01T00:00:{2 * i:02d}Z", captured_at=f"2026-01-01T00:00:{2 * i:02d}Z",
                          source_id="s", model_ref="m", config_ref="c", thresholds={"lower": 0.5, "upper": 0.65},
                          status="ok" if valid else "invalid_input", error=None if valid else "sin frame")


def run_monitor(pattern):
    """pattern: cadena de 'o' (frame válido) y 'x' (sin frame)."""
    mon = SourceHealthMonitor({"source_health": RULE}, "c", "r")
    alerts, updates = [], []
    for i, c in enumerate(pattern, start=1):
        a, u = mon.add(ev(i, c == "o"))
        alerts += a
        updates += u
    return alerts, updates


def test_alert_opens_once_and_resolves_with_downtime():
    alerts, updates = run_monitor("ooo" + "x" * 10 + "ooo")
    assert len(alerts) == 1 and alerts[0]["event_ids"] == ["e4", "e5", "e6"]
    (u,) = updates
    assert u["status"] == "resolved" and u["n_invalid"] == 10
    assert u["downtime_s"] == 20.0            # de e4 (t=8 s) a e14 (t=28 s), primer frame recibido
    assert u["resolved_by_window"] == "e16"   # tercer frame válido seguido


def test_short_gaps_do_not_alert_and_must_be_consecutive():
    assert run_monitor("ooxxoxxoxxo") == ([], [])


def test_flapping_recovery_needs_consecutive_valid_frames():
    alerts, updates = run_monitor("xxx" + "oo" + "x" + "ooo")
    assert len(alerts) == 1 and len(updates) == 1 and updates[0]["n_invalid"] == 4


def test_unresolved_outage_stays_open():
    alerts, updates = run_monitor("oxxxxx")
    assert len(alerts) == 1 and updates == []


def test_disabled_without_config():
    assert SourceHealthMonitor({}, "c", "r").add(ev(1, False)) == ([], [])


def test_scenario_config_inherits_base(tmp_path):
    base = load_config("config/monitor.yaml")
    s1 = load_config("config/escenario-interrupcion.yaml")
    assert s1["policy"] == base["policy"] and s1["alerts"] == base["alerts"] and s1["windows"] == base["windows"]
    assert s1["source"]["url"] == base["source"]["url"] and s1["source"]["frame_timeout_s"] == 3.0
    assert "source_health" in s1 and "source_health" not in base


def test_replay_fault_is_deterministic(tmp_path):
    import cv2

    for i in range(10):
        cv2.imwrite(str(tmp_path / f"{i:06d}.jpg"), np.zeros((8, 8, 3), np.uint8))
    cfg = {"source": {"id": "s", "sample_interval_s": 2.0}}
    src = ReplaySource(cfg, tmp_path, pace=False, fault=FaultPlan(4, 6))   # t = 4, 6, 8 s -> frames 3, 4, 5
    src.start()
    frames = list(src.frames())
    assert [f.seq for f in frames if f.image is None] == [3, 4, 5]
    assert all("interrupción inyectada" in f.error for f in frames if f.image is None)
    log = [h["type"] for h in src.drain_health()]
    assert log == ["conectado", "desconectado", "reconectado"]
    assert src.stats.reconnections == 1 and src.stats.decode_failures == 3


class FakeCapture:
    def __init__(self):
        self.opened = True

    def isOpened(self):
        return self.opened

    def grab(self):
        time.sleep(0.005)
        return True

    def retrieve(self):
        return True, np.zeros((4, 4, 3), np.uint8)

    def release(self):
        self.opened = False


class FakeLive(LiveYouTubeSource):
    """Fuente en vivo con el mismo lector y la misma reconexión, pero sin red ni YouTube."""

    def _resolve(self):
        self.stats.stream_meta = {"format_id": "fake", "width": 4, "height": 4}
        return "fake://stream"

    def _open_capture(self, url):
        return FakeCapture()


def test_live_reader_disconnects_retries_and_recovers():
    cfg = {"source": {"id": "s", "url": "u", "format": "f", "sample_interval_s": 0.1,
                      "reconnect_after_failures": 1000, "reconnect_backoff_s": 0.1,
                      "reconnect_backoff_max_s": 0.2, "stall_timeout_s": 0.2, "frame_timeout_s": 0.15}}
    src = FakeLive(cfg, fault=FaultPlan(0.5, 1.0))
    src.start()
    try:
        frames = list(src.frames(max_frames=30))   # ~3 s: antes, durante y después de la caída
    finally:
        src.stop()
    log = [h["type"] for h in src.drain_health()]
    missing = [f for f in frames if f.image is None]
    assert log[0] == "conectado"
    assert "lectura_fallida" in log and "desconectado" in log and "reconexion_fallida" in log
    assert log[-1] == "reconectado"
    assert missing and all(f.error for f in missing)
    assert frames[0].image is not None and frames[-1].image is not None   # se recupera
    assert src.stats.reconnections == 1 and src.stats.reconnect_attempts >= 2


def test_error_text_strips_ansi_color_codes():
    # yt-dlp colorea "ERROR:"; el registro de la fuente debe guardar texto limpio
    e = RuntimeError("\x1b[0;31mERROR:\x1b[0m [youtube] x: Failed to resolve 'www.youtube.com'")
    assert error_text(e) == "RuntimeError: ERROR: [youtube] x: Failed to resolve 'www.youtube.com'"
