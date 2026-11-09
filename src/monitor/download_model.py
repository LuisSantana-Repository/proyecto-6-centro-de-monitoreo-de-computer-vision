"""Descarga los pesos del modelo declarados en la configuración y verifica su SHA-256.

Uso: uv run python -m monitor.download_model [--config config/escenario-version.yaml]
"""

from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path

from monitor.config import load_config, sha256_file


def fetch(name: str, weights: str, url: str, sha256: str) -> None:
    dest = Path(weights)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"Descargando {url} -> {dest}")
        urllib.request.urlretrieve(url, dest)
    digest = sha256_file(dest)
    if digest != sha256:
        raise SystemExit(f"SHA-256 de {name} no coincide: {digest} != {sha256}")
    print(f"OK {name}: {dest} sha256={digest}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/monitor.yaml")
    args = ap.parse_args()
    cfg = load_config(args.config)
    m = cfg["model"]
    fetch(m["name"], m["weights"], m["url"], m["sha256"])
    # escenario S2: artefactos alternativos declarados (p. ej. config/escenario-version.yaml)
    for name, alt in (cfg.get("model_alternatives") or {}).items():
        fetch(name, alt["weights"], alt["url"], alt["sha256"])

if __name__ == "__main__":
    main()
