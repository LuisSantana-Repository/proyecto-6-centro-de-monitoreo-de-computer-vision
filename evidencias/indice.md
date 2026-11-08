# Índice de evidencias

**Estudiante:** Luis Santana · **Entrega:** individual · **Configuración:** `cfg-0b5e664b118c`
([`config/monitor.yaml`](../config/monitor.yaml)) · **Modelo:** `yolo11n@sha256:0ebbc80d4a76`
([`modelo/README.md`](../modelo/README.md)) · **Extras:** ninguno reclamado.

## Revisiones Git

| Revisión | Qué contiene |
|---|---|
| `0862536` | Código con el que se procesaron las dos referencias (hitos 01–05). |
| `50b3b0a` | Código de C1 y C2 y todas las evidencias. |

Respecto de `0862536`, `50b3b0a` agrega:
- la muestra de recursos del proceso por evento (`resources`, para G3);
- el cálculo de métricas (`monitor.metrics`);
- pruebas;
- un filtro de presentación del JSON en la vista de alerta.

Ninguno cambia la inferencia, las zonas, las ventanas ni las reglas. La configuración (`config_ref`)
es la misma en las cuatro ejecuciones, y `monitor.evaluate` recalcula sus ventanas y alertas con el
código actual y coinciden.

## Ejecuciones oficiales

Cada ejecución está versionada completa en [`datos/runs/`](../datos/runs/), así que los `image_ref`
de los eventos resuelven tal cual. Cada carpeta contiene:
- `events.jsonl`: registro primario;
- `windows.jsonl` y `alerts.jsonl`;
- `manifest.json`: configuración, modelo, entorno y contadores de la fuente;
- `imagenes/`.

| Ejecución | Papel | Entrada (grabación) | Modo |
|---|---|---|---|
| `20261108T155809Z-referencia-manana` | referencia (07:04 JST) | `datos/grabaciones/20261107T220327Z-referencia` (900 frames) | `--no-pace` |
| `20261108T160249Z-referencia-tarde` | referencia (14:32 JST) | `datos/grabaciones/20261108T053156Z-referencia-tarde` (900 frames) | `--no-pace` |
| `20261108T161753Z-c1-normal` | **C1** (14:09 JST) | `datos/grabaciones/20261108T050904Z-c1-normal` (450 frames) | 1 frame / 2 s |
| `20261108T163302Z-c2-baja-confianza` | **C2** (00:19 JST, noche) | `datos/grabaciones/20261108T151924Z-c2-noche` (450 frames) | 1 frame / 2 s |

Las grabaciones completas (≈ 850 MB) no se versionan. Una muestra de 60 frames, la primera
ventana de C1 y de C2, está en [`entradas/`](entradas/).

## Criterios → evidencia

| Criterio | Dónde verificarlo |
|---|---|
| **R1.1** fuente como stream, muestreo | [`src/monitor/source.py`](../src/monitor/source.py) (yt-dlp + hilo lector, 1 frame / 2 s); `manifest.json → source_stats` (0 frames descartados en C1/C2); G2 = 0.4999 FPS |
| **R1.2** inferencia real atribuible | `events.jsonl` (detecciones con `model_ref`); `entradas/c2-noche` reproduce 60/60 eventos idénticos |
| **R1.3** modelo, clases, preprocesamiento, entorno | [`modelo/README.md`](../modelo/README.md); `manifest.json → model_sha256, class_names, environment` |
| **R2** G1–G7 (M1–M5) | [`docs/metricas.md`](../docs/metricas.md); resultados en [`metricas/`](metricas/) (por ejecución y por ventana); [`tests/test_metrics.py`](../tests/test_metrics.py) |
| **R3.1** dos umbrales justificados | `policy` en `config/monitor.yaml` (0.50 / 0.65); [`config/referencia.json`](../config/referencia.json); [`docs/umbrales.md`](../docs/umbrales.md) §1–4 |
| **R3.2** tres zonas, baja confianza conservada, frames sin detecciones | `detections[].zone` y `zone_counts` en cada evento (piso de salida 0.05); estado `no_detections` + captura `sin_detecciones` ([`tests/test_capture.py`](../tests/test_capture.py), [`tests/test_portal.py`](../tests/test_portal.py)) |
| **R3.3** referencia, ventanas, mínimo, cambio sostenido, insuficiente | [`docs/umbrales.md`](../docs/umbrales.md) §5–6; `windows.jsonl` (`state`: normal / fuera_de_rango / insuficiente / incompleta); [`tests/test_alerts.py`](../tests/test_alerts.py) |
| **R4.1** contrato del evento | [`src/monitor/events.py`](../src/monitor/events.py); p. ej. [`portal/api/evento-c2-f000018.json`](portal/api/evento-c2-f000018.json) |
| **R4.2** persistencia y consulta | SQLite (`monitor.ingest`); capturas [`portal/04`](portal/04-eventos-c1.png), [`05`](portal/05-evento-c1.png), [`06`](portal/06-eventos-c2-baja-confianza.png), [`07`](portal/07-evento-c2.png) |
| **R4.3** imágenes con política de captura | [`docs/umbrales.md`](../docs/umbrales.md) §7; `capture_reason` + `image_ref` en eventos; G5 = 1.0 (todos los `image_ref` resuelven) |
| **R5.1** reglas explícitas | `alerts` en `config/monitor.yaml`; [`docs/umbrales.md`](../docs/umbrales.md) §6 |
| **R5.2** alertas reales vinculadas | `alerts.jsonl` de C2 (regla, `config_ref`, ventanas, `event_ids`, `image_refs`); `monitor.evaluate` coincide |
| **R5.3** alertas en el portal | [`portal/08`](portal/08-alertas.png), [`09`](portal/09-alerta-cambio-sostenido.png), [`10`](portal/10-alerta-baja-confianza.png) |
| **R6.1** C1 | ejecución `20261108T161753Z-c1-normal`: 0 alertas, 7 ventanas normales; [`metricas/20261108T161753Z-c1-normal.json`](metricas/20261108T161753Z-c1-normal.json); [`portal/02`](portal/02-run-c1.png) |
| **R6.2** C2 | ejecución `20261108T163302Z-c2-baja-confianza`: 4 alertas (tabla abajo), 93 imágenes; [`portal/03`](portal/03-run-c2.png), [`07`](portal/07-evento-c2.png), [`09`](portal/09-alerta-cambio-sostenido.png) |
| **R7** | [`README.md`](../README.md) (instalación, comandos, autoría, componentes reutilizados, límites); este índice |

