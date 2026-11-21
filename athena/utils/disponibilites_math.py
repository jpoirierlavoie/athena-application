"""Pure domain math for portal self-service appointment slot availability.

Stdlib + ``tz`` + ``utils.deadlines`` + ``utils.rendez_vous_defaults`` only:
zero ``models``, zero ``config``, zero ``flask`` imports.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Optional

from tz import mtl_to_utc, to_mtl
from utils import deadlines
from utils.rendez_vous_defaults import (
    WEEKDAYS,
    default_reglages,
    parse_hhmm,
    parse_strict_int,
)

SLOT_STEP_MINUTES = 30
_FALLBACK_HEARING_DURATION = timedelta(hours=1)
_RETIRED_CONFIRMATIONS = frozenset({"refusée", "annulée_client"})


def _as_utc(dt: object) -> Optional[datetime]:
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def hearing_busy_interval(hearing: dict) -> Optional[tuple[datetime, datetime]]:
    """Return ``(start_utc, end_utc)`` for an active/held hearing, or ``None``.

    Both confirmed hearings (``confirmation == ""``) and pending held slots
    (``confirmation == "à_confirmer"``) block booking availability. Hearings
    explicitly cancelled (``status == "annulée"``) or client/lawyer-retired
    (``confirmation in ("refusée", "annulée_client")``) never block slots.
    """
    if not isinstance(hearing, dict):
        return None
    if hearing.get("status") == "annulée":
        return None
    if (hearing.get("confirmation") or "") in _RETIRED_CONFIRMATIONS:
        return None
    start = _as_utc(hearing.get("start_datetime"))
    if start is None:
        return None
    end = _as_utc(hearing.get("end_datetime"))
    if hearing.get("all_day"):
        start_day = start.date()
        if end is not None and end.date() > start_day:
            end_day = end.date()
        else:
            end_day = start_day + timedelta(days=1)
        start_local = mtl_to_utc(datetime.combine(start_day, time(0, 0)))
        end_local = mtl_to_utc(datetime.combine(end_day, time(0, 0)))
        if start_local is None or end_local is None or end_local <= start_local:
            return None
        return (start_local, end_local)
    if end is None or end <= start:
        end = start + _FALLBACK_HEARING_DURATION
    return (start, end)


def _merge_buffered_busy(
    intervals: Iterable[tuple[datetime, datetime]],
    battement_minutes: int,
) -> list[tuple[datetime, datetime]]:
    pad = timedelta(minutes=max(0, int(battement_minutes)))
    normalized: list[tuple[datetime, datetime]] = []
    for raw_start, raw_end in intervals:
        s = _as_utc(raw_start)
        e = _as_utc(raw_end)
        if s is None or e is None or e <= s:
            continue
        normalized.append((s - pad, e + pad))
    if not normalized:
        return []
    normalized.sort(key=lambda pair: (pair[0], pair[1]))
    merged: list[tuple[datetime, datetime]] = [normalized[0]]
    for s, e in normalized[1:]:
        prev_s, prev_e = merged[-1]
        if s <= prev_e:
            if e > prev_e:
                merged[-1] = (prev_s, e)
        else:
            merged.append((s, e))
    return merged


def _overlaps_busy(
    start_utc: datetime,
    end_utc: datetime,
    merged_busy: list[tuple[datetime, datetime]],
) -> bool:
    for bs, be in merged_busy:
        if be <= start_utc:
            continue
        if bs >= end_utc:
            return False
        return True
    return False


def _day_windows(reglages: dict, day: date) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    wd = day.weekday()
    if wd < 0 or wd >= len(WEEKDAYS):
        return []
    plages = (reglages or {}).get("plages") or {}
    cfg = plages.get(WEEKDAYS[wd])
    if not isinstance(cfg, dict) or not cfg.get("actif"):
        return []
    windows: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for prefix in ("matin", "apres_midi"):
        start_t = parse_hhmm(cfg.get(f"{prefix}_debut"))
        end_t = parse_hhmm(cfg.get(f"{prefix}_fin"))
        if start_t is not None and end_t is not None and start_t < end_t:
            windows.append((start_t, end_t))
    return windows


def _holidays_for_range(start_day: date, end_day: date) -> set[date]:
    holidays: set[date] = set()
    for year in range(start_day.year, end_day.year + 1):
        holidays.update(deadlines.get_quebec_holidays(year, regime=deadlines.PROCEDURAL))
    return holidays


def _to_iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _horizon_bounds(reglages: dict, now_utc: datetime) -> tuple[date, date, datetime]:
    defaults = default_reglages()
    horizon_jours = parse_strict_int(reglages.get("horizon_jours"))
    if horizon_jours is None or horizon_jours < 1:
        horizon_jours = defaults["horizon_jours"]
    delai_min_h = parse_strict_int(reglages.get("delai_minimum_heures"))
    if delai_min_h is None or delai_min_h < 0:
        delai_min_h = defaults["delai_minimum_heures"]
    start_day = to_mtl(now_utc).date()
    end_day = start_day + timedelta(days=horizon_jours)
    earliest_utc = now_utc + timedelta(hours=delai_min_h)
    return start_day, end_day, earliest_utc


def calculer_creneaux_ouverts(
    *,
    reglages: dict,
    now_utc: datetime,
    plages_occupees_utc: Iterable[tuple[datetime, datetime]],
    durees_minutes: Iterable[int],
) -> dict[str, list[str]]:
    """Compute chronological open UTC slot ISO-Z strings keyed by duration string."""
    now = _as_utc(now_utc) or datetime.now(timezone.utc)
    start_day, end_day, earliest_utc = _horizon_bounds(reglages or {}, now)
    battement = parse_strict_int((reglages or {}).get("battement_minutes"))
    if battement is None or battement < 0:
        battement = default_reglages()["battement_minutes"]
    merged_busy = _merge_buffered_busy(plages_occupees_utc, battement)
    holidays = _holidays_for_range(start_day, end_day)

    clean_durations = sorted({
        int(d) for d in durees_minutes
        if isinstance(d, int) and not isinstance(d, bool) and d >= 15
    })
    out: dict[str, list[str]] = {str(d): [] for d in clean_durations}
    if not clean_durations:
        return out

    step = timedelta(minutes=SLOT_STEP_MINUTES)
    cur_day = start_day
    while cur_day <= end_day:
        if cur_day.weekday() < 5 and cur_day not in holidays:
            windows = _day_windows(reglages, cur_day)
            for (sh, sm), (eh, em) in windows:
                win_start_local = datetime.combine(cur_day, time(sh, sm))
                win_end_local = datetime.combine(cur_day, time(eh, em))
                win_end_utc = mtl_to_utc(win_end_local)
                # Align first candidate to SLOT_STEP_MINUTES grid inside the window.
                rem = sm % SLOT_STEP_MINUTES
                cand_local = win_start_local + timedelta(
                    minutes=(SLOT_STEP_MINUTES - rem) if rem else 0
                )
                while cand_local < win_end_local:
                    cand_start_utc = mtl_to_utc(cand_local)
                    if cand_start_utc >= earliest_utc:
                        for duree in clean_durations:
                            dur_td = timedelta(minutes=duree)
                            cand_end_local = cand_local + dur_td
                            cand_end_utc = mtl_to_utc(cand_end_local)
                            if cand_end_utc <= win_end_utc and not _overlaps_busy(
                                cand_start_utc, cand_end_utc, merged_busy
                            ):
                                iso = _to_iso_z(cand_start_utc)
                                bucket = out[str(duree)]
                                if not bucket or bucket[-1] != iso:
                                    bucket.append(iso)
                    cand_local += step
        cur_day += timedelta(days=1)

    return out


def est_creneau_disponible(
    *,
    debut_utc: datetime,
    duree_minutes: int,
    reglages: dict,
    now_utc: datetime,
    plages_occupees_utc: Iterable[tuple[datetime, datetime]],
    verifier_delai_minimum: bool = True,
) -> bool:
    """True when one candidate UTC slot satisfies windows, holidays, lead time & free/busy."""
    start_utc = _as_utc(debut_utc)
    now = _as_utc(now_utc)
    if start_utc is None or now is None:
        return False
    if not isinstance(duree_minutes, int) or isinstance(duree_minutes, bool) or duree_minutes < 15:
        return False

    start_local_aware = to_mtl(start_utc)
    if start_local_aware.second != 0 or start_local_aware.microsecond != 0:
        return False
    if start_local_aware.minute % SLOT_STEP_MINUTES != 0:
        return False

    local_day = start_local_aware.date()
    start_day, end_day, earliest_utc = _horizon_bounds(reglages or {}, now)
    if local_day < start_day or local_day > end_day:
        return False
    if verifier_delai_minimum and start_utc < earliest_utc:
        return False
    if start_utc <= now:
        return False
    if local_day.weekday() >= 5 or local_day in _holidays_for_range(local_day, local_day):
        return False

    end_utc = start_utc + timedelta(minutes=duree_minutes)
    end_local_aware = to_mtl(end_utc)
    if end_local_aware.date() != local_day:
        return False

    start_naive = start_local_aware.replace(tzinfo=None)
    end_naive = end_local_aware.replace(tzinfo=None)
    in_window = False
    for (sh, sm), (eh, em) in _day_windows(reglages, local_day):
        win_start = datetime.combine(local_day, time(sh, sm))
        win_end = datetime.combine(local_day, time(eh, em))
        if start_naive >= win_start and end_naive <= win_end:
            in_window = True
            break
    if not in_window:
        return False

    battement = parse_strict_int((reglages or {}).get("battement_minutes"))
    if battement is None or battement < 0:
        battement = default_reglages()["battement_minutes"]
    merged_busy = _merge_buffered_busy(plages_occupees_utc, battement)
    return not _overlaps_busy(start_utc, end_utc, merged_busy)


__all__ = [
    "SLOT_STEP_MINUTES",
    "calculer_creneaux_ouverts",
    "est_creneau_disponible",
    "hearing_busy_interval",
]
