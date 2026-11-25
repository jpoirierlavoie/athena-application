"""« Réception » — revue des transmissions du portail client (spec L1 §9).

Tout est @login_required, français, POST+redirect avec messages en query
(motif maison sans flash). RIEN n'est ingéré automatiquement : chaque octet
client n'entre au stockage canonique que sur clic « Verser » du juriste, et
seuls les fichiers conformes au vocabulaire documents (11 types depuis le
2026-08-13, ≤ 200 Mo depuis le 2026-08-12) sont versables — par COPIE
côté serveur (les octets ne transitent jamais par l'app) ; les autres se
téléchargent (attachment forcé, §7.5) et se traitent hors application.

Fail-open d'affichage : la base « portail » ou le bucket absents (infra pas
encore créée) rendent des états vides et un avertissement, jamais un 500.
"""

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from firebase_admin import storage
from flask import (
    Blueprint,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from auth import login_required
from client.config import (
    INVITATION_DOCUMENTS_JOURS,
    INVITATION_INTAKE_JOURS,
    PORTAIL_BUCKET,
)
from config import Config
from dav.sync import (
    bump_ctag,
    collection_for,
    record_tombstone,
)
from models import portail_invitation as pi
from models.document import (
    ALLOWED_EXTENSIONS,
    CATEGORY_CHOICES,
    DISPLAY_NAME_MAX,
    MAX_FILE_SIZE,
    build_attachment_disposition,
    ingest_blob_as_document,
    is_addressable_id,
    sign_blob_url,
)
from routes.documents import DOSSIER_READ_FAILED
from routes.taches_portail import _client_expediteur, _composer_cabinet, sha512_flux
from models.dossier import (
    create_dossier,
    get_dossier,
    get_dossier_strict,
    list_dossiers,
    suggest_file_number,
)
from models.folder import (
    PORTAL_FOLDER_NAME,
    SYSTEM_FOLDER_NAMES,
    SYSTEM_ROLE_AUTRES,
    SYSTEM_ROLE_PORTAIL,
    build_folder_tree,
    ensure_default_tree,
    ensure_system_folder,
    get_folder_strict,
    list_dossier_folders,
)
from models.protocol import ensure_ouverture_protocol
from utils import kyc
from models import concurrency
from models import transcription_reunion as tr_model
from models.hearing import (
    RDV_SOURCES,
    get_hearing,
    list_hearings_in_range,
    list_rendez_vous_strict,
    update_hearing,
)
from models.partie import (
    ROLE_LABELS,
    create_partie,
    display_name,
    get_partie,
    get_partie_strict,
    list_parties,
    update_partie,
)
from models.rendez_vous_reglages import get_rendez_vous_reglages
from routes import edit_conflict
from security import sanitize
from services import portail_emission as emission
from services import rendez_vous
from services import transcription_reunion
from services.carnet import apres_ecriture_contact
from utils import (
    cron_schedule,
    disponibilites_math,
    occupations,
    rapprochement,
    storage_identity,
)
from utils.accuse_reception_pdf import build_accuse_reception_pdf
from utils.logging_setup import log_bookings_event, log_portail_event, log_unexpected

logger = logging.getLogger(__name__)

reception_bp = Blueprint("reception", __name__, url_prefix="/reception")

_ONGLETS = ("tout", "documents", "rdv", "ouvertures", "archives")
_JOURS_CHOIX = (14, 30, 60, 90)

# D18 : la catégorie que le sélecteur du VERSEMENT présélectionne
# (templates/reception/index.html la reçoit d'ici, jamais d'un littéral
# recopié). Ce n'est PAS celle du formulaire de téléversement (« autre »,
# models.document.UPLOAD_DEFAULT_CATEGORY) : un fichier reçu du client est
# d'abord une pièce. Une catégorie restée à cette valeur n'a pas été
# choisie ; seule une valeur que le juriste a QUITTÉE l'est. Revue des
# correctifs du lot 2A : la route comparait à « autre », si bien que chaque
# versement laissé sur « pièce » était enregistré comme un choix du juriste
# (que Claude ne pouvait plus corriger), et un « autre » délibérément choisi
# comme un défaut.
VERSEMENT_DEFAULT_CATEGORY = "pièce"


# ── Pastille de navigation (compteur mis en cache, fail-open) ────────────

_BADGE_TTL_SECONDS = 60
_badge_cache: dict = {"at": 0.0, "n": None}
_badge_lock = threading.Lock()


def _invalidater_pastille() -> None:
    with _badge_lock:
        _badge_cache["at"] = 0.0


def _compter_rdv() -> int:
    """Rendez-vous « à_confirmer » + transcriptions Teams « a_revoir » en attente."""
    total = 0
    try:
        total += sum(
            1 for h in list_rendez_vous_strict()
            if h.get("confirmation") == "à_confirmer"
        )
    except Exception:
        logger.exception("reception: rdv count failed")
    try:
        total += len(tr_model.list_transcriptions_a_revoir())
    except Exception:
        logger.exception("reception: transcript count failed")
    return total


def compteur_reception() -> Optional[int]:
    """Transmissions « soumise » + rendez-vous « à_confirmer » en attente de
    revue — pour la pastille de nav.

    Un COUNT Firestore par page serait payé sur CHAQUE rendu de gabarit ;
    cache processus de 60 s. Fail-open : la base « portail » absente rend
    ``compter_soumises`` None — on affiche quand même les RDV le cas échéant,
    et None (les deux indisponibles) → aucune pastille, jamais une page cassée.
    """
    now = time.monotonic()
    if now - _badge_cache["at"] <= _BADGE_TTL_SECONDS:
        return _badge_cache["n"]
    with _badge_lock:
        now = time.monotonic()
        if now - _badge_cache["at"] > _BADGE_TTL_SECONDS:
            soumises = pi.compter_soumises()
            rdv = _compter_rdv()
            if soumises is None and rdv == 0:
                _badge_cache["n"] = None
            else:
                _badge_cache["n"] = (soumises or 0) + rdv
            _badge_cache["at"] = now
    return _badge_cache["n"]


# ── Accès quarantaine (côté principal : objectAdmin) ─────────────────────


def _bucket():
    return storage.bucket(PORTAIL_BUCKET)


def _prefix(inv_id: str, batch: str) -> str:
    return f"submissions/{inv_id}/{batch}/"


def _lire_manifeste(inv_id: str, batch: str) -> Optional[dict]:
    blob = _bucket().blob(_prefix(inv_id, batch) + "manifeste.json")
    if not blob.exists():
        return None
    return json.loads(blob.download_as_bytes())


def _lire_enveloppe(inv_id: str, batch: str) -> Optional[dict]:
    """Jumeau de _lire_manifeste pour les ouvertures (L3) : l'enveloppe EST la
    soumission — il n'y a ni fichier ni manifeste."""
    blob = _bucket().blob(_prefix(inv_id, batch) + "envelope.json")
    if not blob.exists():
        return None
    return json.loads(blob.download_as_bytes())


def _archiver_enveloppe(inv_id: str, batch: str) -> None:
    """Déplacer l'enveloppe traitée sous archive/ (cycle de vie 365 j).

    Copie PUIS suppression, dans cet ordre : une panne entre les deux laisse
    l'enveloppe en place, donc rejouable, plutôt que perdue.
    """
    bucket = _bucket()
    source = bucket.blob(_prefix(inv_id, batch) + "envelope.json")
    if not source.exists():
        return
    bucket.copy_blob(
        source, bucket, f"archive/{inv_id}/{batch}/envelope.json"
    )
    source.delete()


def _lire_enveloppe_archive(inv_id: str, batch: str) -> Optional[dict]:
    """Enveloppe d'une ouverture close : archive/ d'abord, submissions/ en
    repli — jumeau exact de _lire_manifeste_archive."""
    bucket = _bucket()
    for chemin in (f"archive/{inv_id}/{batch}/envelope.json",
                   _prefix(inv_id, batch) + "envelope.json"):
        blob = bucket.blob(chemin)
        if blob.exists():
            return json.loads(blob.download_as_bytes())
    return None


def _ecrire_manifeste(inv_id: str, batch: str, manifeste: dict) -> None:
    _bucket().blob(_prefix(inv_id, batch) + "manifeste.json").upload_from_string(
        json.dumps(manifeste, ensure_ascii=False),
        content_type="application/json",
    )


def _versable(entree: dict) -> bool:
    """Précontrôle du versement (vocabulaire documents courant — 11 types
    depuis 2026-08-13, ≤ 200 Mo depuis 2026-08-12).

    Le verdict final reste celui d'ingest_blob_as_document (sniff des
    octets) — ce contrôle n'existe que pour l'UI et un message français
    précis.
    """
    nom = entree.get("name") or ""
    ext = "." + nom.rsplit(".", 1)[1].lower() if "." in nom else ""
    taille = int(entree.get("size_gcs") or 0)
    return (
        entree.get("etat") == "reçu"
        and ext in ALLOWED_EXTENSIONS
        and 0 < taille <= MAX_FILE_SIZE
    )


_EPOCH_MIN = datetime.min.replace(tzinfo=timezone.utc)


def _parse_sort_dt(raw: object) -> datetime:
    """Normalize a UTC timestamp or ISO string for descending chronological sort."""
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return _EPOCH_MIN
    return _EPOCH_MIN


def _rediriger(message: str = "", erreur: str = "", onglet: str = ""):
    posted = (request.form.get("onglet") or "").strip() if request.method == "POST" else ""
    cible = posted if posted in _ONGLETS else (onglet if onglet in _ONGLETS else "")
    args = {}
    if cible:
        args["onglet"] = cible
    if message:
        args["message"] = message
    if erreur:
        args["erreur"] = erreur
    return redirect(url_for("reception.index", **args))


def _onglet_soumis(defaut: str = "") -> str:
    raw = (request.form.get("onglet") or "").strip()
    return raw if raw in _ONGLETS else defaut


def _folder_options_for_dossier(dossier_id: str) -> tuple[list[tuple[str, str]], bool]:
    """Return ``([(folder_id, breadcrumb_label), ...], readable)`` for *dossier_id*."""
    if not is_addressable_id(dossier_id):
        return [], True
    try:
        folders = list_dossier_folders(dossier_id)
    except Exception as exc:
        logger.warning("reception: dossier folder list failed: %s", type(exc).__name__)
        return [], False
    roots = build_folder_tree(folders)
    options: list[tuple[str, str]] = []

    def _walk(nodes: list[dict], prefix: str) -> None:
        for node in nodes:
            name = (node.get("name") or "").strip() or "Sans nom"
            label = f"{prefix} › {name}" if prefix else name
            options.append((node["id"], label))
            _walk(node.get("children") or [], label)

    _walk(roots, "")
    return options, True


def _dossier_folder_breadcrumb(dossier_id: str, folder: dict) -> str:
    """Best-effort human-readable path label for an already-validated *folder*."""
    name = (folder.get("name") or "").strip() or PORTAL_FOLDER_NAME
    if not folder.get("parent_folder_id"):
        return name
    if folder.get("system_role") == SYSTEM_ROLE_PORTAIL:
        return _chemin_recus(folder)
    try:
        folders = list_dossier_folders(dossier_id)
    except Exception:
        return name
    by_id = {f.get("id"): f for f in folders if f.get("id")}
    parts = [name]
    cur_parent = folder.get("parent_folder_id")
    seen = {folder.get("id")}
    while cur_parent and cur_parent in by_id and cur_parent not in seen:
        seen.add(cur_parent)
        pnode = by_id[cur_parent]
        parts.append((pnode.get("name") or "").strip() or "Dossier")
        cur_parent = pnode.get("parent_folder_id")
    return " › ".join(reversed(parts))


# ── Onglet « Rendez-vous » (Bookings L2 §5) ──────────────────────────────


def _enrichir_rdvs_portail(rdvs: list[dict]) -> None:
    """Attach visual conflict candidates and overlapping schedule hints to portal RDVs."""
    portail_rdvs = [h for h in rdvs if h.get("source") == "portail"]
    if not portail_rdvs:
        return
    try:
        parties = list_parties()
    except Exception:
        logger.exception("reception: parties read failed for rdv conflicts")
        parties = []
    try:
        dossiers = list_dossiers()
    except Exception:
        logger.exception("reception: dossiers read failed for rdv conflicts")
        dossiers = []

    dossier_party_ids: set[str] = set()
    for d in dossiers:
        for champ in ("client_ids", "opposing_party_ids", "avocat_ids"):
            dossier_party_ids.update(str(pid) for pid in (d.get(champ) or ()) if pid)

    index = [(p["id"], display_name(p)) for p in parties if p.get("id")]

    starts = [h.get("start_datetime") for h in portail_rdvs if isinstance(h.get("start_datetime"), datetime)]
    ends = [h.get("end_datetime") for h in portail_rdvs if isinstance(h.get("end_datetime"), datetime)]
    existing_intervals: list[tuple[str, datetime, datetime]] = []
    if starts and ends:
        try:
            window_rows = list_hearings_in_range(
                min(starts) - timedelta(days=2),
                max(ends) + timedelta(days=2),
                limit=500,
                include_unconfirmed=True,
            )
            for row in window_rows:
                iv = disponibilites_math.hearing_busy_interval(row)
                if iv is not None:
                    existing_intervals.append((str(row.get("id") or ""), iv[0], iv[1]))
        except Exception:
            logger.exception("reception: range read failed for rdv overlap hint")

    for h in portail_rdvs:
        noms: list[str] = []
        client_nom = (h.get("client_nom") or "").strip()
        if client_nom:
            noms.append(client_nom)
        for adv in h.get("parties_adverses") or ():
            s_adv = str(adv or "").strip()
            if s_adv:
                noms.append(s_adv)

        conflits = []
        if index:
            for nom in noms:
                trouves = rapprochement.candidats(nom, index)
                if trouves:
                    conflits.append({
                        "nom": nom,
                        "candidats": [
                            {
                                "id": c.cle,
                                "nom": c.nom,
                                "motif": c.motif,
                                "en_dossier": c.cle in dossier_party_ids,
                            }
                            for c in trouves
                        ],
                    })
        h["_conflits_candidats"] = conflits

        chevauche = False
        hs = h.get("start_datetime")
        he = h.get("end_datetime")
        if isinstance(hs, datetime) and isinstance(he, datetime):
            hid = str(h.get("id") or "")
            for other_id, os_dt, oe_dt in existing_intervals:
                if other_id != hid and hs < oe_dt and os_dt < he:
                    chevauche = True
                    break
        h["_conflit_horaire"] = chevauche


def _contexte_rdv() -> dict:
    """Build the « Rendez-vous » tab context: pending cards, divergences, pending invites & transcripts."""
    heures = cron_schedule.cadence_fr("disponibilites")
    teams_actif = bool(Config.TEAMS_REUNIONS_ACTIF and Config.teams_configured())
    od_configure = bool(Config.courrier_configured())
    try:
        rdvs = rendez_vous.lister_en_attente()
        divergences = rendez_vous.lister_divergences()
        erreur_rdv = False
    except rendez_vous.LectureImpossible:
        rdvs = []
        divergences = []
        erreur_rdv = True

    if not erreur_rdv:
        _enrichir_rdvs_portail(rdvs)

    rdv_invites_attente: list[dict] = []
    if not erreur_rdv and Config.PORTAIL_RDV_ACTIF:
        try:
            rdv_invites_attente = [
                inv for inv in pi.lister_invitations(type_="rdv")
                if inv.get("statut") in ("envoyée", "ouverte")
                and inv.get("etat_rdv") in ("a_choisir", "creneau_pris")
                and not pi.est_expiree(inv)
            ]
        except Exception:
            logger.exception("reception: pending rdv invites read failed")

    rdv_transcriptions: list[dict] = []
    erreur_transcriptions = False
    try:
        rdv_transcriptions = transcription_reunion.lister_a_revoir_enrichies()
    except transcription_reunion.LectureImpossible:
        erreur_transcriptions = True

    dossiers_actifs = list_dossiers(status_filter="actif") if rdv_transcriptions else []

    return {
        "rdvs": rdvs,
        "divergences": divergences,
        "rdv_invites_attente": rdv_invites_attente,
        "erreur_rdv": erreur_rdv,
        "rdv_transcriptions": rdv_transcriptions,
        "erreur_transcriptions": erreur_transcriptions,
        "dossiers_actifs": dossiers_actifs,
        "teams_reunions_actif": teams_actif,
        "onedrive_configure": od_configure,
        "heures_synchro": heures,
    }


# ── Ouvertures (L3 §5) ───────────────────────────────────────────────────

# Table de correspondance formulaire → contact (§5.3). Elle vit ICI, du côté
# juriste, et non dans le portail : c'est au moment du versement que la
# décision se prend, et le portail ne connaît pas ``models``. L'ordre est celui
# du formulaire des contacts, pour que la vue côte à côte se lise comme la
# fiche.
_CORRESPONDANCE = (
    ("prenom", "first_name", "Prénom"),
    ("nom", "last_name", "Nom"),
    ("date_naissance", "birth_date", "Date de naissance"),
    ("denomination", "organization_name", "Dénomination sociale"),
    ("neq", "company_neq", "NEQ"),
    ("langue", "language", "Langue"),
    ("courriel", "email", "Courriel"),
    ("telephone", "phone_cell", "Cellulaire"),
    ("telephone2", "phone_home", "Domicile"),
    ("adresse_rue", "address_street", "Rue"),
    ("adresse_app", "address_unit", "Appartement"),
    ("adresse_ville", "address_city", "Ville"),
    ("adresse_province", "address_province", "Province"),
    ("adresse_code_postal", "address_postal_code", "Code postal"),
    ("adresse_pays", "address_country", "Pays"),
    # Section professionnelle de la fiche (2026-11-05). Le libellé de
    # l'employeur suit l'occupation : _comparaison le recompose.
    ("occupation", "occupation", "Occupation"),
    ("poste", "job_title", "Poste ou profession"),
    ("employeur", "organization", occupations.ORGANIZATION_LABEL_DEFAULT),
)


def _occupation_versable(valeur: str) -> bool:
    """Une occupation transmise n'est versée que si le vocabulaire la connaît.

    Le portail refuse toute autre valeur à la saisie ; une enveloppe plus
    ancienne que le vocabulaire en vigueur peut toutefois en porter une, que
    le modèle refuserait — et avec elle toute la fiche.
    """
    return valeur in occupations.OCCUPATIONS


def _comparaison(donnees: dict, partie: Optional[dict]) -> list[dict]:
    """Lignes de la vue côte à côte : valeur actuelle ↔ valeur soumise.

    ``applicable`` n'est vrai que pour les champs qui DIFFÈRENT et dont la
    valeur soumise n'est pas vide : c'est ce qui pré-coche les cases (§5.3).
    Un champ soumis vide ne propose jamais d'effacer une valeur au dossier —
    le silence d'un client n'est pas une rétractation. L'occupation se lit
    par son libellé (``*_affiche``) ; une occupation que le vocabulaire ne
    connaît pas est montrée ``non_reconnu`` et jamais proposée.
    """
    occupation_soumise = (donnees.get("occupation") or "").strip()
    lignes = []
    for champ, cible, libelle in _CORRESPONDANCE:
        soumis = (donnees.get(champ) or "").strip()
        actuel = ""
        if partie:
            valeur = partie.get(cible)
            if valeur is None:
                actuel = ""
            elif hasattr(valeur, "strftime"):
                # birth_date est une DATE SEULE à minuit UTC : str() en ferait
                # « 1985-03-17 00:00:00+00:00 », qui ne serait jamais égal au
                # « 1985-03-17 » transmis — le champ paraîtrait toujours
                # différent, donc toujours pré-coché. strftime, jamais to_mtl
                # (Montréal reculerait la date d'un jour).
                actuel = valeur.strftime("%Y-%m-%d")
            else:
                actuel = str(valeur)
        if not soumis and not actuel:
            continue
        actuel_affiche, soumis_affiche, non_reconnu = actuel, soumis, False
        if champ == "occupation":
            actuel_affiche = occupations.occupation_label(actuel) or actuel
            soumis_affiche = occupations.occupation_label(soumis) or soumis
            non_reconnu = bool(soumis) and not _occupation_versable(soumis)
        elif champ == "employeur":
            libelle = occupations.organization_label(
                occupation_soumise or (partie or {}).get("occupation") or "")
        lignes.append({
            "champ": champ,
            "cible": cible,
            "libelle": libelle,
            "actuel": actuel,
            "soumis": soumis,
            "actuel_affiche": actuel_affiche,
            "soumis_affiche": soumis_affiche,
            "non_reconnu": non_reconnu,
            "differe": bool(soumis) and soumis != actuel,
            "applicable": bool(soumis) and soumis != actuel and not non_reconnu,
        })
    return lignes


def _candidats_adverses(adverses: list[dict], parties: list[dict]) -> list[dict]:
    """Aide VISUELLE au contrôle des conflits (§5.2) — jamais un verdict."""
    index = [(p["id"], display_name(p)) for p in parties if p.get("id")]
    enrichies = []
    for ligne in adverses:
        nom = (ligne.get("nom") or "").strip()
        if not nom:
            continue
        trouves = rapprochement.candidats(nom, index)
        enrichies.append({
            "nom": nom,
            "precision": (ligne.get("precision") or "").strip(),
            "candidats": [
                {"id": c.cle, "nom": c.nom, "motif": c.motif} for c in trouves
            ],
        })
    return enrichies


def _contexte_ouvertures() -> dict:
    """Onglet « Ouvertures » — même forme que _contexte_rdv : lecture sous
    try/except → drapeau d'erreur, tri en Python, TOUT précalculé pour que le
    gabarit reste sans logique."""
    try:
        toutes = pi.lister_invitations(type_="intake")
    except Exception:
        logger.exception("reception: intake invitations read failed")
        return {"ouvertures": [], "intake_actives": [], "intake_traitees": [],
                "erreur_ouvertures": True}

    contexte: dict = {
        "ouvertures": [],
        "intake_actives": [
            i for i in toutes if i.get("statut") in ("envoyée", "ouverte")
        ],
        "intake_traitees": [
            i for i in toutes
            if i.get("statut") in ("traitée", "refusée", "révoquée")
        ][:20],
        "erreur_ouvertures": False,
    }

    soumises = [i for i in toutes if i.get("statut") == "soumise"]
    if not soumises:
        return contexte

    # Une seule lecture des contacts pour tout l'onglet (jeu de données d'un
    # cabinet solo — borné, aucun index requis).
    try:
        parties = list_parties()
    except Exception:
        logger.exception("reception: parties read failed")
        parties = []

    for inv in soumises:
        # La DERNIÈRE soumission fait foi : la ré-entrée permet de corriger,
        # et c'est la version corrigée que le juriste doit voir.
        lots = inv.get("soumissions") or []
        if not lots:
            continue
        batch = (lots[-1].get("batch") or "")
        enveloppe = None
        try:
            enveloppe = _lire_enveloppe(inv["id"], batch)
        except Exception:
            logger.exception("reception: intake envelope read failed")
            contexte["erreur_bucket"] = True
        donnees = (enveloppe or {}).get("donnees") or {}
        partie = None
        if inv.get("partie_id"):
            try:
                partie = get_partie(inv["partie_id"])
            except Exception:
                logger.exception("reception: partie read failed")
        contexte["ouvertures"].append({
            "invitation": inv,
            "batch": batch,
            "enveloppe": enveloppe,
            # Une enveloppe illisible ne disparaît pas en silence : la fiche
            # s'affiche avec un bandeau et reste actionnable (refus).
            "lisible": bool(donnees),
            "partie": partie,
            "nature": donnees.get("nature") or "physique",
            "lignes": _comparaison(donnees, partie),
            "adverses": _candidats_adverses(
                (enveloppe or {}).get("parties_adverses") or [], parties
            ),
            "consentement": (enveloppe or {}).get("consentement") or {},
            "versions": len(lots),
        })
    if contexte["ouvertures"]:
        try:
            contexte["numero_dossier_suggere"] = suggest_file_number()
        except Exception:
            contexte["numero_dossier_suggere"] = ""
    return contexte


# ── Page principale ──────────────────────────────────────────────────────


def _construire_lots_documents(invitations: list[dict]) -> tuple[list[dict], bool]:
    """Read submitted manifests for *invitations* (documents). Fails open per lot."""
    lots: list[dict] = []
    erreur_bucket = False
    folder_cache: dict[str, list[tuple[str, str]]] = {}
    for inv in (i for i in invitations if i.get("statut") == "soumise"):
        dossier_id = (inv.get("dossier_id") or "").strip()
        if dossier_id and dossier_id not in folder_cache:
            opts, readable = _folder_options_for_dossier(dossier_id)
            folder_cache[dossier_id] = opts if readable else []
        for soumission in inv.get("soumissions") or []:
            batch = soumission.get("batch") or ""
            manifeste = None
            try:
                manifeste = _lire_manifeste(inv["id"], batch)
            except Exception:
                logger.exception("reception: manifest read failed")
                erreur_bucket = True
            fichiers = []
            if manifeste:
                for seq, entree in enumerate(manifeste.get("files") or []):
                    fichiers.append(
                        {**entree, "seq": seq, "versable": _versable(entree)}
                    )
            lots.append({
                "invitation": inv,
                "batch": batch,
                "soumission": soumission,
                "manifeste": manifeste,
                "fichiers": fichiers,
                "default_folder_options": folder_cache.get(dossier_id, []),
            })
    return lots, erreur_bucket


def _construire_flux_unifie(contexte: dict) -> list[dict]:
    """Merge pending lots, RDVs, divergences, transcripts, and openings chronologically."""
    items: list[dict] = []
    for lot in contexte.get("lots") or []:
        inv = lot["invitation"]
        submitted = (
            (lot.get("manifeste") or {}).get("submitted_at")
            or (lot.get("soumission") or {}).get("submitted_at")
            or inv.get("updated_at")
            or inv.get("created_at")
        )
        items.append({
            "kind": "lot",
            "id": f"{inv['id']}:{lot['batch']}",
            "sort_dt": _parse_sort_dt(submitted),
            "lot": lot,
        })
    for h in contexte.get("divergences") or []:
        dt = h.get("updated_at") or h.get("start_datetime") or h.get("created_at")
        items.append({
            "kind": "divergence",
            "id": h["id"],
            "sort_dt": _parse_sort_dt(dt),
            "h": h,
        })
    for h in contexte.get("rdvs") or []:
        dt = h.get("created_at") or h.get("updated_at") or h.get("start_datetime")
        items.append({
            "kind": "rdv",
            "id": h["id"],
            "sort_dt": _parse_sort_dt(dt),
            "h": h,
        })
    for tx in contexte.get("rdv_transcriptions") or []:
        dt = tx.get("updated_at") or tx.get("hearing_start_at") or tx.get("created_at")
        items.append({
            "kind": "transcription",
            "id": tx["hearing_id"],
            "sort_dt": _parse_sort_dt(dt),
            "tx": tx,
        })
    for o in contexte.get("ouvertures") or []:
        inv = o["invitation"]
        dt = (
            (o.get("enveloppe") or {}).get("submitted_at")
            or inv.get("updated_at")
            or inv.get("created_at")
        )
        items.append({
            "kind": "ouverture",
            "id": f"{inv['id']}:{o['batch']}",
            "sort_dt": _parse_sort_dt(dt),
            "o": o,
        })
    items.sort(key=lambda it: (it["sort_dt"], it["kind"], it["id"]), reverse=True)
    return items


@reception_bp.get("/")
@login_required
def index():
    onglet = request.args.get("onglet", "tout")
    if onglet not in _ONGLETS:
        onglet = "tout"
    contexte = {
        "onglet": onglet,
        "message": sanitize(request.args.get("message", ""), max_length=300),
        "erreur": sanitize(request.args.get("erreur", ""), max_length=300),
        "lots": [],
        "actives": [],
        "traitees": [],
        "rdvs": [],
        "divergences": [],
        "rdv_invites_attente": [],
        "rdv_transcriptions": [],
        "erreur_rdv": False,
        "erreur_transcriptions": False,
        "ouvertures": [],
        "intake_actives": [],
        "intake_traitees": [],
        "erreur_ouvertures": False,
        "feature_intake": Config.FEATURE_INTAKE,
        "erreur_bucket": False,
        "est_expiree": pi.est_expiree,
        "feed_items": [],
        "pending_counts": {},
    }
    toutes_invitations: Optional[list[dict]] = None
    if onglet in ("tout", "archives"):
        toutes_invitations = pi.lister_invitations(limit=200)
        doc_invs = [i for i in toutes_invitations if i.get("type") == "documents"]
    elif onglet == "documents":
        doc_invs = pi.lister_invitations(type_="documents")
    else:
        doc_invs = []

    if onglet in ("tout", "documents", "archives"):
        inv_source = (
            toutes_invitations
            if onglet in ("tout", "archives") and toutes_invitations is not None
            else doc_invs
        )
        contexte["actives"] = [
            i for i in inv_source if i.get("statut") in ("envoyée", "ouverte")
        ]
        contexte["traitees"] = [
            i for i in inv_source
            if i.get("statut") in ("traitée", "refusée", "révoquée")
        ][:30]
    if onglet in ("tout", "documents"):
        lots, erreur_bucket = _construire_lots_documents(doc_invs)
        contexte["lots"] = lots
        contexte["erreur_bucket"] = erreur_bucket

    if onglet in ("tout", "rdv"):
        contexte.update(_contexte_rdv())
    if onglet in ("tout", "ouvertures"):
        contexte.update(_contexte_ouvertures())

    if onglet == "tout":
        feed_items = _construire_flux_unifie(contexte)
        contexte["feed_items"] = feed_items
        n_docs = len(contexte["lots"])
        n_rdv = (
            len(contexte["rdvs"])
            + len(contexte["divergences"])
            + len(contexte["rdv_transcriptions"])
        )
        n_ouv = len(contexte["ouvertures"])
        contexte["pending_counts"] = {
            "tout": len(feed_items),
            "documents": n_docs,
            "rdv": n_rdv,
            "ouvertures": n_ouv,
            "archives": len(contexte["actives"]) + len(contexte["traitees"]),
        }

    needs_dossiers = onglet in ("tout", "documents") or bool(contexte.get("rdv_transcriptions"))
    dossiers = list_dossiers(status_filter="actif") if needs_dossiers else []
    if dossiers and not contexte.get("dossiers_actifs"):
        contexte["dossiers_actifs"] = dossiers

    return render_template(
        "reception/index.html",
        dossiers=dossiers,
        # The versement select is an INPUT: CHOICES, not LABELS.
        category_choices=CATEGORY_CHOICES,
        # Its pre-selected value — the one D18 counts as « not chosen ».
        versement_default_category=VERSEMENT_DEFAULT_CATEGORY,
        **contexte,
    )


# ── Émission d'invitations (§6.2) ────────────────────────────────────────


def _email_du_dossier(dossier: dict) -> str:
    for client in dossier.get("clients") or []:
        partie = get_partie(client.get("id", ""))
        if partie and (partie.get("email") or partie.get("email_work")):
            return partie.get("email") or partie.get("email_work")
    return ""


def _rdv_inviter_context() -> dict:
    """Meeting types + default duration/agenda for the inviter form."""
    reglages = get_rendez_vous_reglages()
    types = [
        t for t in (reglages.get("types_rdv") or ())
        if isinstance(t, dict) and t.get("cle")
    ]
    first = types[0] if types else {}
    return {
        "rdv_types": types,
        "rdv_type_defaut": str(first.get("cle") or ""),
        "rdv_duree_defaut": int(first.get("duree_minutes") or 30),
        "rdv_agenda_defaut": str(first.get("ordre_du_jour") or ""),
    }


def _parse_duree(raw: str) -> Optional[int]:
    try:
        return int(str(raw or "").strip())
    except (ValueError, TypeError):
        return None


@reception_bp.get("/inviter")
@login_required
def inviter_form():
    dossier_id = request.args.get("dossier_id", "")
    dossier = get_dossier(dossier_id) if dossier_id else None
    type_ = request.args.get("type", "documents")
    if type_ not in ("documents", "intake", "rdv"):
        type_ = "documents"
    # Déclencheur (b) : la fiche partie amène ici avec ?partie_id=, ce qui
    # préremplit le contact et, à la soumission, l'instantané non sensible.
    partie = get_partie(request.args.get("partie_id", "")) or None
    return render_template(
        "reception/inviter.html",
        dossiers=list_dossiers(status_filter="actif"),
        dossier=dossier,
        type_=type_,
        partie=partie,
        email_prefill=(
            (partie.get("email") or partie.get("email_work") or "") if partie
            else (_email_du_dossier(dossier) if dossier else "")
        ),
        jours_choix=_JOURS_CHOIX,
        jours_selection=(
            INVITATION_INTAKE_JOURS if type_ == "intake"
            else INVITATION_DOCUMENTS_JOURS
        ),
        errors=[],
        form={},
        **_rdv_inviter_context(),
    )


@reception_bp.get("/partie-search")
@login_required
def partie_search():
    """HTMX autocomplete for the invitation client picker.

    Rows carry data-id/data-name/data-email; a nonce'd delegated listener on
    the inviter form fills the hidden partie_id + the name/email inputs.
    """
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return render_template("reception/_partie_resultats.html",
                               parties=None, role_labels=ROLE_LABELS)
    parties = list_parties(search=q)[:10]
    rows = [{"id": p["id"], "name": display_name(p),
             "email": p.get("email") or p.get("email_work") or "",
             "role": p.get("contact_role", "")} for p in parties]
    return render_template("reception/_partie_resultats.html",
                           parties=rows, role_labels=ROLE_LABELS)


@reception_bp.post("/inviter")
@login_required
def inviter_submit():
    f = request.form
    type_ = f.get("type", "documents")
    if type_ not in ("documents", "intake", "rdv"):
        type_ = "documents"
    dossier_id = f.get("dossier_id", "").strip()
    dossier = get_dossier(dossier_id) if dossier_id else None
    display_label = sanitize(f.get("display_label", ""), max_length=120).strip()
    if not display_label:
        if type_ == "intake":
            display_label = "Ouverture de votre dossier client"
        elif type_ == "rdv":
            display_label = ""  # portail_emission fills from meeting type
        elif dossier:
            display_label = f"Dossier {dossier.get('file_number', '')}".strip()
    defaut_jours = (
        INVITATION_INTAKE_JOURS if type_ == "intake"
        else INVITATION_DOCUMENTS_JOURS
    )
    jours: Optional[int]
    if type_ == "rdv":
        jours = None
        try:
            jours_fallback = int(f.get("jours", str(defaut_jours)))
        except ValueError:
            jours_fallback = defaut_jours
    else:
        try:
            jours = int(f.get("jours", str(defaut_jours)))
        except ValueError:
            jours = defaut_jours
        jours_fallback = jours

    partie_id = f.get("partie_id", "").strip() or None
    partie = get_partie(partie_id) if partie_id else None
    if partie is None:
        partie_id = None
    client_name = sanitize(f.get("client_name", ""), max_length=200).strip()

    invitation, errors, lien_manuel = emission.emettre_invitation(
        type_,
        f.get("email", ""),
        dossier_id=dossier["id"] if dossier else None,
        partie_id=partie_id,
        client_name=client_name,
        display_label=display_label,
        jours=jours,
        prefill=(
            pi.prefill_depuis_partie(partie)
            if type_ == "intake" and partie else None
        ),
        type_rdv_cle=f.get("type_rdv_cle", "") if type_ == "rdv" else None,
        duree_minutes=_parse_duree(f.get("duree_minutes", "")) if type_ == "rdv" else None,
        ordre_du_jour=f.get("ordre_du_jour", "") if type_ == "rdv" else None,
    )
    if errors:
        return render_template(
            "reception/inviter.html",
            dossiers=list_dossiers(status_filter="actif"),
            dossier=dossier,
            type_=type_,
            partie=partie,
            email_prefill="",
            jours_choix=_JOURS_CHOIX,
            jours_selection=jours_fallback,
            errors=errors,
            form=f,
            **_rdv_inviter_context(),
        ), 400
    if lien_manuel:
        return render_template(
            "reception/lien.html", invitation=invitation, lien=lien_manuel
        )
    onglet = "ouvertures" if type_ == "intake" else ("rdv" if type_ == "rdv" else "")
    return _rediriger(
        message="Invitation transmise par courriel.",
        onglet=onglet,
    )


@reception_bp.post("/invitations/<inv_id>/renvoyer")
@login_required
def renvoyer(inv_id: str):
    ok, message, lien_manuel = emission.renvoyer_invitation(inv_id)
    if not ok:
        return _rediriger(erreur=message or "Renvoi impossible.")
    if lien_manuel:
        invitation = pi.lire_invitation(inv_id)
        return render_template(
            "reception/lien.html", invitation=invitation, lien=lien_manuel
        )
    return _rediriger(message="Nouveau lien transmis par courriel.")


@reception_bp.post("/invitations/<inv_id>/revoquer")
@login_required
def revoquer(inv_id: str):
    onglet = _onglet_soumis()
    if not pi.revoquer(inv_id):
        return _rediriger(erreur="Révocation impossible.", onglet=onglet)
    log_portail_event("invitation_revoquee", invitation_id=inv_id)
    return _rediriger(
        message="Invitation révoquée — le lien du client est immédiatement inopérant.",
        onglet=onglet,
    )


@reception_bp.get("/invitations/<inv_id>/accuse.pdf")
@login_required
def invitation_accuse_pdf(inv_id: str):
    """Téléchargement direct du bordereau d'accusé de réception en PDF."""
    if not is_addressable_id(inv_id):
        return render_template("errors/404.html"), 404
    try:
        invitation = pi.lire_invitation_strict(inv_id)
    except Exception as exc:
        logger.error("reception: invite pdf read failed: %s", type(exc).__name__)
        return Response(
            "Lecture de l'invitation impossible.",
            status=503,
            mimetype="text/plain; charset=utf-8",
        )
    if invitation is None or invitation.get("type") not in ("documents", "intake"):
        return render_template("errors/404.html"), 404

    dossier = None
    if invitation.get("dossier_id"):
        try:
            dossier = get_dossier_strict(invitation["dossier_id"])
        except Exception as exc:
            logger.error("reception: pdf dossier read failed: %s", type(exc).__name__)
            return Response(
                "Lecture du dossier impossible.",
                status=503,
                mimetype="text/plain; charset=utf-8",
            )

    partie = None
    if invitation.get("partie_id"):
        try:
            partie = get_partie_strict(invitation["partie_id"])
        except Exception as exc:
            logger.error("reception: pdf partie read failed: %s", type(exc).__name__)
            return Response(
                "Lecture de la fiche client impossible.",
                status=503,
                mimetype="text/plain; charset=utf-8",
            )

    est_intake = invitation.get("type") == "intake"
    lots: list[dict] = []
    for soumission in invitation.get("soumissions") or []:
        batch = soumission.get("batch") or ""
        try:
            if est_intake:
                enveloppe = _lire_enveloppe_archive(inv_id, batch) or {}
                donnees = enveloppe.get("donnees") or {}
                lots.append({
                    "batch": batch,
                    "soumis_le": enveloppe.get("submitted_at") or soumission.get("submitted_at") or "",
                    "http": enveloppe.get("http") or {},
                    "consentement": enveloppe.get("consentement") or {},
                    "statut_affiche": invitation.get("statut") or "soumise",
                    "lignes": _comparaison(donnees, None) if donnees else [],
                    "adverses": [
                        {
                            "nom": (a.get("nom") or "").strip(),
                            "precision": (a.get("precision") or "").strip(),
                        }
                        for a in (enveloppe.get("parties_adverses") or [])
                        if (a.get("nom") or "").strip()
                    ],
                })
            else:
                manifeste = _lire_manifeste_archive(inv_id, batch)
                lots.append({
                    "batch": batch,
                    "submitted_at": (manifeste or {}).get("submitted_at") or soumission.get("submitted_at") or "",
                    "http": (manifeste or {}).get("http") or {},
                    "fichiers": (manifeste or {}).get("files") or [],
                })
        except Exception as exc:
            logger.error("reception: pdf archive read failed: %s", type(exc).__name__)
            return Response(
                "Lecture des données d'archive impossible.",
                status=503,
                mimetype="text/plain; charset=utf-8",
            )

    pdf_bytes = build_accuse_reception_pdf(
        invitation,
        lots,
        cabinet=_composer_cabinet(),
        client=_client_expediteur(invitation, partie),
        dossier=dossier,
    )
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": build_attachment_disposition(
                f"accuse-reception-{inv_id}.pdf"
            ),
        },
    )


