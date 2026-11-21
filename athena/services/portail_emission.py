"""Émission et renvoi des invitations du portail client (spec L1 §6.2).

MAIN SERVICE ONLY — composes Firebase Auth (user + custom claim), the
« portail » invitation model, the Firebase email-link generation, and the
Graph outbound email. The portal service never imports this module.

Return convention: ``lien_manuel`` is non-empty when the sign-in link could
NOT be emailed (Graph unconfigured, or a send failure) — the invitation
exists and the juriste hands the link over manually. This keeps the feature
usable before the Entra registration (§12.7) is done.
"""

from datetime import datetime
import json
import logging
from typing import Optional

from firebase_admin import auth as fb_auth
from firebase_admin import storage
from flask import render_template

from client.config import PORTAIL_BUCKET, PORTAIL_HOST, PORTAIL_MAX_FILE_MB
from config import Config
from models import portail_invitation as inv_model
from models.integrations import sender_display_name
from models.rendez_vous_reglages import get_rendez_vous_reglages
from security import sanitize
from services import disponibilites
from tz import to_mtl
from utils import courriel
from utils.cabinet import cabinet_dict
from utils.format_fr import format_date_fr
from utils.graph import GraphError, GraphNotConfigured
from utils.logging_setup import log_portail_event
from utils.rendez_vous_defaults import AGENDA_MAX, DURATION_MAX, DURATION_MIN
from utils.validators import normalize_email

logger = logging.getLogger(__name__)


def _generer_lien(email: str, inv_id: str) -> str:
    """Firebase email-link (oobCode) — single-use, bound to the email.

    Raises on failure (e.g. the « email link » provider not enabled in the
    Firebase console, ops checklist §12.8).
    """
    acs = fb_auth.ActionCodeSettings(
        url=f"https://{PORTAIL_HOST}/entree?i={inv_id}",
        handle_code_in_app=True,
    )
    return fb_auth.generate_sign_in_with_email_link(email, acs)


def generer_lien_connexion(email: str, inv_id: str) -> str:
    """Public alias of :func:`_generer_lien` for task handlers."""
    return _generer_lien(email, inv_id)


def preparer_utilisateur_portail(email_n: str) -> list[str]:
    """Verify non-lawyer email, get/create Firebase user, and merge ``portail=True``."""
    if not email_n:
        return ["Adresse courriel invalide."]
    if email_n == (Config.AUTHORIZED_USER_EMAIL or "").strip().lower():
        return [
            "Impossible d'inviter le courriel du juriste : la revendication "
            "« portail » verrouillerait ce compte hors du service principal. "
            "Utilisez un alias pour les essais."
        ]
    try:
        try:
            user = fb_auth.get_user_by_email(email_n)
        except fb_auth.UserNotFoundError:
            user = fb_auth.create_user(email=email_n)
        claims = dict(user.custom_claims or {})
        if claims.get("portail") is not True:
            claims["portail"] = True
            fb_auth.set_custom_user_claims(user.uid, claims)
    except Exception:
        logger.exception("portail invitation firebase user setup failed")
        return ["Erreur lors de la préparation du compte client."]
    return []


# Objet + gabarit, par type d'invitation. Les objets se terminent par la
# désignation vue du client (décision 2026-07-29, symétrie assumée).
_COURRIELS = {
    "documents": (
        "Transmission de documents",
        "reception/_invitation_documents.html",
    ),
    "intake": (
        "Ouverture de votre dossier",
        "reception/_invitation_ouverture.html",
    ),
    "rdv": (
        "Planification de votre rendez-vous",
        "reception/_invitation_rdv.html",
    ),
}
_COURRIELS_RDV_ETAT = {
    "en_attente_courriel": (
        "Vérification de votre demande de rendez-vous",
        "reception/_invitation_rdv_verification.html",
    ),
    "a_confirmer": (
        "Gestion de votre rendez-vous",
        "reception/_invitation_rdv_gestion.html",
    ),
    "confirme": (
        "Gestion de votre rendez-vous",
        "reception/_invitation_rdv_gestion.html",
    ),
}


