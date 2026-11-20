"""Courrier registry model — zero-PII Firestore state for Outlook mail intake.

Stores the processing state machine of emails handled by the Courrier MCP
module in the top-level ``courrier_messages/{cle}`` collection:

* ``inventorie`` — provisional triage/deferral marker carrying a 30-day
  ``expire_at`` TTL (written when a message is explicitly deferred or left in
  Inbox; ``list_courriels`` is strictly read-only and never writes rows).
* ``enregistre`` — permanent record written atomically once the raw ``.eml``
  and attachments have been saved to OneDrive and ingested into Athena
  (``expire_at`` is ``None`` so Firestore TTL never collects it).
* ``classe`` — permanent record after Outlook filing under
  ``Archive > Dossiers > {Actifs, Suspendu, Services-conseils}``
  (``expire_at`` is ``None``).

Keyed-ID exception (Architecture Rule 6): documents are keyed by ``cle`` —
the first 16 lowercase hexadecimal characters of
``sha256(internetMessageId.strip())`` — rather than a random UUIDv4 so
``legacy_ref`` links on ``timeentries`` (``recep:{dossier}:{date}:{cle}``) stay
continuous across retries and across the retired local intake script.

Zero-PII invariant: ``courrier_messages`` stores opaque technical identifiers,
counts, SHA-256 digests, sizes, timestamps, and status tokens ONLY — never
subjects, email addresses, client filenames, or client-named paths.

Expiry is enforced in code on every read (:func:`is_expired`) regardless of
Firestore background TTL lag.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Optional

from firebase_admin import storage
from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import firestore

from models import (
    concurrency,
    db,
    dossier as dossier_model,
    find_by_legacy_ref,
    provenance,
    time_entry as time_entry_model,
)
from models.record_ids import is_addressable_id
from security import sanitize
from utils import phases, storage_identity
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
MAX_ENTREES_TEMPS = 25
MAX_CLES_PAR_LOT_TEMPS = 100

_CLE_RE = re.compile(r"\A[0-9a-f]{16}\Z")
_SHA256_OR_EMPTY_RE = re.compile(r"\A(?:[0-9a-f]{64})?\Z")
_DOSSIER_NUM_RE = re.compile(r"\A[0-9]{4}-[0-9]{3}\Z")
_DATE_ISO_RE = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_SLOT_TOKEN_RE = re.compile(r"\A[a-z0-9_-]{1,32}\Z")
_EXT_RE = re.compile(r"\A\.[a-z0-9]{1,16}\Z")

_ALLOWED_ENTREE_KEYS = frozenset({
    "dossier_id",
    "date",
    "description",
    "hours",
    "rate_cents",
    "billable",
    "phase",
    "sous_phase",
})

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
    "MAX_CLES_PAR_LOT_TEMPS",
    "MAX_ENTREES_TEMPS",
    "StagedCourrierBlob",
    "VALID_ETATS",
    "build_courrier_legacy_ref",
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
    "record_courriels_temps",
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
        self.sha256 = hashlib.sha256()
        self.quickxorhash = QuickXorHash()
        self.size = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

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


# ── Phase 5 : record_courriels_temps (atomic time entries + registry link) ──


def build_courrier_legacy_ref(
    dossier_numero: str,
    date_iso: str,
    cles: Iterable[str],
) -> str:
    """Build ``recep:{dossier}:{date}:{first_sorted_cle}`` anchor."""
    num = str(dossier_numero or "").strip()
    if not _DOSSIER_NUM_RE.match(num):
        raise ValueError("Numéro de dossier invalide pour la référence d'origine.")
    day_str = str(date_iso or "").strip()
    if not _DATE_ISO_RE.match(day_str):
        raise ValueError("Date invalide (AAAA-MM-JJ attendu).")
    date.fromisoformat(day_str)
    cleaned = sorted({str(c or "").strip() for c in cles})
    if not cleaned or not all(is_valid_cle(c) for c in cleaned):
        raise ValueError("Liste de clés Courrier invalide.")
    return f"recep:{num}:{day_str}:{cleaned[0]}"


def _validate_courrier_reg_for_temps(
    cle: str,
    reg_doc: Optional[dict[str, Any]],
    *,
    expected_dossier_id: str,
    ts: datetime,
) -> Optional[str]:
    if (
        reg_doc is None
        or is_expired(reg_doc, now=ts)
        or reg_doc.get("etat") not in (ETAT_ENREGISTRE, ETAT_CLASSE)
    ):
        return (
            f"Courriel {cle} non enregistré : veuillez d'abord l'enregistrer par "
            "save_courriels."
        )
    if str(reg_doc.get("dossier_id") or "") != expected_dossier_id:
        return f"Courriel {cle} rattaché à un autre dossier."
    if (
        str(reg_doc.get("legacy_ref") or "").strip()
        or str(reg_doc.get("time_entry_id") or "").strip()
    ):
        return f"Courriel {cle} déjà compté dans une entrée de temps."
    return None


def _format_temps_result_row(
    spec: dict[str, Any],
    *,
    entry_id: Optional[str],
    t_doc: Optional[dict[str, Any]],
) -> dict[str, Any]:
    merged = spec["merged"]
    return {
        "legacy_ref": spec["legacy_ref"],
        "dossier_id": spec["dossier_id"],
        "dossier": spec["dossier_numero"],
        "date": spec["date_iso"],
        "hours": float(merged["hours"]),
        "rate_cents": int(merged["rate"]),
        "amount_cents": int(merged["amount"]),
        "billable": bool(merged["billable"]),
        "phase": merged["phase"],
        "sous_phase": merged["sous_phase"],
        "cles": list(spec["cles"]),
        "time_entry_id": entry_id,
        "time_entry_doc": t_doc,
    }


def record_courriels_temps(
    entrees: list[dict[str, Any]],
    *,
    essai: bool = False,
    now: Optional[datetime] = None,
) -> tuple[Optional[list[dict[str, Any]]], list[str]]:
    """Atomically create time entries and link covered ``courrier_messages`` rows."""
    if not isinstance(entrees, list) or not entrees:
        return None, ["Aucune entrée de temps à inscrire."]
    if len(entrees) > MAX_ENTREES_TEMPS:
        return None, [
            f"Au plus {MAX_ENTREES_TEMPS} entrées de temps par lot ({len(entrees)} reçues)."
        ]

    ts = _utc_now(now)
    via = provenance.current_via()
    dossier_cache: dict[str, Optional[dict[str, Any]]] = {}
    seen_cles: dict[str, int] = {}
    seen_refs: dict[str, int] = {}
    total_cles = 0
    prepared: list[dict[str, Any]] = []
    errors: list[str] = []

    for idx, raw_item in enumerate(entrees):
        if not isinstance(raw_item, dict):
            errors.append(f"entrees[{idx}] : objet attendu.")
            continue

        raw_ref = raw_item.get("legacy_ref")
        if not isinstance(raw_ref, str) or not raw_ref.strip():
            errors.append(f"entrees[{idx}] : `legacy_ref` est requis.")
            legacy_ref = ""
        else:
            legacy_ref = raw_ref.strip()
            if len(legacy_ref) > 200 or sanitize(legacy_ref, max_length=200) != legacy_ref:
                errors.append(f"entrees[{idx}] : `legacy_ref` invalide ou non autorisé.")
            else:
                prev_ref_idx = seen_refs.setdefault(legacy_ref, idx)
                if prev_ref_idx != idx:
                    errors.append(
                        f"entrees[{idx}] : La référence d'origine « {legacy_ref} » "
                        f"figure déjà à `entrees[{prev_ref_idx}]`."
                    )

        raw_cles = raw_item.get("cles")
        cles_list: list[str] = []
        if not isinstance(raw_cles, list) or not raw_cles or len(raw_cles) > MAX_ENTREES_TEMPS:
            errors.append(
                f"entrees[{idx}] : `cles` doit contenir entre 1 et {MAX_ENTREES_TEMPS} clés."
            )
        else:
            local_seen: set[str] = set()
            row_cles_valid = True
            for raw_cle in raw_cles:
                cle = str(raw_cle or "").strip()
                if not is_valid_cle(cle):
                    errors.append(f"entrees[{idx}] : clé Courrier invalide dans `cles`.")
                    row_cles_valid = False
                    break
                if cle in local_seen:
                    errors.append(
                        f"entrees[{idx}] : clé en double dans `cles` (`{cle}`)."
                    )
                    row_cles_valid = False
                    break
                local_seen.add(cle)
                prev_cle_idx = seen_cles.setdefault(cle, idx)
                if prev_cle_idx != idx:
                    errors.append(
                        "Un même courriel ne peut pas être compté dans deux entrées de temps."
                    )
                    row_cles_valid = False
                    break
                cles_list.append(cle)
            if row_cles_valid:
                total_cles += len(cles_list)

        entree_raw = raw_item.get("entree")
        if not isinstance(entree_raw, dict):
            errors.append(f"entrees[{idx}] : `entree` doit être un objet.")
            continue

        extra_keys = set(entree_raw.keys()) - _ALLOWED_ENTREE_KEYS
        if extra_keys:
            errors.append(
                f"entrees[{idx}] : champ(s) non autorisé(s) dans `entree` : "
                + ", ".join(sorted(extra_keys))
            )
            continue

        dossier_id = str(entree_raw.get("dossier_id") or "").strip()
        dossier_doc: Optional[dict[str, Any]] = None
        if not is_addressable_id(dossier_id):
            errors.append(f"entrees[{idx}] : `dossier_id` invalide.")
        else:
            if dossier_id not in dossier_cache:
                dossier_cache[dossier_id] = dossier_model.get_dossier_strict(dossier_id)
            dossier_doc = dossier_cache[dossier_id]
            if dossier_doc is None:
                errors.append(f"entrees[{idx}] : dossier Athéna introuvable.")

        dossier_numero = (
            str(dossier_doc.get("file_number") or "").strip()
            if dossier_doc is not None
            else ""
        )
        if dossier_doc is not None and not _DOSSIER_NUM_RE.match(dossier_numero):
            errors.append(f"entrees[{idx}] : numéro de dossier Athéna invalide.")

        date_raw = entree_raw.get("date")
        date_iso = str(date_raw or "").strip() if isinstance(date_raw, str) else ""
        when_utc: Optional[datetime] = None
        if not _DATE_ISO_RE.match(date_iso):
            errors.append(f"entrees[{idx}] : `date` invalide (AAAA-MM-JJ attendu).")
        else:
            try:
                parsed_day = date.fromisoformat(date_iso)
                when_utc = datetime(
                    parsed_day.year, parsed_day.month, parsed_day.day, tzinfo=timezone.utc
                )
            except ValueError:
                errors.append(f"entrees[{idx}] : `date` hors calendrier.")

        if (
            legacy_ref
            and cles_list
            and dossier_numero
            and _DOSSIER_NUM_RE.match(dossier_numero)
            and when_utc is not None
        ):
            expected_ref = build_courrier_legacy_ref(dossier_numero, date_iso, cles_list)
            if legacy_ref != expected_ref:
                errors.append(
                    f"entrees[{idx}] : `legacy_ref` attendu « {expected_ref} » "
                    f"(reçu « {legacy_ref} »)."
                )

        desc_raw = entree_raw.get("description")
        desc = desc_raw.strip() if isinstance(desc_raw, str) else ""
        if not desc:
            errors.append(f"entrees[{idx}] : `description` est requise.")
        elif len(desc) > 1000 or sanitize(desc, max_length=1000) != desc:
            errors.append(
                f"entrees[{idx}] : `description` invalide, trop longue (> 1000) ou "
                "contenant du texte entre chevrons."
            )

        hours_raw = entree_raw.get("hours")
        hours_clean: Optional[float] = None
        if (
            isinstance(hours_raw, bool)
            or not isinstance(hours_raw, (int, float))
            or not math.isfinite(float(hours_raw))
            or float(hours_raw) <= 0
            or float(hours_raw) > 24
        ):
            errors.append(
                f"entrees[{idx}] : `hours` doit être un nombre > 0 et <= 24."
            )
        else:
            rounded_h = round(float(hours_raw), 2)
            if abs(rounded_h - float(hours_raw)) > 1e-9:
                errors.append(
                    f"entrees[{idx}] : `hours` ({hours_raw}) porte plus de deux "
                    "décimales (jamais arrondi)."
                )
            else:
                hours_clean = rounded_h

        rate_raw = entree_raw.get("rate_cents")
        rate_clean: Optional[int] = None
        if rate_raw is None:
            raw_default_rate = (
                dossier_doc.get("hourly_rate") if dossier_doc is not None else 0
            )
            try:
                rate_clean = max(0, int(raw_default_rate or 0))
            except (TypeError, ValueError):
                rate_clean = 0
        elif (
            isinstance(rate_raw, bool)
            or not isinstance(rate_raw, int)
            or rate_raw < 0
            or rate_raw > 1_000_000
        ):
            errors.append(
                f"entrees[{idx}] : `rate_cents` doit être un entier entre 0 et 1000000."
            )
        else:
            rate_clean = rate_raw

        billable_raw = entree_raw.get("billable", True)
        if not isinstance(billable_raw, bool):
            errors.append(f"entrees[{idx}] : `billable` doit être un booléen.")
            billable_clean = True
        else:
            billable_clean = billable_raw

        raw_phase = str(entree_raw.get("phase") or "").strip()
        raw_sous = str(entree_raw.get("sous_phase") or "").strip()
        resolved_phase, resolved_sous = phases.resolve_pair(raw_phase, raw_sous)
        if (
            resolved_phase
            and resolved_sous
            and phases.phase_of(resolved_sous) != resolved_phase
        ):
            errors.append(
                f"entrees[{idx}] : la sous-phase « {resolved_sous} » n'appartient "
                f"pas à la phase « {resolved_phase} »."
            )

        if (
            dossier_doc is None
            or not dossier_numero
            or when_utc is None
            or not desc
            or hours_clean is None
            or rate_clean is None
            or not legacy_ref
            or not cles_list
        ):
            continue

        row_payload = {
            "dossier_id": dossier_id,
            "dossier_file_number": dossier_numero,
            "dossier_title": str(dossier_doc.get("title") or ""),
            "date": when_utc,
            "description": desc,
            "hours": hours_clean,
            "rate": rate_clean,
            "billable": billable_clean,
            "phase": resolved_phase,
            "sous_phase": resolved_sous,
            "invoiced": False,
            "created_via": via,
            "legacy_ref": legacy_ref,
        }
        merged, model_errs = time_entry_model._prepare_create(row_payload)
        if model_errs:
            errors.extend(f"entrees[{idx}] : {e}" for e in model_errs)
            continue

        prepared.append({
            "idx": idx,
            "legacy_ref": legacy_ref,
            "dossier_id": dossier_id,
            "dossier_numero": dossier_numero,
            "date_iso": date_iso,
            "cles": cles_list,
            "merged": merged,
            "entry_id": str(uuid.uuid4()),
        })

    if total_cles > MAX_CLES_PAR_LOT_TEMPS:
        errors.append(
            f"Au plus {MAX_CLES_PAR_LOT_TEMPS} courriels par lot ({total_cles} reçus)."
        )

    if errors:
        return None, errors

    for spec in prepared:
        existing_entries = find_by_legacy_ref(
            time_entry_model.COLLECTION, spec["legacy_ref"], limit=1
        )
        if existing_entries:
            return None, [
                f"entrees[{spec['idx']}] : La référence d'origine « {spec['legacy_ref']} » "
                "existe déjà dans les entrées de temps."
            ]

    all_cles = [cle for spec in prepared for cle in spec["cles"]]

    if essai:
        reg_map = get_courrier_messages_map_strict(all_cles, now=ts)
        dry_errors: list[str] = []
        for spec in prepared:
            for cle in spec["cles"]:
                err = _validate_courrier_reg_for_temps(
                    cle,
                    reg_map.get(cle),
                    expected_dossier_id=spec["dossier_id"],
                    ts=ts,
                )
                if err:
                    dry_errors.append(err)
        if dry_errors:
            return None, dry_errors
        sim_rows = [
            _format_temps_result_row(spec, entry_id=None, t_doc=None)
            for spec in prepared
        ]
        return sim_rows, []

    commit_staged = [False]
    staged_docs: list[dict[str, Any]] = []

    @firestore.transactional
    def _txn(transaction):
        staged_docs.clear()
        existing_by_cle: dict[str, dict[str, Any]] = {}
        txn_errs: list[str] = []

        for spec in prepared:
            for cle in spec["cles"]:
                c_ref = db.collection(COLLECTION).document(cle)
                snap = c_ref.get(transaction=transaction)
                existing = {**(snap.to_dict() or {}), "cle": snap.id} if snap.exists else None
                err = _validate_courrier_reg_for_temps(
                    cle,
                    existing,
                    expected_dossier_id=spec["dossier_id"],
                    ts=ts,
                )
                if err:
                    txn_errs.append(err)
                elif existing is not None:
                    existing_by_cle[cle] = existing

        if txn_errs:
            return None, txn_errs

        for spec in prepared:
            t_doc = dict(spec["merged"])
            t_doc["id"] = spec["entry_id"]
            provenance.stamp_create(t_doc, ts)
            t_ref = db.collection(time_entry_model.COLLECTION).document(spec["entry_id"])
            transaction.create(t_ref, t_doc)
            staged_docs.append(t_doc)

            for cle in spec["cles"]:
                c_doc = {
                    **existing_by_cle[cle],
                    "cle": cle,
                    "legacy_ref": spec["legacy_ref"],
                    "time_entry_id": spec["entry_id"],
                }
                provenance.stamp_update(c_doc, ts)
                c_ref = db.collection(COLLECTION).document(cle)
                transaction.set(c_ref, c_doc)

        commit_staged[0] = True
        return list(staged_docs), []

    try:
        committed_docs, txn_errors = _txn(db.transaction())
    except Exception:
        if not commit_staged[0]:
            raise
        log_unexpected("courrier record_courriels_temps write failed", exc_info=False)
        outcome, _stored = concurrency.settle_failed_create(
            db.collection(time_entry_model.COLLECTION).document(prepared[0]["entry_id"])
        )
        if outcome == concurrency.WRITE_PRESENT:
            committed_docs = list(staged_docs)
            txn_errors = []
        elif outcome == concurrency.WRITE_UNKNOWN:
            return None, [concurrency.WRITE_OUTCOME_UNCERTAIN_ERROR]
        else:
            return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

    if txn_errors or committed_docs is None:
        return None, txn_errors

    for t_doc in committed_docs:
        provenance.note_commit(time_entry_model.COLLECTION, t_doc["id"])
    for cle in all_cles:
        provenance.note_commit(COLLECTION, cle)

    rows_out = [
        _format_temps_result_row(
            spec,
            entry_id=t_doc["id"],
            t_doc=t_doc,
        )
        for spec, t_doc in zip(prepared, committed_docs)
    ]
    return rows_out, []