@reception_bp.get("/dossiers/<dossier_id>/dossiers")
@login_required
def dossier_folders(dossier_id: str):
    """Sous-dossiers d'un dossier pour le sélecteur de versement (JSON ou HTML)."""
    options, readable = _folder_options_for_dossier(dossier_id)
    wants_json = (
        request.args.get("format") == "json"
        or "application/json" in (request.headers.get("Accept") or "")
    )
    if wants_json:
        if not readable:
            return jsonify({"options": [], "unreadable": True}), 503
        return jsonify({
            "options": [{"id": fid, "label": flabel} for fid, flabel in options],
            "unreadable": False,
        })
    if not readable:
        return (
            render_template("reception/_dossier_options.html", options=[], unreadable=True),
            503,
        )
    return render_template("reception/_dossier_options.html", options=options, unreadable=False)


def _lire_manifeste_archive(inv_id: str, batch: str) -> Optional[dict]:
    """Manifeste d'un lot traité : archive/ d'abord, submissions/ en repli."""
    bucket = _bucket()
    for chemin in (f"archive/{inv_id}/{batch}/manifeste.json",
                   _prefix(inv_id, batch) + "manifeste.json"):
        blob = bucket.blob(chemin)
        if blob.exists():
            return json.loads(blob.download_as_bytes())
    return None


