"""The expense categories of 2026-11-08 (the lawyer's decisions, lot F of the
financial statements) and the statements' « Honoraires professionnels »
heading.

* ``location_salle`` — a room rented by the occasion (a meeting, an
  examination, a hearing): a VARIABLE cost when it carries no dossier; an
  office's rent stays ``loyer``.
* ``notaire``, ``expert``, ``avocat`` — the fees of another professional;
  ``honoraires_professionnels`` keeps every other one, relabelled
  « Autres honoraires professionnels », its code kept.
* ``timbres_judiciaires`` — court stamps, greffe fees and copies, apart from
  the permits and other duties of ``taxes_permis``.
* ``admin_ledger.EXPENSE_CATEGORY_GROUPS`` — the categories of
  professional fees (``FEES`` below: the three above, the others, the
  bailiff and the stenographer) print under one heading in the statements:
  a presentation, never a category nor a filter.

The model's door on the shared fake Firestore; a fictitious year on a frozen
clock — no date nor amount here is one of the lawyer's (the repository is
public).
"""

import os
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import admin_ledger as al

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
TODAY = "2031-06-20T16:00:00+00:00"
NEW = {
    "location_salle": "Location de salle",
    "notaire": "Notaire",
    "expert": "Expert",
    "avocat": "Avocat",
    "timbres_judiciaires": "Timbres judiciaires et frais de greffe",
}
FEES = ("notaire", "expert", "avocat", "honoraires_professionnels", "huissier",
        "sténographe")


def _d(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    from utils import deadlines as dl

    frozen = datetime.fromisoformat(TODAY)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(dl, "datetime", _Clock)
    f = install(monkeypatch, *_fake_modules())
    f.seed("admin_accounts/ops1", {
        "id": "ops1", "name": "Opérations", "status": "actif",
        "account_type": "opérations", "ledger_balance": 0, "etag": "e0",
    })
    f.seed("dossiers/dos1", {"id": "dos1", "file_number": "2031-001",
                             "title": "Dossier d'essai", "status": "actif"})
    return f


# ══════════════════════════════════════════════════════════════════════
# 1. Vocabulary
# ══════════════════════════════════════════════════════════════════════


def test_the_new_categories_and_their_labels():
    for code, label in NEW.items():
        assert code in al.ADMIN_EXPENSE_CATEGORIES, code
        assert al.ADMIN_CATEGORY_LABELS[code] == label
    # Relabelled, its code kept: every other professional.
    assert al.ADMIN_CATEGORY_LABELS["honoraires_professionnels"] == (
        "Autres honoraires professionnels")
    # Kept: an office's rent, and the permits and other duties.
    assert al.ADMIN_CATEGORY_LABELS["loyer"] == "Loyer"
    assert al.ADMIN_CATEGORY_LABELS["taxes_permis"] == "Taxes et permis"
    assert set(al.ADMIN_CATEGORY_LABELS) == set(al.ADMIN_EXPENSE_CATEGORIES)
    assert len(al.ADMIN_EXPENSE_CATEGORIES) == len(set(al.ADMIN_EXPENSE_CATEGORIES))
    assert len(set(al.ADMIN_CATEGORY_LABELS.values())) == len(al.ADMIN_CATEGORY_LABELS)


def test_the_fees_of_other_professionals_are_one_group():
    assert al.EXPENSE_CATEGORY_GROUPS == {"honoraires": FEES}
    assert al.EXPENSE_CATEGORY_GROUP_LABELS == {"honoraires": "Honoraires professionnels"}


def test_every_group_is_well_formed():
    """A group heads members of the vocabulary that follow each other in its
    order (the heading prints where its first member stands), each in one
    group only; a group is never a category, and its label is no category's
    label (the statements would print two lines alike)."""
    cats = list(al.ADMIN_EXPENSE_CATEGORIES)
    seen: set = set()
    assert set(al.EXPENSE_CATEGORY_GROUP_LABELS) == set(al.EXPENSE_CATEGORY_GROUPS)
    for group, members in al.EXPENSE_CATEGORY_GROUPS.items():
        assert group not in cats
        assert members and set(members) <= set(cats)
        assert not seen & set(members)
        seen |= set(members)
        first = cats.index(members[0])
        assert tuple(cats[first:first + len(members)]) == members
        assert al.EXPENSE_CATEGORY_GROUP_LABELS[group] not in al.ADMIN_CATEGORY_LABELS.values()


@pytest.mark.parametrize("code", al.ADMIN_EXPENSE_CATEGORIES)
def test_category_group_names_each_members_heading(code):
    assert al.category_group(code) == ("honoraires" if code in FEES else None)


def test_category_group_knows_no_group_nor_unknown_code():
    for code in ("honoraires", "", "inconnue"):
        assert al.category_group(code) is None


# ══════════════════════════════════════════════════════════════════════
# 2. Cost classes
# ══════════════════════════════════════════════════════════════════════


def test_a_room_rented_without_a_dossier_is_a_variable_cost():
    assert "location_salle" in al.VARIABLE_COST_CATEGORIES
    assert al.cost_class({"category": "location_salle"}) == "variable"
    assert al.cost_class({"category": "location_salle", "dossier_id": "dos1"}) == "direct"
    assert al.cost_class({"category": "loyer"}) == "fixe"


@pytest.mark.parametrize("code", ["notaire", "expert", "avocat", "timbres_judiciaires"])
def test_the_other_new_categories_are_fixed_or_direct(code):
    assert code not in al.VARIABLE_COST_CATEGORIES
    assert al.cost_class({"category": code}) == "fixe"
    assert al.cost_class({"category": code, "dossier_id": "dos1"}) == "direct"


# ══════════════════════════════════════════════════════════════════════
# 3. The model's door
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("code", sorted(NEW))
def test_a_depense_is_recorded_in_each_new_category(fake, code):
    entry, errs = al.create_transaction({
        "account_id": "ops1", "kind": "dépense", "amount": 12100,
        "method": "virement", "counterparty": "Fournisseur fictif",
        "date": _d(2031, 6, 3), "description": "", "reference": "",
        "category": code, "dossier_id": "dos1",
    })
    assert errs == [], errs
    stored = fake.peek(f"admin_transactions/{entry['id']}")
    assert (stored["kind"], stored["category"], stored["dossier_id"]) == (
        "dépense", code, "dos1")


def test_a_group_is_never_a_category(fake):
    entry, errs = al.create_transaction({
        "account_id": "ops1", "kind": "dépense", "amount": 12100,
        "method": "virement", "counterparty": "Fournisseur fictif",
        "date": _d(2031, 6, 3), "description": "", "reference": "",
        "category": "honoraires",
    })
    assert entry is None and errs == [al._ABORT_MESSAGES["catégorie_invalide"]]
