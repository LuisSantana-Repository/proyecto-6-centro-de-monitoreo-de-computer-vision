# Ficha del modelo

| Campo | Valor |
|---|---|
| Modelo | YOLO11n (detección de objetos), preentrenado en COCO por Ultralytics. No se reentrenó ni se ajustó. |
| Artefacto | `modelo/yolo11n.pt` (no se versiona en Git: 5.4 MB, se descarga y verifica) |
| Origen | https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt |
| SHA-256 | `0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1` |
| Identidad en eventos | `model_ref = yolo11n@sha256:0ebbc80d4a76` |
| Licencia | AGPL-3.0 (Ultralytics). Uso académico; el código de este repositorio solo invoca la biblioteca. |
| Clases monitoreadas | COCO 0 `person`, 1 `bicycle`, 2 `car`, 3 `motorcycle`, 5 `bus`, 7 `truck` |
| Tarea / salida | Por detección: caja `xyxy` en píxeles de la imagen original, clase y puntuación. |

## Obtener y verificar el artefacto

```bash
uv run python -m monitor.download_model   # descarga si falta y verifica el SHA-256
```

`monitor.run` también verifica el SHA-256 antes de procesar y se detiene si no coincide.

## Preprocesamiento e inferencia (fijados en `config/monitor.yaml`)

| Parámetro | Valor | Nota |
|---|---|---|
| Entrada | frame BGR 1280×720 (stream de YouTube, formato 232) | en replay: JPEG grabado |
| `imgsz` | 1280 | letterbox de Ultralytics a múltiplo de 32; RGB, normalización /255 |
| `conf_floor` | 0.05 | piso de salida bajo para observar las zonas de duda y baja confianza |
| `nms_iou` | 0.7 | NMS por clase (no agnóstico) |
| `max_det` | 300 | |
| `device` | cpu | sin GPU |

Justificación de `imgsz` y de los umbrales de la política: [`docs/umbrales.md`](../docs/umbrales.md).

## Entorno de ejecución de las evidencias

Windows 11 (10.0.26200), CPU Intel 12.ª gen. (Family 6 Model 154), sin CUDA; Python 3.12.10,
torch 2.15.0+cpu, ultralytics 8.4.207, opencv-python 4.14.0. Cada ejecución registra su entorno exacto
en `manifest.json` → `environment`. Versiones fijadas en `uv.lock`.

## Artefacto alternativo del escenario S2 (no es el modelo del monitor)

El escenario adicional S2 ([`docs/escenario-s2.md`](../docs/escenario-s2.md)) simula un despliegue que
carga otro modelo sin actualizar la configuración. El artefacto que aparece en su lugar también tiene
identidad resoluble y se verifica igual:

| Campo | Valor |
|---|---|
| Modelo | YOLO11s (Ultralytics, COCO), mismos parámetros de inferencia que arriba |
| Artefacto | `modelo/yolo11s.pt` (18.4 MB, no se versiona) |
| Origen | https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11s.pt |
| SHA-256 | `85a76fe86dd8afe384648546b56a7a78580c7cb7b404fc595f97969322d502d5` |
| Identidad en eventos | `model_ref = yolo11s@sha256:85a76fe86dd8` |
| Declarado en | `model_alternatives` de [`config/escenario-version.yaml`](../config/escenario-version.yaml) |

```bash
uv run python -m monitor.download_model --config config/escenario-version.yaml   # descarga y verifica ambos
```
