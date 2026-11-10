# Escenario adicional S5 · Poco tráfico

La fuente y el modelo funcionan, pero la escena queda casi vacía (de madrugada, con una calle cerrada o con el
encuadre movido). Una ventana necesita al menos 300 detecciones elegibles para que las reglas de confianza se
evalúen. Con poco tráfico no las reúne: queda `insuficiente` y el monitor deja de decir si la confianza es
normal, sin decir por qué.

**Por qué no es C1, C2, S1, S2, S3 ni S4.** C2 es confianza baja con evidencia suficiente. S1 también deja
ventanas `insuficiente`, pero porque la fuente no entrega frames. En S5 la fuente entrega todos los frames
(60/60 válidos) y el modelo responde: lo que falta son objetos. Antes de S5, las dos causas se veían igual
(`insuficiente`) y nada reaccionaba. S5 agrega su propia regla (`trafico-bajo-v1`), una reacción (la ventana
extendida), su registro (`trafico.jsonl`) y su verificación.

| Variante | Fallo inyectado | Lo que se espera |
|---|---|---|
| **S5-a** `zona:120:480` | De los 120 a los 600 s de stream (muestras 61–300, ventanas 2–5) se ve solo la zona central del cruce sobre una escena vacía: ~3 detecciones elegibles por frame frente a ~21 en C1. | Una alerta de poco tráfico. Las ventanas extendidas reúnen la evidencia (2 ventanas cada una) y evalúan la confianza: el monitor no queda ciego. Al volver el tráfico, la alerta se resuelve. |
| **S5-b** `vacia:120:360` | De los 120 a los 480 s (muestras 61–240, ventanas 2–4), solo la escena vacía: ~1 detección por frame. | Una alerta. Ni 3 ventanas juntas reúnen 300 elegibles: `insuficiente_aun_extendida`. La reacción **no alcanza**, y la resolución lo dice (mismo tiempo ciego con y sin reacción). |

## Cómo se simula el poco tráfico

Los S anteriores inyectan el fallo con el mismo reloj de stream, y S5 también. La escena vacía es la
**mediana temporal** de 150 frames de la grabación de C1
([`escena-vacia.jpg`](../evidencias/entradas/s5-poco-trafico/escena-vacia.jpg), generada con
`monitor.traffic escena-vacia`). La mediana quita lo que se mueve: personas y coches. Sobre ella:

- **`zona`**: se deja ver el frame real solo en la zona del cruce `[440, 330, 840, 470]`, con borde suavizado
  (σ = 25 px), para que no haya un corte que el modelo detecte. Quedan las personas y los coches que pasan por
  el centro del cruce.
- **`vacia`**: solo la escena vacía. El modelo detecta un único objeto fijo: la camioneta blanca estacionada
  arriba a la derecha, que quedó en la mediana (`car`, s = 0.706, en los 180 frames).

Ejemplos del mismo frame (muestra 150):
[original](../evidencias/entradas/s5-poco-trafico/000150-original.jpg) ·
[`zona`](../evidencias/entradas/s5-poco-trafico/000150-zona.jpg) ·
[`vacia`](../evidencias/entradas/s5-poco-trafico/000150-vacia.jpg). Los eventos y las capturas guardan el frame
inyectado, así que la evidencia muestra lo que vio el modelo.

## Configuración

[`config/escenario-trafico.yaml`](../config/escenario-trafico.yaml) (`cfg-02c4539fd0a8`) hereda todo de
`monitor.yaml` y solo agrega:

| Parámetro | Valor | Para qué |
|---|---|---|
| `low_traffic.consecutive_windows` | 1 | una ventana de poco tráfico basta para abrir `trafico-bajo-v1` (warning) |
| `low_traffic.max_extension_windows` | 3 | la reacción acumula como mucho 3 ventanas (6 min); si ni así alcanza, `insuficiente_aun_extendida` |
| `low_traffic.injection` | escena vacía, zona, borde | solo para `--inject-low-traffic` (escenario controlado) |

## Cómo funciona ([`src/monitor/traffic.py`](../src/monitor/traffic.py))

- **Clasificación de cada ventana cerrada** (`trafico.jsonl`, `type: ventana`):
  - `suficiente`: normal o fuera de rango;
  - **`poco_trafico`**: `insuficiente` con la fuente sana (`n_valid >= min_valid_frames`) y menos de
    `min_eligible` elegibles;
  - `fuente`: `insuficiente` por pocas muestras válidas, que es S1 y no abre esta regla.

  Cada registro guarda las elegibles por frame frente a las de la referencia (25.0 por frame:
  44 998 / 1 800 en `config/referencia.json`).
