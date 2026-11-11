"""Bank statements of reconciliations — the copy each one rests on.

Art. 32, 3°, of the Règlement sur la comptabilité et les normes d'exercice
professionnel des avocats (RLRQ c. B-1, r. 5) has the lawyer keep, for at
least 7 years, « les livrets ou relevés d'institutions financières » behind
his journals and registers. Since 2026-11-10 (the lawyer's decisions of
that day) each bank reconciliation — of a trust account
(``trust_reconciliations``) or of an administration account or card
(``admin_reconciliations``) — carries ONE copy of the statement it was made
from:

* a « brouillon » takes one, replaces it, or gives it up;
* completing REQUIRES it — ``trust.complete_reconciliation`` and
  ``admin_ledger.complete_reconciliation`` refuse without one
  (:data:`REQUIRED_TO_COMPLETE`);
* a « complétée » that has none — every reconciliation completed before
  this lot — takes one whenever the lawyer gets to it, with no deadline
  (the catch-up: « Relevés manquants », :func:`statement_missing`);
* a statement joined to a completed reconciliation is FROZEN: never
  replaced, never removed.

This module is the ONE door of those statements: their fields on the
reconciliation document, their Storage objects, and every rule on both.

Shape — six fields on the reconciliation's own document (:data:`FIELDS`),
absent until a statement is attached; a legacy reconciliation simply lacks
them::

    "statement_id": str,             # UUIDv4 reserved when the session opened
    "statement_storage_path": str,   # users/{uid}/{register}/conciliations/
                                     #   {reconciliation_id}/releve-{statement_id}{ext}
    "statement_file_type": str,      # the SNIFFED MIME type
    "statement_file_size": int,      # bytes: the staged object's REAL size
    "statement_md5": str,            # the staged object's base64 MD5, or ""
    "statement_attached_at": datetime,  # an instant (UTC)

and, once a draft's statement has been removed or replaced, the ids those
writes RETIRED — never attached again (:data:`RETIRED_FIELD`; kept when
the six fields go)::

    "statement_retired_ids": list[str],  # in the order they were retired

No file name is stored, and none reaches an object name (data
minimisation, as for the proofs of identity): « Consulter » serves
``releve-AAAA-MM{ext}``, from the period's end.

The upload — browser → GCS, never through the application (App Engine caps
a request at 32 MB):

1. :func:`open_statement_upload` judges the extension, the declared size
   and the reconciliation (a strict read: it must exist and accept a
   statement) BEFORE a byte moves, reserves the statement's id and opens a
   create-only resumable session on ``staging/{uid}/releve/{id}/upload{ext}``,
   typed by the whitelist, never by the browser;
2. :func:`file_staged_statement` checks that name's exact shape under the
   request's uid, the real size and a 512-byte sniff, copies the object
   GCS-side and create-only to its final path — the object BEFORE the
   record — and sets the fields in a transaction that re-reads the
   reconciliation. The staging object is consumed whatever the outcome,
   bar an unknown one (``incertain``: both objects are kept, as in
   ``models/piece_identite``).

The five-segment staging shape and its literal third segment ``releve``
are refused by every other finalizer — the documents' and the receipts'
(four segments), the proofs' (``identite``), the connector's (``mcp``) —
and this module refuses theirs.

Declared exceptions to the house rules:

* **Both registers, one module** — beside « mirror, never share », the rule
  that keeps the two registers free to diverge. A statement moves no money
  and reads no entry; the register only chooses the collection and the
  path segment (:data:`REGISTERS`). This module imports neither register's
  model: they import it.
* **Writers return (doc, errors, code)** — :class:`Outcome`, as in
  ``models/piece_identite``: a route maps the closed codes to a status
  without parsing French.
* **The record first, then the object, when a statement goes** — a draft's
  removal, a draft's replacement, an abandoned draft
  (:func:`erase_statement_object`): the reverse of ``piece_identite``'s
  deletion. A COMPLETED reconciliation must never point at an erased
  object, and only a draft's statement ever goes. A removal or a
  replacement RETIRES that statement's id in its own transaction
  (:data:`RETIRED_FIELD`), and no finalization attaches a retired id again
  — not even a re-post of that very upload (the third review of the
  rebase, 2026-11-11) —, so no record can come back to the object erased
  after it. A completion re-reads the statement inside its transaction
  and refuses one changed since its pre-pass checked the object (its id,
  then the reconciliation's etag). A record found pointing at an erased
  path is therefore a defect, raised as « erased object referenced ». An
  erase that fails leaves an object no screen reaches, logged by ids for
  an operator.
* **No etag on these writes.** The transaction re-reads the
  reconciliation's status and statement — the only state these rules
  judge — and regenerates its etag with the provenance stamps.

Never reachable by the connector: no module of ``mcp/``, ``services/`` or
``dav/`` imports this one (pinned by a sweep), and its writers are among
the calls the « register_setup » promise forbids (``mcp/disclosure.py``).
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from typing import NamedTuple, Optional

from firebase_admin import storage
from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import firestore

from models import concurrency, db, provenance
from models import document as document_model
from models.document import sign_blob_url
from utils import storage_identity
from utils.format_fr import formats_label
from utils.logging_setup import (
    log_admin_ledger_event,
    log_trust_event,
    log_unexpected,
    sanitize_log_value,
)

logger = logging.getLogger(__name__)

# ── Constants (one source; labels derived, pinned by tests) ──────────────

TRUST = "fideicommis"
ADMIN = "administration"
#: Each register — also the object path's segment — and its
#: reconciliations' collection (pinned equal to the models' constants).
REGISTERS: dict[str, str] = {
    TRUST: "trust_reconciliations",
    ADMIN: "admin_reconciliations",
}
#: The six fields a statement writes on its reconciliation.
FIELDS: tuple[str, ...] = (
    "statement_id", "statement_storage_path", "statement_file_type",
    "statement_file_size", "statement_md5", "statement_attached_at",
)
#: The statement ids a removal or a replacement retired — never attached
#: again. Not among :data:`FIELDS`: it stays when they go.
RETIRED_FIELD = "statement_retired_ids"

STATUS_DRAFT = "brouillon"
STATUS_COMPLETED = "complétée"

STAGING_SEGMENT = "releve"
STAGING_STEM = "upload"
OBJECT_PREFIX = "releve-"

# The three types every browser SHOWS inline. Keys are lower-case: a file
# name's extension is lower-cased before any lookup (« RELEVE.PDF »).
MIME_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}
EXTENSIONS: tuple[str, ...] = tuple(MIME_TYPES)
#: The file input's ``accept``: the extensions, then the distinct types.
ACCEPT = ",".join(EXTENSIONS + tuple(dict.fromkeys(MIME_TYPES.values())))


# « PDF, JPG ou PNG » — the rule the proofs and the receipts share
# (utils/format_fr.formats_label), over the very mapping that decides.
FORMATS_LABEL = formats_label(MIME_TYPES)
_MIB = 1024 * 1024
MAX_SIZE = 20 * _MIB
MAX_SIZE_LABEL = f"{MAX_SIZE // _MIB} Mo"
SIGNED_URL_MINUTES = 15

# The closed outcome codes.
CODE_OK = "ok"                      # a session is open
CODE_JOINT = "joint"                # THIS call attached the statement
CODE_REJOUE = "rejoue"              # already attached (a replay): nothing new
CODE_RETIRE = "retire"              # THIS call removed the statement
CODE_FORME = "forme"                # not this module's staging shape (400)
CODE_REFUS = "refus"                # refused, with its French reason
CODE_INDISPONIBLE = "indisponible"  # a store failure: nothing done, retry
CODE_INCERTAIN = "incertain"        # the outcome could not be established

SESSION_CODES = (CODE_OK, CODE_REFUS, CODE_INDISPONIBLE)
FILING_CODES = (CODE_JOINT, CODE_REJOUE, CODE_FORME, CODE_REFUS,
                CODE_INDISPONIBLE, CODE_INCERTAIN)
REMOVAL_CODES = (CODE_RETIRE, CODE_REFUS, CODE_INDISPONIBLE, CODE_INCERTAIN)

# French sentences (Rule 3).
TYPE_REFUSED = f"Type de fichier non autorisé. Formats acceptés : {FORMATS_LABEL}."
SIZE_REFUSED = f"Le relevé doit faire entre 1 octet et {MAX_SIZE_LABEL}."
NOT_FOUND = "Conciliation introuvable."
FROZEN = (
    "Le relevé de cette conciliation complétée est figé : il ne se remplace "
    "ni ne se retire (art. 32 du Règlement sur la comptabilité)."
)
NOT_ACCEPTED = "Cette conciliation ne peut pas recevoir de relevé."
NOTHING_TO_REMOVE = "Aucun relevé n'est joint à cette conciliation."
REQUEST_INVALID = "Requête invalide."
STAGED_MISSING = "Fichier téléversé introuvable. Réessayez."
RETIRED = (
    "Ce relevé a été retiré ou remplacé depuis son téléversement : il ne se "
    "joint plus. Rechargez la page."
)
STAGED_READ_FAILED = "Lecture du fichier téléversé impossible. Réessayez."
CONTENT_MISMATCH = "Le contenu du fichier ne correspond pas à son extension."
SESSION_FAILED = "Erreur lors de l'ouverture du téléversement. Réessayez."
FILING_FAILED = "Erreur lors de l'enregistrement du relevé. Veuillez réessayer."
FILING_UNCERTAIN = (
    "L'enregistrement du relevé n'a pas pu être confirmé : rechargez la page "
    "pour voir s'il est joint, avant toute autre action."
)
OCCUPIED = (
    "Un autre fichier occupe déjà l'emplacement de ce relevé : rien n'a été "
    "joint. Recommencez le téléversement."
)
REMOVE_FAILED = "Erreur lors du retrait du relevé. Veuillez réessayer."
REMOVE_UNCERTAIN = (
    "Le retrait du relevé n'a pas pu être confirmé : rechargez la page pour "
    "voir s'il est encore joint."
)
PATH_REFUSED = (
    "Le chemin enregistré de ce relevé n'est pas celui d'un relevé : "
    "consultation refusée."
)
OBJECT_MISSING = "Le fichier de ce relevé est introuvable."
CONSULT_FAILED = "Consultation impossible pour le moment — réessayez."
#: The completion's refusals, both registers (their ``_ABORT_MESSAGES``).
REQUIRED_TO_COMPLETE = (
    "Joignez le relevé bancaire de la période avant de compléter la "
    "conciliation."
)
OBJECT_MISSING_TO_COMPLETE = (
    "Le fichier du relevé joint est introuvable : remplacez le relevé, puis "
    "complétez la conciliation."
)
PATH_REFUSED_TO_COMPLETE = (
    "Le relevé joint n'est pas enregistré à l'emplacement d'un relevé : "
    "remplacez-le, puis complétez la conciliation."
)
STATEMENT_CHANGED = (
    "Le relevé a changé pendant la complétion : vérifiez-le, puis "
    "recommencez."
)
UPLOAD_HINT = f"{FORMATS_LABEL} — {MAX_SIZE_LABEL} au plus."


# ── Results and exceptions ───────────────────────────────────────────────


class Outcome(NamedTuple):
    """A writer's answer: ``(doc, errors, code)`` — see the docstring."""

    doc: Optional[dict]
    errors: list
    code: str


