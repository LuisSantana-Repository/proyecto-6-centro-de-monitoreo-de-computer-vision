# Métrica adicional M4 · Acuerdo entre modelos

**Pregunta que responde.** No hay etiquetas, así que no se puede medir la exactitud. Lo que sí se puede medir
es si el modelo desplegado (yolo11n) sigue viendo lo mismo que un modelo de referencia más grande de la misma
familia (yolo11s) **sobre los mismos frames**. Si el acuerdo baja, cambió algo que afecta más al modelo
pequeño: la imagen (foco, resolución, compresión) o el propio modelo.

**Por qué no es una métrica base ni M1–M3.** G4 y M1 miden solo las puntuaciones del modelo desplegado; G1 y
M2, tiempos; M3, la fuente. M4 **correlaciona dos fuentes de predicción por frame**: las detecciones que el
monitor guardó (`events.jsonl`) y las que la referencia produce sobre el mismo archivo de entrada
(`acuerdo.jsonl`, con el SHA-256 del frame). Los resultados muestran que no repite G4: de noche (C2) la
proporción de baja confianza sube de 0.64 a 0.74, pero el acuerdo no cambia (0.652 frente a 0.654).

## Definición

| | |
|---|---|
| **Población** | Detecciones con `s >= analysis_floor` (0.25) de cada modelo, la misma población de G4, en eventos con inferencia válida (`ok`, `no_detections`) cuyo frame se midió con la referencia. |
| **Emparejamiento** | Por frame, pares uno a uno de la **misma clase** con **IoU >= 0.5**, de mayor a menor IoU (greedy; simétrico y determinista). |
| **Acuerdo (Dice / F1)** | `2·Σm / (Σn_prod + Σn_ref)` sumando los frames de la ventana (m = pares). 1 = las dos listas de detecciones coinciden; 0 = ninguna coincide. |
| **Desglose** | `cobertura_referencia = Σm / Σn_ref` (lo que la referencia ve y producción también); `respaldo_produccion = Σm / Σn_prod`; `acuerdo_sin_clase` (lo mismo ignorando la clase: la diferencia es confusión de clase, p. ej. car/truck). |
| **Ventana** | Las mismas de 60 eventos. Suficiente con >= 45 frames medidos (`min_frames`) y >= 300 detecciones entre los dos modelos (`min_objects`). Si no, `null` (`sin_datos`), nunca 0. La última ventana incompleta no se evalúa. |
| **Exclusiones explícitas** | En `acuerdo.jsonl`, cada evento tiene una línea: `medido`, `excluido` (sin inferencia válida, con el motivo) o `sin_frame` (el archivo ya no existe). Un frame sin objetos en ninguno de los dos modelos es 0/0: no aporta y **no** cuenta como acuerdo perfecto. |
| **Referencia** | El acuerdo por ventana de las dos ejecuciones de referencia (las mismas de los umbrales y de M1): 30 ventanas, de 0.618 a 0.834. **Piso = mínimo** (0.6178); una ventana debajo queda `bajo_referencia`. [`config/referencia-acuerdo.json`](../config/referencia-acuerdo.json). |
| **Parámetros** | [`config/acuerdo.yaml`](../config/acuerdo.yaml) (`cfg-c3223347fd49`). No es una configuración de ejecución: `monitor.run` sigue con `monitor.yaml` (`cfg-0b5e664b118c`) sin cambios. |

## Cómo se mide y cómo se recalcula

- **Medir** (`monitor.agreement medir`) corre la referencia **aparte**, fuera del bucle de producción, sobre
  los frames de la grabación que la ejecución reprodujo (`manifest.source_meta.replay_dir` + `origin` de cada
  evento), con los mismos parámetros de inferencia de la ejecución (`imgsz` 1280, clases, pisos). Verifica el
  SHA-256 de los pesos y guarda una línea por evento en `datos/runs/<run>/acuerdo.jsonl`.
- **Recalcular** (`monitor.metrics` o `monitor.agreement reporte`) empareja las detecciones guardadas de los
  dos archivos **sin correr ningún modelo**. El resultado queda en
  `evidencias/metricas/<run>.json → metricas_adicionales.M4_acuerdo_modelos` y `ventanas[].M4_acuerdo_modelos`.
- **Portal**: `/graficas` agrega la serie M4 con el piso de la referencia cuando la ejecución tiene
  `acuerdo.jsonl` ([`47`](../evidencias/portal/47-graficas-m4-desenfoque.png)–[`49`](../evidencias/portal/49-graficas-m4-c2-noche.png)).