@reception_bp.get("/invitations/<inv_id>/archive")
@login_required
def invitation_archive(inv_id: str):
    """Fenêtre modale : le détail conservé d'une invitation traitée —
    par lot, la liste des fichiers avec empreinte SHA-512 et le choix de
    l'avocat (versé au dossier / refusé). Lu des manifestes archivés."""
    invitation = pi.lire_invitation(inv_id)
    if invitation is None:
        return render_template("reception/_archive_modal.html",
                               invitation=None, lots=[], erreur=True,
                               est_intake=False)
    # Une ouverture (L3) se conserve et se consulte comme un lot de documents,
    # à ceci près que l'enveloppe EST la soumission : pas de fichiers, donc pas
    # d'empreintes — le récapitulatif des réponses tient lieu de contenu.
    est_intake = invitation.get("type") == "intake"
    lots = []
    erreur = False
    for soumission in invitation.get("soumissions") or []:
        batch = soumission.get("batch") or ""
        lot = {"batch": batch, "soumission": soumission, "fichiers": []}
        try:
            if est_intake:
                enveloppe = _lire_enveloppe_archive(inv_id, batch) or {}
                donnees = enveloppe.get("donnees") or {}
                lot.update({
                    "lisible": bool(donnees),
                    # Vue en lecture seule : aucune partie de référence, donc
                    # « actuel » reste vide et seul le déclaré s'affiche.
                    "lignes": _comparaison(donnees, None),
                    "adverses": [
                        {"nom": (a.get("nom") or "").strip(),
                         "precision": (a.get("precision") or "").strip()}
                        for a in (enveloppe.get("parties_adverses") or [])
                        if (a.get("nom") or "").strip()
                    ],
                    "consentement": enveloppe.get("consentement") or {},
                    "soumis_le": enveloppe.get("submitted_at") or "",
                    "http": enveloppe.get("http") or {},
                })
            else:
                manifeste = _lire_manifeste_archive(inv_id, batch)
                lot["fichiers"] = (manifeste or {}).get("files") or []
        except Exception:
            logger.exception("reception: archive read failed")
            erreur = True
            lot.setdefault("lisible", False)
        lots.append(lot)
    return render_template("reception/_archive_modal.html",
                           invitation=invitation, lots=lots, erreur=erreur,
                           est_intake=est_intake)


