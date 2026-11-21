"""Bookings rendez-vous — Réception today, the MCP connector from lot 1b.

A « Bookings with me » reservation is imported during office hours by the
Bookings sync
(``routes/taches_bookings.py``) as a hearing gated behind
``confirmation == "à_confirmer"`` — invisible to DAV, the connector and the
dashboard until the lawyer decides. What lives HERE is that decision, so
that Réception and the connector (lot 1b, ``decide_rendez_vous``) cannot
make it two ways:

* **reading** what awaits a decision through a STRICT reader
  (``models.hearing.list_bookings_strict``) — never the fail-open
  ``list_hearings(include_unconfirmed=True)``, which answered a Firestore
  outage with « rien en attente ». A failed read raises
  :class:`LectureImpossible`; the caller says so;
* **confirming**: the event enters the calendar and DAV (the CTag of its
  collection is bumped). The linked contact is matched SERVER-SIDE on the
  requester's exact address — a caller never supplies a ``partie_id``.
  Confirming an import the CLIENT cancelled is refused;
* **refusing**: the Outlook meeting is cancelled through Graph — which
  notifies the client (the Bookings contract): an OUTBOUND effect. So the
  caller's version is checked BEFORE the Graph call (a page older than the
  sync's last update is refused with nothing done), the cancellation text
  is fixed (:data:`REFUS_MOTIF` — never a caller's prose), and the local
  write that follows the call is UNCONDITIONAL: once the client has been
  notified, the refusal is the truth, and no concurrency check may turn it
  into a retryable error — a retry would call Graph again on a cancelled
  event and tell the lawyer to « cancel manually » a meeting already
  cancelled. A local failure after a successful cancellation is therefore
  a SUCCESS carrying a warning (``graph_cancelled`` True), never an error.
  A refusal bumps no CTag: a pending import was never in DAV.

The intake-invitation email (L3's trigger (a)) and the portal refusal email
(Lot 2) stay OUT of this service (``routes/reception.rdv_confirmer`` /
``rdv_refuser``), so no MCP-reachable service ever imports ``utils.courriel``.

Provenance (``updated_via``) comes from the writer's context
(``models.provenance``) — the request's blueprint or the connector's
``writing_via`` — never from an argument (Architecture Rule 7). Every function returns ``(hearing, errors, report)``: errors
non-empty means NOTHING was WRITTEN, and — on every path but one — that
Outlook was not called either. The exception is :func:`refuser` when the
Graph cancellation itself FAILED and the local write then failed too:
``report["graph_attempted"]`` is True there. A retry is still the right
answer (the cancellation did not take, as far as anything here can know),
which is why that path is an error and not a warned success. The report
says what did happen, in flags a banner (or a tool payload) can state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from config import Config
from dav.sync import bump_ctag, collection_for, record_tombstone, remove_tombstone
from models import concurrency, provenance
from models import hearing as hearing_model
from models import partie as partie_model
from models import portail_invitation
from models.rendez_vous_reglages import get_rendez_vous_reglages
from security import sanitize
from services import disponibilites
from utils import cron_schedule, graph_calendrier, graph_teams
from utils.graph import GraphError, GraphNotConfigured
from utils.logging_setup import log_bookings_event, log_unexpected
from utils.rendez_vous_defaults import AGENDA_MAX, default_agenda_for_hearing_type

# The text Graph carries into the cancellation the CLIENT receives. Fixed:
# the connector's first message to a third party is a sentence the lawyer
# has read, never one a model composed.
REFUS_MOTIF = "Rendez-vous refusé par le juriste."

# What a caller with no banner of its own says on LectureImpossible (lot
# 1b's connector); Réception's template prints the same sentence.
LECTURE_IMPOSSIBLE = (
    "Lecture des rendez-vous impossible — réessayez dans un moment."
)
ANNULE_PAR_LE_CLIENT = (
    "Le client a annulé ce rendez-vous : il ne peut plus être confirmé. "
    "Retirez-le de la réception."
)
DEJA_REFUSE = "Ce rendez-vous a déjà été refusé."
DEJA_CONFIRME_NE_SE_REFUSE_PLUS = (
    "Ce rendez-vous est déjà confirmé : il ne peut plus être refusé d'ici. "
    "Rien n'a été fait."
)
SANS_COURRIEL = (
    "Ce rendez-vous ne porte aucun courriel : aucun contact ne peut y être "
    "lié. Rien n'a été confirmé."
)
AUCUN_CONTACT = (
    "Aucun contact ne porte le courriel de ce rendez-vous — rechargez la "
    "page. Rien n'a été confirmé."
)
CONTACTS_ILLISIBLES = (
    "Impossible de vérifier quel contact porte ce courriel. Rien n'a été "
    "confirmé — réessayez dans un moment."
)
ANNULATION_OUTLOOK_ECHOUEE = (
    "Rendez-vous refusé, mais la réunion n'a PAS pu être annulée côté "
    "Outlook — annulez-la manuellement pour prévenir le client."
)
# A LIVE import (« à_confirmer ») refused while Outlook cannot be reached at
# all — the Graph link is not configured, or the import carries no event id.
# Nothing was attempted, so nothing failed; but the client's meeting is still
# booked and nobody told him. « Rendez-vous refusé. » alone would read as the
# whole job done — the old route's silence, which lot 1b's connector would
# have relayed as a clean refusal.
ANNULATION_OUTLOOK_NON_TENTEE = (
    "Rendez-vous refusé dans Athéna, mais la réunion Outlook n'a PAS été "
    "annulée : la liaison avec Outlook n'est pas configurée, ou ce "
    "rendez-vous ne porte aucune référence d'événement — annulez-la "
    "manuellement pour prévenir le client."
)
ANNULATION_OUTLOOK_INCERTAINE = (
    "Rendez-vous refusé, mais l'annulation de la réunion côté Outlook a "
    "échoué de façon inattendue — vérifiez-la dans Outlook et annulez-la au "
    "besoin pour prévenir le client."
)
REFUS_NON_INSCRIT = (
    "La réunion Outlook a été annulée et le client notifié, mais le refus "
    "n'a pas pu être inscrit dans Athéna. Ne cliquez pas « Refuser » de "
    "nouveau : la prochaine synchronisation Bookings ("
    f"{cron_schedule.cadence_fr('bookings')}) le marquera "
    "« Annulé par le client » ; retirez-le alors."
)
SYNCHRO_NON_AVERTIE = (
    "Les appareils synchronisés n'ont pas pu en être avertis — ils le "
    "verront à la prochaine modification du calendrier."
)


class LectureImpossible(Exception):
    """A strict read failed: what awaits a decision is UNKNOWN.

    Never to be rendered as « rien en attente » — the whole point of the
    strict readers.
    """


# ── Lecture ─────────────────────────────────────────────────────────────


def index_parties_par_courriel() -> dict[str, dict]:
    """``{address: partie}`` — strict (:class:`LectureImpossible`)."""
    try:
        return partie_model.index_by_email_strict()
    except Exception as exc:
        log_unexpected("rendez-vous: contacts index unreadable")
        raise LectureImpossible() from exc


def partie_du_courriel(hearing: dict, index: dict[str, dict]) -> Optional[dict]:
    """The contact whose address is the requester's, exactly — or None."""
    courriel = str(hearing.get("client_email") or "").strip().lower()
    return index.get(courriel) if courriel else None