def _corps_invitation(invitation: dict, lien: str) -> tuple[str, str]:
    """Corps de l'invitation, selon le type et l'état RDV. Rend (objet, corps_html)."""
    type_ = invitation.get("type") or "documents"
    if type_ == "rdv":
        etat_rdv = invitation.get("etat_rdv") or ""
        prefixe, gabarit = _COURRIELS_RDV_ETAT.get(etat_rdv, _COURRIELS["rdv"])
    else:
        prefixe, gabarit = _COURRIELS.get(type_, _COURRIELS["documents"])
    display_label = invitation.get("display_label", "")
    label_phrase = display_label
    if label_phrase.lower().startswith("dossier "):
        label_phrase = label_phrase[len("dossier "):].strip() or display_label
    _cab = cabinet_dict()
    quand_rdv = ""
    slot_debut = invitation.get("slot_debut_utc")
    if slot_debut is not None:
        local = to_mtl(slot_debut)
        quand_rdv = f"{format_date_fr(local.date())} à {local.strftime('%H h %M')}"
    corps = render_template(
        gabarit,
        display_label=display_label,
        display_label_phrase=label_phrase,
        lien=lien,
        max_file_mb=PORTAIL_MAX_FILE_MB,
        date_expiration=format_date_fr(to_mtl(invitation["expires_at"]).date()),
        url_secours=f"https://{PORTAIL_HOST}/entree?i={invitation['id']}",
        quand_rdv=quand_rdv,
        duree_minutes=invitation.get("duree_minutes"),
        firm_name=_cab.get("nom", ""),
        firm_phone=_cab.get("telephone", ""),
        firm_email=_cab.get("courriel", ""),
    )
    return f"{prefixe} — {display_label}", corps


def _expedier(email: str, objet: str, corps_html: str, lien: str,
              invitation_id: str) -> str:
    """Send the invitation email; return the manual link on any failure."""
    try:
        courriel.envoyer(
            email, objet, corps_html, expediteur_nom=sender_display_name()
        )
    except GraphNotConfigured:
        log_portail_event(
            "courriel_echec", "refused",
            invitation_id=invitation_id, reason="graph_not_configured",
        )
        return lien
    except GraphError:
        logger.exception("portail invitation email send failed")
        log_portail_event(
            "courriel_echec", "failure",
            invitation_id=invitation_id, reason="graph_error",
        )
        return lien
    log_portail_event("courriel_envoye", invitation_id=invitation_id)
    return ""


def expedier_courriel(
    email: str, objet: str, corps_html: str, lien: str, invitation_id: str
) -> str:
    """Public alias of :func:`_expedier` for portal task handlers."""
    return _expedier(email, objet, corps_html, lien, invitation_id)


def envoyer_refus_rdv_portail(hearing: dict) -> tuple[bool, str]:
    """Send polite non-mandate refusal email for a refused public portal RDV.

    Lives here (not ``services/rendez_vous.py``) so MCP-reachable services
    never import ``utils.courriel``. Returns ``(sent_ok, warning_message)``.
    """
    client_email = str(hearing.get("client_email") or "").strip()
    if not (client_email and Config.graph_configured()):
        return False, (
            "Rendez-vous refusé dans Athéna, mais le courriel de refus "
            "n'a pas pu être envoyé (Graph non configuré ou adresse absente)."
        )
    display_label = (hearing.get("title") or "Rendez-vous").strip()
    start = hearing.get("start_datetime")
    quand_fr = "créneau demandé"
    if isinstance(start, datetime):
        local = to_mtl(start)
        quand_fr = f"{format_date_fr(local.date())} à {local.strftime('%H h %M')}"
    cab = cabinet_dict()
    objet = f"Suite à votre demande de rendez-vous — {display_label}"
    corps = render_template(
        "reception/_refus_rdv_portail.html",
        display_label=display_label,
        quand_fr=quand_fr,
        firm_name=cab.get("nom", ""),
        firm_phone=cab.get("telephone", ""),
        firm_email=cab.get("courriel", ""),
    )
    hid = str(hearing.get("id") or "")
    try:
        courriel.envoyer(
            client_email, objet, corps, expediteur_nom=sender_display_name()
        )
        return True, ""
    except (GraphError, GraphNotConfigured):
        logger.warning("portail rdv refusal email failed for %s", hid)
        return False, (
            "Rendez-vous refusé dans Athéna, mais le courriel de refus "
            "n'a pas pu être envoyé au demandeur."
        )
    except Exception:
        logger.exception("portail rdv refusal email raised unexpectedly")
        return False, (
            "Rendez-vous refusé dans Athéna, mais l'envoi du courriel "
            "de refus a échoué de façon inattendue."
        )


