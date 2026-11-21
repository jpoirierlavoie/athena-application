"""Gestionnaire Cloud Tasks + réconciliation cron du portail (spec L1 §8.3-8.4).

MACHINE blueprint — no @login_required, CSRF-exempt (main.py), reachable
only by App Engine-internal dispatches: the ``X-AppEngine-QueueName`` /
``X-Appengine-Cron`` headers are STRIPPED from all external traffic, so the
in-handler guards below are proof of origin (the before_request bypasses in
security.py/main.py rely on the same fact).

Retry semantics (§8.3): any raised exception → 5xx → the queue retries per
its policy; « nothing to do » branches return 200. Everything here is
idempotent — the single non-idempotent effect (the accusé email) is guarded
by the transactional test-and-set ``poser_accuse``.
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from firebase_admin import storage
from flask import Blueprint, abort, jsonify, render_template, request

from client.config import PORTAIL_BUCKET, PORTAIL_HOST, PORTAIL_QUEUE
from client.services import disponibilites as portail_dispo
from client.services import rdv_validation, taches
from config import Config
from dav.sync import bump_ctag, collection_for
from models import hearing as hearing_model
from models import portail_invitation as pi
from models.dossier import get_dossier
from models.integrations import sender_display_name
from models.partie import display_name, get_partie
from models.rendez_vous_reglages import get_rendez_vous_reglages
from services import disponibilites, portail_emission as emission
from tz import to_mtl
from utils import courriel, graph_teams
from utils.cabinet import cabinet_dict
from utils.format_fr import format_date_fr
from utils.graph import GraphError, GraphNotConfigured
from utils.logging_setup import log_bookings_event, log_portail_event
from utils.rendez_vous_defaults import default_agenda_for_hearing_type
from utils.template_fields import selected_address, selected_phone, uses_work_block
from utils.validators import format_phone_display, normalize_email

logger = logging.getLogger(__name__)

taches_portail_bp = Blueprint(
    "taches_portail", __name__, url_prefix="/taches/portail"
)

_HASH_CHUNK = 8 * 1024 * 1024  # 8 MiB streaming slices (§8.3.b)


def _bucket():
    return storage.bucket(PORTAIL_BUCKET)


# ── SHA-512 en flux ──────────────────────────────────────────────────────


def sha512_flux(fileobj) -> str:
    """Streaming SHA-512 over 8 MiB slices — never the whole file in memory."""
    h = hashlib.sha512()
    while True:
        chunk = fileobj.read(_HASH_CHUNK)
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()


def _sha512_blob(blob) -> str:
    with blob.open("rb") as f:
        return sha512_flux(f)


# ── Accusé de réception « bordereau » (spec A.2) ─────────────────────────

# Bloc DESTINATAIRE (cabinet) — composé depuis utils.cabinet.cabinet_dict(),
# l'UNE autorité des coordonnées du cabinet (audit 2026-08-26 : ce bloc les
# recopiait en dur, et son propre commentaire nommait la dette). Toujours
# SANS le cellulaire (décision utilisateur 2026-07-25 — cabinet_dict n'en a
# pas) ; « organisation » reste local, cabinet_dict n'a pas ce champ ; le
# bordereau garde son format de téléphone local (sans le préfixe « +1 »).
# Repli intégral quand les FIRM_* ne sont pas configurés (défaut "" —
# tests, poste local sans .env) : un bloc DESTINATAIRE VIDE sur un courriel
# client serait pire que ces littéraux ; en production app.yaml les fournit
# et l'autorité s'applique.
_CABINET_REPLI = {
    "nom": "Me Jason Poirier Lavoie",
    "organisation": "Poirier Lavoie, avocat",
    "adresse_lignes": [
        "9-4970, chemin de la Côte-des-Neiges",
        "Montréal (Québec) H3V 1A4",
    ],
    "telephone": "(514) 737-2525",
    "telecopieur": "(514) 737-6565",
}


def _composer_cabinet() -> dict:
    cab = cabinet_dict()
    if not cab["nom"]:
        return dict(_CABINET_REPLI)
    return {
        "nom": cab["nom"],
        "organisation": cab.get("organisation") or _CABINET_REPLI["organisation"],
        "adresse_lignes": [
            cab["adresse_civique"],
            f"{cab['ville']} ({cab['province']}) {cab['code_postal']}",
        ],
        "telephone": cab["telephone"],
        "telecopieur": cab["telecopieur"],
    }


# DELIBERATELY NOT a module-level constant. This module is imported
# unconditionally by main.py's create_app(), so evaluating the composition
# here would (a) put a Firestore read on every cold start, inside app
# construction, and (b) FREEZE the firm profile for the whole life of the
# gunicorn worker — editing the address would never reach the accusé email
# until a redeploy. tests/test_settings_cabinet.py pins the absence of a
# `_CABINET` attribute so this cannot be reintroduced by a later hand.


def _taille_lisible(octets: int) -> str:
    if octets >= 1024 * 1024:
        return f"{octets / (1024 * 1024):.1f} Mo".replace(".", ",")
    if octets >= 1024:
        return f"{round(octets / 1024)} ko"
    return f"{octets} o"


def _adresse_lignes(partie: dict) -> list[str]:
    """3-line letter address (mirror of templates/parties/_address_letter).

    The BLOCK is chosen by ``template_fields.selected_address`` — the one
    authority, shared with the gabarits: reading ``address_*`` directly left a
    personne morale's accusé de réception with a blank address (a company's
    address can only be entered under « Adresse professionnelle »). Only the
    three-line letter assembly stays here; template_fields has no such format.
    """
    adresse = selected_address(partie)

    def champ(suffixe: str) -> str:
        return adresse.get(suffixe, "")

    provinces = {
        "QC": "Québec", "ON": "Ontario", "BC": "Colombie-Britannique",
        "AB": "Alberta", "MB": "Manitoba", "SK": "Saskatchewan",
        "NB": "Nouveau-Brunswick", "NS": "Nouvelle-Écosse",
        "PE": "Île-du-Prince-Édouard", "NL": "Terre-Neuve-et-Labrador",
        "YT": "Yukon", "NT": "Territoires du Nord-Ouest", "NU": "Nunavut",
    }
    pays = {"CA": "Canada", "US": "États-Unis"}
    street, unit = champ("street"), champ("unit")
    city = champ("city")
    prov = provinces.get(champ("province").upper(), champ("province"))
    postal = champ("postal_code")
    country = pays.get(champ("country").upper(), champ("country"))

    lignes: list[str] = []
    ligne1 = f"{unit}-{street}" if unit and street else street
    if ligne1:
        lignes.append(ligne1)
    ligne2 = city
    if prov:
        ligne2 = f"{ligne2} ({prov})" if ligne2 else f"({prov})"
    if postal:
        ligne2 = f"{ligne2} {postal}".strip()
    if ligne2.strip():
        lignes.append(ligne2.strip())
    if country:
        lignes.append(country)
    return lignes


def _client_expediteur(invitation: dict, partie: dict | None) -> dict:
    """Build the SENDER block. With a party → full contact; else name+email."""
    courriel_client = invitation.get("email", "")
    if partie is None:
        return {
            "nom": invitation.get("client_name") or courriel_client,
            "organisation": "",
            "adresse_lignes": [],
            "telephone": "",
            "courriel": courriel_client,
        }
    telephone = selected_phone(partie)
    # A natural person's employer shows under their name only when the
    # accusé is addressed to the work block (template_fields — personal
    # first since 2026-10-30).
    organisation = (
        partie.get("organization")
        if partie.get("type") != "organization" and uses_work_block(partie)
        else ""
    )
    return {
        "nom": display_name(partie) or invitation.get("client_name") or courriel_client,
        "organisation": organisation or "",
        "adresse_lignes": _adresse_lignes(partie),
        "telephone": format_phone_display(telephone) if telephone else "",
        "courriel": partie.get("email") or courriel_client,
    }


def _corps_accuse(invitation: dict, fichiers: list[dict], quand_utc: datetime,
                  partie: dict | None, dossier: dict | None) -> tuple[str, str]:
    """Render the accusé « bordereau » email. Returns (objet, corps_html)."""
    display_label = invitation.get("display_label", "")
    objet = f"Accusé de réception — {display_label}"
    local = to_mtl(quand_utc)
    quand = f"{format_date_fr(local.date())} à {local.strftime('%H h %M')}"

    liste = [
        {
            "nom": f.get("name") or "",
            "taille": _taille_lisible(int(f.get("size_gcs") or 0)),
            "sha512": (f.get("sha512") or "").upper(),
        }
        for f in fichiers
        if f.get("sha512")
    ]
    dossier_ctx = None
    file_number = ""
    if dossier:
        dossier_ctx = {
            "court_file_number": dossier.get("court_file_number", ""),
            "title": dossier.get("title", ""),
        }
        file_number = dossier.get("file_number", "")

    corps = render_template(
        "reception/_accuse_bordereau.html",
        quand=quand,
        client=_client_expediteur(invitation, partie),
        cabinet=_composer_cabinet(),
        file_number=file_number,
        dossier=dossier_ctx,
        fichiers=liste,
    )
    return objet, corps


# ── Traitement d'une soumission (§8.3 « soumise ») ───────────────────────


def _construire_manifeste(bucket, inv_id: str, batch: str,
                          envelope: dict) -> dict:
    """(a)+(b): rapprocher fichiers déclarés ↔ objets présents, hacher."""
    prefix = f"submissions/{inv_id}/{batch}/"
    blobs = {b.name: b for b in bucket.list_blobs(prefix=prefix + "files/")}
    fichiers: list[dict] = []
    declares = set()
    for f in envelope.get("files", []):
        objet = str(f.get("objet") or "")
        declares.add(objet)
        entree = {
            "objet": objet,
            "name": str(f.get("name") or ""),
            "size_declared": int(f.get("size") or 0),
            "size_gcs": 0,
            "content_type": str(f.get("content_type") or ""),
            "sha512": None,
            "etat": "reçu",
            "divergence": None,
        }
        blob = blobs.get(objet)
        if blob is None:
            # Declared but never landed — recorded, never blocking (§8.3.a).
            entree["etat"] = "manquant"
            entree["divergence"] = "objet absent du stockage"
        else:
            entree["size_gcs"] = int(blob.size or 0)
            if entree["size_gcs"] != entree["size_declared"]:
                entree["divergence"] = "taille déclarée ≠ taille reçue"
            entree["sha512"] = _sha512_blob(blob)
        fichiers.append(entree)
    # Objects present but undeclared (a client that lost state mid-batch):
    # hashed and surfaced rather than silently ignored.
    for name, blob in sorted(blobs.items()):
        if name not in declares:
            fichiers.append({
                "objet": name,
                "name": name.rsplit("/", 1)[-1],
                "size_declared": 0,
                "size_gcs": int(blob.size or 0),
                "content_type": blob.content_type or "",
                "sha512": _sha512_blob(blob),
                "etat": "reçu",
                "divergence": "non déclaré dans l'enveloppe",
            })
    return {
        "batch": batch,
        "invitation_id": inv_id,
        "hashed_at": datetime.now(timezone.utc).isoformat(),
        # Recopiés de l'enveloppe pour que Réception (horodatage, IP/UA —
        # §9.2) et l'accusé (date de RÉCEPTION, pas de traitement) n'aient
        # jamais à relire envelope.json.
        "submitted_at": str(envelope.get("submitted_at") or ""),
        "http": envelope.get("http") or {},
        "files": fichiers,
        "etat_lot": "soumis",
    }


# A batch whose newest object stopped moving this long ago is considered
# abandoned rather than in flight (a 1 GiB upload on a slow link stays well
# inside this window; the cron runs every 15 minutes during office hours).
_ABANDON_APRES = timedelta(hours=2)


def _lot_abandonne(bucket, prefix: str) -> bool:
    """True when *prefix* holds files but no envelope, and has gone quiet.

    Never raises: the reconciliation sweep must finish even if one prefix
    misbehaves (the remaining lots still deserve their scan).
    """
    try:
        blobs = list(bucket.list_blobs(prefix=prefix + "files/"))
        if not blobs:
            return False
        recent = max(
            (b.updated for b in blobs if b.updated is not None), default=None
        )
        if recent is None:
            return False
        return datetime.now(timezone.utc) - recent > _ABANDON_APRES
    except Exception:
        logger.exception("reconciliation: abandoned-lot scan failed")
        return False


def _traiter_soumission(inv_id: str, batch: str) -> None:
    bucket = _bucket()
    prefix = f"submissions/{inv_id}/{batch}/"

    env_blob = bucket.blob(prefix + "envelope.json")
    if not env_blob.exists():
        # No envelope → the client never finalized; nothing durable exists.
        log_portail_event(
            "tache_recue", "refused",
            invitation_id=inv_id, batch=batch, reason="envelope_missing",
        )
        return

    manifest_blob = bucket.blob(prefix + "manifeste.json")
    if manifest_blob.exists():
        # Idempotence (§8.3.b / §13.m): hashes are computed exactly once.
        manifeste = json.loads(manifest_blob.download_as_bytes())
    else:
        envelope = json.loads(env_blob.download_as_bytes())
        manifeste = _construire_manifeste(bucket, inv_id, batch, envelope)
        manifest_blob.upload_from_string(
            json.dumps(manifeste, ensure_ascii=False),
            content_type="application/json",
        )
        log_portail_event(
            "manifeste_ecrit",
            invitation_id=inv_id, batch=batch,
            files_count=len(manifeste["files"]),
        )

    # (d) invitation: statut → soumise + append the submission if absent
    # (both idempotent). A failure here must RETRY — raise.
    recus = [f for f in manifeste["files"] if f.get("etat") != "manquant"]
    total = sum(int(f.get("size_gcs") or 0) for f in recus)
    if not pi.ajouter_soumission(inv_id, batch, len(recus), total):
        raise RuntimeError("portail invitation submission update failed")

    # (e) accusé — the transactional marker is the ONLY guard of the ONLY
    # non-idempotent effect. Once won, a send failure is logged at ERROR but
    # NOT raised: the marker is already set, so a retry could never resend —
    # it would only burn queue attempts on no-ops.
    if pi.poser_accuse(inv_id, batch):
        if not recus:
            # Aucun fichier n'a atteint GCS (lot abandonné en cours, ou
            # charge de finalisation falsifiée) : NE PAS expédier un accusé
            # attestant « réception des fichiers suivants » avec zéro fichier
            # (faux dans un contexte probatoire). Le marqueur poser_accuse a
            # été posé à dessein — il fait converger la réconciliation (les
            # objets ne réapparaîtront pas, le lot est clos côté client) au
            # lieu de la laisser ré-enfiler ce lot vide à chaque passage. Le
            # lot figure déjà en Réception (manifeste tout-« manquant » +
            # soumission files_count=0), où le juriste peut relancer le client.
            log_portail_event(
                "accuse_envoye", "refused",
                invitation_id=inv_id, batch=batch, reason="aucun_fichier_recu",
            )
            return
        invitation = pi.lire_invitation(inv_id) or {}
        # Sender (client) + judicial-file block resolved main-side; a lookup
        # failure degrades the block, never the accusé (best-effort).
        try:
            partie = get_partie(invitation.get("partie_id") or "")
        except Exception:
            partie = None
        try:
            dossier = get_dossier(invitation.get("dossier_id") or "")
        except Exception:
            dossier = None
        # L'accusé atteste la date de RÉCEPTION (l'enveloppe écrite = la
        # soumission acquise, §7.4) — jamais l'heure de traitement, qui peut
        # suivre de plusieurs minutes (file, réconciliation).
        try:
            quand = datetime.fromisoformat(manifeste.get("submitted_at") or "")
        except ValueError:
            quand = datetime.now(timezone.utc)
        # Le RENDU est aussi best-effort — et pour une raison plus grave que
        # les deux lectures ci-dessus : poser_accuse a DÉJÀ commis son
        # marqueur (le test-and-set transactionnel, unique garde de l'unique
        # effet non idempotent). Une exception ici remonterait en 5xx, Cloud
        # Tasks rejouerait, poser_accuse rendrait alors False — et l'accusé ne
        # partirait JAMAIS, sans même un courriel_echec, la réconciliation
        # jugeant le lot complet. L'au-plus-une-fois dégénérerait en
        # zéro-fois pour un bordereau à valeur probatoire. On dégrade donc en
        # échec de courriel, journalisé, comme le fait déjà le bloc d'envoi.
        try:
            objet, corps = _corps_accuse(invitation, recus, quand, partie, dossier)
        except Exception:
            logger.exception("accusé render failed")
            log_portail_event(
                "courriel_echec", "failure",
                invitation_id=inv_id, batch=batch, reason="rendu_accuse",
            )
            return
        try:
            courriel.envoyer(
                invitation.get("email", ""), objet, corps,
                expediteur_nom=sender_display_name(),
            )
            log_portail_event(
                "accuse_envoye", invitation_id=inv_id, batch=batch,
            )
        except GraphNotConfigured:
            log_portail_event(
                "courriel_echec", "refused",
                invitation_id=inv_id, batch=batch,
                reason="graph_not_configured",
            )
        except GraphError:
            logger.exception("accusé send failed")
            log_portail_event(
                "courriel_echec", "failure",
                invitation_id=inv_id, batch=batch, reason="graph_error",
            )


def _enveloppe_intake_valide(envelope: dict) -> bool:
    """Contrôle de forme minimal (§4.2) — le portail a déjà tout borné."""
    return (
        isinstance(envelope, dict)
        and envelope.get("type") == "intake"
        and isinstance(envelope.get("donnees"), dict)
        and isinstance(envelope.get("parties_adverses"), list)
        and isinstance(envelope.get("consentement"), dict)
    )


def _corps_confirmation_intake(quand_utc: datetime) -> tuple[str, str]:
    """Gabarit A.3 — confirmation de RÉCEPTION, jamais d'ouverture.

    Le libellé est juridiquement chargé : accuser réception d'un formulaire ne
    forme aucun mandat et n'ouvre aucun dossier. La dernière phrase le dit
    explicitement, et ne doit pas être adoucie. Aucune donnée du formulaire
    n'est reprise dans le courriel — la boîte du client n'est pas le lieu où
    faire circuler les noms qu'il vient de déclarer.
    """
    local = to_mtl(quand_utc)
    quand = f"{format_date_fr(local.date())} à {local.strftime('%H h %M')}"
    objet = "Confirmation de réception — formulaire d'ouverture"
    corps = render_template(
        "reception/_confirmation_intake.html",
        quand=quand, cabinet=_composer_cabinet(),
    )
    return objet, corps


def _traiter_intake(inv_id: str, batch: str, invitation: dict) -> None:
    """Branche « ouverture » du traitement d'une soumission (L3 §4.2).

    Aucune empreinte de fichier : l'enveloppe EST la soumission. Ce qui doit
    absolument arriver, c'est que ``soumissions[]`` ET ``accuses[batch]``
    finissent renseignés — la réconciliation ne connaît que ces deux critères,
    donc un lot qui n'en pose qu'un serait ré-enfilé à chaque passage, à
    perpétuité.
    """
    bucket = _bucket()
    prefix = f"submissions/{inv_id}/{batch}/"
    env_blob = bucket.blob(prefix + "envelope.json")
    if not env_blob.exists():
        log_portail_event(
            "tache_recue", "refused",
            invitation_id=inv_id, batch=batch, reason="envelope_missing",
        )
        return

    try:
        envelope = json.loads(env_blob.download_as_bytes())
    except Exception:
        logger.exception("intake envelope unreadable")
        envelope = {}

    lisible = _enveloppe_intake_valide(envelope)
    if not lisible:
        # Consigner FORT, mais poser quand même les deux marqueurs : sans eux
        # la réconciliation ré-enfilerait ce lot indéfiniment. Réception
        # l'affiche avec un bandeau, donc rien ne disparaît en silence.
        log_portail_event(
            "intake_soumis", "failure",
            invitation_id=inv_id, batch=batch, reason="enveloppe_malformee",
        )

    if not pi.ajouter_soumission(inv_id, batch, 0, 0):
        raise RuntimeError("portail invitation submission update failed")

    if pi.poser_accuse(inv_id, batch):
        if not lisible:
            log_portail_event(
                "intake_confirmation_envoyee", "refused",
                invitation_id=inv_id, batch=batch,
                reason="enveloppe_malformee",
            )
            return
        try:
            quand = datetime.fromisoformat(envelope.get("submitted_at") or "")
        except ValueError:
            quand = datetime.now(timezone.utc)
        objet, corps = _corps_confirmation_intake(quand)
        try:
            courriel.envoyer(
                invitation.get("email", ""), objet, corps,
                expediteur_nom=sender_display_name(),
            )
            log_portail_event(
                "intake_confirmation_envoyee",
                invitation_id=inv_id, batch=batch,
            )
        except GraphNotConfigured:
            log_portail_event(
                "courriel_echec", "refused",
                invitation_id=inv_id, batch=batch,
                reason="graph_not_configured",
            )
        except GraphError:
            logger.exception("intake confirmation send failed")
            log_portail_event(
                "courriel_echec", "failure",
                invitation_id=inv_id, batch=batch, reason="graph_error",
            )


# ── Rendez-vous portail (Lot 2) ──────────────────────────────────────────


_MAX_RDV_DEMANDES_24H = 3


def _read_json_blob(bucket, path: str) -> dict | None:
    blob = bucket.blob(path)
    if not blob.exists():
        return None
    try:
        data = json.loads(blob.download_as_bytes())
    except Exception:
        logger.exception("portail rdv json blob unreadable")
        return None
    return data if isinstance(data, dict) else None


def _write_json_blob(bucket, path: str, payload: dict) -> None:
    bucket.blob(path).upload_from_string(
        json.dumps(payload, ensure_ascii=False, default=str),
        content_type="application/json",
    )


def _hearing_type_for_rdv_cle(cle: str, reglages: dict | None = None) -> str:
    if reglages:
        for item in reglages.get("types_rdv") or ():
            if isinstance(item, dict) and str(item.get("cle") or "").strip() == cle:
                ht = str(item.get("hearing_type") or "").strip()
                if ht in hearing_model.VALID_HEARING_TYPES:
                    return ht
    if cle in hearing_model.VALID_HEARING_TYPES:
        return cle
    if cle.startswith("consultation"):
        return "consultation"
    return "conférence"


def _corps_rdv_email(
    gabarit: str, objet: str, inv: dict, *, slot_debut_dt: datetime | None
) -> tuple[str, str]:
    quand_fr = (
        portail_dispo.formater_jour_heure_mtl(slot_debut_dt)
        if slot_debut_dt is not None
        else "créneau convenu"
    )
    cab = cabinet_dict()
    corps = render_template(
        gabarit,
        display_label=inv.get("display_label") or inv.get("type_rdv_libelle") or "Rendez-vous",
        quand_fr=quand_fr,
        duree_minutes=inv.get("duree_minutes"),
        preavis_h=int(inv.get("preavis_modification_heures") or 24),
        url_secours=f"https://{PORTAIL_HOST}/entree?i={inv['id']}",
        firm_name=cab.get("nom", ""),
        firm_phone=cab.get("telephone", ""),
        firm_email=cab.get("courriel", ""),
    )
    return objet, corps


def _envoyer_courriel_rdv(
    email: str, objet: str, corps_html: str, *, inv_id: str, event_tag: str
) -> None:
    try:
        courriel.envoyer(
            email, objet, corps_html, expediteur_nom=sender_display_name()
        )
    except GraphNotConfigured:
        log_portail_event(
            "courriel_echec", "refused",
            invitation_id=inv_id, reason=f"{event_tag}_graph_not_configured",
        )
    except GraphError:
        logger.exception("%s email send failed", event_tag)
        log_portail_event(
            "courriel_echec", "failure",
            invitation_id=inv_id, reason=f"{event_tag}_graph_error",
        )


def _creer_audience_retenue_portail(
    inv_id: str,
    inv: dict,
    demande: dict,
    slot_debut_dt: datetime,
    slot_fin_dt: datetime,
) -> dict:
    type_cle = str(inv.get("type_rdv_cle") or demande.get("type_rdv_cle") or "")
    reglages = get_rendez_vous_reglages()
    htype = _hearing_type_for_rdv_cle(type_cle, reglages)
    default_agenda = default_agenda_for_hearing_type(reglages, htype)
    raw_phone = str(demande.get("telephone") or "").strip()
    phone = format_phone_display(raw_phone) or raw_phone
    adverses = [
        str(x).strip()
        for x in (demande.get("parties_adverses") or [])
        if str(x).strip()
    ]
    client_nom = str(demande.get("nom_complet") or inv.get("client_name") or "").strip()
    libelle = str(inv.get("type_rdv_libelle") or inv.get("display_label") or "Consultation").strip()
    title = f"{libelle} — {client_nom}" if client_nom else libelle
    created, errors = hearing_model.create_hearing({
        "title": title,
        "hearing_type": htype,
        "start_datetime": slot_debut_dt,
        "end_datetime": slot_fin_dt,
        "all_day": False,
        "modalite": "visioconférence",
        "status": "à_confirmer",
        "source": "portail",
        "confirmation": "à_confirmer",
        "portail_invitation_id": inv_id,
        "type_rdv_cle": type_cle,
        "client_nom": client_nom,
        "client_email": inv.get("email", ""),
        "client_telephone": phone,
        "parties_adverses": adverses,
        "sujet_client": str(demande.get("sujet") or ""),
        "teams_agenda": default_agenda,
    })
    if errors or created is None:
        raise RuntimeError("portail rdv held hearing create failed")
    return created


def _traiter_rdv_demande(req_id: str, batch: str) -> None:
    bucket = _bucket()
    payload = _read_json_blob(bucket, f"submissions/_rdv_demande_{batch or req_id}.json")
    if payload is None:
        log_portail_event(
            "demande_rdv_ignoree", "refused",
            invitation_id=req_id, reason="blob_missing",
        )
        return

    inv = pi.lire_invitation_strict(req_id)
    if inv is None:
        if not Config.PORTAIL_RDV_ACTIF:
            log_portail_event(
                "demande_rdv_ignoree", "refused",
                invitation_id=req_id, reason="disabled",
            )
            return
        email = normalize_email(str(payload.get("courriel") or ""))
        if not email:
            log_portail_event(
                "demande_rdv_ignoree", "refused",
                invitation_id=req_id, reason="invalid_email",
            )
            return
        owner_email = normalize_email(Config.AUTHORIZED_USER_EMAIL or "")
        if owner_email and email == owner_email:
            log_portail_event(
                "demande_rdv_ignoree", "refused",
                invitation_id=req_id, reason="owner_email",
            )
            return
        depuis = datetime.now(timezone.utc) - timedelta(hours=24)
        if pi.compter_demandes_rdv_recentes_par_courriel(email, depuis) >= _MAX_RDV_DEMANDES_24H:
            log_portail_event(
                "demande_rdv_ignoree", "refused",
                invitation_id=req_id, reason="rate_cap",
            )
            return

        reglages = get_rendez_vous_reglages()
        type_cle = str(payload.get("type_rdv_cle") or "").strip()
        types_publics = {
            str(t.get("cle") or "").strip(): t
            for t in (reglages.get("types_rdv") or ())
            if isinstance(t, dict) and t.get("public")
        }
        cfg_type = types_publics.get(type_cle)
        if cfg_type is None:
            log_portail_event(
                "demande_rdv_ignoree", "refused",
                invitation_id=req_id, reason="type_invalid",
            )
            return
        slot_debut_dt = rdv_validation._parse_utc(payload.get("slot_debut_utc"))
        if slot_debut_dt is None:
            log_portail_event(
                "demande_rdv_ignoree", "refused",
                invitation_id=req_id, reason="slot_invalid",
            )
            return
        duree = int(cfg_type.get("duree_minutes") or payload.get("duree_minutes") or 30)
        slot_fin_dt = slot_debut_dt + timedelta(minutes=duree)
        preavis_h = int(reglages.get("preavis_modification_heures") or 24)
        type_libelle = str(
            cfg_type.get("libelle") or payload.get("type_rdv_libelle") or "Consultation"
        )

        user_errors = emission.preparer_utilisateur_portail(email)
        if user_errors:
            raise RuntimeError("portail rdv user preparation failed")

        _write_json_blob(bucket, f"submissions/{req_id}/demande.json", payload)
        inv, errs = pi.creer_invitation(
            "rdv",
            email,
            inv_id=req_id,
            client_name=str(payload.get("nom_complet") or ""),
            display_label=type_libelle,
            jours=2,
            rdv_mode="public",
            type_rdv_cle=type_cle,
            type_rdv_libelle=type_libelle,
            duree_minutes=duree,
            etat_rdv="en_attente_courriel",
            slot_debut_utc=slot_debut_dt,
            slot_fin_utc=slot_fin_dt,
            preavis_modification_heures=preavis_h,
        )
        if errs or inv is None:
            if "existe déjà" in "".join(errs):
                inv = pi.lire_invitation_strict(req_id)
            else:
                raise RuntimeError("portail rdv invitation create failed")

    if inv is not None and not (inv.get("accuses") or {}).get("verif"):
        lien = emission.generer_lien_connexion(inv["email"], req_id)
        objet, corps = emission.corps_invitation_pour(inv, lien)
        lien_manuel = emission.expedier_courriel(inv["email"], objet, corps, lien, req_id)
        if lien_manuel and Config.graph_configured():
            raise RuntimeError("rdv verification email send failed")
        pi.poser_accuse(req_id, "verif")


def _traiter_rdv_retenir(inv_id: str) -> None:
    inv = pi.lire_invitation_strict(inv_id)
    if inv is None or inv.get("type") != "rdv" or inv.get("statut") == "révoquée":
        return
    pi.marquer_ouverte(inv_id)
    if inv.get("etat_rdv") != "en_attente_courriel" or inv.get("hearing_id"):
        return

    slot_debut_dt = rdv_validation._parse_utc(inv.get("slot_debut_utc"))
    duree = int(inv.get("duree_minutes") or 30)
    if slot_debut_dt is None:
        return
    slot_fin_dt = slot_debut_dt + timedelta(minutes=duree)

    libre, motif = disponibilites.verifier_creneau_libre_strict(slot_debut_dt, duree)
    if motif == "erreur_lecture":
        raise RuntimeError("portail rdv availability check failed")
    now = datetime.now(timezone.utc)
    if not libre:
        pi.mettre_a_jour_rdv(
            inv_id,
            statut="ouverte",
            etat_rdv="creneau_pris",
            expires_at=now + timedelta(days=7),
        )
        log_portail_event(
            "rdv_creneau_pris", "refused",
            invitation_id=inv_id, reason="slot_taken",
        )
        return

    demande = _read_json_blob(_bucket(), f"submissions/{inv_id}/demande.json") or {}
    created = _creer_audience_retenue_portail(inv_id, inv, demande, slot_debut_dt, slot_fin_dt)
    exp = max(slot_fin_dt + timedelta(days=1), now + timedelta(days=7))
    if not pi.mettre_a_jour_rdv(
        inv_id,
        statut="ouverte",
        etat_rdv="a_confirmer",
        hearing_id=created["id"],
        expires_at=exp,
    ):
        raise RuntimeError("portail rdv invitation hold update failed")

    disponibilites.rafraichir_disponibilites_best_effort()
    log_portail_event("rdv_creneau_retenu", invitation_id=inv_id)
    if pi.poser_accuse(inv_id, "retenu"):
        label = inv.get("display_label") or inv.get("type_rdv_libelle") or "Consultation"
        objet, corps = _corps_rdv_email(
            "reception/_accuse_rdv_public.html",
            f"Demande de rendez-vous reçue — {label}",
            inv,
            slot_debut_dt=slot_debut_dt,
        )
        _envoyer_courriel_rdv(
            inv.get("email", ""), objet, corps, inv_id=inv_id, event_tag="rdv_retenu"
        )


def _traiter_rdv_reserver(inv_id: str, batch: str) -> None:
    inv = pi.lire_invitation_strict(inv_id)
    if inv is None or inv.get("type") != "rdv" or inv.get("statut") == "révoquée":
        return
    pi.marquer_ouverte(inv_id)
    if inv.get("etat_rdv") not in ("a_choisir", "creneau_pris") or inv.get("hearing_id"):
        return

    bucket = _bucket()
    payload = _read_json_blob(bucket, f"submissions/{inv_id}/rdv_{batch}.json")
    if payload is None:
        return
    slot_debut_dt = rdv_validation._parse_utc(payload.get("slot_debut_utc"))
    if slot_debut_dt is None:
        return
    duree = int(inv.get("duree_minutes") or payload.get("duree_minutes") or 30)
    slot_fin_dt = slot_debut_dt + timedelta(minutes=duree)

    libre, motif = disponibilites.verifier_creneau_libre_strict(slot_debut_dt, duree)
    if motif == "erreur_lecture":
        raise RuntimeError("portail rdv availability check failed")
    now = datetime.now(timezone.utc)
    if not libre:
        pi.mettre_a_jour_rdv(
            inv_id,
            statut="ouverte",
            etat_rdv="creneau_pris",
            expires_at=now + timedelta(days=7),
        )
        log_portail_event(
            "rdv_creneau_pris", "refused",
            invitation_id=inv_id, batch=batch, reason="slot_taken",
        )
        return

    exp = max(slot_fin_dt + timedelta(days=1), now + timedelta(days=7))
    if inv.get("rdv_mode") == "public":
        demande = _read_json_blob(bucket, f"submissions/{inv_id}/demande.json") or {}
        created = _creer_audience_retenue_portail(inv_id, inv, demande, slot_debut_dt, slot_fin_dt)
        if not pi.mettre_a_jour_rdv(
            inv_id,
            statut="ouverte",
            etat_rdv="a_confirmer",
            hearing_id=created["id"],
            slot_debut_utc=slot_debut_dt,
            slot_fin_utc=slot_fin_dt,
            expires_at=exp,
        ):
            raise RuntimeError("portail rdv invitation update failed")
        disponibilites.rafraichir_disponibilites_best_effort()
        log_portail_event("rdv_creneau_retenu", invitation_id=inv_id, batch=batch)
        if pi.poser_accuse(inv_id, f"retenu_{batch}"):
            label = inv.get("display_label") or inv.get("type_rdv_libelle") or "Consultation"
            objet, corps = _corps_rdv_email(
                "reception/_accuse_rdv_public.html",
                f"Demande de rendez-vous reçue — {label}",
                inv,
                slot_debut_dt=slot_debut_dt,
            )
            _envoyer_courriel_rdv(
                inv.get("email", ""), objet, corps, inv_id=inv_id, event_tag="rdv_reserver"
            )
        return

    # Mode « invite » : confirmation immédiate + réunion Teams + DavX5
    agenda_blob = _read_json_blob(bucket, f"submissions/{inv_id}/agenda.json") or {}
    type_cle = str(inv.get("type_rdv_cle") or "")
    reglages = get_rendez_vous_reglages()
    htype = _hearing_type_for_rdv_cle(type_cle, reglages)
    agenda = str(agenda_blob.get("ordre_du_jour") or "").strip() or (
        default_agenda_for_hearing_type(reglages, htype)
    )
    points = str(payload.get("points_discussion") or "").strip()
    dossier = get_dossier(inv["dossier_id"]) if inv.get("dossier_id") else None
    partie = get_partie(inv["partie_id"]) if inv.get("partie_id") else None
    client_nom = display_name(partie) if partie else str(inv.get("client_name") or "").strip()
    libelle = str(inv.get("type_rdv_libelle") or inv.get("display_label") or "Rendez-vous").strip()
    title = f"{libelle} — {client_nom}" if client_nom else libelle

    join_url = ""
    graph_event_id = ""
    graph_ical_uid = ""
    online_meeting_id = ""
    options_appliquees = False
    if Config.TEAMS_REUNIONS_ACTIF and Config.teams_configured():
        try:
            teams_res = graph_teams.creer_reunion_teams(
                sujet=title,
                debut_utc=slot_debut_dt,
                fin_utc=slot_fin_dt,
                ordre_du_jour=agenda,
                courriels_invites=(inv.get("email", ""),),
                points_discussion=points,
                transaction_id=f"pallas-invite-{inv_id}-{batch}",
            )
        except (GraphError, GraphNotConfigured):
            log_bookings_event(
                "teams_reunion_erreur_graph",
                "failure",
                reason="invite_create_failed",
            )
            raise RuntimeError("portail rdv Teams meeting create failed")
        join_url = teams_res["join_url"]
        graph_event_id = teams_res["graph_event_id"]
        graph_ical_uid = teams_res["graph_ical_uid"]
        online_meeting_id = teams_res["online_meeting_id"]
        options_appliquees = bool(teams_res["options_appliquees"])
    elif Config.ENV == "production":
        raise RuntimeError("Microsoft Teams integration required in production")

    hearing_payload = {
        "dossier_id": dossier["id"] if dossier else "",
        "dossier_file_number": dossier.get("file_number", "") if dossier else "",
        "dossier_title": dossier.get("title", "") if dossier else "",
        "title": title,
        "hearing_type": htype,
        "start_datetime": slot_debut_dt,
        "end_datetime": slot_fin_dt,
        "all_day": False,
        "modalite": "visioconférence",
        "status": "confirmée",
        "source": "portail",
        "confirmation": "",
        "conference_uri": join_url,
        "graph_event_id": graph_event_id,
        "graph_ical_uid": graph_ical_uid,
        "online_meeting_id": online_meeting_id,
        "teams_agenda": agenda,
        "attendee_emails": [inv.get("email", "")] if inv.get("email") else [],
        "transcript_status": "en_attente" if online_meeting_id else "",
        "portail_invitation_id": inv_id,
        "type_rdv_cle": type_cle,
        "partie_id": inv.get("partie_id") or "",
        "client_nom": client_nom,
        "client_email": inv.get("email", ""),
        "sujet_client": points,
    }
    created, errors = hearing_model.create_hearing(hearing_payload)
    if errors or created is None:
        raise RuntimeError("portail rdv confirmed hearing create failed")

    bump_ctag(collection_for(created.get("dossier_id")))
    if not pi.mettre_a_jour_rdv(
        inv_id,
        statut="ouverte",
        etat_rdv="confirme",
        hearing_id=created["id"],
        slot_debut_utc=slot_debut_dt,
        slot_fin_utc=slot_fin_dt,
        dernier_report_echec="",
        expires_at=exp,
    ):
        raise RuntimeError("portail rdv invitation confirm update failed")

    disponibilites.rafraichir_disponibilites_best_effort()
    log_bookings_event(
        "teams_reunion_creee",
        hearing_id=created["id"],
        options_appliquees=options_appliquees,
        invites_count=len(hearing_payload["attendee_emails"]),
    )
    log_portail_event("rdv_invite_confirme", invitation_id=inv_id, batch=batch)
    if pi.poser_accuse(inv_id, f"conf_{batch}"):
        objet, corps = _corps_rdv_email(
            "reception/_confirmation_rdv_portail.html",
            f"Rendez-vous confirmé — {libelle}",
            inv,
            slot_debut_dt=slot_debut_dt,
        )
        _envoyer_courriel_rdv(
            inv.get("email", ""), objet, corps, inv_id=inv_id, event_tag="rdv_confirme"
        )


def _traiter_rdv_reporter(inv_id: str, batch: str) -> None:
    inv = pi.lire_invitation_strict(inv_id)
    if (
        inv is None
        or inv.get("type") != "rdv"
        or inv.get("statut") == "révoquée"
        or inv.get("etat_rdv") not in ("a_confirmer", "confirme")
    ):
        return
    bucket = _bucket()
    payload = _read_json_blob(bucket, f"submissions/{inv_id}/rdv_{batch}.json")
    if payload is None:
        return
    new_debut_dt = rdv_validation._parse_utc(payload.get("slot_debut_utc"))
    if new_debut_dt is None:
        return
    duree = int(inv.get("duree_minutes") or 30)
    new_fin_dt = new_debut_dt + timedelta(minutes=duree)

    hearing_id = str(inv.get("hearing_id") or "").strip()
    hearing = hearing_model.get_hearing_strict(hearing_id) if hearing_id else None
    if hearing is None:
        return

    cur_start = rdv_validation._parse_utc(hearing.get("start_datetime"))
    if cur_start == new_debut_dt:
        return

    if not rdv_validation.est_modifiable_avant_preavis(inv):
        pi.mettre_a_jour_rdv(
            inv_id,
            dernier_report_echec="Le délai minimal pour déplacer ce rendez-vous est dépassé.",
        )
        return

    cur_end = rdv_validation._parse_utc(hearing.get("end_datetime"))
    exclure_graph = (
        (cur_start, cur_end)
        if (hearing.get("graph_event_id") and cur_start and cur_end)
        else None
    )
    libre, motif = disponibilites.verifier_creneau_libre_strict(
        new_debut_dt,
        duree,
        exclure_hearing_id=hearing["id"],
        exclure_graph_slot=exclure_graph,
    )
    if motif == "erreur_lecture":
        raise RuntimeError("portail rdv availability check failed")
    if not libre:
        pi.mettre_a_jour_rdv(
            inv_id,
            dernier_report_echec=(
                "Ce créneau vient d'être pris. Votre rendez-vous initial est maintenu."
            ),
        )
        log_portail_event(
            "rdv_creneau_pris", "refused",
            invitation_id=inv_id, batch=batch, reason="reschedule_slot_taken",
        )
        return

    if (
        hearing.get("confirmation") == ""
        and hearing.get("graph_event_id")
        and Config.teams_configured()
    ):
        try:
            graph_teams.modifier_reunion_teams(
                hearing["graph_event_id"],
                sujet=hearing.get("title") or "Rendez-vous",
                debut_utc=new_debut_dt,
                fin_utc=new_fin_dt,
            )
        except (GraphError, GraphNotConfigured):
            log_bookings_event(
                "teams_reunion_erreur_graph",
                "failure",
                hearing_id=hearing["id"],
                reason="reschedule_failed",
            )
            raise RuntimeError("portail rdv Teams meeting update failed")
        log_bookings_event("teams_reunion_modifiee", hearing_id=hearing["id"])

    written, errs = hearing_model.update_hearing(
        hearing["id"],
        {"start_datetime": new_debut_dt, "end_datetime": new_fin_dt},
    )
    if errs or written is None:
        raise RuntimeError("portail rdv hearing reschedule update failed")

    if hearing.get("confirmation") == "":
        bump_ctag(collection_for(written.get("dossier_id")))

    exp = max(
        new_fin_dt + timedelta(days=1),
        datetime.now(timezone.utc) + timedelta(days=7),
    )
    if not pi.mettre_a_jour_rdv(
        inv_id,
        slot_debut_utc=new_debut_dt,
        slot_fin_utc=new_fin_dt,
        dernier_report_echec="",
        expires_at=exp,
    ):
        raise RuntimeError("portail rdv invitation reschedule update failed")

    disponibilites.rafraichir_disponibilites_best_effort()
    log_portail_event("rdv_reporte", invitation_id=inv_id, batch=batch)
    if pi.poser_accuse(inv_id, f"report_{batch}"):
        label = inv.get("display_label") or inv.get("type_rdv_libelle") or "Rendez-vous"
        if inv.get("etat_rdv") == "confirme":
            gabarit = "reception/_confirmation_rdv_portail.html"
            objet = f"Rendez-vous déplacé (confirmé) — {label}"
        else:
            gabarit = "reception/_accuse_rdv_public.html"
            objet = f"Créneau mis à jour (en attente de confirmation) — {label}"
        objet, corps = _corps_rdv_email(
            gabarit, objet, inv, slot_debut_dt=new_debut_dt
        )
        _envoyer_courriel_rdv(
            inv.get("email", ""), objet, corps, inv_id=inv_id, event_tag="rdv_reporter"
        )


def _traiter_rdv_annuler(inv_id: str, batch: str) -> None:
    inv = pi.lire_invitation_strict(inv_id)
    if (
        inv is None
        or inv.get("type") != "rdv"
        or inv.get("statut") == "révoquée"
        or inv.get("etat_rdv") == "annule"
    ):
        return
    if inv.get("etat_rdv") not in ("a_confirmer", "confirme"):
        return
    if not rdv_validation.est_modifiable_avant_preavis(inv):
        pi.mettre_a_jour_rdv(
            inv_id,
            dernier_report_echec="Le délai minimal d'annulation en ligne est dépassé.",
        )
        return

    hearing_id = str(inv.get("hearing_id") or "").strip()
    hearing = hearing_model.get_hearing_strict(hearing_id) if hearing_id else None
    slot_dt = rdv_validation._parse_utc(inv.get("slot_debut_utc"))
    if slot_dt is None and hearing is not None:
        slot_dt = rdv_validation._parse_utc(hearing.get("start_datetime"))

    if hearing is not None:
        if hearing.get("confirmation") == "":
            if hearing.get("graph_event_id") and Config.teams_configured():
                try:
                    graph_teams.annuler_reunion_teams(
                        hearing["graph_event_id"],
                        motif="Rendez-vous annulé par le client via le portail.",
                    )
                except (GraphError, GraphNotConfigured):
                    log_bookings_event(
                        "teams_reunion_erreur_graph",
                        "failure",
                        hearing_id=hearing["id"],
                        reason="cancel_failed",
                    )
                    raise RuntimeError("portail rdv Teams meeting cancel failed")
                log_bookings_event("teams_reunion_annulee", hearing_id=hearing["id"])
            written, errs = hearing_model.update_hearing(
                hearing["id"], {"status": "annulée"}
            )
            if errs or written is None:
                raise RuntimeError("portail rdv hearing cancel update failed")
            bump_ctag(collection_for(written.get("dossier_id")))
        else:
            written, errs = hearing_model.update_hearing(
                hearing["id"],
                {"status": "annulée"},
                server_fields={"confirmation": "annulée_client"},
            )
            if errs or written is None:
                raise RuntimeError("portail rdv unconfirmed hearing cancel failed")

    if not pi.mettre_a_jour_rdv(
        inv_id,
        statut="traitée",
        etat_rdv="annule",
        dernier_report_echec="",
    ):
        raise RuntimeError("portail rdv invitation cancel update failed")

    disponibilites.rafraichir_disponibilites_best_effort()
    log_portail_event("rdv_annule_client", invitation_id=inv_id, batch=batch)
    if pi.poser_accuse(inv_id, f"annul_{batch or '1'}"):
        label = inv.get("display_label") or inv.get("type_rdv_libelle") or "Rendez-vous"
        objet, corps = _corps_rdv_email(
            "reception/_annulation_rdv_portail.html",
            f"Confirmation d'annulation — {label}",
            inv,
            slot_debut_dt=slot_dt,
        )
        _envoyer_courriel_rdv(
            inv.get("email", ""), objet, corps, inv_id=inv_id, event_tag="rdv_annuler"
        )


# ── POST /taches/portail/evenement ───────────────────────────────────────


@taches_portail_bp.post("/evenement")
def evenement():
    # Proof of origin: App Engine strips X-AppEngine-* from ALL external
    # traffic; only a genuine dispatch from OUR queue carries this value.
    if request.headers.get("X-AppEngine-QueueName") != PORTAIL_QUEUE:
        abort(403)

    retry_count = request.headers.get("X-AppEngine-TaskRetryCount", "0")
    payload = request.get_json(silent=True) or {}
    event = payload.get("event")
    inv_id = str(payload.get("invitation_id") or "")
    batch = payload.get("batch")

    log_portail_event(
        "tache_recue", invitation_id=inv_id or None, batch=batch,
        evenement=str(event), retry_count=retry_count,
    )

    # A malformed payload is a bug, not a transient: retrying it ten times
    # would change nothing. 200 + WARNING instead of a retry storm.
    if event not in taches.EVENEMENTS or not inv_id:
        log_portail_event(
            "tache_recue", "refused", invitation_id=inv_id or None,
            reason="malformed",
        )
        return jsonify({"ok": False, "motif": "charge invalide"}), 200

    if event == "ouverte":
        # Transactional CAS — a plain read-check-write could race the
        # « soumise » task and regress the statut (Cloud Tasks guarantees
        # no ordering).
        if not pi.marquer_ouverte(inv_id):
            raise RuntimeError("statut ouverte update failed")  # retry
        return jsonify({"ok": True})

    if event == "renvoi":
        invitation = pi.lire_invitation(inv_id)
        # peut_relancer, NOT est_active: must mirror the portal's own test
        # (D-2 allows a resend while « soumise »; D-4 caps the total), or this
        # pre-check silently drops a renvoi the client was told was sent.
        if invitation is None or not pi.peut_relancer(invitation):
            return jsonify({"ok": True, "motif": "invitation inactive"})
        ok, _message, lien_manuel = emission.renvoyer_invitation(inv_id)
        if not ok:
            raise RuntimeError("renvoi failed")  # possibly transient — retry
        if lien_manuel and Config.graph_configured():
            # L'envoi a échoué (GraphError transitoire) alors que Graph EST
            # configuré ; en contexte de tâche, personne ne peut remettre le
            # lien manuel au client — lever pour que la file réessaie
            # (§8.3 : échec → lever, la reprise appartient à Cloud Tasks).
            # Graph non configuré : rien à réessayer, 200.
            raise RuntimeError("renvoi email send failed")
        return jsonify({"ok": True})

    if event == "rdv_demande":
        _traiter_rdv_demande(inv_id, str(batch or inv_id))
        return jsonify({"ok": True})

    if event == "rdv_retenir":
        _traiter_rdv_retenir(inv_id)
        return jsonify({"ok": True})

    if event == "rdv_reserver":
        if not batch:
            log_portail_event(
                "tache_recue", "refused", invitation_id=inv_id, reason="no_batch",
            )
            return jsonify({"ok": False, "motif": "charge invalide"}), 200
        _traiter_rdv_reserver(inv_id, str(batch))
        return jsonify({"ok": True})

    if event == "rdv_reporter":
        if not batch:
            log_portail_event(
                "tache_recue", "refused", invitation_id=inv_id, reason="no_batch",
            )
            return jsonify({"ok": False, "motif": "charge invalide"}), 200
        _traiter_rdv_reporter(inv_id, str(batch))
        return jsonify({"ok": True})

    if event == "rdv_annuler":
        _traiter_rdv_annuler(inv_id, str(batch or "1"))
        return jsonify({"ok": True})

    # event == "soumise"
    if not batch:
        log_portail_event(
            "tache_recue", "refused", invitation_id=inv_id, reason="no_batch",
        )
        return jsonify({"ok": False, "motif": "charge invalide"}), 200
    # Aiguillage documents / ouverture. Il se fait sur l'INVITATION, pas sur
    # l'enveloppe : la lecture de envelope.json vit à l'intérieur du
    # court-circuit d'idempotence de _traiter_soumission (« si le manifeste
    # existe, ne rien recalculer »), donc un aiguillage placé là ne
    # s'exécuterait qu'au premier essai — jamais sur un rejeu ni sur une
    # réparation de la réconciliation.
    invitation = pi.lire_invitation(inv_id)
    if invitation is not None and invitation.get("type") == "intake":
        _traiter_intake(inv_id, str(batch), invitation)
    else:
        _traiter_soumission(inv_id, str(batch))
    return jsonify({"ok": True})


# ── GET /taches/portail/reconciliation (cron, §8.4) ──────────────────────


@taches_portail_bp.get("/reconciliation")
def reconciliation():
    # Cron requests carry X-Appengine-Cron: true — stripped from external
    # traffic exactly like the queue header.
    if request.headers.get("X-Appengine-Cron") != "true":
        abort(403)

    bucket = _bucket()
    lots_vus = 0
    repares = 0

    # Two-level prefix walk (delimiter "/"): submissions/{inv}/{batch}/ —
    # trivial volume at a single-practice scale.
    racine = bucket.list_blobs(prefix="submissions/", delimiter="/")
    list(racine)  # consume the iterator so .prefixes populates
    for p_inv in sorted(racine.prefixes):
        niveau2 = bucket.list_blobs(prefix=p_inv, delimiter="/")
        list(niveau2)
        for p_batch in sorted(niveau2.prefixes):
            if not bucket.blob(p_batch + "envelope.json").exists():
                # No envelope = the client never pressed « Soumettre ». Usually
                # an upload still in flight — but if the newest object stopped
                # moving a while ago, the batch is ABANDONED: nothing will ever
                # reference it, Réception cannot see it, and the 90-day
                # lifecycle deletes the client's files unnoticed. Surface it.
                if _lot_abandonne(bucket, p_batch):
                    parts = p_batch.strip("/").split("/")
                    log_portail_event(
                        "lot_abandonne", "failure",
                        invitation_id=parts[1], batch=parts[2],
                    )
                continue
            lots_vus += 1
            parts = p_batch.strip("/").split("/")
            inv_id, batch = parts[1], parts[2]

            invitation = pi.lire_invitation(inv_id)
            complet = False
            if invitation:
                batches = {
                    s.get("batch")
                    for s in (invitation.get("soumissions") or [])
                }
                complet = batch in batches and bool(
                    (invitation.get("accuses") or {}).get(batch)
                )
            if complet:
                continue  # fully processed — intact (§8.4.4)

            # The queue lost work — replay it. Every repair is an ERROR by
            # design: it must be SEEN (§8.4.3). A failed re-enqueue must not
            # abort the sweep — the remaining lots still deserve their scan
            # (this lot comes back next cycle).
            try:
                taches.signaler("soumise", inv_id, batch=batch)
            except Exception:
                logger.exception("reconciliation re-enqueue failed")
                log_portail_event(
                    "tache_enfilage_echec", "failure",
                    invitation_id=inv_id, batch=batch, evenement="soumise",
                )
                continue
            repares += 1
            log_portail_event(
                "reconciliation_reparation", "failure",
                invitation_id=inv_id, batch=batch,
            )

    log_portail_event(
        "reconciliation_execute", lots_vus=lots_vus, lots_repares=repares,
    )
    return jsonify({"lots_vus": lots_vus, "repares": repares})
