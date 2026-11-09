# Escenario adicional S4 · Pérdida de telemetría

Fallan los destinos donde el monitor escribe lo que observa, mientras la fuente y el modelo funcionan. Se
espera que el monitor no se detenga, que avise y que, al recuperarse el destino, **no falte nada**: ningún
evento, ventana ni alerta perdidos, todo en orden y comprobable.

**Por qué no es C1, C2, S1, S2 ni S3.** En todos esos escenarios falla o cambia algo de lo que se observa:
la escena, la fuente, el modelo o la inferencia. Aquí todo eso funciona y falla el registro de lo observado.
Antes de S4, el monitor escribía en SQLite y en los JSONL sin protección: un índice bloqueado por otro
proceso (por ejemplo, un `monitor.ingest` o una herramienta abierta) habría detenido la ejecución con una
excepción. S4 agrega su propia regla (`telemetria-perdida-v1`), los búferes con reintento y su
verificación.

| Variante | Fallo | Lo que se espera |
|---|---|---|
| **S4-a** `indice:150:60` | De los 150 a los 210 s de stream (muestras 76–105), **otra conexión toma un candado `EXCLUSIVE` real** sobre `datos/monitor.db`. Cada escritura del monitor recibe el `sqlite3.OperationalError: database is locked` real. | El monitor sigue y el registro primario queda completo. Las operaciones del índice esperan en una cola ordenada y se aplican solas al liberarse la base. Mientras tanto, el portal responde 503 con el motivo y la vista en vivo sigue funcionando. |
| **S4-b** `registro:330:40` | De los 330 a los 370 s (muestras 166–185), cada escritura de los JSONL falla con `ENOSPC` (disco lleno **simulado**). El intervalo incluye el cierre de la ventana 3 (muestra 180) y la propia alerta. | Las líneas esperan en un búfer en memoria y se escriben **en el mismo orden** al recuperarse. El índice sigue al día. Si el búfer se llenara, la pérdida se contaría explícitamente. |

## Configuración

[`config/escenario-telemetria.yaml`](../config/escenario-telemetria.yaml) (`cfg-7850d9fcc527`) hereda todo
de `monitor.yaml` y solo agrega:

| Parámetro | Valor | Para qué |
|---|---|---|
| `telemetry_health.consecutive_failures` | 2 | 2 muestras seguidas con un destino sin poder escribir abren `telemetria-perdida-v1` (crítica); una alerta por destino |
| `telemetry_health.recover_after_ok` | 3 | 3 muestras escribiendo y **sin pendientes** la resuelven; si vuelve a escribir pero aún está vaciando atrasos, no cuenta |
| `telemetry_health.max_buffer` | 1000 | líneas del registro primario retenidas en memoria; más allá, las más antiguas se descartan y se cuentan como perdidas |
| `telemetry_health.index_timeout_s` | 0.25 | espera máxima de una escritura al índice bloqueado, para que no frene el muestreo |

## Cómo funciona

- **Registro primario** (`telemetry.BufferedLog`): todos los JSONL de la ejecución pasan por un búfer. Cada
  línea se intenta escribir; si falla, queda en cola y se reintenta al empezar cada muestra, siempre en
  orden.
- **Índice** (`telemetry.IndexSink`): cada operación sobre SQLite entra a una cola ordenada. Todas son
  idempotentes (`INSERT OR IGNORE`, `REPLACE`, `UPDATE`), así que reintentarlas es seguro. Hay un solo
  intento por muestra mientras la base esté bloqueada. Si el monitor termina con la base todavía bloqueada,
  lo pendiente queda en `manifest.json → telemetry.indice_pendientes_al_cerrar` y `monitor.ingest` lo
  recupera desde el registro primario.
- **Estado por muestra** (`telemetria.jsonl`): una línea por muestra con, para cada destino, si pudo
  escribir, cuántas operaciones o líneas tiene pendientes, el error y las pérdidas. La regla se evalúa sobre
  estas líneas, así que `monitor.evaluate` la reproduce aunque no se derive de los eventos.
- **Portal**: si el índice está bloqueado, las vistas y la API responden **503** (con `Retry-After`), con
  el motivo y un enlace a `/vivo`, en lugar de un 500. La vista en vivo no lee SQLite.

