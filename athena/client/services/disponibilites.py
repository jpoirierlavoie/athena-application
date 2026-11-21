"""Read-only portal availability reader & Montréal slot formatter (Lot 2).

Reads ``disponibilites/courant`` from the named ``portail`` database via
``client.services.invitations._pdb()``. Zero ``models``, ``config``, or
``security`` imports. Fail-closed: returns ``None`` when unavailable or absent.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from client.services import invitations
from utils.format_fr import format_date_fr

logger = logging.getLogger(__name__)

_MTL = ZoneInfo("America/Montreal")
_WEEKDAY_FR = (
    "Lundi",
    "Mardi",
    "Mercredi",
    "Jeudi",
    "Vendredi",
    "Samedi",
    "Dimanche",
)


def _parse_iso_utc(raw: object) -> Optional[datetime]:
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


def lire_disponibilites() -> Optional[dict]:
    """Return ``disponibilites/courant`` or ``None`` if missing or unreadable."""
    try:
        snap = invitations._pdb().collection("disponibilites").document("courant").get()
    except Exception:
        logger.exception("portail disponibilites read failed")
        return None
    if not getattr(snap, "exists", False):
        return None
    data = snap.to_dict()
    return data if isinstance(data, dict) else None


def types_publics(dispo: Optional[dict]) -> list[dict]:
    """Return public meeting types (``cle``, ``libelle``, ``duree_minutes``)."""
    if not isinstance(dispo, dict):
        return []
    out: list[dict] = []
    for item in dispo.get("types_rdv") or ():
        if not isinstance(item, dict) or not item.get("public"):
            continue
        cle = str(item.get("cle") or "").strip()
        libelle = str(item.get("libelle") or "").strip()
        duree = item.get("duree_minutes")
        if cle and libelle and isinstance(duree, int) and not isinstance(duree, bool):
            out.append({"cle": cle, "libelle": libelle, "duree_minutes": duree})
    return out


def formater_heure_mtl(dt_utc: datetime) -> str:
    local = dt_utc.astimezone(_MTL)
    return local.strftime("%H h %M")


def formater_jour_heure_mtl(dt_utc: datetime) -> str:
    local = dt_utc.astimezone(_MTL)
    jour = format_date_fr(local.date())
    return f"{_WEEKDAY_FR[local.weekday()]} {jour} à {local.strftime('%H h %M')}"


def formater_creneaux_par_jour(
    dispo: Optional[dict],
    duree_minutes: int,
    *,
    now_utc: Optional[datetime] = None,
    verifier_delai_minimum: bool = True,
) -> list[dict]:
    """Return sorted ``[{jour_iso, jour_label, creneaux: [{utc, heure_label}]}]``."""
    if not isinstance(dispo, dict):
        return []
    raw_slots = (dispo.get("creneaux_par_duree") or {}).get(str(int(duree_minutes))) or []
    now = now_utc or datetime.now(timezone.utc)
    delai_h = int(dispo.get("delai_minimum_heures") or 24) if verifier_delai_minimum else 0
    cutoff = now.timestamp() + (delai_h * 3600)

    by_day: dict[str, dict] = {}
    for raw in raw_slots:
        dt = _parse_iso_utc(raw)
        if dt is None or dt.timestamp() < cutoff or dt <= now:
            continue
        local = dt.astimezone(_MTL)
        jour_iso = local.date().isoformat()
        if jour_iso not in by_day:
            by_day[jour_iso] = {
                "jour_iso": jour_iso,
                "jour_label": f"{_WEEKDAY_FR[local.weekday()]} {format_date_fr(local.date())}",
                "creneaux": [],
            }
        by_day[jour_iso]["creneaux"].append({
            "utc": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "heure_label": local.strftime("%H h %M"),
        })
    return [by_day[k] for k in sorted(by_day)]


__all__ = [
    "formater_creneaux_par_jour",
    "formater_heure_mtl",
    "formater_jour_heure_mtl",
    "lire_disponibilites",
    "types_publics",
]