class StoredPathRefused(Exception):
    """A reconciliation's ``statement_storage_path`` is not the shape this
    module builds: no Storage verb may be pointed at it. No message."""


class ObjectMissing(Exception):
    """The statement's record exists, its object does not. No message."""


class _Refused(Exception):
    """Raised inside a transactional body: nothing is written."""

    def __init__(self, errors: list[str], code: str = CODE_REFUS):
        super().__init__("refused")
        self.errors = list(errors)
        self.code = code


class _Exists(Exception):
    """The reconciliation already holds THIS statement (read in the txn)."""

    def __init__(self, current: dict):
        super().__init__("exists")
        self.current = current


# ── Pure checks ──────────────────────────────────────────────────────────


def has_statement(rec: object) -> bool:
    """Whether *rec* carries a statement (a legacy one lacks the key)."""
    return isinstance(rec, dict) and bool(rec.get("statement_storage_path"))


def statement_missing(rec: object) -> bool:
    """The ONE authority for « relevé manquant »: a COMPLETED reconciliation
    without its statement — every list, filter and count reads this."""
    return (isinstance(rec, dict) and rec.get("status") == STATUS_COMPLETED
            and not has_statement(rec))


def accepts_statement(rec: object) -> bool:
    """A brouillon always does (attach or replace); a complétée only while
    it has none (the catch-up); then it is frozen."""
    if not isinstance(rec, dict):
        return False
    if rec.get("status") == STATUS_DRAFT:
        return True
    return rec.get("status") == STATUS_COMPLETED and not has_statement(rec)