```bash
uv run python -m monitor.download_model --config config/acuerdo.yaml          # pesos de yolo11s (SHA-256)
uv run python -m monitor.agreement medir datos/runs/<run_id>                   # ~0.5 s por frame en CPU
uv run python -m monitor.agreement referencia datos/runs/20261108T155809Z-referencia-manana datos/runs/20261108T160249Z-referencia-tarde
uv run python -m monitor.agreement reporte datos/runs/<run_id>                 # por ventana, sin modelo
uv run python -m monitor.metrics datos/runs/<run_id> --out-dir evidencias/metricas
```

## Verificación

Tres comprobaciones, de la más básica a la más útil:

| Comprobación | Qué se espera | Resultado |
|---|---|---|
| **Control: producción contra sí misma** (`--modelo yolo11n`, C1) | La medición aparte reproduce exactamente lo que vio producción: acuerdo 1.0 en todas las ventanas. Confirma el emparejamiento y que la inferencia es determinista, el supuesto en el que descansa todo lo demás. | **1.0 en las 7 ventanas** (`acuerdo-control.jsonl`) |
| **Cambio de modelo** (S2: producción **es** yolo11s en las muestras 76–180) | Donde producción y referencia son el mismo modelo, el acuerdo sube a 1.0. | ventanas 0.682 → 0.909 (mitad) → **1.000** → 0.623 |
| **Caso controlado de imagen degradada** | Mismos frames que C1; las muestras 121–240 con desenfoque gaussiano σ = 3. Las ventanas con frames idénticos dan **diferencia 0 exacta**; las desenfocadas bajan. | ver abajo |

### Caso controlado: desenfoque sobre la grabación de C1

`monitor.agreement degradar` copia las primeras 240 muestras de la grabación de C1 y desenfoca de la 121 en
adelante. Queda registrado en
[`evidencias/entradas/m4-desenfoque/degradacion.json`](../evidencias/entradas/m4-desenfoque/degradacion.json)
(SHA-256 de cada frame original y degradado), con un ejemplo
[original](../evidencias/entradas/m4-desenfoque/000150-original.jpg) y
[desenfocado](../evidencias/entradas/m4-desenfoque/000150-desenfocado.jpg). La ejecución
`20261109T054751Z-m4-desenfoque` lo reprocesa con `monitor.yaml` sin cambios.

`monitor.agreement comparar` empareja las ventanas por número de muestra y cuenta los frames idénticos por
SHA-256:

| Ventana | Muestras | Frames idénticos a C1 | C1 | Desenfoque | Diferencia | ¿Bajo el piso (0.618)? |
|---|---|---|---|---|---|---|
| 1 | 1–60 | 60/60 | 0.6819 | 0.6819 | **0.0** | no |
| 2 | 61–120 | 60/60 | 0.6330 | 0.6330 | **0.0** | no |
| 3 | 121–180 | 0/60 | 0.6580 | 0.6023 | **−0.056** | sí |
| 4 | 181–240 | 0/60 | 0.6234 | 0.4980 | **−0.125** | sí |

La cobertura de la referencia cae más que el respaldo: con el desenfoque, yolo11n pierde las detecciones que
yolo11s todavía encuentra. En esta ejecución el desenfoque también mueve G4 (la ventana 4 abre
`baja-confianza-v1`), la media y W1 (captura [`47`](../evidencias/portal/47-graficas-m4-desenfoque.png)): una
degradación fuerte la ven todas las señales. Lo que solo ve M4 son los otros dos casos: el cambio de modelo
(S2) y la noche, donde la confianza baja y el acuerdo no.

```bash
uv run python -m monitor.agreement degradar datos/grabaciones/20261108T050904Z-c1-normal datos/grabaciones/m4-c1-desenfoque --desde 121 --frames 240 --sigma 3
uv run python -m monitor.run --replay datos/grabaciones/m4-c1-desenfoque --frames 240 --no-pace --label m4-desenfoque
uv run python -m monitor.agreement medir datos/runs/<run_id>
uv run python -m monitor.agreement comparar datos/runs/20261108T161753Z-c1-normal datos/runs/<run_id>
```

## Resultados

| Ejecución | Acuerdo | Cobertura ref. | Respaldo prod. | Sin clase | Ventanas bajo el piso | Por ventana |
|---|---|---|---|---|---|---|
| Referencia mañana | 0.818 | 0.844 | 0.794 | 0.825 | 0/15 | 0.786–0.834 |
| Referencia tarde | 0.650 | 0.582 | 0.735 | 0.677 | 0/15 | 0.618–0.701 |
| **C1 · normal** | **0.654** | 0.584 | 0.742 | 0.675 | **1/7** (ventana 7: 0.596) | 0.596–0.706 |
| **C2 · baja confianza (noche)** | **0.652** | 0.563 | 0.775 | 0.669 | **1/7** (ventana 2: 0.614) | 0.614–0.679 |
| S2 · cambio de versión | 0.813 | 0.759 | 0.875 | 0.826 | 0/4 | 0.682 / 0.909 / **1.0** / 0.623 |
| **M4 · desenfoque** | 0.629 | 0.532 | 0.768 | 0.651 | **2/4** (las dos desenfocadas) | 0.682 / 0.633 / 0.602 / 0.498 |