def corps_invitation_pour(invitation: dict, lien: str) -> tuple[str, str]:
    """Render `(objet, corps_html)` for *invitation* and one-time link."""
    return _corps_invitation(invitation, lien)


def _ecrire_agenda_rdv(inv_id: str, ordre_du_jour: str) -> None:
    """Store lawyer-edited RDV agenda strictly in quarantine bucket (never portail DB)."""
    blob = storage.bucket(PORTAIL_BUCKET).blob(f"submissions/{inv_id}/agenda.json")
    blob.upload_from_string(
        json.dumps({"ordre_du_jour": ordre_du_jour}, ensure_ascii=False),
        content_type="application/json",
    )


def emettre_invitation(
    type_: str,
    email: str,
    *,
    dossier_id: Optional[str] = None,
    partie_id: Optional[str] = None,
    client_name: str = "",
    display_label: str = "",
    jours: Optional[int] = None,
    prefill: Optional[dict] = None,
    type_rdv_cle: Optional[str] = None,
    duree_minutes: Optional[int] = None,
    ordre_du_jour: Optional[str] = None,
) -> tuple[Optional[dict], list[str], str]:
    """Create + send an invitation. Returns (invitation, errors, lien_manuel).

    ``prefill`` (L3, type « intake » seulement) doit venir de
    ``inv_model.prefill_depuis_partie`` — jamais d'un document de partie brut.
    """
    email_n = normalize_email(email or "")
    if not email_n:
        return None, ["Adresse courriel invalide."], ""

    user_errors = preparer_utilisateur_portail(email_n)
    if user_errors:
        return None, user_errors, ""

    extra_creer: dict = {}
    agenda_propre = ""
    est_rdv = type_ == "rdv"
    if est_rdv:
        if not Config.PORTAIL_RDV_ACTIF:
            return None, [
                "Les invitations de rendez-vous sur le portail sont désactivées."
            ], ""
        if not (Config.TEAMS_REUNIONS_ACTIF and Config.teams_configured()):
            return None, [
                "L'intégration Microsoft Teams n'est pas active ou complète "
                "(vérifiez Paramètres → Rendez-vous)."
            ], ""
        reglages = get_rendez_vous_reglages()
        types_par_cle = {
            str(t.get("cle") or "").strip(): t
            for t in (reglages.get("types_rdv") or ())
            if isinstance(t, dict)
        }
        cle = (type_rdv_cle or "").strip()
        if cle not in types_par_cle:
            return None, ["Type de rendez-vous inconnu ou non configuré."], ""
        cfg_type = types_par_cle[cle]
        duree = int(
            duree_minutes
            if duree_minutes is not None
            else (cfg_type.get("duree_minutes") or 30)
        )
        if duree < DURATION_MIN or duree > DURATION_MAX:
            return None, [
                f"La durée doit être comprise entre {DURATION_MIN} et {DURATION_MAX} minutes."
            ], ""
        agenda_src = (
            str(ordre_du_jour)
            if ordre_du_jour is not None and str(ordre_du_jour).strip()
            else str(cfg_type.get("ordre_du_jour") or "")
        )
        agenda_propre = agenda_src.replace("\r\n", "\n").replace("\r", "\n").strip()
        if (
            not agenda_propre
            or len(agenda_propre) > AGENDA_MAX
            or "<" in agenda_propre
            or ">" in agenda_propre
            or sanitize(agenda_propre, max_length=AGENDA_MAX) != agenda_propre
        ):
            return None, [
                "L'ordre du jour minuté est requis, sans chevrons (< ou >), "
                f"et d'au plus {AGENDA_MAX} caractères."
            ], ""
        if not (display_label or "").strip():
            display_label = str(cfg_type.get("libelle") or "Rendez-vous")
        if jours is None:
            horizon = int(reglages.get("horizon_jours") or 60)
            jours = min(max(horizon + 7, 14), 190)
        extra_creer = {
            "rdv_mode": "invite",
            "type_rdv_cle": cle,
            "type_rdv_libelle": str(cfg_type.get("libelle") or ""),
            "duree_minutes": duree,
            "etat_rdv": "a_choisir",
            "preavis_modification_heures": int(
                reglages.get("preavis_modification_heures") or 24
            ),
        }

    invitation, errors = inv_model.creer_invitation(
        type_, email_n,
        dossier_id=dossier_id, partie_id=partie_id, client_name=client_name,
        display_label=display_label, jours=jours, prefill=prefill,
        **extra_creer,
    )
    if errors or invitation is None:
        return None, errors or ["Erreur lors de la création de l'invitation."], ""

    if est_rdv:
        try:
            _ecrire_agenda_rdv(invitation["id"], agenda_propre)
        except Exception:
            logger.exception("portail rdv agenda write failed")
            inv_model.revoquer(invitation["id"])
            return None, [
                "Impossible d'enregistrer l'ordre du jour en quarantaine. "
                "Réessayez dans un moment."
            ], ""
        disponibilites.rafraichir_disponibilites_best_effort()

    try:
        lien = _generer_lien(email_n, invitation["id"])
    except Exception:
        logger.exception("portail sign-in link generation failed")
        inv_model.revoquer(invitation["id"])
        return None, [
            "Impossible de générer le lien de connexion. Vérifier que la "
            "connexion par lien courriel est activée dans la console Firebase "
            "(checklist §12.8)."
        ], ""

    objet, corps = _corps_invitation(invitation, lien)
    lien_manuel = _expedier(email_n, objet, corps, lien, invitation["id"])
    log_portail_event(
        "invitation_emise",
        invitation_id=invitation["id"], dossier_id=dossier_id,
        emailed=not bool(lien_manuel),
    )
    return invitation, [], lien_manuel


