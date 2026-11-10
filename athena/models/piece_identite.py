"""Proofs of identity: the copies a contact's identity was checked against.

Art. 22 of the Règlement sur la comptabilité et les normes d'exercice
professionnel des avocats (RLRQ c. B-1, r. 5) has the lawyer keep « au
dossier » a copy of the documents an identity was verified with. This module
is the ONE door to those copies — their records, their Storage objects and
every rule on both. Filing a proof never changes a compliance status:
``identity_verified`` stays the lawyer's decision (« The lawyer decides »),
and nothing here can reach it.

Shape — ``parties/{partie_id}/pieces_identite/{proof_id}``, a subcollection
the contact owns (Architecture Rule 2; ``pieces_identite`` is the key the
portal intake already reserves for it, ``client/routes.py``)::

    {
      "id": str,            # UUIDv4 RESERVED when the upload session opens,
                            # carried by the staging name (Rule 6)
      "partie_id": str,     # the owner: the parent contact's id
      "display_name": str,  # the lawyer's name for it, ≤ DISPLAY_NAME_MAX;
                            # the file name's stem by default; REFUSED, never
                            # stripped, when security.sanitize would alter it
                            # or it holds a control character
      "file_type": str,     # the SNIFFED MIME type — one of MIME_TYPES' values
      "file_size": int,     # bytes: the staged object's REAL size
      "storage_path": str,  # users/{uid}/parties/{partie_id}/pieces_identite/{id}{ext}
      "created_at", "updated_at", "etag", "created_via", "updated_via"
                            # Rule 7, stamped through models/provenance.py
    }

No ``original_filename``: the browser's file name serves once, as the
default display name, and is never stored (data minimisation). No file name
reaches an object name either — a path holds ids only.

The id (Rule 6) is minted by the server when the session opens and comes
back inside the staging name; that is the documents' reserved-id pattern
(``document.ingest_blob_as_document``). The name is re-checked to its exact
shape under the request's uid, and the record is written with ``create()``:
a forged id can only CREATE a proof of the caller's own upload, never
overwrite one.

Not DAV-exposed: a proof has no vCard property and no UID (Rule 11 does not
apply). No write here touches ``parties/{id}``, so the contact's etag, its
CTag (``dav_sync/parties``) and its place in the contact list never move:
there is no CTag to bump.

Never reachable by the connector (D7): no module of ``mcp/``, ``services/``
or ``dav/`` imports this one (pinned by a sweep), and the deletion trail's
``piece_identite`` rows are filtered out of ``list_deletions``. Proofs are
not documents — documents are bound to a dossier and the connector reads
them. ``parties.kyc_document_ids`` is unrelated (legacy, always ``[]``).

The upload path — the bytes go browser → GCS (App Engine caps a request at
32 MB) and never transit the application:

1. :func:`open_proof_upload` judges the file name, its extension
   (lower-cased), the declared size, the contact and the cap BEFORE any byte
   moves, reserves the proof's id and opens a create-only resumable session
   (``if_generation_match=0``) on ``staging/{uid}/identite/{proof_id}/upload{ext}``
   whose content type is the whitelist's, never the browser's;
2. :func:`file_staged_proof` checks the staging name's exact shape, then the
   name, the real size and the contact, sniffs 512 bytes, copies the object
   GCS-side and create-only to its canonical path — the object BEFORE the
   record — patches its content type, and creates the record with
   ``create()`` in a transaction that re-reads the contact and re-counts the
   cap. From the shape check on, the staging object is consumed whatever the
   outcome, bar one (below).

The five-segment staging shape and its literal third segment are refused by
every other finalizer — the documents' and the receipts' (exactly four
segments), the connector's (``staging/{uid}/mcp/…``) — and this module
refuses theirs, and the zip exports' (``staging/{uid}/exports/…``).

Deletion (D6) erases the live object first, by generation, then deletes the
record EXACTLY ONCE, in a transaction that reads it: only the call that
removed the record answers ``supprime`` — the one its caller journals (ONE
neutral ``audit_events`` row) — and a call that finds it already gone
answers ``deja``. The reverse order, on a failed erase, would strand a
confidential object no screen reaches; this one leaves at worst a listed
entry whose file is gone, which « Consulter » reports and the next
« Supprimer » completes (``incomplet``).

Declared exceptions to the house rules:

* **Writers return (doc, errors, code).** The :class:`Outcome` triple, not
  ``(doc, errors)``: ``code`` belongs to the writer's closed vocabulary
  (:data:`SESSION_CODES`, :data:`FILING_CODES`, :data:`RENAME_CODES`,
  :data:`DELETE_CODES`), so a route maps it to a status or a banner without
  parsing French; ``errors`` carries the French sentence of every code that
  is not a success.
* **Erase, never delete.** Every object this module removes — a rollback, a
  deletion, a consumed staging object — is deleted AT THE GENERATION its own
  reload loaded. google-cloud-storage 3.15.0's ``Blob.delete`` hands
  ``generation=self.generation`` to ``Bucket.delete_blob`` (``blob.py``,
  851-861), whose ``generation`` « permanently deletes a specific revision »
  (``bucket.py`` 1791): the versioned bucket keeps no non-current copy (180
  days), only its 7-day soft delete applies. A rollback erases ONLY the
  generation THIS call created. An object ADOPTED at the reserved path (the
  create-only copy's 412 on identical bytes) is never erased, whatever
  fails afterwards: it may be a parallel finalization's, still to commit
  its record; the « incertain » predecessor's kept copy; or this call's own
  — the library retries a ``rewrite`` sent with ``ifGenerationMatch=0``
  (``DEFAULT_RETRY_IF_GENERATION_SPECIFIED``), so a lost answer comes back
  as a 412 on the object the first attempt created. A call that adopted an
  object and then files nothing (``refus``, ``indisponible``) logs « id
  proof: adopted object left »; a reserved path held by other bytes, or
  that cannot be reloaded, is refused and logs « id proof: object
  occupied ». Both lines carry the ids that name the object kept.
* **An unknown filing outcome KEEPS both objects.** When the record's commit
  raised and the read-back that would settle it
  (``concurrency.settle_failed_create``) fails too, the final object and the
  staging object both stay, « id proof: filing uncertain » is logged with
  ids, and the answer is ``incertain``. The page re-posts the finalization
  once, which the reserved id, the create-only copy and the adoption of
  identical bytes make idempotent; that re-post settles the outcome only
  when the store answers it (``rejoue`` when the first commit landed,
  ``verse`` otherwise). Otherwise the page keeps the uncertain sentence,
  and if the proof is never listed, its object stays under the contact's
  ``pieces_identite/`` prefix, which no screen, deletion or lifecycle rule
  reaches: an operator erases it by hand, from the ids of « id proof:
  filing uncertain ». Erasing instead would leave a listed proof with no
  bytes whenever that commit did land. The same rule holds wherever a
  rollback cannot read whether a committed record points at this call's
  object (« id proof: rollback check unreadable »), and for an adopted or
  occupying object (above): the object is kept, never erased, and the ids
  logged name it for an operator, who erases it the same way once no
  proof under that id is listed.
* **No fail-open single reader.** No display path reads one proof; the
  fail-open twin of :func:`list_proofs_strict` is :func:`list_proofs`.
* **Capped** at :data:`MAX_PER_CONTACT` proofs per contact, checked when a
  session opens AND inside the create transaction.
* **The deletion's etag guards its erase, not its record delete (Rule 7).**
  :func:`delete_proof` compares ``expected_etag`` on the strict read made
  just before the erase — a GCS erase cannot join a Firestore transaction.
  The record delete that follows checks presence only, on purpose:
  refusing it once the file is erased would leave a listed proof with no
  file. A rename landing in that window is deleted with the proof: same
  id, same bytes, and the trail row is neutral.
"""

