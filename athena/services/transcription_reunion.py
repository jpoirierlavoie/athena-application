"""Capture post-réunion Microsoft Teams (WebVTT + Copilot) et versement au dossier (Lot 3).

Orchestration uniquement (principe « One door per rule ») :
1. Sondage ciblé des réunions Teams terminées (``transcript_status == "en_attente"``) :
   - Zéro appel Graph lorsqu'aucune réunion confirmée n'est terminée ;
   - Attente configurable (2 h) après l'apparition d'une transcription lorsque Copilot
     ``aiInsights`` est disponible, contournable manuellement (« Vérifier maintenant ») ;
   - Passage propre à ``"aucune"`` après 24 h sans transcription ni résumé ;
   - Stockage du WebVTT brut dans GCS (``staging/{uid}/transcriptions/{hid}/transcript.vtt``)
     et des métadonnées/notes Markdown dans ``meeting_transcripts/{hid}``.
2. Versement idempotent en un clic (« Verser ») ou rejet (« Écarter ») depuis la
   Réception ou le connecteur MCP :
   - Création d'une Note Athéna (+ bump CTag DavX5) ;
   - Génération et classement de la transcription ``.docx`` dans ``Interne`` ;
   - Dépôt facultatif des deux ``.docx`` dans OneDrive ``Interne`` lorsque
     ``Config.courrier_configured()`` est vrai, avec suivi d'avancement par étape.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Optional

from config import Config
from dav.sync import bump_ctag, collection_for
from models import concurrency
from models import doc_template
from models import document as document_model
from models import dossier as dossier_model
from models import folder as folder_model
from models import hearing as hearing_model
from models import note as note_model
from models import provenance
from models import transcription_reunion as tr_model
from security import sanitize
from services import courrier as courrier_service
from tz import to_mtl
from utils import deadlines, docx_fill, graph_courrier, graph_teams, note_docx, storage_identity, transcription_docx, webvtt
from utils.cabinet import cabinet_dict
from utils.format_fr import format_date_fr
from utils.graph import GraphError, GraphNotConfigured
from utils.logging_setup import log_bookings_event, log_unexpected


class LectureImpossible(Exception):
    """Raised when Firestore fails during a strict transcript read."""


LECTURE_IMPOSSIBLE = "Lecture des transcriptions en attente impossible — réessayez."

COPILOT_WAIT_HOURS = 2
NO_TRANSCRIPT_GIVEUP_HOURS = 24
_NOTE_CATEGORIES_TRANSCRIPTION = ("consultation", "rencontre")
_CATEGORY_LABELS = {
    "consultation": "Consultation",
    "rencontre": "Rencontre",
}


def _utc_now(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _parse_graph_dt(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sync_hearing_transcript_status(hearing: dict[str, Any], new_status: str) -> bool:
    """Update ``hearing.transcript_status`` and bump DAV CTag when already confirmed."""
    hid = str(hearing.get("id") or "")
    if not hid or hearing.get("transcript_status") == new_status:
        return False
    _, errors = hearing_model.update_hearing(
        hid, {}, server_fields={"transcript_status": new_status}
    )
    if errors:
        log_unexpected("transcription: hearing transcript_status update failed", hearing_id=hid)
        return False
    if not (hearing.get("confirmation") or ""):
        try:
            bump_ctag(collection_for(hearing.get("dossier_id")))
        except Exception:
            log_unexpected("transcription: DAV bump failed after transcript_status update", hearing_id=hid)
    return True


def _pick_latest(items: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not items:
        return None
    return max(
        items,
        key=lambda item: (
            _parse_graph_dt(item.get("createdDateTime"))
            or datetime.min.replace(tzinfo=timezone.utc),
            str(item.get("id") or ""),
        ),
    )


def _fallback_notes_skeleton() -> str:
    return "## Synthèse de la réunion\n\n- \n\n## Suivis et actions\n\n- "


def _safe_docx_filename(prefix: str, date_iso: str, title: str) -> str:
    raw_title = " ".join(str(title or "").split()) or "Réunion Teams"
    base = f"{prefix} {date_iso} - {raw_title}" if date_iso else f"{prefix} - {raw_title}"
    stem = courrier_service._sanitize_component_text(base)
    max_stem = courrier_service.PIECE_NOM_MAX_CHARS - 5
    if len(stem) > max_stem:
        stem = stem[:max_stem].rstrip(" .") or prefix
    return f"{stem}.docx"


def _default_note_title(hearing: dict[str, Any]) -> str:
    title = " ".join(str(hearing.get("title") or "").split()) or "Rencontre Teams"
    start = hearing.get("start_datetime")
    if isinstance(start, datetime):
        mtl_day = to_mtl(start).date()
        return f"{title} — {mtl_day.isoformat()}"[:200]
    return title[:200]


def _note_docx_bytes(
    *,
    note_doc: dict[str, Any],
    dossier: dict[str, Any],
    date_label: str,
) -> bytes:
    tpl = doc_template.get_note_template()
    tpl_bytes = doc_template.template_file_bytes(tpl) if tpl else None
    if tpl and tpl_bytes:
        try:
            ctx = note_docx.build_note_context(
                note_doc,
                dossier=dossier,
                firm=cabinet_dict(),
                today=deadlines.today_mtl(),
            )
            values = note_docx.assemble_note_print_values(tpl, ctx)
            return docx_fill.fill_docx(tpl_bytes, values, rich_values=ctx.rich_values)
        except Exception:
            log_unexpected(
                "transcription: note gabarit fill failed, falling back to standalone docx",
                template_id=str(tpl.get("id") or ""),
            )
    dossier_label = str(dossier.get("file_number") or "")
    if dossier.get("title"):
        dossier_label = (
            f"{dossier_label} — {dossier['title']}" if dossier_label else str(dossier["title"])
        )
    cat = str(note_doc.get("category") or "")
    return transcription_docx.build_standalone_note_docx(
        title=str(note_doc.get("title") or "Notes de réunion"),
        dossier_label=dossier_label,
        date_label=date_label,
        category_label=_CATEGORY_LABELS.get(cat, cat),
        markdown_text=str(note_doc.get("content") or ""),
    )


def verifier_une_reunion(
    hearing_id: str,
    *,
    user_id: Optional[str] = None,
    forcer_sans_attente: bool = False,
    now: Optional[datetime] = None,
) -> tuple[str, list[str], Optional[dict[str, Any]]]:
    """Interroge Graph pour une réunion Teams donnée et prépare l'entrée Réception."""
    try:
        hearing = hearing_model.get_hearing_strict(hearing_id)
    except Exception:
        log_unexpected("transcription: hearing strict read failed", hearing_id=hearing_id)
        return "erreur_lecture", ["Lecture de l'événement impossible — réessayez."], None
    if not hearing:
        return "introuvable", ["Événement introuvable."], None

    online_meeting_id = str(hearing.get("online_meeting_id") or "").strip()
    if not online_meeting_id:
        return (
            "sans_reunion_teams",
            ["Cet événement n'est lié à aucune réunion Microsoft Teams suivie."],
            None,
        )

    existing_tr = tr_model.get_transcription(hearing_id)
    if existing_tr and existing_tr.get("status") in (
        tr_model.STATUS_VERSEE,
        tr_model.STATUS_ECARTEE,
    ):
        return existing_tr["status"], [], existing_tr

    if not (Config.TEAMS_REUNIONS_ACTIF and Config.teams_configured()):
        return (
            "non_configure",
            ["L'intégration Microsoft Teams n'est pas activée ou configurée."],
            None,
        )

    try:
        uid = storage_identity.require_uid(user_id or Config.AUTHORIZED_USER_EMAIL)
    except storage_identity.StorageIdentityUnavailable as exc:
        return "erreur_stockage", [storage_identity.public_message(exc)], None

    ts = _utc_now(now)
    try:
        transcripts = graph_teams.lister_transcriptions_reunion(online_meeting_id)
        insights, copilot_disponible = graph_teams.lister_insights_reunion(online_meeting_id)
    except (GraphError, GraphNotConfigured):
        log_bookings_event(
            "teams_transcription_erreur_graph",
            "failure",
            hearing_id=hearing_id,
            reason="poll_failed",
        )
        return (
            "erreur_graph",
            ["Impossible d'interroger Microsoft Graph pour cette réunion. Réessayez."],
            None,
        )

    raw_end = hearing.get("end_datetime") or hearing.get("start_datetime") or ts
    end_dt = _utc_now(raw_end)

    if not transcripts and not insights:
        if (ts - end_dt) >= timedelta(hours=NO_TRANSCRIPT_GIVEUP_HOURS):
            _sync_hearing_transcript_status(hearing, "aucune")
            log_bookings_event("teams_transcription_aucune", hearing_id=hearing_id)
            return "aucune", [], None
        return "en_attente", [], None

    latest_transcript = _pick_latest(transcripts)
    latest_insight = _pick_latest(insights)

    if (
        latest_transcript is not None
        and latest_insight is None
        and copilot_disponible
        and not forcer_sans_attente
    ):
        created_dt = _parse_graph_dt(latest_transcript.get("createdDateTime")) or end_dt
        if (ts - created_dt) < timedelta(hours=COPILOT_WAIT_HOURS):
            return "attente_copilot", [], None

    transcript_id = str((latest_transcript or {}).get("id") or "").strip()
    has_transcript = bool(transcript_id)
    cues: list[webvtt.TranscriptCue] = []
    speakers: list[str] = []
    preview = ""
    staged_vtt_path = ""
    if has_transcript:
        try:
            vtt_bytes = graph_teams.lire_contenu_transcription_vtt(
                online_meeting_id, transcript_id
            )
            cues = webvtt.parse_webvtt(vtt_bytes)
            speakers = webvtt.extract_speakers(cues)
            preview = webvtt.format_transcript_preview(cues)
            staged_vtt_path = tr_model.stage_vtt_bytes(
                vtt_bytes, user_id=uid, hearing_id=hearing_id
            )
        except (GraphError, GraphNotConfigured):
            log_bookings_event(
                "teams_transcription_erreur_graph",
                "failure",
                hearing_id=hearing_id,
                reason="vtt_download_failed",
            )
            return (
                "erreur_graph",
                ["Impossible de télécharger le contenu WebVTT depuis Microsoft Graph."],
                None,
            )
        except Exception:
            log_unexpected("transcription: VTT staging failed", hearing_id=hearing_id)
            return (
                "erreur_stockage",
                ["Impossible d'enregistrer temporairement la transcription."],
                None,
            )

    insight_id = str((latest_insight or {}).get("id") or "").strip()
    copilot_md = ""
    has_insights = False
    if insight_id:
        try:
            detail = graph_teams.lire_insight_reunion(online_meeting_id, insight_id)
            if detail:
                copilot_md = webvtt.format_copilot_insights_markdown(detail)
                has_insights = bool(copilot_md)
        except (GraphError, GraphNotConfigured):
            log_bookings_event(
                "teams_transcription_erreur_graph",
                "failure",
                hearing_id=hearing_id,
                reason="insight_download_failed",
            )

    if not copilot_md:
        copilot_md = _fallback_notes_skeleton()

    doc, up_errors = tr_model.upsert_staged_transcription(
        hearing_id,
        online_meeting_id=online_meeting_id,
        dossier_id=str(hearing.get("dossier_id") or ""),
        transcript_id=transcript_id,
        has_transcript=has_transcript,
        transcript_cue_count=len(cues),
        transcript_speakers=speakers,
        transcript_preview=preview,
        staged_vtt_path=staged_vtt_path,
        insight_id=insight_id,
        has_insights=has_insights,
        copilot_markdown=copilot_md,
        now=ts,
    )
    if up_errors or doc is None:
        if staged_vtt_path:
            tr_model.purge_staged_blob(staged_vtt_path)
        return "erreur_ecriture", up_errors or ["Enregistrement impossible."], None

    _sync_hearing_transcript_status(hearing, "a_revoir")
    log_bookings_event(
        "teams_transcription_preparee",
        hearing_id=hearing_id,
        has_transcript=has_transcript,
        has_insights=has_insights,
        cue_count=len(cues),
    )
    return "a_revoir", [], doc


