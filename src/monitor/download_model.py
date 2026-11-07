"""Descarga los pesos del modelo declarados en la configuración y verifica su SHA-256.

Uso: uv run python -m monitor.download_model --config config/monitor.yaml
"""

from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path

from monitor.config import load_config, sha256_file


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/monitor.yaml")
    args = ap.parse_args()
    m = load_config(args.config)["model"]
    dest = Path(m["weights"])
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"Descargando {m['url']} -> {dest}")
        urllib.request.urlretrieve(m["url"], dest)
    digest = sha256_file(dest)
    if digest != m["sha256"]:
        raise SystemExit(f"SHA-256 no coincide: {digest} != {m['sha256']}")
    print(f"OK {dest} sha256={digest}")


if __name__ == "__main__":
    main()
