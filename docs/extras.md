# Inventario de extras

Las métricas base de G1–G7 ([`docs/metricas.md`](metricas.md)), los escenarios C1 y C2 y las vistas
mínimas del portal **no** se reclaman como extras. Cada extra tiene un ID, su propia evidencia y una
forma de verificarlo.

| ID | Tipo | Descripción | Nivel solicitado | Estado |
|---|---|---|---|---|
| **S1** | Escenario | Interrupción y recuperación de la fuente | 10 | completo: S1-R, S1-V y S1-W |
| **S2** | Escenario | Cambio de versión del modelo no declarado, reacción y reversión | 10 | completo |
| **S3** | Escenario | Error de inferencia: diagnóstico de etapa, recarga automática del modelo y su efecto verificado | 10 | completo: S3-a y S3-b |
| **S4** | Escenario | Pérdida de telemetría: índice bloqueado o registro sin espacio, sin perder datos, verificado | 10 | completo: S4-a y S4-b |
| **M1** | Métrica | Desplazamiento de la distribución de puntuaciones frente a la referencia (Wasserstein-1) | 8 | completo |
| **M2** | Métrica | Latencia de punta a punta (frame → evento), p50/p95 por ejecución y por ventana | 5 | completo |
| **M3** | Métrica | Tiempo de recuperación de la fuente y disponibilidad por ventana | 5 | completo (con S1-R, S1-V y S1-W) |
| **M4** | Métrica | Acuerdo entre modelos (producción frente a un modelo de referencia, mismos frames), con caso controlado | 8 | completo |
| **F1** | Funcionalidad | Gráficas por ventana en el portal (proporción baja, media y W1, con umbrales, huecos y alertas) | 10 | completo |
| **F2** | Funcionalidad | Revisión humana de alertas y capturas: estados coordinados con las reglas, persistencia trazable, cola de revisión | 15 | completo |
| **F3** | Funcionalidad | Exportación de eventos y detecciones (CSV/JSONL) con filtros, procedencia y verificación | 10 | completo |
| **F4** | Funcionalidad | Vista en vivo del stream con resultados, estado de la ejecución y de la fuente | 15 | completo |
| **F5** | Funcionalidad | Panel de operación (dashboard): indicadores, puntos de atención, estado por ejecución y por regla, verificado contra los JSONL | 10 | completo |
| **F6** | Funcionalidad | Gráficas históricas: una señal por ventana a través de todas las ejecuciones, por ejecución o por hora de la escena, verificadas contra los JSONL | 10 | completo |

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

## S3 · Error de inferencia (escenario, nivel 10)

Falla la etapa del modelo mientras la fuente sigue entregando frames. Detalle, reproducción y límites:
[`docs/escenario-s3.md`](escenario-s3.md).

- **Por qué no es C1, C2, S1 ni S2.** Los frames llegan (no es S1) y el modelo es el configurado (no es
  S2), pero no produce resultado: `inference_error`. Agrega su propia regla (`inferencia-fallida-v1`),
  un diagnóstico de etapa en la alerta y una **reacción activa**: recargar el modelo desde los pesos
  verificados y registrar si funcionó (`inferencia.jsonl`).
- **Dos variantes con condiciones de aceptación distintas**, sobre la grabación de C1:
  - **S3-a, estado del modelo corrupto** (`20261109T025839Z-s3a-corrupcion`). Errores en las muestras 76–78
    (`RuntimeError` real de torch). Hay una alerta y una recarga en la muestra 78, que resulta **efectiva**:
    la muestra 79 queda `ok`. Se resuelve con `recovered_by: recarga_del_modelo` y `duration_s` 6.0. Las 4
    ventanas quedan `normal`.
  - **S3-b, fallo persistente** (`20261109T030642Z-s3b-persistente`). Errores en las muestras 151–180. Hay
    3 recargas, todas **sin efecto**, y luego se respeta el tope. La ventana 3 queda `insuficiente`. Se
    resuelve al cesar el fallo, con `recovered_by: fallo_ceso_sin_efecto_de_las_recargas` y `duration_s`
    60.0.
  - `monitor.evaluate` coincide en las dos.
- **Verificación posterior.** Fuera de las muestras con fallo, las detecciones son idénticas a las de C1
  en las mismas muestras: S3-a 75/75 antes y 162/162 después de la recarga; S3-b 150/150 y 60/60.