def lier_parties(hearings: list[dict], index: dict[str, dict]) -> None:
    """Attach ``_partie_id`` / ``_partie_nom`` to each rendez-vous — the
    contact :func:`confirmer` would link. Precomputed so a template stays
    logic-free."""
    for h in hearings:
        p = partie_du_courriel(h, index)
        h["_partie_id"] = p.get("id", "") if p else ""
        h["_partie_nom"] = partie_model.display_name(p) if p else ""


def _bookings(*, include_confirmed: bool) -> list[dict]:
    try:
        return hearing_model.list_bookings_strict(
            include_confirmed=include_confirmed
        )
    except Exception as exc:
        log_unexpected("rendez-vous: bookings read failed")
        raise LectureImpossible() from exc


def _rendez_vous_en_attente() -> list[dict]:
    try:
        return hearing_model.list_rendez_vous_strict(include_confirmed=False)
    except Exception as exc:
        log_unexpected("rendez-vous: strict read failed")
        raise LectureImpossible() from exc


def _chronologique(rows: list[dict]) -> list[dict]:
    """Oldest start first — the order the tab always showed (the reader is
    unordered; ``list_hearings`` used to sort for it)."""
    floor = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda h: h.get("start_datetime") or floor)
    return rows


def lister_en_attente() -> list[dict]:
    """The imports/requests awaiting a decision — ``à_confirmer`` and
    ``annulée_client`` — chronological, each carrying the contact
    :func:`confirmer` would link. Raises :class:`LectureImpossible`."""
    rows = _chronologique(_rendez_vous_en_attente())
    if rows:
        lier_parties(rows, index_parties_par_courriel())
    return rows


