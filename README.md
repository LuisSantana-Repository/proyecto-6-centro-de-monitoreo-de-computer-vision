# Proyecto 6: Centro de monitoreo de computer vision

Entrega individual. Fuente visual: cámara en vivo del cruce de Shibuya (YouTube,
`https://www.youtube.com/watch?v=dfVK7ld38Ys`), muestreada a ritmo fijo. Tarea: detección de
objetos con un modelo YOLO preentrenado (COCO), ejecutado en CPU.

> En construcción. Se completará con instalación, comandos, métricas, escenarios e índice de evidencias.

## Instalación

```bash
uv sync
```

## Comandos

| Para qué | Comando |
|---|---|
| Descargar y verificar los pesos del modelo | `uv run python -m monitor.download_model` |
| Monitorear la cámara en vivo (N frames, 1 cada 2 s) | `uv run python -m monitor.run --frames N --label <etiqueta>` |
| ... y grabar cada frame para reproducirlo después | `... --record` (se guarda en `datos/grabaciones/<run_id>/`) |
| Reproducir una grabación como stream | `uv run python -m monitor.run --replay datos/grabaciones/<run_id> --label <etiqueta>` |
| Reprocesar una grabación sin esperar (análisis) | `... --replay datos/grabaciones/<run_id> --no-pace` |
| Estadísticas de referencia y umbrales derivados | `uv run python -m monitor.reference datos/runs/<ref1> datos/runs/<ref2> --out config/referencia.json` |
| Recalcular ventanas y alertas desde `events.jsonl` y verificarlas | `uv run python -m monitor.evaluate datos/runs/<run_id>` |
| Importar runs existentes a SQLite (idempotente) | `uv run python -m monitor.ingest` |
| Portal de eventos y alertas | `uv run python -m monitor.portal` → http://127.0.0.1:8000 |
| Pruebas | `uv run pytest -q` |

Cada ejecución escribe `datos/runs/<run_id>/events.jsonl` (registro primario), `manifest.json`
(configuración, modelo, entorno y contadores de la fuente), las imágenes capturadas y los eventos
en `datos/monitor.db`.
