"""Defaults, pure parsers, and singleton storage for ``settings/rendez_vous``."""

import copy
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import models.rendez_vous_reglages as m
from tests._fake_firestore import install  # noqa: E402
from utils.rendez_vous_defaults import (  # noqa: E402
    DEFAULT_MEETING_TYPES,
    DEFAULT_WINDOWS,
    FALLBACK_TIMED_AGENDA,
    WEEKDAYS,
    default_agenda_for_hearing_type,
    default_reglages,
    is_valid_type_key,
    parse_hhmm,
    parse_strict_int,
)


@pytest.fixture()
def store(monkeypatch):
    return install(monkeypatch, m)


def _valid_payload(**over) -> dict:
    base = default_reglages()
    base.update(over)
    return base


# ── Pure defaults & linear parsers (utils/rendez_vous_defaults.py) ────────


def test_default_reglages_returns_independent_deep_copies():
    first = default_reglages()
    first["plages"]["lundi"]["actif"] = False
    first["types_rdv"][0]["duree_minutes"] = 999
    first["types_rdv"].append({"cle": "extra"})

    second = default_reglages()
    assert second["plages"] == DEFAULT_WINDOWS
    assert second["types_rdv"] == DEFAULT_MEETING_TYPES
    assert second["plages"]["lundi"]["actif"] is True
    assert second["types_rdv"][0]["duree_minutes"] == 30


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("00:00", (0, 0)),
        ("09:30", (9, 30)),
        (" 16:45 ", (16, 45)),
        ("23:59", (23, 59)),
        ("", None),
        ("9:30", None),
        ("24:00", None),
        ("12:60", None),
        ("09:30:00", None),
        ("ab:cd", None),
        ("\uff109:30", None),  # fullwidth digit refused (ASCII only)
        (None, None),
        (930, None),
    ],
)
def test_parse_hhmm(raw, expected):
    assert parse_hhmm(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, 0),
        (15, 15),
        (-3, -3),
        (" 24 ", 24),
        ("+10", 10),
        ("-7", -7),
        (True, None),
        (False, None),
        (1.5, None),
        ("1.5", None),
        ("", None),
        ("  ", None),
        ("12a", None),
        ("\uff11\uff12", None),  # fullwidth digits refused
        (None, None),
    ],
)
def test_parse_strict_int(raw, expected):
    assert parse_strict_int(raw) == expected


@pytest.mark.parametrize(
    ("cle", "expected"),
    [
        ("a", True),
        ("1", True),
        ("consultation", True),
        ("suivi_2026-01", True),
        ("x" * 40, True),
        ("", False),
        ("x" * 41, False),
        ("Consultation", False),
        ("_debut", False),
        ("-debut", False),
        ("avec espace", False),
        ("café", False),
        (None, False),
        (123, False),
    ],
)
def test_is_valid_type_key(cle, expected):
    assert is_valid_type_key(cle) is expected


def test_default_agenda_for_hearing_type_matches_and_falls_back():
    reglages = default_reglages()
    assert "Consultation" not in default_agenda_for_hearing_type(reglages, "rencontre")
    assert "Vérification préliminaire" in default_agenda_for_hearing_type(
        reglages, "consultation"
    )
    assert "État d'avancement" in default_agenda_for_hearing_type(reglages, "rencontre")
    assert (
        default_agenda_for_hearing_type(reglages, "conférence")
        == FALLBACK_TIMED_AGENDA
    )
    assert (
        default_agenda_for_hearing_type({"types_rdv": []}, "consultation")
        == FALLBACK_TIMED_AGENDA
    )
    assert default_agenda_for_hearing_type(None, "consultation") == FALLBACK_TIMED_AGENDA


# ── Singleton persistence, three-rule contract & validation ───────────────


def test_absent_store_returns_defaults_with_provenance_absent(store):
    data, provenance = m.get_rendez_vous_reglages_state()
    assert provenance == "absent"
    assert data == default_reglages()
    assert m.get_rendez_vous_reglages() == default_reglages()


def test_unreadable_store_fails_open_on_read_and_refuses_write(monkeypatch):
    bad_db = mock.MagicMock()
    bad_db.collection.return_value.document.return_value.get.side_effect = (
        RuntimeError("boom")
    )
    monkeypatch.setattr(m, "db", bad_db)

    data, provenance = m.get_rendez_vous_reglages_state()
    assert provenance == "unreadable"
    assert data == default_reglages()

    saved, errors = m.update_rendez_vous_reglages(_valid_payload())
    assert saved is None
    assert errors == [
        "Le magasin de réglages est illisible ; l'enregistrement est "
        "refusé pour ne pas écraser vos valeurs. Réessayez dans un "
        "moment."
    ]
    bad_db.collection.return_value.document.return_value.set.assert_not_called()