def lister_divergences() -> list[dict]:
    """Imports carrying an unseen ``bookings_divergence`` — a CONFIRMED
    event the client moved or cancelled on the Bookings side (the sync
    never overwrites a confirmed one) — chronological, like the pending
    list. Raises :class:`LectureImpossible`."""
    return _chronologique([
        h for h in _bookings(include_confirmed=True)
        if (h.get("bookings_divergence") or {}).get("motif")
        and not (h.get("bookings_divergence") or {}).get("vu")
    ])


# ── Décisions ───────────────────────────────────────────────────────────


def _read(hid: str) -> tuple[Optional[dict], list[str]]:
    """The stored import/request, read strictly. ``get_hearing`` is fail-open:
    a blip would have answered « introuvable » to a decision about a live
    reservation."""
    try:
        hearing = hearing_model.get_hearing_strict(hid)
    except Exception:
        log_unexpected("rendez-vous: hearing read failed")
        return None, [hearing_model.CONFIRMATION_READ_ERROR]
    if not hearing or hearing.get("source") not in hearing_model.RDV_SOURCES:
        return None, ["Rendez-vous introuvable."]
    return hearing, []


def _guard(hearing: dict, expected_etag: Optional[str]) -> str:
    """The version a decision commits against: the caller's when it gave
    one, else the one this service read (plan rule 3 — never a blind
    write when nothing external has happened yet)."""
    if expected_etag is not None:
        return expected_etag
    return concurrency.etag_of(hearing)


def _enter_dav(hearing: dict, hid: str) -> bool:
    """The confirmed event enters its DAV collection (``""`` → « Général »
    for a Bookings/portail import): drop any stale tombstone, bump."""
    try:
        name = collection_for(hearing.get("dossier_id"))
        remove_tombstone(name, hid)
        bump_ctag(name)
        return True
    except Exception:
        log_unexpected("rendez-vous: DAV bump failed after a confirmation",
                       hearing_id=hid)
        return False


def _leave_dav(hearing: dict, hid: str) -> bool:
    """A CONFIRMED event leaving the live set needs a tombstone — a bump
    alone leaves it on the phone (RFC 6578)."""
    try:
        name = collection_for(hearing.get("dossier_id"))
        record_tombstone(name, hid)
        bump_ctag(name)
        return True
    except Exception:
        log_unexpected("rendez-vous: DAV tombstone failed after a refusal",
                       hearing_id=hid)
        return False


def _maj_portail_rdv(hearing: dict, **fields) -> None:
    inv_id = (hearing.get("portail_invitation_id") or "").strip()
    if not inv_id:
        return
    try:
        portail_invitation.mettre_a_jour_rdv(inv_id, **fields)
    except Exception:
        log_unexpected("rendez-vous: portail invitation status sync failed",
                       invitation_id=inv_id)


