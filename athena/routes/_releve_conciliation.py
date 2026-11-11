"""A reconciliation's bank statement — the web side, both registers (2026-11-10).

Each register's blueprint (``routes/trust.py``, ``routes/admin_ledger.py``)
serves the same four URLs under its own prefix, each a one-line call here:

* ``POST …/conciliations/<id>/releve/televersement`` — open the upload
  (JSON ``{name, size}``): 200 ``{url, objet}``, else the refusal;
* ``POST …/conciliations/<id>/releve`` — attach the uploaded object
  (JSON ``{objet}``): 200 ``{ok, suivant}``, else the refusal;
* ``POST …/conciliations/<id>/releve/retirer`` — a brouillon's « Retirer »
  (a form): back to the worksheet, its outcome a closed ``?releve=`` code;
* ``GET …/conciliations/<id>/releve`` — « Consulter »: a 302 to a
  15-minute signed URL, minted on the click, never embedded in a page.

Every rule is ``models/releve_conciliation``'s; this module only maps its
closed outcome codes to answers. The JSON endpoints carry the CSRF token in
the ``X-CSRFToken`` header (the blueprints are never exempted), as the
proofs of identity do (``routes/parties``). Not a blueprint: no route of
its own, so no prefix or exemption to keep in step.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from flask import Response, jsonify, redirect, request, url_for

from config import Config
from models import concurrency
from models import releve_conciliation as rc
from routes._helpers import redirect_after_post
from tz import to_mtl
from utils import storage_identity
from utils.format_fr import format_date_fr
from utils.logging_setup import log_unexpected, sanitize_log_value

#: register → the blueprint whose endpoints serve it.
BLUEPRINTS = {rc.TRUST: "trust", rc.ADMIN: "admin_ledger"}
_JSON_STATUS = {
    rc.CODE_FORME: 400,
    rc.CODE_REFUS: 422,
    rc.CODE_INDISPONIBLE: 503,
    rc.CODE_INCERTAIN: 503,
}
_RETRY_SECONDS = 2
_FALLBACK = "Opération refusée."

#: The closed ``?releve=`` codes the worksheet shows — any other shows
#: nothing (a hand-made link never reaches the page as text).
NOTICES = {
    "retire": "Le relevé a été retiré.",
    # A neutral notice, never an alert: a double tap on « Retirer » lands
    # here after the first one worked (the review of 2026-11-10).
    "aucun": rc.NOTHING_TO_REMOVE,
}
ERRORS = {
    "introuvable": rc.NOT_FOUND,
    "fige": rc.FROZEN,
    "indisponible": concurrency.READ_UNAVAILABLE_ERROR,
    "retrait": rc.REMOVE_FAILED,
    "incertain": rc.REMOVE_UNCERTAIN,
    "chemin": rc.PATH_REFUSED,
    "fichier": rc.OBJECT_MISSING,
    "consultation": rc.CONSULT_FAILED,
}
_REMOVAL_ERRORS = {
    rc.NOT_FOUND: "introuvable",
    rc.FROZEN: "fige",
}
_TYPE_LABELS = {mime: ext.lstrip(".").upper()
                for ext, mime in reversed(list(rc.MIME_TYPES.items()))}


def _endpoint(register: str, name: str) -> str:
    return f"{BLUEPRINTS[register]}.{name}"


def _worksheet(register: str, rec_id: str, **query) -> str:
    return url_for(_endpoint(register, "reconciliation_worksheet"),
                   rec_id=rec_id, _anchor="releve", **query)


def _size_label(size: object) -> str:
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return ""
    if size < 1024:
        return f"{size} o"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} Ko".replace(".", ",")
    return f"{size / (1024 * 1024):.1f} Mo".replace(".", ",")


def _attached_day(value: object) -> str:
    """« 10 novembre 2026 » — the attachment's Montréal day (Rule 5: an
    instant); ``""`` when the stored value is not one."""
    if not isinstance(value, datetime):
        return ""
    return format_date_fr(to_mtl(value).date())


def worksheet_view(register: str, rec: dict) -> dict:
    """What ``components/_releve_conciliation.html`` renders for *rec*: its
    state (from the model's predicates), the closed notice or error the
    query string names, and the URLs — the script's in ``config``."""
    rec_id = rec.get("id") or ""
    joint = rc.has_statement(rec)
    return {
        "joint": joint,
        "fige": joint and rec.get("status") == rc.STATUS_COMPLETED,
        "peut_joindre": rc.accepts_statement(rec),
        "peut_retirer": rc.removable(rec),
        "manquant": rc.statement_missing(rec),
        "brouillon": rec.get("status") == rc.STATUS_DRAFT,
        "type_label": _TYPE_LABELS.get(rec.get("statement_file_type"), ""),
        "taille": _size_label(rec.get("statement_file_size")),
        "joint_le": _attached_day(rec.get("statement_attached_at")),
        "consulter": url_for(_endpoint(register, "reconciliation_statement_consult"),
                             rec_id=rec_id),
        "retirer": url_for(_endpoint(register, "reconciliation_statement_remove"),
                           rec_id=rec_id),
        "accept": rc.ACCEPT,
        "consignes": rc.UPLOAD_HINT,
        "requis": rc.REQUIRED_TO_COMPLETE,
        "message": NOTICES.get(request.args.get("releve", ""), ""),
        "erreur": ERRORS.get(request.args.get("releve_erreur", ""), ""),
        "config": {
            "televersement": url_for(
                _endpoint(register, "reconciliation_statement_session"),
                rec_id=rec_id),
            "finaliser": url_for(
                _endpoint(register, "reconciliation_statement_finalize"),
                rec_id=rec_id),
            "suivant": _worksheet(register, rec_id),
            "incertain": rc.FILING_UNCERTAIN,
            # A completed reconciliation's catch-up FREEZES what it
            # attaches: the script asks « Joindre et figer » first.
            "figer": rc.statement_missing(rec),
        },
    }


def _payload() -> dict:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def _json_refusal(outcome: rc.Outcome) -> Response:
    """A JSON endpoint's answer to a non-success code — the model's French
    sentence, the code's status."""
    body = {"erreur": " ".join(outcome.errors) or _FALLBACK}
    if outcome.code == rc.CODE_INCERTAIN:
        # Both objects were KEPT: one more finalization — idempotent through
        # the reserved id and the create-only copy — settles it only when
        # the store answers.
        body["incertain"] = True
    resp = jsonify(body)
    resp.status_code = _JSON_STATUS.get(outcome.code, 500)
    if outcome.code == rc.CODE_INCERTAIN:
        resp.headers["Retry-After"] = str(_RETRY_SECONDS)
    return resp


def _request_uid() -> tuple[Optional[str], Optional[Response]]:
    try:
        return storage_identity.request_uid(), None
    except storage_identity.StorageIdentityUnavailable as exc:
        return None, (jsonify({"erreur": storage_identity.public_message(exc)}), 503)


def session(register: str, rec_id: str) -> Response:
    """Open the browser's DIRECT upload of the statement. 200
    ``{url, objet}`` — ``url`` a write capability, handed to the uploading
    script only, never logged —; 422 a refusal; 503 a store failure or a uid
    that cannot name a Storage prefix."""
    payload = _payload()
    user_id, refused = _request_uid()
    if refused is not None:
        return refused
    # The session's CORS origin is the PAGE's; in production TLS ends
    # upstream and request.scheme reads « http » — https is FORCED there
    # (the rule of routes/documents and routes/parties).
    scheme = "https" if Config.ENV == "production" else (request.scheme or "http")
    name = payload.get("name")
    outcome = rc.open_statement_upload(
        register, rec_id, name if isinstance(name, str) else "",
        payload.get("size"), user_id, origin=f"{scheme}://{request.host}",
    )
    if outcome.code == rc.CODE_OK:
        return jsonify({"url": outcome.doc["url"], "objet": outcome.doc["objet"]})
    return _json_refusal(outcome)


def finalize(register: str, rec_id: str) -> Response:
    """Attach the uploaded staging object. 200 ``{ok, suivant}`` when the
    statement is attached — by this call or already (a replay); 400 a
    staging name that is not the module's shape; 422 a refusal; 503 a
    store failure, or ``incertain`` (``"incertain": true`` and
    ``Retry-After``: the script re-posts once)."""
    payload = _payload()
    user_id, refused = _request_uid()
    if refused is not None:
        return refused
    objet = payload.get("objet")
    outcome = rc.file_staged_statement(
        register, rec_id, objet if isinstance(objet, str) else "", user_id)
    if outcome.code not in (rc.CODE_JOINT, rc.CODE_REJOUE):
        return _json_refusal(outcome)
    return jsonify({"ok": True, "suivant": _worksheet(register, rec_id)})


def remove(register: str, rec_id: str) -> Response:
    """« Retirer » — a brouillon's statement; back to the worksheet, the
    outcome a closed code (Rule 8: ``redirect_after_post``)."""
    outcome = rc.remove_statement(register, rec_id)
    if outcome.code == rc.CODE_RETIRE:
        return redirect_after_post(_worksheet(register, rec_id, releve="retire"))
    if outcome.errors == [rc.NOTHING_TO_REMOVE]:
        return redirect_after_post(_worksheet(register, rec_id, releve="aucun"))
    if outcome.code == rc.CODE_INCERTAIN:
        code = "incertain"
    elif outcome.code == rc.CODE_INDISPONIBLE:
        code = "retrait"
    else:
        code = next((_REMOVAL_ERRORS[e] for e in outcome.errors
                     if e in _REMOVAL_ERRORS), "retrait")
    return redirect_after_post(_worksheet(register, rec_id, releve_erreur=code))


def consult(register: str, rec_id: str) -> Response:
    """« Consulter » — a 302 to the signed URL; every failure lands back on
    the worksheet with its own French sentence, logged (ids only)."""
    ids = {"register": register, "reconciliation_id": sanitize_log_value(rec_id)}
    try:
        rec = rc.get_reconciliation_strict(register, rec_id)
    except Exception as exc:
        log_unexpected("reconciliation statement: read failed", exc_info=False,
                       error_type=type(exc).__name__, **ids)
        return redirect(_worksheet(register, rec_id, releve_erreur="indisponible"))
    if rec is None:
        return redirect(url_for(_endpoint(register, "reconciliations_list")))
    if not rc.has_statement(rec):
        return redirect(_worksheet(register, rec_id, releve="aucun"))
    try:
        url = rc.consult_url(register, rec)
    except rc.StoredPathRefused:
        log_unexpected("reconciliation statement: stored path refused",
                       exc_info=False, **ids)
        return redirect(_worksheet(register, rec_id, releve_erreur="chemin"))
    except rc.ObjectMissing:
        log_unexpected("reconciliation statement: object missing",
                       exc_info=False, **ids)
        return redirect(_worksheet(register, rec_id, releve_erreur="fichier"))
    except Exception as exc:
        # The reload or the signing: their text can name the object — the
        # class only, never a traceback.
        log_unexpected("reconciliation statement: signing failed",
                       exc_info=False, error_type=type(exc).__name__, **ids)
        return redirect(_worksheet(register, rec_id, releve_erreur="consultation"))
    return redirect(url)


def missing_count(recs: list) -> int:
    """How many of *recs* are completed without their statement — the
    « Relevés manquants » banner (one authority: ``rc.statement_missing``)."""
    return sum(1 for r in recs if rc.statement_missing(r))


#: The reconciliations list's ``?releve=`` filter (Security Rules: a
#: query-string filter is checked against its vocabulary; any other value
#: is no filter).
VALID_RELEVE_FILTERS = ("", "manquant")


def list_filter() -> str:
    value = request.args.get("releve", "")
    return value if value in VALID_RELEVE_FILTERS else ""
