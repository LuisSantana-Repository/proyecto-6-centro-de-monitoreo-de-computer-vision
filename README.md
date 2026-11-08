# Proyecto 6: Centro de monitoreo de computer vision

**Autor:** Luis Santana (entrega individual) · ITESO, MLOps · Repositorio:
https://github.com/LuisSantana-Repository/proyecto-6-centro-de-monitoreo-de-computer-vision

Monitor de un modelo de detección de objetos sobre una cámara en vivo: muestrea el stream, ejecuta
inferencia, clasifica cada detección en zonas de confianza, persiste eventos e imágenes, calcula
métricas por ventana, emite alertas y las muestra en un portal web.

- **Fuente visual:** cámara en vivo del cruce de Shibuya, Tokio (YouTube, canal *FNN Prime Online*,
  https://www.youtube.com/watch?v=dfVK7ld38Ys), 1280×720, muestreada a **1 frame cada 2 s**. Las
  sesiones en vivo se grabaron frame a frame para poder reproducirlas en las mismas condiciones.
- **Modelo:** YOLO11n preentrenado en COCO (Ultralytics), CPU, `imgsz` 1280. Ficha:
  [`modelo/README.md`](modelo/README.md).
- **Tarea:** detección de personas, bicicletas, autos, motos, autobuses y camiones.

## Arquitectura

```
YouTube HLS ──yt-dlp──▶ LiveYouTubeSource ─┐   (o ReplaySource: grabación a 1 frame / 2 s)
                        hilo lector grab()   │
                        muestreo cada 2 s    ▼
                                     Detector (YOLO11n, SHA-256 verificado)
                                             │ detecciones + tiempos
                                             ▼
                          política de confianza: aceptada / duda / baja
                                             │
                     ┌───────────────────────┼─────────────────────────┐
                     ▼                       ▼                         ▼
            events.jsonl (primario)   imagenes/ (política de     WindowEvaluator
            + SQLite datos/monitor.db  captura)                  ventanas de 60 frames
                                                                 → reglas → alerts.jsonl
                                             │
                     métricas G1–G7 (monitor.metrics) · portal FastAPI (lectura de SQLite)
```

| Módulo | Responsabilidad |
|---|---|
| [`source.py`](src/monitor/source.py) | Fuente en vivo (yt-dlp + OpenCV, reconexión con espera creciente, diagnóstico en `fuente.jsonl`, corte inyectable) y replay |
| [`detector.py`](src/monitor/detector.py) | Carga del modelo, inferencia y tiempos |
| [`policy.py`](src/monitor/policy.py) | Zonas de confianza con dos umbrales |
| [`events.py`](src/monitor/events.py) | Contrato del evento de inferencia (Pydantic) |
| [`alerts.py`](src/monitor/alerts.py) | Ventanas, estado (normal / fuera de rango / insuficiente), reglas y episodios de alerta |
| [`run.py`](src/monitor/run.py) | Recorrido principal, política de captura, recursos del proceso, manifest |
| [`store.py`](src/monitor/store.py), [`ingest.py`](src/monitor/ingest.py) | Persistencia SQLite e ingesta idempotente desde los JSONL |
| [`reference.py`](src/monitor/reference.py) | Estadísticas de referencia y umbrales derivados |
| [`evaluate.py`](src/monitor/evaluate.py) | Recalcula ventanas y alertas desde `events.jsonl` y las compara con lo registrado |
| [`metrics.py`](src/monitor/metrics.py) | Métricas base G1–G7 y adicionales M2–M3 por ejecución y por ventana |
| [`drift.py`](src/monitor/drift.py) | Métrica adicional M1: W1 de las puntuaciones frente a la referencia |
| [`portal/`](src/monitor/portal/) | Portal FastAPI: eventos (filtros), detalle con cajas, alertas, runs, configuración |

Documentación de decisiones:
- [`docs/umbrales.md`](docs/umbrales.md): umbrales, zonas, referencia, ventanas y reglas.
- [`docs/metricas.md`](docs/metricas.md): inventario G1–G7 con M1–M5 y resultados.
- [`evidencias/indice.md`](evidencias/indice.md): índice de evidencias por criterio.

## Instalación

Requisitos: Python 3.12 y [uv](https://docs.astral.sh/uv/). Las dependencias están fijadas en `uv.lock`
(ultralytics, torch CPU, opencv-python, yt-dlp, fastapi, uvicorn, jinja2, pydantic, psutil).

```bash
uv sync
uv run python -m monitor.download_model     # descarga yolo11n.pt y verifica el SHA-256
uv run pytest -q                            # pruebas
```

Toda la configuración (fuente, modelo, muestreo, umbrales, referencia, ventanas, reglas, captura)
está en [`config/monitor.yaml`](config/monitor.yaml). Cada evento lleva `config_ref` (hash de esa
configuración, `cfg-0b5e664b118c` en las evidencias) y `model_ref` (`yolo11n@sha256:0ebbc80d4a76`).

## Comandos

| Para qué | Comando |
|---|---|
| Monitorear la cámara en vivo (N frames, 1 cada 2 s) | `uv run python -m monitor.run --frames N --label <etiqueta>` |
| ... y grabar cada frame para reproducirlo después | `... --record` (se guarda en `datos/grabaciones/<run_id>/`) |
| Reproducir una grabación como stream (1 frame / 2 s) | `uv run python -m monitor.run --replay <carpeta> --label <etiqueta>` |
| Reprocesar una grabación sin esperar (análisis) | `... --replay <carpeta> --no-pace` |
| Estadísticas de referencia y umbrales derivados | `uv run python -m monitor.reference datos/runs/<ref1> datos/runs/<ref2> --out config/referencia.json` |
| Recalcular ventanas y alertas y verificarlas | `uv run python -m monitor.evaluate datos/runs/<run_id>` |
| Métricas G1–G7 (+ adicionales M1–M3) | `uv run python -m monitor.metrics datos/runs/<run_id> --out-dir evidencias/metricas` |
| Desplazamiento de la distribución (M1) | `uv run python -m monitor.drift datos/runs/<run_id>` (referencia: `--build <ref1> <ref2>`) |
| Escenario S1: corte de la fuente | `uv run python -m monitor.run --config config/escenario-interrupcion.yaml --inject-outage 180:90 ...` |
| Importar runs a SQLite (idempotente) | `uv run python -m monitor.ingest [datos/runs/<run_id> ...]` |
| Portal de eventos y alertas | `uv run python -m monitor.portal` → http://127.0.0.1:8000 |

Cada ejecución escribe en `datos/runs/<run_id>/`:
- `events.jsonl`: registro primario, un evento por frame;
- `windows.jsonl` y `alerts.jsonl`;
- `manifest.json`: configuración, modelo, entorno y contadores de la fuente;
- `imagenes/`: los frames capturados.

Además inserta todo en `datos/monitor.db`.

## Escenarios

Las grabaciones de cada sesión en vivo están en `datos/grabaciones/` (sin versionar por tamaño).
Una muestra de cada una está en `evidencias/entradas/`. Las ejecuciones oficiales reproducen la
grabación completa a 1 frame cada 2 s con la configuración `cfg-0b5e664b118c`:

```bash
# referencia (mañana y tarde, Tokio): base de los umbrales
uv run python -m monitor.run --replay datos/grabaciones/20261107T220327Z-referencia --no-pace --label referencia-manana
uv run python -m monitor.run --replay datos/grabaciones/20261108T053156Z-referencia-tarde --no-pace --label referencia-tarde
# C1 · operación normal: 14:09 JST, 450 frames (15 min)
uv run python -m monitor.run --replay datos/grabaciones/20261108T050904Z-c1-normal --label c1-normal
# C2 · baja confianza: misma cámara de noche, 00:19 JST, 450 frames (15 min)
uv run python -m monitor.run --replay datos/grabaciones/20261108T151924Z-c2-noche --label c2-baja-confianza
```

C2 usa una condición real de la escena (iluminación nocturna). Ninguna puntuación se modifica: los
umbrales y las reglas se fijaron con la referencia diurna antes de procesar C2.

## Resultados

| | C1 · operación normal | C2 · baja confianza (noche) |
|---|---|---|
| Ejecución | `20261108T161753Z-c1-normal` | `20261108T163302Z-c2-baja-confianza` |
| Eventos (ok) / imágenes | 450 (450) / 59 | 450 (450) / 93 |
| Ventanas | 7 normal + 1 incompleta | 7 fuera de rango + 1 incompleta |
| Proporción de baja confianza (G4) | 0.6375 (ventanas 0.585 – 0.696; umbral 0.724) | 0.7397 (ventanas 0.683 – 0.793) |
| Media de puntuación (ventanas) | 0.452 – 0.494 (umbral 0.445) | 0.402 – 0.440 |
| Alertas | **0** | **4 episodios**: `baja-confianza-v1` (warning) en las ventanas 1, 4 y 6, dos de ellos resueltos; `cambio-sostenido-v1` (critical) desde la ventana 3, abierto |
| Inferencia p50 / p95 (G1) | 314.9 / 442.8 ms | 307.0 / 411.6 ms |
| FPS de procesamiento (G2) | 0.4999 (declarado 0.5), 0 frames descartados | 0.4999, 0 frames descartados |
| RSS p95 (G3) | 446 MB | 445 MB |
| Trazabilidad (G5) / versión (G6) | 1.0 / 1.0 | 1.0 / 1.0 |

- **Reglas recalculables.** `monitor.evaluate` recalcula ventanas y alertas desde `events.jsonl`, y
  coinciden con lo registrado en las cuatro ejecuciones oficiales.
- **Inferencia determinista.** La muestra de 60 frames de C2 en `evidencias/entradas/` reproduce las
  mismas detecciones (60/60 eventos) y la misma alerta de la ventana 1.
- **Más detalle:** [`docs/metricas.md`](docs/metricas.md), [`docs/umbrales.md`](docs/umbrales.md) §8 y
  [`evidencias/indice.md`](evidencias/indice.md).

## Autoría y componentes reutilizados

**Aportación propia** (todo el código en `src/monitor/`, `tests/`, la configuración y la documentación):
- el muestreo del stream con su contabilidad de frames descartados;
- el contrato del evento;
- la política de zonas;
- la referencia y la derivación de umbrales;
- ventanas, reglas y episodios de alerta;
- la política de captura;
- la persistencia, el portal y las métricas G1–G7;
- la grabación y la selección de escenarios.

El desarrollo se hizo con ayuda de un asistente de IA (Claude Code). Las decisiones de fuente,
escenarios y grabación son del autor.

**Componentes reutilizados (no modificados):**

| Componente | Uso | Licencia |
|---|---|---|
| YOLO11n preentrenado en COCO, Ultralytics (`yolo11n.pt` v8.3.0) | modelo de detección | AGPL-3.0 |
| Biblioteca `ultralytics` | inferencia, preprocesamiento y NMS | AGPL-3.0 |
| `yt-dlp` | resolver la URL HLS del stream de YouTube | Unlicense |
| OpenCV, FastAPI, Jinja2, Pydantic, psutil, SQLite | lectura de video, portal, contratos, recursos, persistencia | BSD / MIT / Apache |
| Stream del cruce de Shibuya, FNN Prime Online (YouTube) | fuente visual pública; se usa solo para fines académicos | derechos de su titular |

No se reutiliza código del Proyecto 5; la fuente es nueva.

## Límites conocidos

- **Margen estrecho de C1.** La media de puntuación de C1 queda cerca del umbral de cambio sostenido
  (0.445). Una tarde con aún más gente o lluvia podría acercarse a la alerta (ver
  [`docs/umbrales.md`](docs/umbrales.md)).
- **Tiempos de un solo equipo.** Son tiempos de CPU en una sola laptop con otras cargas; no son
  un benchmark.
- **Sin etiquetas de verdad.** La confianza del modelo es una señal indirecta de calidad, no de
  exactitud.
- **Diferencias entre vivo y replay.** El replay usa JPEG grabados, lo que mueve ligeramente las
  puntuaciones frente al stream original. Por eso referencia, C1 y C2 se procesan todos desde
  grabación.
- **Disponibilidad del stream.** La transmisión de YouTube puede cambiar de formato o dejar de estar
  disponible. Las grabaciones permiten repetir los escenarios sin ella.

## Extras

Inventario completo, con evidencia y control de doble conteo: [`docs/extras.md`](docs/extras.md).

| ID | Extra | Nivel solicitado |
|---|---|---|
| S1 | Escenario: interrupción y recuperación de la fuente ([`docs/escenario-s1.md`](docs/escenario-s1.md)) | 10 |
| M1 | Métrica: desplazamiento de la distribución de puntuaciones frente a la referencia (W1) | 8 |
| M2 | Métrica: latencia de punta a punta (frame → evento) | 5 |
| M3 | Métrica: tiempo de recuperación de la fuente y disponibilidad | 5 |
