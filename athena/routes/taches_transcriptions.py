"""Tâche planifiée — capture ciblée des transcriptions et résumés Copilot Teams (Lot 3).

Blueprint MACHINE : sans @login_required et exempté de CSRF (main.py),
accessible uniquement par Cloud Scheduler / App Engine Cron via l'en-tête
``X-Appengine-Cron: true`` (retiré de tout trafic externe).
"""

from __future__ import annotations

from flask import Blueprint, abort, jsonify, request

from config import Config
from services import transcription_reunion
from utils.logging_setup import log_bookings_event

taches_transcriptions_bp = Blueprint(
    "taches_transcriptions", __name__, url_prefix="/taches/transcriptions"
)


@taches_transcriptions_bp.route("/sync", methods=["GET"])
def sync():
    if request.headers.get("X-Appengine-Cron") != "true":
        abort(403)
    if not (Config.TEAMS_REUNIONS_ACTIF and Config.teams_configured()):
        return jsonify({"actif": False}), 200
    try:
        res = transcription_reunion.verifier_reunions_terminees()
    except transcription_reunion.LectureImpossible:
        log_bookings_event(
            "teams_transcription_erreur_graph",
            "failure",
            reason="lecture_firestore",
        )
        return jsonify({"actif": True, "erreur": "lecture_firestore"}), 200
    return jsonify({"actif": True, **res}), 200