- **Evidencia:** registros de las dos ejecuciones (con `inferencia.jsonl`), capturas
  [`39`](../evidencias/portal/39-alerta-inferencia-s3a.png)–[`42`](../evidencias/portal/42-graficas-s3b.png),
  [`tests/test_inference_health.py`](../tests/test_inference_health.py) (8 pruebas, una de punta a punta con
  `monitor.run` real).

## S4 · Pérdida de telemetría (escenario, nivel 10)

Fallan los destinos donde el monitor escribe (el índice SQLite o el registro primario JSONL) mientras la fuente
y el modelo funcionan. Detalle, reproducción y límites: [`docs/escenario-s4.md`](escenario-s4.md).

- **Por qué no es C1, C2, S1, S2 ni S3.** Lo observado está bien; falla el registro de lo observado. Agrega
  su propia regla (`telemetria-perdida-v1`, evaluada sobre `telemetria.jsonl`), escritura con búfer y cola
  ordenada para ambos destinos, y 503 en el portal cuando el índice está bloqueado. Antes de S4, un índice
  bloqueado detenía el monitor con una excepción.
- **Dos variantes con condiciones de aceptación distintas**, sobre la grabación de C1:
  - **S4-a, índice bloqueado** (`20261109T033540Z-s4a-indice-bloqueado`). Otra conexión toma un candado
    `EXCLUSIVE` **real** en las muestras 76–105. Hay una alerta, la cola llega a 31 operaciones, se aplica
    sola al liberarse y queda resuelta con 0 perdidos y `duration_s` 60.0.
  - **S4-b, registro sin espacio** (`20261109T034343Z-s4b-registro-sin-espacio`). ENOSPC simulado en las
    muestras 166–185. Hay una alerta, el búfer llega a 41 líneas (con la ventana 3 y la propia alerta), se
    escribe en orden y queda resuelta con 0 perdidos y `duration_s` 40.0.
- **Verificación posterior.** Registro e índice con las 240 muestras en orden, sin huecos ni duplicados.
  `monitor.evaluate` coincide y `monitor.dashboard --verificar` confirma índice = JSONL. Las detecciones son
  idénticas a C1 en 240/240 muestras en ambas variantes.
- **Costo medido.** Durante el candado, `e2e_ms` p50 sube de 105 a 870 ms (M2), pero sin retrasar ninguna
  muestra: el espaciado se mantiene en 2.0 s.
- **Evidencia:** registros con `telemetria.jsonl`, capturas
  [`43`](../evidencias/portal/43-vivo-indice-bloqueado.png)–[`46`](../evidencias/portal/46-alerta-telemetria-s4b.png)
  (la vista en vivo durante el candado y el 503 del portal), [`tests/test_telemetry.py`](../tests/test_telemetry.py)
  (7 pruebas: candado real, búfer con pérdida explícita, regla, 503 y punta a punta para los dos destinos).

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

## M4 · Acuerdo entre modelos (métrica, nivel 8)

Detalle, verificación y límites: [`docs/metrica-m4.md`](metrica-m4.md).

| | |
|---|---|
| **Definición** | Acuerdo (Dice) entre las detecciones de producción (yolo11n, `events.jsonl`) y las de un modelo de referencia (yolo11s) sobre **los mismos frames** (`acuerdo.jsonl`, con el SHA-256 de cada frame): `2·pares / (n_prod + n_ref)`, con s ≥ 0.25, misma clase e IoU ≥ 0.5. Por ejecución y por ventana, con desglose de cobertura, respaldo y confusión de clase. |
| **Correlación de fuentes** | Dos fuentes de predicción por frame, unidas por `event_id` y por el archivo de entrada. La referencia corre aparte, fuera del bucle de producción; el cálculo se rehace desde los dos registros sin modelo. |
| **Población, exclusiones y cero observaciones** | Eventos con inferencia válida y frame medido. `excluido` y `sin_frame` quedan registrados por evento. Un frame 0/0 no aporta. Ventana suficiente con ≥ 45 frames y ≥ 300 detecciones; si no, `null`. |
| **Referencia** | Las dos ejecuciones de referencia: 30 ventanas entre 0.618 y 0.834; piso = mínimo (0.6178). |
| **Caso controlado** | Grabación de C1 con desenfoque σ = 3 desde la muestra 121 (`20261109T054751Z-m4-desenfoque`). `monitor.agreement comparar`: ventanas 1–2 con frames idénticos a C1 → **diferencia 0 exacta**; ventanas 3–4 desenfocadas → **−0.056 y −0.125**, ambas bajo el piso. Además: control de producción contra sí misma = **1.0** en las 7 ventanas de C1, y en S2 (producción es yolo11s en las muestras 76–180) la ventana 3 = **1.0**. |
| **Resultado** | C1 0.654 y C2 (noche) 0.652: la baja confianza de C2 no mueve el acuerdo, así que no repite G4. Referencias 0.818 (mañana) y 0.650 (tarde). |
| **Límites verificados** | El piso absoluto deja 2 de 14 ventanas normales por debajo (la escena pesa más que el desenfoque): se reporta como métrica, sin regla de alerta. La referencia no es la verdad. Requiere los frames (reproducciones). |
| **Recálculo** | `monitor.metrics` → `metricas_adicionales.M4_acuerdo_modelos` y `ventanas[].M4_acuerdo_modelos`; serie en `/graficas` (capturas 47–49); [`tests/test_agreement.py`](../tests/test_agreement.py) (10 pruebas). |

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