def removable(rec: object) -> bool:
    """Only a brouillon's statement goes."""
    return (isinstance(rec, dict) and rec.get("status") == STATUS_DRAFT
            and has_statement(rec))


def file_extension(filename: object) -> str:
    """The extension of *filename*, LOWER-CASED with its dot; ``''`` when it
    has none (or is not a string)."""
    if not isinstance(filename, str) or "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[1].lower()


def _register(register: str) -> str:
    if register not in REGISTERS:
        raise ValueError("unknown register")
    return register


def staging_object(uid: object, statement_id: str, ext: str) -> str:
    """``staging/{uid}/releve/{statement_id}/upload{ext}`` — the ONE builder.

    *ext* must already be lower-case. Raises
    ``storage_identity.InvalidStorageUid`` for a uid that cannot name a
    prefix, ``ValueError`` for an id or an extension this module never
    mints."""
    uid = storage_identity.require_uid(uid)
    if not document_model.is_canonical_uuid4(statement_id) or ext not in MIME_TYPES:
        raise ValueError("not a statement's staging object")
    return f"staging/{uid}/{STAGING_SEGMENT}/{statement_id}/{STAGING_STEM}{ext}"


def storage_path(uid: object, register: str, rec_id: str, statement_id: str,
                 ext: str) -> str:
    """``users/{uid}/{register}/conciliations/{rec_id}/releve-{statement_id}{ext}``
    — the ONE builder of a statement's object name (ids only). Raises like
    :func:`staging_object`."""
    uid = storage_identity.require_uid(uid)
    _register(register)
    if (not document_model.is_addressable_id(rec_id)
            or not document_model.is_canonical_uuid4(statement_id)
            or ext not in MIME_TYPES):
        raise ValueError("not a statement's object")
    return (f"users/{uid}/{register}/conciliations/{rec_id}/"
            f"{OBJECT_PREFIX}{statement_id}{ext}")


def parse_staging_object(objet: object, uid: object) -> Optional[tuple[str, str]]:
    """``(statement_id, ext)`` when *objet* is EXACTLY a statement's staging
    name under the REQUEST's uid, else ``None`` — every other shape is
    refused before any Storage verb: the documents' and receipts' (four
    segments), the proofs' (``…/identite/…``), the connector's
    (``…/mcp/…``), the zip exports', another uid's, an upper-case
    extension."""
    if not isinstance(objet, str):
        return None
    parts = objet.split("/")
    if len(parts) != 5 or parts[0] != "staging" or parts[2] != STAGING_SEGMENT:
        return None
    stem, ext = parts[4][:len(STAGING_STEM)], parts[4][len(STAGING_STEM):]
    if stem != STAGING_STEM or ext not in MIME_TYPES:
        return None
    try:
        expected = staging_object(uid, parts[3], ext)
    except (storage_identity.StorageIdentityUnavailable, ValueError):
        return None
    return (parts[3], ext) if expected == objet else None


def checked_path(rec: object, register: str) -> str:
    """The stored object path of *rec*'s statement, re-checked against the
    ONE shape :func:`storage_path` builds before any Storage verb points at
    it — a record whose path was altered must never aim an erase (or a
    signed URL) at another object. The uid segment is validated, never
    compared with the session's (a changed Firebase uid must not orphan
    every statement); then the register, the reconciliation's own id, the
    statement's id and the extension. Raises :class:`StoredPathRefused`."""
    record = rec if isinstance(rec, dict) else {}
    path = record.get("statement_storage_path")
    rec_id = record.get("id")
    statement_id = record.get("statement_id")
    if not all(isinstance(v, str) for v in (path, rec_id, statement_id)):
        raise StoredPathRefused()
    parts = path.split("/")
    head = OBJECT_PREFIX + statement_id
    if len(parts) != 6 or not parts[5].startswith(head):
        raise StoredPathRefused()
    try:
        expected = storage_path(parts[1], register, rec_id, statement_id,
                                parts[5][len(head):])
    except (storage_identity.StorageIdentityUnavailable, ValueError):
        raise StoredPathRefused() from None
    if path != expected:
        raise StoredPathRefused()
    return path