- **Diagnóstico** (la alerta): causa `poco_trafico`, frames válidos, elegibles por frame y fracción de la
  referencia.
- **Reacción: ventana extendida** (`type: extendida`):
  - las ventanas de poco tráfico seguidas se acumulan hasta reunir `min_eligible` elegibles;
  - sobre ese conjunto se evalúan las reglas de confianza: mismas señales, umbrales y población (s ≥ 0.25 en
    frames válidos).
  - Resultado: `normal` o `fuera_de_rango` con sus condiciones; `insuficiente_aun_extendida` si se llega a
    `max_extension_windows` sin la evidencia; `sin_completar` si el poco tráfico termina antes de reunirla.
  - Es un diagnóstico: no abre ni resuelve las alertas de confianza, que siguen definidas por ventana (R3).
- **Recuperación**: la primera ventana suficiente resuelve la alerta. Registra la duración, las ventanas de poco
  tráfico, las evaluaciones extendidas con su estado, las ventanas cubiertas y el **tiempo sin evaluar la
  confianza con y sin la reacción**.
- **Portal**: la alerta muestra el diagnóstico, la resolución y la tabla de la reacción (cada ventana con su
  clasificación y cada evaluación extendida con su resultado).
- **`monitor.evaluate`**: recalcula la regla junto con las ventanas, desde `events.jsonl`.

## Etapas y lo que se espera en cada una

| Etapa | Qué pasa | Dónde queda la evidencia |
|---|---|---|
| 1. Operación normal | ventana 1 normal (1 198 elegibles) | `windows.jsonl`, `trafico.jsonl` (`suficiente`) |
| 2. **Fallo** | la escena se vacía; la fuente sigue entregando 60/60 frames | eventos (`ok`, pocas detecciones), `manifest.json → low_traffic_fault` |
| 3. **Diagnóstico** | la ventana 2 queda `insuficiente` con la fuente sana: alerta `trafico-bajo-v1` con la causa y la comparación con la referencia | `alerts.jsonl → metric.diagnostico`, `/alertas/…` |
| 4. **Reacción** | ventanas extendidas: la confianza se evalúa sobre 2 ventanas juntas (S5-a) o no se alcanza ni con 3 (S5-b) | `trafico.jsonl` (`extendida`), consola, tabla de la alerta |
| 5. **Recuperación** | vuelve el tráfico: la primera ventana suficiente resuelve la alerta | `alerts.jsonl` (`update`) |
| 6. **Verificación posterior** | `monitor.evaluate` coincide; fuera del intervalo inyectado las detecciones son idénticas a C1; las estadísticas de cada ventana extendida son las de sus eventos | abajo |

## Reproducir

```bash
uv run python -m monitor.traffic escena-vacia datos/grabaciones/20261108T050904Z-c1-normal evidencias/entradas/s5-poco-trafico/escena-vacia.jpg
uv run python -m monitor.run --config config/escenario-trafico.yaml --replay datos/grabaciones/20261108T050904Z-c1-normal \
    --frames 360 --inject-low-traffic zona:120:480 --label s5a-poco-trafico
uv run python -m monitor.run --config config/escenario-trafico.yaml --replay datos/grabaciones/20261108T050904Z-c1-normal \
    --frames 300 --inject-low-traffic vacia:120:360 --label s5b-escena-vacia

# Verificar
uv run python -m monitor.evaluate datos/runs/20261110T053920Z-s5a-poco-trafico datos/runs/20261110T053925Z-s5b-escena-vacia
```

## Resultados

Las dos ejecuciones son reproducciones de la grabación de C1 a 1 frame / 2 s. `monitor.evaluate` coincide en
las dos (ventanas y alerta).

