# Proyecto 6: Centro de monitoreo de computer vision

Entrega individual. Fuente visual: cámara en vivo del cruce de Shibuya (YouTube,
`https://www.youtube.com/watch?v=dfVK7ld38Ys`), muestreada a ritmo fijo. Tarea: detección de
objetos con un modelo YOLO preentrenado (COCO), ejecutado en CPU.

> En construcción. Se completará con instalación, comandos, métricas, escenarios e índice de evidencias.

## Instalación

```bash
uv sync
```

## Primer recorrido (hito 01)

```bash
uv run python -m monitor.run --config config/monitor.yaml --frames 10
```
