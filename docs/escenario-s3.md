# Escenario adicional S3 · Error de inferencia

Falla la etapa del modelo mientras la fuente sigue entregando frames. El monitor tiene que distinguirlo de
una caída de la fuente (S1), avisar una sola vez, intentar arreglarlo solo y dejar constancia de si su
reacción funcionó.

**Por qué no es C1, C2, S1 ni S2.** En C1 y C2 la inferencia funciona y cambia la escena. En S1 lo que falla
es la fuente: no llegan frames (`invalid_input`). En S2 la inferencia funciona, pero con otro modelo. Aquí
los frames llegan y el modelo configurado no produce resultado (`inference_error`). La regla es otra
(`inferencia-fallida-v1`), y el escenario agrega algo que S1 y S2 no tienen: una **reacción activa** del
monitor (recargar el modelo) con su efecto verificado.

Se prueban dos variantes, con la misma regla y la misma reacción, que cambian la condición de aceptación:

| Variante | Fallo | Lo que se espera de la reacción |
|---|---|---|
| **S3-a** `corrupcion:150` | A los 150 s de stream (muestra 76), el estado del modelo en memoria se corrompe: la primera convolución queda con 1 canal de entrada. Cada inferencia falla con el `RuntimeError` real de torch y **no se cura sola**. | La recarga desde los pesos verificados **lo arregla**, y la alerta se resuelve por eso. |
| **S3-b** `persistente:300:60` | De los 300 a los 360 s (muestras 151–180), la inferencia falla siempre: simula un dispositivo o una dependencia externa al modelo que no responde. | Las recargas **no lo arreglan**, y eso queda registrado. Se limitan a 3, la alerta sigue abierta mientras dure el fallo y se resuelve cuando cesa. Ninguna ventana con datos insuficientes pasa por normal. |

## Configuración

[`config/escenario-inferencia.yaml`](../config/escenario-inferencia.yaml) (`cfg-5ed9a8d8ea02`) hereda todo
de `monitor.yaml` (modelo, política, ventanas, reglas y captura con `on_error: true`) y solo agrega:

| Parámetro | Valor | Para qué |
|---|---|---|
| `inference_health.consecutive_errors` | 3 | 3 muestras seguidas con `inference_error` abren `inferencia-fallida-v1` (crítica) |
| `inference_health.recover_after_ok` | 3 | 3 inferencias válidas seguidas la resuelven |
| `inference_health.reload_after` | 3 | reacción: primera recarga del modelo al tercer error seguido |
| `inference_health.reload_every` | 5 | si sigue fallando, otra recarga cada 5 errores, no en cada muestra |
| `inference_health.max_reloads` | 3 | como máximo 3 por episodio: si no bastan, el fallo no es del estado del modelo |

Las muestras sin frame (`invalid_input`) no cuentan para esta regla ni cortan su racha, porque no hubo
inferencia que juzgar: esas son de S1.

## Etapas y lo que se espera en cada una

| Etapa | Qué pasa | Dónde queda la evidencia |
|---|---|---|
| 1. Operación normal | eventos `ok` con el modelo configurado | `events.jsonl`, `windows.jsonl` |
| 2. **Fallo** | desde la muestra del fallo, cada evento queda `inference_error` con el mensaje de la excepción; el frame se captura (`capture_reason: error`) | `events.jsonl` (`status`, `error`, `image_ref`); `manifest.json → inference_fault` (qué se inyectó) |
| 3. **Diagnóstico** | la alerta dice en qué etapa está el fallo. `inference_error` implica que la fuente sí entregó frame; sin frame el evento sería `invalid_input`. También agrupa los tipos de error y cuenta las capturas del fallo, que permiten ver que los frames estaban bien | `alerts.jsonl → metric.diagnostico` |
| 4. **Reacción** | **una** alerta crítica a los 3 errores, sin repetirse. En ese mismo evento el monitor recarga el modelo desde el archivo, verificando antes su SHA-256. La siguiente muestra con inferencia dice si la recarga fue efectiva | `alerts.jsonl`; `inferencia.jsonl` (`recarga_modelo` con resultado, SHA y duración; `recarga_verificada` con el efecto) |
| 5. **Recuperación** | S3-a: la recarga devuelve un modelo sano. S3-b: el fallo cesa en la muestra 181 | `events.jsonl` |
| 6. **Verificación posterior** | 3 inferencias válidas resuelven la alerta con la duración, los errores, las recargas y **qué la resolvió** (`recovered_by`). Fuera de las muestras con fallo, las detecciones son idénticas a las de C1 en las mismas muestras. `monitor.evaluate` reproduce ventanas y alertas | `alerts.jsonl` (`type: update`), comparación con C1 (abajo) |