### IDs de ejemplo

| Qué | ID |
|---|---|
| Evento C1 con imagen (muestra normal) | `evt-20261108T161753Z-c1-normal-f000015` |
| Evento C2 con imagen (`baja_confianza`, 72 de 74 detecciones en zona baja) | `evt-20261108T163302Z-c2-baja-confianza-f000018` |
| Alerta crítica de cambio sostenido (abierta) | `alert-20261108T163302Z-c2-baja-confianza-cambio-sostenido-v1-w0003` |
| Alerta de proporción (resuelta en la ventana 3) | `alert-20261108T163302Z-c2-baja-confianza-baja-confianza-v1-w0001` |
| Alertas de proporción posteriores | `...-baja-confianza-v1-w0004` (resuelta), `...-baja-confianza-v1-w0006` (abierta) |

## Reproducir o recalcular

Desde un clon limpio:

```bash
uv sync
uv run python -m monitor.download_model         # pesos verificados por SHA-256
uv run pytest -q                                # 29 pruebas

# 1) Recalcular desde los registros primarios versionados (sin modelo)
uv run python -m monitor.evaluate datos/runs/20261108T163302Z-c2-baja-confianza   # ventanas y alertas: coinciden=True
uv run python -m monitor.metrics  datos/runs/20261108T163302Z-c2-baja-confianza   # G1–G7

# 2) Reproducir la inferencia sobre la muestra (60 frames = 1 ventana; ~1 min)
uv run python -m monitor.run --replay evidencias/entradas/c2-noche --no-pace --label muestra-c2
#    → misma ventana 1 que la oficial (prop_baja 0.754, media 0.414, 1247 elegibles) y la alerta baja-confianza-v1

# 3) Portal sobre las ejecuciones oficiales
uv run python -m monitor.ingest                 # crea datos/monitor.db desde datos/runs/
uv run python -m monitor.portal                 # http://127.0.0.1:8000
```

## Recorrido del portal

Las capturas están en [`portal/`](portal/); se tomaron con Edge headless sobre el portal en local.

| # | URL | Qué muestra |
|---|---|---|
| 01 | `/runs` | las cuatro ejecuciones oficiales con su configuración y conteos |
| 02 | `/runs/20261108T161753Z-c1-normal` | C1: ventanas normales, sin alertas, contadores de la fuente, entorno |
| 03 | `/runs/20261108T163302Z-c2-baja-confianza` | C2: ventanas fuera de rango y condiciones por regla, 4 alertas |
| 04 | `/eventos?run_id=…c1-normal&with_image=true` | filtro de eventos con imagen |
| 05 | `/eventos/evt-…c1-normal-f000015` | detalle: imagen con cajas por zona, detecciones, tiempos, modelo, configuración |
| 06 | `/eventos?run_id=…c2-baja-confianza&reason=baja_confianza` | filtro por motivo de captura |
| 07 | `/eventos/evt-…c2-baja-confianza-f000018` | frame nocturno: 1 aceptada, 1 duda, 72 bajas; alertas que lo referencian |
| 08 | `/alertas` | lista con severidad, estado, regla y momento |
| 09 | `/alertas/alert-…cambio-sostenido-v1-w0003` | motivo, acción sugerida, métrica por ventana, eventos e imágenes de la evidencia |
| 10 | `/alertas/alert-…baja-confianza-v1-w0001` | alerta de proporción y su resolución |
| 11 | `/config/cfg-0b5e664b118c` | configuración completa usada por los eventos |

Respuestas JSON de la API en el mismo momento: [`portal/api/`](portal/api/).
