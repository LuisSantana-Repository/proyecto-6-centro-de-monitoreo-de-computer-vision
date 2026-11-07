"""Política de confianza: dos umbrales, tres zonas.

- s >= upper          -> "accepted" (aceptada por la política; no implica acierto)
- lower <= s < upper  -> "doubt"
- s < lower           -> "low"
Sin puntuación (frame sin detecciones o inferencia fallida) no se asigna zona.
"""

from __future__ import annotations

ACCEPTED, DOUBT, LOW = "accepted", "doubt", "low"


def zone(score: float, lower: float, upper: float) -> str:
    if score >= upper:
        return ACCEPTED
    if score >= lower:
        return DOUBT
    return LOW


def zone_counts(zones: list[str]) -> dict[str, int]:
    return {z: zones.count(z) for z in (ACCEPTED, DOUBT, LOW)}
