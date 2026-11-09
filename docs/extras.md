# Inventario de extras

Las métricas base de G1–G7 ([`docs/metricas.md`](metricas.md)), los escenarios C1 y C2 y las vistas
mínimas del portal **no** se reclaman como extras. Cada extra tiene un ID, su propia evidencia y una
forma de verificarlo.

| ID | Tipo | Descripción | Nivel solicitado | Estado |
|---|---|---|---|---|
| **S1** | Escenario | Interrupción y recuperación de la fuente | 10 | completo: S1-R, S1-V y S1-W |
| **S2** | Escenario | Cambio de versión del modelo no declarado, reacción y reversión | 10 | completo |
| **M1** | Métrica | Desplazamiento de la distribución de puntuaciones frente a la referencia (Wasserstein-1) | 8 | completo |
| **M2** | Métrica | Latencia de punta a punta (frame → evento), p50/p95 por ejecución y por ventana | 5 | completo |
| **M3** | Métrica | Tiempo de recuperación de la fuente y disponibilidad por ventana | 5 | completo (con S1-R, S1-V y S1-W) |
| **F1** | Funcionalidad | Gráficas por ventana en el portal (proporción baja, media y W1, con umbrales, huecos y alertas) | 10 | completo |
| **F2** | Funcionalidad | Revisión humana de alertas y capturas: estados coordinados con las reglas, persistencia trazable, cola de revisión | 15 | completo |
| **F3** | Funcionalidad | Exportación de eventos y detecciones (CSV/JSONL) con filtros, procedencia y verificación | 10 | completo |

---

## S1 · Interrupción y recuperación de la fuente (escenario, nivel 10)

Fallo, diagnóstico, reacción, recuperación y verificación posterior, con evidencia de cada etapa.
Detalle completo, variantes y cómo reproducirlo: [`docs/escenario-s1.md`](escenario-s1.md).

- **Por qué no es C1 ni C2.** Prueba la disponibilidad de la fuente, no la confianza. Agrega su
  propia regla (`fuente-interrumpida-v1`), un registro de diagnóstico (`fuente.jsonl`) y la
  reconexión con espera creciente.
- **Tres variantes ejecutadas:**

  | Variante | Ejecución | Fallo | Caída (`downtime_s`) | Ventana del corte |
  |---|---|---|---|---|
  | S1-R | `20261108T193149Z-s1-replay-interrupcion` | inyectado en replay (determinista) | 90.0 s | insuficiente |
  | S1-V | `20261108T222800Z-s1-vivo-interrupcion` | inyectado en vivo; 8 intentos de reconexión | 92.0 s | insuficiente |
  | S1-W | `20261108T224433Z-s1-wifi` | WiFi apagado de verdad; falla de DNS; 7 intentos | 84.6 s | insuficiente |

  Cada una abre **una** alerta crítica `fuente-interrumpida-v1`, que se resuelve sola, y
  `monitor.evaluate` coincide. En S1-W el monitor detectó la caída 5 s antes que el lector.
