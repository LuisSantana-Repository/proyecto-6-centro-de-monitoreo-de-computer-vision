"""Funcionalidad adicional F2 · revisión humana de alertas y de capturas.

Dos flujos con estado propio, separados del estado que calcula cada regla:

Alertas (estado de revisión, independiente de open/resolved de la regla):
    pendiente --reconocer--> reconocida --cerrar(veredicto)--> revisada
    `comentar` se permite en cualquier estado y no lo cambia.
    `cerrar` exige que la regla ya haya resuelto la alerta: una persona no puede dar por terminada una
    condición que sigue activa (409). Las alertas nunca se modifican: la revisión es un registro aparte.

Capturas (imágenes de eventos):
    `revisar` con una etiqueta de un conjunto fijo y una nota opcional. Se puede volver a revisar: queda
    el historial y la etiqueta vigente es la última. Un evento sin imagen no se revisa (409).

Persistencia y trazabilidad: cada acción es un registro inmutable en
`datos/runs/<run_id>/revisiones.jsonl` (registro primario, versionado con la ejecución) y se indexa en
SQLite (`reviews`). El estado vigente se obtiene plegando el historial, así que se reconstruye igual
desde el JSONL (`monitor.ingest`). `request_id` hace idempotente el envío: reenviar el mismo formulario
no duplica la acción.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from monitor.source import utc_now_iso

ALERT_ACTIONS = ("reconocer", "comentar", "cerrar")
VERDICTS = {
    "confirmada": "la condición ocurrió y la alerta era pertinente",
    "esperada": "la condición ocurrió, pero era esperada (p. ej. un escenario provocado o una condición conocida)",
    "falsa_alarma": "la alerta no corresponde a un problema real",
}
CAPTURE_LABELS = {
    "sin_problema": "la imagen no muestra un problema de calidad",
    "iluminacion": "iluminación (noche, contraluz, reflejos)",
    "clima": "clima (lluvia, niebla, paraguas)",
    "oclusion": "oclusión o multitud",
    "camara": "cámara movida, desenfocada o con artefactos",
    "otro": "otro motivo (explicar en la nota)",
}
MAX_OPERATOR, MAX_NOTE = 60, 1000


class ReviewError(Exception):
    """Acción rechazada. `status` es el código HTTP que corresponde (404, 409, 422)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def alert_state(history: list[dict]) -> dict:
    """Estado vigente de revisión de una alerta a partir de su historial (ordenado por `at`)."""
    st = {"state": "pendiente", "acknowledged_by": None, "acknowledged_at": None, "verdict": None,
          "closed_by": None, "closed_at": None, "n_actions": len(history)}
    for r in history:
        if r["action"] == "reconocer":
            st.update(state="reconocida", acknowledged_by=r["operator"], acknowledged_at=r["at"])
        elif r["action"] == "cerrar":
            st.update(state="revisada", verdict=r["verdict"], closed_by=r["operator"], closed_at=r["at"])
    return st


def capture_state(history: list[dict]) -> dict:
    last = history[-1] if history else None
    return {"state": "revisada" if last else "pendiente", "label": last["label"] if last else None,
            "reviewed_by": last["operator"] if last else None, "reviewed_at": last["at"] if last else None,
            "n_reviews": len(history)}


def _common(form: dict) -> tuple[str, str]:
    operator = (form.get("operator") or "").strip()
    note = (form.get("note") or "").strip()
    if not operator:
        raise ReviewError(422, "Falta el nombre de quien revisa (operador).")
    if len(operator) > MAX_OPERATOR:
        raise ReviewError(422, f"El operador admite hasta {MAX_OPERATOR} caracteres.")
    if len(note) > MAX_NOTE:
        raise ReviewError(422, f"La nota admite hasta {MAX_NOTE} caracteres.")
    return operator, note