def verifier_reunions_terminees(
    *,
    user_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, int]:
    """Cron / vérification manuelle des réunions Teams terminées (« en_attente »)."""
    counts = {"eligibles": 0, "pretes": 0, "attente": 0, "aucune": 0, "erreurs": 0}
    if not (Config.TEAMS_REUNIONS_ACTIF and Config.teams_configured()):
        return counts

    try:
        candidates = hearing_model.list_hearings_by_transcript_status_strict("en_attente")
    except Exception as exc:
        log_unexpected("transcription: list en_attente hearings failed")
        raise LectureImpossible() from exc

    ts = _utc_now(now)
    eligibles = [
        h
        for h in candidates
        if not (h.get("confirmation") or "")
        and h.get("status") != "annulée"
        and str(h.get("online_meeting_id") or "").strip()
        and isinstance(h.get("end_datetime"), datetime)
        and _utc_now(h["end_datetime"]) <= ts
    ]
    counts["eligibles"] = len(eligibles)
    if not eligibles:
        # Zéro appel Graph tant qu'aucune réunion suivie n'est terminée.
        return counts

    for hearing in eligibles:
        outcome, _errs, _doc = verifier_une_reunion(
            hearing["id"],
            user_id=user_id,
            forcer_sans_attente=False,
            now=ts,
        )
        if outcome == "a_revoir":
            counts["pretes"] += 1
        elif outcome == "aucune":
            counts["aucune"] += 1
        elif outcome in ("en_attente", "attente_copilot"):
            counts["attente"] += 1
        else:
            counts["erreurs"] += 1
    return counts


