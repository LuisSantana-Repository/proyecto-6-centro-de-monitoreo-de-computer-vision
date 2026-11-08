"""Carga y validación de la configuración; cálculo de config_ref."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml


def load_config(path: str | Path) -> dict:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    lower, upper = cfg["policy"]["lower"], cfg["policy"]["upper"]
    if not (0 <= lower < upper <= 1):
        raise ValueError(f"Umbrales inválidos: se requiere 0 <= lower ({lower}) < upper ({upper}) <= 1")
    if cfg["model"]["conf_floor"] > lower:
        raise ValueError("conf_floor debe ser <= lower; si no, la zona de baja confianza queda oculta")
    floor = cfg["policy"].get("analysis_floor", cfg["model"]["conf_floor"])
    if not (cfg["model"]["conf_floor"] <= floor < lower):
        raise ValueError("Se requiere conf_floor <= analysis_floor < lower")
    if cfg["source"]["sample_interval_s"] <= 0:
        raise ValueError("sample_interval_s debe ser > 0")
    for rule in cfg.get("alerts", []):
        if rule["signal"] not in ("low_ratio", "mean_score") or rule["op"] not in (">", "<"):
            raise ValueError(f"Regla inválida: {rule['rule_id']}")
        if rule["consecutive"] < 1:
            raise ValueError(f"consecutive debe ser >= 1 en {rule['rule_id']}")
    return cfg


def config_ref(cfg: dict) -> str:
    """Identificador estable de la configuración: hash del contenido normalizado.

    Excluye la sección `output` (rutas de salida), que no afecta muestreo, inferencia ni política.
    """
    relevant = {k: v for k, v in cfg.items() if k != "output"}
    canon = json.dumps(relevant, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "cfg-" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
