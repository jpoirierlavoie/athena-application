"""Portal availability sync cron handler (Lot 2).

MACHINE blueprint — no @login_required, CSRF-exempt (main.py), reachable only
by App Engine cron (``X-Appengine-Cron: true`` stripped from external traffic).
"""

from __future__ import annotations

from flask import Blueprint, abort, jsonify, request

from config import Config
from services import disponibilites
from utils.graph import GraphError, GraphNotConfigured
from utils.logging_setup import log_bookings_event

taches_disponibilites_bp = Blueprint(
    "taches_disponibilites", __name__, url_prefix="/taches/disponibilites"
)


@taches_disponibilites_bp.route("/sync", methods=["GET"])
def sync():
    if request.headers.get("X-Appengine-Cron") != "true":
        abort(403)
    if not Config.PORTAIL_RDV_ACTIF:
        return jsonify({"actif": False}), 200
    try:
        res = disponibilites.synchroniser_disponibilites()
    except (GraphError, GraphNotConfigured, Exception) as exc:
        log_bookings_event(
            "disponibilites_sync_echec",
            "failure",
            reason=type(exc).__name__,
        )
        return jsonify({"actif": True, "erreur": type(exc).__name__}), 200
    return jsonify({"actif": True, **res}), 200
