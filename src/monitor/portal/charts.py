"""Gráficas por ventana del portal (SVG generado en el servidor, sin bibliotecas de JS).

Una gráfica por señal (small multiples): cada medida tiene su propia escala, nunca dos ejes en la
misma gráfica. Una sola serie por gráfica (el título la nombra), umbral como línea de referencia,
ventanas sin evidencia suficiente como hueco con etiqueta (nunca 0) y alertas marcadas con texto.
Cada columna de ventana tiene un área de hover más grande que el punto, con su tooltip.
"""

from __future__ import annotations

from html import escape

# Paleta de referencia validada (dataviz): serie 1 azul, estado crítico, tinta y superficies.
SERIES = "#2a78d6"
SURFACE = "#fcfcfb"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
INK_2 = "#52514e"
INK_3 = "#6f6e69"

W, H = 880, 200
PAD_L, PAD_R, PAD_T, PAD_B = 48, 92, 28, 30


def _scale(lo: float, hi: float, a: float, b: float):
    span = (hi - lo) or 1.0
    return lambda v: a + (v - lo) / span * (b - a)


def _nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    step = (hi - lo) / n
    return [lo + i * step for i in range(n + 1)]


def window_chart(title: str, points: list[dict], threshold: float | None, op: str | None,
                 fmt: str = "{:.3f}", note: str = "", has_rule: bool = True) -> str:
    """points: [{"idx", "value" (None = sin valor), "state", "alerts": [rule_id...]}]."""
    vals = [p["value"] for p in points if p["value"] is not None]
    head = (f'<figure class="chart"><figcaption><b>{escape(title)}</b>'
            f'{f" <span class=muted>{escape(note)}</span>" if note else ""}</figcaption>')
    if not points or not vals:
        return head + '<div class="empty">Sin ventanas con evidencia suficiente para esta señal.</div></figure>'

    lo_v = min(vals + ([threshold] if threshold is not None else []))
    hi_v = max(vals + ([threshold] if threshold is not None else []))
    pad = (hi_v - lo_v) * 0.15 or 0.01
    lo, hi = lo_v - pad, hi_v + pad
    n = len(points)
    x = _scale(0.5, n + 0.5, PAD_L, W - PAD_R)
    y = _scale(lo, hi, H - PAD_B, PAD_T)
    step = (W - PAD_L - PAD_R) / n
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="{escape(title)}" '
           f'style="background:{SURFACE};max-width:{W}px">']

    for t in _nice_ticks(lo, hi):                                    # rejilla recesiva + etiquetas del eje
        out.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(t):.1f}" y2="{y(t):.1f}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{PAD_L - 6}" y="{y(t) + 4:.1f}" text-anchor="end" font-size="11" fill="{INK_3}">{fmt.format(t)}</text>')
    out.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{H - PAD_B}" y2="{H - PAD_B}" stroke="{AXIS}" stroke-width="1"/>')
    for p in points:
        out.append(f'<text x="{x(p["idx"]):.1f}" y="{H - PAD_B + 16}" text-anchor="middle" font-size="11" fill="{INK_3}">{p["idx"]}</text>')
    out.append(f'<text x="{W - PAD_R + 8}" y="{H - PAD_B + 16}" font-size="11" fill="{INK_3}">ventana</text>')

    if threshold is not None:                                         # umbral: línea de referencia rotulada
        yt = y(threshold)
        out.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{yt:.1f}" y2="{yt:.1f}" stroke="{INK_2}" stroke-width="1"/>')
        out.append(f'<text x="{W - PAD_R + 8}" y="{yt + 4:.1f}" font-size="11" fill="{INK_2}">umbral {escape(op or "")} {fmt.format(threshold)}</text>')

    segs, cur = [], []                                                # la línea se corta donde no hay valor
    for p in points:
        if p["value"] is None:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(f'{x(p["idx"]):.1f},{y(p["value"]):.1f}')
    if cur:
        segs.append(cur)
    for s in segs:
        if len(s) > 1:
            out.append(f'<polyline points="{" ".join(s)}" fill="none" stroke="{SERIES}" stroke-width="2" '
                       f'stroke-linejoin="round" stroke-linecap="round"/>')

    last = max((p for p in points if p["value"] is not None), key=lambda p: p["idx"])
    for p in points:
        cx = x(p["idx"])
        if p["value"] is None:
            out.append(f'<text x="{cx:.1f}" y="{H - PAD_B - 6}" text-anchor="middle" font-size="10" fill="{INK_3}">'
                       f'{escape(p["state"])}</text>')
        else:
            cy = y(p["value"])
            over = threshold is not None and ((p["value"] > threshold) if op == ">" else (p["value"] < threshold))
            # punto con anillo de superficie; los que cruzan el umbral se rellenan de blanco (codificación
            # secundaria de forma, no solo color)
            out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="{SURFACE if over else SERIES}" '
                       f'stroke="{SERIES}" stroke-width="2"/>')
            if p is last:
                out.append(f'<text x="{cx + 8:.1f}" y="{cy - 6:.1f}" font-size="11" fill="{INK_2}">{fmt.format(p["value"])}</text>')
        if p.get("alerts"):
            out.append(f'<text x="{cx:.1f}" y="{PAD_T - 10}" text-anchor="middle" font-size="10" '
                       f'fill="{INK_2}">▲ alerta</text>')
        val = fmt.format(p["value"]) if p["value"] is not None else "sin valor"
        tip = f'Ventana {p["idx"]} · {val} · {p["state"]}' + (f' · alerta: {", ".join(p["alerts"])}' if p.get("alerts") else "")
        out.append(f'<rect class="hit" x="{cx - step / 2:.1f}" y="{PAD_T}" width="{step:.1f}" height="{H - PAD_T - PAD_B}" '
                   f'fill="transparent"><title>{escape(tip)}</title></rect>')
    out.append("</svg>")
    alerts = [f'ventana {p["idx"]}: {", ".join(p["alerts"])}' for p in points if p.get("alerts")]
    legend = ('<p class="muted chart-key">Punto relleno: dentro del umbral · punto hueco: cruza el umbral · '
              'hueco en la línea: ventana no evaluada (su estado está rotulado) · '
              + ('▲ alerta abierta en esa ventana' + (f' ({escape("; ".join(alerts))})' if alerts else " (ninguna)")
                 if has_rule else "sin regla de alerta propia: el umbral es de referencia"
                 + (f' · ▲ alerta de la fuente, de versión o de inferencia ({escape("; ".join(alerts))})' if alerts else ""))
              + '.</p>')
    return head + "".join(out) + legend + "</figure>"