from __future__ import annotations

import logging
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import NamedTuple, Optional

from firebase_admin import storage
from google.api_core.exceptions import (
    AlreadyExists,
    NotFound,
    PreconditionFailed,
)
from google.cloud import firestore

from models import concurrency, db, provenance
from models import document as document_model
from models import partie as partie_model
from models.document import sign_blob_url
from security import sanitize
from utils import storage_identity
from utils.format_fr import formats_label
from utils.logging_setup import log_unexpected, sanitize_log_value

logger = logging.getLogger(__name__)

# ── Constants (one source; their labels are derived and pinned) ──────────

SUBCOLLECTION = "pieces_identite"
STAGING_SEGMENT = "identite"
STAGING_STEM = "upload"

# The three types every browser SHOWS inline — what « Consulter » promises.
# Keys are lower-case: a file name's extension is lower-cased before any
# lookup (« SCAN.PDF », « PHOTO.JPG »).
MIME_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}
EXTENSIONS: tuple[str, ...] = tuple(MIME_TYPES)
# The file input's ``accept``: the extensions, then the distinct types.
ACCEPT = ",".join(EXTENSIONS + tuple(dict.fromkeys(MIME_TYPES.values())))


# « PDF, JPG ou PNG » — the rule shared with the pièces justificatives
# (utils/format_fr.formats_label; this name kept for the tests that pin it).
_formats_label = formats_label
FORMATS_LABEL = _formats_label(MIME_TYPES)

_MIB = 1024 * 1024
MAX_SIZE = 20 * _MIB
MAX_SIZE_LABEL = f"{MAX_SIZE // _MIB} Mo"
MAX_PER_CONTACT = 20
DISPLAY_NAME_MAX = document_model.DISPLAY_NAME_MAX
SIGNED_URL_MINUTES = 15

# ── Outcome codes (closed vocabularies, one per writer) ──────────────────

CODE_OK = "ok"                      # a session is open
CODE_VERSE = "verse"                # this call filed the proof
CODE_REJOUE = "rejoue"              # already filed (a replay): nothing new
CODE_MODIFIE = "modifie"            # renamed
CODE_INCHANGE = "inchange"          # the same name: nothing written
CODE_SUPPRIME = "supprime"          # THIS call removed the record: journal it
CODE_DEJA = "deja"                  # the record was already gone: no journal
CODE_FORME = "forme"                # not this module's staging shape (400)
CODE_REFUS = "refus"                # the input is refused (French reason)
CODE_VERSION = "version"            # etag missing or stale
CODE_INTROUVABLE = "introuvable"    # no such proof
CODE_CHEMIN = "chemin"              # the stored path is not this module's shape
CODE_FICHIER = "fichier"            # the object could not be erased: nothing deleted
CODE_INCOMPLET = "incomplet"        # object erased, record left: retry completes
CODE_INDISPONIBLE = "indisponible"  # a store failure: nothing done, retry
CODE_INCERTAIN = "incertain"        # the outcome could not be established

SESSION_CODES = (CODE_OK, CODE_REFUS, CODE_INDISPONIBLE)
FILING_CODES = (CODE_VERSE, CODE_REJOUE, CODE_FORME, CODE_REFUS,
                CODE_INDISPONIBLE, CODE_INCERTAIN)
RENAME_CODES = (CODE_MODIFIE, CODE_INCHANGE, CODE_VERSION, CODE_INTROUVABLE,
                CODE_REFUS, CODE_INDISPONIBLE, CODE_INCERTAIN)
DELETE_CODES = (CODE_SUPPRIME, CODE_DEJA, CODE_VERSION, CODE_INTROUVABLE,
                CODE_CHEMIN, CODE_FICHIER, CODE_INCOMPLET, CODE_INDISPONIBLE,
                CODE_INCERTAIN)

# ── French sentences (every number formatted from its constant) ─────────

DISPLAY_NAME_LABEL = "Nom du document"
FILE_NAME_LABEL = "Nom du fichier"

