"""Pure validation helpers for portal appointment requests, bookings, and changes.

Stdlib + ``utils.validators.normalize_email`` only: no Flask, no Firestore, no
``models`` or ``config``. Shared by ``client/routes.py`` and unit tests.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Optional

from utils.validators import normalize_email

_MAX_NOM = 150
_MAX_TEL = 40
_MAX_SUJET = 300
_MAX_ADVERSE = 150
_MAX_ADVERSES = 10
_MAX_POINTS = 1000


def _clean_text(val: object, max_len: int) -> tuple[str, bool]:
    """Return ``(cleaned, ok)`` rejecting chevrons, control chars, or overflow."""
    if not isinstance(val, str):
        return "", False
    norm = unicodedata.normalize("NFC", val).replace("\r\n", "\n").replace("\r", "\n")
    norm = norm.strip()
    if len(norm) > max_len:
        return "", False
    if "<" in norm or ">" in norm:
        return "", False
    for ch in norm:
        if unicodedata.category(ch)[0] == "C" and ch not in ("\n", "\t"):
            return "", False
    return norm, True


def _parse_utc(raw: object) -> Optional[datetime]:
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=timezone.utc) if raw.tzinfo is None else raw.astimezone(timezone.utc)
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _slot_listed(dispo: Optional[dict], duree_minutes: int, debut_utc: datetime, now_utc: datetime, *, verifier_delai: bool = True) -> bool:
    if not isinstance(dispo, dict):
        return False
    iso = debut_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    slots = (dispo.get("creneaux_par_duree") or {}).get(str(int(duree_minutes))) or []
    if iso not in slots:
        return False
    if debut_utc <= now_utc:
        return False
    if verifier_delai:
        delai_h = int(dispo.get("delai_minimum_heures") or 24)
        if debut_utc < now_utc + timedelta(hours=delai_h):
            return False
    return True


def valider_demande_rdv_publique(
    payload: dict,
    dispo: Optional[dict],
    now_utc: Optional[datetime] = None,
) -> tuple[Optional[dict], str]:
    """Validate an anonymous public ``POST /api/rdv/demander`` submission."""
    if not isinstance(payload, dict) or not isinstance(dispo, dict):
        return None, "Les disponibilités ne sont pas accessibles pour le moment."
    now = _parse_utc(now_utc) or datetime.now(timezone.utc)

    type_cle = str(payload.get("type_rdv_cle") or "").strip()
    public_types = {
        str(t.get("cle") or "").strip(): t
        for t in (dispo.get("types_rdv") or ())
        if isinstance(t, dict) and t.get("public")
    }
    if type_cle not in public_types:
        return None, "Veuillez choisir un type de rendez-vous offert."
    cfg = public_types[type_cle]
    duree = int(cfg.get("duree_minutes") or 30)

    slot_dt = _parse_utc(payload.get("slot_debut_utc"))
    if slot_dt is None or not _slot_listed(dispo, duree, slot_dt, now):
        return None, "Ce créneau n'est plus disponible. Veuillez en choisir un autre."

    nom_complet, ok = _clean_text(payload.get("nom_complet", ""), _MAX_NOM)
    if not ok or not nom_complet:
        return None, "Veuillez indiquer votre nom complet (sans chevrons)."

    courriel = normalize_email(str(payload.get("courriel") or ""))
    if not courriel:
        return None, "Veuillez indiquer une adresse courriel valide."

    telephone, ok = _clean_text(payload.get("telephone", ""), _MAX_TEL)
    if not ok or not telephone:
        return None, "Veuillez indiquer un numéro de téléphone valide."

    aucune_adverse = bool(payload.get("aucune_partie_adverse"))
    raw_adverses = payload.get("parties_adverses")
    adverses: list[str] = []
    if isinstance(raw_adverses, str):
        raw_list = [ligne.strip() for ligne in raw_adverses.split("\n") if ligne.strip()]
    elif isinstance(raw_adverses, list):
        raw_list = [str(item).strip() for item in raw_adverses if str(item).strip()]
    else:
        raw_list = []

    if len(raw_list) > _MAX_ADVERSES:
        return None, f"Maximum {_MAX_ADVERSES} parties adverses peuvent être indiquées."
    for item in raw_list:
        clean_adv, adv_ok = _clean_text(item, _MAX_ADVERSE)
        if not adv_ok or not clean_adv:
            return None, "Nom de partie adverse invalide ou trop long."
        adverses.append(clean_adv)

    if not aucune_adverse and not adverses:
        return None, (
            "Veuillez nommer l'autre partie concernée (pour la vérification des "
            "conflits d'intérêts) ou cocher « Aucune partie adverse »."
        )
    if aucune_adverse:
        adverses = []

    sujet, ok = _clean_text(payload.get("sujet", ""), _MAX_SUJET)
    if not ok or not sujet:
        return None, (
            "Veuillez indiquer brièvement le sujet général de votre demande "
            "(maximum 300 caractères, sans renseignements confidentiels)."
        )

    slot_fin = slot_dt + timedelta(minutes=duree)
    return {
        "type_rdv_cle": type_cle,
        "type_rdv_libelle": str(cfg.get("libelle") or "Consultation"),
        "duree_minutes": duree,
        "slot_debut_utc": slot_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "slot_fin_utc": slot_fin.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nom_complet": nom_complet,
        "courriel": courriel,
        "telephone": telephone,
        "parties_adverses": adverses,
        "sujet": sujet,
    }, ""


def valider_reservation_rdv(
    payload: dict,
    invitation: dict,
    dispo: Optional[dict],
    now_utc: Optional[datetime] = None,
) -> tuple[Optional[dict], str]:
    """Validate slot pick for an authenticated RDV invitation (invite or public re-pick)."""
    if not isinstance(payload, dict) or not isinstance(dispo, dict):
        return None, "Les disponibilités ne sont pas accessibles pour le moment."
    now = _parse_utc(now_utc) or datetime.now(timezone.utc)
    duree = int(invitation.get("duree_minutes") or 30)
    slot_dt = _parse_utc(payload.get("slot_debut_utc"))
    if slot_dt is None or not _slot_listed(dispo, duree, slot_dt, now):
        return None, "Ce créneau n'est plus disponible. Veuillez en choisir un autre."

    points = ""
    if "points_discussion" in payload and payload.get("points_discussion") is not None:
        cleaned_pts, ok = _clean_text(payload.get("points_discussion"), _MAX_POINTS)
        if not ok:
            return None, (
                f"Les points à aborder ne doivent pas dépasser {_MAX_POINTS} "
                "caractères ni contenir de chevrons (< ou >)."
            )
        points = cleaned_pts

    slot_fin = slot_dt + timedelta(minutes=duree)
    return {
        "slot_debut_utc": slot_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "slot_fin_utc": slot_fin.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duree_minutes": duree,
        "points_discussion": points,
    }, ""


def est_modifiable_avant_preavis(
    invitation: dict,
    now_utc: Optional[datetime] = None,
) -> bool:
    """True when ``now + preavis_modification_heures <= slot_debut_utc``."""
    slot_dt = _parse_utc(invitation.get("slot_debut_utc"))
    if slot_dt is None:
        return False
    now = _parse_utc(now_utc) or datetime.now(timezone.utc)
    preavis_h = int(invitation.get("preavis_modification_heures") or 24)
    return (now + timedelta(hours=max(0, preavis_h))) <= slot_dt


__all__ = [
    "est_modifiable_avant_preavis",
    "valider_demande_rdv_publique",
    "valider_reservation_rdv",
]