def _ext_of_path(path: str) -> str:
    """The extension of a path :func:`checked_path` accepted."""
    return "." + path.rsplit(".", 1)[1]


def _period_day(value: object) -> Optional[date]:
    """A stored date-only value's UTC calendar day (Rule 5: never through
    ``to_mtl``)."""
    if isinstance(value, datetime):
        return (value.astimezone(timezone.utc) if value.tzinfo else value).date()
    if isinstance(value, date):
        return value
    return None


def consult_filename(rec: dict, ext: str) -> str:
    """``releve-AAAA-MM{ext}`` — from the period's end; ``releve{ext}``
    when the stored day is unreadable."""
    day = _period_day((rec or {}).get("period_end"))
    return f"releve-{day:%Y-%m}{ext}" if day else f"releve{ext}"


# ── Readers ──────────────────────────────────────────────────────────────


def _ref(register: str, rec_id: str):
    return db.collection(REGISTERS[_register(register)]).document(rec_id)


def get_reconciliation_strict(register: str, rec_id: str) -> Optional[dict]:
    """One reconciliation, ``None`` when it does not exist or an id cannot
    name one (Rule « a blank id never queries »). A read failure RAISES."""
    if not document_model.is_addressable_id(rec_id):
        return None
    snap = _ref(register, rec_id).get()
    return {**(snap.to_dict() or {}), "id": snap.id} if snap.exists else None


# ── Logging and Storage helpers ──────────────────────────────────────────


def _ids(register: str, rec_id: object, statement_id: object = None) -> dict:
    ids = {"register": register,
           "reconciliation_id": sanitize_log_value(rec_id)}
    if statement_id is not None:
        ids["statement_id"] = sanitize_log_value(statement_id)
    return ids


def _error_type(exc: BaseException) -> str:
    return type(exc).__name__


def _log_attached(register: str, rec_id: str, statement_id: str, size: int,
                  file_type: str, *, catch_up: bool) -> None:
    """The typed event of a statement THIS call attached — ids, size and
    type only, never a file name. ``catch_up``: joined to a reconciliation
    already completed."""
    fields = {"statement_id": statement_id, "size": size,
              "file_type": file_type, "catch_up": catch_up}
    if register == TRUST:
        log_trust_event("trust_reconciliation_statement_attached",
                        reconciliation_id=rec_id, **fields)
    else:
        log_admin_ledger_event("admin_reconciliation_statement_attached",
                               reconciliation_id=rec_id, **fields)


def _log_removed(register: str, rec_id: str, statement_id: str) -> None:
    if register == TRUST:
        log_trust_event("trust_reconciliation_statement_removed",
                        reconciliation_id=rec_id, statement_id=statement_id)
    else:
        log_admin_ledger_event("admin_reconciliation_statement_removed",
                               reconciliation_id=rec_id,
                               statement_id=statement_id)


def _erase_generation(path: str, generation: Optional[int],
                      metageneration: Optional[int], ids: dict) -> None:
    """Erase *path* at *generation* — the one THIS call created — for good,
    and only while its *metageneration* is still the last one THIS call saw.
    ``None`` (an adopted object, or nothing written) erases nothing. A live
    object of another generation is not this call's, and one whose
    metageneration moved was ADOPTED meanwhile by a parallel finalization of
    the same upload (an adoption always patches the object, which raises its
    metageneration) — that call may be about to point a reconciliation at
    it: 412, untouched (the race the review of 2026-11-10 found: a check of
    the record, then an erase, could otherwise freeze a reconciliation on an
    erased object)."""
    if generation is None:
        return
    try:
        blob = storage.bucket().blob(path)
        blob.reload()
        blob.delete(if_generation_match=generation,
                    if_metageneration_match=metageneration)
    except NotFound:
        return
    except PreconditionFailed:
        # Adopted meanwhile (its adopter attaches it, or names it « adopted
        # object left »), or this call's own patch landed unanswered: the
        # object stays — named here, for an operator, by its ids.
        logger.warning("reconciliation statement: rollback erase skipped: "
                       "%s / %s", ids.get("reconciliation_id"),
                       ids.get("statement_id"))
        return
    except Exception as exc:
        log_unexpected("reconciliation statement: rollback erase failed",
                       exc_info=False, error_type=_error_type(exc), **ids)


def _consume_staging(blob) -> None:
    """Delete the staging object at the generation its reload loaded.
    Best-effort: the bucket's ``staging/`` 7-day lifecycle rule sweeps what
    is left."""
    try:
        if blob.generation is None:
            blob.reload()
        blob.delete(if_generation_match=blob.generation)
    except NotFound:
        return
    except Exception as exc:
        logger.warning("reconciliation statement: staging cleanup failed: %s",
                       type(exc).__name__)


def erase_statement_object(register: str, rec: dict) -> None:
    """Erase the object a statement record pointed at, AFTER the record
    stopped pointing at it — a replaced or removed draft statement, an
    abandoned draft's. A follow-on effect: never raises; a refused path or
    a failed erase is logged by ids (the object then waits for an
    operator, under its reconciliation's prefix)."""
    ids = _ids(register, (rec or {}).get("id"), (rec or {}).get("statement_id"))
    try:
        path = checked_path(rec, register)
    except StoredPathRefused:
        log_unexpected("reconciliation statement: stored path refused",
                       exc_info=False, **ids)
        return
    try:
        blob = storage.bucket().blob(path)
        blob.reload()
        # The reload loaded the live generation, which the delete carries:
        # nothing survives as a non-current version (a draft's statement is
        # no record the regulation keeps).
        blob.delete(if_generation_match=blob.generation)
    except NotFound:
        # Gone already — or a retried delete whose first answer was lost
        # (the library retries a conditional delete): the record is
        # checked all the same.
        pass
    except Exception as exc:
        log_unexpected("reconciliation statement: object erase failed",
                       exc_info=False, error_type=_error_type(exc), **ids)
        return
    _check_after_erase(register, rec, path, ids)