def check_alert_action(alert: dict, history: list[dict], form: dict) -> dict:
    """Valida una acción sobre una alerta y devuelve el registro a guardar (sin ids ni hora)."""
    action = form.get("action")
    if action not in ALERT_ACTIONS:
        raise ReviewError(422, f"Acción desconocida: {action!r}. Válidas: {', '.join(ALERT_ACTIONS)}.")
    operator, note = _common(form)
    before = alert_state(history)["state"]
    verdict = None
    if action == "reconocer":
        if before != "pendiente":
            raise ReviewError(409, f"La alerta ya está {before}: solo se reconoce una vez.")
        after = "reconocida"
    elif action == "comentar":
        if not note:
            raise ReviewError(422, "Un comentario necesita texto.")
        after = before
    else:  # cerrar
        verdict = form.get("verdict")
        if verdict not in VERDICTS:
            raise ReviewError(422, f"Veredicto inválido: {verdict!r}. Válidos: {', '.join(VERDICTS)}.")
        if not note:
            raise ReviewError(422, "Cerrar la revisión requiere una nota que justifique el veredicto.")
        if before == "pendiente":
            raise ReviewError(409, "Primero hay que reconocer la alerta.")
        if before == "revisada":
            raise ReviewError(409, "La revisión de esta alerta ya está cerrada.")
        if alert["status"] != "resolved":
            raise ReviewError(409, "La regla todavía no resolvió la alerta: la revisión no puede cerrarse "
                                   "mientras la condición sigue activa.")
        after = "revisada"
    return {"target_type": "alert", "target_id": alert["alert_id"], "run_id": alert["run_id"],
            "action": action, "operator": operator, "note": note, "verdict": verdict, "label": None,
            "state_before": before, "state_after": after, "alert_status": alert["status"],
            "rule_id": alert["rule_id"], "config_ref": alert["config_ref"], "image_ref": None}


def check_capture_review(event: dict, history: list[dict], form: dict) -> dict:
    if (form.get("action") or "revisar") != "revisar":
        raise ReviewError(422, "La única acción sobre una captura es 'revisar'.")
    operator, note = _common(form)
    if not event.get("image_ref"):
        raise ReviewError(409, "Este evento no tiene imagen capturada: no hay nada que revisar.")
    label = form.get("label")
    if label not in CAPTURE_LABELS:
        raise ReviewError(422, f"Etiqueta inválida: {label!r}. Válidas: {', '.join(CAPTURE_LABELS)}.")
    if label == "otro" and not note:
        raise ReviewError(422, "La etiqueta 'otro' requiere una nota.")
    before = capture_state(history)
    return {"target_type": "event", "target_id": event["event_id"], "run_id": event["run_id"],
            "action": "revisar", "operator": operator, "note": note, "verdict": None, "label": label,
            "state_before": before["label"] or "pendiente", "state_after": label, "alert_status": None,
            "rule_id": None, "config_ref": event["config_ref"], "image_ref": event["image_ref"]}


class ReviewLog:
    """Escribe una acción validada: primero el JSONL de la ejecución (primario), luego SQLite (índice).

    El candado serializa validar + escribir, para que dos envíos simultáneos no pasen la misma
    transición (p. ej. dos "reconocer" a la vez)."""

    _lock = threading.Lock()

    def __init__(self, runs_dir: Path, store_factory):
        self.runs_dir = Path(runs_dir)
        self.store_factory = store_factory

    def submit(self, request_id: str, load, check) -> tuple[dict, bool]:
        """load() -> (objetivo, historial); check(objetivo, historial) -> registro.
        Devuelve (registro, es_nuevo). Un request_id ya registrado devuelve el registro original."""
        request_id = (request_id or "").strip()
        if not request_id or len(request_id) > 80:
            raise ReviewError(422, "Falta request_id (identificador del envío, para no duplicarlo).")
        with self._lock:
            store = self.store_factory()
            try:
                prev = store.review_by_request(request_id)
                if prev is not None:
                    return prev, False
                target, history = load(store)
                rec = check(target, history)
                run_dir = self.runs_dir / rec["run_id"]
                if not run_dir.is_dir():
                    raise ReviewError(409, f"No existe la carpeta de la ejecución {rec['run_id']}: la revisión "
                                           "no tendría registro primario.")
                rec = {"review_id": "rev-" + hashlib.sha1(request_id.encode()).hexdigest()[:12],
                       "request_id": request_id, "at": utc_now_iso(), **rec}
                with open(run_dir / "revisiones.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                store.insert_review(rec)
                return rec, True
            finally:
                store.close()
