"""Courrier registry model — zero-PII Firestore state for Outlook mail intake.

Stores the processing state machine of emails handled by the Courrier MCP
module in the top-level ``courrier_messages/{cle}`` collection:

* ``inventorie`` — provisional triage/deferral marker carrying a 30-day
  ``expire_at`` TTL (written when a message is explicitly deferred or left in
  Inbox/SentItems; ``list_courriels`` is strictly read-only and never writes rows).
* ``enregistre`` — permanent record written atomically once the raw ``.eml``
  and attachments have been saved to OneDrive and ingested into Athena
  (``expire_at`` is ``None`` so Firestore TTL never collects it).
* ``classe`` — permanent record after Outlook filing under
  ``Archive > Dossiers > {Actifs, Suspendu, Services-conseils}``
  (``expire_at`` is ``None``).

Keyed-ID exception (Architecture Rule 6): documents are keyed by ``cle`` —
the first 16 lowercase hexadecimal characters of
``sha256(internetMessageId.strip())`` — rather than a random UUIDv4 so
historical ``legacy_ref`` links on ``timeentries`` (``recep:{dossier}:{date}:{cle}``)
stay intact across reads and retries.

Zero-PII invariant: ``courrier_messages`` stores opaque technical identifiers,
counts, SHA-256 digests, sizes, timestamps, and status tokens ONLY — never
subjects, email addresses, client filenames, or client-named paths.

Expiry is enforced in code on every read (:func:`is_expired`) regardless of
Firestore background TTL lag.
"""

from __future__ import annotations

import hashlib
import io
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Optional

from firebase_admin import storage
from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import firestore

from models import db, provenance
from models.record_ids import is_addressable_id
from utils import storage_identity
from utils.logging_setup import log_unexpected
from utils.quickxorhash import QuickXorHash

COLLECTION = "courrier_messages"

ETAT_INVENTORIE = "inventorie"
ETAT_ENREGISTRE = "enregistre"
ETAT_CLASSE = "classe"
VALID_ETATS: tuple[str, ...] = (
    ETAT_INVENTORIE,
    ETAT_ENREGISTRE,
    ETAT_CLASSE,
)

INVENTORIE_TTL_DAYS = 30
_GCS_STAGE_CHUNK_BYTES = 4 * 1024 * 1024

_CLE_RE = re.compile(r"\A[0-9a-f]{16}\Z")
_SHA256_OR_EMPTY_RE = re.compile(r"\A(?:[0-9a-f]{64})?\Z")
_DOSSIER_NUM_RE = re.compile(r"\A[0-9]{4}-[0-9]{3}\Z")
_SLOT_TOKEN_RE = re.compile(r"\A[a-z0-9_-]{1,32}\Z")
_EXT_RE = re.compile(r"\A\.[a-z0-9]{1,16}\Z")

_FICHIER_ALLOWED_KEYS = frozenset({
    "slot",
    "sha256",
    "taille",
    "onedrive_item_id",
    "document_id",
    "statut_onedrive",
    "statut_athena",
})

__all__ = [
    "COLLECTION",
    "ETAT_CLASSE",
    "ETAT_ENREGISTRE",
    "ETAT_INVENTORIE",
    "INVENTORIE_TTL_DAYS",
    "StagedCourrierBlob",
    "VALID_ETATS",
    "compute_cle",
    "courrier_staging_path",
    "discard_staged_courrier_blob",
    "get_courrier_message",
    "get_courrier_message_strict",
    "get_courrier_messages_map_strict",
    "is_expired",
    "is_valid_cle",
    "read_staged_courrier_slice",
    "record_classe",
    "record_enregistre",
    "record_inventorie",
    "reserved_courrier_document_id",
    "stage_courrier_bytes",
]


def _utc_now(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def compute_cle(internet_message_id: str) -> str:
    """Compute the 16-hex SHA-256 key from an RFC 5322 ``internetMessageId``."""
    cleaned = str(internet_message_id or "").strip()
    if not cleaned:
        raise ValueError("Identifiant de message vide.")
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]


def is_valid_cle(cle: Any) -> bool:
    """Return True when ``cle`` is a 16-char lowercase hexadecimal digest."""
    return isinstance(cle, str) and bool(_CLE_RE.match(cle))