def test_non_dict_snapshot_is_treated_as_unreadable(monkeypatch):
    """Pins the MagicMock trap: a mock/non-dict to_dict() never passes as empty."""
    bad_db = mock.MagicMock()
    snap = mock.MagicMock()
    snap.exists = True
    snap.to_dict.return_value = mock.MagicMock()
    bad_db.collection.return_value.document.return_value.get.return_value = snap
    monkeypatch.setattr(m, "db", bad_db)

    _, provenance = m.get_rendez_vous_reglages_state()
    assert provenance == "unreadable"
    saved, errors = m.update_rendez_vous_reglages(_valid_payload())
    assert saved is None and "illisible" in errors[0]


def test_rule_1_stored_truth_never_resurrects_removed_default_type(store):
    only_one = [
        {
            "cle": "bref",
            "libelle": "Appel bref",
            "hearing_type": "conférence",
            "duree_minutes": 15,
            "public": True,
            "ordre_du_jour": "1. Point unique (15 min)",
        }
    ]
    saved, errors = m.update_rendez_vous_reglages(_valid_payload(types_rdv=only_one))
    assert errors == []
    assert saved is not None

    loaded, provenance = m.get_rendez_vous_reglages_state()
    assert provenance == "store"
    assert len(loaded["types_rdv"]) == 1
    assert loaded["types_rdv"][0]["cle"] == "bref"

    raw = store.peek(f"{m.COLLECTION}/{m.DOC_ID}")
    assert raw["id"] == m.DOC_ID
    assert "created_at" in raw and "updated_at" in raw


def test_rule_2_presence_gated_merge_keeps_absent_top_level_fields(store):
    custom_types = [
        {
            "cle": "suivi",
            "libelle": "Suivi",
            "hearing_type": "rencontre",
            "duree_minutes": 45,
            "public": False,
            "ordre_du_jour": "1. Point (45 min)",
        }
    ]
    _, err1 = m.update_rendez_vous_reglages(
        _valid_payload(battement_minutes=20, types_rdv=custom_types)
    )
    assert err1 == []
    first_created = store.peek(f"{m.COLLECTION}/{m.DOC_ID}")["created_at"]

    saved, err2 = m.update_rendez_vous_reglages({"battement_minutes": 30})
    assert err2 == []
    assert saved["battement_minutes"] == 30
    assert saved["types_rdv"] == custom_types
    stored_after = store.peek(f"{m.COLLECTION}/{m.DOC_ID}")
    assert stored_after["created_at"] == first_created


def test_crlf_line_endings_in_agenda_normalize_cleanly_on_save(store):
    payload = _valid_payload()
    payload["types_rdv"][0]["ordre_du_jour"] = "1. Intro (5 min)\r\n2. Fond (25 min)"

    saved, errors = m.update_rendez_vous_reglages(payload)
    assert errors == []
    assert saved["types_rdv"][0]["ordre_du_jour"] == (
        "1. Intro (5 min)\n2. Fond (25 min)"
    )


def test_inactive_weekday_allows_blank_windows_without_error(store):
    payload = _valid_payload()
    payload["plages"]["vendredi"] = {
        "actif": False,
        "matin_debut": "",
        "matin_fin": "",
        "apres_midi_debut": "",
        "apres_midi_fin": "",
    }
    saved, errors = m.update_rendez_vous_reglages(payload)
    assert errors == []
    assert saved["plages"]["vendredi"]["actif"] is False


def test_changed_field_names_lists_only_modified_keys():
    before = default_reglages()
    after = copy.deepcopy(before)
    assert m.changed_field_names(before, after) == []

    after["horizon_jours"] = 30
    after["plages"]["lundi"]["actif"] = False
    assert m.changed_field_names(before, after) == ["horizon_jours", "plages"]


def test_numeric_validation_refuses_out_of_range_and_bools(store):
    for bad in (
        {"battement_minutes": -1},
        {"battement_minutes": 121},
        {"delai_minimum_heures": 0},
        {"horizon_jours": 181},
        {"preavis_modification_heures": True},
        {"horizon_jours": "soixante"},
    ):
        saved, errors = m.update_rendez_vous_reglages(_valid_payload(**bad))
        assert saved is None
        assert errors and "doit être un entier" in errors[0]