# ── Fichiers d'un lot ────────────────────────────────────────────────────


def _entree_ou_none(inv_id: str, batch: str, seq: int):
    try:
        manifeste = _lire_manifeste(inv_id, batch)
    except Exception:
        logger.exception("reception: manifest read failed")
        return None, None
    if manifeste is None:
        return None, None
    fichiers = manifeste.get("files") or []
    if not 0 <= seq < len(fichiers):
        return manifeste, None
    return manifeste, fichiers[seq]


@reception_bp.get("/lots/<inv_id>/<batch>/fichiers/<int:seq>")
@login_required
def telecharger(inv_id: str, batch: str, seq: int):
    _manifeste, entree = _entree_ou_none(inv_id, batch, seq)
    if entree is None:
        return render_template("errors/404.html"), 404
    blob = _bucket().blob(entree.get("objet", ""))
    if not blob.exists():
        return render_template("errors/404.html"), 404
    # En-tête Content-Disposition : un CR/LF dans le nom (charge falsifiée
    # à la finalisation — le nom d'origine est conservé VERBATIM dans
    # l'enveloppe) ferait lever werkzeug. Contrôles retirés AU POINT
    # D'USAGE seulement — l'enveloppe et le manifeste probants restent
    # intacts.
    nom = "".join(
        c for c in (entree.get("name") or "document") if ord(c) >= 32
    ) or "document"
    # §7.5 — téléchargement FORCÉ (attachment), content_type DÉCLARÉ, jamais
    # de rendu en ligne depuis la quarantaine. REDIRECTION vers un URL signé
    # V4 (15 min) plutôt que send_file : App Engine Standard PLAFONNE toute
    # réponse à 32 Mo (« Response size was too large » — 2026-08-12, un lot
    # de 68 et 128 Mo répondait 500), donc les octets vont de GCS au
    # navigateur sans transiter par l'application.
    try:
        url = sign_blob_url(blob, {
            "response-content-disposition": build_attachment_disposition(nom),
            "response-content-type": (
                entree.get("content_type") or "application/octet-stream"
            ),
        })
    except Exception:
        logger.exception("reception: quarantine signed url failed")
        return _rediriger(erreur="Téléchargement impossible. Réessayez.")
    return redirect(url)


def _nom_par_defaut(nom_client: str) -> str:
    """The display name a versed file gets when the lawyer typed none.

    The client's file name, made acceptable to the model rather than
    refused there (the lawyer never typed it): NO chevron at all — a single
    ``sanitize`` pass can leave one (« <<a>b> » → « <b> »), which the model
    would then refuse — and bounded to its ceiling. Control characters are
    dropped like the storage name's.
    """
    propre = "".join(
        ch for ch in nom_client if ch not in "<>" and ch.isprintable()
    ).strip()
    return propre[:DISPLAY_NAME_MAX].strip()


def _categorie_choisie(soumise: object) -> bool:
    """D18 — la catégorie SOUMISE au versement est-elle un choix du juriste ?

    Vrai seulement quand le sélecteur a été déplacé hors de sa valeur
    présélectionnée (:data:`VERSEMENT_DEFAULT_CATEGORY`) ; un champ absent
    ou vide n'est le choix de personne."""
    return (isinstance(soumise, str) and bool(soumise.strip())
            and soumise.strip() != VERSEMENT_DEFAULT_CATEGORY)


def _resoudre_dossier_cible_folder(
    dossier_reel: str, chosen_folder_id: str
) -> tuple[Optional[dict], str, Optional[str]]:
    """Resolve destination folder (strictly when chosen, else SYSTEM_ROLE_PORTAIL)."""
    if chosen_folder_id:
        try:
            folder = get_folder_strict(dossier_reel, chosen_folder_id)
        except Exception as exc:
            logger.error("reception: target folder read failed: %s", type(exc).__name__)
            return None, "", (
                "Lecture du sous-dossier cible impossible. Le fichier reste en quarantaine."
            )
        if folder is None:
            return None, "", (
                "Sous-dossier introuvable dans ce dossier. Le fichier reste en quarantaine."
            )
        return folder, _dossier_folder_breadcrumb(dossier_reel, folder), None
    folder, folder_errors = ensure_system_folder(dossier_reel, SYSTEM_ROLE_PORTAIL)
    if folder is None:
        return None, "", (
            (folder_errors[0] if folder_errors else
             f"Le dossier « {PORTAL_FOLDER_NAME} » est indisponible.")
            + " Le fichier reste en quarantaine."
        )
    return folder, _chemin_recus(folder), None


