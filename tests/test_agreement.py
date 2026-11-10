"""Métrica adicional M4 (acuerdo entre modelos): emparejamiento, población, ventanas, referencia y caso controlado."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

from monitor import agreement
from monitor.agreement import aggregate, degrade, frame_counts, match, run_agreement
from monitor.config import load_config
from monitor.events import Detection, InferenceEvent
from monitor.metrics import compute

REPO = Path(__file__).resolve().parents[1]
CFG = {"policy": {"analysis_floor": 0.25}, "windows": {"size_frames": 3}}
ACFG = {"agreement": {"reference": "yolo11s", "iou_min": 0.5, "min_frames": 2, "min_objects": 4}}
BOX = lambda x: [x, 0.0, x + 10.0, 10.0]


def d(c, s, x):
    return {"class_id": c, "score": s, "bbox_xyxy": BOX(x)}


def test_match_is_one_to_one_by_iou_class_and_symmetric():
    p = [(0, 0.9, BOX(0)), (0, 0.8, BOX(1)), (2, 0.7, BOX(50))]
    r = [(0, 0.6, BOX(0.5)), (7, 0.9, BOX(50)), (0, 0.5, BOX(200))]
    assert match(p, r, 0.5) == 1                          # dos de producción compiten por una caja: un solo par
    assert match(r, p, 0.5) == 1                          # simétrico
    assert match(p, r, 0.5, same_class=False) == 2        # car frente a truck: misma caja, otra clase
    assert match([(0, 0.9, BOX(0))], [(0, 0.9, BOX(6))], 0.5) == 0   # IoU 4/16 = 0.25 < 0.5
    assert match([], r, 0.5) == 0


def test_frame_counts_use_the_analysis_floor():
    c = frame_counts([d(0, 0.9, 0), d(0, 0.2, 100)], [d(0, 0.3, 0), d(0, 0.24, 100)], 0.25, 0.5)
    assert c == {"m": 1, "m_sin_clase": 1, "n_prod": 1, "n_ref": 1}  # las de s < 0.25 no entran


def ev(i, dets, status="ok"):
    return InferenceEvent(event_id=f"e{i}", run_id="r", frame_id=f"f{i}", seq=i, timestamp="t", captured_at="t",
                          source_id="s", model_ref="m", config_ref="c", thresholds={}, status=status,
                          detections=[Detection(class_name="x", zone="low", **x) for x in dets] if status == "ok" else [])


def rec(i, dets, status="medido", model="yolo11s@sha256:85a76fe86dd8"):
    return {"seq": i, "event_id": f"e{i}", "status": status, "reference_model_ref": model,
            **({"detections": dets} if status == "medido" else {})}


def test_aggregate_dice_coverage_backing_and_empty_population():
    pairs = [(ev(1, [d(0, .9, 0), d(0, .9, 30)]), rec(1, [d(0, .9, 0)])),          # 1 par; 2 prod, 1 ref
             (ev(2, [d(2, .9, 0)]), rec(2, [d(7, .9, 0), d(0, .9, 60)])),          # 0 pares (clase); 1 prod, 2 ref
             (ev(3, []), rec(3, []))]                                              # 0/0: no aporta ni resta
    a = aggregate(pairs, 0.25, 0.5, 2, 4)
    assert (a["m"], a["n_prod"], a["n_ref"], a["n_frames"]) == (1, 3, 3, 3)
    assert a["acuerdo"] == round(2 / 6, 4) and a["cobertura_referencia"] == round(1 / 3, 4)
    assert a["respaldo_produccion"] == round(1 / 3, 4) and a["acuerdo_sin_clase"] == round(4 / 6, 4)
    # sin objetos o sin frames medidos: null (sin_datos), nunca 0 ni 1
    assert aggregate([(ev(3, []), rec(3, []))] * 3, 0.25, 0.5, 2, 4)["acuerdo"] is None
    assert aggregate([(ev(1, [], "invalid_input"), rec(1, [], "excluido"))] * 3, 0.25, 0.5, 2, 1)["acuerdo"] is None


def test_run_agreement_windows_floor_exclusions_and_consistency():
    same = [d(0, .9, 0), d(0, .9, 30)]
    events = [ev(i, same) for i in (1, 2, 3)]                                      # ventana 1: acuerdo 1.0
    events += [ev(4, same), ev(5, same), ev(6, same)]                              # ventana 2: la referencia no ve nada
    events += [ev(7, same), ev(8, [], "inference_error"), ev(9, same)]             # ventana 3: 2 frames medidos
    recs = [rec(i, same) for i in (1, 2, 3)] + [rec(i, []) for i in (4, 5, 6)]
    recs += [rec(7, same), rec(8, [], "excluido"), rec(9, [], "sin_frame")]
    out = run_agreement(events, recs, CFG, ACFG, {"piso": 0.6})
    w = out["ventanas"]
    assert [x["acuerdo"] for x in w] == [1.0, 0.0, None]                           # ventana 3: 1 frame < min_frames
    assert [x["bajo_referencia"] for x in w] == [False, True, None]
    assert out["frames"] == {"medido": 7, "excluido": 1, "sin_frame": 1} and out["ventanas_bajo_referencia"] == 1
    assert out["valor"] == round(2 * 8 / (14 + 8), 4)                              # ejecución: 8 pares, 14 prod, 8 ref
    #                                                                (e9 tiene detecciones pero no frame: excluido)
    with pytest.raises(ValueError, match="no están en events"):
        run_agreement(events[:3], recs, CFG, ACFG, None)
    with pytest.raises(ValueError, match="mezcla"):
        run_agreement(events[:3], recs[:2] + [rec(3, same, model="otro@sha256:0")], CFG, ACFG, None)


def test_config_requires_a_declared_reference(tmp_path):
    cfg = load_config(REPO / "config" / "acuerdo.yaml")
    assert cfg["agreement"]["reference"] == "yolo11s" and cfg["model"]["name"] == "yolo11n"
    p = tmp_path / "a.yaml"
    p.write_text(f"base: {(REPO / 'config' / 'acuerdo.yaml').as_posix()}\nagreement:\n  reference: yolo99\n",
                 encoding="utf-8")
    with pytest.raises(ValueError, match="agreement"):
        load_config(p)


def test_degrade_is_deterministic_and_only_touches_the_declared_frames(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    rng = np.random.default_rng(0)
    for i in range(1, 6):
        cv2.imwrite(str(src / f"{i:06d}.jpg"), rng.integers(0, 255, (40, 60, 3), dtype=np.uint8))
    a = degrade(src, tmp_path / "a", desde=4, frames=5, sigma=3)
    b = degrade(src, tmp_path / "b", desde=4, frames=5, sigma=3)
    assert [x["sha256"] for x in a["archivos"]] == [x["sha256"] for x in b["archivos"]]
    assert [x["sha256"] == x["src_sha256"] for x in a["archivos"]] == [True, True, True, False, False]
    assert json.loads((tmp_path / "a" / "degradacion.json").read_text(encoding="utf-8"))["desde_seq"] == 4


def test_metrics_include_m4_only_when_measured(tmp_path):
    run = tmp_path / "r"
    run.mkdir()
    src = REPO / "datos" / "runs" / "20261108T232126Z-s2-cambio-version"
    if not (src / agreement.OUT).exists():
        pytest.skip("falta acuerdo.jsonl de S2 (monitor.agreement medir)")
    for f in ("manifest.json", "events.jsonl", "alerts.jsonl"):
        shutil.copy(src / f, run / f)
    acfg = load_config(REPO / "config" / "acuerdo.yaml")
    m = compute(run, agree_cfg=acfg, root=REPO)
    assert m["metricas_adicionales"]["M4_acuerdo_modelos"]["estado"] == "sin_datos"
    assert all(w["M4_acuerdo_modelos"] is None for w in m["ventanas"])
    shutil.copy(src / agreement.OUT, run / agreement.OUT)
    m = compute(run, agree_cfg=acfg, root=REPO)
    m4 = m["metricas_adicionales"]["M4_acuerdo_modelos"]
    # S2: producción es yolo11s en las muestras 76-180 -> la ventana 3 (121-180) coincide exactamente
    assert m4["estado"] == "ok" and m4["modelo_referencia"].startswith("yolo11s@")
    assert m["ventanas"][2]["M4_acuerdo_modelos"] == 1.0
    assert m["ventanas"][0]["M4_acuerdo_modelos"] < 0.8 and m["ventanas"][3]["M4_acuerdo_modelos"] < 0.8


WEIGHTS = REPO / "modelo" / "yolo11n.pt"


@pytest.mark.skipif(not WEIGHTS.exists(), reason="faltan los pesos (monitor.download_model)")
def test_measure_against_itself_is_exactly_one(tmp_path):
    """Control: el modelo de producción como referencia reproduce sus propias detecciones -> acuerdo 1.0."""
    replay = tmp_path / "replay"
    replay.mkdir()
    for f in sorted((REPO / "evidencias" / "entradas" / "c1-normal").glob("*.jpg"))[:6]:
        shutil.copy(f, replay / f.name)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f"base: {(REPO / 'config' / 'monitor.yaml').as_posix()}\nwindows:\n  size_frames: 3\noutput:\n"
                   f"  runs_dir: {(tmp_path / 'runs').as_posix()}\n  db: {(tmp_path / 'm.db').as_posix()}\n",
                   encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "monitor.run", "--config", str(cfg), "--replay", str(replay),
                        "--no-pace", "--sin-vivo", "--label", "m4"], cwd=REPO, capture_output=True, text=True,
                       encoding="utf-8", timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    (run,) = list((tmp_path / "runs").iterdir())
    acfg = load_config(REPO / "config" / "acuerdo.yaml")
    acfg["agreement"] |= {"min_frames": 1, "min_objects": 1}
    agreement.measure(run, acfg, name="yolo11n", out_name="control.jsonl")
    recs = agreement.load_records(run / "control.jsonl")
    assert [x["status"] for x in recs] == ["medido"] * 6 and all(len(x["frame_sha256"]) == 64 for x in recs)
    out = agreement.report(run, acfg, None, "control.jsonl")
    assert out["valor"] == 1.0 and [w["acuerdo"] for w in out["ventanas"]] == [1.0, 1.0]


def test_charts_show_m4_only_for_measured_runs(tmp_path):
    from fastapi.testclient import TestClient
    from monitor.portal.app import create_app
    from monitor.store import Store

    src = REPO / "datos" / "runs" / "20261108T232126Z-s2-cambio-version"
    if not (src / agreement.OUT).exists():
        pytest.skip("falta acuerdo.jsonl de S2 (monitor.agreement medir)")
    (tmp_path / "config").mkdir()
    for f in ("monitor.yaml", "acuerdo.yaml"):
        shutil.copy(REPO / "config" / f, tmp_path / "config" / f)
    run = tmp_path / "runs" / src.name
    shutil.copytree(src, run, ignore=shutil.ignore_patterns("imagenes"))
    store = Store(tmp_path / "m.db")
    store.ingest_run_dir(run)
    store.close()
    c = TestClient(create_app(tmp_path / "m.db", root=tmp_path, runs_dir="runs"))
    page = c.get(f"/graficas?run_id={src.name}")
    assert page.status_code == 200 and "métrica M4, yolo11s@sha256:85a76fe86dd8" in page.text
    api = c.get(f"/api/runs/{src.name}/graficas").json()["m4"]
    assert [p["value"] for p in api["points"]][2] == 1.0 and api["floor"] is None   # sin referencia: sin piso
    (run / agreement.OUT).unlink()
    assert "métrica M4" not in c.get(f"/graficas?run_id={src.name}").text


def test_controlled_blur_case_paired_with_c1():
    """Caso controlado versionado: frames 1-120 idénticos a C1 (diferencia 0 exacta); 121-240 desenfocados (baja)."""
    base = REPO / "datos" / "runs" / "20261108T161753Z-c1-normal"
    blur = REPO / "datos" / "runs" / "20261109T054751Z-m4-desenfoque"
    if not ((base / agreement.OUT).exists() and (blur / agreement.OUT).exists()):
        pytest.skip("faltan las mediciones de M4")
    w = agreement.compare(base, blur, load_config(REPO / "config" / "acuerdo.yaml"))
    assert [x["frames_identicos"] for x in w] == [60, 60, 0, 0]
    assert [x["diferencia"] for x in w[:2]] == [0.0, 0.0]
    assert all(x["diferencia"] < -0.05 for x in w[2:])