def confirmer(
    hid: str,
    *,
    lier_partie: bool,
    expected_etag: Optional[str] = None,
    teams_agenda: Optional[str] = None,
) -> tuple[Optional[dict], list[str], dict]:
    """Confirm a pending Bookings or portal request: enters Calendar + DAV.

    For ``source == "portail"``, validates/normalizes *teams_agenda*, creates
    the Graph Teams meeting (sending the invite to ``client_email``), commits
    the confirmed status/URLs, syncs the portal invitation and availability
    snapshot, and bumps DavX5 CTag.
    """
    report = {"changed": False, "partie_liee": False, "partie_id": "",
              "dav_synced": False, "warning": ""}
    hearing, errors = _read(hid)
    if errors:
        return None, errors, report
    etat = hearing.get("confirmation") or ""
    if etat == "":
        report["dav_synced"] = True
        return hearing, [], report
    if not concurrency.matches(hearing, expected_etag):
        return None, [concurrency.STALE_ETAG_ERROR], report
    if etat == "annulée_client":
        return None, [ANNULE_PAR_LE_CLIENT], report
    if etat == "refusée":
        return None, [DEJA_REFUSE], report

    partie_id = None
    if lier_partie:
        if not str(hearing.get("client_email") or "").strip():
            return None, [SANS_COURRIEL], report
        try:
            partie = partie_du_courriel(hearing, index_parties_par_courriel())
        except LectureImpossible:
            return None, [CONTACTS_ILLISIBLES], report
        if not partie:
            return None, [AUCUN_CONTACT], report
        partie_id = partie["id"]

    is_portail = hearing.get("source") == "portail"
    extra_fields: Optional[dict] = None
    commit_etag: Optional[str] = _guard(hearing, expected_etag)
    warnings: list[str] = []

    if is_portail:
        if not (Config.TEAMS_REUNIONS_ACTIF and Config.teams_configured()):
            return None, [
                "La création automatique de réunions Microsoft Teams est "
                "désactivée ou incomplète (vérifiez Paramètres → Rendez-vous). "
                "Rien n'a été confirmé."
            ], report
        raw_agenda = (
            teams_agenda if teams_agenda is not None
            else (hearing.get("teams_agenda") or "")
        )
        agenda = raw_agenda.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not agenda:
            agenda = default_agenda_for_hearing_type(
                get_rendez_vous_reglages(), hearing.get("hearing_type", "")
            )
        if (
            len(agenda) > AGENDA_MAX
            or "<" in agenda
            or ">" in agenda
            or sanitize(agenda, max_length=AGENDA_MAX) != agenda
        ):
            return None, [
                "L'ordre du jour minuté contient des chevrons (< ou >) ou "
                f"dépasse {AGENDA_MAX} caractères."
            ], report

        client_email = str(hearing.get("client_email") or "").strip()
        invites = (client_email,) if client_email else ()
        try:
            teams_res = graph_teams.creer_reunion_teams(
                sujet=hearing.get("title") or "Consultation",
                debut_utc=hearing["start_datetime"],
                fin_utc=hearing["end_datetime"],
                ordre_du_jour=agenda,
                courriels_invites=invites,
                points_discussion=hearing.get("sujet_client", ""),
                transaction_id=f"pallas-confirm-{hid}",
            )
        except (GraphError, GraphNotConfigured):
            log_bookings_event(
                "teams_reunion_erreur_graph",
                "failure",
                hearing_id=hid,
                reason="create_failed",
            )
            return None, [
                "Impossible de créer la réunion Microsoft Teams auprès de "
                "Microsoft Graph (vérifiez Paramètres → Rendez-vous). "
                "Rien n'a été confirmé."
            ], report

        extra_fields = {
            "status": "confirmée",
            "conference_uri": teams_res["join_url"],
            "graph_event_id": teams_res["graph_event_id"],
            "graph_ical_uid": teams_res["graph_ical_uid"],
            "online_meeting_id": teams_res["online_meeting_id"],
            "teams_agenda": agenda,
            "attendee_emails": list(invites),
            "transcript_status": (
                "en_attente" if teams_res["online_meeting_id"] else ""
            ),
        }
        commit_etag = None  # Teams invite already committed externally
        if not teams_res["options_appliquees"]:
            warnings.append(
                "Les options de transcription et de salle d'attente n'ont "
                "pas pu être appliquées (vérifiez la politique Teams)."
            )

    written, errors, _previous = hearing_model.set_bookings_confirmation(
        hid,
        "",
        partie_id=partie_id,
        expected_etag=commit_etag,
        extra_fields=extra_fields,
    )
    if errors:
        return None, errors, report

    report.update(
        changed=True,
        partie_liee=partie_id is not None,
        partie_id=partie_id or "",
    )
    report["dav_synced"] = _enter_dav(written, hid)
    if not report["dav_synced"]:
        warnings.append(SYNCHRO_NON_AVERTIE)
    if warnings:
        report["warning"] = " ".join(warnings)
    if is_portail:
        log_bookings_event(
            "teams_reunion_creee",
            hearing_id=hid,
            options_appliquees=bool(
                extra_fields and not any("transcription" in w for w in warnings)
            ),
            invites_count=len(extra_fields.get("attendee_emails") or []) if extra_fields else 0,
        )
        _maj_portail_rdv(written, etat_rdv="confirme")
        disponibilites.rafraichir_disponibilites_best_effort()
    log_bookings_event(
        "reception_rdv_confirme",
        hearing_id=hid,
        partie_liee=report["partie_liee"],
        via=provenance.current_via(),
    )
    return written, [], report


