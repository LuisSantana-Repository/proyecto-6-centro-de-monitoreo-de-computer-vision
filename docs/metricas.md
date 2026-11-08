# Métricas base por grupo (G1–G7)

Los siete grupos **aplican** a esta arquitectura (G = 7): hay procesamiento de inferencias (G1), una
fuente visual en stream (G2), un proceso con recursos medibles en CPU/RAM (G3), salidas puntuadas
(G4), eventos e imágenes persistidos (G5), una fuente y una versión de modelo identificadas (G6) y
reglas de alerta (G7). No se excluye ningún grupo.

Todas las métricas se calculan con [`src/monitor/metrics.py`](../src/monitor/metrics.py) desde los
registros primarios de cada ejecución (`events.jsonl`, `alerts.jsonl`, `manifest.json`):

```bash
uv run python -m monitor.metrics datos/runs/<run_id> --out-dir evidencias/metricas
```

**Regla común para cero observaciones (M3):** si la población de una métrica está vacía, el valor es
`null` con `estado = "sin_datos"`; nunca se reporta 0. Si la métrica no representa el sistema en esa
ejecución (FPS en un reprocesamiento sin pausa), `estado = "no_aplica"`.
**Ventana:** las métricas por ventana usan las mismas ventanas de 60 eventos (2 min a 0.5 FPS) que
las alertas; la última ventana de una ejecución puede ser incompleta (`completa: false`).
**Percentiles:** por rango más cercano (*nearest-rank*), de modo que el valor siempre es una
observación real y se puede localizar en `events.jsonl`.

| Grupo | Métrica base | Unidad | Fórmula | Población y exclusiones |
|---|---|---|---|---|
| G1 Salud y rendimiento | **Tiempo de inferencia p50 / p95** | ms por frame | percentil 50 y 95 de `timings.inference_ms` | eventos con inferencia válida (`ok`, `no_detections`); excluye fallos e entradas inválidas. Solo el modelo: no incluye pre/postproceso, captura ni escritura |
| G2 Captura y stream | **FPS de procesamiento** | frames/s | `(n − 1) / (timestamp_último − timestamp_primero)` | eventos con inferencia válida; `no_aplica` en reprocesamientos `--no-pace`. Referencia: declarado 0.5 FPS |
| G3 Recursos | **RAM del proceso (RSS) p95** | MB | percentil 95 de `resources.rss_mb` | una muestra por evento del proceso del monitor (no del equipo); eventos sin muestra se excluyen |
| G4 Predicciones | **Proporción de baja confianza** | proporción | elegibles con `s < lower` / elegibles | detecciones con `s ≥ analysis_floor` (0.25) en frames con inferencia válida; excluye candidatas < 0.25 (dependen de la multitud) y frames sin inferencia |
| G5 Datos y trazabilidad | **Cobertura de trazabilidad** | proporción de eventos | eventos sin problemas / eventos | todos los eventos. Un evento es completo si tiene los campos de identidad, su `config_ref` y SHA del modelo coinciden con el manifest, sus umbrales y zonas son coherentes con la configuración y su `image_ref` (si existe) resuelve a un archivo no vacío |
| G6 Fuentes y versiones | **Versión esperada frente a reportada** | proporción de eventos | eventos con `model_ref` = versión configurada / eventos | todos los eventos; la versión esperada sale de `config/monitor.yaml` (`name@sha256[:12]`); se reporta también la distribución de versiones observadas |
| G7 Alertas | **Alertas activas** | número de alertas | episodios abiertos en un instante (abiertos ≤ t y no resueltos ≤ t) | episodios registrados en `alerts.jsonl`; se reporta al final de cada ventana y al final de la ejecución |

## M2 · Adquisición (instrumentación)

| Grupo | Dónde se mide |
|---|---|
| G1 | `detector.py`: `r.speed["inference"]` de Ultralytics → `timings.inference_ms` del evento (también `predict_total_ms` con `time.perf_counter`) |
| G2 | `run.py`: `timestamp` UTC de emisión de cada evento; contexto en `manifest.source_stats` (frames recibidos, muestreados, descartados por muestreo / espera de la fuente / saturación, reconexiones) |
| G3 | `run.py` → `ResourceProbe` (psutil): `rss_mb`, `cpu_percent` del proceso y `disk_free_gb`, una muestra por evento |
| G4 | `run.py`: puntuación y zona de cada detección del evento; `alerts.window_stats` |
| G5 | `events.jsonl` + `manifest.json` + archivos de `imagenes/`; validación del contrato con Pydantic |
| G6 | `detector.py`: `model_ref = name@sha256[:12]` del artefacto verificado al arrancar |
| G7 | `alerts.py` → `alerts.jsonl` (`type: alert` al abrir, `type: update` con `status: resolved` al resolver) |

