"""TPS/TVQ remittances and refunds in the administration ledger (2026-11-07).

Lot A of the financial statements. Two natures join those outside the
firm's results (``admin_ledger.NON_RESULT_KINDS``): ``remise_tps_tvq`` — the
TPS/TVQ the firm pays the tax authorities — and ``remboursement_tps_tvq`` —
a refund of it. Like the lawyer's drawings and the internal transfers they
move the balance, clear and reconcile, carry no category, no split and no
invoice, and count in no revenue, expense or input-tax total; like the
drawings they never carry a dossier (``NO_DOSSIER_KINDS``). They exist
because they SETTLE the TPS/TVQ the firm owes — what the statements'
liability subtracts. One expense category joins the vocabulary as well:
« infonuagique_ia », usage-billed cloud and AI services.

Same bench as test_admin_natures: the shared fake Firestore
(``tests/_fake_firestore.py``) and a fictitious year on a frozen clock — no
date nor amount here is one of the lawyer's (the repository is public).
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
    from services import comptabilite as svc

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
TODAY = "2031-06-20T16:00:00+00:00"
REMISE = "remise_tps_tvq"
REMBOURSEMENT = "remboursement_tps_tvq"


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
    f.seed("admin_accounts/card1", {
        "id": "card1", "name": "Carte", "status": "actif",
        "account_type": "carte_crédit", "ledger_balance": 0, "etag": "e1",
    })
    f.seed("dossiers/dos1", {"id": "dos1", "file_number": "2031-001",
                             "title": "Dossier d'essai", "status": "actif"})
    return f


def _entry(kind: str, **over) -> dict:
    d = {
        "account_id": "ops1", "kind": kind, "amount": 28463,
        "method": "virement", "counterparty": "Administration fiscale fictive",
        "date": _d(2031, 6, 3), "description": "", "reference": "",
    }
    if kind == "dépense":
        d["category"] = "autre"
    d.update(over)
    return d


def _create(kind: str, **over) -> dict:
    entry, errs = al.create_transaction(_entry(kind, **over))
    assert errs == [], errs
    return entry


def _stored(fake, entry: dict) -> dict:
    return fake.peek(f"admin_transactions/{entry['id']}")


def _ledger(fake, account: str = "ops1") -> int:
    return fake.peek(f"admin_accounts/{account}")["ledger_balance"]


def _row(id_, kind, amount, *, category=None, net=0, gst=0, qst=0, **extra):
    return {"id": id_, "kind": kind, "amount": amount,
            "direction": al._KIND_DIRECTION.get(kind, "recette"),
            "category": category, "net_amount": net, "gst_amount": gst,
            "qst_amount": qst, "status": "compensée", **extra}


# ══════════════════════════════════════════════════════════════════════
# 1. Vocabulary — one source, every list derived and pinned
# ══════════════════════════════════════════════════════════════════════


def test_the_two_natures_sit_outside_the_results_and_carry_no_dossier():
    assert al.SALES_TAX_KINDS == (REMISE, REMBOURSEMENT)
    assert al.NON_RESULT_KINDS == (
        al.OWNER_KINDS + al.INTERNAL_TRANSFER_KINDS + al.SALES_TAX_KINDS)
    assert al.NO_DOSSIER_KINDS == al.OWNER_KINDS + al.SALES_TAX_KINDS
    for kind in al.SALES_TAX_KINDS:
        assert kind not in al.REVENUE_KINDS + al.EXPENSE_KINDS
    # Before the two structural kinds, so the connector's ordered copy
    # stays a plain prefix of theirs.
    assert al.VALID_KINDS[-4:] == (REMISE, REMBOURSEMENT, "paiement_carte",
                                   al.REVERSAL_KIND)


def test_they_are_signed_labelled_creatable_and_editable():
    assert (al._KIND_DIRECTION[REMISE], al._KIND_DIRECTION[REMBOURSEMENT]) == (
        "déboursé", "recette")
    assert al.KIND_LABELS[REMISE] == "Remise de TPS/TVQ"
    assert al.KIND_LABELS[REMBOURSEMENT] == "Remboursement de TPS/TVQ"
    for kind in al.SALES_TAX_KINDS:
        assert kind in al._SIMPLE_KINDS and kind in al._EDITABLE_KINDS
        assert kind in al.FORM_KINDS


def test_every_simple_kind_is_offered_by_the_create_form():
    """A kind missing from FORM_KINDS would be offered by the connector and
    not by the web form — a remittance then booked as a dépense, which
    falsifies both the results and the TPS/TVQ balance."""
    offered = set(al.FORM_KINDS)
    for kind in al._SIMPLE_KINDS:
        assert al.form_kind(kind)[0] in offered, kind


def test_the_category_vocabulary_has_its_labels_and_the_cloud_category():
    """A category without a label renders an empty option and a blank
    « Catégorie » in the CSV — silently. Pinned, with the new key placed
    after « abonnements » (the connector's copy is order-pinned)."""
    assert set(al.ADMIN_CATEGORY_LABELS) == set(al.ADMIN_EXPENSE_CATEGORIES)
    cats = list(al.ADMIN_EXPENSE_CATEGORIES)
    assert cats[cats.index("abonnements") + 1] == "infonuagique_ia"
    assert al.ADMIN_CATEGORY_LABELS["infonuagique_ia"] == (
        "Infonuagique et IA (à l'usage)")


def test_moves_are_closed_within_each_sign():
    """Every editable kind may become any other of its sign — the closure
    the connector's update description states — and only dépense <->
    recette_autre crosses signs."""
    editable = al._EDITABLE_KINDS
    for old in editable:
        for new in editable:
            if old == new:
                continue
            same_sign = al._KIND_DIRECTION[old] == al._KIND_DIRECTION[new]
            legacy = {old, new} == {"dépense", "recette_autre"}
            assert al.kind_move_allowed(old, new) is (same_sign or legacy), (old, new)


def test_the_service_repeats_the_no_dossier_kinds_under_their_own_name():
    """The connector reads the rule through the service (handlers never
    import the model). The constant once named for the lawyer's own money
    now names what it is: the kinds that never carry a dossier."""
    assert svc.ADMIN_NO_DOSSIER_KINDS == al.NO_DOSSIER_KINDS
    assert not hasattr(svc, "ADMIN_OWNER_KINDS")


# ══════════════════════════════════════════════════════════════════════
# 2. Create — the sign, the canonical split, no dossier
# ══════════════════════════════════════════════════════════════════════


def test_a_remittance_is_a_déboursé_for_its_whole_amount(fake):
    entry = _create(REMISE)
    stored = _stored(fake, entry)
    assert stored["direction"] == "déboursé"
    assert stored["category"] is None
    assert (stored["net_amount"], stored["gst_amount"], stored["qst_amount"]) == (
        28463, 0, 0)
    assert _ledger(fake) == -28463


def test_a_refund_is_a_recette_with_no_split(fake):
    entry = _create(REMBOURSEMENT)
    stored = _stored(fake, entry)
    assert stored["direction"] == "recette"
    assert (stored["net_amount"], stored["gst_amount"], stored["qst_amount"]) == (
        0, 0, 0)
    assert _ledger(fake) == 28463


def test_a_remittance_may_be_paid_by_card(fake):
    """No account-type rule: the tax authorities accept a card through a
    processor, and the card's liability then carries it."""
    entry = _create(REMISE, account_id="card1")
    assert _stored(fake, entry)["account_id"] == "card1"
    assert _ledger(fake, "card1") == -28463


@pytest.mark.parametrize("kind", (REMISE, REMBOURSEMENT))
def test_a_remittance_or_refund_never_carries_a_dossier(fake, kind):
    entry, errs = al.create_transaction(_entry(kind, dossier_id="dos1"))
    assert entry is None
    assert errs == [al._ABORT_MESSAGES["dossier_interdit"]]
    for no_dossier in al.NO_DOSSIER_KINDS:
        assert f"« {al.KIND_LABELS[no_dossier]} »" in errs[0]
    assert fake.peek_collection("admin_transactions") == {}


@pytest.mark.parametrize("field, value, reason", [
    ("category", "taxes_permis", "catégorie_interdite"),
    ("gst_amount", 1351, "ventilation_interdite"),
    ("invoice_id", "inv1", "facture_interdite"),
])
def test_a_field_a_remittance_never_carries_is_refused(fake, field, value, reason):
    entry, errs = al.create_transaction(_entry(REMISE, **{field: value}))
    assert entry is None
    assert errs == [al._ABORT_MESSAGES[reason]]


def test_the_split_refusal_says_a_remittance_is_entered_whole():
    """« elles ne portent ni TPS ni TVQ » was false of a remittance, which
    IS TPS/TVQ: it is entered for its whole amount, never split."""
    text = al._ABORT_MESSAGES["ventilation_interdite"]
    assert "montant total" in text
    assert "ne portent ni TPS ni TVQ" not in text


def test_the_cloud_category_is_an_ordinary_dépense_category(fake):
    entry = _create("dépense", category="infonuagique_ia",
                    net_amount=24756, gst_amount=1238, qst_amount=2469)
    assert _stored(fake, entry)["category"] == "infonuagique_ia"


# ══════════════════════════════════════════════════════════════════════
# 3. Edit — passages within the sign, the dossier cleared
# ══════════════════════════════════════════════════════════════════════


def test_a_dépense_becoming_a_remittance_drops_its_category_and_taxes(fake):
    """A remittance booked as a dépense (taxes_permis, the tempting wrong
    home) is moved: its category and its claimed taxes go — they would
    have counted as an expense and as input-tax credits."""
    entry = _create("dépense", category="taxes_permis", net_amount=24756,
                    gst_amount=1238, qst_amount=2469)
    updated, errs = al.update_transaction(entry["id"], {"kind": REMISE})
    assert errs == [], errs
    stored = _stored(fake, entry)
    assert (stored["kind"], stored["direction"], stored["category"]) == (
        REMISE, "déboursé", None)
    assert (stored["net_amount"], stored["gst_amount"], stored["qst_amount"]) == (
        28463, 0, 0)


def test_a_dossier_left_on_an_entry_becoming_a_remittance_is_refused_then_cleared(fake):
    entry = _create("dépense", dossier_id="dos1")
    updated, errs = al.update_transaction(entry["id"], {"kind": REMISE})
    assert updated is None
    assert errs == [al._ABORT_MESSAGES["dossier_interdit"]]
    updated, errs = al.update_transaction(entry["id"], {"kind": REMISE,
                                                        "dossier_id": None})
    assert errs == [], errs
    assert _stored(fake, entry)["dossier_id"] is None


@pytest.mark.parametrize("old, new", [
    (REMISE, "dépense"), (REMISE, "prélèvement"),
    (REMISE, "virement_interne_sortant"), ("prélèvement", REMISE),
    ("virement_interne_sortant", REMISE),
    (REMBOURSEMENT, "recette_autre"), (REMBOURSEMENT, "apport"),
    (REMBOURSEMENT, "virement_interne_entrant"), ("recette_autre", REMBOURSEMENT),
    ("apport", REMBOURSEMENT), ("virement_interne_entrant", REMBOURSEMENT),
])
def test_passages_within_the_sign_are_allowed(fake, old, new):
    entry = _create(old)
    data = {"kind": new}
    if new == "dépense":
        data["category"] = "taxes_permis"
    updated, errs = al.update_transaction(entry["id"], data)
    assert errs == [], (old, new, errs)
    # The KIND moved — a same-sign passage cannot show in the direction
    # alone (final review of lot A).
    stored = _stored(fake, entry)
    assert (stored["kind"], stored["direction"]) == (new, al._KIND_DIRECTION[new])
    assert updated["kind"] == new
    assert "kind" in stored["revisions"][-1]["changes"]


@pytest.mark.parametrize("old, new", [
    (REMISE, REMBOURSEMENT), (REMBOURSEMENT, REMISE),
    (REMISE, "recette_autre"), (REMBOURSEMENT, "dépense"),
])
def test_a_passage_across_the_sign_is_refused(fake, old, new):
    entry = _create(old)
    data = {"kind": new}
    if new == "dépense":
        data["category"] = "taxes_permis"
    updated, errs = al.update_transaction(entry["id"], data)
    assert updated is None
    assert errs == [al._ABORT_MESSAGES["changement_de_sens"]]


# ══════════════════════════════════════════════════════════════════════
# 4. Results, equity, reversal — counted nowhere, netted when reversed
# ══════════════════════════════════════════════════════════════════════


def test_remittances_count_in_no_revenue_expense_nor_tax_total():
    rows = [
        _row("a", "dépense", 2299, category="loyer", net=2000, gst=100, qst=199),
        _row("b", REMISE, 31415, net=31415),
        _row("c", REMBOURSEMENT, 2718),
    ]
    statement = al.results_statement(rows)
    assert statement["total_revenue"] == 0
    assert statement["total_expenses"] == 2299
    assert (statement["gst"], statement["qst"]) == (100, 199)
    assert al.owner_equity(rows) == {"contributions": 0, "drawings": 0, "net": 0}


def test_a_reversed_remittance_carries_no_category_nor_split(fake):
    entry = _create(REMISE)
    reversal, errs = al.reverse_transaction(entry["id"], "erreur de saisie")
    assert errs == [], errs
    stored = fake.peek(f"admin_transactions/{reversal['id']}")
    assert stored["reverses_kind"] == REMISE
    assert stored["category"] is None
    assert (stored["net_amount"], stored["gst_amount"], stored["qst_amount"]) == (
        0, 0, 0)
    rows = [_stored(fake, entry), stored]
    assert al.results_statement(rows)["total_expenses"] == 0
    assert _ledger(fake) == 0


# ══════════════════════════════════════════════════════════════════════
# 5. Integrity — check 11 holds the two natures to their rules
# ══════════════════════════════════════════════════════════════════════


def _run_integrity(fake, monkeypatch, capsys) -> tuple[int, str]:
    from scripts import verify_admin_integrity as vai

    install(monkeypatch, vai, fake=fake)
    code = vai.main()
    return code, capsys.readouterr().out


def test_a_register_with_remittances_is_clean(fake, monkeypatch, capsys):
    _create(REMISE)
    _create(REMBOURSEMENT, amount=1913)
    refund = _create(REMBOURSEMENT, amount=577)
    _, errs = al.reverse_transaction(refund["id"], "erreur de saisie")
    assert errs == [], errs
    code, out = _run_integrity(fake, monkeypatch, capsys)
    assert code == 0, out


def test_check_11_names_a_remittance_carrying_a_dossier(fake, monkeypatch, capsys):
    """Only an edit outside the model (the console) could leave one."""
    entry = _create(REMISE)
    path = f"admin_transactions/{entry['id']}"
    fake.external_write(path, {**fake.peek(path), "dossier_id": "dos1"})
    code, out = _run_integrity(fake, monkeypatch, capsys)
    assert code == 1, out
    assert (f"écriture {entry['id']}: {al.KIND_LABELS[REMISE]} rattachée à un "
            "dossier") in out


def test_check_11_says_a_remittance_claims_no_tax(fake, monkeypatch, capsys):
    """Final review of lot A — « porte ni TPS ni TVQ » is false of a
    remittance: check 11 reads « ne réclame ni TPS ni TVQ »."""
    entry = _create(REMISE)
    path = f"admin_transactions/{entry['id']}"
    fake.external_write(path, {**fake.peek(path), "gst_amount": 1351})
    code, out = _run_integrity(fake, monkeypatch, capsys)
    assert code == 1, out
    assert (f"écriture {entry['id']}: {al.KIND_LABELS[REMISE]} ventilée "
            "28463+1351+0 — cette nature ne réclame ni TPS ni TVQ") in out