## Etapas y lo que se espera en cada una

| Etapa | Qué pasa | Dónde queda la evidencia |
|---|---|---|
| 1. Operación normal | ambos destinos escriben; 0 pendientes | `telemetria.jsonl` |
| 2. **Fallo** | el destino rechaza escrituras (candado real o ENOSPC simulado) | `telemetria.jsonl` (`ok: false`, `error`); `manifest.json → telemetry_fault` |
| 3. **Diagnóstico** | la alerta dice qué destino falla, con qué error, cuántas operaciones o líneas esperan y qué implica (el portal no ve lo nuevo, o el registro espera en memoria) | `alerts.jsonl → metric` |
| 4. **Reacción** | el monitor sigue muestreando. Lo que no se pudo escribir se encola o se retiene y se reintenta en cada muestra. **Una** alerta por destino, sin repetirse | `telemetria.jsonl` (`pendientes` crece), consola, `/vivo` (alerta abierta) |
| 5. **Recuperación** | el destino vuelve a aceptar escrituras y la cola se vacía en orden | `telemetria.jsonl` (`pendientes` vuelve a 0) |
| 6. **Verificación posterior** | 3 muestras sin pendientes resuelven la alerta con la duración, el máximo de pendientes y las pérdidas. El registro y el índice tienen las 240 muestras en orden, `monitor.evaluate` coincide, `monitor.dashboard --verificar` confirma índice = JSONL, y las detecciones son idénticas a C1 | abajo |

## Reproducir

```bash
uv run python -m monitor.run --config config/escenario-telemetria.yaml --replay datos/grabaciones/20261108T050904Z-c1-normal \
    --frames 240 --inject-telemetry-loss indice:150:60 --label s4a-indice-bloqueado
uv run python -m monitor.run --config config/escenario-telemetria.yaml --replay datos/grabaciones/20261108T050904Z-c1-normal \
    --frames 240 --inject-telemetry-loss registro:330:40 --label s4b-registro-sin-espacio

# versión corta con la muestra versionada (~30 s sin pausa): fallo en las muestras 3-5
uv run python -m monitor.run --config config/escenario-telemetria.yaml --replay evidencias/entradas/c1-normal \
    --no-pace --inject-telemetry-loss indice:4:6 --label s4-muestra

# Verificar
uv run python -m monitor.evaluate datos/runs/20261109T033540Z-s4a-indice-bloqueado datos/runs/20261109T034343Z-s4b-registro-sin-espacio
uv run python -m monitor.ingest && uv run python -m monitor.dashboard --verificar      # índice = JSONL
```

## Resultados

Las dos ejecuciones usan la grabación de C1, 240 muestras a 1 frame / 2 s. `monitor.evaluate` coincide en
las dos (4 ventanas normales y 1 alerta cada una), y `monitor.dashboard --verificar` confirma que el índice
tiene exactamente lo mismo que los JSONL.

| | S4-a · índice bloqueado | S4-b · registro sin espacio |
|---|---|---|
| Ejecución | `20261109T033540Z-s4a-indice-bloqueado` | `20261109T034343Z-s4b-registro-sin-espacio` |
| Muestras con el destino fallando | 76–105 (30) | 166–185 (20) |
| Error | `OperationalError: database is locked` (de SQLite, candado real) | `OSError: [Errno 28] No queda espacio en el dispositivo (fallo inyectado, escenario S4)` |
| Alerta | `…-telemetria-perdida-v1-indice-f000076`, abierta en la muestra 77 | `…-telemetria-perdida-v1-registro-f000166`, abierta en la muestra 167 |
| Máximo pendiente | 31 operaciones del índice | 41 líneas del registro (eventos, `telemetria`, la ventana 3 y la propia alerta) |
| Recuperación | muestra 106: la cola se aplica completa | muestra 186: el búfer se escribe completo y en orden |
| Resolución | muestra 108 · `duration_s` 60.0 · **0 perdidos** | muestra 188 · `duration_s` 40.0 · **0 perdidos** |
| Registro primario | 240/240 eventos, `seq` 1–240 sin huecos ni duplicados | 240/240, `seq` 1–240 en orden; las 4 ventanas en `windows.jsonl` |
| Índice | 240/240 eventos, 4 ventanas, la alerta `resolved` | igual |
| `manifest.json → telemetry` | 0 pérdidas, 0 pendientes al cerrar | igual |
| Detecciones frente a C1 | **240/240 idénticas** | **240/240 idénticas** |

