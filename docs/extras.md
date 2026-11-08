# Inventario de extras

Las métricas base de G1–G7 ([`docs/metricas.md`](metricas.md)), los escenarios C1 y C2 y las vistas
mínimas del portal **no** se reclaman como extras. Cada extra tiene un ID, su propia evidencia y una
forma de verificarlo.

| ID | Tipo | Descripción | Nivel solicitado | Estado |
|---|---|---|---|---|
| **S1** | Escenario | Interrupción y recuperación de la fuente | 10 | S1-R completo; S1-V y S1-W pendientes de grabar en vivo |
| **M1** | Métrica | Desplazamiento de la distribución de puntuaciones frente a la referencia (Wasserstein-1) | 8 | completo |
| **M2** | Métrica | Latencia de punta a punta (frame → evento), p50/p95 por ejecución y por ventana | 5 | completo |
| **M3** | Métrica | Tiempo de recuperación de la fuente y disponibilidad por ventana | 5 | completo (con S1-R) |

---

## S1 · Interrupción y recuperación de la fuente (escenario, nivel 10)

Fallo, diagnóstico, reacción, recuperación y verificación posterior, con evidencia de cada etapa.
Detalle completo, variantes y cómo reproducirlo: [`docs/escenario-s1.md`](escenario-s1.md).

- **Por qué no es C1 ni C2.** Prueba la disponibilidad de la fuente, no la confianza. Agrega su
  propia regla (`fuente-interrumpida-v1`), un registro de diagnóstico (`fuente.jsonl`) y la
  reconexión con espera creciente.
- **Evidencia:** la ejecución `20261108T193149Z-s1-replay-interrupcion` (S1-R) con `fuente.jsonl`,
  `alerts.jsonl` y `windows.jsonl`, más [`tests/test_source_health.py`](../tests/test_source_health.py).

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
| **Resultado** | Referencias: 0/30 ventanas sobre el umbral. **C1: 0/7** (máx. 0.042). **C2: 6/7** (W1 de la ejecución 0.0615, el doble que C1, 0.0282). S1-R: 0/3, con su ventana insuficiente en `null`. |
| **Recálculo** | `uv run python -m monitor.drift datos/runs/<run_id>`; también en `metricas_adicionales` de `evidencias/metricas/<run_id>.json`. |
| **Supuestos y límites** | La referencia mezcla una mañana con poca gente y una tarde con multitud, así que el umbral incluye esa variación normal. Las detecciones de un mismo frame no son independientes, por eso W1 no se usa como prueba estadística, solo como distancia con umbral empírico. El umbral sale de 30 ventanas. W1 resume la distribución completa: complementa la media de la regla `cambio-sostenido-v1` (capta cambios de forma, no solo de nivel), pero en C2 ambas reaccionan a lo mismo. |
| **Alternativa descartada** | PSI con deciles de la referencia: depende de cómo se agrupan las puntuaciones, y la mezcla mañana/tarde lo infla (la referencia llega a 0.22 por ventana). Separaba peor: C2 solo 3/7 ventanas sobre el máximo de la referencia. |

## M2 · Latencia de punta a punta (métrica, nivel 5)

| | |
|---|---|
| **Definición** | `e2e_ms`: del momento en que la fuente entrega el frame (`t_capture`) a la creación del evento. Incluye preprocesamiento, inferencia, posprocesamiento, zonas y captura de imagen; excluye la escritura del evento. Se reporta p50 / p95 / máximo por ejecución y p95 por ventana. |
| **Distinta de G1** | G1 mide solo `inference_ms`, el modelo. M2 mide la etapa completa del monitor. En C1 la diferencia de p50 es 349 − 315 ≈ 34 ms. |
| **Población** | Eventos con inferencia válida; los `invalid_input` se excluyen, porque su espera es de la fuente y no del procesamiento. Sin eventos válidos: `null`. |
| **Resultado** | C1 349 / 488 ms (p50 / p95); C2 338 / 445 ms; referencias ≈ 250 / 330 ms (`--no-pace`). |
| **Recálculo** | `monitor.metrics` → `metricas_adicionales.M2_latencia_e2e` y `ventanas[].M2_e2e_p95_ms`; [`tests/test_metrics.py`](../tests/test_metrics.py). |

## M3 · Tiempo de recuperación de la fuente y disponibilidad (métrica, nivel 5)

| | |
|---|---|
| **Definición** | Para cada incidente resuelto de `fuente-interrumpida-v1`: segundos del primer frame perdido al primer frame recibido (`downtime_s`). Se reporta la media y el máximo por ejecución, y los incidentes abiertos al final. La disponibilidad es la proporción de muestras con frame, por ejecución y por ventana. |
| **Correlación de registros** | Incidente (`alerts.jsonl`, alerta + resolución) ↔ eventos perdidos (`n_invalid`, `first_ok_event_id` en `events.jsonl`) ↔ diagnóstico de la fuente (`fuente.jsonl`: `desconectado` → `reconectado`, `caida_s`). |
| **Cero observaciones** | Sin incidentes, el tiempo de recuperación es `null` / `sin_datos`, no 0, que sugeriría una recuperación instantánea. Un incidente abierto al terminar no entra en la media; se cuenta en `incidentes_abiertos`. |
| **Resultado** | S1-R: 1 incidente, **90.0 s**, 45 muestras perdidas; disponibilidad 0.8125, con 0.50 en la ventana 2 y 0.75 en la 3. C1, C2 y referencias: sin incidentes, disponibilidad 1.0. |
| **Recálculo** | `monitor.metrics` → `metricas_adicionales.M3_recuperacion_fuente` y `ventanas[].M3_disponibilidad_fuente`; [`tests/test_metrics.py`](../tests/test_metrics.py). |

## Control de doble conteo

- M1 y la regla `cambio-sostenido-v1` (R3.3) usan la misma población, pero M1 compara distribuciones
  completas contra la referencia con su propio umbral y caso controlado. La regla base usa la media.
  El escenario C2 no se vuelve a contar como extra.
- M3 se deriva de los registros de S1, pero es un indicador distinto con su propio cálculo y prueba.
  S1 es el escenario: el comportamiento del sistema ante el fallo.
- M2 y G1 miden etapas distintas, ambas instrumentadas (`e2e_ms` y `inference_ms`).