TYPE_REFUSED = f"Type de fichier non autorisé. Formats acceptés : {FORMATS_LABEL}."
SIZE_REFUSED = f"Chaque fichier doit faire entre 1 octet et {MAX_SIZE_LABEL}."
CAP_REACHED = (
    f"Ce contact détient déjà {MAX_PER_CONTACT} preuves d'identité : "
    "supprimez-en avant d'en verser d'autres."
)
CONTACT_NOT_FOUND = partie_model.PARTIE_NOT_FOUND
REQUEST_INVALID = "Requête invalide."
STAGED_MISSING = "Fichier téléversé introuvable. Réessayez."
STAGED_READ_FAILED = "Lecture du fichier téléversé impossible. Réessayez."
CONTENT_MISMATCH = "Le contenu du fichier ne correspond pas à son extension."
SESSION_FAILED = "Erreur lors de l'ouverture du téléversement. Réessayez."
FILING_FAILED = "Erreur lors du versement. Veuillez réessayer."
FILING_UNCERTAIN = (
    "Le versement n'a pas pu être confirmé : rechargez la fiche ; si la "
    "preuve n'y figure pas, téléversez-la de nouveau."
)
OCCUPIED = (
    "Un autre fichier occupe déjà l'emplacement de cette preuve : rien n'a "
    "été versé. Téléversez le fichier de nouveau."
)
CONFLICT = "Une autre preuve porte déjà cet identifiant : rien n'a été versé."
PROOF_NOT_FOUND = "Preuve d'identité introuvable."
VERSION_REQUIRED = (
    "La version de la preuve est requise : rechargez la page, puis "
    "recommencez."
)
PROOF_CHANGED = (
    "Cette preuve a changé depuis l'affichage — rien n'a été modifié. "
    "Rechargez la page, puis recommencez."
)
RENAME_FAILED = (
    "Le nom n'a pas pu être enregistré : rien n'a été modifié. Réessayez."
)
RENAME_UNCERTAIN = (
    "Le nouveau nom n'a pas pu être confirmé : rechargez la page pour le "
    "vérifier."
)
DELETE_NOT_FOUND = (
    "Cette preuve d'identité ne figure plus à la fiche : elle a peut-être "
    "déjà été supprimée."
)
ALREADY_DELETED = "Cette preuve d'identité était déjà supprimée."
ERASE_FAILED = (
    "Le fichier n'a pas pu être effacé : rien n'a été supprimé. Réessayez."
)
DELETE_UNCERTAIN = (
    "La suppression n'a peut-être pas abouti — rechargez la page pour le "
    "vérifier."
)
DELETE_INCOMPLETE = (
    "Le fichier est effacé, mais son entrée n'a pas pu être retirée de la "
    "liste : cliquez de nouveau sur « Supprimer » pour terminer."
)
PATH_REFUSED = (
    "L'emplacement enregistré du fichier de cette preuve est invalide : "
    "rien n'a été fait."
)
OBJECT_MISSING = (
    "Le fichier de cette preuve est introuvable : supprimez-la, puis "
    "versez-la de nouveau."
)
CONSULT_FAILED = "Consultation impossible pour le moment — réessayez."
UPLOAD_HINT = f"{FORMATS_LABEL} — {MAX_SIZE_LABEL} au plus par fichier."
TRUNCATED_NOTICE = (
    f"Liste tronquée : seules les {MAX_PER_CONTACT} premières preuves sont "
    "affichées."
)
# A proof's NEUTRAL name, wherever its own name must not appear: the title of
# its deletion-trail row (D6 — ``audit_events`` is write-once, and a name
# typed by hand, an ID number in it, must not outlive the erased file), and
# the file name « Consulter » serves when a stored name is unusable.
NEUTRAL_NAME = "Preuve d'identité"


# ── Results and exceptions ───────────────────────────────────────────────


class Listing(NamedTuple):
    """A contact's proofs, oldest first, at most MAX_PER_CONTACT of them."""

    proofs: list
    truncated: bool


class Outcome(NamedTuple):
    """A writer's answer: ``(doc, errors, code)`` — see the docstring."""

    doc: Optional[dict]
    errors: list
    code: str


class StoredPathRefused(Exception):
    """A proof record's ``storage_path`` is not the shape this module builds:
    no Storage verb may be pointed at it. Takes no message."""


class ObjectMissing(Exception):
    """A proof's record exists, its object does not. Takes no message."""


class _Refused(Exception):
    """Raised inside a transactional body: nothing is written."""

    def __init__(self, errors: list[str], code: str):
        super().__init__("refused")
        self.errors = list(errors)
        self.code = code


class _Exists(Exception):
    """The reserved record already exists (read inside the transaction)."""

    def __init__(self, existing: dict):
        super().__init__("exists")
        self.existing = existing


class _Gone(Exception):
    """The record to delete is already absent (read inside the transaction)."""


# ── Pure checks ──────────────────────────────────────────────────────────


def file_extension(filename: object) -> str:
    """The extension of *filename*, LOWER-CASED with its dot; ``''`` when it
    has none (or is not a string)."""
    if not isinstance(filename, str) or "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[1].lower()


def default_display_name(filename: object) -> str:
    """The display name a filed proof starts with: the file name's stem."""
    if not isinstance(filename, str):
        return ""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return stem.strip()


def _has_control_char(value: str) -> bool:
    """C0, DEL and C1 (Unicode category Cc) — a superset of
    ``dav_ids.has_control_char``: a display name travels into a
    ``Content-Disposition`` header, whose callers must never hand it one."""
    return any(unicodedata.category(ch) == "Cc" for ch in value)


def display_name_errors(
    value: object, *, label: str = DISPLAY_NAME_LABEL
) -> list[str]:
    """The French refusal of *value* as a display name — ``[]`` when it may
    be stored EXACTLY as given. Refused, never altered: an empty name, one
    over :data:`DISPLAY_NAME_MAX` characters, a control character, or
    anything ``security.sanitize`` would strip (a run between chevrons). A
    no-break space is allowed: French typography uses it."""
    if not isinstance(value, str) or not value.strip():
        return [f"{label} : requis."]
    if len(value) > DISPLAY_NAME_MAX:
        return [f"{label} : {DISPLAY_NAME_MAX} caractères au plus."]
    if _has_control_char(value):
        return [f"{label} : caractères de contrôle refusés."]
    if sanitize(value, max_length=DISPLAY_NAME_MAX) != value:
        return [document_model.TEXT_CHEVRONS_ERROR.format(label=label)]
    return []


def file_name_errors(filename: object) -> list[str]:
    """The French refusal of a browser's file name — ``[]`` when a proof may
    be filed under it: an allowed extension (compared LOWER-CASED), no
    control character anywhere, and a stem that is a valid display name."""
    if not isinstance(filename, str) or not filename.strip():
        return [f"{FILE_NAME_LABEL} : requis."]
    if file_extension(filename) not in MIME_TYPES:
        return [TYPE_REFUSED]
    if _has_control_char(filename):
        return [f"{FILE_NAME_LABEL} : caractères de contrôle refusés."]
    return display_name_errors(default_display_name(filename),
                               label=FILE_NAME_LABEL)


def staging_object(uid: object, proof_id: str, ext: str) -> str:
    """``staging/{uid}/identite/{proof_id}/upload{ext}`` — the ONE builder.

    *ext* must already be lower-case (:func:`file_extension`). Raises
    ``storage_identity.InvalidStorageUid`` for a uid that cannot name a
    prefix, ``ValueError`` for an id or an extension this module never
    mints."""
    uid = storage_identity.require_uid(uid)
    if not document_model.is_canonical_uuid4(proof_id) or ext not in MIME_TYPES:
        raise ValueError("not a proof's staging object")
    return f"staging/{uid}/{STAGING_SEGMENT}/{proof_id}/{STAGING_STEM}{ext}"


def storage_path(uid: object, partie_id: str, proof_id: str, ext: str) -> str:
    """``users/{uid}/parties/{partie_id}/pieces_identite/{proof_id}{ext}`` —
    the ONE builder of a proof's object name (ids only, no file name).
    Raises like :func:`staging_object`."""
    uid = storage_identity.require_uid(uid)
    if (not document_model.is_addressable_id(partie_id)
            or not document_model.is_canonical_uuid4(proof_id)
            or ext not in MIME_TYPES):
        raise ValueError("not a proof's object")
    return f"users/{uid}/parties/{partie_id}/{SUBCOLLECTION}/{proof_id}{ext}"


