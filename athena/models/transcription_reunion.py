"""Staged post-meeting Teams transcripts & Copilot notes (collection ``meeting_transcripts``).

Keyed-ID exception (Architecture Rule 6): documents live at
``meeting_transcripts/{hearing_id}`` so repeated background checks and manual
« Vérifier maintenant » polls converge on ONE record per hearing without duplicate
staging cards.

Privacy & Storage separation:
- Raw WebVTT bytes live in Firebase Storage under
  ``staging/{uid}/transcriptions/{hearing_id}/transcript.vtt`` until « Verser »
  or « Écarter » runs (and are purged immediately on completion).
- Firestore holds only metadata, structured Copilot Markdown notes, speaker list,
  and a bounded preview for Réception / MCP review.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from firebase_admin import storage
from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from models import concurrency, db, provenance
from models.record_ids import is_addressable_id
from security import sanitize
from utils import storage_identity
from utils.logging_setup import log_unexpected

COLLECTION = "meeting_transcripts"

STATUS_A_REVOIR = "a_revoir"
STATUS_VERSEE = "versee"
STATUS_ECARTEE = "ecartee"
VALID_STATUSES: tuple[str, ...] = (
    STATUS_A_REVOIR,
    STATUS_VERSEE,
    STATUS_ECARTEE,
)

STATUS_LABELS: dict[str, str] = {
    STATUS_A_REVOIR: "À revoir",
    STATUS_VERSEE: "Versée",
    STATUS_ECARTEE: "Écartée",
}

COPILOT_MARKDOWN_MAX_LENGTH = 100_000
_FIELD_MAX_LENGTH = 2000

_VERSEMENT_ALLOWED_KEYS = frozenset({
    "note_id",
    "transcript_document_id",
    "onedrive_note_item_id",
    "onedrive_transcript_item_id",
    "onedrive_statut",
    "dossier_id",
    "dossier_file_number",
    "note_title",
    "note_category",
    "versed_at",
})


def _utc_now(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _default_versement() -> dict[str, Any]:
    return {
        "note_id": "",
        "transcript_document_id": "",
        "onedrive_note_item_id": "",
        "onedrive_transcript_item_id": "",
        "onedrive_statut": "",
        "dossier_id": "",
        "dossier_file_number": "",
        "note_title": "",
        "note_category": "",
        "versed_at": None,
    }


def _default_doc(hearing_id: str = "") -> dict[str, Any]:
    return {
        "id": hearing_id,
        "hearing_id": hearing_id,
        "online_meeting_id": "",
        "status": STATUS_A_REVOIR,
        "dossier_id": "",
        "transcript_id": "",
        "has_transcript": False,
        "transcript_cue_count": 0,
        "transcript_speakers": [],
        "transcript_preview": "",
        "staged_vtt_path": "",
        "insight_id": "",
        "has_insights": False,
        "copilot_markdown": "",
        "versement": _default_versement(),
        "captured_at": None,
        "created_at": None,
        "updated_at": None,
        "etag": "",
    }


def _migrate_doc(raw: dict[str, Any], doc_id: str = "") -> dict[str, Any]:
    hid = str(raw.get("hearing_id") or raw.get("id") or doc_id or "")
    merged = {**_default_doc(hid), **raw}
    merged["id"] = hid
    merged["hearing_id"] = hid
    merged["versement"] = {
        **_default_versement(),
        **(raw.get("versement") if isinstance(raw.get("versement"), dict) else {}),
    }
    if not isinstance(merged.get("transcript_speakers"), list):
        merged["transcript_speakers"] = []
    return merged


def reserved_transcript_document_id(hearing_id: str, dossier_id: str) -> str:
    """Deterministic canonical RFC 4122 UUIDv4 for idempotent transcript ingestion."""
    cleaned_hid = str(hearing_id or "").strip()
    cleaned_dossier = str(dossier_id or "").strip()
    if not is_addressable_id(cleaned_hid):
        raise ValueError("Identifiant de réunion invalide.")
    if not is_addressable_id(cleaned_dossier):
        raise ValueError("Identifiant de dossier invalide.")
    raw16 = hashlib.sha256(
        f"transcription-doc:{cleaned_dossier}:{cleaned_hid}".encode("utf-8")
    ).digest()[:16]
    return str(uuid.UUID(bytes=raw16, version=4))


# ── Readers (strict vs fail-open display) ─────────────────────────────────


def get_transcription_strict(hearing_id: str) -> Optional[dict[str, Any]]:
    """Fetch ``meeting_transcripts/{hearing_id}``; Firestore errors propagate."""
    if not is_addressable_id(hearing_id):
        return None
    snap = db.collection(COLLECTION).document(hearing_id).get()
    if not snap.exists:
        return None
    return _migrate_doc(snap.to_dict() or {}, doc_id=snap.id)


def get_transcription(hearing_id: str) -> Optional[dict[str, Any]]:
    """Fail-open display reader for ``meeting_transcripts/{hearing_id}``."""
    try:
        return get_transcription_strict(hearing_id)
    except Exception:
        log_unexpected("meeting_transcripts read failed", hearing_id=hearing_id)
        return None


def list_transcriptions_strict(
    *, status: Optional[str] = None
) -> list[dict[str, Any]]:
    """List staged post-meeting transcripts (strict — errors propagate).

    Single-field ``status`` equality uses Firestore's auto-index (no composite index).
    """
    col = db.collection(COLLECTION)
    if status is not None and status != "":
        if status not in VALID_STATUSES:
            raise ValueError("Statut de transcription invalide.")
        query = col.where(filter=FieldFilter("status", "==", status))
        snaps = query.stream()
    else:
        snaps = col.stream()
    rows = [_migrate_doc(doc.to_dict() or {}, doc_id=doc.id) for doc in snaps]
    floor = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(
        key=lambda r: (
            r.get("captured_at") or r.get("updated_at") or floor,
            r.get("hearing_id") or "",
        ),
        reverse=True,
    )
    return rows


def list_transcriptions_a_revoir() -> list[dict[str, Any]]:
    """Fail-open display reader for the Réception badge."""
    try:
        return list_transcriptions_strict(status=STATUS_A_REVOIR)
    except Exception:
        log_unexpected("meeting_transcripts list a_revoir failed")
        return []


# ── Writers (provenance + transactional concurrency) ─────────────────────


def upsert_staged_transcription(
    hearing_id: str,
    *,
    online_meeting_id: str,
    dossier_id: str = "",
    transcript_id: str = "",
    has_transcript: bool = False,
    transcript_cue_count: int = 0,
    transcript_speakers: Optional[list[str]] = None,
    transcript_preview: str = "",
    staged_vtt_path: str = "",
    insight_id: str = "",
    has_insights: bool = False,
    copilot_markdown: str = "",
    now: Optional[datetime] = None,
) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Create or refresh a staged ``a_revoir`` record; never downgrades ``versee``/``ecartee``."""
    if not is_addressable_id(hearing_id):
        return None, ["Identifiant d'audience invalide."]

    ts = _utc_now(now)
    ref = db.collection(COLLECTION).document(hearing_id)
    cleaned_speakers = [
        sanitize(str(s), max_length=120)
        for s in (transcript_speakers or [])[:25]
        if str(s or "").strip()
    ]
    cleaned_preview = sanitize(str(transcript_preview or ""), max_length=_FIELD_MAX_LENGTH)
    cleaned_md = sanitize(str(copilot_markdown or ""), max_length=COPILOT_MARKDOWN_MAX_LENGTH)
    cleaned_did = str(dossier_id or "").strip()

    @firestore.transactional
    def _txn(transaction):
        snap = ref.get(transaction=transaction)
        existing = _migrate_doc(snap.to_dict() or {}, doc_id=snap.id) if snap.exists else None
        if existing is not None and existing.get("status") in (
            STATUS_VERSEE,
            STATUS_ECARTEE,
        ):
            return existing, [], False

        base = existing if existing is not None else _default_doc(hearing_id)
        keep_md = (
            existing.get("copilot_markdown") or ""
            if existing is not None and existing.get("has_insights")
            else ""
        )
        effective_md = keep_md or cleaned_md or (
            existing.get("copilot_markdown") or "" if existing is not None else ""
        )
        effective_has_insights = bool(
            has_insights or (existing and existing.get("has_insights"))
        )
        effective_has_transcript = bool(
            has_transcript or (existing and existing.get("has_transcript"))
        )
        doc = {
            **base,
            "id": hearing_id,
            "hearing_id": hearing_id,
            "online_meeting_id": sanitize(online_meeting_id, max_length=512),
            "status": STATUS_A_REVOIR,
            "dossier_id": base.get("dossier_id") or cleaned_did,
            "transcript_id": sanitize(transcript_id, max_length=512)
            or base.get("transcript_id", ""),
            "has_transcript": effective_has_transcript,
            "transcript_cue_count": (
                int(transcript_cue_count)
                if has_transcript
                else int(base.get("transcript_cue_count") or 0)
            ),
            "transcript_speakers": (
                cleaned_speakers
                if has_transcript and cleaned_speakers
                else list(base.get("transcript_speakers") or [])
            ),
            "transcript_preview": (
                cleaned_preview if has_transcript else base.get("transcript_preview", "")
            ),
            "staged_vtt_path": (
                staged_vtt_path if has_transcript else base.get("staged_vtt_path", "")
            ),
            "insight_id": sanitize(insight_id, max_length=512)
            or base.get("insight_id", ""),
            "has_insights": effective_has_insights,
            "copilot_markdown": effective_md,
            "captured_at": base.get("captured_at") or ts,
        }
        if existing is not None:
            provenance.stamp_update(doc, ts)
        else:
            provenance.stamp_create(doc, ts)
        transaction.set(ref, doc)
        return doc, [], True

    try:
        doc, errors, wrote = _txn(db.transaction())
    except Exception:
        log_unexpected("meeting_transcripts upsert failed", hearing_id=hearing_id)
        return None, ["Erreur lors de l'enregistrement de la transcription. Réessayez."]
    if errors:
        return None, errors
    if wrote:
        provenance.note_commit(COLLECTION, hearing_id)
    return doc, []