@reception_bp.post("/lots/<inv_id>/<batch>/fichiers/<int:seq>/verser")
@login_required
def verser(inv_id: str, batch: str, seq: int):
    onglet = _onglet_soumis()
    manifeste, entree = _entree_ou_none(inv_id, batch, seq)
    if entree is None:
        return _rediriger(erreur="Fichier introuvable.", onglet=onglet)
    if entree.get("etat") != "reçu":
        return _rediriger(erreur="Ce fichier a déjà été traité.", onglet=onglet)
    if not _versable(entree):
        return _rediriger(
            erreur=(
                "Ce fichier ne peut pas être versé tel quel : seuls les PDF, "
                "Word (doc/docx), Excel (xls/xlsx), JPEG, PNG, TIFF, ZIP et "
                "courriels (.eml/.msg) de 200 Mo ou moins sont admis au "
                "dossier. Téléchargez-le et traitez-le hors application."
            ),
            onglet=onglet,
        )

    dossier_id = request.form.get("dossier_id", "").strip()
    if not is_addressable_id(dossier_id):
        return _rediriger(erreur="Choisissez le dossier de destination.", onglet=onglet)
    try:
        dossier = get_dossier_strict(dossier_id)
    except Exception as exc:
        logger.error("reception: dossier read failed: %s", type(exc).__name__)
        return _rediriger(erreur=DOSSIER_READ_FAILED, onglet=onglet)
    if dossier is None:
        return _rediriger(erreur="Choisissez le dossier de destination.", onglet=onglet)

    try:
        user_id = storage_identity.request_uid()
    except storage_identity.StorageIdentityUnavailable as exc:
        return _rediriger(erreur=storage_identity.public_message(exc), onglet=onglet)

    try:
        blob = _bucket().blob(entree["objet"])
        blob.reload()
    except Exception as exc:
        logger.error("reception: quarantine blob reload failed: %s",
                     type(exc).__name__)
        return _rediriger(erreur="Lecture du fichier en quarantaine impossible.", onglet=onglet)
    if int(blob.size or 0) > MAX_FILE_SIZE:
        log_portail_event(
            "versement_divergence", "failure",
            invitation_id=inv_id, batch=batch, seq=seq, reason="taille",
        )
        return _rediriger(
            erreur=(
                "Le fichier en quarantaine ne correspond plus au manifeste "
                "(taille modifiée depuis la réception). Versement refusé — "
                "vérifiez le lot avant toute décision."
            ),
            onglet=onglet,
        )

    sha_manifeste = entree.get("sha512") or ""
    if sha_manifeste:
        try:
            with blob.open("rb") as flux:
                calcule = sha512_flux(flux)
        except Exception as exc:
            logger.error("reception: quarantine hash failed: %s",
                         type(exc).__name__)
            return _rediriger(
                erreur="Lecture du fichier en quarantaine impossible.",
                onglet=onglet,
            )
        if calcule != sha_manifeste:
            log_portail_event(
                "versement_divergence", "failure",
                invitation_id=inv_id, batch=batch, seq=seq, reason="sha512",
            )
            return _rediriger(
                erreur=(
                    "Le fichier en quarantaine ne correspond plus à l'empreinte "
                    "SHA-512 calculée à la réception. Versement refusé — "
                    "retéléchargez le fichier et vérifiez le lot."
                ),
                onglet=onglet,
            )

    dossier_reel = dossier["id"]
    chosen_folder_id = (request.form.get("folder_id") or "").strip()
    folder, chemin_cible, folder_err = _resoudre_dossier_cible_folder(
        dossier_reel, chosen_folder_id
    )
    if folder is None:
        return _rediriger(erreur=folder_err or "Dossier cible indisponible.", onglet=onglet)

    metadata = {
        "category": request.form.get("category", "autre"),
        "display_name": (request.form.get("display_name") or "").strip()
        or _nom_par_defaut(entree.get("name") or ""),
        "tags": ["portail"],
        "folder_id": folder["id"],
    }
    portail = {
        "portail_invitation_id": inv_id,
        "portail_lot": batch,
        "portail_sha512": entree.get("sha512") or "",
    }
    document, errors = ingest_blob_as_document(
        blob,
        dossier_reel,
        dossier.get("file_number", ""),
        entree.get("name") or "document",
        metadata,
        user_id,
        portail=portail,
        lawyer_set_category=_categorie_choisie(request.form.get("category")),
    )
    if document is None and concurrency.is_outcome_uncertain(errors):
        return _rediriger(
            erreur=(
                "Le versement n'a pas pu être confirmé : le fichier est "
                f"peut-être déjà au dossier {dossier.get('file_number', '')}, "
                f"sous « {chemin_cible} ». Vérifiez-le avant de le "
                "verser de nouveau ; il reste en quarantaine. S'il y figure, "
                "refusez-le pour clore le lot."
            ),
            onglet=onglet,
        )
    if errors or document is None:
        return _rediriger(erreur=" ".join(errors) or "Versement impossible.", onglet=onglet)

    entree["etat"] = "versé"
    entree["verse_dossier"] = dossier.get("file_number", "")
    entree["verse_nom"] = metadata["display_name"]
    log_portail_event(
        "document_verse",
        invitation_id=inv_id, batch=batch,
        dossier_id=dossier_reel, document_id=document["id"],
    )
    try:
        _ecrire_manifeste(inv_id, batch, manifeste)
    except Exception as exc:
        logger.error("reception: manifest update failed after ingest: %s",
                     type(exc).__name__)
        return _rediriger(
            erreur=(
                f"Le fichier a bien été versé au dossier "
                f"{dossier.get('file_number', '')}, mais l'état de quarantaine "
                "n'a pas pu être mis à jour : il apparaîtra encore comme "
                "« reçu ». Ne le versez pas une seconde fois — refusez-le pour "
                "clore le lot."
            ),
            onglet=onglet,
        )
    return _rediriger(
        message=f"Fichier versé au dossier {dossier.get('file_number', '')} "
                f"(dossier « {chemin_cible} »).",
        onglet=onglet,
    )


def _chemin_recus(folder: dict) -> str:
    """Where the versement landed, as the browser shows it: « Autres ›
    Reçus du portail » since the default tree (2026-09-30). The folder is
    found by its ROLE and used where it is — a dossier not yet given the
    tree (backfill, « Créer l'arborescence par défaut ») still keeps its
    old ROOT « Reçus du portail », and the banner must not send the lawyer
    looking under an « Autres » that does not hold it. A system folder does
    not move, so a folder with a parent sits under « Autres »."""
    if folder.get("parent_folder_id"):
        return f"{SYSTEM_FOLDER_NAMES[SYSTEM_ROLE_AUTRES]} › {PORTAL_FOLDER_NAME}"
    return PORTAL_FOLDER_NAME


@reception_bp.post("/lots/<inv_id>/<batch>/fichiers/<int:seq>/refuser")
@login_required
def refuser(inv_id: str, batch: str, seq: int):
    onglet = _onglet_soumis()
    manifeste, entree = _entree_ou_none(inv_id, batch, seq)
    if entree is None:
        return _rediriger(erreur="Fichier introuvable.", onglet=onglet)
    if entree.get("etat") not in ("reçu", "manquant"):
        return _rediriger(erreur="Ce fichier a déjà été traité.", onglet=onglet)
    entree["etat"] = "refusé"
    try:
        _ecrire_manifeste(inv_id, batch, manifeste)
    except Exception:
        logger.exception("reception: manifest update failed")
        return _rediriger(erreur="Mise à jour du manifeste impossible.", onglet=onglet)
    log_portail_event("document_refuse", invitation_id=inv_id, batch=batch)
    return _rediriger(message="Fichier refusé.", onglet=onglet)


def _archiver_un_lot(inv_id: str, batch: str) -> tuple[bool, str]:
    """Archive one submitted documents lot if every file was decided.

    Returns ``(ok, french_message_or_error)``.
    """
    bucket = _bucket()
    prefix = _prefix(inv_id, batch)
    archive = f"archive/{inv_id}/{batch}/"

    deja_archive = False
    try:
        manifeste = _lire_manifeste(inv_id, batch)
        if manifeste is None:
            src = bucket.blob(archive + "manifeste.json")
            if src.exists():
                manifeste = json.loads(src.download_as_bytes())
                deja_archive = True
    except Exception:
        logger.exception("reception: manifest read failed")
        return False, "Lecture du lot impossible. Réessayez."
    if manifeste is None:
        return False, "Lot introuvable."

    if not deja_archive:
        if any(f.get("etat") == "reçu" for f in manifeste.get("files") or []):
            return False, "Chaque fichier du lot doit d'abord être versé ou refusé."
        manifeste["etat_lot"] = "traité"
        try:
            _ecrire_manifeste(inv_id, batch, manifeste)
            for blob in list(bucket.list_blobs(prefix=prefix + "files/")):
                blob.delete()
            for nom in ("envelope.json", "manifeste.json"):
                src = bucket.blob(prefix + nom)
                if src.exists():
                    bucket.copy_blob(src, bucket, archive + nom)
                    src.delete()
        except Exception as exc:
            logger.error("reception: lot archive failed: %s", type(exc).__name__)
            return False, "Archivage du lot impossible. Réessayez."
        log_portail_event("lot_traite", invitation_id=inv_id, batch=batch)

    try:
        freres = [
            b for b in bucket.list_blobs(prefix=f"submissions/{inv_id}/")
            if b.name.endswith("/envelope.json")
        ]
    except Exception:
        logger.exception("reception: sibling-lot scan failed")
        freres = [batch]
    if freres:
        return True, (
            "Lot traité et archivé — un autre lot de cette invitation "
            "reste à examiner."
        )
    if not pi.maj_statut(inv_id, "traitée"):
        return False, (
            "Lot archivé, mais mise à jour du statut impossible. Cliquez de "
            "nouveau « Marquer le lot traité » pour réessayer."
        )
    return True, "Lot traité — fichiers de quarantaine purgés, enveloppe et manifeste archivés."


@reception_bp.post("/lots/<inv_id>/<batch>/traiter")
@login_required
def traiter_lot(inv_id: str, batch: str):
    onglet = _onglet_soumis()
    ok, msg = _archiver_un_lot(inv_id, batch)
    if ok:
        return _rediriger(message=msg, onglet=onglet)
    return _rediriger(erreur=msg, onglet=onglet)


def _parse_file_keys(raw_keys: list[str]) -> tuple[list[tuple[str, str, int]], int]:
    """Parse ``<inv_id>:<batch>:<seq>`` checkboxes preserving order and deduplicating."""
    parsed: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str, int]] = set()
    invalid = 0
    for raw in raw_keys:
        parts = (raw or "").strip().split(":")
        if len(parts) != 3:
            invalid += 1
            continue
        inv_id, batch, seq_raw = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not is_addressable_id(inv_id) or not batch or "/" in batch:
            invalid += 1
            continue
        try:
            seq = int(seq_raw)
        except ValueError:
            invalid += 1
            continue
        if seq < 0:
            invalid += 1
            continue
        key = (inv_id, batch, seq)
        if key not in seen:
            seen.add(key)
            parsed.append(key)
    return parsed, invalid