def parse_staging_object(objet: object, uid: object) -> Optional[tuple[str, str]]:
    """``(proof_id, ext)`` when *objet* is EXACTLY a proof's staging name
    under the REQUEST's uid — ``staging/{uid}/identite/{uuid4}/upload{ext}``,
    a canonical UUIDv4 and a lower-case allowed extension — else ``None``.

    Every other shape is refused before any Storage verb: the documents'
    and receipts' (four segments), the connector's (``…/mcp/…``), the zip
    exports' (``…/exports/…``), another uid's, an upper-case extension."""
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


def checked_path(proof: object, partie_id: str) -> str:
    """The stored object path of *proof*, re-checked against the ONE shape
    :func:`storage_path` builds, before any Storage verb points at it — a
    record whose path was altered must never aim a delete (or a signed URL)
    at another object. The uid segment is validated by
    ``storage_identity.require_uid`` and NEVER compared with the session's:
    a changed Firebase uid must not make every proof undeletable. Then the
    contact (*partie_id*, the record's parent), the proof's own id and the
    extension. Raises :class:`StoredPathRefused`."""
    record = proof if isinstance(proof, dict) else {}
    path = record.get("storage_path")
    proof_id = record.get("id")
    if not isinstance(path, str) or not isinstance(proof_id, str):
        raise StoredPathRefused()
    parts = path.split("/")
    if len(parts) != 6 or not parts[5].startswith(proof_id):
        raise StoredPathRefused()
    try:
        expected = storage_path(parts[1], partie_id, proof_id,
                                parts[5][len(proof_id):])
    except (storage_identity.StorageIdentityUnavailable, ValueError):
        raise StoredPathRefused() from None
    if path != expected:
        raise StoredPathRefused()
    return path


def _ext_of_path(path: str) -> str:
    """The extension of a path :func:`checked_path` accepted."""
    return "." + path.rsplit(".", 1)[1]


def _consult_filename(proof: dict, ext: str) -> str:
    """The file name « Consulter » serves: the display name, plus *ext*
    unless it already ends in an extension of the same type. A stored name
    the rules refuse (a hand edit) is never put in a header: a neutral name
    replaces it."""
    name = (proof or {}).get("display_name")
    if not isinstance(name, str) or display_name_errors(name):
        name = NEUTRAL_NAME
    if MIME_TYPES.get(file_extension(name)) == MIME_TYPES[ext]:
        return name
    return name + ext


# ── Readers ──────────────────────────────────────────────────────────────


def _collection(partie_id: str):
    return (db.collection(partie_model.COLLECTION).document(partie_id)
            .collection(SUBCOLLECTION))


def _commit_scope(partie_id: str) -> str:
    return f"{partie_model.COLLECTION}/{partie_id}/{SUBCOLLECTION}"


def _row(snap) -> dict:
    """A stored proof; its document id is its identity."""
    return {**(snap.to_dict() or {}), "id": snap.id}


def list_proofs_strict(partie_id: str) -> Listing:
    """The contact's proofs, oldest first; a read failure RAISES.

    Reads ``MAX_PER_CONTACT + 1`` so a list that exceeds the cap (a legacy
    or forged record) says it is ``truncated`` rather than passing for
    complete. An id that cannot name a contact holds nothing (no query)."""
    if not document_model.is_addressable_id(partie_id):
        return Listing([], False)
    query = (_collection(partie_id).order_by("created_at")
             .limit(MAX_PER_CONTACT + 1))
    rows = [_row(snap) for snap in query.stream()]
    return Listing(rows[:MAX_PER_CONTACT], len(rows) > MAX_PER_CONTACT)


def list_proofs(partie_id: str) -> Optional[Listing]:
    """:func:`list_proofs_strict`, fail-open for DISPLAY ONLY: ``None`` when
    the read failed — never an empty list a page would show as « aucune »."""
    try:
        return list_proofs_strict(partie_id)
    except Exception as exc:
        logger.warning("id proof list read failed: %s", type(exc).__name__)
        return None


def has_proofs_strict(partie_id: str, *, transaction=None) -> bool:
    """Whether the contact holds at least one proof; a read failure RAISES.

    *transaction* makes the read part of the caller's transaction
    (``partie.delete_partie``), so a proof filed meanwhile aborts its
    commit."""
    if not document_model.is_addressable_id(partie_id):
        return False
    query = _collection(partie_id).limit(1)
    return bool(list(query.stream(transaction=transaction)))


def get_proof_strict(partie_id: str, proof_id: str) -> Optional[dict]:
    """One proof, ``None`` when it does not exist — or when an id cannot
    name one (an unaddressable contact id, a proof id that is not a
    canonical UUIDv4). A read failure RAISES."""
    if (not document_model.is_addressable_id(partie_id)
            or not document_model.is_canonical_uuid4(proof_id)):
        return None
    snap = _collection(partie_id).document(proof_id).get()
    return _row(snap) if snap.exists else None


def _count_strict(partie_id: str) -> int:
    """How many proofs the contact holds, counted up to the cap."""
    return sum(1 for _ in _collection(partie_id).limit(MAX_PER_CONTACT).stream())


# ── Storage helpers ──────────────────────────────────────────────────────


def _error_type(exc: BaseException) -> str:
    return type(exc).__name__


def _erase_generation(path: str, generation: Optional[int], ids: dict) -> None:
    """Erase *path* at *generation* — the one THIS call created — for good.

    ``None`` (an adopted object, or nothing written) erases nothing. The
    reload loads the live generation, which the delete then carries
    (``Blob.delete`` → ``generation=self.generation``): nothing survives as
    a non-current version. ``if_generation_match`` keeps the delete on THIS
    call's generation: a live object of another generation is not this
    call's, and the service refuses (412) to touch it."""
    if generation is None:
        return
    try:
        blob = storage.bucket().blob(path)
        blob.reload()
        blob.delete(if_generation_match=generation)
    except (NotFound, PreconditionFailed):
        # Gone already, or no longer this call's generation: nothing of
        # this call's is left to erase.
        return
    except Exception as exc:
        log_unexpected("id proof: rollback erase failed", exc_info=False,
                       error_type=_error_type(exc), **ids)


def _consume_staging(blob) -> None:
    """Delete the staging object at the generation its reload loaded.
    Best-effort: a failure is a warning, and the bucket's ``staging/``
    7-day lifecycle rule sweeps what is left."""
    try:
        if blob.generation is None:
            blob.reload()
        blob.delete(if_generation_match=blob.generation)
    except NotFound:
        return
    except Exception as exc:
        logger.warning("id proof: staging cleanup failed: %s",
                       type(exc).__name__)


