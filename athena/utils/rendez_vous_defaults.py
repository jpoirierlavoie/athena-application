"""Pure vocabulary, defaults, and linear parsers for Rendez-vous & Teams settings.

Stdlib only (plus pure stdlib helpers from ``utils.integrations_defaults``):
no ``models``, no ``config``, no ``flask``, no backtracking regexes (CWE-1333).
Shared by ``models/rendez_vous_reglages.py``, ``routes/settings.py``, and
``routes/hearings.py``.
"""

from __future__ import annotations

import copy
from typing import Optional

from utils.integrations_defaults import parse_bool

WEEKDAYS: tuple[str, ...] = (
    "lundi",
    "mardi",
    "mercredi",
    "jeudi",
    "vendredi",
)

WEEKDAY_LABELS: dict[str, str] = {
    "lundi": "Lundi",
    "mardi": "Mardi",
    "mercredi": "Mercredi",
    "jeudi": "Jeudi",
    "vendredi": "Vendredi",
}

MAX_MEETING_TYPES = 5
DURATION_MIN = 15
DURATION_MAX = 240
TYPE_KEY_MAX = 40
TYPE_LABEL_MAX = 120
AGENDA_MAX = 2000

# Bounds for numeric scheduling controls.
NUMERIC_BOUNDS: dict[str, tuple[int, int]] = {
    "battement_minutes": (0, 120),
    "delai_minimum_heures": (1, 720),
    "horizon_jours": (1, 180),
    "preavis_modification_heures": (0, 168),
}

NUMERIC_DEFAULTS: dict[str, int] = {
    "battement_minutes": 15,
    "delai_minimum_heures": 24,
    "horizon_jours": 60,
    "preavis_modification_heures": 24,
}

NUMERIC_FIELDS: tuple[str, ...] = tuple(NUMERIC_BOUNDS)

EDITABLE_FIELDS: tuple[str, ...] = (
    *NUMERIC_FIELDS,
    "plages",
    "types_rdv",
)

DEFAULT_WINDOWS: dict[str, dict[str, object]] = {
    day: {
        "actif": True,
        "matin_debut": "09:30",
        "matin_fin": "12:00",
        "apres_midi_debut": "13:30",
        "apres_midi_fin": "16:30",
    }
    for day in WEEKDAYS
}

DEFAULT_MEETING_TYPES: list[dict[str, object]] = [
    {
        "cle": "consultation",
        "libelle": "Consultation initiale",
        "hearing_type": "consultation",
        "duree_minutes": 30,
        "public": True,
        "ordre_du_jour": (
            "1. Vérification préliminaire et cadre de la consultation (5 min)\n"
            "2. Exposé de la situation et objectifs (15 min)\n"
            "3. Pistes juridiques et prochaines étapes (10 min)"
        ),
    },
    {
        "cle": "rencontre",
        "libelle": "Rencontre de suivi",
        "hearing_type": "rencontre",
        "duree_minutes": 60,
        "public": False,
        "ordre_du_jour": (
            "1. État d'avancement du dossier (10 min)\n"
            "2. Points à discuter et stratégie (35 min)\n"
            "3. Prochaines étapes et échéances (15 min)"
        ),
    },
]

FALLBACK_TIMED_AGENDA = (
    "1. Ouverture et objectifs (5 min)\n"
    "2. Points à discuter (45 min)\n"
    "3. Prochaines étapes (10 min)"
)

_ASCII_LOWER = frozenset("abcdefghijklmnopqrstuvwxyz")
_ASCII_DIGITS = frozenset("0123456789")
_KEY_START = _ASCII_LOWER | _ASCII_DIGITS
_KEY_CHARS = _KEY_START | frozenset("_-")


def default_reglages() -> dict:
    """Return a fresh deep-copied default Rendez-vous settings dict."""
    return {
        **NUMERIC_DEFAULTS,
        "plages": copy.deepcopy(DEFAULT_WINDOWS),
        "types_rdv": copy.deepcopy(DEFAULT_MEETING_TYPES),
    }


def parse_hhmm(val: object) -> Optional[tuple[int, int]]:
    """Parse strict ASCII ``HH:MM`` (00:00–23:59) into ``(hour, minute)``, or None."""
    if not isinstance(val, str):
        return None
    s = val.strip()
    if len(s) != 5 or s[2] != ":":
        return None
    hh, mm = s[:2], s[3:]
    if not (
        all(ch in _ASCII_DIGITS for ch in hh)
        and all(ch in _ASCII_DIGITS for ch in mm)
    ):
        return None
    hour = int(hh)
    minute = int(mm)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return (hour, minute)


def parse_strict_int(raw: object) -> Optional[int]:
    """Parse an integer without coercing bools or floats; returns None when invalid."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    digits = s[1:] if s[0] in ("+", "-") else s
    if not digits or not all(ch in _ASCII_DIGITS for ch in digits):
        return None
    try:
        return int(s)
    except ValueError:
        return None


def is_valid_type_key(cle: object) -> bool:
    """True when *cle* is 1..40 lowercase ASCII [a-z0-9_-] starting with [a-z0-9]."""
    if not isinstance(cle, str) or not (1 <= len(cle) <= TYPE_KEY_MAX):
        return False
    if cle[0] not in _KEY_START:
        return False
    return all(ch in _KEY_CHARS for ch in cle)


def default_agenda_for_hearing_type(reglages: dict, hearing_type: str) -> str:
    """Return the configured timed agenda matching *hearing_type*, or the fallback."""
    if isinstance(reglages, dict):
        for entry in reglages.get("types_rdv") or ():
            if not isinstance(entry, dict):
                continue
            if entry.get("hearing_type") == hearing_type:
                agenda = str(entry.get("ordre_du_jour") or "").strip()
                if agenda:
                    return agenda
    return FALLBACK_TIMED_AGENDA


__all__ = [
    "AGENDA_MAX",
    "DEFAULT_MEETING_TYPES",
    "DEFAULT_WINDOWS",
    "DURATION_MAX",
    "DURATION_MIN",
    "EDITABLE_FIELDS",
    "FALLBACK_TIMED_AGENDA",
    "MAX_MEETING_TYPES",
    "NUMERIC_BOUNDS",
    "NUMERIC_DEFAULTS",
    "NUMERIC_FIELDS",
    "TYPE_KEY_MAX",
    "TYPE_LABEL_MAX",
    "WEEKDAYS",
    "WEEKDAY_LABELS",
    "default_agenda_for_hearing_type",
    "default_reglages",
    "is_valid_type_key",
    "parse_bool",
    "parse_hhmm",
    "parse_strict_int",
]