def refuser(
    hid: str,
    *,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str], dict]:
    """Refuse a pending Bookings or portal import (or retire cancelled card)."""
    report = {"changed": False, "graph_attempted": False,
              "graph_cancelled": False, "local_written": False,
              "confirmed_meanwhile": False, "dav_left": False,
              "envoyer_refus_portail": False, "warning": ""}
    hearing, errors = _read(hid)
    if errors:
        return None, errors, report
    etat = hearing.get("confirmation") or ""
    if etat == "refusée":
        return hearing, [], report
    if not concurrency.matches(hearing, expected_etag):
        return None, [concurrency.STALE_ETAG_ERROR], report
    if etat == "":
        return None, [DEJA_CONFIRME_NE_SE_REFUSE_PLUS], report

    is_portail = hearing.get("source") == "portail"
    gid = hearing.get("graph_event_id")
    non_tente: Optional[str] = None

    if not is_portail:
        if etat == "à_confirmer" and not (gid and Config.bookings_configured()):
            non_tente = "not_configured" if gid else "sans_evenement"
            report["warning"] = ANNULATION_OUTLOOK_NON_TENTEE
        elif etat == "à_confirmer":
            report["graph_attempted"] = True
            try:
                graph_calendrier.annuler_reservation(gid, REFUS_MOTIF)
                report["graph_cancelled"] = True
            except (GraphError, GraphNotConfigured):
                log_unexpected("rendez-vous: graph cancel failed", hearing_id=hid)
                report["warning"] = ANNULATION_OUTLOOK_ECHOUEE
            except Exception:
                log_unexpected("rendez-vous: graph cancel raised unexpectedly",
                               hearing_id=hid)
                report["warning"] = ANNULATION_OUTLOOK_INCERTAINE

    guard = None if report["graph_attempted"] else _guard(hearing, expected_etag)
    written, errors, previous = hearing_model.set_bookings_confirmation(
        hid, "refusée", expected_etag=guard,
    )
    if errors and report["graph_cancelled"]:
        written, errors, previous = hearing_model.set_bookings_confirmation(
            hid, "refusée",
        )
    if errors:
        if report["graph_cancelled"]:
            report["warning"] = REFUS_NON_INSCRIT
            log_bookings_event(
                "reception_rdv_refuse", "failure", hearing_id=hid,
                graph_annule=True, reason="ecriture_locale",
                via=provenance.current_via(),
            )
            return None, [], report
        return None, errors, report

    report.update(changed=True, local_written=True)
    if previous == "":
        report["confirmed_meanwhile"] = True
        report["dav_left"] = _leave_dav(written, hid)
    if is_portail:
        report["envoyer_refus_portail"] = previous == "à_confirmer"
        _maj_portail_rdv(written, etat_rdv="refuse", statut="refusée")
        disponibilites.rafraichir_disponibilites_best_effort()
    failed = report["graph_attempted"] and not report["graph_cancelled"]
    reason = "graph_error" if failed else non_tente
    log_bookings_event(
        "reception_rdv_refuse", "refused" if reason else "success",
        hearing_id=hid, graph_annule=report["graph_cancelled"],
        reason=reason,
        via=provenance.current_via(),
    )
    return written, [], report