def _check_after_erase(register: str, rec: dict, path: str, ids: dict) -> None:
    """After an erase, read the reconciliation again: a record pointing at
    the erased path is a statement whose bytes survive only in the bucket's
    7-day soft delete. A retired statement is never attached again, so it
    is a DEFECT — an ERROR an operator acts on, by these ids, within the
    week, whatever the record's ``status`` (read here: it may lag a
    completion in flight; a « complétée » one, frozen, is repaired by
    nothing but the restore). Never raises."""
    rec_id = (rec or {}).get("id")
    if not rec_id:
        return
    try:
        current = get_reconciliation_strict(register, rec_id)
    except Exception as exc:
        log_unexpected("reconciliation statement: erase check unreadable",
                       exc_info=False, error_type=_error_type(exc), **ids)
        return
    if current is not None and current.get("statement_storage_path") == path:
        log_unexpected("reconciliation statement: erased object referenced",
                       exc_info=False,
                       status=sanitize_log_value(current.get("status")), **ids)


def _uid_refusal(value: object) -> tuple[Optional[str], Optional[Outcome]]:
    """``(uid, None)``, or ``(None, refusal)`` when *value* cannot name a
    Storage prefix — the sentence chosen by type, never read off the error."""
    try:
        return storage_identity.require_uid(value), None
    except storage_identity.StorageIdentityUnavailable as exc:
        return None, Outcome(None, [storage_identity.public_message(exc)],
                             CODE_INDISPONIBLE)


def _read_refusal(register: str, rec_id: str, ids: dict
                  ) -> tuple[Optional[dict], Optional[Outcome]]:
    """``(rec, None)`` — or the refusal: a failed read is
    ``indisponible``, never « introuvable »."""
    try:
        rec = get_reconciliation_strict(register, rec_id)
    except Exception as exc:
        log_unexpected("reconciliation statement: reconciliation unreadable",
                       exc_info=False, error_type=_error_type(exc), **ids)
        return None, Outcome(None, [concurrency.READ_UNAVAILABLE_ERROR],
                             CODE_INDISPONIBLE)
    if rec is None:
        return None, Outcome(None, [NOT_FOUND], CODE_REFUS)
    return rec, None


def _not_accepted(rec: dict) -> list[str]:
    return [FROZEN] if rec.get("status") == STATUS_COMPLETED else [NOT_ACCEPTED]


def completion_refusal(register: str, rec: dict) -> str:
    """The French reason *rec* may not be completed on its statement — ``""``
    when it may: a statement is attached, its stored path is this module's
    shape, and its object EXISTS. A completion freezes the statement, so it
    checks the bytes it freezes, not only the fields (the review of
    2026-11-10). Called by both registers' ``complete_reconciliation``
    before their transaction, which then refuses a statement id that moved.
    Never raises: a failed read is « indisponible », logged by ids."""
    if not has_statement(rec):
        return REQUIRED_TO_COMPLETE
    ids = _ids(register, rec.get("id"), rec.get("statement_id"))
    try:
        path = checked_path(rec, register)
    except StoredPathRefused:
        log_unexpected("reconciliation statement: stored path refused",
                       exc_info=False, **ids)
        return PATH_REFUSED_TO_COMPLETE
    try:
        storage.bucket().blob(path).reload()
    except NotFound:
        log_unexpected("reconciliation statement: object missing",
                       exc_info=False, **ids)
        return OBJECT_MISSING_TO_COMPLETE
    except Exception as exc:
        log_unexpected("reconciliation statement: object check failed",
                       exc_info=False, error_type=_error_type(exc), **ids)
        return concurrency.READ_UNAVAILABLE_ERROR
    return ""


# ── Writers ──────────────────────────────────────────────────────────────


def open_statement_upload(
    register: str,
    rec_id: str,
    filename: object,
    size: object,
    user_id: object,
    *,
    origin: str,
) -> Outcome:
    """Open the browser's direct upload of a reconciliation's statement —
    every check BEFORE a byte moves.

    In order: the uid, the file name's lower-cased extension, the declared
    size (1 to :data:`MAX_SIZE`), the reconciliation (strict read: it must
    exist and accept a statement — :func:`accepts_statement`). Then the
    statement's id is reserved and a resumable session opened on its staging
    name: ``size=`` (GCS refuses any byte beyond it),
    ``if_generation_match=0`` (create-only), the whitelist's content type.

    ``doc`` is ``{"url", "objet", "statement_id"}`` on :data:`CODE_OK`.
    ``url`` is a write capability: never log, persist or render it but to
    the uploading script. Codes: :data:`SESSION_CODES`."""
    _register(register)
    uid, refused = _uid_refusal(user_id)
    if refused is not None:
        return refused
    ext = file_extension(filename)
    if ext not in MIME_TYPES:
        return Outcome(None, [TYPE_REFUSED], CODE_REFUS)
    if (not isinstance(size, int) or isinstance(size, bool)
            or not 1 <= size <= MAX_SIZE):
        return Outcome(None, [SIZE_REFUSED], CODE_REFUS)
    ids = _ids(register, rec_id)
    rec, refused = _read_refusal(register, rec_id, ids)
    if refused is not None:
        return refused
    if not accepts_statement(rec):
        return Outcome(None, _not_accepted(rec), CODE_REFUS)

    statement_id = str(uuid.uuid4())
    objet = staging_object(uid, statement_id, ext)
    ids["statement_id"] = statement_id
    try:
        url = storage.bucket().blob(objet).create_resumable_upload_session(
            content_type=MIME_TYPES[ext], size=size, origin=origin,
            if_generation_match=0,
        )
    except Exception as exc:
        # The class only: the session URI is a capability.
        log_unexpected("reconciliation statement: session open failed",
                       exc_info=False, error_type=_error_type(exc), **ids)
        return Outcome(None, [SESSION_FAILED], CODE_INDISPONIBLE)
    if not isinstance(url, str) or not url.startswith("https://"):
        log_unexpected("reconciliation statement: session open failed",
                       exc_info=False, error_type="not_https", **ids)
        return Outcome(None, [SESSION_FAILED], CODE_INDISPONIBLE)
    return Outcome({"url": url, "objet": objet, "statement_id": statement_id},
                   [], CODE_OK)


