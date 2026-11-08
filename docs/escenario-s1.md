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

# S1-W (en vivo, sin inyección: apagar el WiFi hacia el minuto 3 durante ~60 s)
uv run python -m monitor.run --config config/escenario-interrupcion.yaml --frames 240 --label s1-wifi

# Verificar cualquiera de ellas
uv run python -m monitor.evaluate datos/runs/<run_id>
uv run python -m monitor.metrics  datos/runs/<run_id>
uv run pytest tests/test_source_health.py
```

En S1-V el corte se cuenta desde que arranca la fuente, antes de la primera conexión (yt-dlp tarda
~5 s). Por eso cae unas 3 muestras antes que en el replay.

## Resultados

Las tres variantes usan `cfg-3ebc8541e4f7` y el mismo código de fuente. En las tres,
`monitor.evaluate` recalcula las ventanas y las alertas (incluida la de la fuente) desde
`events.jsonl` y coinciden con lo registrado. G5 = G6 = 1.0.

| | S1-R · replay | S1-V · vivo, corte inyectado | S1-W · vivo, WiFi apagado |
|---|---|---|---|
| Ejecución | `20261108T193149Z-s1-replay-interrupcion` | `20261108T222800Z-s1-vivo-interrupcion` | `20261108T224433Z-s1-wifi` |
| Hora en Tokio | 14:09 (grabación de C1) | 07:28, lluvia | 07:44, lluvia |
| Corte | 90 s inyectados (t = 180–270 s de stream) | 90 s inyectados (t = 180–270 s desde que arranca la fuente) | real: el equipo pierde la red |
| Error registrado | `desconectado` / `reconectado` del replay | `interrupción inyectada: sin acceso a la red` | `getaddrinfo failed`: no resuelve `www.youtube.com` (DNS) |
| Intentos de reconexión | — | 7 fallidos (esperas de 2, 4, 8, 16, 20, 20 y 20 s); reconecta en el 8.º | 6 fallidos (2, 4, 8, 16, 20 y 20 s); reconecta en el 7.º |
| Alerta `fuente-interrumpida-v1` | 1, resuelta | 1, resuelta | 1, resuelta |
| Caída (`downtime_s`, M3) | **90.0 s** | **92.0 s** | **84.6 s** |
| Caída según la conexión (`caida_s`) | — | 91.9 s | 73.4 s |
| Muestras perdidas (`n_invalid`) | 45 | 31 | 28 |
| Ventanas | 1 normal · **2 insuficiente** · 3–4 normal | 1 normal · **2 insuficiente** · 3–4 fuera de rango | 1 fuera de rango · **2 insuficiente** · 3–4 fuera de rango |
| Disponibilidad (M3) | 0.8125 | 0.8708 | 0.8792 |
| Otras alertas | ninguna | `baja-confianza-v1` en la ventana 4 | `baja-confianza-v1` desde la ventana 1; `cambio-sostenido-v1` en la 4 |

### Lo que muestra cada etapa

1. **Fallo y diagnóstico.** En S1-V el lector falla en cuanto empieza el corte: registra
   `lectura_fallida` y, 3.2 s después, `desconectado`. En S1-W el corte es real y el lector se queda
   esperando datos sin recibir un error. El límite de 3 s por muestra (`frame_timeout_s`) lo detecta
   primero: la primera muestra sin frame es de las 22:47:57 y `lectura_fallida` llega a las 22:48:05.
   Por eso las tres primeras muestras dicen "fuente conectado; último error: ninguno". Después,
   cada intento de reconexión registra el error real de DNS.
2. **Reacción.** Se abre **una sola** alerta crítica en la tercera muestra sin frame: 22:31:17 en
   S1-V y 22:48:03 en S1-W. En S1-W eso es **5 s antes** de que el lector se declare
   `desconectado` (22:48:08). Mientras dura la caída la alerta no se repite.
3. **Recuperación.** La reconexión vuelve a resolver la URL con yt-dlp y abre el stream. En S1-V
   la red "vuelve" a los 270 s (22:32:38) y el siguiente intento llega a las 22:32:41, porque la
   espera creciente ya estaba en 20 s: la caída medida es 92.0 s para 90 s de corte. En S1-W
   la red volvió entre el intento 6 (22:49:00, falló) y el 7 (22:49:20, funcionó).
4. **Verificación posterior.** Tres frames válidos seguidos resuelven la alerta con `downtime_s`,
   `n_invalid` y `first_ok_event_id`. La ventana del corte queda `insuficiente` en las tres
   variantes: no se evalúa ni como normal ni como anómala y no produce alertas de confianza.

### Dos medidas de la caída

`downtime_s` va de la **primera muestra sin frame** a la **primera muestra con frame**: es la caída
que vive el monitor, y es lo que reporta M3. `caida_s` va de `desconectado` a `reconectado`:
es la caída de la conexión, según el lector. En S1-V casi coinciden (92.0 frente a 91.9 s) porque
el lector detecta el corte inyectado de inmediato. En S1-W difieren 11 s: 8 s porque el monitor
detectó la falta de frames antes que el lector, y 3 s porque se necesitan 3 muestras válidas para
resolver.

### Alertas de confianza durante S1-V y S1-W

Las dos sesiones en vivo se hicieron temprano por la mañana en Tokio y con lluvia: hay muchos
paraguas que tapan a las personas (capturas en `imagenes/` de cada ejecución). Las alertas de
confianza son de la escena, no del corte:

- En S1-W `baja-confianza-v1` ya estaba abierta en la ventana 1, **antes** de apagar el WiFi.
- La ventana insuficiente no cuenta para las rachas, pero tampoco las reinicia. `cambio-sostenido-v1`
  se abrió con las ventanas 1, 3 y 4 de S1-W.
- M1 (W1) coincide: 3/3 ventanas sobre el umbral en S1-W y 1/3 en S1-V (la ventana 4).

S1 no pretende que el resto de las señales esté en calma. Lo que se comprueba es que la regla de
la fuente y las de confianza no se mezclan: la caída no abre alertas de confianza y la lluvia no
abre alertas de la fuente.

### Evidencia

| Qué | Dónde |
|---|---|
| Registros primarios | `datos/runs/<ejecución>/`: `fuente.jsonl`, `events.jsonl` (`error` de cada muestra perdida), `alerts.jsonl`, `windows.jsonl`, `manifest.json` (`fault_injection`, `source_stats`) |
| Métricas | [`evidencias/metricas/`](../evidencias/metricas/), una por ejecución (M3 en `metricas_adicionales`) |
| Portal | [`15-run-s1-wifi.png`](../evidencias/portal/15-run-s1-wifi.png) (salud de la fuente), [`16-alerta-fuente-s1-wifi.png`](../evidencias/portal/16-alerta-fuente-s1-wifi.png) (alerta, evidencia y resolución), [`17-graficas-s1-wifi.png`](../evidencias/portal/17-graficas-s1-wifi.png), [`18-run-s1-vivo.png`](../evidencias/portal/18-run-s1-vivo.png), [`19-graficas-s1-vivo.png`](../evidencias/portal/19-graficas-s1-vivo.png), [`13-graficas-s1.png`](../evidencias/portal/13-graficas-s1.png) (S1-R) |
| API | [`portal/api/alerta-fuente-s1-wifi.json`](../evidencias/portal/api/alerta-fuente-s1-wifi.json), [`portal/api/graficas-s1-vivo.json`](../evidencias/portal/api/graficas-s1-vivo.json) |
| Pruebas | [`tests/test_source_health.py`](../tests/test_source_health.py) |

## Límites

- **Qué simula la inyección.** Simula la falta de red a nivel de la fuente: `grab()` falla y la
  conexión se rechaza. No reproduce los modos de falla de la pila de red, como la latencia, los
  segmentos HLS a medias o el DNS. S1-W cubre ese caso con un corte real, y en él aparecieron
  justamente esos dos modos: el lector bloqueado sin error y la falla de DNS.
- **La caída medida es mayor que el corte.** Incluye la espera de la reconexión: con espera
  creciente, el último intento puede llegar hasta 20 s después de que vuelve la red.
- **Menos muestras durante la caída.** Cada muestra espera hasta 3 s, así que se registra
  aproximadamente una muestra cada 3 s, no cada 2 s. Los ticks perdidos quedan en
  `source_stats.dropped_source_wait`.
- **El corte real no se cronometró por fuera.** En S1-W no hay una marca independiente de cuándo
  se apagó y se encendió el WiFi; la duración sale de los registros del monitor.
- **Códigos de color en el registro.** Los errores de yt-dlp traían códigos de color ANSI. Desde
  esta revisión la fuente los quita antes de escribir `fuente.jsonl` (`error_text`); el
  `fuente.jsonl` de S1-W es anterior y los conserva, y el portal los filtra al mostrarlo.
- **G1 no es comparable con C1.** En S1-V y S1-W la inferencia tarda unos 100 ms (p50), frente a
  315 ms en C1, con el mismo modelo, `imgsz`, configuración de inferencia y entorno. La laptop
  estaba conectada a la corriente; es probable que en C1 estuviera con batería y en modo de ahorro,
  pero eso no quedó registrado. S1 no usa G1 ni M2 para nada.