### El costo del índice bloqueado

Cada muestra hace un intento de escritura contra la base bloqueada antes de inferir. Eso sube la latencia
de punta a punta (M2):

| | Fuera del fallo | Durante el fallo |
|---|---|---|
| S4-a: `e2e_ms` p50 | 105 ms | **870 ms** (máximo 974) |
| S4-b: `e2e_ms` p50 | 106 ms | 102 ms |

Aun así queda por debajo del intervalo de 2 s: el espaciado entre muestras es de exactamente 2.0 s en toda
la ejecución, sin muestras retrasadas ni perdidas. Con un intervalo menor a ~1 s, el índice bloqueado sí
frenaría el muestreo (ver límites).

### Mientras dura el fallo

- [`43`](../evidencias/portal/43-vivo-indice-bloqueado.png): `/vivo` a la mitad del candado de S4-a
  (muestra 93), en curso, con la alerta `telemetria-perdida-v1` abierta. En ese momento `/eventos`
  respondía 500: así se encontró el problema que motivó el manejo 503.
- [`44`](../evidencias/portal/44-portal-503-indice-bloqueado.png): con el manejo nuevo, la misma situación
  (otra conexión con candado exclusivo sobre la base) da 503 con el motivo y el enlace a `/vivo`. Al
  liberarse, `/eventos` vuelve a 200.

### Evidencia

| Qué | Dónde |
|---|---|
| Registros primarios | [`datos/runs/20261109T033540Z-s4a-indice-bloqueado/`](../datos/runs/20261109T033540Z-s4a-indice-bloqueado/) y [`datos/runs/20261109T034343Z-s4b-registro-sin-espacio/`](../datos/runs/20261109T034343Z-s4b-registro-sin-espacio/): **`telemetria.jsonl`** (estado de los destinos por muestra), `events.jsonl`, `alerts.jsonl`, `windows.jsonl`, `manifest.json` (`telemetry_fault`, `telemetry`) |
| Métricas | [`evidencias/metricas/20261109T033540Z-s4a-indice-bloqueado.json`](../evidencias/metricas/20261109T033540Z-s4a-indice-bloqueado.json) (M2 p95 872 ms por el candado), [`…-s4b-registro-sin-espacio.json`](../evidencias/metricas/20261109T034343Z-s4b-registro-sin-espacio.json) |
| Portal | [`43`](../evidencias/portal/43-vivo-indice-bloqueado.png)–[`46`](../evidencias/portal/46-alerta-telemetria-s4b.png) |
| API | [`portal/api/alerta-telemetria-s4a.json`](../evidencias/portal/api/alerta-telemetria-s4a.json), [`alerta-telemetria-s4b.json`](../evidencias/portal/api/alerta-telemetria-s4b.json) |
| Pruebas | [`tests/test_telemetry.py`](../tests/test_telemetry.py) (7 pruebas) |

## Límites

- El disco lleno de S4-b es simulado: falla la escritura de los JSONL, pero las imágenes se siguen
  guardando. En un disco lleno real también fallarían; el evento se registraría igual, pero con la
  referencia a una imagen que no existe (G5 lo detectaría). El candado de S4-a sí es real.
- El búfer del registro está en memoria: si el proceso muere durante el fallo, se pierden las líneas
  retenidas, como mucho `max_buffer`. Escribirlas a otro disco evitaría eso. El índice no tiene ese
  riesgo, porque se reconstruye con `monitor.ingest`.
- El intento contra el índice bloqueado es síncrono (~0.75 s por muestra). Con 1 frame / 2 s cabe; con un
  intervalo menor haría falta mover las escrituras del índice a un hilo aparte.
- La regla mira si se pudo escribir, no si lo escrito es correcto. Un JSONL truncado por un corte de
  energía lo detectan el contrato de `monitor.ingest` (líneas rechazadas) y `monitor.dashboard
  --verificar`, no esta regla.