Las demás ejecuciones no se midieron (S1-R, S3 y S4 repiten la grabación de C1; S1-V, S1-W y F4 son en vivo,
sin todos los frames guardados): su M4 es `sin_datos`, no 0.

**Lectura:**
- El acuerdo es de 0.65 en las condiciones de C1: yolo11n encuentra el 58 % de lo que yolo11s ve con s ≥ 0.25,
  y la referencia confirma el 74 % de lo que ve yolo11n. La confusión de clase pesa poco (≈ 0.02 entre
  `acuerdo` y `acuerdo_sin_clase`); casi todo el desacuerdo es que un modelo detecta un objeto y el otro no.
- **La escena pesa más que el desenfoque σ = 3.** La referencia de la mañana (≈ 0.82) y la de la tarde
  (≈ 0.65) difieren más que C1 y su versión desenfocada. No se aisló la causa; en la tarde, yolo11s encuentra
  más detecciones que yolo11n (22.5 frente a 17.9 por frame) y en la mañana, al revés (30.2 frente a 32.1).

## Supuestos y límites

- **Supuesto: la referencia no es la verdad.** Un acuerdo bajo dice que los modelos difieren, no cuál acierta.
  yolo11s es más grande y en general más exacto, pero no se validó contra etiquetas en esta cámara.
- **Supuesto: inferencia determinista.** Lo confirma el control (1.0 exacto) y la ventana 3 de S2. Con otro
  hardware o versión de torch podría haber diferencias mínimas; el control lo detectaría.
- **El piso absoluto no sirve como regla de alerta.** Con el mínimo de la referencia, 2 de 14 ventanas normales
  (C1 y C2) quedan debajo, y la ventana 3 desenfocada (0.602) queda casi igual que la ventana 7 normal de C1
  (0.596). Por eso M4 se reporta como métrica, sin regla. La verificación robusta del cambio es la
  **comparación pareada** sobre los mismos frames, posible solo en un caso controlado. Una regla en operación
  necesitaría una referencia por franja horaria o por densidad de la escena.
- **Costo.** yolo11s tarda 380–500 ms por frame (p50) a 1280 en esta CPU. En el bucle de producción, sumado a
  yolo11n (~300 ms), cabría en el intervalo de 2 s, pero se decidió medir aparte para no tocar la latencia de
  producción (M2). La contrapartida es que M4 no está disponible en vivo, solo después.
- **Requiere los frames.** Se mide sobre reproducciones de grabaciones (`datos/grabaciones/`, no versionadas
  por tamaño). Las ejecuciones en vivo guardan solo las capturas, así que M4 no se puede medir sobre ellas
  completas. `acuerdo.jsonl` guarda el SHA-256 de cada frame, para verificar que una nueva medición usa los
  mismos bytes.

## Evidencia

| Qué | Dónde |
|---|---|
| Código | [`src/monitor/agreement.py`](../src/monitor/agreement.py); integración en [`metrics.py`](../src/monitor/metrics.py) y en el portal (`/graficas`) |
| Registros de la referencia | `acuerdo.jsonl` en las ejecuciones de referencia, C1, C2, S2 y `20261109T054751Z-m4-desenfoque`; control en `datos/runs/20261108T161753Z-c1-normal/acuerdo-control.jsonl` |
| Referencia | [`config/referencia-acuerdo.json`](../config/referencia-acuerdo.json) |
| Caso controlado | [`evidencias/entradas/m4-desenfoque/`](../evidencias/entradas/m4-desenfoque/); ejecución `20261109T054751Z-m4-desenfoque` |
| Métricas | `metricas_adicionales.M4_acuerdo_modelos` y `ventanas[].M4_acuerdo_modelos` en [`evidencias/metricas/`](../evidencias/metricas/) |
| Portal | [`47`](../evidencias/portal/47-graficas-m4-desenfoque.png) (desenfoque), [`48`](../evidencias/portal/48-graficas-m4-cambio-version.png) (S2), [`49`](../evidencias/portal/49-graficas-m4-c2-noche.png) (C2); [`portal/api/graficas-m4-desenfoque.json`](../evidencias/portal/api/graficas-m4-desenfoque.json) |
| Pruebas | [`tests/test_agreement.py`](../tests/test_agreement.py) (10: emparejamiento, población y exclusiones, ventanas y piso, configuración, degradación determinista, métricas, portal, control contra sí mismo, caso controlado pareado) |
