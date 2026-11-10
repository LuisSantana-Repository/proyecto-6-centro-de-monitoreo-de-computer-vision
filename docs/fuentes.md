# Extra F7 · Varias fuentes

El monitor vigila dos cámaras en vivo del mismo cruce de Shibuya, al mismo tiempo, cada una con su propia
configuración, referencia y umbrales:

| Fuente | Cámara | Configuración | Referencia | Muestreo |
|---|---|---|---|---|
| `shibuya-scramble-01` | desde arriba ([`dfVK7ld38Ys`](https://www.youtube.com/watch?v=dfVK7ld38Ys)) | [`config/monitor.yaml`](../config/monitor.yaml) | [`config/referencia.json`](../config/referencia.json): 2 grabaciones (mañana y tarde), 30 ventanas | 1 frame / 2 s |
| `shibuya-scramble-02` | desde la calle, ANN/TV Asahi ([`5B4LmH4JbnY`](https://www.youtube.com/watch?v=5B4LmH4JbnY)) | [`config/fuente-02.yaml`](../config/fuente-02.yaml) (hereda de `monitor.yaml`) | [`config/referencia-fuente-02.json`](../config/referencia-fuente-02.json): 1 grabación (tarde), 7 ventanas | 1 frame / 5 s |

**Por qué cada fuente necesita lo suyo.** El mismo modelo, en la misma escena desde otro ángulo, produce otra
distribución de confianza. Las dos referencias se calculan con la misma regla (media ± 2 sd por ventana), pero
dan umbrales distintos:

| | Fuente 01 | Fuente 02 |
|---|---|---|
| Detecciones por frame (todas / elegibles s ≥ 0.25) | 87–104 / 25.0 | 96 / 24.4 |
| Proporción baja por ventana: media ± sd (rango) | 0.598 ± 0.063 (0.459–0.720) | 0.582 ± **0.017** (0.558–0.612) |
| Puntuación media por ventana: media ± sd | 0.480 ± 0.018 | 0.487 ± **0.007** |
| **Umbral `baja-confianza-v1`** (prop. baja >) | 0.724 | **0.615** |
| **Umbral `cambio-sostenido-v1`** (media <) | 0.445 | **0.472** |
| Captura por baja confianza (media del frame <) | 0.38 | 0.43 |
| Umbral de W1 (M1) | 0.048 | 0.014 |
| Segmentos HLS del stream | 2 s | **5 s** |

La fuente 02 es mucho más estable: la cámara de calle ve siempre la misma multitud a lo lejos. Con los umbrales
de la 01, un cambio en la 02 tendría que ser enorme para alertar. Al revés, con los de la 02, la 01 alertaría
por su variación normal. La sección de resultados lo muestra con datos.

**Un hallazgo del stream.** El lector pide un frame cada `sample_interval_s` y espera el siguiente que entregue
el stream. La fuente 02 entrega el video en segmentos HLS de 5 s (la 01, de 2 s), así que pedir un frame cada
2 s solo esperaba al siguiente segmento: la grabación de referencia muestreó cada ~5.1 s en lugar de 2 s.
`fuente-02.yaml` declara `sample_interval_s: 5.0`, así que su ventana de 60 muestras dura 5 min. En vivo
sostuvo 0.1997 FPS de 0.2 declarados (G2), sin muestras perdidas.

## Qué agrega

- **Referencia con identidad de fuente.** `monitor.reference` guarda el `source_id` (sin el sufijo `-replay`) y
  se niega a mezclar ejecuciones de fuentes distintas. La referencia de la 01 se regeneró con las mismas
  ejecuciones: solo cambian `generated_at` y el campo nuevo, y los umbrales son idénticos.
- **Protección al arrancar.** `monitor.run` no arranca si la referencia de su configuración es de otra fuente
  (`sources.check_reference`): "La referencia … es de la fuente shibuya-scramble-01, pero esta configuración
  monitorea shibuya-scramble-02…".
- **W1 por fuente.** `reference.distribution_file` permite a cada fuente su referencia de distribución para M1;
  `monitor.metrics` y `/graficas` usan la de la fuente de cada ejecución.
- **Portal.**
  - `/fuentes` (y `/api/fuentes`): una tarjeta por fuente con su stream, ejecuciones (en vivo, replay, en curso),
    eventos, detecciones por frame, ventanas por estado, alertas y una tabla de configuraciones con su
    referencia, **de qué fuente es** esa referencia y sus umbrales.
  - Filtro **Fuente** en `/eventos`, `/alertas`, `/historico` y en sus APIs. En replay, el `source_id` de los
    eventos lleva `-replay`: el filtro incluye los dos.
  - En `/historico` (F6), con fuentes de umbrales distintos no se dibuja una línea común, y cada punto marca si
    cruzó el umbral de *su* ejecución.
- **Verificación** (`monitor.sources --verificar`). Desde los registros primarios de cada ejecución, comprueba:
  - que su referencia es de su fuente;
  - que sus umbrales son exactamente los derivados de esa referencia;
  - que todos sus eventos son de esa fuente.
- **Contrafactual** (`monitor.sources --cruzado RUN --config OTRA`): reevalúa las ventanas de una ejecución con
  los umbrales de otra configuración y lo compara con lo registrado.

## Ejecuciones

| Ejecución | Fuente | Qué | Escena (JST) |
|---|---|---|---|
| `20261110T140928Z-referencia-fuente02` | 02 | replay de la grabación de referencia (456 muestras), con la configuración final | 15:34–16:11, martes |
| `20261110T071439Z-f7-vivo-fuente01` | 01 | **en vivo, al mismo tiempo que la siguiente**: 600 muestras | 16:15–16:34 |
| `20261110T071439Z-f7-vivo-fuente02` | 02 | en vivo, simultánea: 240 muestras | 16:17–16:32 |
| `20261110T073617Z-f7-anochecer-fuente01` | 01 | en vivo, simultánea, durante el atardecer y la noche: 600 muestras | 16:37–23:08 (suspensión del equipo, ver abajo) |
| `20261110T073617Z-f7-anochecer-fuente02` | 02 | en vivo, simultánea: 240 muestras | 16:38–23:05 |

Las dos parejas corrieron en paralelo en la misma CPU. Las capturas [`58`](../evidencias/portal/58-vivo-dos-fuentes.png)
y [`59`](../evidencias/portal/59-vivo-fuente02.png) muestran la vista en vivo de cada una durante la primera pareja.

## Resultados

**De día (16:15–16:34 JST).** Las dos fuentes quedan normales con sus umbrales: 0 alertas. La fuente 01 tiene
una ventana fuera de rango aislada (media 0.441 < 0.445), que no completa las 3 seguidas de
`cambio-sostenido-v1`.

**Al anochecer y de noche.**

| | Fuente 01 (umbrales 0.724 / 0.445) | Fuente 02 (umbrales 0.615 / 0.472) |
|---|---|---|
| Ventanas | 1–3 fuera de rango (media 0.426–0.428), 4 normal, 5–10 fuera de rango (prop. baja 0.73–0.76) | 1–2 normales, **3–4 fuera de rango** (prop. baja 0.621, 0.628; media 0.470, 0.466) |
| Alertas | `cambio-sostenido-v1` al anochecer (resuelta en la ventana 4), `baja-confianza-v1` y `cambio-sostenido-v1` de noche | `baja-confianza-v1` en la ventana 3 |

De noche, la fuente 02 se degrada mucho menos que la 01 (0.62 frente a 0.75 de proporción baja), pero respecto
de *su* referencia es un cambio claro, y su regla lo detecta.

**Contrafactual: cada ejecución con los umbrales de la otra fuente** (`monitor.sources --cruzado`, salida en
[`evidencias/fuentes/cruzado.txt`](../evidencias/fuentes/cruzado.txt)):

| Ejecución | Con sus umbrales | Con los de la otra fuente | Efecto |
|---|---|---|---|
| fuente 02, día | 0/4 fuera de rango, 0 alertas | 0/4, 0 alertas | igual |
| fuente 02, anochecer y noche | **2/4 fuera de rango, 1 alerta** | **0/4, 0 alertas** | con los umbrales de la 01 el cambio **se pierde** |
| fuente 01, día | 1/10 fuera de rango, 0 alertas | **6/10, 2 alertas** (`baja-confianza-v1`, `cambio-sostenido-v1`) | con los de la 02, **falsas alarmas** de día |
| fuente 01, anochecer y noche | 9/10, 3 alertas | 9/10, 4 alertas | más alertas, mismo diagnóstico |

**Verificación.**
- `monitor.sources --verificar` sobre las 21 ejecuciones versionadas (18 de la fuente 01 y 3 de la 02): cada una
  usa la referencia de su fuente y sus umbrales derivados, y sus eventos son de esa fuente
  ([`verificacion-versionadas.txt`](../evidencias/fuentes/verificacion-versionadas.txt)).
- `monitor.evaluate` coincide en las 5 ejecuciones nuevas.
- La referencia de la fuente 02 calculada en una carpeta temporal y la del replay oficial dan los mismos umbrales.

**Dos fallos reales encontrados.**
1. **La grabación de referencia de la fuente 02** (`20261110T063255Z-referencia-fuente02`, en vivo, no
   versionada) se lanzó cuando `fuente-02.yaml` todavía heredaba la referencia de la 01: corrió con umbrales de
   otra fuente. `/fuentes` lo muestra en su tabla de configuraciones ("otra: shibuya-scramble-01",
   [`60`](../evidencias/portal/60-fuentes.png)). La protección de arranque se agregó por eso: hoy ese arranque se
   rechaza (prueba `test_run_refuses_a_reference_of_another_source`). Esa grabación se detuvo a mano a las 456
   muestras para quedarse en luz de día, así que no tiene `manifest.json` y el panel (F5) la marca sin cerrar.
2. **Suspensión del equipo.** La pareja del anochecer se congeló unas 6 h a la vez en las dos fuentes, cuando el
   equipo se suspendió: hay huecos de 85 min y 4.8 h entre muestras consecutivas, y siguió sola al despertar (la
   fuente 01 se reconectó en 3 s). G2 lo mide: 0.025 y 0.010 FPS en esas ejecuciones, frente a 0.5 y 0.2
   declarados. Las ventanas se cuentan por muestras, así que la ventana 5 de la fuente 01 y la 2 de la 02 abarcan
   el hueco: mezclan el atardecer con la noche.

## Reproducir

```bash
# referencia de la fuente 02 (la grabación no se versiona)
uv run python -m monitor.run --config config/fuente-02.yaml --frames 900 --record --label referencia-fuente02
uv run python -m monitor.run --config config/fuente-02.yaml --replay datos/grabaciones/<grabación> --no-pace --label referencia-fuente02
uv run python -m monitor.reference datos/runs/<replay> --config config/fuente-02.yaml --out config/referencia-fuente-02.json
uv run python -m monitor.drift --build datos/runs/<replay> --config config/fuente-02.yaml --ref config/referencia-distribucion-fuente-02.json

# las dos cámaras a la vez (dos terminales)
uv run python -m monitor.run --config config/monitor.yaml   --frames 600 --label f7-vivo-fuente01
uv run python -m monitor.run --config config/fuente-02.yaml --frames 240 --label f7-vivo-fuente02

# verificar y contrafactual
uv run python -m monitor.sources --verificar
uv run python -m monitor.sources --cruzado datos/runs/20261110T073617Z-f7-anochecer-fuente02 --config config/monitor.yaml
```

## Evidencia

| Qué | Dónde |
|---|---|
| Código | [`src/monitor/sources.py`](../src/monitor/sources.py); `source_id` en [`reference.py`](../src/monitor/reference.py); protección en [`run.py`](../src/monitor/run.py); `ref_path` en [`drift.py`](../src/monitor/drift.py); portal (`/fuentes`, filtros) |
| Configuración y referencias | [`config/fuente-02.yaml`](../config/fuente-02.yaml) (`cfg-d1ae5ba10d53`), [`config/referencia-fuente-02.json`](../config/referencia-fuente-02.json), [`config/referencia-distribucion-fuente-02.json`](../config/referencia-distribucion-fuente-02.json) |
| Verificación | [`evidencias/fuentes/`](../evidencias/fuentes/): versionadas (coincide), base local y contrafactual |
| Métricas | [`evidencias/metricas/`](../evidencias/metricas/) de las 5 ejecuciones nuevas (G2 por fuente, M1 con la referencia de cada fuente) |
| Portal | [`58`](../evidencias/portal/58-vivo-dos-fuentes.png)–[`59`](../evidencias/portal/59-vivo-fuente02.png) (vista en vivo de las dos a la vez), [`60`](../evidencias/portal/60-fuentes.png) (`/fuentes`), [`61`](../evidencias/portal/61-historico-dos-fuentes.png) (histórico de las dos: umbrales distintos), [`62`](../evidencias/portal/62-historico-fuente02-hora.png) (histórico filtrado por la fuente 02), [`63`](../evidencias/portal/63-alertas-fuente02.png) (alertas filtradas), [`64`](../evidencias/portal/64-graficas-anochecer-fuente02.png)–[`65`](../evidencias/portal/65-graficas-anochecer-fuente01.png) (cada fuente con sus umbrales); [`portal/api/fuentes.json`](../evidencias/portal/api/fuentes.json) |
| Pruebas | [`tests/test_sources.py`](../tests/test_sources.py) (6 pruebas) |

## Límites

- **Una sola referencia para la fuente 02**: 35 min de una tarde, 7 ventanas. Sus umbrales son estrechos
  (sd 0.017) y probablemente demasiado sensibles a cambios normales que no se vieron esa tarde. La fuente 01
  usa dos horarios. Habría que sumar una grabación de mañana.
- **Las dos cámaras comparten la CPU.** Con las dos en vivo, la inferencia p50 fue de 122 y 141 ms. La 01
  perdió 33 de 600 muestras esperando a la fuente (espaciado p50 2.12 s), como le pasa también cuando corre sola.
- **El muestreo depende del stream.** Si el proveedor cambia la duración de los segmentos HLS, hay que ajustar
  `sample_interval_s` de esa fuente. Muestrear por tiempo del stream (marcas de tiempo de los frames) en lugar
  del reloj evitaría depender de eso.
- **Las ventanas se cuentan por muestras, no por tiempo.** Tras una suspensión del equipo, una ventana puede
  abarcar horas. G2 lo revela; una regla que corte la ventana ante un hueco grande lo evitaría.
- **El letrero sobrepuesto** de la fuente 02 ("LIVE Shibuya, Tokyo") queda dentro del frame. No genera
  detecciones elegibles (0 dentro del letrero en los 1 392 eventos de la fuente 02), pero no se enmascaró.