# ── Writers ──────────────────────────────────────────────────────────────


def _ids(partie_id: object, proof_id: object = None) -> dict:
    ids = {"partie_id": sanitize_log_value(partie_id)}
    if proof_id is not None:
        ids["proof_id"] = sanitize_log_value(proof_id)
    return ids


def _contact_refusal(partie_id: str, ids: dict) -> Optional[Outcome]:
    """``None`` when the contact exists; the refusal otherwise. Strict: a
    failed read is ``indisponible``, never « Contact introuvable »."""
    if not document_model.is_addressable_id(partie_id):
        return Outcome(None, [CONTACT_NOT_FOUND], CODE_REFUS)
    try:
        contact = partie_model.get_partie_strict(partie_id)
    except Exception as exc:
        log_unexpected("id proof: contact unreadable", exc_info=False,
                       error_type=_error_type(exc), **ids)
        return Outcome(None, [concurrency.READ_UNAVAILABLE_ERROR],
                       CODE_INDISPONIBLE)
    if contact is None:
        return Outcome(None, [CONTACT_NOT_FOUND], CODE_REFUS)
    return None


def _uid_refusal(value: object) -> tuple[Optional[str], Optional[Outcome]]:
    """``(uid, None)``, or ``(None, refusal)`` when *value* cannot name a
    Storage prefix — the sentence chosen by type, never read off the error."""
    try:
        return storage_identity.require_uid(value), None
    except storage_identity.StorageIdentityUnavailable as exc:
        return None, Outcome(None, [storage_identity.public_message(exc)],
                             CODE_INDISPONIBLE)


def open_proof_upload(
    partie_id: str,
    filename: str,
    size: int,
    user_id: str,
    *,
    origin: str,
) -> Outcome:
    """Open the browser's direct upload of one proof — every check BEFORE a
    byte moves.

    *size* is the declared byte count (an ``int``); *origin* the page's
    origin, the session's CORS policy (the route forces https in
    production). In order: the uid (``require_uid``), the file name and its
    lower-cased extension, the size (1 to :data:`MAX_SIZE`), the contact
    (strict read) and the cap. Then the proof's id is reserved and a
    resumable session is opened on its staging name: ``size=`` (GCS refuses
    any byte beyond it), ``if_generation_match=0`` (create-only), and the
    whitelist's content type, never the browser's.

    ``doc`` is ``{"url", "objet", "proof_id"}`` on :data:`CODE_OK`. ``url``
    is a write capability: never log, persist or render it but to the
    uploading script. Codes: :data:`SESSION_CODES`."""
    uid, refused = _uid_refusal(user_id)
    if refused is not None:
        return refused
    errors = file_name_errors(filename)
    if errors:
        return Outcome(None, errors, CODE_REFUS)
    if (not isinstance(size, int) or isinstance(size, bool)
            or not 1 <= size <= MAX_SIZE):
        return Outcome(None, [SIZE_REFUSED], CODE_REFUS)
    ids = _ids(partie_id)
    refused = _contact_refusal(partie_id, ids)
    if refused is not None:
        return refused
    try:
        held = _count_strict(partie_id)
    except Exception as exc:
        log_unexpected("id proof: count unreadable", exc_info=False,
                       error_type=_error_type(exc), **ids)
        return Outcome(None, [concurrency.READ_UNAVAILABLE_ERROR],
                       CODE_INDISPONIBLE)
    if held >= MAX_PER_CONTACT:
        return Outcome(None, [CAP_REACHED], CODE_REFUS)

    ext = file_extension(filename)
    proof_id = str(uuid.uuid4())
    objet = staging_object(uid, proof_id, ext)
    ids["proof_id"] = proof_id
    try:
        url = storage.bucket().blob(objet).create_resumable_upload_session(
            content_type=MIME_TYPES[ext], size=size, origin=origin,
            if_generation_match=0,
        )
    except Exception as exc:
        # The class only: the session URI is a capability, and the
        # service's text is not needed.
        log_unexpected("id proof: session open failed", exc_info=False,
                       error_type=_error_type(exc), **ids)
        return Outcome(None, [SESSION_FAILED], CODE_INDISPONIBLE)
    if not isinstance(url, str) or not url.startswith("https://"):
        log_unexpected("id proof: session open failed", exc_info=False,
                       error_type="not_https", **ids)
        return Outcome(None, [SESSION_FAILED], CODE_INDISPONIBLE)
    return Outcome({"url": url, "objet": objet, "proof_id": proof_id}, [],
                   CODE_OK)


def _same_proof(existing: dict, partie_id: str, size: int, path: str) -> bool:
    """True when a stored record IS this upload's proof."""
    return (existing.get("partie_id") == partie_id
            and existing.get("file_size") == size
            and existing.get("storage_path") == path)


def _answer_existing(existing: dict, partie_id: str, size: int, path: str,
                     generation: Optional[int], ids: dict, *,
                     landed: bool = False) -> Outcome:
    """The answer when a record already holds the reserved id: this
    upload's proof (``rejoue``; ``verse`` when it is this call's own commit,
    landed with its answer lost) or a conflict. This call's own object is
    erased only when no record points at it."""
    if _same_proof(existing, partie_id, size, path):
        provenance.note_commit(_commit_scope(partie_id), existing.get("id") or "")
        return Outcome(existing, [], CODE_VERSE if landed else CODE_REJOUE)
    if existing.get("storage_path") != path:
        _erase_generation(path, generation, ids)
    return Outcome(None, [CONFLICT], CODE_REFUS)


def _after_object_failure(ref, path: str, generation: Optional[int],
                          partie_id: str, size: int, ids: dict) -> Outcome:
    """The copy or its patch FAILED. A parallel finalization of the same
    upload may have adopted this call's object and committed its record:
    its record is then the answer, and the object stays. Otherwise this
    call's own generation (never an adopted one) is erased. An unreadable
    record keeps the object: never erase what a record may reference."""
    try:
        snap = ref.get()
    except Exception as exc:
        log_unexpected("id proof: rollback check unreadable", exc_info=False,
                       error_type=_error_type(exc), **ids)
        return Outcome(None, [FILING_FAILED], CODE_INDISPONIBLE)
    if snap.exists:
        return _answer_existing(_row(snap), partie_id, size, path,
                                generation, ids)
    _erase_generation(path, generation, ids)
    return Outcome(None, [FILING_FAILED], CODE_INDISPONIBLE)