@reception_bp.post("/lots/verser-lot")
@login_required
def verser_lot():
    """Versement groupé des fichiers cochés vers un dossier et sous-dossier choisis."""
    onglet = _onglet_soumis()
    parsed, invalides = _parse_file_keys(request.form.getlist("file_keys"))
    if not parsed and invalides == 0:
        return _rediriger(erreur="Sélectionnez au moins un fichier à verser.", onglet=onglet)
    if not parsed:
        return _rediriger(erreur="Sélection de fichiers invalide.", onglet=onglet)

    dossier_id = (request.form.get("dossier_id") or "").strip()
    if not is_addressable_id(dossier_id):
        return _rediriger(erreur="Choisissez le dossier de destination.", onglet=onglet)
    try:
        dossier = get_dossier_strict(dossier_id)
    except Exception as exc:
        logger.error("reception: bulk dossier read failed: %s", type(exc).__name__)
        return _rediriger(erreur=DOSSIER_READ_FAILED, onglet=onglet)
    if dossier is None:
        return _rediriger(erreur="Choisissez le dossier de destination.", onglet=onglet)

    try:
        user_id = storage_identity.request_uid()
    except storage_identity.StorageIdentityUnavailable as exc:
        return _rediriger(erreur=storage_identity.public_message(exc), onglet=onglet)

    dossier_reel = dossier["id"]
    chosen_folder_id = (request.form.get("folder_id") or "").strip()
    folder, chemin_cible, folder_err = _resoudre_dossier_cible_folder(
        dossier_reel, chosen_folder_id
    )
    if folder is None:
        return _rediriger(erreur=folder_err or "Dossier cible indisponible.", onglet=onglet)

    category_raw = request.form.get("category", "autre")
    lawyer_set_cat = _categorie_choisie(category_raw)
    bucket = _bucket()
    manifest_cache: dict[tuple[str, str], Optional[dict]] = {}
    manifest_err_lots: set[tuple[str, str]] = set()
    dirty_lots: set[tuple[str, str]] = set()

    ok_count = 0
    uncertain_count = 0
    fail_reasons: list[str] = []
    if invalides:
        fail_reasons.append(f"{invalides} sélection(s) invalide(s)")

    for inv_id, batch, seq in parsed:
        lot_key = (inv_id, batch)
        if lot_key not in manifest_cache and lot_key not in manifest_err_lots:
            try:
                manifest_cache[lot_key] = _lire_manifeste(inv_id, batch)
            except Exception as exc:
                logger.error("reception: bulk manifest read failed: %s", type(exc).__name__)
                manifest_err_lots.add(lot_key)
                fail_reasons.append("lecture d'un manifeste impossible")
                continue
        if lot_key in manifest_err_lots:
            fail_reasons.append("manifeste illisible")
            continue
        manifeste = manifest_cache.get(lot_key)
        fichiers = (manifeste or {}).get("files") or []
        if manifeste is None or seq >= len(fichiers):
            fail_reasons.append("fichier introuvable")
            continue
        entree = fichiers[seq]
        if entree.get("etat") != "reçu":
            fail_reasons.append("déjà traité")
            continue
        if not _versable(entree):
            fail_reasons.append("format ou taille non versable")
            continue

        try:
            blob = bucket.blob(entree["objet"])
            blob.reload()
        except Exception as exc:
            logger.error("reception: bulk quarantine blob reload failed: %s", type(exc).__name__)
            fail_reasons.append("fichier en quarantaine illisible")
            continue
        if int(blob.size or 0) > MAX_FILE_SIZE:
            log_portail_event(
                "versement_divergence", "failure",
                invitation_id=inv_id, batch=batch, seq=seq, reason="taille",
            )
            fail_reasons.append("taille modifiée en quarantaine")
            continue

        sha_manifeste = entree.get("sha512") or ""
        if sha_manifeste:
            try:
                with blob.open("rb") as flux:
                    calcule = sha512_flux(flux)
            except Exception as exc:
                logger.error("reception: bulk quarantine hash failed: %s", type(exc).__name__)
                fail_reasons.append("vérification d'empreinte impossible")
                continue
            if calcule != sha_manifeste:
                log_portail_event(
                    "versement_divergence", "failure",
                    invitation_id=inv_id, batch=batch, seq=seq, reason="sha512",
                )
                fail_reasons.append("empreinte SHA-512 divergente")
                continue

        display_name = _nom_par_defaut(entree.get("name") or "")
        metadata = {
            "category": category_raw,
            "display_name": display_name,
            "tags": ["portail"],
            "folder_id": folder["id"],
        }
        portail = {
            "portail_invitation_id": inv_id,
            "portail_lot": batch,
            "portail_sha512": sha_manifeste,
        }
        document, errors = ingest_blob_as_document(
            blob,
            dossier_reel,
            dossier.get("file_number", ""),
            entree.get("name") or "document",
            metadata,
            user_id,
            portail=portail,
            lawyer_set_category=lawyer_set_cat,
        )
        if document is None and concurrency.is_outcome_uncertain(errors):
            uncertain_count += 1
            fail_reasons.append("confirmation de versement incertaine")
            continue
        if errors or document is None:
            fail_reasons.append(errors[0] if errors else "versement refusé")
            continue

        entree["etat"] = "versé"
        entree["verse_dossier"] = dossier.get("file_number", "")
        entree["verse_nom"] = display_name
        dirty_lots.add(lot_key)
        ok_count += 1
        log_portail_event(
            "document_verse",
            invitation_id=inv_id, batch=batch,
            dossier_id=dossier_reel, document_id=document["id"],
        )

    manifest_write_failures = 0
    for inv_id, batch in dirty_lots:
        try:
            _ecrire_manifeste(inv_id, batch, manifest_cache[(inv_id, batch)])
        except Exception as exc:
            manifest_write_failures += 1
            logger.error("reception: bulk manifest update failed after ingest: %s", type(exc).__name__)

    if fail_reasons or manifest_write_failures or uncertain_count:
        parts = []
        if ok_count:
            parts.append(f"{ok_count} fichier(s) versé(s) vers « {chemin_cible} »")
        if uncertain_count:
            parts.append(f"{uncertain_count} à vérifier au dossier (issue incertaine, ne pas re-verser)")
        if manifest_write_failures:
            parts.append(f"{manifest_write_failures} lot(s) dont l'état de quarantaine n'a pas été enregistré")
        other_fails = len(fail_reasons) - uncertain_count
        if other_fails > 0:
            unique_reasons = list(dict.fromkeys(r for r in fail_reasons if r != "confirmation de versement incertaine"))[:3]
            parts.append(f"{other_fails} non versé(s) ({', '.join(unique_reasons)})")
        return _rediriger(erreur=" ; ".join(parts) + ".", onglet=onglet)

    return _rediriger(
        message=(
            f"{ok_count} fichier(s) versé(s) au dossier {dossier.get('file_number', '')} "
            f"(dossier « {chemin_cible} »)."
        ),
        onglet=onglet,
    )


@reception_bp.post("/lots/refuser-lot")
@login_required
def refuser_lot():
    """Refus groupé des fichiers sélectionnés."""
    onglet = _onglet_soumis()
    parsed, invalides = _parse_file_keys(request.form.getlist("file_keys"))
    if not parsed and invalides == 0:
        return _rediriger(erreur="Sélectionnez au moins un fichier à refuser.", onglet=onglet)
    if not parsed:
        return _rediriger(erreur="Sélection de fichiers invalide.", onglet=onglet)

    manifest_cache: dict[tuple[str, str], Optional[dict]] = {}
    manifest_err_lots: set[tuple[str, str]] = set()
    dirty_lots: set[tuple[str, str]] = set()
    refused_by_lot: dict[tuple[str, str], int] = {}
    ok_count = 0
    skipped = invalides

    for inv_id, batch, seq in parsed:
        lot_key = (inv_id, batch)
        if lot_key not in manifest_cache and lot_key not in manifest_err_lots:
            try:
                manifest_cache[lot_key] = _lire_manifeste(inv_id, batch)
            except Exception as exc:
                logger.error("reception: bulk refuse manifest read failed: %s", type(exc).__name__)
                manifest_err_lots.add(lot_key)
                skipped += 1
                continue
        if lot_key in manifest_err_lots:
            skipped += 1
            continue
        manifeste = manifest_cache.get(lot_key)
        fichiers = (manifeste or {}).get("files") or []
        if manifeste is None or seq >= len(fichiers):
            skipped += 1
            continue
        entree = fichiers[seq]
        if entree.get("etat") not in ("reçu", "manquant"):
            skipped += 1
            continue
        entree["etat"] = "refusé"
        dirty_lots.add(lot_key)
        refused_by_lot[lot_key] = refused_by_lot.get(lot_key, 0) + 1

    write_errors = 0
    for inv_id, batch in dirty_lots:
        try:
            _ecrire_manifeste(inv_id, batch, manifest_cache[(inv_id, batch)])
            ok_count += refused_by_lot[(inv_id, batch)]
            log_portail_event("document_refuse", invitation_id=inv_id, batch=batch)
        except Exception as exc:
            write_errors += 1
            logger.error("reception: bulk refuse manifest write failed: %s", type(exc).__name__)

    if write_errors or skipped or not ok_count:
        msg_parts = []
        if ok_count:
            msg_parts.append(f"{ok_count} fichier(s) refusé(s)")
        if skipped:
            msg_parts.append(f"{skipped} introuvable(s) ou déjà traité(s)")
        if write_errors:
            msg_parts.append(f"{write_errors} lot(s) non mis à jour")
        return _rediriger(erreur=" ; ".join(msg_parts) + ".", onglet=onglet)

    return _rediriger(message=f"{ok_count} fichier(s) refusé(s).", onglet=onglet)