HW, HH = 960, 300
HPAD_L, HPAD_R, HPAD_T, HPAD_B = 56, 110, 24, 36


def history_chart(d: dict) -> str:
    """F6: una señal por ventana a través de ejecuciones. Eje `ejecucion`: una columna por ejecución (número de la
    tabla), sus ventanas en orden dentro de la columna y unidas por una línea. Eje `hora`: hora del día de la escena
    (JST, 0-24 h), una línea por ejecución. Cada punto enlaza a su ejecución; hueco = cruza el umbral."""
    pts, fmt = d["puntos"], d["fmt"]
    title = f'{d["titulo"]} · {"por ejecución" if d["eje"] == "ejecucion" else "por hora de la escena (JST)"}'
    head = f'<figure class="chart"><figcaption><b>{escape(title)}</b></figcaption>'
    if not pts:
        return head + '<div class="empty">Ninguna ventana con valor para estos filtros.</div></figure>'
    th = d["umbral"]["valor"] if d["umbral"] else None
    vals = [p["value"] for p in pts] + ([th] if th is not None else [])
    lo_v, hi_v = min(vals), max(vals)
    pad = (hi_v - lo_v) * 0.12 or 0.01
    y = _scale(lo_v - pad, hi_v + pad, HH - HPAD_B, HPAD_T)
    runs = sorted({p["n"] for p in pts})
    if d["eje"] == "ejecucion":
        n_runs = max(r["n"] for r in d["ejecuciones"])
        slot = (HW - HPAD_L - HPAD_R) / n_runs
        max_idx = {}
        for p in pts:
            max_idx[p["n"]] = max(max_idx.get(p["n"], 1), p["idx"])
        x = lambda p: HPAD_L + (p["n"] - 1) * slot + slot * (0.15 + 0.7 * ((p["idx"] - 0.5) / max(max_idx[p["n"]], 1)))
    else:
        xs = _scale(0, 24, HPAD_L, HW - HPAD_R)
        x = lambda p: xs(p["hora_escena"])
    out = [f'<svg viewBox="0 0 {HW} {HH}" width="100%" role="img" aria-label="{escape(title)}" '
           f'style="background:{SURFACE};max-width:{HW}px">']
    for t in _nice_ticks(lo_v - pad, hi_v + pad):
        out.append(f'<line x1="{HPAD_L}" x2="{HW - HPAD_R}" y1="{y(t):.1f}" y2="{y(t):.1f}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{HPAD_L - 6}" y="{y(t) + 4:.1f}" text-anchor="end" font-size="11" fill="{INK_3}">{fmt.format(t)}</text>')
    base = HH - HPAD_B
    out.append(f'<line x1="{HPAD_L}" x2="{HW - HPAD_R}" y1="{base}" y2="{base}" stroke="{AXIS}" stroke-width="1"/>')
    if d["eje"] == "ejecucion":
        for r in d["ejecuciones"]:
            cx = HPAD_L + (r["n"] - 0.5) * slot
            if r["n"] > 1:
                sx = HPAD_L + (r["n"] - 1) * slot
                out.append(f'<line x1="{sx:.1f}" x2="{sx:.1f}" y1="{HPAD_T}" y2="{base}" stroke="{GRID}" stroke-width="1"/>')
            out.append(f'<text x="{cx:.1f}" y="{base + 16}" text-anchor="middle" font-size="11" fill="{INK_3}">{r["n"]}</text>')
        out.append(f'<text x="{HW - HPAD_R + 8}" y="{base + 16}" font-size="11" fill="{INK_3}">ejecución</text>')
    else:
        for h in range(0, 25, 3):
            out.append(f'<text x="{xs(h):.1f}" y="{base + 16}" text-anchor="middle" font-size="11" fill="{INK_3}">{h:02d}</text>')
        out.append(f'<text x="{HW - HPAD_R + 8}" y="{base + 16}" font-size="11" fill="{INK_3}">hora JST</text>')
    if th is not None:
        out.append(f'<line x1="{HPAD_L}" x2="{HW - HPAD_R}" y1="{y(th):.1f}" y2="{y(th):.1f}" stroke="{INK_2}" stroke-width="1"/>')
        out.append(f'<text x="{HW - HPAD_R + 8}" y="{y(th) + 4:.1f}" font-size="11" fill="{INK_2}">umbral '
                   f'{escape(d["umbral"]["op"])} {fmt.format(th)}</text>')
    for n in runs:                                                    # una línea fina por ejecución
        seq = sorted((p for p in pts if p["n"] == n), key=lambda p: p["idx"])
        segs, cur, prev = [], [], None
        for p in seq:
            if prev is not None and p["idx"] != prev + 1:              # ventana excluida: corta la línea
                segs.append(cur); cur = []
            cur.append(f'{x(p):.1f},{y(p["value"]):.1f}')
            prev = p["idx"]
        segs.append(cur)
        for s in segs:
            if len(s) > 1:
                out.append(f'<polyline points="{" ".join(s)}" fill="none" stroke="{SERIES}" stroke-opacity="0.55" '
                           f'stroke-width="1.5" stroke-linejoin="round"/>')
    for p in pts:
        cx, cy = x(p), y(p["value"])
        tip = (f'{p["n"]} · {p["label"] or p["run_id"]} · ventana {p["idx"]} · {fmt.format(p["value"])} · {p["state"]}'
               + (f' · escena {p["escena_jst"]} JST ({p["hora_tipo"]})' if p["escena_jst"] else ""))
        out.append(f'<a href="/runs/{escape(p["run_id"])}"><circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" '
                   f'fill="{SURFACE if p["cruza_umbral"] else SERIES}" stroke="{SERIES}" stroke-width="2"/>'
                   f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="9" fill="transparent"><title>{escape(tip)}</title></circle></a>')
    out.append("</svg>")
    key = ("Cada punto es una ventana y enlaza a su ejecución · punto hueco: cruza el umbral de la regla · "
           "la línea une ventanas consecutivas de una misma ejecución y se corta donde hay una ventana excluida."
           if th is not None else
           "Cada punto es una ventana y enlaza a su ejecución · sin regla de alerta para esta señal · "
           "la línea une ventanas consecutivas de una misma ejecución.")
    return head + "".join(out) + f'<p class="muted chart-key">{key}</p></figure>'