| | S5-a · zona | S5-b · escena vacía |
|---|---|---|
| Ejecución | `20261110T053920Z-s5a-poco-trafico` | `20261110T053925Z-s5b-escena-vacia` |
| Muestras inyectadas | 61–300 (ventanas 2–5) | 61–240 (ventanas 2–4) |
| Elegibles por frame inyectado | 2.97 (C1 en las mismas muestras: 21.0) | 1.00 (la camioneta estacionada) |
| Ventanas | 1 normal · 2–5 `insuficiente` (167, 179, 146, 221 elegibles) · 6 normal | 1 normal · 2–4 `insuficiente` (60 cada una) · 5 normal |
| Diagnóstico | fuente sana 60/60 · 2.78 elegibles por frame = **11 %** de la referencia | 60/60 · 1.0 por frame = **4 %** |
| Reacción | extendida **2+3**: `normal` (346 elegibles, prop. baja 0.471, media 0.530) · extendida **4+5**: `normal` (367, 0.469, 0.524) | extendida **2+3+4**: `insuficiente_aun_extendida` (180 elegibles < 300) |
| Resolución | ventana 6 · `duration_s` 480.4 · 4 ventanas de poco tráfico, **4 cubiertas** | ventana 5 · `duration_s` 360.0 · 3 ventanas, **0 cubiertas** |
| Tiempo sin evaluar la confianza | **0 s con la reacción**, 480 s sin ella | 360 s con y sin la reacción: no alcanza, y la resolución lo dice |
| Fuera del intervalo inyectado | 120/120 detecciones idénticas a C1 | 120/120 idénticas a C1 |

**Lectura.** En las ventanas extendidas de S5-a la proporción de baja confianza (0.47) es menor que la de C1
(0.60). Con poco tráfico quedan los objetos del centro del cruce, más grandes y aislados, y el modelo está más
seguro de ellos. Eso cumple la regla (no es baja confianza) y muestra que la población cambia con el tráfico.
Por eso la referencia de umbrales es de escenas con tráfico normal.

**Costo.** Las dos ejecuciones corrieron en paralelo en la misma CPU. La inferencia p50 fue de 913 y 939 ms
(G1), frente a ~300 ms de una sola ejecución, y M2 p95 fue de 1.5 s. Aun así, el espaciado entre muestras se
mantuvo entre 1.95 y 2.36 s, sin descartes por saturación.

### Evidencia

| Qué | Dónde |
|---|---|
| Registros primarios | [`datos/runs/20261110T053920Z-s5a-poco-trafico/`](../datos/runs/20261110T053920Z-s5a-poco-trafico/) y [`datos/runs/20261110T053925Z-s5b-escena-vacia/`](../datos/runs/20261110T053925Z-s5b-escena-vacia/): **`trafico.jsonl`**, `events.jsonl`, `windows.jsonl`, `alerts.jsonl`, `manifest.json` (`low_traffic_fault`) |
| Entradas | [`evidencias/entradas/s5-poco-trafico/`](../evidencias/entradas/s5-poco-trafico/) (escena vacía y su procedencia, ejemplos del frame 150) |
| Métricas | [`evidencias/metricas/20261110T053920Z-s5a-poco-trafico.json`](../evidencias/metricas/20261110T053920Z-s5a-poco-trafico.json), [`…-s5b-escena-vacia.json`](../evidencias/metricas/20261110T053925Z-s5b-escena-vacia.json) |
| Portal | [`54`](../evidencias/portal/54-alerta-trafico-s5a.png) (alerta S5-a con la reacción), [`55`](../evidencias/portal/55-alerta-trafico-s5b.png) (S5-b: la reacción no alcanza), [`56`](../evidencias/portal/56-run-s5a-ventanas.png) (ventanas de S5-a), [`57`](../evidencias/portal/57-graficas-s5a.png) (F1: las ventanas 2–5 sin evaluar) |
| API | [`portal/api/alerta-trafico-s5a.json`](../evidencias/portal/api/alerta-trafico-s5a.json), [`alerta-trafico-s5b.json`](../evidencias/portal/api/alerta-trafico-s5b.json) |
| Pruebas | [`tests/test_traffic.py`](../tests/test_traffic.py) (5 pruebas) |

## Límites

- **El poco tráfico es simulado.** La escena vacía es una mediana: conserva lo que estuvo quieto (la camioneta)
  y una textura tenue donde hubo multitudes. En una madrugada real habría además otra iluminación, y eso
  movería la confianza. La lógica de la regla no depende de cómo se produjo el poco tráfico, solo de los
  conteos.
- **Menos resolución temporal.** Una evaluación extendida de 2 ventanas (4 min) puede diluir un cambio corto de
  confianza. Por eso no abre alertas de confianza: es un diagnóstico y queda registrado como tal.
- **El umbral de poco tráfico es el mismo `min_eligible` de las ventanas (300).** No se calibró por hora del día.
  Una escena con tráfico moderado (≈ 5 por frame) alterna ventanas suficientes y de poco tráfico, y la alerta se
  abriría y resolvería varias veces. Se observó con una zona más grande en una prueba previa sin pausa: la
  ventana 5 reunió 335 elegibles y resolvió la alerta aunque el tráfico seguía bajo. Por eso la resolución dice
  `evidencia_suficiente` y no "volvió el tráfico".