def _is_this_statement(rec: dict, statement_id: str, path: str) -> bool:
    return (rec.get("statement_id") == statement_id
            and rec.get("statement_storage_path") == path)


def _retired(rec: dict) -> list[str]:
    """The statement ids a removal or a replacement retired from *rec* —
    never attached again. A legacy record lacks the key."""
    value = rec.get(RETIRED_FIELD)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _retiring(rec: dict) -> list[str]:
    """*rec*'s retired ids, plus the statement it holds now — the value a
    removal or a replacement of that statement writes."""
    retired = _retired(rec)
    current = rec.get("statement_id")
    if isinstance(current, str) and current and current not in retired:
        retired.append(current)
    return retired


def _previous(rec: dict) -> Optional[dict]:
    """What :func:`erase_statement_object` needs of the statement *rec*
    holds now — ``None`` when it holds none."""
    if not has_statement(rec):
        return None
    return {"id": rec.get("id"), "statement_id": rec.get("statement_id"),
            "statement_storage_path": rec.get("statement_storage_path")}


def file_staged_statement(
    register: str,
    rec_id: str,
    objet: object,
    user_id: object,
) -> Outcome:
    """Attach the uploaded staging object *objet* as *rec_id*'s statement.

    The ONE door every Storage verb of an attachment goes through. In order:
    the staging name's exact shape under the request's uid (else ``forme``,
    nothing touched — from here on a ``finally`` CONSUMES the staging
    object); its reload (``NotFound``: a replay, answered from the
    reconciliation); its REAL size; the reconciliation (strict read —
    already this statement is ``rejoue``; one that accepts no statement is
    refused); a 512-byte sniff; a GCS-side, create-only copy (a 412 on
    identical bytes ADOPTS the object, which this call then never erases; on
    an occupant it cannot reload, ``indisponible``);
    the object's content type; then a transaction that re-reads the
    reconciliation and sets the six fields. A draft's earlier statement is
    erased AFTER that commit. Codes: :data:`FILING_CODES`."""
    _register(register)
    uid, refused = _uid_refusal(user_id)
    if refused is not None:
        return refused
    parsed = parse_staging_object(objet, uid)
    if parsed is None:
        return Outcome(None, [REQUEST_INVALID], CODE_FORME)
    statement_id, ext = parsed
    ids = _ids(register, rec_id, statement_id)
    try:
        staging = storage.bucket().blob(objet)
    except Exception as exc:
        log_unexpected("reconciliation statement: staging reload failed",
                       exc_info=False, error_type=_error_type(exc), **ids)
        return Outcome(None, [STAGED_READ_FAILED], CODE_INDISPONIBLE)
    keep = {"staging": False, "adopted": False}
    outcome: Optional[Outcome] = None
    try:
        outcome = _file(staging, register, rec_id, uid, statement_id, ext,
                        ids, keep)
        return outcome
    finally:
        if keep["adopted"] and (outcome is None or outcome.code not in (
                CODE_JOINT, CODE_REJOUE, CODE_INCERTAIN)):
            log_unexpected("reconciliation statement: adopted object left",
                           exc_info=False, **ids)
        if not keep["staging"]:
            _consume_staging(staging)