def lister_a_revoir_enrichies() -> list[dict[str, Any]]:
    """Liste stricte des transcriptions ``a_revoir`` enrichies pour Réception / MCP."""
    try:
        items = tr_model.list_transcriptions_strict(status=tr_model.STATUS_A_REVOIR)
    except Exception as exc:
        log_unexpected("transcription: list a_revoir strict failed")
        raise LectureImpossible() from exc

    enriched: list[dict[str, Any]] = []
    for item in items:
        hid = item["hearing_id"]
        hearing = hearing_model.get_hearing(hid) or {}
        dossier_id = item.get("dossier_id") or str(hearing.get("dossier_id") or "")
        hearing_type = str(hearing.get("hearing_type") or "")
        suggested_category = (
            "consultation" if hearing_type == "consultation" else "rencontre"
        )
        start_dt = hearing.get("start_datetime")
        start_label = ""
        if isinstance(start_dt, datetime):
            start_label = format_date_fr(to_mtl(start_dt).date())
        enriched.append(
            {
                **item,
                "hearing_title": str(hearing.get("title") or "Réunion Teams"),
                "hearing_start_datetime": start_dt,
                "hearing_start_label": start_label,
                "hearing_type": hearing_type,
                "client_nom": str(hearing.get("client_nom") or ""),
                "client_email": str(hearing.get("client_email") or ""),
                "suggested_dossier_id": dossier_id,
                "suggested_note_category": suggested_category,
                "suggested_note_title": _default_note_title(hearing),
            }
        )
    return enriched


