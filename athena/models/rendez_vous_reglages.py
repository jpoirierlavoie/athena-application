"""Rendez-vous & Teams scheduling settings — the ``settings/rendez_vous`` singleton.

Follows the three singleton rules of ``models/integrations.py``:
1. Stored truth takes precedence over code defaults once created (no partial
   fallback resurrecting cleared meeting types).
2. ``_normalize`` is strictly presence-gated per top-level key.
3. Writes refuse fail-closed when the store is ``unreadable``.

Lives in the existing ``settings`` collection under keyed document
``rendez_vous`` — no new collection, no Firestore rules change, no composite
index required.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Optional

from models import db, provenance
from security import sanitize
from utils.logging_setup import log_unexpected
from utils.rendez_vous_defaults import (
    AGENDA_MAX,
    DURATION_MAX,
    DURATION_MIN,
    EDITABLE_FIELDS,
    MAX_MEETING_TYPES,
    NUMERIC_BOUNDS,
    NUMERIC_DEFAULTS,
    NUMERIC_FIELDS,
    TYPE_LABEL_MAX,
    WEEKDAYS,
    WEEKDAY_LABELS,
    default_reglages,
    is_valid_type_key,
    parse_bool,
    parse_hhmm,
    parse_strict_int,
)

COLLECTION = "settings"
DOC_ID = "rendez_vous"

_FIELDS: tuple[str, ...] = EDITABLE_FIELDS

_NUMERIC_LABELS: dict[str, str] = {
    "battement_minutes": "Battement entre rendez-vous (minutes)",
    "delai_minimum_heures": "Délai minimal avant un rendez-vous (heures)",
    "horizon_jours": "Horizon de prise de rendez-vous (jours)",
    "preavis_modification_heures": "Préavis minimal d'annulation ou de report (heures)",
}


def _read_state() -> tuple[Optional[dict], str]:
    """Return ``(payload, state)`` with ``state`` in ``ok | absent | unreadable``."""
    try:
        snap = db.collection(COLLECTION).document(DOC_ID).get()
    except Exception:
        log_unexpected(
            "rendez-vous settings read failed", reason="store_unreadable"
        )
        return None, "unreadable"
    if not getattr(snap, "exists", False):
        return None, "absent"
    data = snap.to_dict()
    if not isinstance(data, dict):
        return None, "unreadable"
    return data, "ok"


def _project_window(raw: object, fallback: dict) -> dict:
    if not isinstance(raw, dict):
        return dict(fallback)
    return {
        "actif": parse_bool(raw.get("actif")),
        "matin_debut": str(raw.get("matin_debut") or "").strip(),
        "matin_fin": str(raw.get("matin_fin") or "").strip(),
        "apres_midi_debut": str(raw.get("apres_midi_debut") or "").strip(),
        "apres_midi_fin": str(raw.get("apres_midi_fin") or "").strip(),
    }


def _project_type(raw: dict) -> dict:
    duree = parse_strict_int(raw.get("duree_minutes"))
    return {
        "cle": str(raw.get("cle") or "").strip().lower(),
        "libelle": str(raw.get("libelle") or "").strip(),
        "hearing_type": str(raw.get("hearing_type") or "").strip(),
        "duree_minutes": duree if duree is not None else 30,
        "public": parse_bool(raw.get("public")),
        "ordre_du_jour": str(raw.get("ordre_du_jour") or "").strip(),
    }


def _project(stored: dict) -> dict:
    """Project a stored payload onto a clean deep-copied structure."""
    defaults = default_reglages()
    out: dict = {k: defaults[k] for k in NUMERIC_FIELDS}
    out["plages"] = copy.deepcopy(defaults["plages"])
    out["types_rdv"] = copy.deepcopy(defaults["types_rdv"])

    for key in NUMERIC_FIELDS:
        if key in stored:
            parsed = parse_strict_int(stored[key])
            out[key] = parsed if parsed is not None else NUMERIC_DEFAULTS[key]

    if "plages" in stored and isinstance(stored["plages"], dict):
        plages_in = stored["plages"]
        for day in WEEKDAYS:
            if day in plages_in:
                out["plages"][day] = _project_window(
                    plages_in[day], defaults["plages"][day]
                )

    if "types_rdv" in stored and isinstance(stored["types_rdv"], list):
        out["types_rdv"] = [
            _project_type(item)
            for item in stored["types_rdv"]
            if isinstance(item, dict)
        ]

    return out


def get_rendez_vous_reglages_state() -> tuple[dict, str]:
    """Return ``(settings_dict, provenance)`` with provenance in ``store | absent | unreadable``."""
    stored, state = _read_state()
    if state != "ok" or stored is None:
        return default_reglages(), state
    return _project(stored), "store"


def get_rendez_vous_reglages_strict() -> dict:
    """Strict reader propagating Firestore failures for scheduling writes/sync."""
    snap = db.collection(COLLECTION).document(DOC_ID).get()
    if not getattr(snap, "exists", False):
        return default_reglages()
    data = snap.to_dict()
    if not isinstance(data, dict):
        raise RuntimeError("Rendez-vous settings document has non-dict payload.")
    return _project(data)


def get_rendez_vous_reglages() -> dict:
    """Fail-open reader returning the active settings or deep-copied defaults."""
    return get_rendez_vous_reglages_state()[0]


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _normalize(data: dict) -> dict:
    """Normalize IN PLACE, strictly presence-gated on top-level keys."""
    for key in NUMERIC_FIELDS:
        if key in data:
            data[key] = parse_strict_int(data[key])

    if "plages" in data and isinstance(data["plages"], dict):
        normalized_plages: dict[str, dict] = {}
        for day, raw_win in data["plages"].items():
            if not isinstance(raw_win, dict):
                normalized_plages[day] = raw_win
                continue
            normalized_plages[day] = {
                "actif": parse_bool(raw_win.get("actif")),
                "matin_debut": str(raw_win.get("matin_debut") or "").strip(),
                "matin_fin": str(raw_win.get("matin_fin") or "").strip(),
                "apres_midi_debut": str(raw_win.get("apres_midi_debut") or "").strip(),
                "apres_midi_fin": str(raw_win.get("apres_midi_fin") or "").strip(),
            }
        data["plages"] = normalized_plages

    if "types_rdv" in data and isinstance(data["types_rdv"], list):
        normalized_types: list = []
        for raw_item in data["types_rdv"]:
            if not isinstance(raw_item, dict):
                normalized_types.append(raw_item)
                continue
            normalized_types.append({
                "cle": str(raw_item.get("cle") or "").strip().lower(),
                "libelle": str(raw_item.get("libelle") or "").strip(),
                "hearing_type": str(raw_item.get("hearing_type") or "").strip(),
                "duree_minutes": parse_strict_int(raw_item.get("duree_minutes")),
                "public": parse_bool(raw_item.get("public")),
                "ordre_du_jour": _normalize_newlines(
                    str(raw_item.get("ordre_du_jour") or "")
                ),
            })
        data["types_rdv"] = normalized_types

    return data


def _sanitize_altered(val: str, max_length: int) -> bool:
    """True if *val* exceeds *max_length*, contains chevrons, or sanitize alters it."""
    if len(val) > max_length or "<" in val or ">" in val:
        return True
    return sanitize(val, max_length=max_length) != val


def _validate(data: dict) -> list[str]:
    """French validation errors, empty when acceptable."""
    from models.hearing import VALID_HEARING_TYPES_EXTRAJUDICIAIRE as _TYPES

    errors: list[str] = []

    for key in NUMERIC_FIELDS:
        label = _NUMERIC_LABELS[key]
        low, high = NUMERIC_BOUNDS[key]
        val = data.get(key)
        if not isinstance(val, int) or isinstance(val, bool) or not (low <= val <= high):
            errors.append(
                f"{label} : doit être un entier entre {low} et {high}."
            )

    plages = data.get("plages")
    if not isinstance(plages, dict) or set(plages) != set(WEEKDAYS):
        errors.append("Les plages horaires doivent couvrir du lundi au vendredi.")
    else:
        for day in WEEKDAYS:
            day_label = WEEKDAY_LABELS[day]
            win = plages.get(day)
            if not isinstance(win, dict):
                errors.append(f"{day_label} : plage horaire invalide.")
                continue
            actif = bool(win.get("actif"))
            m_start_raw = win.get("matin_debut", "")
            m_end_raw = win.get("matin_fin", "")
            a_start_raw = win.get("apres_midi_debut", "")
            a_end_raw = win.get("apres_midi_fin", "")

            m_start = parse_hhmm(m_start_raw) if m_start_raw else None
            m_end = parse_hhmm(m_end_raw) if m_end_raw else None
            a_start = parse_hhmm(a_start_raw) if a_start_raw else None
            a_end = parse_hhmm(a_end_raw) if a_end_raw else None

            if m_start_raw and m_start is None:
                errors.append(f"{day_label} : heure de début du matin invalide (HH:MM attendu).")
            if m_end_raw and m_end is None:
                errors.append(f"{day_label} : heure de fin du matin invalide (HH:MM attendu).")
            if a_start_raw and a_start is None:
                errors.append(f"{day_label} : heure de début d'après-midi invalide (HH:MM attendu).")
            if a_end_raw and a_end is None:
                errors.append(f"{day_label} : heure de fin d'après-midi invalide (HH:MM attendu).")

            if bool(m_start_raw) != bool(m_end_raw):
                errors.append(f"{day_label} : indiquez à la fois le début et la fin du matin.")
            elif m_start is not None and m_end is not None and m_start >= m_end:
                errors.append(f"{day_label} : l'heure de fin du matin doit suivre celle de début.")

            if bool(a_start_raw) != bool(a_end_raw):
                errors.append(f"{day_label} : indiquez à la fois le début et la fin de l'après-midi.")
            elif a_start is not None and a_end is not None and a_start >= a_end:
                errors.append(f"{day_label} : l'heure de fin d'après-midi doit suivre celle de début.")

            if m_end is not None and a_start is not None and m_end > a_start:
                errors.append(f"{day_label} : la plage d'après-midi ne peut pas chevaucher celle du matin.")

            has_morning = m_start is not None and m_end is not None and m_start < m_end
            has_afternoon = a_start is not None and a_end is not None and a_start < a_end
            if actif and not (has_morning or has_afternoon):
                errors.append(
                    f"{day_label} est activé : définissez au moins une plage horaire (matin ou après-midi)."
                )

    types_rdv = data.get("types_rdv")
    if not isinstance(types_rdv, list) or not types_rdv:
        errors.append("Au moins un type de rendez-vous est requis.")
    elif len(types_rdv) > MAX_MEETING_TYPES:
        errors.append(f"Maximum {MAX_MEETING_TYPES} types de rendez-vous.")
    else:
        seen_keys: set[str] = set()
        for idx, item in enumerate(types_rdv, start=1):
            if not isinstance(item, dict):
                errors.append(f"Type de rendez-vous #{idx} invalide.")
                continue
            cle = item.get("cle", "")
            libelle = item.get("libelle", "")
            htype = item.get("hearing_type", "")
            duree = item.get("duree_minutes")
            agenda = item.get("ordre_du_jour", "")

            if not is_valid_type_key(cle):
                errors.append(
                    f"Type #{idx} : la clé « {cle} » est invalide "
                    "(lettres minuscules a-z, chiffres, « _ » ou « - », 40 caractères max)."
                )
            elif cle in seen_keys:
                errors.append(f"Type #{idx} : la clé « {cle} » est en double.")
            else:
                seen_keys.add(cle)

            if not libelle:
                errors.append(f"Type #{idx} : le libellé est requis.")
            elif _sanitize_altered(libelle, TYPE_LABEL_MAX):
                errors.append(
                    f"Type #{idx} : le libellé contient des chevrons (< ou >) "
                    f"ou dépasse {TYPE_LABEL_MAX} caractères."
                )

            if htype not in _TYPES:
                errors.append(
                    f"Type #{idx} : « {htype} » n'est pas un type d'événement extrajudiciaire admis."
                )

            if (
                not isinstance(duree, int)
                or isinstance(duree, bool)
                or not (DURATION_MIN <= duree <= DURATION_MAX)
            ):
                errors.append(
                    f"Type #{idx} : la durée doit être comprise entre "
                    f"{DURATION_MIN} et {DURATION_MAX} minutes."
                )

            if not agenda:
                errors.append(f"Type #{idx} : l'ordre du jour minuté par défaut est requis.")
            elif _sanitize_altered(agenda, AGENDA_MAX):
                errors.append(
                    f"Type #{idx} : l'ordre du jour minuté contient des chevrons (< ou >) "
                    f"ou dépasse {AGENDA_MAX} caractères."
                )

    return errors


def _sanitize_data(data: dict) -> dict:
    """Return a clean copy ready for persistence (after validation)."""
    out = dict(data)
    if "types_rdv" in out and isinstance(out["types_rdv"], list):
        out["types_rdv"] = [
            {
                "cle": item["cle"],
                "libelle": sanitize(item["libelle"], max_length=TYPE_LABEL_MAX),
                "hearing_type": item["hearing_type"],
                "duree_minutes": item["duree_minutes"],
                "public": bool(item["public"]),
                "ordre_du_jour": sanitize(item["ordre_du_jour"], max_length=AGENDA_MAX),
            }
            for item in out["types_rdv"]
        ]
    return out


def update_rendez_vous_reglages(data: dict) -> tuple[Optional[dict], list[str]]:
    """Persist Rendez-vous settings. Refuses fail-closed when store is unreadable."""
    existing, state = _read_state()
    if state == "unreadable":
        return None, [
            "Le magasin de réglages est illisible ; l'enregistrement est "
            "refusé pour ne pas écraser vos valeurs. Réessayez dans un "
            "moment."
        ]

    base = _project(existing) if existing is not None else default_reglages()
    incoming = _normalize(copy.deepcopy(dict(data)))
    merged = {**base, **incoming}
    merged = {k: v for k, v in merged.items() if k in _FIELDS}

    errors = _validate(merged)
    if errors:
        return None, errors

    cleaned = _sanitize_data(merged)
    now = datetime.now(timezone.utc)
    stored = copy.deepcopy(cleaned)
    stored["id"] = DOC_ID
    stored["created_at"] = (existing or {}).get("created_at") or now
    provenance.stamp_update(stored, now)

    try:
        db.collection(COLLECTION).document(DOC_ID).set(stored)
    except Exception:
        log_unexpected("rendez-vous settings write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

    return cleaned, []


def changed_field_names(before: dict, after: dict) -> list[str]:
    """Return sorted top-level field names that changed (never values)."""
    return sorted(
        key for key in _FIELDS
        if before.get(key) != after.get(key)
    )


__all__ = [
    "COLLECTION",
    "DOC_ID",
    "changed_field_names",
    "get_rendez_vous_reglages",
    "get_rendez_vous_reglages_state",
    "get_rendez_vous_reglages_strict",
    "update_rendez_vous_reglages",
]
