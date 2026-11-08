# Escenario adicional S1 · Interrupción y recuperación de la fuente

**Tipo:** escenario adicional (S), nivel solicitado **10**: secuencia compuesta con fallo, diagnóstico y
reacción, recuperación y verificación posterior, con evidencia reproducible de cada etapa.

**Por qué es distinto de C1 y C2:**
- C1 y C2 prueban la **confianza** del modelo con la fuente sana.
- S1 prueba la **fuente**: qué hace el monitor cuando deja de recibir frames y cómo vuelve.
- La política de confianza, las ventanas y las reglas de confianza son exactamente las de C1 y C2.

## Configuración

[`config/escenario-interrupcion.yaml`](../config/escenario-interrupcion.yaml) hereda todo de
`monitor.yaml` (`base: monitor.yaml`) y solo agrega lo siguiente:

| Parámetro | Valor | Para qué |
|---|---|---|
| `source.frame_timeout_s` | 3.0 | una muestra que no recibe frame en 3 s (1.5 × intervalo) se registra como `invalid_input` con el motivo |
| `source.stall_timeout_s` | 5.0 | 5 s sin frames del stream → se da por desconectado y se reconecta |
| `source.read_timeout_s` / `open_timeout_s` | 5 / 15 | timeouts de OpenCV/FFmpeg, para que `grab()` no se bloquee sin red |
| `source.reconnect_backoff_s` / `_max_s` | 2 / 20 | espera creciente entre intentos: 2, 4, 8, 16, 20… s |
| `source_health.consecutive_invalid` | 3 | 3 muestras seguidas sin frame (~6–9 s) abren `fuente-interrumpida-v1` (critical) |
| `source_health.recover_after_valid` | 3 | 3 muestras válidas seguidas la resuelven |

`config_ref` = `cfg-3ebc8541e4f7`. La prueba
`tests/test_source_health.py::test_scenario_config_inherits_base` comprueba que la política, las
ventanas y las reglas de confianza son idénticas a las de `monitor.yaml`.

## Etapas y lo que se espera en cada una

| Etapa | Qué pasa | Dónde queda la evidencia |
|---|---|---|
| 1. Operación normal | frames válidos, ventana normal | `events.jsonl`, `windows.jsonl` |
| 2. **Fallo** | la fuente deja de entregar datos | `fuente.jsonl`: `lectura_fallida` → `desconectado` (con motivo y último error) |
| 3. **Diagnóstico** | cada muestra sin frame es un evento `invalid_input` con su `error`; cada intento de reconexión queda con su error y la espera siguiente | `events.jsonl` (`error`), `fuente.jsonl` (`reconexion_fallida`, `intento`, `espera_s`) |
| 4. **Reacción** | a las 3 muestras sin frame se abre **una** alerta crítica con el motivo y la acción sugerida; no se repite mientras dure | `alerts.jsonl` (`type: alert`), portal `/alertas` |
| 5. **Recuperación** | la reconexión vuelve a resolver la URL con yt-dlp y abre el stream | `fuente.jsonl`: `reconectado` (`intento`, `caida_s`) |
| 6. **Verificación posterior** | 3 frames válidos seguidos resuelven la alerta con la duración de la caída. La ventana afectada queda **insuficiente** (ni normal ni anómala, sin falsas alertas de confianza) y las siguientes vuelven a ser normales. `monitor.evaluate` reproduce ventanas y alertas | `alerts.jsonl` (`type: update`, `downtime_s`, `n_invalid`, `first_ok_event_id`), `windows.jsonl`, `manifest.json → source_stats` |

## Variantes

| ID | Fuente | Cómo se provoca el fallo | Reproducible sin red |
|---|---|---|---|
| **S1-R** | replay de la grabación de C1 a 1 frame / 2 s | `--inject-outage 180:90`: se pierden los frames de t = 180 a 270 s de stream (determinista) | sí |
| **S1-V** | cámara en vivo | `--inject-outage`: durante el corte `grab()` falla y cada reconexión se rechaza como sin red; usa el mismo lector, la misma reconexión y la misma resolución con yt-dlp que la operación real | no (necesita el stream) |
| **S1-W** | cámara en vivo | corte **real**: se apaga el WiFi del equipo unos 60 s | no |

La inyección no altera puntuaciones ni eventos: solo hace que la fuente no entregue frames durante
el corte. Todo lo demás (eventos, regla, reconexión, ventanas) es el código normal.

## Reproducir

```bash
# S1-R (sin red, determinista; 8 min a ritmo real)
uv run python -m monitor.run --config config/escenario-interrupcion.yaml \
    --replay datos/grabaciones/20261108T050904Z-c1-normal --frames 240 \
    --inject-outage 180:90 --label s1-replay-interrupcion
# versión corta con la muestra versionada (60 frames, ~2 min): pierde t = 40..90 s
uv run python -m monitor.run --config config/escenario-interrupcion.yaml \
    --replay evidencias/entradas/c1-normal --inject-outage 40:50 --label s1-muestra

# S1-V (en vivo, corte inyectado a los 180 s durante 90 s)
uv run python -m monitor.run --config config/escenario-interrupcion.yaml --frames 240 \
    --inject-outage 180:90 --label s1-vivo-interrupcion

# Verificar cualquiera de ellas
uv run python -m monitor.evaluate datos/runs/<run_id>
uv run python -m monitor.metrics  datos/runs/<run_id>
uv run pytest tests/test_source_health.py
```

En S1-V el corte se cuenta desde que arranca la fuente, antes de la primera conexión (yt-dlp tarda
~5 s). Por eso cae unas 3 muestras antes que en el replay.

## Resultados

_Se completan con las ejecuciones oficiales._

## Límites

- **Qué simula la inyección.** Simula la falta de red a nivel de la fuente: `grab()` falla y la
  conexión se rechaza. No reproduce los modos de falla de la pila de red, como la latencia, los
  segmentos HLS a medias o el DNS. S1-W cubre ese caso con un corte real.
- **La caída medida es mayor que el corte.** Incluye la espera de la reconexión: con espera
  creciente, el último intento puede llegar hasta 20 s después de que vuelve la red.
- **Menos muestras durante la caída.** Cada muestra espera hasta 3 s, así que se registra
  aproximadamente una muestra cada 3 s, no cada 2 s. Los ticks perdidos quedan en
  `source_stats.dropped_source_wait`.