def verser_transcription(
    hearing_id: str,
    *,
    dossier_id: str,
    note_title: str,
    note_category: str,
    markdown_content: str,
    expected_etag: str,
    user_id: Optional[str] = None,
    category_source: str = "juriste",
    now: Optional[datetime] = None,
) -> tuple[Optional[dict[str, Any]], list[str], list[str]]:
    """Verse une transcription/notes préparée dans Athéna (Note + ``Interne``) et OneDrive."""
    warnings: list[str] = []

    try:
        tr = tr_model.get_transcription_strict(hearing_id)
        hearing = hearing_model.get_hearing_strict(hearing_id)
        dossier = dossier_model.get_dossier_strict(dossier_id)
    except Exception:
        log_unexpected("transcription: verser strict read failed", hearing_id=hearing_id)
        return None, [concurrency.READ_UNAVAILABLE_ERROR], warnings

    if tr is None:
        return None, ["Transcription introuvable."], warnings
    if tr.get("status") == tr_model.STATUS_ECARTEE:
        return None, ["Cette transcription a déjà été écartée."], warnings
    if tr.get("status") == tr_model.STATUS_VERSEE:
        return tr, [], ["Cette transcription était déjà versée."]
    if not concurrency.matches(tr, expected_etag):
        return None, [concurrency.STALE_ETAG_ERROR], warnings

    if dossier is None:
        return None, ["Dossier introuvable."], warnings

    cleaned_category = str(note_category or "").strip()
    if cleaned_category not in _NOTE_CATEGORIES_TRANSCRIPTION:
        return (
            None,
            ["Catégorie de note invalide : choisissez « consultation » ou « rencontre »."],
            warnings,
        )

    cleaned_title = str(note_title or "").replace("\r\n", " ").replace("\r", " ").replace("\n", " ").strip()
    if not cleaned_title:
        return None, ["Le titre de la note est requis."], warnings
    if len(cleaned_title) > 200:
        return None, ["Le titre de la note ne doit pas dépasser 200 caractères."], warnings
    if sanitize(cleaned_title, max_length=200) != cleaned_title:
        return None, ["Le titre contient des chevrons (< ou >) non autorisés."], warnings

    md_text = str(markdown_content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not md_text:
        return None, ["Le contenu Markdown des notes est requis."], warnings
    if len(md_text) > note_model.CONTENT_MAX_LENGTH:
        return (
            None,
            [f"Le contenu dépasse {note_model.CONTENT_MAX_LENGTH} caractères."],
            warnings,
        )
    if sanitize(md_text, max_length=note_model.CONTENT_MAX_LENGTH) != md_text:
        return (
            None,
            ["Les notes contiennent du HTML brut (<...>) — retirez les chevrons."],
            warnings,
        )

    try:
        uid = storage_identity.require_uid(user_id or Config.AUTHORIZED_USER_EMAIL)
    except storage_identity.StorageIdentityUnavailable as exc:
        return None, [storage_identity.public_message(exc)], warnings

    interne_folder: Optional[dict[str, Any]] = None
    if tr.get("has_transcript"):
        interne_folder, folder_errors = folder_model.ensure_system_folder(
            dossier_id, folder_model.SYSTEM_ROLE_INTERNE
        )
        if folder_errors or interne_folder is None:
            return (
                None,
                folder_errors or ["Impossible de résoudre le dossier système « Interne »."],
                warnings,
            )

    ts = _utc_now(now)
    start_dt = (hearing or {}).get("start_datetime")
    if isinstance(start_dt, datetime):
        meeting_day = to_mtl(start_dt).date()
    else:
        meeting_day = deadlines.today_mtl()
    date_iso = meeting_day.isoformat()
    date_label = format_date_fr(meeting_day)
    dossier_num = str(dossier.get("file_number") or "")
    dossier_titre = str(dossier.get("title") or "")
    dossier_display = f"{dossier_num} — {dossier_titre}" if dossier_num and dossier_titre else (dossier_num or dossier_titre)
    meeting_title = str((hearing or {}).get("title") or cleaned_title)

    progress = dict(tr.get("versement") or {})
    current_etag = concurrency.etag_of(tr)
    working_tr = tr
    staged_vtt_path = str(tr.get("staged_vtt_path") or "")

    # Étape A : Note Athéna + bump CTag DavX5
    note_doc: Optional[dict[str, Any]] = None
    note_id = str(progress.get("note_id") or "")
    if note_id:
        try:
            note_doc = note_model.get_note_strict(note_id)
        except Exception:
            note_doc = None
    if note_doc is None:
        note_doc, note_errors = note_model.create_note(
            {
                "dossier_id": dossier_id,
                "title": cleaned_title,
                "category": cleaned_category,
                "content": md_text,
            }
        )
        if note_errors or note_doc is None:
            return None, note_errors or ["Création de la note impossible."], warnings
        note_id = note_doc["id"]
        try:
            bump_ctag(f"dossier:{dossier_id}")
        except Exception:
            log_unexpected("transcription: DAV note CTag bump failed", dossier_id=dossier_id)
            warnings.append("Note créée, mais la synchronisation téléphone (DavX5) n'a pas été rafraîchie.")

        saved_tr, save_errs = tr_model.save_versement_progress(
            hearing_id,
            expected_etag=current_etag,
            dossier_id=dossier_id,
            copilot_markdown=md_text,
            versement_updates={
                "note_id": note_id,
                "dossier_id": dossier_id,
                "dossier_file_number": dossier_num,
                "note_title": cleaned_title,
                "note_category": cleaned_category,
            },
            now=ts,
        )
        if save_errs or saved_tr is None:
            return working_tr, save_errs or ["Suivi de versement impossible."], warnings
        working_tr = saved_tr
        current_etag = concurrency.etag_of(working_tr)
        progress = dict(working_tr.get("versement") or {})

    # Étape B : Transcription .docx dans Interne (Athéna)
    transcript_doc_id = str(progress.get("transcript_document_id") or "")
    transcript_docx_bytes: Optional[bytes] = None
    transcript_filename = _safe_docx_filename("Transcription", date_iso, cleaned_title)

    def _build_tx_docx() -> bytes:
        cues: list[webvtt.TranscriptCue] = []
        if staged_vtt_path:
            raw_vtt = tr_model.read_staged_vtt_bytes(staged_vtt_path)
            cues = webvtt.parse_webvtt(raw_vtt)
        speakers = list(tr.get("transcript_speakers") or ()) or webvtt.extract_speakers(cues)
        return transcription_docx.build_transcript_docx(
            title=meeting_title,
            dossier_label=dossier_display,
            date_label=date_label,
            speakers=speakers,
            cues=cues,
        )

    if tr.get("has_transcript"):
        try:
            transcript_docx_bytes = _build_tx_docx()
        except Exception:
            log_unexpected("transcription: staged WebVTT read/build failed", hearing_id=hearing_id)
            return working_tr, ["Impossible de relire ou convertir la transcription WebVTT."], warnings

        existing_doc: Optional[dict[str, Any]] = None
        if transcript_doc_id:
            try:
                existing_doc = document_model.get_document_strict(transcript_doc_id)
            except Exception:
                existing_doc = None

        if existing_doc is None:
            assert interne_folder is not None
            reserved_doc_id = tr_model.reserved_transcript_document_id(hearing_id, dossier_id)
            doc_date = datetime.combine(meeting_day, dtime.min, tzinfo=timezone.utc)
            transcript_display_name = f"Transcription — {cleaned_title}"[:200]
            staged_docx = None
            keep_staged_docx = False
            try:
                staged_docx = tr_model.stage_temp_docx_bytes(
                    transcript_docx_bytes, user_id=uid, hearing_id=hearing_id
                )
                doc_rec, doc_errs = document_model.ingest_blob_as_document(
                    staged_docx,
                    dossier_id,
                    dossier_num,
                    transcript_filename,
                    {
                        "category": "autre",
                        "folder_id": interne_folder["id"],
                        "display_name": transcript_display_name,
                        "document_date": doc_date,
                    },
                    uid,
                    document_id=reserved_doc_id,
                    category_source=category_source,
                )
                if doc_errs or doc_rec is None:
                    if doc_errs and concurrency.WRITE_OUTCOME_UNCERTAIN_ERROR in doc_errs:
                        keep_staged_docx = True
                    return working_tr, doc_errs or ["Versement de la transcription .docx impossible."], warnings
                transcript_doc_id = doc_rec["id"]
            finally:
                if staged_docx is not None and not keep_staged_docx:
                    tr_model.purge_staged_blob(staged_docx)

            saved_tr, save_errs = tr_model.save_versement_progress(
                hearing_id,
                expected_etag=current_etag,
                dossier_id=dossier_id,
                versement_updates={"transcript_document_id": transcript_doc_id},
                now=ts,
            )
            if save_errs or saved_tr is None:
                return working_tr, save_errs or ["Suivi de versement document impossible."], warnings
            working_tr = saved_tr
            current_etag = concurrency.etag_of(working_tr)
            progress = dict(working_tr.get("versement") or {})

    # Étape C : Dépôt OneDrive facultatif dans {racine}/{dossier}/Interne
    od_note_id = str(progress.get("onedrive_note_item_id") or "")
    od_tx_id = str(progress.get("onedrive_transcript_item_id") or "")

    def _od_status_suffix(exc: Exception) -> str:
        if isinstance(exc, graph_courrier.CourrierGraphError) and exc.status_code is not None:
            return f" (HTTP {exc.status_code})"
        return ""

    if Config.courrier_configured():
        if not od_note_id:
            note_bytes = _note_docx_bytes(
                note_doc=note_doc, dossier=dossier, date_label=date_label
            )
            note_filename = _safe_docx_filename("Notes", date_iso, cleaned_title)
            try:
                od_res = courrier_service.deposer_docx_interne_onedrive(
                    user_id=uid,
                    dossier_numero=dossier_num,
                    dossier_titre=dossier_titre,
                    nom_fichier=note_filename,
                    contenu_docx=note_bytes,
                )
            except (graph_courrier.CourrierGraphError, graph_courrier.CourrierNotConfigured) as exc:
                log_unexpected("transcription: OneDrive note upload failed", hearing_id=hearing_id)
                return (
                    working_tr,
                    [
                        f"Versement Athéna enregistré, mais le dépôt des notes dans OneDrive a échoué{_od_status_suffix(exc)} — réessayez pour compléter OneDrive sans doublon."
                    ],
                    warnings,
                )
            od_note_id = str(od_res.get("item_id") or "")
            saved_tr, save_errs = tr_model.save_versement_progress(
                hearing_id,
                expected_etag=current_etag,
                versement_updates={"onedrive_note_item_id": od_note_id},
                now=ts,
            )
            if save_errs or saved_tr is None:
                return working_tr, save_errs or ["Suivi OneDrive impossible."], warnings
            working_tr = saved_tr
            current_etag = concurrency.etag_of(working_tr)

        if tr.get("has_transcript") and not od_tx_id:
            assert transcript_docx_bytes is not None
            try:
                od_tx_res = courrier_service.deposer_docx_interne_onedrive(
                    user_id=uid,
                    dossier_numero=dossier_num,
                    dossier_titre=dossier_titre,
                    nom_fichier=transcript_filename,
                    contenu_docx=transcript_docx_bytes,
                )
            except (graph_courrier.CourrierGraphError, graph_courrier.CourrierNotConfigured) as exc:
                log_unexpected(
                    "transcription: OneDrive transcript upload failed",
                    hearing_id=hearing_id,
                )
                return (
                    working_tr,
                    [
                        f"Versement Athéna et notes OneDrive enregistrés, mais le dépôt de la transcription dans OneDrive a échoué{_od_status_suffix(exc)} — réessayez pour compléter OneDrive sans doublon."
                    ],
                    warnings,
                )
            od_tx_id = str(od_tx_res.get("item_id") or "")

        onedrive_statut = "depose"
    else:
        onedrive_statut = "non_configure"
        warnings.append("Module Courrier (OneDrive) non configuré : versé dans Athéna seulement.")

    # Étape D : Clôture atomique & purge du WebVTT temporaire
    final_tr, final_errs = tr_model.save_versement_progress(
        hearing_id,
        expected_etag=current_etag,
        dossier_id=dossier_id,
        status=tr_model.STATUS_VERSEE,
        staged_vtt_path="",
        copilot_markdown=md_text,
        versement_updates={
            "note_id": note_id,
            "transcript_document_id": transcript_doc_id,
            "onedrive_note_item_id": od_note_id,
            "onedrive_transcript_item_id": od_tx_id,
            "onedrive_statut": onedrive_statut,
            "dossier_id": dossier_id,
            "dossier_file_number": dossier_num,
            "note_title": cleaned_title,
            "note_category": cleaned_category,
            "versed_at": ts,
        },
        now=ts,
    )
    if final_errs or final_tr is None:
        return working_tr, final_errs or ["Clôture du versement impossible."], warnings

    if staged_vtt_path:
        tr_model.purge_staged_blob(staged_vtt_path)
    if hearing is not None:
        _sync_hearing_transcript_status(hearing, "versee")

    log_bookings_event(
        "teams_transcription_versee",
        hearing_id=hearing_id,
        dossier_id=dossier_id,
        has_transcript=bool(tr.get("has_transcript")),
        onedrive_statut=onedrive_statut,
        via=provenance.current_via(),
    )
    return final_tr, [], warnings


def ecarter_transcription(
    hearing_id: str,
    *,
    expected_etag: str,
    now: Optional[datetime] = None,
) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Écarte une transcription préparée et purge immédiatement son WebVTT temporaire."""
    try:
        tr = tr_model.get_transcription_strict(hearing_id)
        hearing = hearing_model.get_hearing_strict(hearing_id)
    except Exception:
        log_unexpected("transcription: ecarter strict read failed", hearing_id=hearing_id)
        return None, [concurrency.READ_UNAVAILABLE_ERROR]

    if tr is None:
        return None, ["Transcription introuvable."]
    old_vtt_path = str(tr.get("staged_vtt_path") or "")

    doc, errs = tr_model.mark_transcription_ecartee(
        hearing_id, expected_etag=expected_etag, now=now
    )
    if errs or doc is None:
        return None, errs

    if old_vtt_path:
        tr_model.purge_staged_blob(old_vtt_path)
    if hearing is not None:
        _sync_hearing_transcript_status(hearing, "ecartee")

    log_bookings_event(
        "teams_transcription_ecartee",
        hearing_id=hearing_id,
        via=provenance.current_via(),
    )
    return doc, []


__all__ = [
    "COPILOT_WAIT_HOURS",
    "LECTURE_IMPOSSIBLE",
    "NO_TRANSCRIPT_GIVEUP_HOURS",
    "LectureImpossible",
    "ecarter_transcription",
    "lister_a_revoir_enrichies",
    "verifier_reunions_terminees",
    "verifier_une_reunion",
    "verser_transcription",
]