## F4 · Vista en vivo del stream con resultados (funcionalidad, nivel 15)

Flujo compuesto con integración adicional (el proceso del monitor y el portal se coordinan mientras
la ejecución corre), coordinación de estados, persistencia y trazabilidad, con un caso normal y casos
de fallo reproducibles. Código: [`src/monitor/live.py`](../src/monitor/live.py), el publicador en
[`run.py`](../src/monitor/run.py) y las rutas `/vivo`, `/vivo/panel/<id>`, `/vivo/frame/<id>`,
`/api/vivo` y `/api/vivo/<id>`.

| | |
|---|---|
| **Qué hace** | En cada muestra, `monitor.run` publica en `datos/vivo/<run_id>/` el último frame con sus cajas (`frame.jpg`) y su estado (`estado.json`): el último evento (estado, conteos por zona, proporción baja, inferencia, modelo, error), la ventana en curso con su avance y sus valores parciales, la última ventana cerrada, las alertas abiertas, el estado de la fuente y los contadores. `/vivo` lo muestra y se actualiza cada 2 s con un fragmento renderizado en el servidor (la misma plantilla con o sin JavaScript), y tiene un selector cuando hay varias ejecuciones. El frame solo se vuelve a pedir cuando cambia. |
| **Coordinación de estados** | El portal clasifica cada ejecución con reglas explícitas (`live.classify`). **`en_curso`**: el monitor reportó dentro del plazo, max(10 s, 5 × intervalo). **`fuente_caida`**: el monitor reporta, pero el último evento no trae frame; se muestra el último frame recibido y su antigüedad. **`estancada`**: el monitor dejó de reportar sin terminar (proceso detenido, colgado o equipo suspendido). **`terminada`**: la ejecución dejó su resumen final. Además, señala un modelo distinto del configurado (S2). |
| **Persistencia y trazabilidad** | `estado.json` y `frame.jpg` son el estado *actual*, no un registro primario: no se versionan y se reescriben con escritura atómica (temporal y `os.replace`, con reintentos por si Windows tiene el archivo abierto), así que el portal nunca lee un JSON a medias. Todo lo que muestran enlaza a lo persistido: el evento (`/eventos/<id>`), la ejecución, las alertas abiertas y la ventana. El manifest registra `live_view.dir` y `live_view.write_errors`. Es de mejor esfuerzo: si una escritura falla, se cuenta y el monitor sigue (`--sin-vivo` la desactiva). |
| **Estados vacíos y errores** | Sin ejecuciones con vista en vivo: mensaje con el comando para iniciar una, y la página se recarga sola. Ejecución inexistente o `run_id` inválido (con puntos o separadores, p. ej. `..` codificado): 404 en página, fragmento, frame y API. Todavía sin frame: mensaje. Fallo de red o del portal durante la actualización: se reintenta en el siguiente ciclo. |
| **Uso con datos reales** | **Normal, cámara en vivo**: la ejecución `20261109T005919Z-f4-vivo` (10:00 JST, lluvia; 90 muestras, `evaluate` coincide, 0 errores de escritura), capturada en curso con la ventana 1 a 33/60 ([`31`](../evidencias/portal/31-vivo-en-curso.png)) y terminada con su alerta de baja confianza por la lluvia ([`32`](../evidencias/portal/32-vivo-terminada.png)). **Fallo de la fuente, reproducible**: un replay de C1 con `--inject-outage 30:40`; mientras dura el corte, la vista muestra `fuente_caida`, el último frame recibido hace 15.7 s y la alerta `fuente-interrumpida-v1` abierta ([`33`](../evidencias/portal/33-vivo-fuente-caida.png)); al volver la fuente, la alerta se cierra. **Monitor detenido**: el mismo proceso se detuvo a la mitad (muestra 40 de 90) y, a los 20 s, la vista lo marca `estancada` ([`34`](../evidencias/portal/34-vivo-estancada.png)). Esta última ejecución quedó incompleta a propósito y no se versiona; se reproduce con el comando de abajo, deteniéndola a mano. |
| **Reproducir** | `uv run python -m monitor.portal` y abrir `/vivo`. En otra terminal: `uv run python -m monitor.run --frames 90 --label vivo` (cámara) o `uv run python -m monitor.run --config config/escenario-interrupcion.yaml --replay datos/grabaciones/20261108T050904Z-c1-normal --frames 90 --inject-outage 30:40 --label vivo-fallo` (fuente caída y luego Ctrl+C para `estancada`). |
| **Pruebas** | [`tests/test_live.py`](../tests/test_live.py), 7 pruebas: clasificación de los cuatro estados con sus antigüedades; el publicador escribe el estado y el frame, conserva el último frame durante la caída y deja `terminada` sin temporales; un fallo de escritura no detiene el monitor; estado vacío; vistas, fragmento, frame, API y 404 (incluido `..` codificado); `estancada` y `fuente_caida` en la página; y una prueba de punta a punta que corre `monitor.run` real sobre 3 frames y verifica el estado final. |
| **Por qué nivel 15** | Coordina dos procesos con estados derivados de reglas explícitas, enlaza todo lo que muestra con registros persistidos y demuestra tanto el caso normal (cámara real) como fallos reproducibles: fuente caída por inyección y proceso detenido. |

