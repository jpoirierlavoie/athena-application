"""Asynchronous OneDrive mirror worker and catch-up cron (``Athena -> OneDrive``).

MACHINE blueprint — no ``@login_required``, CSRF-exempt in ``main.py``.

* ``POST /taches/onedrive/event`` — invoked exclusively by Cloud Tasks
  (``X-AppEngine-QueueName == Config.MIROIR_ONEDRIVE_QUEUE``). Processes
  one post-commit mirror event idempotently; returns 500 on transient
  failures so Cloud Tasks retries automatically, or 200 on poison/refused
  or disabled payloads.
* ``GET /taches/onedrive/sync`` — invoked exclusively by App Engine Cron
  (``X-Appengine-Cron == "true"``; schedule in ``cron.yaml``). Runs a
  bounded catch-up sweep aligning OneDrive with Athena state.
"""

from __future__ import annotations

from flask import Blueprint, abort, jsonify, request

from config import Config
from services import miroir_onedrive
from utils.logging_setup import log_onedrive_mirror_event, log_unexpected

taches_onedrive_bp = Blueprint(
    "taches_onedrive", __name__, url_prefix="/taches/onedrive"
)


@taches_onedrive_bp.post("/event")
def event():
    """Handle one Cloud Tasks OneDrive mirror dispatch."""
    if request.headers.get("X-AppEngine-QueueName") != Config.MIROIR_ONEDRIVE_QUEUE:
        abort(403)

    if not Config.MIROIR_ONEDRIVE_ACTIF:
        return jsonify({"actif": False, "outcome": "disabled"}), 200
    if not Config.courrier_configured():
        return jsonify({"actif": True, "configured": False, "outcome": "skipped"}), 200

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        log_onedrive_mirror_event(
            "miroir_onedrive_dispatch_error",
            outcome="refused",
            reason="invalid_json",
        )
        return jsonify({"ok": False, "outcome": "refused"}), 200

    try:
        result = miroir_onedrive.handle_event(payload)
    except Exception as exc:
        log_onedrive_mirror_event(
            "miroir_onedrive_dispatch_error",
            outcome="failure",
            action=str(payload.get("action") or ""),
            error_type=type(exc).__name__,
        )
        log_unexpected("taches_onedrive event failed", error_type=type(exc).__name__)
        return jsonify({"ok": False, "outcome": "retry"}), 500

    return jsonify(result), 200


@taches_onedrive_bp.get("/sync")
def sync():
    """Periodic catch-up cron sweep for OneDrive mirroring."""
    if request.headers.get("X-Appengine-Cron") != "true":
        abort(403)

    if not Config.MIROIR_ONEDRIVE_ACTIF:
        return jsonify({"actif": False, "synced": 0, "deleted": 0, "errors": 0}), 200
    if not Config.courrier_configured():
        return jsonify(
            {"actif": True, "configured": False, "synced": 0, "deleted": 0, "errors": 0}
        ), 200

    try:
        stats = miroir_onedrive.executer_rattrapage_cron()
    except Exception as exc:
        log_onedrive_mirror_event(
            "miroir_onedrive_sweep",
            outcome="failure",
            error_type=type(exc).__name__,
        )
        log_unexpected("taches_onedrive cron sweep failed", error_type=type(exc).__name__)
        return jsonify({"actif": True, "errors": 1, "outcome": "error"}), 200

    return jsonify(stats), 200