## Reproducir

```bash
# las dos variantes oficiales (replay de la grabación de C1 a 1 frame / 2 s, 8 min cada una)
uv run python -m monitor.run --config config/escenario-inferencia.yaml --replay datos/grabaciones/20261108T050904Z-c1-normal \
    --frames 240 --inject-inference-error corrupcion:150 --label s3a-corrupcion
uv run python -m monitor.run --config config/escenario-inferencia.yaml --replay datos/grabaciones/20261108T050904Z-c1-normal \
    --frames 240 --inject-inference-error persistente:300:60 --label s3b-persistente

# versión corta con la muestra versionada (~30 s sin pausa): corrupción en la muestra 3
uv run python -m monitor.run --config config/escenario-inferencia.yaml --replay evidencias/entradas/c1-normal \
    --no-pace --inject-inference-error corrupcion:4 --label s3-muestra

# Verificar (sin modelo, desde los registros versionados)
uv run python -m monitor.evaluate datos/runs/20261109T025839Z-s3a-corrupcion datos/runs/20261109T030642Z-s3b-persistente
```

La grabación de C1 no se versiona por su tamaño. La prueba de punta a punta
(`test_run_end_to_end_with_reaction`) corre el mismo flujo sobre la muestra versionada.

## Resultados

Las dos ejecuciones usan la grabación de C1 (`20261108T050904Z-c1-normal`), 240 muestras a 1 frame / 2 s, y
`monitor.evaluate` coincide en las dos (4 ventanas y 1 alerta cada una).

| | S3-a · corrupción | S3-b · fallo persistente |
|---|---|---|
| Ejecución | `20261109T025839Z-s3a-corrupcion` | `20261109T030642Z-s3b-persistente` |
| Muestras con `inference_error` | 76–78 (3) | 151–180 (30) |
| Error | `RuntimeError: Given groups=1, weight of size [16, 1, 3, 3], expected input[1, 3, 736, 1280] to have 1 channels, but got 3 channels instead` (de torch) | `RuntimeError: dispositivo de inferencia no disponible (fallo inyectado, escenario S3)` |
| Alerta | `…-inferencia-fallida-v1-f000076`, abierta en la muestra 78 | `…-inferencia-fallida-v1-f000151`, abierta en la muestra 153 |
| Diagnóstico | etapa `inferencia`, la fuente entregó frame, `RuntimeError` × 3, 3 capturas | etapa `inferencia`, la fuente entregó frame, `RuntimeError` × 3, 3 capturas |
| Recargas | 1, en la muestra 78: SHA-256 verificado, 337.6 ms. **Efectiva**: la muestra 79 quedó `ok` | 3, en las muestras 153, 158 y 163 (184.5, 256.7 y 204.1 ms), todas con SHA verificado y **sin efecto**: 154, 159 y 164 siguieron con `inference_error`. Al llegar al tope no hubo más recargas |
| Resolución | muestra 81 · `duration_s` 6.0 · 3 errores · `recovered_by: recarga_del_modelo` | muestra 183 · `duration_s` 60.0 · 30 errores · `recovered_by: fallo_ceso_sin_efecto_de_las_recargas` |
| Ventanas | 1–4 `normal`. La ventana 2 tiene 57 de 60 muestras válidas (mínimo 45), así que el monitoreo de confianza sigue | 1, 2 y 4 `normal`; **3 `insuficiente`** (30 válidas < 45): no se interpreta como normal ni como anómala, y no suma ni rompe rachas |
| G5 / G6 / G7 | 1.0 / 1.0 / 0 abiertas al final | 1.0 / 1.0 / 0 abiertas al final |

