# Índice de evidencias

**Estudiante:** Luis Santana · **Entrega:** individual · **Configuración:** `cfg-0b5e664b118c`
([`config/monitor.yaml`](../config/monitor.yaml)) · **Modelo:** `yolo11n@sha256:0ebbc80d4a76`
([`modelo/README.md`](../modelo/README.md)) · **Extras:** S1, S2, M1, M2, M3, F1, F2, F3, F4 y F5 ([`docs/extras.md`](../docs/extras.md);
sección [Extras](#extras) abajo).

## Revisiones Git

| Revisión | Qué contiene |
|---|---|
| `0862536` | Código con el que se procesaron las dos referencias (hitos 01–05). |
| `50b3b0a` | Código de C1 y C2 y todas las evidencias base. |
| `b74506e` | Extra S1: salud de la fuente, reconexión con espera creciente, inyección de cortes, S1-R. |
| `12d76dd` | Extras M1–M3 (`monitor.drift`, latencia de punta a punta, recuperación de la fuente). |
| `061ba82` | Extra F1: gráficas por ventana en el portal. |
| `a18c724` | Ejecuciones en vivo S1-V y S1-W, sus evidencias y el filtro de códigos ANSI en el registro de la fuente. |
| `2e97a73` | Extras S2 (regla de versión del modelo, estado `otra_version`, `--swap-model`), F2 (revisión humana) y F3 (exportación), con sus ejecuciones y evidencias. |
| `d00aeca` | Extra F4 (vista en vivo: estado publicado por `monitor.run`, `/vivo` con sus cuatro estados), con la ejecución `20261109T005919Z-f4-vivo` y las capturas 31–34. |
| `1572001` | Extra F5 (panel de operación `/panel`, reglas de atención y `monitor.dashboard --verificar`), con las capturas 35–38 y las verificaciones en `evidencias/panel/`. |

Respecto de `0862536`, `50b3b0a` agrega:
- la muestra de recursos del proceso por evento (`resources`, para G3);
- el cálculo de métricas (`monitor.metrics`);
- pruebas;
- un filtro de presentación del JSON en la vista de alerta.

Ninguno cambia la inferencia, las zonas, las ventanas ni las reglas. La configuración (`config_ref`)
es la misma en las cuatro ejecuciones base, y `monitor.evaluate` recalcula sus ventanas y alertas con
el código actual y coinciden. Los extras tampoco cambian nada de eso: agregan una regla de la fuente,
métricas y vistas. Las ejecuciones de S1 usan `cfg-3ebc8541e4f7`, que hereda la configuración base y
solo agrega los parámetros de la fuente. La de S2 usa `cfg-2b094007ccc7`, que hereda el mismo modelo
esperado y solo agrega la regla de versión y el artefacto alternativo.

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
| `20261108T193149Z-s1-replay-interrupcion` | extra **S1-R** | grabación de C1, corte inyectado a los 180 s durante 90 s (240 frames) | 1 frame / 2 s |
| `20261108T222800Z-s1-vivo-interrupcion` | extra **S1-V** (07:28 JST, lluvia) | cámara en vivo, corte inyectado a los 180 s durante 90 s (240 frames) | en vivo |
| `20261108T224433Z-s1-wifi` | extra **S1-W** (07:44 JST, lluvia) | cámara en vivo, WiFi apagado de verdad (240 frames) | en vivo |
| `20261108T232126Z-s2-cambio-version` | extra **S2** | grabación de C1; `yolo11s` en las muestras 76–180 (240 frames) | 1 frame / 2 s |
| `20261109T005919Z-f4-vivo` | extra **F4** (10:00 JST, lluvia) | cámara en vivo con la vista en vivo publicada (90 frames) | en vivo |

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
uv run pytest -q                                # 107 pruebas

# 1) Recalcular desde los registros primarios versionados (sin modelo)
uv run python -m monitor.evaluate datos/runs/20261108T163302Z-c2-baja-confianza   # ventanas y alertas: coinciden=True
uv run python -m monitor.metrics  datos/runs/20261108T163302Z-c2-baja-confianza   # G1–G7

# 2) Portal sobre las ejecuciones oficiales
uv run python -m monitor.ingest                 # crea datos/monitor.db desde datos/runs/
uv run python -m monitor.dashboard --verificar  # el panel (F5) coincide con los JSONL de cada ejecución
uv run python -m monitor.portal                 # http://127.0.0.1:8000 (abre en /panel)

# 3) Reproducir la inferencia sobre la muestra (60 frames = 1 ventana; ~1 min)
uv run python -m monitor.run --replay evidencias/entradas/c2-noche --no-pace --label muestra-c2
#    → misma ventana 1 que la oficial (prop_baja 0.754, media 0.414, 1247 elegibles) y la alerta baja-confianza-v1
#    (agrega la ejecución muestra-c2 a datos/runs/ y a la base; por eso va después del paso 2)
```

Comprobado en un clon limpio de `97469f3` (antes de los extras), descargado desde GitHub el 2026-11-08:
- 29 pruebas pasan (hoy son 107).
- `evaluate` coincide en las cuatro ejecuciones.
- Las métricas de C2 son idénticas a [`metricas/`](metricas/).
- La muestra de C2 reproduce la ventana 1 y su alerta.
- Todas las vistas del portal responden 200, y la API devuelve las 4 alertas.

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
| 12 | `/graficas?run_id=…c2-baja-confianza` | F1: gráficas por ventana de C2, con umbrales y alertas |
| 13 | `/graficas?run_id=…s1-replay-interrupcion` | F1 sobre S1-R: hueco "insuficiente" y alerta de la fuente |
| 14 | `/graficas?run_id=…c1-normal` | F1 sobre C1: todo dentro de los umbrales |
| 15 | `/runs/20261108T224433Z-s1-wifi` | S1-W: salud de la fuente (desconexión, errores de DNS, reconexión), ventana insuficiente |
| 16 | `/alertas/alert-…s1-wifi-fuente-interrumpida-v1-f000094` | alerta de la fuente: motivo, muestras sin frame y resolución (84.6 s) |
| 17 | `/graficas?run_id=…s1-wifi` | F1 sobre S1-W |
| 18 | `/runs/20261108T222800Z-s1-vivo-interrupcion` | S1-V: los 7 intentos de reconexión con su espera |
| 19 | `/graficas?run_id=…s1-vivo-interrupcion` | F1 sobre S1-V |
| 20 | `/runs/20261108T232126Z-s2-cambio-version` | S2: versiones reportadas (135 `yolo11n`, 105 `yolo11s`) y ventanas `otra_version` |
| 21 | `/alertas/alert-…s2-cambio-version-version-modelo-v1-f000076` | alerta de versión: esperada frente a reportada, evidencia y resolución (210 s, 105 eventos) |
| 22 | `/graficas?run_id=…s2-cambio-version` | F1 sobre S2: huecos `otra_version`; W1 de `yolo11s` dentro del umbral |
| 23 | `/revision?run_id=…s1-wifi` | F2: cola de capturas, avance y alertas de la ejecución con su estado de revisión |
| 24 | `/alertas/alert-…s1-wifi-fuente-interrumpida-v1-f000094` | F2: alerta reconocida y cerrada (`esperada`) con su historial |
| 25 | `/alertas/alert-…c2-baja-confianza-cambio-sostenido-v1-w0003` | F2: alerta abierta reconocida; no se ofrece cerrar |
| 26 | `/eventos/evt-…s1-wifi-f000001` | F2: captura revisada por el autor dos veces (historial) |
| 27 | `/alertas?revision=revisada` | F2: filtro de alertas por estado de revisión |
| 28 | `/exportar?preview=1&run_id=…s2…&model_ref=yolo11s…&class_name=person&min_score=0.5&nivel=deteccion` | F3: vista previa (146 detecciones, SHA-256, primeras filas) |
| 29 | `/exportar?preview=1&run_id=…c1-normal&status=inference_error` | F3: exportación vacía y registro de descargas |
| 30 | `/exportar?preview=1&…&since=ayer` | F3: error 422 con los filtros conservados |
| 31 | `/vivo` | F4: cámara en vivo en curso (frame con cajas, ventana 33/60 con valores parciales, fuente conectada) |
| 32 | `/vivo?run_id=…f4-vivo` | F4: la misma ejecución terminada, con su alerta de baja confianza |
| 33 | `/vivo` | F4: replay con corte inyectado: `fuente_caida`, antigüedad del último frame, alerta de la fuente abierta |
| 34 | `/vivo?run_id=…f4-demo-fallo` | F4: el mismo proceso detenido a la mitad: `estancada` |
| 35 | `/panel` | F5: panel sobre la base reconstruida desde lo versionado (9 ejecuciones, 18 puntos de atención) |
| 36 | `/panel?mode=live` | F5: filtro por modo: las 3 ejecuciones en vivo |
| 37 | `/panel?mode=live&since=ayer` | F5: error 422 con los filtros conservados |
| 38 | `/panel` (base de trabajo local) | F5: dos fallos reales detectados: `E2` (demo de F4 detenida) y `E4` (ejecución indexada sin carpeta) |

Respuestas JSON de la API en el mismo momento: [`portal/api/`](portal/api/).

## Extras

Detalle, definiciones y control de doble conteo: [`docs/extras.md`](../docs/extras.md).

| ID | Nivel | Evidencia principal |
|---|---|---|
| **S1** interrupción y recuperación de la fuente | 10 | ejecuciones S1-R, S1-V y S1-W (tabla de arriba); [`docs/escenario-s1.md`](../docs/escenario-s1.md#resultados); capturas [`15`](portal/15-run-s1-wifi.png)–[`19`](portal/19-graficas-s1-vivo.png); [`tests/test_source_health.py`](../tests/test_source_health.py) |
| **S2** cambio de versión del modelo | 10 | ejecución `20261108T232126Z-s2-cambio-version`; [`docs/escenario-s2.md`](../docs/escenario-s2.md#resultados); capturas [`20`](portal/20-run-s2.png)–[`22`](portal/22-graficas-s2.png); [`portal/api/alerta-version-s2.json`](portal/api/alerta-version-s2.json); [`tests/test_model_version.py`](../tests/test_model_version.py) |
| **F2** revisión humana de alertas y capturas | 15 | `datos/runs/*/revisiones.jsonl` (9 acciones); capturas [`23`](portal/23-revision-cola-s1-wifi.png)–[`27`](portal/27-alertas-por-revision.png); [`portal/api/revisiones.json`](portal/api/revisiones.json); [`tests/test_review.py`](../tests/test_review.py) |
| **F3** exportación de eventos | 10 | [`exportaciones/`](exportaciones/) (4 archivos + registro, verificados con `monitor.export --verificar`); capturas [`28`](portal/28-exportar-vista-previa.png)–[`30`](portal/30-exportar-error.png); [`tests/test_export.py`](../tests/test_export.py) |
| **F4** vista en vivo | 15 | ejecución `20261109T005919Z-f4-vivo`; capturas [`31`](portal/31-vivo-en-curso.png)–[`34`](portal/34-vivo-estancada.png); [`tests/test_live.py`](../tests/test_live.py) |
| **F5** panel de operación | 10 | capturas [`35`](portal/35-panel.png)–[`38`](portal/38-panel-base-local.png); [`panel/`](panel/) (verificación contra los JSONL: coincide en la base reconstruida, falla en la local); [`portal/api/panel.json`](portal/api/panel.json); [`tests/test_dashboard.py`](../tests/test_dashboard.py) |
| **M1** desplazamiento de la distribución (W1) | 8 | [`config/referencia-distribucion.json`](../config/referencia-distribucion.json); `metricas_adicionales` en [`metricas/`](metricas/); [`tests/test_drift.py`](../tests/test_drift.py) |
| **M2** latencia de punta a punta | 5 | `metricas_adicionales.M2_latencia_e2e` y `ventanas[].M2_e2e_p95_ms` en [`metricas/`](metricas/) |
| **M3** recuperación de la fuente y disponibilidad | 5 | `metricas_adicionales.M3_recuperacion_fuente` de las tres ejecuciones de S1 |
| **F1** gráficas por ventana en el portal | 10 | capturas [`12`](portal/12-graficas-c2.png), [`13`](portal/13-graficas-s1.png), [`14`](portal/14-graficas-c1.png), [`17`](portal/17-graficas-s1-wifi.png), [`19`](portal/19-graficas-s1-vivo.png); [`portal/api/graficas-s1-vivo.json`](portal/api/graficas-s1-vivo.json); [`tests/test_portal.py`](../tests/test_portal.py) |

Para recalcular S1 desde los registros versionados:

```bash
uv run python -m monitor.evaluate datos/runs/20261108T224433Z-s1-wifi   # incluye la alerta de la fuente
uv run python -m monitor.metrics  datos/runs/20261108T224433Z-s1-wifi   # M3: 84.6 s, disponibilidad 0.8792
uv run python -m monitor.evaluate datos/runs/20261108T232126Z-s2-cambio-version                          # S2: alerta de versión
uv run python -m monitor.evaluate datos/runs/20261108T232126Z-s2-cambio-version --sin-reaccion-version   # S2: contrafactual
```