def file_staged_proof(
    objet: str,
    partie_id: str,
    filename: str,
    user_id: str,
) -> Outcome:
    """File the uploaded staging object *objet* as a proof of *partie_id*.

    The ONE door every Storage verb of a filing goes through. In order:

    1. the shape — :func:`parse_staging_object` under the request's uid; a
       foreign shape is ``forme`` and nothing is touched. From here on, a
       ``finally`` CONSUMES the staging object, even when the name is
       refused next;
    2. the name — its lower-cased extension must be the staging name's
       (else ``forme``), and :func:`file_name_errors` must pass;
    3. the staging object is reloaded — ``NotFound`` is a replay, answered
       from the reserved record (``rejoue``) or refused;
    4. its REAL size (1 to :data:`MAX_SIZE`);
    5. the contact (strict read);
    6. a 512-byte sniff (``document._sniff_header``) must give the
       extension's own type;
    7. the reserved record — already there is a replay (``rejoue``) or a
       conflict;
    8. a GCS-side copy, create-only (``if_generation_match=0``); a 412 means
       the object exists: identical bytes are ADOPTED (and then never
       erased by this call — named once, « id proof: adopted object
       left », when the call then files nothing), anything else, or an
       object that cannot be reloaded, is refused (« id proof: object
       occupied »);
    9. the object's content type is patched to the sniffed one;
    10. a transaction re-reads the contact and the reserved record, re-counts
        the cap and ``create()``s the record.

    A commit that RAISED is settled with ``concurrency.settle_failed_create``:
    landed → ``verse``; absent → this call's own generation is erased and
    the answer is ``indisponible`` (retry); unknown → BOTH objects are kept
    and the answer is ``incertain`` (module docstring). Codes:
    :data:`FILING_CODES`; ``doc`` is the record on ``verse``/``rejoue``.
    """
    uid, refused = _uid_refusal(user_id)
    if refused is not None:
        return refused
    parsed = parse_staging_object(objet, uid)
    if parsed is None:
        return Outcome(None, [REQUEST_INVALID], CODE_FORME)
    proof_id, ext = parsed
    ids = _ids(partie_id, proof_id)
    try:
        staging = storage.bucket().blob(objet)
    except Exception as exc:
        log_unexpected("id proof: staging reload failed", exc_info=False,
                       error_type=_error_type(exc), **ids)
        return Outcome(None, [STAGED_READ_FAILED], CODE_INDISPONIBLE)
    keep = {"staging": False, "adopted": False}
    outcome: Optional[Outcome] = None
    try:
        outcome = _file(staging, uid, proof_id, ext, partie_id, filename,
                        ids, keep)
        return outcome
    finally:
        if keep["adopted"] and (outcome is None or outcome.code not in (
                CODE_VERSE, CODE_REJOUE, CODE_INCERTAIN)):
            # The adopted object is never erased (it may be another call's)
            # and this call filed nothing: named ONCE, whichever exit —
            # a refusal, a commit settled absent, a failed read. A record
            # points at it on verse/rejoue; incertain has its own line.
            log_unexpected("id proof: adopted object left", exc_info=False,
                           **ids)
        if not keep["staging"]:
            _consume_staging(staging)


def _file(staging, uid: str, proof_id: str, ext: str, partie_id: str,
          filename: str, ids: dict, keep: dict) -> Outcome:
    """Steps 2-10 of :func:`file_staged_proof`. Its ``finally`` consumes the
    staging object unless ``keep["staging"]`` is set, and names the object
    ``keep["adopted"]`` marks when the call files nothing."""
    if file_extension(filename) != ext:
        return Outcome(None, [REQUEST_INVALID], CODE_FORME)
    errors = file_name_errors(filename)
    if errors:
        return Outcome(None, errors, CODE_REFUS)

    try:
        staging.reload()
    except NotFound:
        keep["staging"] = True      # nothing left to consume
        return _replayed(uid, partie_id, proof_id, ext, ids)
    except Exception as exc:
        log_unexpected("id proof: staging reload failed", exc_info=False,
                       error_type=_error_type(exc), **ids)
        return Outcome(None, [STAGED_READ_FAILED], CODE_INDISPONIBLE)

    try:
        size = int(staging.size)
    except (TypeError, ValueError):
        size = -1
    if not 1 <= size <= MAX_SIZE:
        return Outcome(None, [SIZE_REFUSED], CODE_REFUS)

    refused = _contact_refusal(partie_id, ids)
    if refused is not None:
        return refused

    try:
        header = staging.download_as_bytes(
            start=0, end=document_model._SNIFF_PROBE_BYTES - 1,
            if_generation_match=staging.generation,
        )
    except Exception as exc:
        log_unexpected("id proof: staged read failed", exc_info=False,
                       error_type=_error_type(exc), **ids)
        return Outcome(None, [STAGED_READ_FAILED], CODE_INDISPONIBLE)
    sniffed = document_model._sniff_header(header, ext)
    if sniffed != MIME_TYPES[ext]:
        return Outcome(None, [CONTENT_MISMATCH], CODE_REFUS)

    path = storage_path(uid, partie_id, proof_id, ext)
    ref = _collection(partie_id).document(proof_id)
    try:
        snap = ref.get()
    except Exception as exc:
        log_unexpected("id proof: reserved record unreadable", exc_info=False,
                       error_type=_error_type(exc), **ids)
        return Outcome(None, [concurrency.READ_UNAVAILABLE_ERROR],
                       CODE_INDISPONIBLE)
    if snap.exists:
        return _answer_existing(_row(snap), partie_id, size, path, None, ids)

    created_generation: Optional[int] = None
    try:
        dest = storage.bucket().blob(path)
        try:
            token, _, _ = dest.rewrite(staging, if_generation_match=0)
            while token is not None:
                token, _, _ = dest.rewrite(staging, token=token,
                                           if_generation_match=0)
        except PreconditionFailed:
            # The object exists, and only this upload's finalizations write
            # its reserved path: a parallel one, the « incertain »
            # predecessor (which kept its copy), or this very call — the
            # library retries a rewrite sent with ifGenerationMatch=0, so a
            # lost answer comes back as a 412 on the object its first
            # attempt created. It is ADOPTED only when it holds these very
            # bytes, and an adopted object is never erased by this call
            # (``created_generation`` stays None). Whether a record already
            # points at it, the create transaction reads below.
            if not document_model._same_object(dest, staging):
                # Other bytes, or a reload that failed: refused, and the
                # occupant — a copy this upload left — is named.
                log_unexpected("id proof: object occupied", exc_info=False,
                               **ids)
                return Outcome(None, [OCCUPIED], CODE_REFUS)
            keep["adopted"] = True
        else:
            created_generation = dest.generation
        dest.content_type = sniffed
        dest.patch(if_generation_match=dest.generation)
    except Exception as exc:
        log_unexpected("id proof: object write failed", exc_info=False,
                       error_type=_error_type(exc), **ids)
        return _after_object_failure(ref, path, created_generation,
                                     partie_id, size, ids)

    record = {
        "id": proof_id,
        "partie_id": partie_id,
        "display_name": default_display_name(filename),
        "file_type": sniffed,
        "file_size": size,
        "storage_path": path,
    }
    provenance.stamp_create(record, datetime.now(timezone.utc))
    contact_ref = db.collection(partie_model.COLLECTION).document(partie_id)
    staged: dict = {}

    @firestore.transactional
    def _create(transaction) -> None:
        staged.clear()
        if not contact_ref.get(transaction=transaction).exists:
            raise _Refused([CONTACT_NOT_FOUND], CODE_REFUS)
        current = ref.get(transaction=transaction)
        if current.exists:
            raise _Exists(_row(current))
        held = sum(1 for _ in _collection(partie_id).limit(MAX_PER_CONTACT)
                   .stream(transaction=transaction))
        if held >= MAX_PER_CONTACT:
            raise _Refused([CAP_REACHED], CODE_REFUS)
        transaction.create(ref, record)
        staged["record"] = True

    try:
        _create(db.transaction())
    except _Refused as refusal:
        _erase_unreferenced(ref, path, created_generation, ids)
        return Outcome(None, refusal.errors, refusal.code)
    except _Exists as exists:
        return _answer_existing(exists.existing, partie_id, size, path,
                                created_generation, ids)
    except AlreadyExists:
        return _after_object_failure(ref, path, created_generation,
                                     partie_id, size, ids)
    except Exception as exc:
        log_unexpected("id proof: record write failed", exc_info=False,
                       error_type=_error_type(exc), **ids)
        if not staged:
            # The body failed before staging the create: nothing was
            # written by this call.
            _erase_unreferenced(ref, path, created_generation, ids)
            return Outcome(None, [FILING_FAILED], CODE_INDISPONIBLE)
        outcome, stored = concurrency.settle_failed_create(ref)
        if outcome == concurrency.WRITE_LANDED:
            return _answer_existing({**(stored or {}), "id": proof_id},
                                    partie_id, size, path,
                                    created_generation, ids, landed=True)
        if outcome == concurrency.WRITE_ABSENT:
            _erase_generation(path, created_generation, ids)
            return Outcome(None, [FILING_FAILED], CODE_INDISPONIBLE)
        keep["staging"] = True
        log_unexpected("id proof: filing uncertain", exc_info=False, **ids)
        return Outcome(None, [FILING_UNCERTAIN], CODE_INCERTAIN)

    provenance.note_commit(_commit_scope(partie_id), proof_id)
    return Outcome(record, [], CODE_VERSE)