def _file(staging, register: str, rec_id: str, uid: str, statement_id: str,
          ext: str, ids: dict, keep: dict) -> Outcome:
    """The steps of :func:`file_staged_statement` after the shape."""
    try:
        staging.reload()
    except NotFound:
        keep["staging"] = True      # nothing left to consume
        return _replayed(register, rec_id, uid, statement_id, ext, ids)
    except Exception as exc:
        log_unexpected("reconciliation statement: staging reload failed",
                       exc_info=False, error_type=_error_type(exc), **ids)
        return Outcome(None, [STAGED_READ_FAILED], CODE_INDISPONIBLE)

    try:
        size = int(staging.size)
    except (TypeError, ValueError):
        size = -1
    if not 1 <= size <= MAX_SIZE:
        return Outcome(None, [SIZE_REFUSED], CODE_REFUS)

    rec, refused = _read_refusal(register, rec_id, ids)
    if refused is not None:
        return refused
    path = storage_path(uid, register, rec_id, statement_id, ext)
    if _is_this_statement(rec, statement_id, path):
        return Outcome(rec, [], CODE_REJOUE)
    # Retired: refused before any copy (the transaction re-checks it).
    if statement_id in _retired(rec):
        return Outcome(None, [RETIRED], CODE_REFUS)
    if not accepts_statement(rec):
        return Outcome(None, _not_accepted(rec), CODE_REFUS)

    try:
        header = staging.download_as_bytes(
            start=0, end=document_model._SNIFF_PROBE_BYTES - 1,
            if_generation_match=staging.generation,
        )
    except Exception as exc:
        log_unexpected("reconciliation statement: staged read failed",
                       exc_info=False, error_type=_error_type(exc), **ids)
        return Outcome(None, [STAGED_READ_FAILED], CODE_INDISPONIBLE)
    sniffed = document_model._sniff_header(header, ext)
    if sniffed != MIME_TYPES[ext]:
        return Outcome(None, [CONTENT_MISMATCH], CODE_REFUS)

    # The generation THIS call created, and the last metageneration it saw
    # on it: a rollback erases only that pair (_erase_generation).
    created_generation: Optional[int] = None
    created_metageneration: Optional[int] = None
    try:
        dest = storage.bucket().blob(path)
        try:
            token, _, _ = dest.rewrite(staging, if_generation_match=0)
            while token is not None:
                token, _, _ = dest.rewrite(staging, token=token,
                                           if_generation_match=0)
        except PreconditionFailed:
            # The object exists: only this upload's finalizations write its
            # reserved path (a parallel one, an « incertain » predecessor,
            # or this call's own retried rewrite). ADOPTED only when it
            # holds these very bytes — and then never erased by this call.
            # An occupant that cannot be reloaded is UNKNOWN, never « other
            # bytes » (document._object_verdict — the shared ingestion's
            # rule since d4a7965, and the receipts' door's): « indisponible ».
            verdict = document_model._object_verdict(dest, staging)
            if verdict is None:
                log_unexpected("reconciliation statement: occupant unreadable",
                               exc_info=False, **ids)
                return Outcome(None, [FILING_FAILED], CODE_INDISPONIBLE)
            if verdict is False:
                log_unexpected("reconciliation statement: object occupied",
                               exc_info=False, **ids)
                return Outcome(None, [OCCUPIED], CODE_REFUS)
            keep["adopted"] = True
        else:
            created_generation = dest.generation
            created_metageneration = dest.metageneration
        dest.content_type = sniffed
        if created_generation is None:
            dest.patch(if_generation_match=dest.generation)
        else:
            try:
                # Pinned to the metageneration THIS call's copy produced: a
                # parallel finalization that adopted the object patched it
                # FIRST (the second review of 2026-11-10), or this call's
                # own first patch landed with its answer lost. Either way
                # the object is no longer this call's alone: ADOPTED from
                # here on, never erased by this call.
                dest.patch(if_generation_match=created_generation,
                           if_metageneration_match=created_metageneration)
            except PreconditionFailed:
                created_generation = None
                created_metageneration = None
                keep["adopted"] = True
            else:
                created_metageneration = dest.metageneration
    except Exception as exc:
        log_unexpected("reconciliation statement: object write failed",
                       exc_info=False, error_type=_error_type(exc), **ids)
        _erase_unreferenced(register, rec_id, statement_id, path,
                            created_generation, created_metageneration, ids)
        return Outcome(None, [FILING_FAILED], CODE_INDISPONIBLE)

    now = datetime.now(timezone.utc)
    md5 = staging.md5_hash if isinstance(staging.md5_hash, str) else ""
    fields = {
        "statement_id": statement_id,
        "statement_storage_path": path,
        "statement_file_type": sniffed,
        "statement_file_size": size,
        "statement_md5": md5 if len(md5) <= 64 else "",
        "statement_attached_at": now,
    }
    ref = _ref(register, rec_id)
    staged: dict = {}

    @firestore.transactional
    def _attach(transaction) -> None:
        staged.clear()
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise _Refused([NOT_FOUND])
        current = {**(snap.to_dict() or {}), "id": snap.id}
        if _is_this_statement(current, statement_id, path):
            raise _Exists(current)
        # A statement a removal or a replacement retired never comes back —
        # not even through a re-post of its own upload, which would point
        # the record at the object its retirement erases.
        if statement_id in _retired(current):
            raise _Refused([RETIRED])
        if not accepts_statement(current):
            raise _Refused(_not_accepted(current))
        staged["previous"] = _previous(current)
        staged["catch_up"] = current.get("status") == STATUS_COMPLETED
        update = {**fields, **provenance.update_fields(now)}
        if staged["previous"]:
            # Replaced: the outgoing statement is retired in this write.
            update[RETIRED_FIELD] = _retiring(current)
        transaction.update(ref, update)
        staged["written"] = True

    try:
        _attach(db.transaction())
    except _Refused as refusal:
        _erase_unreferenced(register, rec_id, statement_id, path,
                            created_generation, created_metageneration, ids)
        return Outcome(None, refusal.errors, refusal.code)
    except _Exists as exists:
        return Outcome(exists.current, [], CODE_REJOUE)
    except Exception as exc:
        # A replaced draft statement is named too: when the outcome stays
        # unknown, its object waits for an operator under these ids.
        previous_ids = ({"previous_statement_id": sanitize_log_value(
            staged["previous"].get("statement_id"))}
            if staged.get("previous") else {})
        log_unexpected("reconciliation statement: record write failed",
                       exc_info=False, error_type=_error_type(exc), **ids,
                       **previous_ids)
        if not staged.get("written"):
            _erase_unreferenced(register, rec_id, statement_id, path,
                                created_generation, created_metageneration,
                                ids)
            return Outcome(None, [FILING_FAILED], CODE_INDISPONIBLE)
        # The commit's answer was lost: re-read to settle it, never report
        # « nothing written ».
        try:
            settled = get_reconciliation_strict(register, rec_id)
        except Exception as settle_exc:
            log_unexpected("reconciliation statement: attachment uncertain",
                           exc_info=False, error_type=_error_type(settle_exc),
                           **ids, **previous_ids)
            keep["staging"] = True
            return Outcome(None, [FILING_UNCERTAIN], CODE_INCERTAIN)
        if settled is None or not _is_this_statement(settled, statement_id, path):
            _erase_unreferenced(register, rec_id, statement_id, path,
                                created_generation, created_metageneration,
                                ids)
            return Outcome(None, [FILING_FAILED], CODE_INDISPONIBLE)

    provenance.note_commit(REGISTERS[register], rec_id)
    previous = staged.get("previous")
    if previous and previous.get("statement_storage_path") != path:
        erase_statement_object(register, previous)
    _log_attached(register, rec_id, statement_id, size, sniffed,
                  catch_up=bool(staged.get("catch_up")))
    return Outcome({**fields, "id": rec_id}, [], CODE_JOINT)