## F5 · Panel de operación (dashboard) (funcionalidad, nivel 10)

Flujo compuesto con consulta parametrizada, persistencia pertinente y verificación de resultados y de
estados vacíos o erróneos. Código: [`src/monitor/dashboard.py`](../src/monitor/dashboard.py), la
plantilla [`panel.html`](../src/monitor/portal/templates/panel.html) y las rutas `/panel` (la portada del
portal) y `/api/panel`.

| | |
|---|---|
| **Qué hace** | Una vista de todas las ejecuciones a la vez, filtrada por modo (`live` o `replay`) y por rango de fecha de inicio (UTC). **Indicadores**: ejecuciones por modo y por estado, eventos y porcentaje válido, ventanas evaluadas y porcentaje fuera de rango, alertas abiertas, revisiones de alertas sin cerrar y la inferencia p50/p95. **Requiere atención**: una lista con niveles (crítico, aviso, seguimiento), en la que cada punto enlaza al registro que hay que atender. **Una fila por ejecución**: estado, eventos, % válidos, una barra con sus ventanas por estado, ventanas fuera de rango, alertas abiertas, revisiones sin cerrar, capturas sin revisar y enlaces a sus eventos, alertas, cola de revisión y gráficas. **Alertas por regla**: abiertas, resueltas, avance de revisión y veredictos. Mientras alguna ejecución está en curso, la página se recarga cada 15 s. |
| **Reglas de atención** | Explícitas en `ATTENTION_RULES`, y la página las muestra. **A1**: alerta abierta sin reconocer; es crítica si la alerta es `critical`. **A2**: la regla ya resolvió la alerta, pero falta cerrar su revisión con un veredicto. **A3**: alerta reconocida cuya regla sigue abierta. **E1** y **E2**: ejecución sin terminar con la fuente caída, o cuyo monitor dejó de reportar (los estados de F4). **E3**: ejecución sin resumen final ni estado en vivo. **E4**: ejecución indexada en SQLite cuya carpeta de registros primarios no existe. **C1**: capturas por baja confianza sin revisar. El estado de cada ejecución lo da primero su registro (con `finished_at` está terminada); si no terminó, lo explica la vista en vivo. |
| **Persistencia y verificación** | Solo lee: SQLite en modo de solo lectura y los `estado.json` de la vista en vivo. No guarda estado propio: lo que muestra son los registros de las ejecuciones, las alertas y las revisiones. `python -m monitor.dashboard --verificar` recalcula, por ejecución, eventos, válidos, sin frame, capturas, ventanas por estado, alertas, alertas abiertas, estado de revisión y capturas revisadas directamente de `events`, `windows`, `alerts` y `revisiones.jsonl`, sin SQLite. Además comprueba que los totales sean la suma de las filas. Sale con 1 y la lista de diferencias si algo no coincide. |
| **Estados vacíos y errores** | Sin ejecuciones: mensaje con los comandos para crearlas o indexarlas. Ningún resultado con los filtros: mensaje y enlace para quitarlos; los indicadores quedan en "—", nunca en 0 %. Ejecución sin ventanas: "sin ventanas" y "—" en fuera de rango. Modo desconocido, fecha que no es ISO 8601, rango invertido o parámetro desconocido: 422 con el motivo, conservando los filtros en el formulario. En la API, 422 con `detail`. Base inexistente en la CLI: mensaje con el comando para crearla. |
| **Uso con datos reales** | **Base reconstruida desde lo versionado** (como la tendría quien clona el repositorio): 9 ejecuciones, 3 750 eventos, 56 ventanas evaluadas (27 % fuera de rango), 6 de 12 alertas abiertas y 18 puntos de atención. El punto crítico es el cambio sostenido abierto de S1-W. La verificación coincide: [`verificacion-reconstruida.txt`](../evidencias/panel/verificacion-reconstruida.txt), [`35`](../evidencias/portal/35-panel.png), [`portal/api/panel.json`](../evidencias/portal/api/panel.json). **Filtro**: solo las 3 ejecuciones en vivo ([`36`](../evidencias/portal/36-panel-filtro-vivo.png)). **Error**: `since=ayer` da 422 y el formulario conserva el filtro ([`37`](../evidencias/portal/37-panel-error-filtro.png)). **Base de trabajo local, con dos fallos reales**: el panel marcó `E2` en la demo de F4 que se detuvo a propósito (sin reportes desde hace más de una hora) y `E4` en una ejecución S1-R vieja (`20261108T191228Z`) que seguía en la base, aunque su carpeta ya se había borrado al repetir el escenario. La verificación falla justo en esa ejecución ([`verificacion-base-local.txt`](../evidencias/panel/verificacion-base-local.txt), [`38`](../evidencias/portal/38-panel-base-local.png)). Reconstruir el índice con `monitor.ingest` la elimina. |
| **Reproducir** | `uv run python -m monitor.ingest` y luego `uv run python -m monitor.dashboard --verificar` (o abrir `/panel` en el portal). Con `--mode live`, `--since` y `--until` se aplican los mismos filtros de la página. |
| **Pruebas** | [`tests/test_dashboard.py`](../tests/test_dashboard.py), 8 pruebas sobre cinco ejecuciones escritas como JSONL e indexadas con `Store.ingest_run_dir`, con la vista en vivo de dos de ellas. Cubren: indicadores y filas (con un evento reenviado que no se cuenta dos veces y la mediana interpolada); las siete reglas de atención y su orden, y cómo cambian al cerrar o reconocer una revisión; el estado coordinado de cada ejecución; los filtros y sus cuatro errores; la verificación (coincide, detecta un JSONL modificado sin reindexar y una carpeta borrada, con E4); las vistas, la API, la recarga automática, el 422 que conserva el filtro, los dos estados vacíos; y la CLI `--verificar` con salida 0 y 1. |
| **Por qué nivel 10** | Consulta parametrizada sobre lo persistido, resultados verificados contra los registros primarios y estados vacíos y erróneos probados. Las reglas de atención combinan el estado de las alertas, de la revisión (F2) y de la vista en vivo (F4), pero esos estados ya se cobraron en F2 y F4, así que no se usan para pedir 15. |