def test_window_validation_refuses_incoherent_slots(store):
    missing_day = copy.deepcopy(DEFAULT_WINDOWS)
    del missing_day["vendredi"]
    _, err = m.update_rendez_vous_reglages(_valid_payload(plages=missing_day))
    assert any("lundi au vendredi" in e for e in err)

    cases = [
        ({"actif": True, "matin_debut": "", "matin_fin": "", "apres_midi_debut": "", "apres_midi_fin": ""}, "définissez au moins une plage"),
        ({"actif": True, "matin_debut": "9h30", "matin_fin": "12:00", "apres_midi_debut": "", "apres_midi_fin": ""}, "début du matin invalide"),
        ({"actif": True, "matin_debut": "09:30", "matin_fin": "", "apres_midi_debut": "", "apres_midi_fin": ""}, "début et la fin du matin"),
        ({"actif": True, "matin_debut": "12:00", "matin_fin": "09:30", "apres_midi_debut": "", "apres_midi_fin": ""}, "fin du matin doit suivre"),
        ({"actif": True, "matin_debut": "", "matin_fin": "", "apres_midi_debut": "13:00", "apres_midi_fin": ""}, "début et la fin de l'après-midi"),
        ({"actif": True, "matin_debut": "", "matin_fin": "", "apres_midi_debut": "16:00", "apres_midi_fin": "14:00"}, "fin d'après-midi doit suivre"),
        ({"actif": True, "matin_debut": "09:00", "matin_fin": "13:30", "apres_midi_debut": "13:00", "apres_midi_fin": "16:00"}, "ne peut pas chevaucher"),
    ]
    for lundi_win, expected_substr in cases:
        plages = copy.deepcopy(DEFAULT_WINDOWS)
        plages["lundi"] = lundi_win
        saved, errors = m.update_rendez_vous_reglages(_valid_payload(plages=plages))
        assert saved is None
        assert any(expected_substr in e for e in errors), f"{lundi_win} -> {errors}"


def test_meeting_type_validation_rules_in_french(store):
    base_type = {
        "cle": "eval",
        "libelle": "Évaluation",
        "hearing_type": "consultation",
        "duree_minutes": 45,
        "public": True,
        "ordre_du_jour": "1. Sujet (45 min)",
    }

    _, err_empty = m.update_rendez_vous_reglages(_valid_payload(types_rdv=[]))
    assert any("Au moins un type" in e for e in err_empty)

    too_many = [{**base_type, "cle": f"t{i}"} for i in range(6)]
    _, err_many = m.update_rendez_vous_reglages(_valid_payload(types_rdv=too_many))
    assert any("Maximum 5 types" in e for e in err_many)

    _, err_dup = m.update_rendez_vous_reglages(
        _valid_payload(types_rdv=[base_type, dict(base_type)])
    )
    assert any("en double" in e for e in err_dup)

    _, err_key = m.update_rendez_vous_reglages(
        _valid_payload(types_rdv=[{**base_type, "cle": "_bad"}])
    )
    assert any("clé" in e and "invalide" in e for e in err_key)

    _, err_ht = m.update_rendez_vous_reglages(
        _valid_payload(types_rdv=[{**base_type, "hearing_type": "audience"}])
    )
    assert any("extrajudiciaire admis" in e for e in err_ht)

    _, err_dur = m.update_rendez_vous_reglages(
        _valid_payload(types_rdv=[{**base_type, "duree_minutes": 10}])
    )
    assert any("15 et 240 minutes" in e for e in err_dur)

    _, err_html_label = m.update_rendez_vous_reglages(
        _valid_payload(types_rdv=[{**base_type, "libelle": "Test <b>"}])
    )
    assert any("chevrons" in e for e in err_html_label)

    _, err_blank_agenda = m.update_rendez_vous_reglages(
        _valid_payload(types_rdv=[{**base_type, "ordre_du_jour": "   "}])
    )
    assert any("ordre du jour minuté par défaut est requis" in e for e in err_blank_agenda)

    _, err_long_agenda = m.update_rendez_vous_reglages(
        _valid_payload(types_rdv=[{**base_type, "ordre_du_jour": "a" * 2001}])
    )
    assert any("2000 caractères" in e for e in err_long_agenda)


def test_write_exception_returns_french_error(monkeypatch):
    fake_db = mock.MagicMock()
    snap = mock.MagicMock()
    snap.exists = False
    fake_db.collection.return_value.document.return_value.get.return_value = snap
    fake_db.collection.return_value.document.return_value.set.side_effect = (
        RuntimeError("quota")
    )
    monkeypatch.setattr(m, "db", fake_db)

    saved, errors = m.update_rendez_vous_reglages(_valid_payload())
    assert saved is None
    assert errors == ["Erreur lors de la sauvegarde. Veuillez réessayer."]
