# Umbrales, referencia, ventanas y reglas de alerta

Todos los valores viven en [`config/monitor.yaml`](../config/monitor.yaml); las estadísticas de las que
salen, en [`config/referencia.json`](../config/referencia.json), regenerables con:

```bash
uv run python -m monitor.reference datos/runs/<referencia-manana> datos/runs/<referencia-tarde> --out config/referencia.json
```

## 1. Referencia: operación normal de día

| Grabación | Hora en Tokio | Condición | Frames |
|---|---|---|---|
| `datos/grabaciones/20261107T220327Z-referencia` | dom 07:04–07:35 JST | luz de mañana, poca gente (~87 detecciones/frame) | 900 |
| `datos/grabaciones/20261108T053156Z-referencia-tarde` | dom 14:32–15:03 JST | tarde, multitud (~103 detecciones/frame) | 900 |

Ambas se grabaron en vivo (1 frame cada 2 s) y se **reprocesaron con la configuración final**
(`monitor.run --replay ... --no-pace`), de modo que referencia, C1 y C2 comparten modelo,
preprocesamiento, tamaño de entrada y `config_ref`. Las grabaciones guardan cada frame en JPEG; reprocesar
desde JPEG cambia ligeramente las puntuaciones respecto de la inferencia en vivo, por eso la referencia
oficial se calcula sobre el replay y no sobre la corrida en vivo.

**Por qué dos sesiones.** Con solo la sesión de la mañana, C1 (mediodía) quedaba ~8 desviaciones
estándar por encima en la proporción de baja confianza: la multitud cambia la distribución de
puntuaciones (más personas pequeñas u ocluidas). Una referencia de un solo horario no era
representativa; la combinación mañana + tarde cubre ambas densidades.

## 2. Tamaño de entrada del modelo

Comparación sobre 150 frames de la referencia de la mañana (YOLO11n, CPU):

| `imgsz` | ms/frame (p50) | detecciones ≥ 0.6 por frame | detecciones < 0.35 |
|---|---|---|---|
| 640 | 38 | 0.9 | 88.6 % |
| 960 | 71 | 3.0 | 79.5 % |
| **1280** | 160 | **7.9** | 72.4 % |

Las personas del cruce ocupan pocos píxeles; con 640 casi nada supera 0.6. Se eligió 1280: cabe
holgadamente en el intervalo de muestreo de 2 s.

## 3. Población elegible (`analysis_floor = 0.25`)

El detector conserva candidatas desde `conf_floor = 0.05` para que las zonas de duda y baja confianza
queden visibles. Pero el **74 % de las candidatas** de la referencia está por debajo de 0.25, y su
cantidad depende sobre todo de la multitud: la proporción de candidatas < 0.35 era 0.72 en la mañana y
0.89 en la tarde, sin ningún cambio de la cámara. Por eso las métricas de confianza usan como
población las **detecciones con s ≥ 0.25** ("elegibles"); las de menor puntuación se registran en cada
evento pero se excluyen de las métricas por ventana.

## 4. Umbrales de zona

Cuantiles de las puntuaciones elegibles de la referencia (44 808 detecciones):

| p25 | p50 | p60 | p75 | p80 | p90 |
|---|---|---|---|---|---|
| 0.332 | 0.450 | 0.507 | 0.606 | 0.641 | 0.730 |

- **`upper = 0.65`** (~p80): "aceptada por la política" = quinto superior de lo que se observa en
  operación normal. No significa que la detección sea correcta.
- **`lower = 0.50`** (~p60): se probaron 0.35, 0.40, 0.45 y 0.50 como límite de "baja confianza" y se
  midió cuánto se separaba la noche (C2) del día. Con 0.35 la proporción de baja confianza casi no
  cambiaba de noche (z = +1.2); con 0.50 es donde más se separa sin alertas falsas en C1.
- Zonas: `s ≥ 0.65` aceptada; `0.50 ≤ s < 0.65` duda; `s < 0.50` baja. La igualdad con el inferior es
  duda y con el superior es aceptada. Frames sin detecciones o con inferencia fallida no reciben zona
  (estados `no_detections`, `inference_error`, `invalid_input`).

## 5. Ventanas y mínimo de observaciones

- **Ventana = 60 frames muestreados consecutivos** (2 min a 0.5 FPS). Se usan frames y no tiempo para
  que el cálculo sea idéntico en vivo, en replay y en reprocesamiento.
- **Suficiente** si tiene ≥ 45 frames con inferencia válida **y** ≥ 300 detecciones elegibles (la
  ventana más pobre observada tuvo ~750). Si no, el estado es **`insuficiente`**: evidencia
  insuficiente, que no se reporta como normal ni como anómala y no rompe ni suma rachas.
- La última ventana de una ejecución, si tiene menos de 60 frames, es **`incompleta`** y no se evalúa.
- Frames consecutivos están correlacionados; por eso no se alerta por frame sino por ventana.

Señales por ventana:

- `low_ratio` = elegibles con s < 0.50 / elegibles (métrica base de G4: proporción de baja confianza).
- `mean_score` = media de las puntuaciones elegibles.

Estadísticas de la referencia (30 ventanas suficientes):

| Señal | media | sd | mín | p5 | p95 | máx |
|---|---|---|---|---|---|---|
| `low_ratio` | 0.598 | 0.063 | 0.459 | 0.502 | 0.704 | 0.720 |
| `mean_score` | 0.480 | 0.018 | 0.443 | 0.450 | 0.508 | 0.519 |

## 6. Reglas de alerta

| Regla | Condición | Persistencia | Severidad |
|---|---|---|---|
| `baja-confianza-v1` | `low_ratio > 0.724` (media + 2 sd) | 1 ventana suficiente | warning |
| `cambio-sostenido-v1` | `mean_score < 0.445` (media − 2 sd) | 3 ventanas suficientes seguidas (6 min) | critical |

- Cada regla abre **una alerta por episodio**. Mientras la condición siga, el episodio se extiende y se
  cuentan las repeticiones suprimidas; la primera ventana suficiente que ya no cumple la condición lo
  marca como resuelto.
- La alerta guarda regla, momento, severidad, motivo con los valores medidos, `config_ref`, ventana
  (inicio/fin), los `event_id` de las ventanas que la originaron, sus imágenes y una acción sugerida.
- `cambio-sostenido-v1` indica un *posible* cambio respecto de la referencia (p. ej. noche, lluvia,
  cámara movida). No confirma drift, pérdida de calidad ni necesidad de reentrenar.

## 7. Política de captura de imágenes

Se conserva la imagen de un frame (sin expiración) cuando:

1. su inferencia falla o no tiene detecciones;
2. **baja confianza**: tiene ≥ 5 detecciones elegibles y su media es < 0.38 (percentil 5 por frame en
   la referencia);
3. **muestra normal**: 1 de cada 15 frames.

Las alertas enlazan las imágenes capturadas dentro de sus ventanas.

## 8. Validación

Ver la sección de resultados en el README / índice de evidencias: la referencia no genera alertas, C1
(operación normal) no genera alertas y C2 (noche) dispara ambas reglas.

## 9. Limitaciones

- El margen entre C1 y el límite de `cambio-sostenido-v1` es pequeño (ventana más baja de C1 ≈ 0.45
  frente a 0.445). Los límites se derivan de la misma referencia y C1 aporta solo 7 ventanas para
  comprobarlos fuera de ella.
- Una sola cámara y dos sesiones de domingo: no cubren lluvia, días laborales ni eventos masivos.
- La confianza del detector no está calibrada ni mide acierto; las reglas describen cambios en la
  salida del modelo, no su calidad.