## F6 · Gráficas históricas (funcionalidad, nivel 10)

Flujo compuesto con consulta parametrizada, persistencia pertinente y verificación de resultados y de
estados vacíos o erróneos. Código: [`src/monitor/history.py`](../src/monitor/history.py), la gráfica
`history_chart` en [`charts.py`](../src/monitor/portal/charts.py), la plantilla
[`historico.html`](../src/monitor/portal/templates/historico.html) y las rutas `/historico` y `/api/historico`.

| | |
|---|---|
| **Qué hace** | Una señal por ventana a través de **todas** las ejecuciones; F1 muestra las ventanas de una sola. **Señal**: proporción de baja confianza (G4), puntuación media, inferencia p95 por ventana (G1) o proporción de frames con inferencia válida. **Eje**: por ejecución (una columna por ejecución, en orden de inicio, con sus ventanas unidas por una línea) o por **hora del día de la escena en Shibuya (JST)**, para comparar la señal según la hora sin importar cuándo se procesó. **Filtros**: modo, rango de fecha de inicio y texto en la etiqueta. Cada punto enlaza a su ejecución y tiene un tooltip con la ventana, el valor, el estado y la hora de la escena. Abajo, una tabla por ejecución (también la vista accesible de los datos): hora de la escena, ventanas con valor, mínimo, media, máximo y alertas de la regla de esa señal. |
| **Hora de la escena** | En vivo es exacta (`captured_at`). En un replay se reconstruye: el nombre de la grabación empieza con su hora de inicio (UTC) y cada muestra está a `sample_interval_s` de la anterior. Contra las cuatro grabaciones locales (con sus `captured_at` reales), el error es de 5–40 s al inicio y de 2.3 min como máximo al final, por el arranque y una pequeña deriva acumulada: despreciable en un eje de 24 h. Una grabación sin hora en el nombre (la de M4) se excluye del eje `hora` y se cuenta. |
| **Valores y exclusiones** | Baja confianza y media solo en ventanas evaluadas (`normal`, `fuera_de_rango`), igual que F1 y las reglas. Inferencia p95 (por rango más cercano, como G1) y frames válidos en toda ventana completa. Lo excluido se cuenta por motivo (incompleta, insuficiente, otra versión, sin hora de escena) y nunca se grafica como 0. El umbral de la regla se dibuja solo si todas las ejecuciones filtradas usan el mismo. |
| **Persistencia y verificación** | Solo lee: el índice SQLite en modo de solo lectura. No guarda estado propio. `python -m monitor.history --verificar` recalcula las **4 señales × 2 ejes** desde `manifest.json`, `windows.jsonl`, `events.jsonl` y `alerts.jsonl`, sin SQLite, y compara cada punto (valor, hora de escena, cruce del umbral) y cada fila. Sale con 1 y la lista de diferencias si algo no coincide. |
| **Estados vacíos y errores** | Sin ejecuciones: mensaje con los comandos. Ninguna ejecución con los filtros: mensaje y enlace para quitarlos. Ninguna ventana con valor: "Ninguna ventana con valor para estos filtros". Señal, eje o modo desconocidos, fecha que no es ISO 8601, rango invertido o parámetro desconocido: 422 con el motivo y el formulario conservado; en la API, 422 con `detail`. |
| **Uso con datos reales** | **Base reconstruida desde lo versionado**: 14 ejecuciones, 75 ventanas con valor en baja confianza; la verificación coincide en las 8 combinaciones ([`verificacion-reconstruida.txt`](../evidencias/historico/verificacion-reconstruida.txt), [`50`](../evidencias/portal/50-historico-por-ejecucion.png), [`portal/api/historico-hora.json`](../evidencias/portal/api/historico-hora.json)). **Lo que deja ver y no se ve en una sola ejecución**: (1) por hora de la escena ([`51`](../evidencias/portal/51-historico-hora-escena.png)), la baja confianza depende más del **día** que de la hora: las 07:00 del 8 de noviembre dan 0.555 de media, y las 07:30–07:50 del 9, 0.73–0.76 (ese día, a las 10:00, F4 registró lluvia); la tarde, 0.64–0.65, y la noche, 0.74. (2) La inferencia p95 sobre los **mismos frames de C1** bajó de 300–420 ms (8 de noviembre) a 120–175 ms (S3 y S4, 9 de noviembre) con el mismo modelo ([`52`](../evidencias/portal/52-historico-inferencia-replay.png)); no se aisló la causa (carga del equipo). **Error**: `senal=fps&eje=dia` da 422 ([`53`](../evidencias/portal/53-historico-filtro-invalido.png)). **Base de trabajo local**: la verificación falla justo en las dos ejecuciones sin registros primarios que F5 ya marcaba con E4 ([`verificacion-base-local.txt`](../evidencias/historico/verificacion-base-local.txt)). |
| **Reproducir** | `uv run python -m monitor.ingest` y luego `uv run python -m monitor.history --verificar` (o abrir `/historico`). Con `--senal`, `--eje`, `--modo`, `--desde`, `--hasta` y `--etiqueta` se aplican los mismos filtros de la página. |
| **Pruebas** | [`tests/test_history.py`](../tests/test_history.py), 6 pruebas sobre tres ejecuciones escritas como JSONL e indexadas (en vivo, replay con hora en la grabación y replay sin hora). Cubren los seis errores de filtro; valores, exclusiones por motivo, cruce del umbral, hora de la escena exacta y aproximada y la exclusión del eje `hora`; las otras señales (p95 por rango más cercano, válidos) y los filtros; la verificación (coincide en las 8 combinaciones, detecta un JSONL distinto del índice y una carpeta sin registros); la página, la API, el 422 y los estados vacíos; y la CLI `--verificar` con salida 0 y 1. |
| **Por qué nivel 10** | Consulta parametrizada sobre lo persistido, resultados verificados contra los registros primarios y estados vacíos y erróneos probados. No coordina estados ni integra otra fuente, así que no se pide 15. |

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
- S3 comparte con S1 y S2 el patrón de regla por evento, pero vigila otra etapa (la inferencia, con la
  fuente sana y el modelo correcto) y es el único escenario con reacción activa del monitor (recargar el
  modelo) cuyo efecto se verifica. S3-a y S3-b son un mismo escenario con dos condiciones de aceptación
  (la reacción arregla el fallo o no lo arregla); se reclaman como un solo extra.
