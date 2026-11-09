# Escenario adicional S2 · Cambio de versión del modelo no declarado

**Tipo:** escenario adicional (S), nivel solicitado **10**. Es una secuencia compuesta: fallo,
diagnóstico y reacción, recuperación y verificación posterior, con evidencia reproducible de cada
etapa.

**Qué se prueba.** Un despliegue reemplaza el modelo en caliente: `yolo11n` pasa a `yolo11s`,
sin actualizar la configuración. Después se revierte. El monitor tiene que:
- notar que los eventos ya no vienen del modelo configurado;
- dejar de interpretar las ventanas con umbrales que se calcularon para otro modelo;
- confirmar que todo volvió a la normalidad después de la reversión.

**Por qué es distinto de C1, C2 y S1:**
- C1 y C2 prueban la confianza con el modelo correcto.
- S1 prueba la disponibilidad de la fuente.
- S2 prueba la **identidad del modelo**. El stream y la fuente están sanos y los eventos son
  válidos; lo que cambia es *qué* modelo los produce.

## Configuración

[`config/escenario-version.yaml`](../config/escenario-version.yaml) hereda todo de `monitor.yaml`
(`base: monitor.yaml`), incluido el modelo esperado (`yolo11n`), y solo agrega lo siguiente:

| Parámetro | Valor | Para qué |
|---|---|---|
| `model_alternatives.yolo11s` | pesos, URL y SHA-256 | el artefacto que el despliegue pone en su lugar; tiene identidad resoluble y se verifica igual que el principal ([`modelo/README.md`](../modelo/README.md)) |
| `model_version.consecutive_mismatch` | 3 | 3 eventos con inferencia seguidos que reportan otro `model_ref` abren `version-modelo-v1` (crítica) |
| `model_version.recover_after_match` | 3 | 3 eventos seguidos con la versión esperada la resuelven |
| `model_version.exclude_windows` | true | reacción: una ventana con algún evento de otra versión queda `otra_version` |

`config_ref` = `cfg-2b094007ccc7`. La prueba
`tests/test_model_version.py::test_scenario_config_inherits_base_and_declares_alternative`
comprueba que el modelo esperado, la política, las ventanas y las reglas son idénticos a los de
`monitor.yaml`.

**Cómo se provoca.** `--swap-model yolo11s:150:210` usa `yolo11s` de t = 150 s a t = 360 s del
stream, contando t = (seq − 1) × 2 s, y luego vuelve a `yolo11n`.
- **Antes de empezar.** Los dos modelos se cargan y se verifican por SHA-256; el cambio en sí
  es instantáneo.
- **Qué registra cada evento.** Su `model_ref` es el del modelo que **realmente** se ejecutó,
  no el configurado. Por eso G6 y la regla pueden ver el cambio.
- **Qué no cambia.** No se modifica ninguna puntuación. Las detecciones de esas muestras son las
  que da `yolo11s` con los mismos parámetros de inferencia.

## Etapas y lo que se espera en cada una

| Etapa | Qué pasa | Dónde queda la evidencia |
|---|---|---|
| 1. Operación normal | eventos de `yolo11n`; ventana normal | `events.jsonl` (`model_ref`), `windows.jsonl` |
| 2. **Fallo** | desde la muestra 76 los eventos reportan `yolo11s@sha256:85a76fe86dd8` | `events.jsonl`; `manifest.json → model_swap` (qué se inyectó, para auditar) |
| 3. **Diagnóstico** | la regla compara el `model_ref` de cada evento válido con el configurado (`model.name` + SHA-256 declarado); la alerta dice cuál se esperaba y cuál apareció | `alerts.jsonl` (`metric.expected_model_ref`, `metric.reported_model_ref`) |
| 4. **Reacción** | se abre **una** alerta crítica a los 3 eventos y no se repite. Las ventanas con eventos de otra versión quedan `otra_version`: no se comparan con la referencia ni con los umbrales, y no suman ni rompen rachas | `alerts.jsonl`, `windows.jsonl` (`state`, `n_other_version`) |
| 5. **Recuperación** | el despliegue se revierte: desde la muestra 181 vuelve `yolo11n` | `events.jsonl` |
| 6. **Verificación posterior** | 3 eventos seguidos con la versión esperada resuelven la alerta, con la duración, los eventos afectados y las versiones vistas. La ventana siguiente vuelve a ser `normal`, y sus detecciones son **idénticas** a las de C1 en esas mismas muestras. `monitor.evaluate` reproduce ventanas y alertas | `alerts.jsonl` (`type: update`), comparación con C1 (abajo) |