@reception_bp.post("/actions-lot")
@login_required
def actions_lot():
    """Actions groupées sur le fil unifié et les invitations (archiver / écarter / révoquer / refuser)."""
    onglet = _onglet_soumis()
    action = (request.form.get("bulk_action") or "").strip()
    raw_keys = request.form.getlist("feed_keys")
    if action not in ("archiver_lot", "ecarter", "revoquer", "refuser_ouverture"):
        return _rediriger(erreur="Action groupée non reconnue.", onglet=onglet)
    if not raw_keys:
        return _rediriger(erreur="Sélectionnez au moins un élément.", onglet=onglet)

    seen: set[str] = set()
    keys = []
    for k in raw_keys:
        cleaned = (k or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            keys.append(cleaned)

    ok_count = 0
    fail_messages: list[str] = []

    if action == "archiver_lot":
        for k in keys:
            parts = k.split(":")
            if len(parts) != 3 or parts[0] != "lot" or not is_addressable_id(parts[1]) or not parts[2]:
                fail_messages.append("élément non compatible avec l'archivage de lot")
                continue
            ok, msg = _archiver_un_lot(parts[1], parts[2])
            if ok:
                ok_count += 1
            else:
                fail_messages.append(msg)
    elif action == "ecarter":
        for k in keys:
            parts = k.split(":")
            if len(parts) not in (2, 3) or parts[0] != "transcription" or not is_addressable_id(parts[1]):
                fail_messages.append("élément non compatible avec l'écartement")
                continue
            hid = parts[1]
            etag = parts[2] if len(parts) == 3 else ""
            _doc, errs = transcription_reunion.ecarter_transcription(
                hid, expected_etag=etag
            )
            if errs:
                fail_messages.append(errs[0])
            else:
                ok_count += 1
        _invalidater_pastille()
    elif action == "revoquer":
        for k in keys:
            parts = k.split(":")
            inv_id = parts[1] if len(parts) >= 2 and parts[0] in ("inv", "lot", "ouverture") else ""
            if not is_addressable_id(inv_id):
                fail_messages.append("invitation invalide")
                continue
            try:
                inv = pi.lire_invitation_strict(inv_id)
            except Exception as exc:
                logger.error("reception: bulk revoke read failed: %s", type(exc).__name__)
                fail_messages.append("lecture d'une invitation impossible")
                continue
            if inv is None:
                fail_messages.append("invitation introuvable")
                continue
            if inv.get("statut") in ("traitée", "refusée", "révoquée"):
                fail_messages.append("invitation déjà close")
                continue
            if pi.revoquer(inv_id):
                log_portail_event("invitation_revoquee", invitation_id=inv_id)
                ok_count += 1
            else:
                fail_messages.append("révocation impossible")
    elif action == "refuser_ouverture":
        for k in keys:
            parts = k.split(":")
            if len(parts) != 3 or parts[0] != "ouverture" or not is_addressable_id(parts[1]) or not parts[2]:
                fail_messages.append("élément non compatible avec le refus d'ouverture")
                continue
            try:
                strict_inv = pi.lire_invitation_strict(parts[1])
            except Exception:
                log_unexpected("batch ouverture read failed", invitation_id=parts[1])
                fail_messages.append("ouverture inaccessible (erreur de lecture)")
                continue
            if strict_inv is None or strict_inv.get("type") != "intake":
                fail_messages.append("ouverture introuvable")
                continue
            if strict_inv.get("statut") in ("traitée", "refusée", "révoquée"):
                fail_messages.append("ouverture déjà close")
                continue
            try:
                env = _lire_enveloppe(parts[1], parts[2])
            except Exception:
                log_unexpected("batch ouverture envelope read failed", invitation_id=parts[1], batch=parts[2])
                fail_messages.append("enveloppe d'ouverture inaccessible (erreur de lecture)")
                continue
            if env is None:
                fail_messages.append("ouverture introuvable ou déjà close")
                continue
            _cloturer(strict_inv, parts[2], "refusée")
            log_portail_event("intake_refuse", "refused", invitation_id=parts[1], batch=parts[2])
            ok_count += 1
        if ok_count:
            _invalidater_pastille()

    label_ok = {
        "archiver_lot": "lot(s) archivé(s)",
        "ecarter": "transcription(s) écartée(s)",
        "revoquer": "invitation(s) révoquée(s)",
        "refuser_ouverture": "ouverture(s) refusée(s)",
    }[action]

    if fail_messages or not ok_count:
        parts_msg = []
        if ok_count:
            parts_msg.append(f"{ok_count} {label_ok}")
        uniq_fails = list(dict.fromkeys(fail_messages))[:3]
        parts_msg.append(f"{len(fail_messages)} refus/échec(s) ({', '.join(uniq_fails)})")
        return _rediriger(erreur=" ; ".join(parts_msg) + ".", onglet=onglet)

    return _rediriger(message=f"{ok_count} {label_ok}.", onglet=onglet)


# ── Actions « Rendez-vous » (Bookings L2 §5.2-5.4) ───────────────────────


def _rdv_ou_erreur(hid: str) -> Optional[dict]:
    """Fetch a Bookings or portal RDV hearing or None."""
    hearing = get_hearing(hid)
    if not hearing or hearing.get("source") not in RDV_SOURCES:
        return None
    return hearing


# A decision posted from a page older than the stored rendez-vous — the
# Bookings sync moves the slot of a pending import silently, another tab
# may have decided already. Said in the rendez-vous' own terms: the model's
# generic refusal speaks of « modifications » the lawyer never made here.
_RDV_PERIME = (
    "Ce rendez-vous a changé depuis l'affichage de la page — il a peut-être "
    "été mis à jour depuis un autre onglet ou par le client. Rien n'a été fait, "
    "Outlook n'a pas été touché et rien n'a été envoyé : vérifiez-le ci-dessous, "
    "puis refaites votre choix."
)


def _erreur_rdv(errors: list[str]):
    """A refusal bounced on ``?erreur=`` (a 2xx-bound redirect — htmx never
    swaps a 4xx). NOTHING happened when the service returns errors."""
    if concurrency.is_stale(errors):
        return _rediriger(erreur=_RDV_PERIME, onglet="rdv")
    return _rediriger(erreur=" ".join(errors), onglet="rdv")


@reception_bp.post("/rdv/<hid>/confirmer")
@login_required
def rdv_confirmer(hid: str):
    expected = edit_conflict.submitted_etag()
    # The linked contact is matched by the SERVICE on the requester's exact
    # address; a posted partie_id (pages rendered before lot 1a) is ignored.
    hearing, errors, rapport = rendez_vous.confirmer(
        hid, lier_partie=request.form.get("lier") == "on",
        expected_etag=expected,
        teams_agenda=request.form.get("teams_agenda"),
    )
    if errors:
        return _erreur_rdv(errors)
    if not rapport["changed"]:
        return _rediriger(message="Ce rendez-vous était déjà confirmé.",
                          onglet="rdv")

    message = ("Rendez-vous confirmé — il apparaît au calendrier et se "
               "synchronise avec vos appareils.")
    if rapport["warning"]:
        message += " " + rapport["warning"]
    # Déclencheur (a) de la phase L3 : le courriel du rendez-vous ne
    # correspond à aucune partie → proposer le formulaire d'ouverture. Une
    # panne d'émission n'annule JAMAIS la confirmation, qui est déjà commise
    # (CTag bumpé) : bandeau, jamais un échec. Cet envoi reste ICI — le
    # service ne l'émet jamais (services/rendez_vous.py).
    if (
        Config.FEATURE_INTAKE
        and request.form.get("intake") == "on"
        and not rapport["partie_liee"]
        and (hearing.get("client_email") or "").strip()
    ):
        try:
            _inv, erreurs, lien_manuel = emission.emettre_invitation(
                "intake",
                hearing["client_email"],
                client_name=hearing.get("client_nom", ""),
                display_label="Ouverture de votre dossier client",
            )
        except Exception:
            logger.exception("reception: intake invitation from rdv failed")
            erreurs, lien_manuel = ["Erreur inattendue."], ""
        if erreurs:
            message += (" Le formulaire d'ouverture n'a PAS pu être envoyé — "
                        "réessayez depuis l'onglet Ouvertures.")
        elif lien_manuel:
            message += (" Le formulaire d'ouverture a été créé, mais le "
                        "courriel n'est pas parti : transmettez le lien depuis "
                        "l'onglet Ouvertures.")
        else:
            message += " Le formulaire d'ouverture a été envoyé au client."

    return _rediriger(message=message, onglet="rdv")


@reception_bp.post("/rdv/<hid>/refuser")
@login_required
def rdv_refuser(hid: str):
    # Decision 2026-07-25 (Calendars.ReadWrite): a refusal CANCELS the
    # Outlook meeting (notifying the client via Bookings) — see
    # services.rendez_vous.refuser for the order that makes it safe: the
    # page's version is checked BEFORE Graph, the write after it is
    # unconditional.
    expected = edit_conflict.submitted_etag()
    hearing, errors, rapport = rendez_vous.refuser(hid, expected_etag=expected)
    if errors:
        return _erreur_rdv(errors)
    if rapport["warning"]:
        # Refused, but Outlook needs the lawyer's hand — or the refusal did
        # not reach Athéna. Red, because something remains to be done.
        return _rediriger(erreur=rapport["warning"], onglet="rdv")
    if not rapport["changed"]:
        return _rediriger(message="Ce rendez-vous était déjà refusé.",
                          onglet="rdv")
    envoye_portail = False
    if rapport.get("envoyer_refus_portail") and hearing is not None:
        envoye_portail, warn_mail = emission.envoyer_refus_rdv_portail(hearing)
        if warn_mail:
            return _rediriger(erreur=warn_mail, onglet="rdv")
    if envoye_portail:
        message = "Rendez-vous refusé — un courriel de refus poli a été transmis."
    elif rapport["graph_cancelled"]:
        message = (
            "Rendez-vous refusé — la réunion Outlook a été annulée et le client "
            "notifié."
        )
    else:
        message = "Rendez-vous refusé."
    return _rediriger(message=message, onglet="rdv")


@reception_bp.post("/rdv/<hid>/divergence/<action>")
@login_required
def rdv_divergence(hid: str, action: str):
    if action not in ("appliquer", "ignorer", "annuler", "conserver"):
        return _rediriger(erreur="Action inconnue.", onglet="rdv")
    hearing = _rdv_ou_erreur(hid)
    if hearing is None:
        return _rediriger(erreur="Rendez-vous introuvable.", onglet="rdv")
    div = hearing.get("bookings_divergence") or {}
    if not div.get("motif"):
        return _rediriger(erreur="Aucune divergence à traiter.", onglet="rdv")

    # The slot is content (data); the divergence and the confirmation gate
    # are server-owned (server) — models.hearing.update_hearing refuses the
    # other split.
    data: dict = {}
    server: dict = {}
    bump = False
    tombstone = False
    if action == "appliquer" and div.get("motif") == "modifié_côté_client":
        # Apply the client's new slot from the stashed values, then clear. The
        # event STAYS live (still confirmed) — an update, not a removal, so no
        # tombstone.
        server["bookings_divergence"] = None
        try:
            if div.get("nouveau_debut"):
                data["start_datetime"] = datetime.fromisoformat(div["nouveau_debut"])
            if div.get("nouveau_fin"):
                data["end_datetime"] = datetime.fromisoformat(div["nouveau_fin"])
        except ValueError:
            logger.warning("reception: bad divergence datetime")
        bump = True
    elif action == "annuler" and div.get("motif") == "annulé_côté_client":
        # A CONFIRMED (already-synced) event LEAVES the DAV live set. A CTag
        # bump alone does NOT propagate the removal — DavX5 keeps its local
        # copy unless a tombstone reports the 404 (RFC 6578). Record one, or
        # the cancelled meeting lingers on the device forever.
        server["confirmation"] = "annulée_client"
        server["bookings_divergence"] = None
        bump = True
        tombstone = True
    else:
        # ignorer / conserver → dismiss the alert (vu=True), keep the event.
        server["bookings_divergence"] = {**div, "vu": True}
        # The event is unchanged on the phone, but update_hearing regenerates
        # its ETAG — and a confirmed rendez-vous is a live DAV member. Without
        # the bump the phone keeps the old etag, and its next edit of the
        # event (If-Match on it) answers 412, the server version then winning
        # over the phone's edit (finitions, sync-2).
        bump = (hearing.get("confirmation") or "") == ""

    _updated, errors = update_hearing(hid, data, server_fields=server)
    if errors:
        return _rediriger(erreur=" ".join(errors), onglet="rdv")
    sync_name = collection_for(hearing.get("dossier_id"))
    if tombstone:
        record_tombstone(sync_name, hid)
    if bump:
        bump_ctag(sync_name)
    log_bookings_event("reception_rdv_divergence_traitee", hearing_id=hid,
                       action=action)
    return _rediriger(message="Divergence traitée.", onglet="rdv")


# ── Ouvertures : actions (§5.3) ──────────────────────────────────────────


def _ouverture_ou_erreur(inv_id: str, batch: str):
    """(invitation, enveloppe) ou (None, None) — jamais une exception."""
    try:
        invitation = pi.lire_invitation(inv_id)
    except Exception:
        logger.exception("reception: intake invitation read failed")
        return None, None
    if invitation is None or invitation.get("type") != "intake":
        return None, None
    try:
        return invitation, _lire_enveloppe(inv_id, batch)
    except Exception:
        logger.exception("reception: intake envelope read failed")
        return invitation, None


def _valeurs_choisies(donnees: dict) -> dict:
    """Champs contact issus des seules cases cochées.

    Le formulaire poste ``appliquer=<champ>`` par case ; rien d'autre n'entre.
    Une case non cochée n'est PAS une instruction d'effacer : elle est
    simplement absente du dictionnaire, et update_partie fusionne — donc la
    valeur au dossier survit.
    """
    choisis = set(request.form.getlist("appliquer"))
    valeurs: dict = {}
    for champ, cible, _libelle in _CORRESPONDANCE:
        if champ in choisis:
            valeur = (donnees.get(champ) or "").strip()
            if champ == "occupation" and not _occupation_versable(valeur):
                continue
            if valeur:
                valeurs[cible] = valeur
    return valeurs


def _creer_adverses_coches(
    adverses: list[dict],
    inv_id: str,
    *,
    collect: Optional[list[dict]] = None,
) -> int:
    """Créer les contacts « partie adverse » dont la case reste cochée (D-L3-2).

    Chaque création bumpe le CTag du carnet — le compteur retourné sert au
    message ; le bump, lui, appartient à l'appelant (un seul par requête).
    """
    coches = set(request.form.getlist("creer_adverse"))
    crees = 0
    for ligne in adverses:
        nom = (ligne.get("nom") or "").strip()
        if not nom or nom not in coches:
            continue
        partie_adv, erreurs = create_partie({
            "type": "individual",
            "contact_role": "partie_adverse",
            "last_name": nom,
            # Provenance dans le champ de notes EXISTANT — aucun champ nouveau,
            # et le juriste voit d'où vient la fiche.
            "notes": f"Déclaré par le client via le portail, invitation {inv_id}.",
        })
        if erreurs or partie_adv is None:
            logger.warning("reception: adverse contact refused: %s", erreurs)
            continue
        if collect is not None:
            collect.append(partie_adv)
        crees += 1
        log_portail_event("intake_adverse_cree", invitation_id=inv_id)
    return crees


def _creer_dossier_depuis_ouverture(
    client_partie: dict,
    adverses: list[dict],
    inv_id: str,
) -> tuple[Optional[dict], list[str]]:
    """Create a dossier when Réception intake approves with ``ouvrir_dossier``."""
    client_nom = display_name(client_partie) or "Client"
    adv_nom = display_name(adverses[0]) if adverses else ""
    titre_defaut = f"{client_nom} c. {adv_nom}" if adv_nom else client_nom
    numero = (
        request.form.get("dossier_file_number", "").strip()
        or suggest_file_number()
    )
    titre = request.form.get("dossier_title", "").strip() or titre_defaut
    payload = {
        "file_number": numero,
        "title": titre,
        "forum_type": "prejudiciaire",
        "court_file_number": "Préjudiciaire",
        "clients": [{
            "id": client_partie["id"],
            "name": client_nom,
            "roles": [],
            "avocat_id": "",
            "avocat_name": "",
        }],
        "opposing_parties": [{
            "id": p["id"],
            "name": display_name(p),
            "roles": [],
            "avocat_id": "",
            "avocat_name": "",
        } for p in adverses if p.get("id")],
    }
    dossier_doc, errors = create_dossier(payload, kyc_source=kyc.SOURCE_JURISTE)
    if errors or dossier_doc is None:
        return None, errors or ["Impossible de créer le dossier."]
    new_id = dossier_doc["id"]
    try:
        ensure_default_tree(new_id)
    except Exception:
        logger.exception("reception: default tree failed for dossier %s", new_id)
    try:
        ensure_ouverture_protocol(new_id, dossier=dossier_doc)
    except Exception:
        logger.exception("reception: ouverture protocol failed for dossier %s", new_id)
    log_portail_event("intake_dossier_cree", invitation_id=inv_id, dossier_id=new_id)
    return dossier_doc, []


def _cloturer(invitation: dict, batch: str, statut: str) -> None:
    """Clore une ouverture : statut + archivage de TOUTES ses enveloppes.

    Toutes, pas seulement celle qui vient d'être traitée : la ré-entrée permet
    au client de corriger, donc une invitation peut porter plusieurs lots dont
    seul le dernier a été examiné. Les précédents restaient sous
    ``submissions/``, que le cycle de vie purge à 90 jours — ce qui aurait fait
    disparaître, sans trace, ce que le client avait d'abord déclaré. Un
    formulaire traité ou refusé se conserve intégralement, comme un lot de
    documents.
    """
    inv_id = invitation["id"]
    pi.maj_statut(inv_id, statut)
    batches = [
        s.get("batch") for s in (invitation.get("soumissions") or [])
        if s.get("batch")
    ]
    if batch and batch not in batches:
        batches.append(batch)
    for b in batches:
        try:
            _archiver_enveloppe(inv_id, b)
        except Exception:
            # L'archivage rate → le cycle de vie du seau purgera à 90 jours.
            # Ne jamais faire échouer un versement déjà commis pour un
            # déplacement d'objet, ni interrompre les lots suivants.
            logger.exception("reception: intake envelope archive failed")


@reception_bp.post("/ouvertures/<inv_id>/<batch>/creer")
@login_required
def ouverture_creer(inv_id: str, batch: str):
    invitation, enveloppe = _ouverture_ou_erreur(inv_id, batch)
    if invitation is None or not enveloppe:
        return _rediriger(erreur="Soumission introuvable.", onglet="ouvertures")
    donnees = enveloppe.get("donnees") or {}

    valeurs = {
        cible: (donnees.get(champ) or "").strip()
        for champ, cible, _l in _CORRESPONDANCE
        if (donnees.get(champ) or "").strip()
    }
    occupation_ecartee = False
    if "occupation" in valeurs and not _occupation_versable(valeurs["occupation"]):
        del valeurs["occupation"]
        occupation_ecartee = True
    valeurs["type"] = (
        "organization" if donnees.get("nature") == "morale" else "individual"
    )
    valeurs["contact_role"] = "client"
    # Section Conformité INTACTE (§5.3) : la collecte n'est pas une
    # vérification. create_partie applique déjà « non_vérifié » par défaut —
    # ne rien écrire ici est donc délibéré, pas un oubli.
    valeurs["notes"] = (
        f"Ouverture transmise par le client via le portail, "
        f"invitation {inv_id}."
    )

    partie, erreurs = create_partie(valeurs)
    if erreurs or partie is None:
        return _rediriger(
            erreur=" ".join(erreurs) or "Création impossible.",
            onglet="ouvertures",
        )

    adverses_crees: list[dict] = []
    crees = _creer_adverses_coches(
        enveloppe.get("parties_adverses") or [], inv_id, collect=adverses_crees,
    )
    # ⚠️ Le carnet DavX5 suit la ROUTE, jamais le modèle : une création de
    # partie qui l'oublie n'atteint JAMAIS le téléphone, en silence. Une
    # seule porte (services/carnet.py) couvre la fiche cliente et les
    # parties adverses, toutes au carnet.
    apres_ecriture_contact(partie)

    nouveau_dossier = None
    dossier_erreur = ""
    if request.form.get("ouvrir_dossier") == "on":
        nouveau_dossier, d_erreurs = _creer_dossier_depuis_ouverture(
            partie, adverses_crees, inv_id,
        )
        if d_erreurs:
            dossier_erreur = " ".join(d_erreurs)

    _cloturer(invitation, batch, "traitée")
    log_portail_event("intake_partie_creee", invitation_id=inv_id, batch=batch,
                      adverses_crees=crees)
    message = "Fiche créée — vérifiez et complétez-la."
    if occupation_ecartee:
        message += (" L'occupation transmise n'est pas reconnue : "
                    "choisissez-la sur la fiche.")
    if crees:
        message += f" {crees} contact(s) « partie adverse » créé(s)."
    if dossier_erreur:
        return redirect(url_for(
            "parties.partie_detail",
            partie_id=partie["id"],
            message=message,
            erreur=f"Dossier non créé : {dossier_erreur}",
        ))
    if nouveau_dossier is not None:
        return redirect(url_for(
            "dossiers.dossier_detail", dossier_id=nouveau_dossier["id"],
            tab="protocole",
        ))
    # url_for encode le message ; une concaténation le casserait au premier
    # « & » ou accent.
    return redirect(url_for("parties.partie_detail",
                            partie_id=partie["id"], message=message))


@reception_bp.post("/ouvertures/<inv_id>/<batch>/appliquer")
@login_required
def ouverture_appliquer(inv_id: str, batch: str):
    invitation, enveloppe = _ouverture_ou_erreur(inv_id, batch)
    if invitation is None or not enveloppe:
        return _rediriger(erreur="Soumission introuvable.", onglet="ouvertures")
    partie_id = invitation.get("partie_id") or ""
    partie_courante = get_partie(partie_id) if partie_id else None
    if not partie_id or not partie_courante:
        return _rediriger(
            erreur="Le contact lié à cette invitation est introuvable.",
            onglet="ouvertures",
        )

    valeurs = _valeurs_choisies(enveloppe.get("donnees") or {})
    _maj = None
    if valeurs:
        _maj, erreurs = update_partie(partie_id, valeurs)
        if erreurs:
            return _rediriger(erreur=" ".join(erreurs), onglet="ouvertures")

    adverses_crees: list[dict] = []
    crees = _creer_adverses_coches(
        enveloppe.get("parties_adverses") or [], inv_id, collect=adverses_crees,
    )
    if valeurs or crees:
        apres_ecriture_contact(_maj if valeurs else None)

    nouveau_dossier = None
    dossier_erreur = ""
    if request.form.get("ouvrir_dossier") == "on":
        nouveau_dossier, d_erreurs = _creer_dossier_depuis_ouverture(
            _maj or partie_courante, adverses_crees, inv_id,
        )
        if d_erreurs:
            dossier_erreur = " ".join(d_erreurs)

    _cloturer(invitation, batch, "traitée")
    log_portail_event("intake_partie_mise_a_jour", invitation_id=inv_id,
                      batch=batch, champs=len(valeurs), adverses_crees=crees)
    message = (
        f"{len(valeurs)} champ(s) appliqué(s)." if valeurs
        else "Aucun champ appliqué."
    )
    if crees:
        message += f" {crees} contact(s) « partie adverse » créé(s)."
    if dossier_erreur:
        return redirect(url_for(
            "parties.partie_detail",
            partie_id=partie_id,
            message=message,
            erreur=f"Dossier non créé : {dossier_erreur}",
        ))
    if nouveau_dossier is not None:
        return redirect(url_for(
            "dossiers.dossier_detail", dossier_id=nouveau_dossier["id"],
            tab="protocole",
        ))
    return redirect(url_for("parties.partie_detail",
                            partie_id=partie_id, message=message))


@reception_bp.post("/ouvertures/<inv_id>/<batch>/refuser")
@login_required
def ouverture_refuser(inv_id: str, batch: str):
    invitation, _enveloppe = _ouverture_ou_erreur(inv_id, batch)
    if invitation is None:
        return _rediriger(erreur="Soumission introuvable.", onglet="ouvertures")
    # D-L3-3 : aucun courriel au client — le suivi est humain.
    _cloturer(invitation, batch, "refusée")
    log_portail_event("intake_refuse", "refused", invitation_id=inv_id,
                      batch=batch)
    return _rediriger(message="Ouverture refusée.", onglet="ouvertures")


# ── Transcriptions & notes Teams post-réunion (Lot 3) ────────────────────


@reception_bp.post("/transcriptions/verifier")
@login_required
def transcriptions_verifier():
    operator_email = str(session.get("email") or Config.AUTHORIZED_USER_EMAIL or "")
    try:
        res = transcription_reunion.verifier_reunions_terminees(user_id=operator_email)
    except transcription_reunion.LectureImpossible:
        return _rediriger(erreur=transcription_reunion.LECTURE_IMPOSSIBLE, onglet="rdv")
    _invalidater_pastille()
    if res["eligibles"] == 0:
        msg = "Aucune réunion Teams terminée en attente de transcription."
    else:
        msg = (
            f"Vérification terminée : {res['pretes']} prête(s), "
            f"{res['attente']} en attente, {res['aucune']} sans transcription"
        )
        if res["erreurs"]:
            msg += f", {res['erreurs']} erreur(s) Graph"
        msg += "."
    return _rediriger(message=msg, onglet="rdv")


@reception_bp.post("/transcriptions/<hid>/verser")
@login_required
def transcription_verser(hid: str):
    expected = edit_conflict.submitted_etag()
    dossier_id = (request.form.get("dossier_id") or "").strip()
    if not dossier_id:
        return _rediriger(erreur="Veuillez sélectionner un dossier de destination.", onglet="rdv")
    note_title = request.form.get("note_title") or ""
    note_category = (request.form.get("note_category") or "").strip()
    copilot_markdown = request.form.get("copilot_markdown") or ""
    operator_email = str(session.get("email") or Config.AUTHORIZED_USER_EMAIL or "")

    _doc, errors, warnings = transcription_reunion.verser_transcription(
        hid,
        dossier_id=dossier_id,
        note_title=note_title,
        note_category=note_category,
        markdown_content=copilot_markdown,
        expected_etag=expected,
        user_id=operator_email,
        category_source="juriste",
    )
    _invalidater_pastille()
    if errors:
        if concurrency.is_stale(errors):
            return _rediriger(
                erreur="Cette transcription a été modifiée ailleurs — vérifiez ci-dessous avant de réessayer.",
                onglet="rdv",
            )
        return _rediriger(erreur=" ".join(errors), onglet="rdv")
    message = "Notes et transcription versées au dossier."
    if warnings:
        message += " " + " ".join(warnings)
    return _rediriger(message=message, onglet="rdv")


@reception_bp.post("/transcriptions/<hid>/ecarter")
@login_required
def transcription_ecarter(hid: str):
    expected = edit_conflict.submitted_etag()
    _doc, errors = transcription_reunion.ecarter_transcription(
        hid, expected_etag=expected
    )
    _invalidater_pastille()
    if errors:
        if concurrency.is_stale(errors):
            return _rediriger(
                erreur="Cette transcription a été modifiée ailleurs — vérifiez ci-dessous avant de réessayer.",
                onglet="rdv",
            )
        return _rediriger(erreur=" ".join(errors), onglet="rdv")
    return _rediriger(message="Transcription écartée et fichier temporaire purgé.", onglet="rdv")