def _erase_unreferenced(ref, path: str, generation: Optional[int],
                        ids: dict) -> None:
    """:func:`_erase_generation`, unless a committed record points at the
    object (a parallel finalization that adopted it). An unreadable record
    keeps the object."""
    if generation is None:
        return
    try:
        snap = ref.get()
    except Exception as exc:
        log_unexpected("id proof: rollback check unreadable", exc_info=False,
                       error_type=_error_type(exc), **ids)
        return
    if snap.exists and (snap.to_dict() or {}).get("storage_path") == path:
        return
    _erase_generation(path, generation, ids)


def _replayed(uid: str, partie_id: str, proof_id: str, ext: str,
              ids: dict) -> Outcome:
    """The staging object is gone: a finalization arriving AGAIN after the
    first one consumed it. The reserved record says whether it landed."""
    try:
        existing = get_proof_strict(partie_id, proof_id)
    except Exception as exc:
        log_unexpected("id proof: reserved record unreadable", exc_info=False,
                       error_type=_error_type(exc), **ids)
        return Outcome(None, [concurrency.READ_UNAVAILABLE_ERROR],
                       CODE_INDISPONIBLE)
    if (existing is not None and existing.get("partie_id") == partie_id
            and existing.get("storage_path")
            == storage_path(uid, partie_id, proof_id, ext)):
        provenance.note_commit(_commit_scope(partie_id), proof_id)
        return Outcome(existing, [], CODE_REJOUE)
    return Outcome(None, [STAGED_MISSING], CODE_REFUS)


def rename_proof(
    partie_id: str,
    proof_id: str,
    display_name: str,
    *,
    expected_etag: Optional[str],
) -> Outcome:
    """Rename one proof — compare-and-set on the etag the page rendered.

    *display_name* is judged EXACTLY as it will be stored (the route trims
    the form's value). ``expected_etag`` is required (a new form: there is
    no legacy path). A name identical to the stored one writes nothing
    (``inchange``). The write is a partial update of ``display_name`` and
    the stamps only (``concurrency.commit_fields`` + ``provenance.
    update_fields``); a commit that raised is settled by re-reading the
    etag it carried. ``doc`` is the WRITTEN row on ``modifie`` (the stored
    one on ``inchange``). Codes: :data:`RENAME_CODES`."""
    if expected_etag is None:
        return Outcome(None, [VERSION_REQUIRED], CODE_VERSION)
    ids = _ids(partie_id, proof_id)
    try:
        existing = get_proof_strict(partie_id, proof_id)
    except Exception as exc:
        log_unexpected("id proof: read failed", exc_info=False,
                       error_type=_error_type(exc), **ids)
        return Outcome(None, [concurrency.READ_UNAVAILABLE_ERROR],
                       CODE_INDISPONIBLE)
    if existing is None:
        return Outcome(None, [PROOF_NOT_FOUND], CODE_INTROUVABLE)
    if not concurrency.matches(existing, expected_etag):
        return Outcome(None, [PROOF_CHANGED], CODE_VERSION)
    errors = display_name_errors(display_name)
    if errors:
        return Outcome(None, errors, CODE_REFUS)
    if display_name == existing.get("display_name"):
        return Outcome(existing, [], CODE_INCHANGE)

    fields = {"display_name": display_name,
              **provenance.update_fields(datetime.now(timezone.utc))}
    ref = _collection(partie_id).document(proof_id)
    try:
        concurrency.commit_fields(
            ref, fields, expected_etag=str(expected_etag),
            read_etag=concurrency.etag_of(existing),
        )
    except concurrency.StaleWrite:
        return Outcome(None, [PROOF_CHANGED], CODE_VERSION)
    except concurrency.Vanished:
        return Outcome(None, [PROOF_NOT_FOUND], CODE_INTROUVABLE)
    except Exception as exc:
        log_unexpected("id proof: write failed", exc_info=False,
                       error_type=_error_type(exc), **ids)
        # A commit that raised may have landed: the fresh etag it carried
        # says whether it did.
        try:
            snap = ref.get()
        except Exception:
            return Outcome(None, [RENAME_UNCERTAIN], CODE_INCERTAIN)
        if not (snap.exists and concurrency.etag_of(snap.to_dict())
                == concurrency.etag_of(fields)):
            return Outcome(None, [RENAME_FAILED], CODE_INDISPONIBLE)
    provenance.note_commit(_commit_scope(partie_id), proof_id)
    return Outcome({**existing, **fields}, [], CODE_MODIFIE)


