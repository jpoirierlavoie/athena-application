"""Anonymized availability projection stored in the named ``portail`` DB.

Single-writer principle (mirrors ``models/portail_invitation.py``): the main
service writes ``disponibilites/courant``; the portal service reads it
read-only. Zero client names, zero event subjects, zero internal notes, and
zero meeting agenda prose ever enter this document.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from models import portail_invitation

COLLECTION = "disponibilites"
DOC_ID = "courant"


def _doc_ref():
    return portail_invitation._pdb().collection(COLLECTION).document(DOC_ID)


def ecrire_disponibilites(payload: dict) -> dict:
    """Write the anonymized portal availability projection strictly (raises on failure)."""
    raw_types = payload.get("types_rdv") or []
    sanitized_types = [
        {
            "cle": str(item.get("cle") or "").strip(),
            "libelle": str(item.get("libelle") or "").strip(),
            "duree_minutes": int(item.get("duree_minutes") or 30),
            "public": bool(item.get("public")),
        }
        for item in raw_types
        if isinstance(item, dict) and str(item.get("cle") or "").strip()
    ]
    raw_slots = payload.get("creneaux_par_duree") or {}
    clean_slots = {
        str(k): [str(ts) for ts in v]
        for k, v in raw_slots.items()
        if isinstance(v, list)
    }
    doc = {
        "updated_at": payload.get("updated_at") or datetime.now(timezone.utc),
        "horizon_jours": int(payload.get("horizon_jours") or 60),
        "delai_minimum_heures": int(payload.get("delai_minimum_heures") or 24),
        "preavis_modification_heures": int(
            payload.get("preavis_modification_heures") or 24
        ),
        "types_rdv": sanitized_types,
        "creneaux_par_duree": clean_slots,
    }
    _doc_ref().set(doc)
    return doc


def lire_disponibilites_strict() -> Optional[dict]:
    """Read ``disponibilites/courant`` strictly (raises on Firestore errors)."""
    snap = _doc_ref().get()
    if not getattr(snap, "exists", False):
        return None
    data = snap.to_dict()
    return data if isinstance(data, dict) else None


__all__ = [
    "COLLECTION",
    "DOC_ID",
    "ecrire_disponibilites",
    "lire_disponibilites_strict",
]