## Reproducir

```bash
uv run python -m monitor.download_model --config config/escenario-version.yaml   # yolo11n + yolo11s, verificados
uv run python -m monitor.run --config config/escenario-version.yaml \
    --replay datos/grabaciones/20261108T050904Z-c1-normal --frames 240 \
    --swap-model yolo11s:150:210 --label s2-cambio-version
# versión corta con la muestra versionada (60 frames, ~1 min sin pausa): yolo11s de la muestra 16 a la 35
uv run python -m monitor.run --config config/escenario-version.yaml \
    --replay evidencias/entradas/c1-normal --no-pace --swap-model yolo11s:30:40 --label s2-muestra

# Verificar (sin modelo, desde los registros versionados)
uv run python -m monitor.evaluate datos/runs/20261108T232126Z-s2-cambio-version
uv run python -m monitor.metrics  datos/runs/20261108T232126Z-s2-cambio-version
uv run python -m monitor.evaluate datos/runs/20261108T232126Z-s2-cambio-version --sin-reaccion-version   # contrafactual
uv run pytest tests/test_model_version.py
```

## Resultados

Ejecución oficial: **`20261108T232126Z-s2-cambio-version`**. Es un replay de la grabación de C1
(14:09 JST) a 1 frame cada 2 s: 240 muestras y 4 ventanas.

| Ventana | Muestras | Modelo | Estado | Prop. baja | Media | W1 (M1) | G6 de la ventana | Inferencia p50 |
|---|---|---|---|---|---|---|---|---|
| 1 | 1–60 | yolo11n | normal | 0.599 | 0.494 | 0.0242 | 1.00 | 336 ms |
| 2 | 61–120 | yolo11n (61–75) + yolo11s (76–120) | **otra_version** | 0.634 | 0.477 | 0.0276 | 0.25 | 667 ms |
| 3 | 121–180 | yolo11s | **otra_version** | 0.607 | 0.478 | 0.0166 | 0.00 | 669 ms |
| 4 | 181–240 | yolo11n | normal | 0.696 | 0.452 | 0.0419 | 1.00 | 335 ms |

- **Alerta** `alert-20261108T232126Z-s2-cambio-version-version-modelo-v1-f000076` (crítica):
  - se abre con los eventos 76–78, unos 4 s después del cambio;
  - se resuelve en el evento 183, el tercero con la versión esperada;
  - la resolución registra `duration_s` **210.0 s**, `n_mismatch` **105** y
    `reported_model_refs` = `[yolo11s@sha256:85a76fe86dd8]`;
  - el primer evento correcto es el 181.
- **Ninguna alerta de confianza.**
- **`monitor.evaluate`** coincide: 4 ventanas y 1 alerta.
- **Métricas:**
  - G6 = **0.5625**: 135 de 240 eventos con la versión esperada.
  - G5 = **1.0**. Los eventos de `yolo11s` siguen siendo trazables porque su artefacto está
    declarado y verificado. Otra versión no es un evento roto: eso lo mide G6.
  - G1 p50 = 402 ms en la ejecución completa.

### Verificación posterior: mismas muestras que C1

S2 reproduce la misma grabación que C1 (`20261108T161753Z-c1-normal`, procesada con `yolo11n`), así
que se pueden comparar muestra por muestra:

| Muestras | Comparación con C1 |
|---|---|
| 1–75 (antes del cambio) | **75/75 eventos con detecciones idénticas** (clase, puntuación y caja) |
| 181–240 (después de la reversión) | **60/60 idénticos**: la reversión deja el monitor exactamente como estaba |
| 76–180 (durante el cambio) | mismo frame, distinto modelo (tabla siguiente) |