def renvoyer_invitation(inv_id: str) -> tuple[bool, str, str]:
    """Regenerate + resend the link (§6.2 steps 4-5).

    Allowed while the invitation may still receive files — statut
    envoyée/ouverte/**soumise** (D-2: a submitted lot stays open until the
    lawyer processes it, so a forgotten page is recoverable), not expired, and
    under the per-invitation resend cap (D-4). MUST agree with the portal's
    ``peut_relancer`` or a renvoi the portal enqueues gets dropped here with a
    200 while the client is told a link was sent. Returns (ok, message,
    lien_manuel).
    """
    invitation = inv_model.lire_invitation(inv_id)
    if invitation is None or not inv_model.peut_relancer(invitation):
        # Generic wording — this path also serves the portal-initiated
        # anti-enumeration flow, which must not learn why.
        return False, "Invitation invalide ou expirée.", ""

    try:
        lien = _generer_lien(invitation["email"], inv_id)
    except Exception:
        logger.exception("portail resend link generation failed")
        return False, "Impossible de générer le lien de connexion.", ""

    objet, corps = _corps_invitation(invitation, lien)
    lien_manuel = _expedier(invitation["email"], objet, corps, lien, inv_id)
    inv_model.incrementer_resend(inv_id)
    log_portail_event(
        "renvoi_demande", invitation_id=inv_id, emailed=not bool(lien_manuel)
    )
    return True, "", lien_manuel
