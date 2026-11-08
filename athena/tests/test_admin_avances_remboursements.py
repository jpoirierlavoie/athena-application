"""A client's advance, a supplier's refund, the purpose of a transfer and
two categories in the administration ledger (the lawyer's decisions of
2026-11-08, lot E of the financial statements).

* ``avance_honoraires`` — a client's advance received in the operations
  account instead of in trust, named as the trust register names it: client
  money, outside the firm's results (``NON_RESULT_KINDS``), a dossier
  allowed.
* A virement interne SORTANT may carry ``transfer_purpose`` « avances »: the
  transfer that moves those advances to trust (``TRANSFER_PURPOSES``) — the
  statements settle the advances owed with it.
* ``remboursement_depense`` — a supplier's refund of an expense: a recette
  that carries a category and a split like a dépense (``CATEGORY_KINDS``)
  and counts AGAINST it, never as a revenue (``REFUND_KINDS``).
* The categories « documentation » (law reports, journals, books) and
  « recherche » (research billed by the search — a variable cost), and
  « abonnements » relabelled « Logiciels et bureautique », its code kept.

Same bench as test_admin_remises_tps_tvq: the shared fake Firestore and a
fictitious year on a frozen clock — no date nor amount here is one of the
lawyer's (the repository is public).
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
    from models import trust

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
TODAY = "2031-06-20T16:00:00+00:00"
AVANCE = "avance_honoraires"
REMBOURSEMENT = "remboursement_depense"
SORTANT = "virement_interne_sortant"


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


def _entry(kind: str, **over) -> dict:
    d = {
        "account_id": "ops1", "kind": kind, "amount": 2100,
        "method": "virement", "counterparty": "Contrepartie fictive",
        "date": _d(2031, 6, 3), "description": "", "reference": "",
    }
    if kind in al.CATEGORY_KINDS:
        d["category"] = "infonuagique_ia"
    d.update(over)
    return d


def _create(kind: str, **over) -> dict:
    entry, errs = al.create_transaction(_entry(kind, **over))
    assert errs == [], errs
    return entry


def _refused(kind: str, **over) -> str:
    entry, errs = al.create_transaction(_entry(kind, **over))
    assert entry is None and len(errs) == 1, errs
    return errs[0]


def _stored(fake, entry: dict) -> dict:
    return fake.peek(f"admin_transactions/{entry['id']}")


def _split(doc: dict) -> tuple:
    return (doc["net_amount"], doc["gst_amount"], doc["qst_amount"])


# ══════════════════════════════════════════════════════════════════════
# 1. Vocabulary
# ══════════════════════════════════════════════════════════════════════


def test_the_natures_are_signed_labelled_creatable_and_editable():
    assert al.ADVANCE_KINDS == (AVANCE,) and al.REFUND_KINDS == (REMBOURSEMENT,)
    assert al.CATEGORY_KINDS == ("dépense", REMBOURSEMENT)
    assert AVANCE in al.NON_RESULT_KINDS and AVANCE not in al.NO_DOSSIER_KINDS
    for kind in al.REFUND_KINDS:
        assert kind not in al.NON_RESULT_KINDS + al.REVENUE_KINDS + al.EXPENSE_KINDS
    for kind in (AVANCE, REMBOURSEMENT):
        assert al._KIND_DIRECTION[kind] == "recette"
        assert kind in al._SIMPLE_KINDS and kind in al._EDITABLE_KINDS
        assert kind in al.FORM_KINDS
    # Named as the trust register names it — the lawyer's wish.
    assert al.KIND_LABELS[AVANCE] == trust.PURPOSE_LABELS["avance_honoraires"]
    assert al.KIND_LABELS[REMBOURSEMENT] == "Remboursement de dépense"
    assert al.TRANSFER_PURPOSE_KIND == SORTANT
    assert al.TRANSFER_PURPOSES == ("avances",)
    assert set(al.TRANSFER_PURPOSE_LABELS) == {""} | set(al.TRANSFER_PURPOSES)


def test_the_categories_and_the_relabelled_subscriptions():
    for code, label in (("documentation", "Documentation juridique"),
                        ("recherche", "Frais de recherche"),
                        ("abonnements", "Logiciels et bureautique")):
        assert code in al.ADMIN_EXPENSE_CATEGORIES
        assert al.ADMIN_CATEGORY_LABELS[code] == label
    assert set(al.ADMIN_CATEGORY_LABELS) == set(al.ADMIN_EXPENSE_CATEGORIES)
    assert "recherche" in al.VARIABLE_COST_CATEGORIES
    assert "documentation" not in al.VARIABLE_COST_CATEGORIES


# ══════════════════════════════════════════════════════════════════════
# 2. A supplier's refund
# ══════════════════════════════════════════════════════════════════════


def test_a_refund_carries_a_category_and_a_split_of_its_own(fake):
    made = _create(REMBOURSEMENT, net_amount=2000, gst_amount=100, qst_amount=0,
                   dossier_id="dos1")
    stored = _stored(fake, made)
    assert (stored["direction"], stored["category"]) == ("recette", "infonuagique_ia")
    assert _split(stored) == (2000, 100, 0)
    assert stored["dossier_id"] == "dos1"
    assert fake.peek("admin_accounts/ops1")["ledger_balance"] == 2100
    # A blank split claims no tax: net = amount, as on a dépense.
    blank = _stored(fake, _create(REMBOURSEMENT))
    assert _split(blank) == (2100, 0, 0)


@pytest.mark.parametrize("over, reason", [
    ({"category": ""}, "catégorie_requise"),
    ({"category": "logiciels"}, "catégorie_invalide"),
    ({"net_amount": 2000, "gst_amount": 99, "qst_amount": 0}, "ventilation_invalide"),
])
def test_a_refund_is_refused_what_a_depense_is(fake, over, reason):
    assert _refused(REMBOURSEMENT, **over) == al._ABORT_MESSAGES[reason]
    assert fake.peek_collection("admin_transactions") == {}


def test_a_refund_counts_against_its_category_never_as_revenue(fake):
    rows = [_stored(fake, _create("dépense", amount=11498, net_amount=10000,
                                  gst_amount=500, qst_amount=998)),
            _stored(fake, _create(REMBOURSEMENT, net_amount=2000, gst_amount=100,
                                  qst_amount=0))]
    statement = al.results_statement(rows)
    assert statement["total_revenue"] == 0
    assert statement["expenses"] == {"infonuagique_ia": 11498 - 2100}
    assert statement["expenses_net"] == {"infonuagique_ia": 8000}
    assert (statement["gst"], statement["qst"]) == (400, 998)


def test_a_refunds_reversal_carries_its_category_and_split(fake):
    made = _create(REMBOURSEMENT, net_amount=2000, gst_amount=100, qst_amount=0)
    _entry_rows, errs = al.reverse_transaction(made["id"], "Saisie en double")
    assert errs == [], errs
    rows = list(fake.peek_collection("admin_transactions").values())
    correction = next(r for r in rows if r["kind"] == al.REVERSAL_KIND)
    assert correction["category"] == "infonuagique_ia"
    assert _split(correction) == (2000, 100, 0)
    assert correction["reverses_kind"] == REMBOURSEMENT
    statement = al.results_statement(rows)
    assert statement["expenses_net"] == {"infonuagique_ia": 0}
    assert (statement["gst"], statement["qst"]) == (0, 0)


def test_a_refunds_new_amount_needs_its_split(fake):
    made = _create(REMBOURSEMENT, net_amount=2000, gst_amount=100, qst_amount=0)
    entry, errs = al.update_transaction(made["id"], {"amount": 3000})
    assert entry is None and errs == [al._ABORT_MESSAGES["ventilation_requise"]]
    entry, errs = al.update_transaction(made["id"], {
        "amount": 3000, "net_amount": 3000, "gst_amount": 0, "qst_amount": 0})
    assert errs == [] and _split(entry) == (3000, 0, 0)


def test_an_autre_recette_becomes_a_refund_with_its_category(fake):
    made = _create("recette_autre")
    entry, errs = al.update_transaction(made["id"], {"kind": REMBOURSEMENT})
    assert entry is None and errs == [al._ABORT_MESSAGES["catégorie_requise"]]
    entry, errs = al.update_transaction(made["id"], {
        "kind": REMBOURSEMENT, "category": "recherche",
        "net_amount": 2000, "gst_amount": 100, "qst_amount": 0})
    assert errs == []
    assert (entry["kind"], entry["direction"], entry["category"]) == (
        REMBOURSEMENT, "recette", "recherche")
    assert _split(entry) == (2000, 100, 0)
    # And back: the category and the split go.
    entry, errs = al.update_transaction(made["id"], {"kind": "recette_autre"})
    assert errs == [] and entry["category"] is None and _split(entry) == (0, 0, 0)


# ══════════════════════════════════════════════════════════════════════
# 3. A client's advance
# ══════════════════════════════════════════════════════════════════════


def test_an_advance_is_client_money_outside_the_results(fake):
    made = _create(AVANCE, amount=172463, dossier_id="dos1",
                   counterparty="Client fictif")
    stored = _stored(fake, made)
    assert (stored["direction"], stored["category"]) == ("recette", None)
    assert _split(stored) == (0, 0, 0)
    assert stored["dossier_id"] == "dos1"
    assert al.results_statement([stored])["total_revenue"] == 0
    assert al.cash_flow_line(stored) == "avances_recues"


@pytest.mark.parametrize("over, reason", [
    ({"category": "loyer"}, "catégorie_interdite"),
    ({"net_amount": 2100}, "ventilation_interdite"),
    ({"invoice_id": "inv1"}, "facture_interdite"),
    ({"transfer_purpose": "avances"}, "objet_virement_interdit"),
])
def test_an_advance_refuses_what_a_nature_outside_the_results_refuses(fake, over, reason):
    assert _refused(AVANCE, **over) == al._ABORT_MESSAGES[reason]


# ══════════════════════════════════════════════════════════════════════
# 4. The purpose of a virement interne sortant
# ══════════════════════════════════════════════════════════════════════


def test_only_a_virement_interne_sortant_carries_a_purpose(fake):
    marked = _stored(fake, _create(SORTANT, transfer_purpose="avances",
                                   counterparty="Compte en fidéicommis"))
    assert marked["transfer_purpose"] == "avances"
    assert al.is_advance_transfer(marked)
    assert al.cash_flow_line(marked) == al.ADVANCE_TRANSFER_LINE
    plain = _stored(fake, _create(SORTANT))
    assert plain["transfer_purpose"] == "" and not al.is_advance_transfer(plain)
    assert al.cash_flow_line(plain) == "virements_sortants"
    for kind in ("virement_interne_entrant", "dépense", "recette_autre", REMBOURSEMENT):
        assert (_refused(kind, transfer_purpose="avances")
                == al._ABORT_MESSAGES["objet_virement_interdit"]), kind
    assert (_refused(SORTANT, transfer_purpose="épargne")
            == al._ABORT_MESSAGES["objet_virement_invalide"])


def test_a_purpose_is_edited_and_leaves_with_its_kind(fake):
    made = _create(SORTANT, transfer_purpose="avances")
    entry, errs = al.update_transaction(made["id"], {"transfer_purpose": ""})
    assert errs == [] and entry["transfer_purpose"] == ""
    assert entry["revisions"][-1]["changes"] == {"transfer_purpose": ["avances", ""]}
    entry, errs = al.update_transaction(made["id"], {"transfer_purpose": "avances"})
    assert errs == [] and entry["transfer_purpose"] == "avances"
    # Becoming another kind, the transfer's mark goes with it — never refused.
    entry, errs = al.update_transaction(made["id"], {"kind": "prélèvement"})
    assert errs == [] and entry["transfer_purpose"] == ""
    assert entry["revisions"][-1]["changes"]["transfer_purpose"] == ["avances", ""]
    # A purpose SENT on the kind it becomes is refused, nothing written.
    before = fake.peek(f"admin_transactions/{made['id']}")
    entry, errs = al.update_transaction(made["id"], {"transfer_purpose": "avances"})
    assert entry is None and errs == [al._ABORT_MESSAGES["objet_virement_interdit"]]
    assert fake.peek(f"admin_transactions/{made['id']}") == before


def test_a_legacy_row_without_the_field_reads_as_no_purpose(fake):
    """Rows written before 2026-11-08 lack the key: « autre », never an
    advance — and an edit that does not name it adds nothing."""
    made = _create(SORTANT)
    path = f"admin_transactions/{made['id']}"
    legacy = {k: v for k, v in fake.peek(path).items() if k != "transfer_purpose"}
    fake.external_write(path, legacy)
    assert not al.is_advance_transfer(legacy)
    entry, errs = al.update_transaction(made["id"], {"description": "Note"})
    assert errs == [] and "transfer_purpose" not in entry
    assert entry["revisions"][-1]["changes"] == {"description": ["", "Note"]}


# ══════════════════════════════════════════════════════════════════════
# 5. The integrity script's check 11
# ══════════════════════════════════════════════════════════════════════


def _run_integrity(fake, monkeypatch, capsys) -> tuple[int, str]:
    from scripts import verify_admin_integrity as vai

    install(monkeypatch, vai, fake=fake)
    code = vai.main()
    return code, capsys.readouterr().out


def test_a_register_with_the_new_natures_is_clean(fake, monkeypatch, capsys):
    _create(AVANCE, amount=172463, dossier_id="dos1")
    _create(SORTANT, amount=172463, transfer_purpose="avances")
    refund = _create(REMBOURSEMENT, net_amount=2000, gst_amount=100, qst_amount=0)
    _create(REMBOURSEMENT, amount=577, category="documentation")
    _, errs = al.reverse_transaction(refund["id"], "erreur de saisie")
    assert errs == [], errs
    code, out = _run_integrity(fake, monkeypatch, capsys)
    assert code == 0, out


@pytest.mark.parametrize("kind, damage, said", [
    (REMBOURSEMENT, {"category": None}, "Remboursement de dépense sans catégorie connue"),
    (REMBOURSEMENT, {"net_amount": 1}, "Remboursement de dépense dont la ventilation"),
    (REMBOURSEMENT, {"invoice_id": "inv1"}, "Remboursement de dépense liée à une facture"),
    (AVANCE, {"category": "loyer"}, "Avance d'honoraires portant une catégorie"),
    (AVANCE, {"gst_amount": 99}, "Avance d'honoraires ventilée"),
    ("virement_interne_entrant", {"transfer_purpose": "avances"},
     "objet de virement « avances » sur une écriture « Virement interne (entrant) »"),
    (SORTANT, {"transfer_purpose": "épargne"},
     "objet de virement « épargne » sur une écriture « Virement interne (sortant) »"),
])
def test_check_11_names_what_only_an_edit_outside_the_model_leaves(
        fake, monkeypatch, capsys, kind, damage, said):
    entry = _create(kind)
    path = f"admin_transactions/{entry['id']}"
    fake.external_write(path, {**fake.peek(path), **damage})
    code, out = _run_integrity(fake, monkeypatch, capsys)
    assert code == 1, out
    assert f"écriture {entry['id']}: {said}" in out, out