def _erase_unreferenced(register: str, rec_id: str, statement_id: str,
                        path: str, generation: Optional[int],
                        metageneration: Optional[int], ids: dict) -> None:
    """:func:`_erase_generation`, unless the reconciliation points at the
    object (a parallel finalization that adopted it committed). An
    unreadable reconciliation keeps the object."""
    if generation is None:
        return
    try:
        rec = get_reconciliation_strict(register, rec_id)
    except Exception as exc:
        log_unexpected("reconciliation statement: rollback check unreadable",
                       exc_info=False, error_type=_error_type(exc), **ids)
        return
    if rec is not None and _is_this_statement(rec, statement_id, path):
        return
    _erase_generation(path, generation, metageneration, ids)


def _replayed(register: str, rec_id: str, uid: str, statement_id: str,
              ext: str, ids: dict) -> Outcome:
    """The staging object is gone: a finalization arriving AGAIN after the
    first one consumed it. The reconciliation says whether it landed."""
    rec, refused = _read_refusal(register, rec_id, ids)
    if refused is not None:
        return refused
    if _is_this_statement(rec, statement_id,
                          storage_path(uid, register, rec_id, statement_id, ext)):
        return Outcome(rec, [], CODE_REJOUE)
    if statement_id in _retired(rec):
        return Outcome(None, [RETIRED], CODE_REFUS)
    return Outcome(None, [STAGED_MISSING], CODE_REFUS)


def remove_statement(register: str, rec_id: str) -> Outcome:
    """Remove a BROUILLON's statement: the six fields go in a transaction
    that re-reads the reconciliation (a completed one refuses — frozen),
    THEN the object is erased (:func:`erase_statement_object` — a follow-on
    that never raises). A commit whose answer was lost is settled by a
    re-read. Codes: :data:`REMOVAL_CODES`."""
    _register(register)
    ids = _ids(register, rec_id)
    if not document_model.is_addressable_id(rec_id):
        return Outcome(None, [NOT_FOUND], CODE_REFUS)
    ref = _ref(register, rec_id)
    now = datetime.now(timezone.utc)
    staged: dict = {}

    @firestore.transactional
    def _remove(transaction) -> None:
        staged.clear()
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise _Refused([NOT_FOUND])
        current = {**(snap.to_dict() or {}), "id": snap.id}
        if not has_statement(current):
            raise _Refused([NOTHING_TO_REMOVE])
        if not removable(current):
            raise _Refused([FROZEN])
        staged["removed"] = _previous(current)
        transaction.update(ref, {
            **{name: firestore.DELETE_FIELD for name in FIELDS},
            # Retired in this write: never attached again.
            RETIRED_FIELD: _retiring(current),
            **provenance.update_fields(now),
        })
        staged["written"] = True

    try:
        _remove(db.transaction())
    except _Refused as refusal:
        return Outcome(None, refusal.errors, refusal.code)
    except Exception as exc:
        removed = staged.get("removed") or {}
        if removed.get("statement_id"):
            ids["statement_id"] = sanitize_log_value(removed.get("statement_id"))
        log_unexpected("reconciliation statement: removal failed",
                       exc_info=False, error_type=_error_type(exc), **ids)
        if not staged.get("written"):
            return Outcome(None, [REMOVE_FAILED], CODE_INDISPONIBLE)
        try:
            settled = get_reconciliation_strict(register, rec_id)
        except Exception as settle_exc:
            # Unknown: the object is KEPT (erasing it could leave the draft
            # pointing at nothing); if the removal landed, it waits under
            # these ids for an operator.
            log_unexpected("reconciliation statement: removal uncertain",
                           exc_info=False, error_type=_error_type(settle_exc),
                           **ids)
            return Outcome(None, [REMOVE_UNCERTAIN], CODE_INCERTAIN)
        if (settled is not None and settled.get("statement_id")
                == removed.get("statement_id")):
            return Outcome(None, [REMOVE_FAILED], CODE_INDISPONIBLE)

    provenance.note_commit(REGISTERS[register], rec_id)
    removed = staged.get("removed") or {}
    erase_statement_object(register, removed)
    _log_removed(register, rec_id, str(removed.get("statement_id") or ""))
    return Outcome({"id": rec_id}, [], CODE_RETIRE)


def consult_url(register: str, rec: dict) -> str:
    """A 15-minute V4 signed URL that SHOWS *rec*'s statement in the
    browser. The stored path is re-checked (:class:`StoredPathRefused`), the
    object reloaded (:class:`ObjectMissing` when gone), then signed with an
    inline ``Content-Disposition`` naming ``releve-AAAA-MM{ext}`` and the
    whitelist's type. Any other failure PROPAGATES: the caller logs its
    class and answers in French (:data:`CONSULT_FAILED`). Never log,
    persist or render the URL: redirect to it."""
    path = checked_path(rec, register)
    ext = _ext_of_path(path)
    blob = storage.bucket().blob(path)
    try:
        blob.reload()
    except NotFound:
        raise ObjectMissing() from None
    params = {
        "response-content-disposition": document_model.content_disposition(
            consult_filename(rec, ext), inline=True),
        "response-content-type": MIME_TYPES[ext],
    }
    return sign_blob_url(blob, params, expiry_minutes=SIGNED_URL_MINUTES)