- S4 usa M2 (latencia) para medir el costo del índice bloqueado, pero no la reclama otra vez, y su
  verificación reutiliza `monitor.dashboard --verificar` (F5) sin cobrarlo de nuevo. Lo que se reclama es el
  escenario: la regla de telemetría, los búferes con reintento, el 503 del portal y la comprobación de que
  no se pierde nada. S4-a y S4-b se reclaman como un solo extra.
- M4 no repite G4 ni M1: esas miden solo las puntuaciones del modelo desplegado; M4 compara sus detecciones
  con las de otro modelo sobre los mismos frames (en C2 la confianza baja y el acuerdo no cambia). Usa la
  ejecución de S2 como uno de sus casos de verificación, sin volver a reclamar S2. Su serie en `/graficas`
  amplía F1, que no se cobra otra vez.
- F6 no repite F1 ni F5: F1 grafica las ventanas de una sola ejecución y F5 cuenta estados sin series. F6
  compara la misma señal entre ejecuciones y por hora de la escena, con su propia verificación. Usa las
  métricas base G1 y G4 por ventana, que no se cobran; tampoco reclama M4 ni W1, que no grafica.
- F2 no repite F1 ni las vistas base: agrega acciones humanas con estado, persistencia y reglas de
  transición. Las capturas y alertas que revisa son las mismas de C1, C2, S1 y S2, que no se vuelven
  a contar.