## M4 · Trazabilidad

Cada valor por ventana guarda `first_event_id`, `last_event_id`, `start` y `end`; cada problema de G5
lista los `event_id` afectados; G6 lista las versiones observadas con su conteo; G7 enlaza los
`alert_id`. En el portal, `/runs/<run_id>` muestra las ventanas y `/eventos/<event_id>` el registro
primario de cualquier observación.

## M5 · Verificación

- Recalcular: `uv run python -m monitor.metrics datos/runs/<run_id>` (desde los JSONL; no usa la base
  de datos ni el portal).
- Pruebas de los cálculos y casos borde (cero observaciones, percentiles, enlaces rotos, zonas
  incoherentes, alertas activas en el tiempo): `uv run pytest tests/test_metrics.py`.
- Resultados guardados: `evidencias/metricas/<run_id>.json`.
- Recálculo independiente del código del monitor (solo biblioteca estándar), por ejemplo G1 y G4 de C2.
  Da `G1 p50 = 306.95` y `G4 = 0.7397 n = 7348`, igual que la tabla:

```python
import json, math
ev = [json.loads(l) for l in open("datos/runs/20261108T163302Z-c2-baja-confianza/events.jsonl")]
inf = sorted(e["timings"]["inference_ms"] for e in ev if e["status"] in ("ok", "no_detections"))
print("G1 p50 =", inf[math.ceil(0.5 * len(inf)) - 1])
s = [d["score"] for e in ev if e["status"] in ("ok", "no_detections") for d in e["detections"] if d["score"] >= 0.25]
print("G4 =", round(sum(x < 0.5 for x in s) / len(s), 4), "n =", len(s))
```

## Resultados

Ejecuciones oficiales con `cfg-0b5e664b118c` y `yolo11n@sha256:0ebbc80d4a76`. Las C1 y C2 se
reprodujeron a ritmo de stream: 1 frame cada 2 s durante 15 min.

| Métrica base | Referencia mañana | Referencia tarde | **C1 · normal** | **C2 · baja confianza** |
|---|---|---|---|---|
| G1 inferencia p50 / p95 (ms) | 231.6 / 305.4 ¹ | 234.3 / 309.5 ¹ | **314.9 / 442.8** | **307.0 / 411.6** |
| G2 FPS de procesamiento | no aplica ² | no aplica ² | **0.4999** (declarado 0.5) | **0.4999** (declarado 0.5) |
| G3 RSS p95 (MB) | sin datos ³ | sin datos ³ | **446.4** (p50 442.0) | **444.9** (p50 433.7) |
| G4 proporción de baja confianza | 0.5595 (n = 28 925) | 0.6405 (n = 16 073) | **0.6375** (n = 8 867) | **0.7397** (n = 7 348) |
| G5 cobertura de trazabilidad | 1.0 (900; 60 imágenes) | 1.0 (900; 148 imágenes) | **1.0** (450; 59 imágenes) | **1.0** (450; 93 imágenes) |
| G6 versión esperada = reportada | 1.0 | 1.0 | **1.0** | **1.0** |
| G7 alertas activas al final (máx. por ventana) | 0 (0) | 0 (0) | **0** (0) | **2** (2); 4 emitidas, 2 resueltas |

¹ **Reprocesamiento `--no-pace`.** Los frames se encadenan sin pausa, así que el tiempo es de CPU
bajo carga continua. En C1 y C2 la latencia es mayor. Las causas posibles son la CPU en reposo entre
frames y otras cargas del equipo; no se aisló cuál pesa más. En todos los casos queda muy por debajo del intervalo de 2 s, y la fuente no descartó frames por saturación
(`dropped_saturation = 0`).
² En `--no-pace` el ritmo no representa el stream: `estado = "no_aplica"`.
³ Las referencias se procesaron antes de agregar la instrumentación de recursos (`resources`). Por
la regla de cero observaciones, G3 es `null` / `sin_datos`, no 0.

**Lectura:**
- G2 confirma que el monitor sostiene el muestreo declarado.
- G3 es estable: no crece durante los 15 min.
- G4 separa la noche (0.74) del día (0.56–0.64).
- G5 y G6 confirman que todos los eventos son trazables a la configuración y al modelo desplegados.
- G7 muestra la diferencia entre C1 (sin alertas) y C2 (dos alertas abiertas al final: la de cambio
  sostenido y la última de proporción).

Los valores por ventana de cada métrica están en `ventanas` dentro de cada
`evidencias/metricas/<run_id>.json`.
