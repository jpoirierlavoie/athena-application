"""Tests for the template kind discriminator (Phase H.3 — the « note » kind).

Pure: the form-mapping helper and label agreement, no Firestore (the
selectors are Firestore-coupled and stay untested, consistent with the
existing suite's treatment of get_note_honoraires_template).
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models.doc_template import (
        GABARIT_LIST_KINDS, KIND_LABELS, MODELE_KIND, SPECIAL_KINDS,
        VALID_KINDS, is_modele,
    )
    from routes.doc_templates import _kind_from_form


def test_valid_kinds_all_labelled():
    assert set(VALID_KINDS) == set(KIND_LABELS)
    # Lot 3d: « note_credit », the credit note of a write-off — a special
    # kind, designated « actif » like the note d'honoraires.
    assert VALID_KINDS == ("gabarit", "note_honoraires", "note_credit", "note",
                           "modele")
    assert SPECIAL_KINDS == ("note_honoraires", "note_credit", "note")
    assert KIND_LABELS["note_credit"] == "Note de crédit (radiation)"


def test_the_modele_kind_is_a_plain_kind_listed_apart():
    """Bibliothèque (2026-11-11): « modele » — a contract model — is never
    designated (not special) and is the ONE kind the Gabarits tab and the
    generation popup leave out; every other kind, a future one included,
    lands on the Gabarits side (derived, never restated)."""
    assert MODELE_KIND == "modele"
    assert MODELE_KIND not in SPECIAL_KINDS
    assert set(GABARIT_LIST_KINDS) == set(VALID_KINDS) - {MODELE_KIND}
    assert KIND_LABELS[MODELE_KIND] == "Modèle (sans champs)"


def test_is_modele_reads_the_kind_only():
    assert is_modele({"kind": "modele"})
    for kind in GABARIT_LIST_KINDS:
        assert not is_modele({"kind": kind})
    # A legacy record (no kind) is a gabarit; no record is no modèle.
    assert not is_modele({})
    assert not is_modele(None)


def test_radio_value_wins():
    for kind in VALID_KINDS:
        assert _kind_from_form({"kind": kind}) == kind


def test_radio_value_wins_over_legacy_checkbox():
    assert _kind_from_form({"kind": "note", "is_note_honoraires": "1"}) == "note"


def test_unknown_radio_falls_through_to_legacy_checkbox():
    assert _kind_from_form({"kind": "n_importe_quoi", "is_note_honoraires": "1"}) == (
        "note_honoraires"
    )


def test_neither_field_defaults_to_gabarit():
    assert _kind_from_form({}) == "gabarit"
    assert _kind_from_form({"kind": "  "}) == "gabarit"
