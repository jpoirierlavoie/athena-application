"""Portal availability synchronization and strict slot verification service.

Orchestrates Outlook ``getSchedule`` free/busy, active/held Athena hearings,
and ``settings/rendez_vous`` rules into the pure math of
``utils.disponibilites_math`` and writes the anonymized projection
``disponibilites/courant`` in the named ``portail`` Firestore database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from config import Config
from models import hearing as hearing_model
from models import portail_disponibilites, portail_invitation
from models.rendez_vous_reglages import get_rendez_vous_reglages_strict
from utils import disponibilites_math, graph_teams
from utils.graph import GraphError, GraphNotConfigured
from utils.logging_setup import log_bookings_event

_DEFAULT_DURATIONS = {30, 45, 60, 90}


def _collect_busy_intervals(
    *,
    hearings: list[dict],
    outlook_intervals: list[tuple[datetime, datetime]],
    exclure_hearing_id: str = "",
    exclure_graph_slot: Optional[tuple[datetime, datetime]] = None,
) -> list[tuple[datetime, datetime]]:
    intervals: list[tuple[datetime, datetime]] = []
    for h in hearings:
        if exclure_hearing_id and h.get("id") == exclure_hearing_id:
            continue
        iv = disponibilites_math.hearing_busy_interval(h)
        if iv is not None:
            intervals.append(iv)

    if exclure_graph_slot is not None:
        es, ee = exclure_graph_slot
        removed_one = False
        for s, e in outlook_intervals:
            if (
                not removed_one
                and abs((s - es).total_seconds()) <= 60
                and abs((e - ee).total_seconds()) <= 60
            ):
                removed_one = True
                continue
            intervals.append((s, e))
    else:
        intervals.extend(outlook_intervals)
    return intervals


def synchroniser_disponibilites() -> dict:
    """Compute and persist the anonymized ``disponibilites/courant`` snapshot."""
    now_utc = datetime.now(timezone.utc)
    reglages = get_rendez_vous_reglages_strict()
    horizon_jours = int(reglages.get("horizon_jours") or 60)
    floor_utc = now_utc - timedelta(days=14)
    ceiling_utc = now_utc + timedelta(days=horizon_jours + 2)

    hearings = hearing_model.list_hearings_range_strict(floor_utc, ceiling_utc)
    outlook_intervals: list[tuple[datetime, datetime]] = []
    if Config.teams_configured():
        outlook_intervals = graph_teams.lire_plages_occupees(now_utc, ceiling_utc)
    elif Config.ENV == "production":
        raise GraphNotConfigured(
            "Calendrier Microsoft Graph non configuré pour les disponibilités."
        )

    busy = _collect_busy_intervals(
        hearings=hearings,
        outlook_intervals=outlook_intervals,
    )
    durees = set(_DEFAULT_DURATIONS)
    for t in reglages.get("types_rdv") or ():
        if isinstance(t, dict) and isinstance(t.get("duree_minutes"), int):
            durees.add(int(t["duree_minutes"]))
    durees |= portail_invitation.lister_durees_rdv_actives()

    creneaux = disponibilites_math.calculer_creneaux_ouverts(
        reglages=reglages,
        now_utc=now_utc,
        plages_occupees_utc=busy,
        durees_minutes=durees,
    )
    portail_disponibilites.ecrire_disponibilites(
        {
            "updated_at": now_utc,
            "horizon_jours": horizon_jours,
            "delai_minimum_heures": int(reglages.get("delai_minimum_heures") or 24),
            "preavis_modification_heures": int(
                reglages.get("preavis_modification_heures") or 24
            ),
            "types_rdv": reglages.get("types_rdv") or [],
            "creneaux_par_duree": creneaux,
        }
    )
    total = sum(len(v) for v in creneaux.values())
    log_bookings_event(
        "disponibilites_synchronisees",
        horizon_jours=horizon_jours,
        plages_occupees=len(busy),
        creneaux_total=total,
    )
    return {
        "horizon_jours": horizon_jours,
        "plages_occupees": len(busy),
        "creneaux_total": total,
    }


def rafraichir_disponibilites_best_effort() -> None:
    """Best-effort post-commit refresh; logs failure and never raises."""
    if not (Config.PORTAIL_RDV_ACTIF and Config.teams_configured()):
        return
    try:
        synchroniser_disponibilites()
    except Exception as exc:
        log_bookings_event(
            "disponibilites_sync_echec",
            "failure",
            reason=type(exc).__name__,
        )


def verifier_creneau_libre_strict(
    debut_utc: datetime,
    duree_minutes: int,
    *,
    exclure_hearing_id: str = "",
    exclure_graph_slot: Optional[tuple[datetime, datetime]] = None,
    verifier_delai_minimum: bool = True,
) -> tuple[bool, str]:
    """Fail-closed live check against settings, Firestore hearings & Graph free/busy."""
    try:
        now_utc = datetime.now(timezone.utc)
        reglages = get_rendez_vous_reglages_strict()
        fin_utc = debut_utc + timedelta(minutes=int(duree_minutes))
        hearings = hearing_model.list_hearings_range_strict(
            debut_utc - timedelta(days=14),
            fin_utc + timedelta(days=2),
        )
        outlook_intervals: list[tuple[datetime, datetime]] = []
        if Config.teams_configured():
            outlook_intervals = graph_teams.lire_plages_occupees(
                debut_utc - timedelta(hours=4),
                fin_utc + timedelta(hours=4),
            )
        elif Config.ENV == "production":
            raise GraphNotConfigured(
                "Calendrier Microsoft Graph non configuré pour la vérification."
            )
    except (GraphError, GraphNotConfigured, Exception) as exc:
        log_bookings_event(
            "disponibilites_sync_echec",
            "failure",
            reason=f"live_{type(exc).__name__}",
        )
        return False, "erreur_lecture"

    busy = _collect_busy_intervals(
        hearings=hearings,
        outlook_intervals=outlook_intervals,
        exclure_hearing_id=exclure_hearing_id,
        exclure_graph_slot=exclure_graph_slot,
    )
    libre = disponibilites_math.est_creneau_disponible(
        debut_utc=debut_utc,
        duree_minutes=int(duree_minutes),
        reglages=reglages,
        now_utc=now_utc,
        plages_occupees_utc=busy,
        verifier_delai_minimum=verifier_delai_minimum,
    )
    return (True, "") if libre else (False, "creneau_indisponible")


__all__ = [
    "rafraichir_disponibilites_best_effort",
    "synchroniser_disponibilites",
    "verifier_creneau_libre_strict",
]
