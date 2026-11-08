import pytest

from monitor.config import load_config
from monitor.policy import ACCEPTED, DOUBT, LOW, zone, zone_counts


@pytest.mark.parametrize(
    "score,expected",
    [
        (0.0, LOW),
        (0.3499, LOW),
        (0.35, DOUBT),   # igualdad con el inferior -> duda
        (0.5999, DOUBT),
        (0.60, ACCEPTED),  # igualdad con el superior -> aceptada
        (1.0, ACCEPTED),
    ],
)
def test_zone_boundaries(score, expected):
    assert zone(score, 0.35, 0.60) == expected


def test_zone_counts():
    assert zone_counts([LOW, LOW, DOUBT]) == {ACCEPTED: 0, DOUBT: 1, LOW: 2}


def test_invalid_thresholds_rejected(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "source: {sample_interval_s: 2}\nmodel: {conf_floor: 0.05}\npolicy: {lower: 0.6, upper: 0.6}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_config(cfg)


def test_conf_floor_must_not_hide_low_zone(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "source: {sample_interval_s: 2}\nmodel: {conf_floor: 0.5}\npolicy: {lower: 0.35, upper: 0.6}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_config(cfg)