def is_expired(doc: Optional[dict[str, Any]], *, now: Optional[datetime] = None) -> bool:
    """Return True when a registry row is missing or past its in-code expiry.

    Processed emails (``enregistre`` / ``classe``) are permanent and never
    expire. Unmatched ``inventorie`` rows fail closed if ``expire_at`` is
    missing or elapsed.
    """
    if doc is None:
        return True
    etat = doc.get("etat")
    if etat in (ETAT_ENREGISTRE, ETAT_CLASSE):
        return False
    if etat != ETAT_INVENTORIE:
        return True
    expire_at = doc.get("expire_at")
    if not isinstance(expire_at, datetime):
        return True
    if expire_at.tzinfo is None:
        expire_at = expire_at.replace(tzinfo=timezone.utc)
    else:
        expire_at = expire_at.astimezone(timezone.utc)
    return expire_at <= _utc_now(now)


def _base_doc(
    *,
    cle: str,
    graph_id: str,
    etat: str,
    expire_at: Optional[datetime],
    dossier_id: str = "",
    dossier_numero: str = "",
    fichiers: Optional[list[dict[str, Any]]] = None,
    legacy_ref: str = "",
    time_entry_id: str = "",
    outlook_folder_id: str = "",
    saved_at: Optional[datetime] = None,
    moved_at: Optional[datetime] = None,
) -> dict[str, Any]:
    return {
        "cle": cle,
        "graph_id": graph_id,
        "etat": etat,
        "dossier_id": dossier_id,
        "dossier_numero": dossier_numero,
        "fichiers": list(fichiers) if fichiers is not None else [],
        "legacy_ref": legacy_ref,
        "time_entry_id": time_entry_id,
        "outlook_folder_id": outlook_folder_id,
        "saved_at": saved_at,
        "moved_at": moved_at,
        "expire_at": expire_at,
    }


def _normalize_fichiers(
    fichiers: Any,
) -> tuple[Optional[list[dict[str, Any]]], list[str]]:
    if not isinstance(fichiers, list):
        return None, ["`fichiers` doit être une liste."]
    out: list[dict[str, Any]] = []
    for idx, raw in enumerate(fichiers):
        if not isinstance(raw, dict):
            return None, [f"Entrée `fichiers[{idx}]` invalide."]
        extra = set(raw.keys()) - _FICHIER_ALLOWED_KEYS
        if extra:
            return None, [
                f"Champ(s) non autorisé(s) dans `fichiers[{idx}]` : "
                + ", ".join(sorted(extra))
            ]
        slot = str(raw.get("slot") or "").strip()
        if not slot or len(slot) > 64:
            return None, [f"`fichiers[{idx}].slot` est invalide."]
        sha256 = str(raw.get("sha256") or "").strip().lower()
        if not _SHA256_OR_EMPTY_RE.match(sha256):
            return None, [f"`fichiers[{idx}].sha256` est invalide."]
        taille = raw.get("taille")
        if not isinstance(taille, int) or isinstance(taille, bool) or taille < 0:
            return None, [f"`fichiers[{idx}].taille` doit être un entier >= 0."]
        onedrive_item_id = str(raw.get("onedrive_item_id") or "").strip()
        if len(onedrive_item_id) > 256:
            return None, [f"`fichiers[{idx}].onedrive_item_id` est trop long."]
        document_id = str(raw.get("document_id") or "").strip()
        if document_id and not is_addressable_id(document_id):
            return None, [f"`fichiers[{idx}].document_id` est invalide."]
        statut_onedrive = str(raw.get("statut_onedrive") or "").strip()
        statut_athena = str(raw.get("statut_athena") or "").strip()
        if len(statut_onedrive) > 64 or len(statut_athena) > 64:
            return None, [f"Statut trop long dans `fichiers[{idx}]`."]
        out.append({
            "slot": slot,
            "sha256": sha256,
            "taille": taille,
            "onedrive_item_id": onedrive_item_id,
            "document_id": document_id,
            "statut_onedrive": statut_onedrive,
            "statut_athena": statut_athena,
        })
    return out, []