- F3 reutiliza los filtros de la vista de eventos (base), pero su aportación es otra: la descarga
  con granularidad, la procedencia, el determinismo y la verificación. Los filtros adicionales se
  presentan como parte de F3, no como un extra de "filtros avanzados".
- F4 muestra en vivo lo mismo que después se persiste (eventos, ventanas y alertas de C1, C2 y S1), pero
  no recalcula ni reclama ninguna métrica. Su aportación es la publicación del estado en curso, su
  clasificación (`en_curso`, `fuente_caida`, `estancada`, `terminada`) y la coordinación entre procesos.
  S1 es el escenario de fallo; F4 solo lo hace visible mientras ocurre.
- F5 agrega lo que muestran las vistas base, F1, F2 y F4, pero no las repite: no grafica señales por
  ventana (eso es F1), no escribe revisiones (F2) ni publica estado en vivo (F4). Su aportación es la
  vista de todas las ejecuciones a la vez, las reglas de atención que cruzan esos estados y la
  verificación del índice contra los registros primarios. La inferencia p50/p95 es G1 (base), solo
  mostrada; no se reclama como métrica.
- F1 es la visualización. Muestra W1 (M1), pero su aportación propia es la consulta parametrizada,
  la presentación por ventana y el manejo de estados vacíos o erróneos. La rúbrica permite bonos
  separados para una funcionalidad visual y la métrica que muestra. La tabla de ventanas de
  `/runs/<id>` es base y no se reclama.
