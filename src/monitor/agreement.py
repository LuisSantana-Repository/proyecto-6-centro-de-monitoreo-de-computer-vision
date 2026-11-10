"""Métrica adicional M4 · acuerdo entre modelos (producción frente a un modelo de referencia).

Pregunta: sin etiquetas, ¿el modelo desplegado (yolo11n) sigue viendo lo mismo que un modelo de referencia más
grande (yolo11s) sobre **los mismos frames**? Correlaciona dos fuentes de predicción por frame: las detecciones
que produjo el monitor (registro primario, `events.jsonl`) y las de la referencia sobre el mismo archivo de
entrada (`acuerdo.jsonl`, con el SHA-256 de cada frame).

Emparejamiento por frame: detecciones con s >= analysis_floor de cada modelo (la población de G4). Se aceptan
pares de la misma clase con IoU >= iou_min, de mayor a menor IoU, sin reutilizar cajas (greedy, simétrico y
determinista). Con m pares, n_prod y n_ref detecciones:

  acuerdo (Dice / F1)   = 2·Σm / (Σn_prod + Σn_ref)    sumado sobre los frames de la ventana
  cobertura_referencia  = Σm / Σn_ref                   (lo que la referencia ve y producción también)
  respaldo_produccion   = Σm / Σn_prod                  (lo que producción ve y la referencia confirma)
  acuerdo_sin_clase     = lo mismo ignorando la clase   (la diferencia es desacuerdo de clase: car/truck…)

Población: eventos con inferencia válida (`ok`, `no_detections`) que tienen frame medido por la referencia.
Exclusiones explícitas en acuerdo.jsonl: eventos sin inferencia válida (`excluido`) y frames que ya no existen
(`sin_frame`). Ventana: las mismas de 60 eventos; suficiente si tiene >= min_frames frames medidos y
Σn_prod + Σn_ref >= min_objects; si no, null (`sin_datos`), nunca 0. Un frame sin objetos en ninguno de los
dos modelos no aporta (0/0), no cuenta como acuerdo perfecto.

Referencia: el acuerdo por ventana de las dos ejecuciones de referencia (las mismas de los umbrales y de M1).
Piso = mínimo de esas ventanas; una ventana por debajo queda `bajo_referencia`. Se guarda en
config/referencia-acuerdo.json.

Uso:
  uv run python -m monitor.agreement medir datos/runs/<run_id> [...]          # corre la referencia -> acuerdo.jsonl
  uv run python -m monitor.agreement medir datos/runs/<run_id> --modelo yolo11n --salida acuerdo-control.jsonl
  uv run python -m monitor.agreement referencia datos/runs/<ref1> datos/runs/<ref2>
  uv run python -m monitor.agreement reporte datos/runs/<run_id> [...]        # por ventana, sin modelo
  uv run python -m monitor.agreement degradar <grabación> <destino> --desde 121 --frames 240 --sigma 3
  uv run python -m monitor.agreement comparar datos/runs/<base> datos/runs/<degradada>   # mismas muestras, pareado
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

from monitor.config import config_ref, load_config, sha256_file
from monitor.events import InferenceEvent
from monitor.source import utc_now_iso

VALID = ("ok", "no_detections")
CONFIG = "config/acuerdo.yaml"
REF_FILE = "config/referencia-acuerdo.json"
OUT = "acuerdo.jsonl"


# -- emparejamiento -----------------------------------------------------------------
def iou(a, b) -> float:
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def as_tuples(dets, floor: float) -> list[tuple]:
    """(class_id, score, bbox) de detecciones con s >= floor; acepta dicts o Detection."""
    out = []
    for d in dets:
        c, s, b = (d["class_id"], d["score"], d["bbox_xyxy"]) if isinstance(d, dict) else (d.class_id, d.score, d.bbox_xyxy)
        if s >= floor:
            out.append((c, s, b))
    return out


def match(prod: list[tuple], ref: list[tuple], iou_min: float, same_class: bool = True) -> int:
    """Pares uno a uno con IoU >= iou_min (y misma clase), de mayor a menor IoU."""
    cand = sorted(((iou(p[2], r[2]), i, j) for i, p in enumerate(prod) for j, r in enumerate(ref)
                   if not same_class or p[0] == r[0]), key=lambda x: (-x[0], x[1], x[2]))
    used_p, used_r, m = set(), set(), 0
    for v, i, j in cand:
        if v < iou_min:
            break
        if i not in used_p and j not in used_r:
            used_p.add(i); used_r.add(j); m += 1
    return m


def frame_counts(prod_dets, ref_dets, floor: float, iou_min: float) -> dict:
    p, r = as_tuples(prod_dets, floor), as_tuples(ref_dets, floor)
    return {"m": match(p, r, iou_min), "m_sin_clase": match(p, r, iou_min, same_class=False),
            "n_prod": len(p), "n_ref": len(r)}


# -- medición (corre el modelo de referencia) ---------------------------------------
def load_events(run_dir: Path) -> list[InferenceEvent]:
    return [InferenceEvent.model_validate_json(l) for l in open(run_dir / "events.jsonl", encoding="utf-8")]


def reference_artifact(acfg: dict, run_cfg: dict, name: str | None) -> tuple[str, str, str]:
    """(nombre, pesos, sha256) del modelo de referencia; `name` igual al de producción da el control."""
    name = name or acfg["agreement"]["reference"]
    if name == run_cfg["model"]["name"]:
        return name, run_cfg["model"]["weights"], run_cfg["model"]["sha256"]
    alt = acfg["model_alternatives"][name]
    return name, alt["weights"], alt["sha256"]


def measure(run_dir: Path, acfg: dict, name: str | None = None, out_name: str = OUT,
            replay_dir: str | None = None) -> Path:
    from monitor.detector import Detector     # import diferido: reporte/metrics no cargan torch
    import cv2

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    cfg = manifest["config"]                  # mismos parámetros de inferencia que la ejecución medida
    src = Path(replay_dir or (manifest.get("source_meta") or {}).get("replay_dir") or "")
    if not replay_dir and manifest.get("mode") != "replay":
        raise SystemExit(f"{run_dir.name}: no es un replay; pasa --frames-dir con la grabación de esos frames")
    if not src.is_dir():
        raise SystemExit(f"{run_dir.name}: no existe la carpeta de frames {src}")
    name, weights, sha = reference_artifact(acfg, cfg, name)
    if not Path(weights).exists() or sha256_file(weights) != sha:
        raise SystemExit(f"Pesos de {name} ausentes o con SHA-256 distinto: "
                         f"uv run python -m monitor.download_model --config {CONFIG}")
    det = Detector(cfg, sha, weights=weights, name=name)
    det.warmup()
    floor = cfg["policy"]["analysis_floor"]
    common = {"run_id": manifest["run_id"], "reference_model_ref": det.model_ref,
              "agreement_config_ref": config_ref(acfg), "analysis_floor": floor}
    tmp = run_dir / (out_name + ".tmp")
    n = {"medido": 0, "excluido": 0, "sin_frame": 0}
    with open(tmp, "w", encoding="utf-8") as f:
        for e in load_events(run_dir):
            rec = {"seq": e.seq, "event_id": e.event_id, "origin": e.origin, **common}
            frame = src / e.origin if e.origin else None
            if e.status not in VALID:
                rec |= {"status": "excluido", "motivo": e.status}
            elif frame is None or not frame.is_file():
                rec |= {"status": "sin_frame", "motivo": f"no existe {frame}"}
            else:
                dets, t = det.predict(cv2.imread(str(frame)))
                rec |= {"status": "medido", "frame_sha256": sha256_file(frame), "inference_ms": t["inference_ms"],
                        "n_total": len(dets), "detections": [d for d in dets if d["score"] >= floor]}
            n[rec["status"]] += 1
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    out = run_dir / out_name
    os.replace(tmp, out)
    print(f"== {run_dir.name}: {out_name} con {det.model_ref} · medidos={n['medido']} excluidos={n['excluido']} "
          f"sin_frame={n['sin_frame']}")
    return out


# -- cálculo (desde los registros, sin modelo) --------------------------------------
def load_records(path: Path) -> list[dict]:
    return [json.loads(l) for l in open(path, encoding="utf-8")]


def aggregate(pairs: list[tuple], floor: float, iou_min: float, min_frames: int, min_objects: int,
              size: int | None = None) -> dict:
    """pairs: (evento, registro de acuerdo) de una ventana o de la ejecución."""
    tot = {"m": 0, "m_sin_clase": 0, "n_prod": 0, "n_ref": 0}
    n_frames = 0
    for e, r in pairs:
        if e.status in VALID and r is not None and r["status"] == "medido":
            c = frame_counts(e.detections, r["detections"], floor, iou_min)
            for k in tot:
                tot[k] += c[k]
            n_frames += 1
    denom = tot["n_prod"] + tot["n_ref"]
    ok = n_frames >= min_frames and denom >= min_objects and (size is None or len(pairs) == size)
    r4 = lambda a, b: round(a / b, 4) if ok and b else None
    return {"acuerdo": r4(2 * tot["m"], denom), "cobertura_referencia": r4(tot["m"], tot["n_ref"]),
            "respaldo_produccion": r4(tot["m"], tot["n_prod"]),
            "acuerdo_sin_clase": r4(2 * tot["m_sin_clase"], denom), "n_frames": n_frames, **tot}


def run_agreement(events: list[InferenceEvent], recs: list[dict], cfg: dict, acfg: dict,
                  ref: dict | None) -> dict:
    a = acfg["agreement"]
    floor, size = cfg["policy"]["analysis_floor"], cfg["windows"]["size_frames"]
    by_id = {r["event_id"]: r for r in recs}
    unknown = set(by_id) - {e.event_id for e in events}
    if unknown:
        raise ValueError(f"acuerdo.jsonl tiene {len(unknown)} registros de eventos que no están en events.jsonl")
    if len({r["reference_model_ref"] for r in recs}) > 1:
        raise ValueError("acuerdo.jsonl mezcla modelos de referencia")
    piso = ref["piso"] if ref else None
    args = (floor, a["iou_min"], a["min_frames"], a["min_objects"])
    per_window = []
    for i in range(0, len(events), size):
        ch = events[i:i + size]
        w = aggregate([(e, by_id.get(e.event_id)) for e in ch], *args, size=size)
        per_window.append({"idx": i // size + 1, "first_event_id": ch[0].event_id, "last_event_id": ch[-1].event_id,
                           **w, "bajo_referencia": (w["acuerdo"] < piso) if (piso is not None and w["acuerdo"] is not None) else None})
    run = aggregate([(e, by_id.get(e.event_id)) for e in events], *args)
    vals = [w["acuerdo"] for w in per_window if w["acuerdo"] is not None]
    st = {s: sum(r["status"] == s for r in recs) for s in ("medido", "excluido", "sin_frame")}
    return {
        "valor": run["acuerdo"], "unidad": "proporción (Dice de detecciones emparejadas)", "n": len(vals),
        "estado": "ok" if run["acuerdo"] is not None else "sin_datos",
        "modelo_referencia": recs[0]["reference_model_ref"] if recs else None,
        "cobertura_referencia": run["cobertura_referencia"], "respaldo_produccion": run["respaldo_produccion"],
        "acuerdo_sin_clase": run["acuerdo_sin_clase"], "frames": st, "eventos_sin_registro": len(events) - len(recs),
        "piso_referencia": piso, "ventanas_bajo_referencia": sum(v < piso for v in vals) if piso is not None else None,
        "min_ventana": min(vals) if vals else None,
        "definicion": f"2·pares / (n_prod + n_ref); s >= {floor}, misma clase, IoU >= {a['iou_min']}",
        "ventanas": per_window,
    }


def report(run_dir: Path, acfg: dict, ref: dict | None, file: str = OUT) -> dict:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    return run_agreement(load_events(run_dir), load_records(run_dir / file), manifest["config"], acfg, ref)


def build_reference(runs: list[Path], acfg: dict) -> dict:
    vals, per_run = [], {}
    for r in runs:
        d = report(r, acfg, None)
        w = [x["acuerdo"] for x in d["ventanas"] if x["acuerdo"] is not None]
        per_run[r.name] = {"acuerdo": d["valor"], "ventanas": w}
        vals += w
    vals_sorted = sorted(vals)
    return {
        "generated_at": utc_now_iso(), "metric": "acuerdo_dice", "agreement_config_ref": config_ref(acfg),
        "modelo_referencia": acfg["agreement"]["reference"], "runs": [r.name for r in runs], "n_windows": len(vals),
        "min": vals_sorted[0], "max": vals_sorted[-1], "media": round(sum(vals) / len(vals), 4),
        "piso": vals_sorted[0], "regla_piso": "mínimo del acuerdo por ventana en las ejecuciones de referencia",
        "por_ejecucion": per_run,
    }


def compare(base_dir: Path, other_dir: Path, acfg: dict) -> list[dict]:
    """Caso controlado pareado: ventana a ventana sobre las mismas muestras (seq) de dos ejecuciones. Una
    ventana con los mismos frames de entrada (SHA-256 de acuerdo.jsonl) debe dar diferencia 0 exacta; en las
    ventanas cuyos frames cambiaron, la diferencia es el efecto del cambio."""
    a, b = report(base_dir, acfg, None), report(other_dir, acfg, None)
    sha = lambda d: {r["seq"]: r.get("frame_sha256") for r in load_records(d / OUT)}
    sa, sb = sha(base_dir), sha(other_dir)
    size = json.loads((other_dir / "manifest.json").read_text(encoding="utf-8"))["config"]["windows"]["size_frames"]
    out = []
    for wb in b["ventanas"]:
        wa = next((w for w in a["ventanas"] if w["idx"] == wb["idx"]), None)
        seqs = range((wb["idx"] - 1) * size + 1, wb["idx"] * size + 1)
        same = sum(sa.get(q) is not None and sa.get(q) == sb.get(q) for q in seqs)
        va, vb = (wa or {}).get("acuerdo"), wb["acuerdo"]
        out.append({"idx": wb["idx"], "frames_identicos": same, "frames": len([q for q in seqs if q in sb]),
                    "base": va, "otra": vb, "diferencia": round(vb - va, 4) if None not in (va, vb) else None})
    return out


def load_reference(path: str | Path = REF_FILE) -> dict | None:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


# -- caso controlado: grabación degradada -------------------------------------------
def degrade(src: Path, dst: Path, desde: int, frames: int, sigma: float) -> dict:
    """Copia los primeros `frames` de una grabación; desde la muestra `desde` aplica desenfoque gaussiano σ
    (pérdida de nitidez: foco, lente sucia, compresión fuerte). Determinista; deja degradacion.json."""
    import cv2

    files = sorted(src.glob("*.jpg"))[:frames]
    dst.mkdir(parents=True, exist_ok=False)
    out = []
    for seq, f in enumerate(files, start=1):
        if seq >= desde:
            cv2.imwrite(str(dst / f.name), cv2.GaussianBlur(cv2.imread(str(f)), (0, 0), sigma),
                        [cv2.IMWRITE_JPEG_QUALITY, 95])
        else:
            shutil.copy2(f, dst / f.name)
        out.append({"seq": seq, "file": f.name, "degradado": seq >= desde, "src_sha256": sha256_file(f),
                    "sha256": sha256_file(dst / f.name)})
    meta = {"generated_at": utc_now_iso(), "origen": src.as_posix(), "frames": len(out), "desde_seq": desde,
            "transformacion": f"cv2.GaussianBlur sigma={sigma}, JPEG calidad 95", "archivos": out}
    (dst / "degradacion.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Escrito {dst}: {len(out)} frames, desenfoque sigma={sigma} desde la muestra {desde}")
    return meta


def print_report(name: str, d: dict) -> None:
    print(f"== {name}: acuerdo={d['valor']} (cobertura {d['cobertura_referencia']}, respaldo "
          f"{d['respaldo_produccion']}, sin clase {d['acuerdo_sin_clase']}) · {d['modelo_referencia']} · "
          f"bajo el piso {d['piso_referencia']}: {d['ventanas_bajo_referencia']}/{d['n']}")
    print("   " + " ".join(f"{w['idx']}:{w['acuerdo'] if w['acuerdo'] is not None else '-'}" for w in d["ventanas"]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("medir")
    m.add_argument("runs", nargs="+")
    m.add_argument("--modelo", default=None, help="modelo de referencia (por defecto agreement.reference)")
    m.add_argument("--salida", default=OUT)
    m.add_argument("--frames-dir", default=None, help="carpeta de frames si no es la del manifest")
    r = sub.add_parser("referencia")
    r.add_argument("runs", nargs="+")
    p = sub.add_parser("reporte")
    p.add_argument("runs", nargs="+")
    p.add_argument("--archivo", default=OUT)
    c = sub.add_parser("comparar")
    c.add_argument("base")
    c.add_argument("otra")
    g = sub.add_parser("degradar")
    g.add_argument("src")
    g.add_argument("dst")
    g.add_argument("--desde", type=int, required=True)
    g.add_argument("--frames", type=int, required=True)
    g.add_argument("--sigma", type=float, default=3.0)
    for s in (m, r, p, c):
        s.add_argument("--config", default=CONFIG)
        s.add_argument("--ref", default=REF_FILE)
    args = ap.parse_args()
    if args.cmd == "degradar":
        degrade(Path(args.src), Path(args.dst), args.desde, args.frames, args.sigma)
        return
    acfg = load_config(args.config)
    if args.cmd == "medir":
        for run in args.runs:
            measure(Path(run), acfg, args.modelo, args.salida, args.frames_dir)
    elif args.cmd == "comparar":
        for w in compare(Path(args.base), Path(args.otra), acfg):
            print(f"   ventana {w['idx']}: frames idénticos {w['frames_identicos']}/{w['frames']} · "
                  f"{w['base']} -> {w['otra']} (diferencia {w['diferencia']})")
    elif args.cmd == "referencia":
        ref = build_reference([Path(x) for x in args.runs], acfg)
        Path(args.ref).write_text(json.dumps(ref, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Escrito {args.ref}: {ref['n_windows']} ventanas, acuerdo {ref['min']}–{ref['max']} "
              f"(media {ref['media']}) -> piso {ref['piso']}")
    else:
        ref = load_reference(args.ref)
        for run in args.runs:
            print_report(Path(run).name, report(Path(run), acfg, ref, args.archivo))


if __name__ == "__main__":
    main()