def get_courrier_message_strict(
    cle: str,
    *,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """Read a registry document by ``cle``; propagates Firestore read errors."""
    if not is_valid_cle(cle):
        return None
    snap = db.collection(COLLECTION).document(cle).get()
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    if is_expired(data, now=now):
        return None
    return {**data, "cle": snap.id}


def get_courrier_message(
    cle: str,
    *,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """Fail-open display reader for a ``courrier_messages/{cle}`` record."""
    try:
        return get_courrier_message_strict(cle, now=now)
    except Exception:
        log_unexpected("courrier get_courrier_message failed")
        return None


def get_courrier_messages_map_strict(
    cles: Iterable[str],
    *,
    now: Optional[datetime] = None,
) -> dict[str, dict[str, Any]]:
    """Batch-read registry rows via ``db.get_all``; propagates read errors."""
    unique_cles = list(dict.fromkeys(c for c in cles if is_valid_cle(c)))
    if not unique_cles:
        return {}
    refs = [db.collection(COLLECTION).document(c) for c in unique_cles]
    out: dict[str, dict[str, Any]] = {}
    for snap in db.get_all(refs):
        if not snap.exists:
            continue
        data = snap.to_dict() or {}
        if is_expired(data, now=now):
            continue
        out[snap.id] = {**data, "cle": snap.id}
    return out


def record_inventorie(
    cle: str,
    graph_id: str,
    *,
    now: Optional[datetime] = None,
) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Record or refresh a 30-day ``inventorie`` marker (never downgrades)."""
    if not is_valid_cle(cle):
        return None, ["Clé de courriel invalide."]
    cleaned_graph_id = str(graph_id or "").strip()
    if not cleaned_graph_id:
        return None, ["Identifiant Graph du courriel requis."]

    ts = _utc_now(now)
    expire_at = ts + timedelta(days=INVENTORIE_TTL_DAYS)
    ref = db.collection(COLLECTION).document(cle)

    @firestore.transactional
    def _txn(transaction):
        snap = ref.get(transaction=transaction)
        existing = snap.to_dict() or {} if snap.exists else None
        live = existing is not None and not is_expired(existing, now=ts)
        if live and existing.get("etat") in (ETAT_ENREGISTRE, ETAT_CLASSE):
            return None, ["Ce courriel est déjà enregistré ou classé."]
        if live and existing is not None:
            doc = {
                **existing,
                "cle": cle,
                "graph_id": cleaned_graph_id,
                "etat": ETAT_INVENTORIE,
                "expire_at": expire_at,
            }
            provenance.stamp_update(doc, ts)
        else:
            doc = _base_doc(
                cle=cle,
                graph_id=cleaned_graph_id,
                etat=ETAT_INVENTORIE,
                expire_at=expire_at,
            )
            provenance.stamp_create(doc, ts)
        transaction.set(ref, doc)
        return doc, []

    doc, errors = _txn(db.transaction())
    if errors:
        return None, errors
    provenance.note_commit(COLLECTION, cle)
    return doc, []


def record_enregistre(
    cle: str,
    graph_id: str,
    *,
    dossier_id: str,
    dossier_numero: str,
    fichiers: list[dict[str, Any]],
    now: Optional[datetime] = None,
) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Atomically record a permanent ``enregistre`` row for a saved email."""
    if not is_valid_cle(cle):
        return None, ["Clé de courriel invalide."]
    cleaned_graph_id = str(graph_id or "").strip()
    if not cleaned_graph_id:
        return None, ["Identifiant Graph du courriel requis."]
    cleaned_dossier_id = str(dossier_id or "").strip()
    if not is_addressable_id(cleaned_dossier_id):
        return None, ["Identifiant de dossier invalide."]
    cleaned_numero = str(dossier_numero or "").strip()
    if not _DOSSIER_NUM_RE.match(cleaned_numero):
        return None, ["Numéro de dossier invalide."]
    normalized_fichiers, file_errors = _normalize_fichiers(fichiers)
    if file_errors or normalized_fichiers is None:
        return None, file_errors

    ts = _utc_now(now)
    ref = db.collection(COLLECTION).document(cle)

    @firestore.transactional
    def _txn(transaction):
        snap = ref.get(transaction=transaction)
        existing = snap.to_dict() or {} if snap.exists else None
        preserved_etat = (
            ETAT_CLASSE
            if existing is not None and existing.get("etat") == ETAT_CLASSE
            else ETAT_ENREGISTRE
        )
        doc = _base_doc(
            cle=cle,
            graph_id=cleaned_graph_id,
            etat=preserved_etat,
            expire_at=None,
            dossier_id=cleaned_dossier_id,
            dossier_numero=cleaned_numero,
            fichiers=normalized_fichiers,
            legacy_ref=str(existing.get("legacy_ref") or "") if existing else "",
            time_entry_id=str(existing.get("time_entry_id") or "") if existing else "",
            outlook_folder_id=(
                str(existing.get("outlook_folder_id") or "") if existing else ""
            ),
            saved_at=ts,
            moved_at=existing.get("moved_at") if existing else None,
        )
        if existing is not None:
            doc["created_at"] = existing.get("created_at")
            doc["created_via"] = existing.get("created_via", "")
            if "mcp_updated_at" in existing:
                doc["mcp_updated_at"] = existing["mcp_updated_at"]
            provenance.stamp_update(doc, ts)
        else:
            provenance.stamp_create(doc, ts)
        transaction.set(ref, doc)
        return doc, []

    doc, errors = _txn(db.transaction())
    if errors:
        return None, errors
    provenance.note_commit(COLLECTION, cle)
    return doc, []


def record_classe(
    cle: str,
    graph_id: str,
    *,
    outlook_folder_id: str,
    now: Optional[datetime] = None,
) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Mark an already-saved email as ``classe`` in Outlook (never before save)."""
    if not is_valid_cle(cle):
        return None, ["Clé de courriel invalide."]
    cleaned_graph_id = str(graph_id or "").strip()
    if not cleaned_graph_id:
        return None, ["Identifiant Graph du courriel requis."]
    cleaned_folder_id = str(outlook_folder_id or "").strip()
    if not cleaned_folder_id:
        return None, ["Identifiant du dossier Outlook requis."]

    ts = _utc_now(now)
    ref = db.collection(COLLECTION).document(cle)

    @firestore.transactional
    def _txn(transaction):
        snap = ref.get(transaction=transaction)
        existing = snap.to_dict() or {} if snap.exists else None
        if (
            existing is None
            or is_expired(existing, now=ts)
            or existing.get("etat") not in (ETAT_ENREGISTRE, ETAT_CLASSE)
        ):
            return None, ["Enregistrement non constaté : déplacement refusé."]
        doc = {
            **existing,
            "cle": cle,
            "graph_id": cleaned_graph_id,
            "etat": ETAT_CLASSE,
            "outlook_folder_id": cleaned_folder_id,
            "moved_at": ts,
            "expire_at": None,
        }
        provenance.stamp_update(doc, ts)
        transaction.set(ref, doc)
        return doc, []

    doc, errors = _txn(db.transaction())
    if errors:
        return None, errors
    provenance.note_commit(COLLECTION, cle)
    return doc, []


# ── GCS staging & deterministic document reservation (Phase 3) ──────────


@dataclass(frozen=True)
class StagedCourrierBlob:
    """Metadata and digests of one streamed file held in temporary GCS staging."""

    blob: Any
    size: int
    sha256: str
    quickxorhash: str


def reserved_courrier_document_id(dossier_id: str, cle: str, slot: str) -> str:
    """Deterministic canonical RFC 4122 UUIDv4 for idempotent document ingestion."""
    cleaned_dossier = str(dossier_id or "").strip()
    cleaned_slot = str(slot or "").strip()
    if not is_addressable_id(cleaned_dossier):
        raise ValueError("Identifiant de dossier invalide.")
    if not is_valid_cle(cle):
        raise ValueError("Clé de courriel invalide.")
    if not cleaned_slot or len(cleaned_slot) > 64:
        raise ValueError("Slot de fichier invalide.")
    raw16 = hashlib.sha256(
        f"courrier-doc:{cleaned_dossier}:{cle}:{cleaned_slot}".encode("utf-8")
    ).digest()[:16]
    return str(uuid.UUID(bytes=raw16, version=4))


def courrier_staging_path(
    user_id: str,
    cle: str,
    slot_token: str,
    ext: str,
) -> str:
    """Five-segment neutral staging object path under ``staging/{uid}/courrier/``."""
    uid = storage_identity.require_uid(user_id)
    if not is_valid_cle(cle):
        raise ValueError("Clé de courriel invalide.")
    token = str(slot_token or "").strip().lower()
    if not _SLOT_TOKEN_RE.match(token):
        raise ValueError("Jeton de slot invalide.")
    raw_ext = str(ext or "").strip().lower()
    clean_ext = raw_ext if _EXT_RE.match(raw_ext) else ".bin"
    return f"staging/{uid}/courrier/{cle}/{token}{clean_ext}"


class _StreamingDigestReader(io.RawIOBase):
    """Readable non-seekable adapter hashing bytes incrementally as GCS pulls them."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        super().__init__()
        self._iter: Iterator[bytes] = iter(chunks)
        self._buf = bytearray()
        self._exhausted = False
        self._pos = 0
        self.sha256 = hashlib.sha256()
        self.quickxorhash = QuickXorHash()
        self.size = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            out = bytearray(self._buf)
            self._buf.clear()
            while not self._exhausted:
                try:
                    chunk = next(self._iter)
                except StopIteration:
                    self._exhausted = True
                    break
                if chunk:
                    self._fold(chunk)
                    out.extend(chunk)
            self._pos += len(out)
            return bytes(out)

        if size == 0:
            return b""
        while len(self._buf) < size and not self._exhausted:
            try:
                chunk = next(self._iter)
            except StopIteration:
                self._exhausted = True
                break
            if chunk:
                self._fold(chunk)
                self._buf.extend(chunk)
        take = bytes(self._buf[:size])
        del self._buf[:size]
        self._pos += len(take)
        return take

    def _fold(self, chunk: bytes) -> None:
        self.sha256.update(chunk)
        self.quickxorhash.update(chunk)
        self.size += len(chunk)


def stage_courrier_bytes(
    chunks: Iterable[bytes],
    *,
    user_id: str,
    cle: str,
    slot_token: str,
    ext: str,
) -> StagedCourrierBlob:
    """Stream *chunks* directly to GCS staging while computing SHA-256 and QuickXorHash."""
    uid = storage_identity.require_uid(user_id)
    path = courrier_staging_path(uid, cle, slot_token, ext)
    reader = _StreamingDigestReader(chunks)
    blob = storage.bucket().blob(path)
    blob.chunk_size = _GCS_STAGE_CHUNK_BYTES
    try:
        blob.upload_from_file(reader, content_type="application/octet-stream")
        blob.reload()
        stored_size = int(blob.size or 0)
        if reader.size <= 0 or stored_size != reader.size:
            raise ValueError("Taille incohérente du fichier mis en zone temporaire.")
        return StagedCourrierBlob(
            blob=blob,
            size=reader.size,
            sha256=reader.sha256.hexdigest(),
            quickxorhash=reader.quickxorhash.b64digest(),
        )
    except Exception:
        _best_effort_delete_staged(blob)
        raise


def read_staged_courrier_slice(
    staged: StagedCourrierBlob,
    start: int,
    length: int,
) -> bytes:
    """Read a bounded byte range from a staged GCS object (pinned to its generation)."""
    if length <= 0:
        return b""
    end = start + length - 1
    gen = getattr(staged.blob, "generation", None)
    return staged.blob.download_as_bytes(
        start=start,
        end=end,
        if_generation_match=gen,
        checksum=None,
    )


def _best_effort_delete_staged(blob: Any) -> None:
    if blob is None:
        return
    try:
        gen = getattr(blob, "generation", None)
        if gen is not None:
            blob.delete(if_generation_match=gen)
        else:
            blob.delete()
    except (NotFound, PreconditionFailed):
        return
    except Exception:
        log_unexpected("courrier staging cleanup failed")


def discard_staged_courrier_blob(staged: Optional[StagedCourrierBlob]) -> None:
    """Best-effort cleanup of a temporary Courrier staging object."""
    if staged is not None:
        _best_effort_delete_staged(staged.blob)
