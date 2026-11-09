"""Carga y validación de la configuración; cálculo de config_ref."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml


def _merge(base: dict, over: dict) -> dict:
    """Mezcla profunda: los diccionarios se combinan; cualquier otro valor de `over` reemplaza."""
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _load_raw(path: Path) -> dict:
    """Lee un YAML; si declara `base: otro.yaml`, lo carga primero y aplica encima solo lo que cambia."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = raw.pop("base", None)
    return _merge(_load_raw(path.parent / base), raw) if base else raw


def load_config(path: str | Path) -> dict:
    cfg = _load_raw(Path(path))
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
    sh = cfg.get("source_health")
    if sh and (sh["consecutive_invalid"] < 1 or sh["recover_after_valid"] < 1):
        raise ValueError("source_health: consecutive_invalid y recover_after_valid deben ser >= 1")
    mv = cfg.get("model_version")
    if mv and (mv["consecutive_mismatch"] < 1 or mv["recover_after_match"] < 1):
        raise ValueError("model_version: consecutive_mismatch y recover_after_match deben ser >= 1")
    ih = cfg.get("inference_health")
    if ih and (ih["consecutive_errors"] < 1 or ih["recover_after_ok"] < 1
               or ih.get("reload_after", 1) < 1 or ih.get("reload_every", 1) < 1 or ih.get("max_reloads", 0) < 0):
        raise ValueError("inference_health: consecutive_errors, recover_after_ok, reload_after y reload_every deben "
                         "ser >= 1 y max_reloads >= 0")
    for name, alt in (cfg.get("model_alternatives") or {}).items():
        if not {"weights", "sha256"} <= set(alt):
            raise ValueError(f"model_alternatives.{name}: faltan weights o sha256")
    return cfg


def config_ref(cfg: dict) -> str:
    """Identificador estable de la configuración: hash del contenido normalizado.

    Excluye la sección `output` (rutas de salida), que no afecta muestreo, inferencia ni política.
    """
    relevant = {k: v for k, v in cfg.items() if k != "output"}
    canon = json.dumps(relevant, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "cfg-" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]


def expected_model_ref(cfg: dict) -> str:
    """model_ref que deben reportar los eventos según la configuración (nombre + SHA-256 declarado)."""
    return f"{cfg['model']['name']}@sha256:{cfg['model']['sha256'][:12]}"


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