### Verificación posterior: mismas muestras que C1

Mismo replay y mismo modelo que la C1 oficial (`20261108T161753Z-c1-normal`), así que fuera del fallo cada
muestra debe dar exactamente las mismas detecciones (clase, puntuación y caja):

| | S3-a | S3-b |
|---|---|---|
| Antes del fallo | **75/75 idénticas** | **150/150 idénticas** |
| Después del fallo | **162/162 idénticas**: el modelo recargado es el mismo artefacto | **60/60 idénticas** |
| Inferencia p50 antes → después | 112 → 106 ms | 112 → 96 ms |

En S3-a esto demuestra que la reacción no solo quitó el error: dejó el monitor exactamente como estaba.

### Evidencia

| Qué | Dónde |
|---|---|
| Registros primarios | [`datos/runs/20261109T025839Z-s3a-corrupcion/`](../datos/runs/20261109T025839Z-s3a-corrupcion/) y [`datos/runs/20261109T030642Z-s3b-persistente/`](../datos/runs/20261109T030642Z-s3b-persistente/): `events.jsonl`, `alerts.jsonl`, `windows.jsonl`, **`inferencia.jsonl`** (la reacción), `manifest.json` (`inference_fault`), `imagenes/` (capturas del fallo) |
| Métricas | [`evidencias/metricas/20261109T025839Z-s3a-corrupcion.json`](../evidencias/metricas/20261109T025839Z-s3a-corrupcion.json), [`…-s3b-persistente.json`](../evidencias/metricas/20261109T030642Z-s3b-persistente.json) |
| Portal | [`39`](../evidencias/portal/39-alerta-inferencia-s3a.png) (S3-a: diagnóstico, recarga efectiva, resuelta por la recarga), [`40`](../evidencias/portal/40-alerta-inferencia-s3b.png) (S3-b: tres recargas sin efecto), [`41`](../evidencias/portal/41-run-s3b.png) (ejecución con la ventana 3 `insuficiente`), [`42`](../evidencias/portal/42-graficas-s3b.png) (F1: hueco `insuficiente` con la alerta de inferencia) |
| API | [`portal/api/alerta-inferencia-s3a.json`](../evidencias/portal/api/alerta-inferencia-s3a.json), [`alerta-inferencia-s3b.json`](../evidencias/portal/api/alerta-inferencia-s3b.json) |
| Pruebas | [`tests/test_inference_health.py`](../tests/test_inference_health.py) (8 pruebas) |

## Límites

- Los fallos son inyectados. La corrupción produce el error real de torch y la recarga real lo arregla,
  pero un fallo de producción (memoria, controlador o archivo de pesos dañado) puede verse distinto. Si
  lo que se dañó es el archivo de pesos, la recarga lo detecta (`sha256_verificado: false`) y no carga
  el modelo.
- La recarga es síncrona: la muestra en la que ocurre tarda la carga más el calentamiento (190–340 ms
  aquí), dentro del intervalo de 2 s. Con un modelo mucho más grande podría retrasar la muestra siguiente.
- "Efectiva" se juzga solo con la muestra siguiente. Si un fallo intermitente coincidiera con la recarga,
  esta se marcaría efectiva o sin efecto por azar. Por eso la resolución exige 3 inferencias válidas
  seguidas, no una.
- La regla mira errores explícitos. Un modelo que falla en silencio (por ejemplo, pesos con NaN que
  producen cero detecciones sin excepción) no da `inference_error`. Eso lo cubrirían las reglas de
  confianza y W1 (M1), y no se prueba aquí.