| Muestras 76–180 | yolo11n (C1) | yolo11s (S2) | Cambio |
|---|---|---|---|
| Detecciones elegibles por frame (s ≥ 0.25) | 21.0 | 26.9 | +28 % |
| Detecciones aceptadas por frame (s ≥ 0.65) | 4.53 | 6.10 | +35 % |
| Personas elegibles por frame | 9.4 | 11.4 | +21 % |
| Puntuación media de las elegibles | 0.4708 | 0.4838 | +0.013 |
| Proporción de baja confianza | 0.6359 | 0.6056 | −0.030 |
| W1 frente a la referencia, ventana 3 | 0.0264 | 0.0166 | más cerca de la referencia |
| Tiempo de inferencia p50 | 318 ms | 673 ms | ×2.1 |

### Lo que habría pasado sin la reacción

Con `exclude_windows: false`, las reglas de confianza habrían evaluado las ventanas 2 y 3 como
**`normal`**. M1 tampoco cruza su umbral: 0.0276 y 0.0166, con umbral 0.048.

`yolo11s` detecta más objetos y con algo más de confianza, así que **ninguna señal de confianza
(reglas ni W1) delata el cambio**. Además, el cambio es real y afecta lo que se reporta: los conteos
suben entre 21 % y 35 % sobre los mismos frames. Por eso hace falta comparar la identidad del
modelo, y por eso las ventanas de otra versión no se interpretan: un "normal" ahí sería una
conclusión sobre un modelo que la referencia nunca midió. El contrafactual se reproduce con
`uv run python -m monitor.evaluate datos/runs/20261108T232126Z-s2-cambio-version --sin-reaccion-version`:
recalcula las ventanas desde `events.jsonl` con `exclude_windows: false`. La prueba
`test_window_with_other_version_is_not_evaluated_and_keeps_streak` comprueba los dos
comportamientos.

El tiempo de inferencia sí cambia: se duplica, como se ve en G1 por ventana. Pero no hay una
regla de rendimiento, y un aumento así también podría venir de la carga del equipo, como se vio
entre C1 y S1. No identifica la causa; la regla de versión sí.

### Evidencia

| Qué | Dónde |
|---|---|
| Registros primarios | [`datos/runs/20261108T232126Z-s2-cambio-version/`](../datos/runs/20261108T232126Z-s2-cambio-version/): `events.jsonl` (`model_ref` por evento), `alerts.jsonl`, `windows.jsonl`, `manifest.json` (`model_swap`) |
| Métricas | [`evidencias/metricas/20261108T232126Z-s2-cambio-version.json`](../evidencias/metricas/20261108T232126Z-s2-cambio-version.json) (G6 por ejecución y por ventana) |
| Portal | [`20-run-s2.png`](../evidencias/portal/20-run-s2.png) (versiones reportadas y ventanas `otra_version`), [`21-alerta-version-s2.png`](../evidencias/portal/21-alerta-version-s2.png) (alerta, evidencia y resolución), [`22-graficas-s2.png`](../evidencias/portal/22-graficas-s2.png) (huecos `otra_version`; W1 dentro del umbral) |
| API | [`portal/api/alerta-version-s2.json`](../evidencias/portal/api/alerta-version-s2.json) |
| Pruebas | [`tests/test_model_version.py`](../tests/test_model_version.py) (8 pruebas) |

## Límites

- **Cambio simulado.** El cambio se inyecta en el proceso del monitor (`--swap-model`). En un
  despliegue real lo haría otro proceso o el reemplazo del archivo de pesos. El monitor no depende
  de cómo ocurra: compara lo que reportan los eventos.
- **Identidad, no comportamiento.** La regla detecta un `model_ref` distinto. Si alguien
  reemplazara los pesos y el evento siguiera reportando el nombre y el SHA-256 configurados, la
  regla no lo vería. Por eso `monitor.run` verifica el SHA-256 de los pesos al arrancar, y el
  `model_ref` se calcula del archivo cargado, no de la configuración.
- **Una sola versión alternativa.** El contrato admite varias (`reported_model_refs` es una lista),
  pero solo se probó `yolo11s`.
- **Ventana mixta.** Basta un evento de otra versión para que la ventana quede `otra_version`. Es
  una regla conservadora: la ventana 2 tenía 15 eventos de `yolo11n` y no se evaluó.