def save_versement_progress(
    hearing_id: str,
    *,
    expected_etag: Optional[str] = None,
    dossier_id: str = "",
    status: str = STATUS_A_REVOIR,
    copilot_markdown: Optional[str] = None,
    staged_vtt_path: Optional[str] = None,
    versement_updates: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Persist partial or completed « Verser » progress atomically with etag check."""
    if not is_addressable_id(hearing_id):
        return None, ["Identifiant d'audience invalide."]
    if status not in (STATUS_A_REVOIR, STATUS_VERSEE):
        return None, ["Statut de versement invalide."]

    try:
        existing = get_transcription_strict(hearing_id)
    except Exception:
        log_unexpected("meeting_transcripts strict read failed", hearing_id=hearing_id)
        return None, [concurrency.READ_UNAVAILABLE_ERROR]
    if existing is None:
        return None, ["Transcription introuvable."]
    if existing.get("status") == STATUS_ECARTEE:
        return None, ["Cette transcription a déjà été écartée."]
    if not concurrency.matches(existing, expected_etag):
        return None, [concurrency.STALE_ETAG_ERROR]

    if copilot_markdown is not None:
        text = str(copilot_markdown)
        if len(text) > COPILOT_MARKDOWN_MAX_LENGTH:
            return None, [
                f"Les notes dépassent {COPILOT_MARKDOWN_MAX_LENGTH} caractères."
            ]
        if sanitize(text, max_length=COPILOT_MARKDOWN_MAX_LENGTH) != text:
            return None, [
                "Les notes contiennent du texte entre chevrons (<...>) qui "
                "serait supprimé à l'enregistrement — retirez les chevrons."
            ]

    merged_versement = {
        **_default_versement(),
        **(existing.get("versement") or {}),
    }
    if versement_updates:
        extra = set(versement_updates) - _VERSEMENT_ALLOWED_KEYS
        if extra:
            return None, [f"Champ de versement non autorisé : « {sorted(extra)[0]} »."]
        for key, val in versement_updates.items():
            if isinstance(val, str):
                merged_versement[key] = sanitize(val, max_length=_FIELD_MAX_LENGTH)
            else:
                merged_versement[key] = val

    ts = _utc_now(now)
    fields: dict[str, Any] = {
        "status": status,
        "versement": merged_versement,
        **provenance.update_fields(ts),
    }
    if dossier_id:
        fields["dossier_id"] = sanitize(dossier_id, max_length=100)
    if copilot_markdown is not None:
        fields["copilot_markdown"] = copilot_markdown
    if staged_vtt_path is not None:
        fields["staged_vtt_path"] = staged_vtt_path

    try:
        concurrency.commit_fields(
            db.collection(COLLECTION).document(hearing_id),
            fields,
            expected_etag=expected_etag,
            read_etag=concurrency.etag_of(existing),
        )
    except concurrency.StaleWrite:
        return None, [concurrency.STALE_ETAG_ERROR]
    except concurrency.Vanished:
        return None, ["Transcription introuvable."]
    except Exception:
        log_unexpected("meeting_transcripts save_versement_progress failed", hearing_id=hearing_id)
        return None, ["Erreur lors de la sauvegarde du versement. Réessayez."]

    provenance.note_commit(COLLECTION, hearing_id)
    return _migrate_doc({**existing, **fields}, doc_id=hearing_id), []


def mark_transcription_ecartee(
    hearing_id: str,
    *,
    expected_etag: Optional[str] = None,
    now: Optional[datetime] = None,
) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Mark a staged transcript record as discarded (« ecartee »)."""
    if not is_addressable_id(hearing_id):
        return None, ["Identifiant d'audience invalide."]
    try:
        existing = get_transcription_strict(hearing_id)
    except Exception:
        log_unexpected("meeting_transcripts strict read failed", hearing_id=hearing_id)
        return None, [concurrency.READ_UNAVAILABLE_ERROR]
    if existing is None:
        return None, ["Transcription introuvable."]
    if existing.get("status") == STATUS_VERSEE:
        return None, ["Cette transcription a déjà été versée au dossier et ne peut plus être écartée."]
    if existing.get("status") == STATUS_ECARTEE:
        return existing, []
    if not concurrency.matches(existing, expected_etag):
        return None, [concurrency.STALE_ETAG_ERROR]

    ts = _utc_now(now)
    fields: dict[str, Any] = {
        "status": STATUS_ECARTEE,
        "staged_vtt_path": "",
        **provenance.update_fields(ts),
    }
    try:
        concurrency.commit_fields(
            db.collection(COLLECTION).document(hearing_id),
            fields,
            expected_etag=expected_etag,
            read_etag=concurrency.etag_of(existing),
        )
    except concurrency.StaleWrite:
        return None, [concurrency.STALE_ETAG_ERROR]
    except concurrency.Vanished:
        return None, ["Transcription introuvable."]
    except Exception:
        log_unexpected("meeting_transcripts ecarter failed", hearing_id=hearing_id)
        return None, ["Erreur lors du rejet de la transcription. Réessayez."]

    provenance.note_commit(COLLECTION, hearing_id)
    return _migrate_doc({**existing, **fields}, doc_id=hearing_id), []


# ── Firebase Storage staging (kept out of services/ and mcp/) ────────────


def vtt_staging_path(user_id: str, hearing_id: str) -> str:
    """Canonical neutral Storage staging path for a raw WebVTT transcript."""
    uid = storage_identity.require_uid(user_id)
    if not is_addressable_id(hearing_id):
        raise ValueError("Identifiant de réunion invalide.")
    return f"staging/{uid}/transcriptions/{hearing_id}/transcript.vtt"


def temp_docx_staging_path(user_id: str, hearing_id: str) -> str:
    """Short-lived neutral Storage staging path for transcript .docx ingestion."""
    uid = storage_identity.require_uid(user_id)
    if not is_addressable_id(hearing_id):
        raise ValueError("Identifiant de réunion invalide.")
    return f"staging/{uid}/transcriptions/{hearing_id}/transcript.docx"


def stage_vtt_bytes(vtt_bytes: bytes, *, user_id: str, hearing_id: str) -> str:
    """Store raw WebVTT bytes in canonical GCS staging and return the object path."""
    path = vtt_staging_path(user_id, hearing_id)
    blob = storage.bucket().blob(path)
    blob.upload_from_string(vtt_bytes, content_type="text/vtt; charset=utf-8")
    return path


def read_staged_vtt_bytes(staged_path: str) -> bytes:
    """Download raw WebVTT bytes from an approved staging path."""
    cleaned = str(staged_path or "").strip()
    if not cleaned.startswith("staging/") or not cleaned.endswith("/transcript.vtt"):
        raise ValueError("Chemin de transcription temporaire invalide.")
    blob = storage.bucket().blob(cleaned)
    return blob.download_as_bytes()


def stage_temp_docx_bytes(
    docx_bytes: bytes, *, user_id: str, hearing_id: str
) -> Any:
    """Stage a generated ``.docx`` in GCS for ``document_model.ingest_blob_as_document``."""
    path = temp_docx_staging_path(user_id, hearing_id)
    blob = storage.bucket().blob(path)
    blob.upload_from_string(
        docx_bytes,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    blob.reload()
    return blob


def purge_staged_blob(staged_path_or_blob: Any) -> None:
    """Best-effort removal of a staged GCS object (never raises)."""
    if not staged_path_or_blob:
        return
    try:
        if isinstance(staged_path_or_blob, str):
            if not staged_path_or_blob.startswith("staging/"):
                return
            blob = storage.bucket().blob(staged_path_or_blob)
        else:
            blob = staged_path_or_blob
        gen = getattr(blob, "generation", None)
        if gen is not None:
            blob.delete(if_generation_match=gen)
        else:
            blob.delete()
    except (NotFound, PreconditionFailed):
        return
    except Exception:
        log_unexpected("meeting_transcripts staged blob purge failed")