- **Evidencia:** `fuente.jsonl`, `alerts.jsonl` y `windows.jsonl` de cada ejecución; capturas del
  portal [`15`](../evidencias/portal/15-run-s1-wifi.png)–[`19`](../evidencias/portal/19-graficas-s1-vivo.png);
  [`tests/test_source_health.py`](../tests/test_source_health.py). Resultados por etapa en
  [`docs/escenario-s1.md`](escenario-s1.md#resultados).

## S2 · Cambio de versión del modelo no declarado (escenario, nivel 10)

Un despliegue reemplaza `yolo11n` por `yolo11s` en caliente, sin actualizar la configuración, y luego
se revierte. Detalle, reproducción y límites: [`docs/escenario-s2.md`](escenario-s2.md).

- **Por qué no es C1, C2 ni S1.** La fuente está sana y los eventos son válidos; cambia *qué modelo*
  los produce. Agrega su propia regla (`version-modelo-v1`), que compara el `model_ref` reportado
  con el configurado, y una reacción en las ventanas (estado `otra_version`).
- **Resultado** (`20261108T232126Z-s2-cambio-version`, replay de C1):
  - una alerta crítica a los 3 eventos del cambio, resuelta tras la reversión, con
    `duration_s` 210.0 y 105 eventos;
  - las ventanas 2–3 quedan `otra_version`, la 4 vuelve a `normal`;
  - G6 = 0.5625 y G5 = 1.0;
  - `monitor.evaluate` coincide.
- **Verificación posterior.** Antes del cambio y después de la reversión, las detecciones son
  idénticas a las de C1 en las mismas muestras: 75/75 y 60/60.
- **Por qué hace falta.** Sin la reacción, las ventanas de `yolo11s` habrían salido `normal` y W1
  queda dentro de su umbral, aunque los conteos sobre los mismos frames suben entre 21 % y 35 %.
  Ninguna señal de confianza delata el cambio; solo la identidad del modelo.
- **Evidencia:** registros de la ejecución, capturas
  [`20`](../evidencias/portal/20-run-s2.png)–[`22`](../evidencias/portal/22-graficas-s2.png),
  [`tests/test_model_version.py`](../tests/test_model_version.py) y el contrafactual
  `monitor.evaluate --sin-reaccion-version`.

## M1 · Desplazamiento de la distribución frente a la referencia (métrica, nivel 8)

Compara la distribución de las puntuaciones contra la referencia, con supuestos y límites
declarados y un caso controlado de cambio verificado.
Código: [`src/monitor/drift.py`](../src/monitor/drift.py).

| | |
|---|---|
| **Definición** | Wasserstein-1 entre las puntuaciones elegibles de una ventana y las de la referencia: W1 = ∫ \|Q_ventana(u) − Q_ref(u)\| du, aproximada con 1000 puntos de la función cuantil. Unidad: puntos de confianza; un corrimiento uniforme de δ da W1 = δ. |
| **Población y exclusiones** | Detecciones con s ≥ 0.25 en frames con inferencia válida (la de G4). Solo ventanas completas y suficientes (≥ 45 frames válidos y ≥ 300 elegibles); si no, `null`. Sin ventanas suficientes, el valor de la ejecución es `null` / `sin_datos`. |
| **Referencia** | Las dos ejecuciones de referencia de los umbrales (30 ventanas, 44 998 puntuaciones). Se guarda su función cuantil en [`config/referencia-distribucion.json`](../config/referencia-distribucion.json): `uv run python -m monitor.drift --build <ref1> <ref2>`. |
| **Umbral** | 0.048: el máximo de W1 de cada ventana de referencia contra el resto de la referencia, dejando fuera esa ventana. La media es 0.0295 ± 0.0104. |
| **Caso controlado** | Desplazar la referencia δ = 0.02 / 0.05 / 0.10 da W1 = 0.020 / 0.050 / 0.100 ([`tests/test_drift.py`](../tests/test_drift.py)). |
| **Resultado** | Referencias: 0/30 ventanas sobre el umbral. **C1: 0/7** (máx. 0.042). **C2: 6/7** (W1 de la ejecución 0.0615, el doble que C1, 0.0282). S1-R: 0/3, con su ventana insuficiente en `null`. S1-V: 1/3 y S1-W: 3/3 (W1 0.0654), ambas con lluvia en Tokio, en línea con sus alertas de confianza. S2: 0/4. Las ventanas de `yolo11s` (0.0276 y 0.0166) quedan dentro del umbral: W1 compara puntuaciones, no la identidad del modelo, y por eso S2 tiene su propia regla. |
| **Recálculo** | `uv run python -m monitor.drift datos/runs/<run_id>`; también en `metricas_adicionales` de `evidencias/metricas/<run_id>.json`. |
| **Supuestos y límites** | La referencia mezcla una mañana con poca gente y una tarde con multitud, así que el umbral incluye esa variación normal. Las detecciones de un mismo frame no son independientes, por eso W1 no se usa como prueba estadística, solo como distancia con umbral empírico. El umbral sale de 30 ventanas. W1 resume la distribución completa: complementa la media de la regla `cambio-sostenido-v1` (capta cambios de forma, no solo de nivel), pero en C2 ambas reaccionan a lo mismo. |
| **Alternativa descartada** | PSI con deciles de la referencia: depende de cómo se agrupan las puntuaciones, y la mezcla mañana/tarde lo infla (la referencia llega a 0.22 por ventana). Separaba peor: C2 solo 3/7 ventanas sobre el máximo de la referencia. |

## M2 · Latencia de punta a punta (métrica, nivel 5)

| | |
|---|---|
| **Definición** | `e2e_ms`: del momento en que la fuente entrega el frame (`t_capture`) a la creación del evento. Incluye preprocesamiento, inferencia, posprocesamiento, zonas y captura de imagen; excluye la escritura del evento. Se reporta p50 / p95 / máximo por ejecución y p95 por ventana. |
| **Distinta de G1** | G1 mide solo `inference_ms`, el modelo. M2 mide la etapa completa del monitor. En C1 la diferencia de p50 es 349 − 315 ≈ 34 ms. |
| **Población** | Eventos con inferencia válida; los `invalid_input` se excluyen, porque su espera es de la fuente y no del procesamiento. Sin eventos válidos: `null`. |
| **Resultado** | C1 349 / 488 ms (p50 / p95); C2 338 / 445 ms; referencias ≈ 250 / 330 ms (`--no-pace`). S1-V 108 / 128 ms y S1-W 115 / 140 ms: en vivo y con la laptop conectada a la corriente, así que no se comparan con C1 (ver los límites de [`escenario-s1.md`](escenario-s1.md#límites)). Lo que queda fuera del modelo (M2 − G1, p50) es unos 9 ms en S1-V y S1-W, frente a 34 ms en C1. |
| **Recálculo** | `monitor.metrics` → `metricas_adicionales.M2_latencia_e2e` y `ventanas[].M2_e2e_p95_ms`; [`tests/test_metrics.py`](../tests/test_metrics.py). |

## M3 · Tiempo de recuperación de la fuente y disponibilidad (métrica, nivel 5)

| | |
|---|---|
| **Definición** | Para cada incidente resuelto de `fuente-interrumpida-v1`: segundos del primer frame perdido al primer frame recibido (`downtime_s`). Se reporta la media y el máximo por ejecución, y los incidentes abiertos al final. La disponibilidad es la proporción de muestras con frame, por ejecución y por ventana. |
| **Correlación de registros** | Incidente (`alerts.jsonl`, alerta + resolución) ↔ eventos perdidos (`n_invalid`, `first_ok_event_id` en `events.jsonl`) ↔ diagnóstico de la fuente (`fuente.jsonl`: `desconectado` → `reconectado`, `caida_s`). |
| **Cero observaciones** | Sin incidentes, el tiempo de recuperación es `null` / `sin_datos`, no 0, que sugeriría una recuperación instantánea. Un incidente abierto al terminar no entra en la media; se cuenta en `incidentes_abiertos`. |
| **Resultado** | S1-R: 1 incidente, **90.0 s**, 45 muestras perdidas; disponibilidad 0.8125, con 0.50 en la ventana 2 y 0.75 en la 3. S1-V: **92.0 s**, 31 muestras, disponibilidad 0.8708 (0.48 en la ventana 2). S1-W: **84.6 s**, 28 muestras, disponibilidad 0.8792 (0.55 en la ventana 2, 0.97 en la 3). C1, C2 y referencias: sin incidentes, disponibilidad 1.0. |
| **Recálculo** | `monitor.metrics` → `metricas_adicionales.M3_recuperacion_fuente` y `ventanas[].M3_disponibilidad_fuente`; [`tests/test_metrics.py`](../tests/test_metrics.py). |

## F1 · Gráficas por ventana en el portal (funcionalidad, nivel 10)

Flujo con consulta parametrizada, persistencia pertinente y verificación de resultados y de estados
vacíos o erróneos. Código: [`src/monitor/portal/charts.py`](../src/monitor/portal/charts.py) y la ruta
`/graficas` en [`app.py`](../src/monitor/portal/app.py).

| | |
|---|---|
| **Qué hace** | `/graficas?run_id=<id>`: por ejecución, tres gráficas por ventana, una por señal y cada una con su escala: proporción de baja confianza, puntuación media y W1 (M1). Cada una tiene su umbral rotulado, el último valor rotulado y un tooltip por ventana. Las ventanas insuficientes o incompletas son un hueco con etiqueta, nunca 0. Los puntos que cruzan el umbral se dibujan huecos, una codificación de forma además de color. Las alertas se marcan en la ventana donde se abrieron: cada gráfica, las de su regla y las de la fuente. Hay selector de ejecución y enlaces a la tabla de ventanas y al JSON. |
| **Datos** | Se leen de SQLite (`windows`, `alerts`, `events`, `configs`), en modo de solo lectura. W1 se calcula desde las detecciones guardadas contra `config/referencia-distribucion.json`. El umbral de cada gráfica sale de la configuración de esa ejecución (`config_ref`), no de la vigente. |
| **API** | `/api/runs/<id>/graficas` devuelve las mismas series en JSON. Para C2, sus valores de W1 coinciden con `monitor.drift`. |
| **Estados vacíos o erróneos** | Sin ejecuciones: mensaje. Ejecución sin ventanas: mensaje. Señal sin ventanas suficientes o sin referencia de W1: estado vacío con el motivo. Ejecución inexistente: 404 en HTML y API. Probado en [`tests/test_portal.py`](../tests/test_portal.py): `test_charts_series_gaps_and_alert_marks` y `test_charts_empty_and_error_states`. |
| **Diseño** | Una serie por gráfica y nunca dos ejes. Color validado de la paleta de referencia (serie 1), rejilla recesiva y el umbral en tinta. La identidad nunca depende solo del color. |
| **Evidencia** | [`evidencias/portal/12-graficas-c2.png`](../evidencias/portal/12-graficas-c2.png) (C2: alertas de proporción en las ventanas 1, 4 y 6; cambio sostenido en la 3; W1 sobre el umbral en 6/7), [`13-graficas-s1.png`](../evidencias/portal/13-graficas-s1.png) (S1-R: hueco "insuficiente" y alerta de la fuente en la ventana 2), [`14-graficas-c1.png`](../evidencias/portal/14-graficas-c1.png) (C1: todo dentro de los umbrales), [`17-graficas-s1-wifi.png`](../evidencias/portal/17-graficas-s1-wifi.png) y [`19-graficas-s1-vivo.png`](../evidencias/portal/19-graficas-s1-vivo.png) (en vivo: hueco del corte, alerta de la fuente y alertas de confianza por la lluvia, cada una en su gráfica), [`22-graficas-s2.png`](../evidencias/portal/22-graficas-s2.png) (S2: huecos `otra_version` y alerta de versión). |

## F2 · Revisión humana de alertas y capturas (funcionalidad, nivel 15)

Flujo compuesto con coordinación de estados, persistencia y trazabilidad, más casos normales y de
borde o fallo reproducibles. Código: [`src/monitor/review.py`](../src/monitor/review.py), con las rutas
`/revision`, `POST /alertas/<id>/revision` y `POST /eventos/<id>/revision` en
[`app.py`](../src/monitor/portal/app.py).

| | |
|---|---|
| **Qué hace** | Una persona **reconoce** una alerta, la **comenta** y **cierra su revisión** con un veredicto (`confirmada`, `esperada`, `falsa_alarma`) y una nota. También **revisa capturas**: a cada imagen le pone una etiqueta de un conjunto fijo (`sin_problema`, `iluminacion`, `clima`, `oclusion`, `camara`, `otro`) y una nota. `/revision` es la cola de una ejecución: capturas pendientes o revisadas con filtros por estado, motivo de captura y etiqueta, el avance (revisadas de un total, conteo por etiqueta) y las alertas con su estado de revisión. `/alertas` filtra por estado de revisión. |
| **Coordinación de estados** | La revisión tiene su propio estado: `pendiente` → `reconocida` → `revisada`. Es independiente de `open`/`resolved`, que decide la regla y que la revisión nunca modifica. **Cerrar exige que la regla ya haya resuelto la alerta**: una persona no puede dar por terminada una condición que sigue activa (409), y el portal no ofrece ese botón mientras la alerta está abierta. Cada registro guarda el estado de la alerta en ese momento (`alert_status`). |
| **Persistencia y trazabilidad** | Cada acción es un registro inmutable con `review_id`, `request_id`, hora, operador, acción, estado antes y después, veredicto o etiqueta, nota, regla, `config_ref` e `image_ref`. Se escribe primero en `datos/runs/<run_id>/revisiones.jsonl` (registro primario, versionado con la ejecución) y después en SQLite (tabla `reviews`). El estado vigente se obtiene plegando el historial, así que `monitor.ingest` lo reconstruye igual desde el JSONL. Los eventos, ventanas y alertas no se tocan: el portal sigue leyendo en modo de solo lectura y la revisión es la única escritura. |
| **Casos de borde y fallo** | Alerta o evento inexistente: 404. Transición inválida (reconocer dos veces, cerrar sin reconocer, cerrar con la alerta abierta, cerrar dos veces): 409. Evento sin imagen: 409. Operador vacío o demasiado largo, acción, veredicto o etiqueta desconocidos, cierre sin nota, `otro` sin nota, comentario vacío: 422. Formulario que no viene en UTF-8: 422, en vez de guardar texto alterado. Falta la carpeta de la ejecución (no habría registro primario): 409. **Reenvío del mismo formulario** (`request_id` repetido): no se duplica; responde 200 con el registro original. **Dos personas a la vez**: un candado serializa validar y escribir, así que de 8 "reconocer" simultáneos solo 1 pasa. En HTML el error se muestra en la página con su código; si todo sale bien, se redirige (303) con un mensaje y el operador se recuerda en una cookie. |
| **API** | `GET`/`POST /api/alertas/<id>/revision`, `GET`/`POST /api/eventos/<id>/revision` (201 si es nuevo, 200 si es duplicado, 4xx con `detail`) y `GET /api/revisiones?run_id=&target_type=`. |
| **Pruebas** | [`tests/test_review.py`](../tests/test_review.py), 11 pruebas: máquina de estados y rechazos, reglas de captura, flujo completo por API con idempotencia y conflictos, historial de capturas, reconstrucción desde el JSONL con una base nueva (y reingesta sin duplicar), formularios HTML (303, 409 y 404), cola y filtros, estado vacío, carpeta faltante, concurrencia y codificación. |
| **Evidencia de uso** (ejecuciones oficiales) | 9 acciones en 4 `revisiones.jsonl`. **Revisión de capturas por el autor (operador "Santana")**: S1-W #1 como `oclusion`, revisada dos veces (con historial), y C1 #63 como `sin_problema`. **Alertas, solo veredictos fácticos (operador "Claude Code (asistente)")**: la de la fuente de S1-W y la de versión de S2, reconocidas y cerradas como `esperada` porque eran escenarios provocados. La de cambio sostenido de C2 quedó reconocida y comentada: el intento de cierre dio 409 porque la regla sigue abierta, y no se registró. Un reenvío con el mismo `request_id` dio 200 "duplicado" y tampoco agregó una línea. Al reconstruir con una base nueva solo desde los JSONL, el estado de los 5 objetivos es idéntico. Capturas: [`23`](../evidencias/portal/23-revision-cola-s1-wifi.png) (cola y avance), [`24`](../evidencias/portal/24-revision-alerta-fuente.png) (alerta revisada con historial), [`25`](../evidencias/portal/25-revision-alerta-abierta-c2.png) (alerta abierta: no se ofrece cerrar), [`26`](../evidencias/portal/26-revision-captura.png) (captura con historial), [`27`](../evidencias/portal/27-alertas-por-revision.png) (filtro por estado de revisión); API: [`portal/api/revisiones.json`](../evidencias/portal/api/revisiones.json). |
| **Distinta de lo base** | La vista mínima de eventos y alertas es de solo consulta y no se reclama. F2 agrega escritura con su propio estado, reglas de transición y registro. No se reclama ninguna métrica. |

## F3 · Exportación de eventos (funcionalidad, nivel 10)

Flujo con consulta parametrizada, persistencia pertinente y verificación de los resultados y de los
estados vacíos o erróneos. Código: [`src/monitor/export.py`](../src/monitor/export.py), con las rutas
`/exportar`, `/exportar/descargar`, `/api/exportar` y `/api/exportaciones`.

| | |
|---|---|
| **Qué hace** | `/exportar` arma una exportación con los filtros de `/eventos` (ejecución, estado, motivo de captura, con imagen, rango de fechas) y otros: modelo (`model_ref`), eventos de una alerta, rango de proporción baja, clase, zona y puntuación mínima. Se elige la granularidad (**una fila por evento** o **por detección**) y el formato (**CSV** o **JSONL**). Antes de descargar, muestra cuántas filas saldrán, el tamaño, el SHA-256 y las primeras 10 filas. Desde `/eventos`, "Exportar estos eventos" lleva los filtros activos. |
| **Trazabilidad en cada fila** | En el nivel evento: `event_id`, `run_id`, `seq`, `model_ref`, `config_ref`, `image_ref`, motivo de captura y error, más las **alertas que citan el evento** (`alert_ids`) y la **etiqueta vigente de su revisión** (`revision_captura`, de F2). En el nivel detección: el evento, la clase, la puntuación, la zona y la caja. Un dato ausente sale vacío (CSV) o `null` (JSONL), nunca 0. |
| **Determinismo y verificación** | El orden es fijo (ejecución, `seq`, puntuación) y el archivo no lleva marcas de tiempo, así que los mismos filtros sobre los mismos datos dan los mismos bytes. La respuesta trae `X-Export-Rows` y `X-Export-SHA256`. `uv run python -m monitor.export <filtros> --verificar <archivo>` recalcula el archivo desde la base y compara el SHA-256: si se alteró un byte, no coincide (código 1). |
| **Persistencia** | Cada descarga queda en `datos/exportaciones.jsonl` con la hora, el origen (portal o API), el archivo, los filtros, las filas y el SHA-256. El portal lo muestra en "Descargas registradas" y en `/api/exportaciones`. Con eso, un archivo recibido se puede verificar contra lo que se exportó. |
| **Estados vacíos y errores** | Sin resultados: la vista previa lo dice y no ofrece descarga, y la API entrega un archivo válido con solo el encabezado y `X-Export-Rows: 0`. Formato, nivel, estado o zona desconocidos, fecha que no es ISO 8601, rango de fechas invertido, número fuera de [0, 1], mínimo mayor que máximo o parámetro desconocido: 422, con el motivo en la página o en `detail`. Ejecución o alerta inexistente: 404. La página de error conserva los filtros para corregirlos. |
| **Uso con datos reales** | Cuatro descargas desde el portal, guardadas en [`evidencias/exportaciones/`](../evidencias/exportaciones/) con su registro ([`registro-exportaciones.jsonl`](../evidencias/exportaciones/registro-exportaciones.jsonl)): **(1)** los 180 eventos de la alerta de cambio sostenido de C2, los mismos que cita la alerta; **(2)** las 146 detecciones de personas con s ≥ 0.5 de `yolo11s` en S2, y un conteo independiente desde `events.jsonl` también da 146; **(3)** las 29 entradas inválidas de S1-W en JSONL, igual que el contador del manifest; **(4)** una exportación vacía (errores de inferencia en C1: 0 filas, solo encabezado). Las cuatro se verificaron con `--verificar`: coinciden. Capturas: [`28`](../evidencias/portal/28-exportar-vista-previa.png) (vista previa), [`29`](../evidencias/portal/29-exportar-vacio-y-error.png) (vacía y registro de descargas), [`30`](../evidencias/portal/30-exportar-error.png) (error 422 con los filtros conservados). |
| **Pruebas** | [`tests/test_export.py`](../tests/test_export.py), 27 casos: trazabilidad y conteo igual al de `/api/eventos`, nivel detección y sus filtros, 8 filtros, determinismo y JSONL, 12 errores (422 y 404) en el módulo y en la API, cabeceras, registro y exportación vacía, vista previa con estados vacío y de error, formulario que conserva los filtros, y CLI con los mismos bytes y verificación de un archivo alterado. |

## Control de doble conteo

- M1 y la regla `cambio-sostenido-v1` (R3.3) usan la misma población, pero M1 compara distribuciones
  completas contra la referencia con su propio umbral y caso controlado. La regla base usa la media.
  El escenario C2 no se vuelve a contar como extra.
- M3 se deriva de los registros de S1, pero es un indicador distinto con su propio cálculo y prueba.
  S1 es el escenario: el comportamiento del sistema ante el fallo.
- M2 y G1 miden etapas distintas, ambas instrumentadas (`e2e_ms` y `inference_ms`).
- S2 usa G6 (base) como evidencia, pero no se reclama G6 ni una métrica nueva. Lo que se reclama
  es el escenario: la regla de versión, la reacción en las ventanas, la reversión y su
  verificación. S1 y S2 comparten el patrón de regla por evento, pero vigilan cosas distintas
  (disponibilidad de la fuente frente a identidad del modelo), con condiciones de aceptación
  distintas.
- F2 no repite F1 ni las vistas base: agrega acciones humanas con estado, persistencia y reglas de
  transición. Las capturas y alertas que revisa son las mismas de C1, C2, S1 y S2, que no se vuelven
  a contar.
- F3 reutiliza los filtros de la vista de eventos (base), pero su aportación es otra: la descarga
  con granularidad, la procedencia, el determinismo y la verificación. Los filtros adicionales se
  presentan como parte de F3, no como un extra de "filtros avanzados".
- F1 es la visualización. Muestra W1 (M1), pero su aportación propia es la consulta parametrizada,
  la presentación por ventana y el manejo de estados vacíos o erróneos. La rúbrica permite bonos
  separados para una funcionalidad visual y la métrica que muestra. La tabla de ventanas de
  `/runs/<id>` es base y no se reclama.