def _erase_live(path: str, ids: dict) -> str:
    """Erase the object at *path*, whatever its generation — the one its
    own reload loads, so the delete is permanent. ``"gone"`` (erased now,
    or already absent), ``"kept"`` (nothing was erased) or ``"unknown"``
    (the delete raised and the reload that would tell raised too)."""
    try:
        blob = storage.bucket().blob(path)
        blob.reload()
    except NotFound:
        return "gone"
    except Exception as exc:
        log_unexpected("id proof: erase failed", exc_info=False,
                       error_type=_error_type(exc), **ids)
        return "kept"
    try:
        blob.delete(if_generation_match=blob.generation)
        return "gone"
    except NotFound:
        return "gone"
    except Exception as exc:
        log_unexpected("id proof: erase failed", exc_info=False,
                       error_type=_error_type(exc), **ids)
    try:
        storage.bucket().blob(path).reload()
    except NotFound:
        return "gone"
    except Exception:
        return "unknown"
    return "kept"


def delete_proof(
    partie_id: str,
    proof_id: str,
    *,
    expected_etag: Optional[str],
) -> Outcome:
    """Delete one proof for good (D6): its object, then its record.

    ``expected_etag`` is required, and it guards the ERASE, not the record
    delete (a declared exception to Rule 7): it is compared on the strict
    read made just before the erase, since a GCS erase cannot join a
    Firestore transaction, while the record delete checks presence only, on
    purpose — refusing it once the file is erased would leave a listed
    proof with no file. A rename landing in that window is deleted with the
    proof (same id, same bytes, neutral trail row).

    The record is read strictly and its stored path re-checked
    (:func:`checked_path`); the live object is then erased BY GENERATION
    (``fichier`` when it could not be: nothing was deleted; ``incertain``
    when that cannot be told), and only then is the record deleted, EXACTLY
    ONCE, in a transaction that reads it: present → deleted, ``supprime``;
    absent → ``deja``. A commit that raised is settled by re-reading:
    absent after this call staged the delete → ``supprime``; still present
    → ``incomplet`` (the object is gone, the next call completes it);
    unreadable → ``incertain``. A failure line is logged BEFORE the read
    that settles it, so it can precede a success: « id proof: erase
    failed » precedes ``fichier`` or ``incertain`` — or, when the follow-up
    reload finds the object gone (the delete landed, its answer lost), the
    record step and its ``supprime`` —, and « id proof: record delete
    failed after erase » precedes ``supprime``, ``deja``, ``incomplet`` or
    ``incertain``.

    Only ``supprime`` means THIS call removed the record: the caller writes
    the one ``audit_events`` row and the one event then, and never on
    ``deja``. ``doc`` is the deleted record on ``supprime``. Codes:
    :data:`DELETE_CODES`."""
    if expected_etag is None:
        return Outcome(None, [VERSION_REQUIRED], CODE_VERSION)
    ids = _ids(partie_id, proof_id)
    try:
        existing = get_proof_strict(partie_id, proof_id)
    except Exception as exc:
        log_unexpected("id proof: read failed", exc_info=False,
                       error_type=_error_type(exc), **ids)
        return Outcome(None, [concurrency.READ_UNAVAILABLE_ERROR],
                       CODE_INDISPONIBLE)
    if existing is None:
        return Outcome(None, [DELETE_NOT_FOUND], CODE_INTROUVABLE)
    if not concurrency.matches(existing, expected_etag):
        return Outcome(None, [PROOF_CHANGED], CODE_VERSION)
    try:
        path = checked_path(existing, partie_id)
    except StoredPathRefused:
        log_unexpected("id proof: stored path refused", exc_info=False, **ids)
        return Outcome(None, [PATH_REFUSED], CODE_CHEMIN)

    erased = _erase_live(path, ids)
    if erased == "kept":
        return Outcome(None, [ERASE_FAILED], CODE_FICHIER)
    if erased == "unknown":
        return Outcome(None, [DELETE_UNCERTAIN], CODE_INCERTAIN)

    ref = _collection(partie_id).document(proof_id)
    staged: dict = {}

    @firestore.transactional
    def _remove(transaction) -> None:
        staged.clear()
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise _Gone()
        transaction.delete(ref)
        staged["proof"] = _row(snap)

    try:
        _remove(db.transaction())
    except _Gone:
        return Outcome(None, [ALREADY_DELETED], CODE_DEJA)
    except Exception as exc:
        log_unexpected("id proof: record delete failed after erase",
                       exc_info=False, error_type=_error_type(exc), **ids)
        try:
            still = ref.get().exists
        except Exception:
            return Outcome(None, [DELETE_UNCERTAIN], CODE_INCERTAIN)
        if still:
            return Outcome(None, [DELETE_INCOMPLETE], CODE_INCOMPLET)
        if not staged:
            return Outcome(None, [ALREADY_DELETED], CODE_DEJA)
    provenance.note_commit(_commit_scope(partie_id), proof_id)
    return Outcome(staged.get("proof") or existing, [], CODE_SUPPRIME)


def consult_url(partie_id: str, proof: dict) -> str:
    """A 15-minute V4 signed URL that SHOWS *proof*'s file in the browser.

    The stored path is re-checked (:class:`StoredPathRefused`), the object
    reloaded (:class:`ObjectMissing` when it is gone), then signed with an
    inline ``Content-Disposition`` naming the display name (plus the
    extension) and the whitelist's content type. Any other failure — the
    reload, the signing — PROPAGATES: the caller logs it and answers in
    French (:data:`CONSULT_FAILED`). Calls the module-level
    ``sign_blob_url`` (imported from ``models.document``). Never log,
    persist or render the URL: redirect to it."""
    path = checked_path(proof, partie_id)
    ext = _ext_of_path(path)
    blob = storage.bucket().blob(path)
    try:
        blob.reload()
    except NotFound:
        raise ObjectMissing() from None
    params = {
        "response-content-disposition": document_model.content_disposition(
            _consult_filename(proof, ext), inline=True),
        "response-content-type": MIME_TYPES[ext],
    }
    return sign_blob_url(blob, params, expiry_minutes=SIGNED_URL_MINUTES)
