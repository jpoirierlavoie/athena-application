"""The financial statements' rules — models/financial_statements.py (lot B).

PURE: snapshots are built by hand (no Firestore). Every year, amount, name
and number here is synthetic — the repository is public; the books' start
is pinned to the lawyer's decision once, then each test passes its own
fictitious dates through the module's period arithmetic.

The seeded property test is the rollforward's guard: on random ledgers —
defects injected (a direction that contradicts the kind, inexact splits,
card legs in different periods, inconsistent invoices, payments to voided,
draft or unknown invoices, provisions, hand-set « payée ») — the
« écart non expliqué » is zero for every period, and every defect is a
point of its own.
"""

import os
import random
import sys
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import admin_ledger as al
    from models import financial_statements as fs
    from models import invoice as inv_model
    from models import trust
    from services import financial_statements as svc

from mcp import import_audit  # noqa: E402
from utils import periods  # noqa: E402

UTC = timezone.utc
BOOKS = date(2031, 1, 1)


def _d(y, m, d, h=0):
    return datetime(y, m, d, h, tzinfo=UTC)


def _period(slug, today=date(2033, 6, 15)):
    period, error = periods.parse_period(slug, today=today, books_start=BOOKS)
    assert period is not None, error
    return period


def _compute(snapshot, slug, today=date(2033, 6, 15)):
    return fs.compute(snapshot, _period(slug, today), today=today,
                      now=datetime(2033, 6, 15, 14, 0, tzinfo=UTC), books_start=BOOKS)


ACCOUNTS = [
    {"id": "ops1", "name": "Opérations A", "account_type": "opérations"},
    {"id": "ops2", "name": "Opérations B", "account_type": "opérations", "status": "fermé"},
    {"id": "card1", "name": "Carte A", "account_type": "carte_crédit"},
]


def _tx(id_, kind, amount, day, *, account="ops1", direction=None, **extra):
    row = {"id": id_, "account_id": account, "kind": kind, "amount": amount,
           "counterparty": "Contrepartie fictive",
           "date": day, "direction": direction or al.implied_direction(kind),
           "net_amount": 0, "gst_amount": 0, "qst_amount": 0, "status": "compensée"}
    if kind == "dépense" and "net_amount" not in extra:
        row["net_amount"] = amount
    if (kind in al.NON_RESULT_KINDS and row["direction"] == "déboursé"
            and "net_amount" not in extra):
        row["net_amount"] = amount
    row.update(extra)
    return row


def _correction(id_, original, day):
    return {"id": id_, "account_id": original["account_id"], "kind": "correction",
            "amount": original["amount"], "date": day,
            "direction": "recette" if original["direction"] == "déboursé" else "déboursé",
            "net_amount": original["net_amount"] if original["kind"] == "dépense" else 0,
            "gst_amount": original["gst_amount"] if original["kind"] == "dépense" else 0,
            "qst_amount": original["qst_amount"] if original["kind"] == "dépense" else 0,
            "category": original.get("category") if original["kind"] == "dépense" else None,
            "dossier_id": original.get("dossier_id"), "invoice_id": None,
            "trust_transaction_id": None, "reverses_id": original["id"],
            "reverses_kind": original["kind"], "status": "en_circulation"}


def _invoice(id_, day, *, status="envoyée", fees=10000, expenses=0, retainer=0,
             number=None, dossier="dos1", **extra):
    gst = round(fees * 0.05) if fees else 0
    qst = round(fees * 0.09975) if fees else 0
    total = fees + expenses + gst + qst
    inv = {"id": id_, "invoice_number": number or f"2031-F{id_[-3:]}", "status": status,
           "date": day, "subtotal_fees": fees, "subtotal_expenses": expenses,
           "gst_amount": gst, "qst_amount": qst, "total": total,
           "retainer_applied": retainer, "amount_due": total - retainer,
           "amount_paid": 0, "dossier_id": dossier}
    inv.update(extra)
    return inv


def _snapshot(*, transactions=(), invoices=(), trust_rows=(), trust_accounts=None,
              time_entries=(), expenses=(), reconciliations=(), accounts=None):
    return fs.Snapshot(
        admin_accounts=list(accounts if accounts is not None else ACCOUNTS),
        admin_transactions=list(transactions), admin_reconciliations=list(reconciliations),
        invoices=list(invoices),
        trust_accounts=list(trust_accounts if trust_accounts is not None else [
            {"id": "tr1", "name": "Fidéicommis général", "account_type": "général"}]),
        trust_transactions=list(trust_rows), time_entries=list(time_entries),
        expenses=list(expenses))


def _points(st):
    return {p["key"]: p for p in st.points}


# ══════════════════════════════════════════════════════════════════════════
# 1. One source per list
# ══════════════════════════════════════════════════════════════════════════


def test_the_books_start_on_the_lawyers_day():
    assert fs.BOOKS_START == date(2025, 1, 1)


def test_every_kind_has_one_cash_flow_line_and_every_line_a_label():
    assert set(al.CASH_FLOW_LINES) == set(al.VALID_KINDS) - {al.REVERSAL_KIND}
    assert set(fs.CASH_FLOW_LINE_ORDER) == (
        set(al.CASH_FLOW_LINES.values()) | {"encaissements_factures_absentes"})
    assert len(fs.CASH_FLOW_LINE_ORDER) == len(set(fs.CASH_FLOW_LINE_ORDER))
    assert set(fs.CASH_FLOW_LINE_LABELS) == set(fs.CASH_FLOW_LINE_ORDER)


def test_the_cost_classes_and_revenue_statuses_are_the_models():
    assert set(al.VARIABLE_COST_CATEGORIES) <= set(al.ADMIN_EXPENSE_CATEGORIES)
    assert al.VARIABLE_COST_CATEGORIES == ("infonuagique_ia", "fournitures")
    assert set(al.COST_CLASS_LABELS) == set(al.COST_CLASSES)
    assert inv_model.REVENUE_STATUSES == ("envoyée", "en_retard", "payée")
    assert set(inv_model.REVENUE_STATUSES) <= set(inv_model.VALID_STATUSES)


def test_the_service_reads_every_collection_of_the_snapshot():
    assert tuple(svc.READERS) == fs.COLLECTIONS
    assert set(fs.COLLECTION_LABELS) == set(fs.COLLECTIONS)
    assert set(fs.REQUIRED_FOR_PDF) <= set(fs.SECTION_TITLES)


@pytest.mark.parametrize("row, expected", [
    ({"dossier_id": "dos1", "category": "infonuagique_ia"}, "direct"),
    ({"dossier_id": "dos1", "category": "loyer"}, "direct"),
    ({"category": "infonuagique_ia"}, "variable"),
    ({"category": "fournitures"}, "variable"),
    ({"category": "abonnements"}, "fixe"),
    ({"category": "huissier"}, "fixe"),
    ({}, "fixe"),
])
def test_cost_class(row, expected):
    assert al.cost_class(row) == expected


@pytest.mark.parametrize("cited, stored, expected", [
    ("251601-01", "facture:25160101", True),
    ("2031-F012", "2031-F012", True),
    (" 2031-f012 ", "2031-F012", True),
    ("2031-F012", "2031-F0121", False),
    ("", "", True),
])
def test_normalize_reference_compares_identifiers(cited, stored, expected):
    same = inv_model.normalize_reference(cited) == inv_model.normalize_reference(stored)
    assert same is expected


def test_normalize_reference_drops_the_prefix_punctuation_and_accents():
    assert inv_model.normalize_reference("facture:2031-é/12") == "203112"
    assert inv_model.normalize_reference(None) == ""


@pytest.mark.parametrize("marks", [
    {}, {"imported": True}, {"imported": False}, {"imported": False, "legacy_ref": "x"},
    {"legacy_ref": "facture:1"}, {"legacy_ref": "  "}, {"created_via": "mcp"},
    {"created_via": "web"}, {"imported": "oui"},
])
def test_is_imported_is_the_connectors_predicate(marks):
    assert inv_model.is_imported(marks) is import_audit.is_imported(marks)


# ══════════════════════════════════════════════════════════════════════════
# 2. Corrections
# ══════════════════════════════════════════════════════════════════════════


def test_a_correction_counts_against_its_originals_kind_and_links():
    original = _tx("o1", "recette_autre", 5000, _d(2031, 3, 1), trust_transaction_id="t1",
                   dossier_id="dos1")
    fix = _correction("c1", original, _d(2031, 4, 2))
    out = {r["id"]: r for r in al.resolve_corrections([original, fix])}
    assert out["o1"]["sign"] == 1
    assert (out["c1"]["sign"], out["c1"]["kind"], out["c1"]["trust_transaction_id"],
            out["c1"]["dossier_id"], out["c1"]["corrects_id"]) == (
        -1, "recette_autre", "t1", "dos1", "o1")
    assert out["c1"]["direction"] == "déboursé" and out["c1"]["date"] == _d(2031, 4, 2)


@pytest.mark.parametrize("damage", ["absent", "other_account", "of_a_correction"])
def test_an_unresolvable_correction_raises_even_when_stamped(damage):
    original = _tx("o1", "dépense", 5000, _d(2031, 3, 1), category="loyer")
    fix = _correction("c1", original, _d(2031, 4, 2))
    rows = [original, fix]
    if damage == "absent":
        rows = [fix]
    elif damage == "other_account":
        rows = [{**original, "account_id": "ops2"}, fix]
    else:
        rows = [original, fix, _correction("c2", {**fix, "kind": "correction"}, _d(2031, 4, 3))]
    with pytest.raises(al.UnresolvedCorrection):
        al.resolve_corrections(rows)


def test_an_unknown_kind_or_a_row_without_account_raises():
    with pytest.raises(ValueError):
        al.resolve_corrections([{**_tx("x", "dépense", 1, _d(2031, 1, 2)), "kind": "retrait"}])
    with pytest.raises(ValueError):
        al.resolve_corrections([_tx("x", "dépense", 1, _d(2031, 1, 2), account_id="")])


def test_a_correction_in_a_later_period_reverses_there():
    expense = _tx("e1", "dépense", 11498, _d(2031, 3, 30), category="loyer",
                  net_amount=10000, gst_amount=500, qst_amount=998)
    snap = _snapshot(transactions=[expense, _correction("c1", expense, _d(2031, 4, 2))])
    march = _compute(snap, "2031-03").sections["resultats"]
    april = _compute(snap, "2031-04").sections["resultats"]
    assert march["cost_totals"]["fixe"] == 10000
    assert april["cost_totals"]["fixe"] == -10000
    assert _compute(snap, "2031-T2").sections["taxes"]["cti"] == -500


# ══════════════════════════════════════════════════════════════════════════
# 3. Income statement
# ══════════════════════════════════════════════════════════════════════════


def test_revenue_is_billed_invoices_by_invoice_date_before_tax():
    invoices = [
        _invoice("i001", _d(2031, 5, 3), status="envoyée", fees=10000, expenses=2500),
        _invoice("i002", _d(2031, 5, 9), status="en_retard", fees=20000),
        _invoice("i003", _d(2031, 5, 20), status="payée", fees=30000),
        _invoice("i004", _d(2031, 5, 21), status="brouillon", fees=40000),
        _invoice("i005", _d(2031, 5, 22), status="annulée", fees=50000),
        _invoice("i006", _d(2031, 6, 1), status="envoyée", fees=60000),
    ]
    income = _compute(_snapshot(invoices=invoices), "2031-05").sections["resultats"]
    assert (income["fees"], income["billed_expenses"]) == (60000, 2500)
    assert income["revenue"] == 62500


def test_costs_are_net_and_classed_and_equal_the_ledgers_results_statement():
    rows = [
        _tx("e1", "dépense", 2299, _d(2031, 5, 2), category="infonuagique_ia",
            net_amount=2000, gst_amount=100, qst_amount=199, account="card1"),
        _tx("e2", "dépense", 11498, _d(2031, 5, 3), category="huissier", dossier_id="dos1",
            net_amount=10000, gst_amount=500, qst_amount=998),
        _tx("e3", "dépense", 5000, _d(2031, 5, 4), category="loyer"),
        _tx("e4", "dépense", 700, _d(2031, 5, 5), category="fournitures"),
    ]
    income = _compute(_snapshot(transactions=rows), "2031-05").sections["resultats"]
    assert income["cost_totals"] == {"direct": 10000, "variable": 2700, "fixe": 5000}
    assert income["costs"]["variable"] == {"infonuagique_ia": 2000, "fournitures": 700}
    statement = al.results_statement(rows)
    assert sum(income["cost_totals"].values()) == sum(statement["expenses_net"].values())
    assert income["net_income"] == -17700


def test_other_income_excludes_trust_linked_receipts():
    rows = [_tx("r1", "recette_autre", 4000, _d(2031, 5, 2), counterparty="Intérêts"),
            _tx("r2", "recette_autre", 9000, _d(2031, 5, 3), trust_transaction_id="t9")]
    income = _compute(_snapshot(transactions=rows), "2031-05").sections["resultats"]
    assert income["other_total"] == 4000
    assert [r["id"] for r in income["other"]] == ["r1"]


def test_gross_margin_and_net_income():
    rows = [_tx("e1", "dépense", 1000, _d(2031, 5, 2), category="huissier", dossier_id="d"),
            _tx("e2", "dépense", 300, _d(2031, 5, 2), category="fournitures"),
            _tx("e3", "dépense", 200, _d(2031, 5, 2), category="loyer")]
    income = _compute(_snapshot(transactions=rows, invoices=[
        _invoice("i001", _d(2031, 5, 1), fees=5000)]), "2031-05").sections["resultats"]
    assert income["gross_margin"] == 4000
    assert income["net_income"] == 3500


# ══════════════════════════════════════════════════════════════════════════
# 4. Attribution, provisions, hand-set « payée », sums to attach
# ══════════════════════════════════════════════════════════════════════════


def _fee(id_, amount, day, **extra):
    row = {"id": id_, "account_id": "tr1", "purpose": "virement_honoraires",
           "direction": "déboursé", "amount": amount, "date": day,
           "status": "compensée", "dossier_id": "dos1"}
    row.update(extra)
    return row


def test_attribution_by_number_normalized_and_by_id():
    invoices = [_invoice("i001", _d(2031, 5, 1), legacy_ref="facture:25160101",
                         number="25160101", imported=True),
                _invoice("i002", _d(2031, 5, 1), number="2031-F002")]
    trust_rows = [_fee("t1", 5000, _d(2031, 5, 10), invoice_external_ref="251601-01"),
                  _fee("t2", 6000, _d(2031, 5, 11), invoice_id="i002")]
    rows = [_tx("r1", "recette_autre", 5000, _d(2031, 5, 10), trust_transaction_id="t1"),
            _tx("r2", "recette_autre", 6000, _d(2031, 5, 11), trust_transaction_id="t2")]
    st = _compute(_snapshot(transactions=rows, invoices=invoices, trust_rows=trust_rows),
                  "2031-05")
    closing = st.sections["bilan"]["closing"]
    due = invoices[0]["amount_due"] + invoices[1]["amount_due"]
    assert closing.receivables == due - 11000
    assert st.sections["flux"]["lines"]["encaissements_factures"] == 11000
    assert _points(st)["rattachements_presumes"]["count"] == 2
    assert st.sections["avoir"]["unexplained"] == 0


@pytest.mark.parametrize("ref, invoices, expected", [
    ("2031-F777", [], "factures_absentes"),
    ("", [], "sans_reference"),
    ("2031-F001", [_invoice("i001", _d(2031, 5, 1), number="2031-F001", dossier="autre")],
     "autre_dossier"),
    ("2031-F001", [_invoice("i001", _d(2031, 5, 1), number="2031-F001"),
                   _invoice("i002", _d(2031, 5, 1), number="2031F001")], "numero_ambigu"),
])
def test_an_unmatched_trust_receipt_is_no_revenue_and_is_listed(ref, invoices, expected):
    trust_rows = [_fee("t1", 5000, _d(2031, 5, 10), invoice_external_ref=ref)]
    rows = [_tx("r1", "recette_autre", 5000, _d(2031, 5, 10), trust_transaction_id="t1")]
    st = _compute(_snapshot(transactions=rows, invoices=invoices, trust_rows=trust_rows),
                  "2031-05")
    assert st.sections["resultats"]["revenue"] == sum(
        i["subtotal_fees"] + i["subtotal_expenses"] for i in invoices)
    point = _points(st)[expected]
    assert point["refs"] == ["10 mai 2031 — Contrepartie fictive"]
    assert st.sections["avoir"]["unexplained"] == 0
    assert st.sections["flux"]["lines"]["encaissements_factures_absentes"] == 5000
    assert st.sections["avoir"]["factures_absentes"] == 5000
    others = {"factures_absentes", "sans_reference", "autre_dossier", "numero_ambigu",
              "lien_introuvable"} - {expected}
    assert not others & set(_points(st))


def test_a_missing_trust_entry_leaves_the_receipt_unattributed():
    rows = [_tx("r1", "recette_autre", 5000, _d(2031, 5, 10), trust_transaction_id="gone")]
    st = _compute(_snapshot(transactions=rows), "2031-05")
    assert _points(st)["lien_introuvable"]["count"] == 1
    assert "factures_absentes" not in _points(st)


def test_attributed_transfers_cover_the_provision_first():
    inv = _invoice("i001", _d(2031, 5, 1), fees=100000, retainer=30000, imported=True)
    trust_rows = [_fee("t1", 30000, _d(2031, 5, 5), invoice_id="i001"),
                  _fee("t2", 50000, _d(2031, 6, 5), invoice_id="i001")]
    rows = [_tx("r1", "recette_autre", 30000, _d(2031, 5, 5), trust_transaction_id="t1"),
            _tx("r2", "recette_autre", 50000, _d(2031, 6, 5), trust_transaction_id="t2")]
    snap = _snapshot(transactions=rows, invoices=[inv], trust_rows=trust_rows)
    may = _compute(snap, "2031-05")
    assert may.sections["bilan"]["closing"].receivables == inv["amount_due"]
    roll = may.sections["avoir"]
    assert (roll["provisions_applied"], roll["provisions_received"]) == (30000, 30000)
    assert roll["unexplained"] == 0
    june = _compute(snap, "2031-06")
    assert june.sections["bilan"]["closing"].receivables == inv["amount_due"] - 50000
    assert june.sections["avoir"]["unexplained"] == 0


def test_a_provision_received_before_the_invoice_is_a_sum_to_attach_until_then():
    inv = _invoice("i001", _d(2031, 6, 2), fees=100000, retainer=30000)
    trust_rows = [_fee("t1", 30000, _d(2031, 5, 5), invoice_id="i001")]
    rows = [_tx("r1", "recette_autre", 30000, _d(2031, 5, 5), trust_transaction_id="t1")]
    snap = _snapshot(transactions=rows, invoices=[inv], trust_rows=trust_rows)
    may = _compute(snap, "2031-05").sections
    assert may["bilan"]["closing"].unattached == 30000
    june = _compute(snap, "2031-06").sections
    assert june["bilan"]["closing"].unattached == 0
    assert june["avoir"]["provisions_received"] == 30000
    assert june["avoir"]["unexplained"] == 0


def test_a_hand_set_payee_invoice_counts_as_settled_and_is_listed():
    inv = _invoice("i001", _d(2031, 5, 1), status="payée", fees=10000)
    st = _compute(_snapshot(invoices=[inv]), "2031-05")
    assert st.sections["bilan"]["closing"].receivables == 0
    assert st.sections["avoir"]["settled"] == inv["amount_due"]
    assert _points(st)["reglees_hors_registres"]["amount"] == inv["amount_due"]
    assert st.sections["avoir"]["unexplained"] == 0


def test_a_partially_paid_then_hand_set_invoice_settles_its_shortfall():
    inv = _invoice("i001", _d(2031, 5, 1), status="payée", fees=10000, amount_paid=4000)
    rows = [_tx("a1", "encaissement_facture", 4000, _d(2031, 5, 9), invoice_id="i001")]
    st = _compute(_snapshot(transactions=rows, invoices=[inv]), "2031-05")
    assert st.sections["avoir"]["settled"] == inv["amount_due"] - 4000
    assert st.sections["bilan"]["closing"].receivables == 0


def test_an_invoice_paid_after_the_period_is_a_receivable_at_its_end():
    inv = _invoice("i001", _d(2031, 5, 1), status="payée", fees=10000)
    inv["amount_paid"] = inv["amount_due"]
    rows = [_tx("a1", "encaissement_facture", inv["amount_due"], _d(2031, 6, 9),
                invoice_id="i001")]
    st = _compute(_snapshot(transactions=rows, invoices=[inv]), "2031-05")
    assert st.sections["bilan"]["closing"].receivables == inv["amount_due"]
    assert "reglees_hors_registres" not in _points(st)


@pytest.mark.parametrize("status", ["annulée", "brouillon"])
def test_a_payment_on_an_uncounted_invoice_is_a_sum_to_attach(status):
    inv = _invoice("i001", _d(2031, 5, 1), status=status)
    rows = [_tx("a1", "encaissement_facture", 3000, _d(2031, 5, 9), invoice_id="i001")]
    st = _compute(_snapshot(transactions=rows, invoices=[inv]), "2031-05")
    closing = st.sections["bilan"]["closing"]
    assert (closing.receivables, closing.unattached) == (0, 3000)
    assert _points(st)["a_rattacher"]["ids"] == ["a1"]
    assert st.sections["avoir"]["unexplained"] == 0


def test_a_payment_to_an_unknown_invoice_is_a_sum_to_attach():
    rows = [_tx("a1", "encaissement_facture", 3000, _d(2031, 5, 9), invoice_id="gone")]
    st = _compute(_snapshot(transactions=rows), "2031-05")
    assert st.sections["bilan"]["closing"].unattached == 3000


# ══════════════════════════════════════════════════════════════════════════
# 5. Balance sheet, cash flow, taxes
# ══════════════════════════════════════════════════════════════════════════


def test_the_balance_sheet_lists_every_account_and_balances():
    rows = [
        _tx("a1", "apport", 50000, _d(2031, 4, 1)),
        _tx("a2", "apport", 7000, _d(2031, 4, 1), account="ops2"),
        _tx("e1", "dépense", 2299, _d(2031, 5, 2), category="infonuagique_ia",
            net_amount=2000, gst_amount=100, qst_amount=199, account="card1"),
        _tx("p1", "paiement_carte", 1000, _d(2031, 5, 20), direction="déboursé",
            related_transaction_id="p2"),
        _tx("p2", "paiement_carte", 1000, _d(2031, 5, 20), account="card1",
            direction="recette", related_transaction_id="p1"),
    ]
    st = _compute(_snapshot(transactions=rows), "2031-05")
    opening, closing = st.sections["bilan"]["opening"], st.sections["bilan"]["closing"]
    assert opening.cash == {"ops1": 50000, "ops2": 7000}
    assert closing.cash == {"ops1": 49000, "ops2": 7000}
    assert closing.card == {"card1": 1299}
    assert closing.tax_payable == -299
    assert closing.net == 49000 + 7000 - 1299 + 299
    assert st.sections["avoir"]["unexplained"] == 0
    flux = st.sections["flux"]
    assert (flux["opening"], flux["closing"]) == (57000, 56000)
    assert flux["lines"]["paiements_carte"] == -1000


def test_the_tax_table_closes_and_names_remittances_and_refunds():
    invoices = [_invoice("i001", _d(2031, 5, 1), fees=100000)]
    rows = [_tx("e1", "dépense", 11498, _d(2031, 5, 2), category="loyer",
                net_amount=10000, gst_amount=500, qst_amount=998),
            _tx("x1", "remise_tps_tvq", 4000, _d(2031, 5, 20)),
            _tx("x2", "remboursement_tps_tvq", 1000, _d(2031, 5, 25))]
    taxes = _compute(_snapshot(transactions=rows, invoices=invoices), "2031-05").sections["taxes"]
    collected = invoices[0]["gst_amount"] + invoices[0]["qst_amount"]
    assert taxes["net"] == collected - 1498
    assert (taxes["remitted"], taxes["refunded"]) == (4000, 1000)
    assert taxes["closing"] == collected - 1498 - 4000 + 1000


def test_remittances_never_reach_the_income_statement():
    rows = [_tx("x1", "remise_tps_tvq", 4000, _d(2031, 5, 20)),
            _tx("x2", "remboursement_tps_tvq", 1000, _d(2031, 5, 25))]
    st = _compute(_snapshot(transactions=rows), "2031-05")
    assert st.sections["resultats"]["net_income"] == 0
    assert st.sections["avoir"]["unexplained"] == 0


# ══════════════════════════════════════════════════════════════════════════
# 6. The rollforward property — écart non expliqué == 0
# ══════════════════════════════════════════════════════════════════════════


def _random_world(rng: random.Random):
    days = [date(2031, 1, 1) + timedelta(days=i) for i in range(0, 700, 3)]

    def day():
        d = rng.choice(days)
        return _d(d.year, d.month, d.day)

    invoices = []
    for n in range(rng.randint(3, 12)):
        fees = rng.choice([0, 12345, 50000, 99999])
        retainer = rng.choice([0, 0, 0, 5000])
        status = rng.choice(["brouillon", "envoyée", "en_retard", "payée", "annulée"])
        inv = _invoice(f"i{n:03d}", day(), status=status, fees=fees,
                       expenses=rng.choice([0, 777]), retainer=min(retainer, fees),
                       number=f"2031-F{n:03d}", dossier=rng.choice(["dos1", "dos2"]))
        if rng.random() < 0.15:
            inv["amount_due"] += rng.choice([-7, 13])           # inconsistent
        if rng.random() < 0.05:
            inv["date"] = None                                  # undated
        invoices.append(inv)
    trust_rows, rows = [], []
    for n in range(rng.randint(20, 60)):
        kind = rng.choice(["encaissement_facture", "recette_autre", "dépense", "prélèvement",
                           "apport", "virement_interne_sortant", "virement_interne_entrant",
                           "remise_tps_tvq", "remboursement_tps_tvq", "paiement_carte",
                           "trust_receipt"])
        amount = rng.choice([100, 2500, 9999, 31415])
        account = rng.choice(["ops1", "ops2", "card1"])
        tid = f"a{n:03d}"
        if kind == "paiement_carte":
            first = day()
            second = first if rng.random() < 0.8 else first + timedelta(days=rng.choice([1, 40]))
            rows.append(_tx(tid + "o", "paiement_carte", amount, first, account="ops1",
                            direction="déboursé"))
            rows.append(_tx(tid + "c", "paiement_carte", amount, second, account="card1",
                            direction="recette"))
            continue
        if kind == "trust_receipt":
            fee_id = f"t{n:03d}"
            target = rng.choice(invoices)
            ref = rng.choice(["id", "number", "dashed", "unknown", "none"])
            fee = _fee(fee_id, amount, day(), dossier_id=target["dossier_id"])
            if ref == "id":
                fee["invoice_id"] = target["id"]
            elif ref == "number":
                fee["invoice_external_ref"] = target["invoice_number"]
            elif ref == "dashed":
                fee["invoice_external_ref"] = target["invoice_number"].replace("-", "")
            elif ref == "unknown":
                fee["invoice_external_ref"] = "9999-X9"
            if rng.random() > 0.1:
                trust_rows.append(fee)
            rows.append(_tx(tid, "recette_autre", amount, fee["date"], account="ops1",
                            trust_transaction_id=fee_id))
            continue
        extra = {}
        if kind == "encaissement_facture":
            account = "ops1"
            extra["invoice_id"] = rng.choice([i["id"] for i in invoices] + ["gone"])
        if kind == "dépense":
            extra.update(category=rng.choice(al.ADMIN_EXPENSE_CATEGORIES),
                         dossier_id=rng.choice([None, "dos1"]))
            gst = amount // 20
            qst = amount // 10
            net = amount - gst - qst + (rng.choice([0, 0, 0, 3]))     # sometimes inexact
            extra.update(net_amount=net, gst_amount=gst, qst_amount=qst)
        direction = al.implied_direction(kind)
        if rng.random() < 0.05:                                       # wrong sign
            direction = "recette" if direction == "déboursé" else "déboursé"
        rows.append(_tx(tid, kind, amount, day(), account=account, direction=direction,
                        **extra))
    for n, original in enumerate(rng.sample(rows, k=min(6, len(rows)))):
        later = original["date"] + timedelta(days=rng.choice([0, 10, 90]))
        rows.append(_correction(f"c{n:03d}", original, later))
    return _snapshot(transactions=rows, invoices=invoices, trust_rows=trust_rows)


@pytest.mark.parametrize("seed", range(40))
def test_the_rollforward_always_closes(seed):
    rng = random.Random(20311107 + seed)
    snap = _random_world(rng)
    for slug in ("2031-02", "2031-07", "2031-T3", "2031", "2032-03", "2032-T4", "2032"):
        st = _compute(snap, slug)
        assert st.unavailable == {}, st.unavailable
        roll = st.sections["avoir"]
        assert roll["unexplained"] == 0, (seed, slug, roll)
        bilan = st.sections["bilan"]
        assert roll["opening"] == bilan["opening"].net
        assert roll["closing"] == bilan["closing"].net
        for key in ("split", "card", "direction", "invoice"):
            if roll["defects"][key]:
                assert f"ecart_{key}" in _points(st), (seed, slug, key)


def test_a_clean_ledger_has_no_measured_defect():
    rows = [_tx("a1", "apport", 50000, _d(2031, 4, 1)),
            _tx("e1", "dépense", 11498, _d(2031, 5, 2), category="loyer",
                net_amount=10000, gst_amount=500, qst_amount=998)]
    roll = _compute(_snapshot(transactions=rows), "2031").sections["avoir"]
    assert roll["defects_total"] == 0 and roll["unexplained"] == 0


@pytest.mark.parametrize("damage, key", [
    ({"net_amount": 9990}, "split"),
    ({"direction": "recette"}, "direction"),
])
def test_each_defect_is_measured_and_listed(damage, key):
    row = _tx("e1", "dépense", 11498, _d(2031, 5, 2), category="loyer",
              net_amount=10000, gst_amount=500, qst_amount=998)
    row.update(damage)
    st = _compute(_snapshot(transactions=[row]), "2031-05")
    assert st.sections["avoir"]["defects"][key] != 0
    assert _points(st)[f"ecart_{key}"]["ids"] == ["e1"]
    assert st.sections["avoir"]["unexplained"] == 0


def test_card_legs_in_different_periods_are_measured():
    rows = [_tx("p1", "paiement_carte", 1000, _d(2031, 5, 31), direction="déboursé"),
            _tx("p2", "paiement_carte", 1000, _d(2031, 6, 1), account="card1",
                direction="recette")]
    st = _compute(_snapshot(transactions=rows), "2031-05")
    assert st.sections["avoir"]["defects"]["card"] == -1000
    assert "ecart_card" in _points(st)
    assert _compute(_snapshot(transactions=rows), "2031-T2").sections["avoir"]["defects"]["card"] == 0


# ══════════════════════════════════════════════════════════════════════════
# 7. Adjustments, memos
# ══════════════════════════════════════════════════════════════════════════


def test_adjustments_list_voided_and_late_invoices_of_earlier_periods():
    invoices = [
        _invoice("i001", _d(2031, 3, 1), status="annulée", voided_at=_d(2031, 5, 10, 15)),
        _invoice("i002", _d(2031, 3, 2), created_at=_d(2031, 5, 11, 15)),
        _invoice("i003", _d(2031, 3, 3), created_at=_d(2031, 5, 12, 15), imported=True),
        _invoice("i004", _d(2031, 3, 4), status="annulée"),
        _invoice("i005", _d(2031, 5, 4), created_at=_d(2031, 5, 4, 15)),
    ]
    adj = _compute(_snapshot(invoices=invoices), "2031-05").sections["redressements"]
    assert [i["id"] for i in adj["voided"]] == ["i001"]
    assert [i["id"] for i in adj["late"]] == ["i002"]
    assert [i["id"] for i in adj["late_imported"]] == ["i003"]
    assert adj["undated_voids"] == 1


def test_a_void_at_montreal_midnight_counts_on_montreals_day():
    inv = _invoice("i001", _d(2031, 3, 1), status="annulée", voided_at=_d(2031, 6, 1, 2))
    adj = _compute(_snapshot(invoices=[inv]), "2031-05").sections["redressements"]
    assert [i["id"] for i in adj["voided"]] == ["i001"]          # 31 May in Montréal


def test_the_trust_memo_is_the_book_balance_of_each_account():
    rows = [{"id": "t1", "account_id": "tr1", "direction": "recette", "amount": 10000,
             "status": "compensée", "date": _d(2031, 4, 1)},
            {"id": "t2", "account_id": "tr1", "direction": "déboursé", "amount": 2500,
             "status": "annulée", "date": _d(2031, 5, 2)},
            {"id": "t3", "account_id": "tr1", "direction": "recette", "amount": 2500,
             "status": "annulée", "date": _d(2031, 5, 2)}]
    memo = _compute(_snapshot(trust_rows=rows), "2031-05").sections["fideicommis"]
    (account, opening, closing), = memo["accounts"]
    assert (opening, closing) == (10000, 10000)
    assert opening == sum(trust.compute_deltas(r["direction"], r["amount"], r["status"])["book"]
                          for r in rows if r["date"] < _d(2031, 5, 1))


def test_work_in_progress_at_each_day():
    invoices = [_invoice("i001", _d(2031, 5, 20)), _invoice("i002", _d(2031, 5, 3),
                                                          status="brouillon"),
                _invoice("i003", _d(2031, 4, 3), status="annulée")]
    time_entries = [
        {"id": "h1", "date": _d(2031, 4, 2), "billable": True, "amount": 30000,
         "invoice_id": "i001"},
        {"id": "h2", "date": _d(2031, 5, 2), "billable": True, "amount": 15000,
         "invoice_id": "i002"},
        {"id": "h3", "date": _d(2031, 5, 3), "billable": False, "amount": 7000},
        {"id": "h6", "date": _d(2031, 4, 4), "billable": True, "amount": 2500,
         "invoiced": True, "invoice_id": None},
        {"id": "h4", "date": _d(2031, 4, 9), "amount": 4000, "invoice_id": "i003"},
        {"id": "h5", "date": _d(2031, 6, 2), "billable": True, "amount": 9000},
    ]
    expenses = [{"id": "x1", "date": _d(2031, 4, 20), "amount": 1200}]
    wip = _compute(_snapshot(invoices=invoices, time_entries=time_entries, expenses=expenses),
                   "2031-05").sections["travaux"]
    assert wip["opening"] == {"time": 30000, "expenses": 1200, "voided": 4000,
                              "frozen": 2500}
    assert wip["closing"] == {"time": 15000, "expenses": 1200, "voided": 4000,
                              "frozen": 2500}


# ══════════════════════════════════════════════════════════════════════════
# 8. Unavailable, never zero
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("collection, lost", [
    ("invoices", {"resultats", "flux", "bilan", "avoir", "taxes", "redressements", "travaux"}),
    ("admin_transactions", {"resultats", "flux", "bilan", "avoir", "taxes"}),
    ("trust_transactions", {"flux", "bilan", "avoir", "fideicommis"}),
    ("time_entries", {"travaux"}),
])
def test_a_failed_read_makes_its_sections_unavailable(collection, lost):
    snap = _snapshot(invoices=[_invoice("i001", _d(2031, 5, 1))])
    setattr(snap, collection, None)
    snap.failures[collection] = "read"
    st = _compute(snap, "2031-05")
    assert set(st.unavailable) == lost
    for key in lost:
        assert key not in st.sections
        assert fs.COLLECTION_LABELS[collection] in st.unavailable[key]


def test_a_truncated_read_says_so():
    snap = _snapshot()
    snap.invoices = None
    snap.failures["invoices"] = "truncated"
    st = _compute(snap, "2031-05")
    assert st.unavailable["resultats"].startswith("Lecture tronquée (factures)")


def test_an_unresolved_correction_makes_the_ledger_sections_unavailable():
    orphan = _correction("c1", _tx("gone", "dépense", 100, _d(2031, 4, 1), category="loyer"),
                         _d(2031, 5, 2))
    st = _compute(_snapshot(transactions=[orphan]), "2031-05")
    assert "ne retrouve pas l'écriture" in st.unavailable["resultats"]
    assert fs.pdf_refusal(st)


def test_an_unreadable_amount_is_never_rounded():
    snap = _snapshot(invoices=[{**_invoice("i001", _d(2031, 5, 1)), "subtotal_fees": 100.5}])
    st = _compute(snap, "2031-05")
    assert "Montant illisible (subtotal_fees)" in st.unavailable["resultats"]


def test_a_row_naming_an_unknown_account_is_unavailable():
    st = _compute(_snapshot(transactions=[_tx("a1", "apport", 1, _d(2031, 5, 1),
                                              account="ghost")]), "2031-05")
    assert "compte d'administration introuvable" in st.unavailable["resultats"]


def test_the_pdf_needs_the_three_statements():
    snap = _snapshot()
    assert fs.pdf_refusal(_compute(snap, "2031-05")) == ""
    snap.trust_transactions = None
    assert fs.pdf_refusal(_compute(snap, "2031-05"))


# ══════════════════════════════════════════════════════════════════════════
# 9. Points à vérifier
# ══════════════════════════════════════════════════════════════════════════


def test_points_count_every_condition():
    invoices = [
        _invoice("i001", _d(2031, 5, 3), status="brouillon"),
        _invoice("i002", None),
        _invoice("i003", _d(2031, 5, 4), status="mystère"),
        _invoice("i004", _d(2030, 12, 4)),
        {**_invoice("i005", _d(2031, 5, 5)), "amount_due": None},
    ]
    rows = [
        _tx("e1", "dépense", 900, _d(2031, 5, 6), category="huissier"),
        _tx("e2", "dépense", 900, _d(2031, 5, 6), category="taxes_permis"),
        _tx("e3", "dépense", 900, _d(2031, 5, 6), category="autre",
            counterparty="Revenu Québec"),
        _tx("r1", "recette_autre", 300, _d(2031, 5, 7), account="card1"),
    ]
    points = _points(_compute(_snapshot(transactions=rows, invoices=invoices), "2031-05"))
    assert points["brouillons"]["count"] == 1
    assert points["factures_sans_date"]["count"] == 1
    assert points["statut_inconnu"]["count"] == 1
    assert points["avant_les_livres"]["count"] == 1
    assert points["factures_incoherentes"]["count"] == 1
    assert points["huissier_sans_dossier"]["ids"] == ["e1"]
    assert set(points["remise_possible"]["ids"]) == {"e2", "e3"}
    assert set(points["sans_ventilation"]["ids"]) == {"e1", "e2", "e3"}
    assert points["recette_sur_carte"]["ids"] == ["r1"]


def test_an_evening_invoice_of_an_open_period_is_listed():
    inv = _invoice("i001", _d(2033, 6, 16))
    st = _compute(_snapshot(invoices=[inv]), "2033-06", today=date(2033, 6, 15))
    assert st.in_progress
    assert _points(st)["datees_apres_calcul"]["count"] == 1


def test_amount_paid_against_the_register():
    inv = _invoice("i001", _d(2031, 5, 3), amount_paid=500)
    points = _points(_compute(_snapshot(invoices=[inv]), "2031-05"))
    assert points["montant_paye"]["count"] == 1


def test_a_manual_recette_for_an_unlinked_fee_payment_is_marked_not_moved():
    trust_rows = [_fee("t1", 7000, _d(2031, 5, 5), invoice_external_ref="X-1")]
    rows = [_tx("r1", "recette_autre", 7000, _d(2031, 5, 6), counterparty="Client")]
    income = _compute(_snapshot(transactions=rows, trust_rows=trust_rows),
                      "2031-05").sections["resultats"]
    assert income["other_total"] == 7000
    assert income["marks"] == {"r1": date(2031, 5, 5)}


# ══════════════════════════════════════════════════════════════════════════
# 10. Presentation
# ══════════════════════════════════════════════════════════════════════════


def test_present_labels_every_section_in_order():
    view = fs.present(_compute(_snapshot(invoices=[_invoice("i001", _d(2031, 5, 3))]),
                               "2031-05"))
    assert [s["key"] for s in view["sections"]] == list(fs.SECTION_TITLES)
    assert view["period_label"] == "Mai 2031"
    assert view["span"] == "Du 1er mai 2031 au 31 mai 2031"
    assert view["as_of_line"] == "Arrêtée au 31 mai 2031."
    assert view["computed_line"].startswith("Calculé le 15 juin 2033 à 10:00 (heure de Montréal)")
    assert [k["label"] for k in view["kpis"]] == [
        "Produits", "Bénéfice net", "Encaisse", "Comptes clients"]
    bilan = next(s for s in view["sections"] if s["key"] == "bilan")
    assert bilan["columns"] == ["Au 30 avril 2031", "Au 31 mai 2031"]
    for section in view["sections"] + [view["points"]]:
        for row in section["rows"]:
            assert row["style"] in ("line", "subtotal", "total", "heading", "note")
            assert row["style"] == "note" or len(row["values"]) == len(section["columns"])


def test_present_marks_an_open_period_and_an_unavailable_section():
    snap = _snapshot()
    snap.trust_accounts = None
    view = fs.present(_compute(snap, "2033-06", today=date(2033, 6, 15)))
    assert view["as_of_line"] == "Période en cours : calculée au 15 juin 2033 inclusivement."
    memo = next(s for s in view["sections"] if s["key"] == "fideicommis")
    assert memo["unavailable"].startswith("Lecture impossible (comptes en fidéicommis)")
    assert memo["rows"] == []


def test_present_signs_the_rollforward_and_lists_the_floors():
    rows = [_tx("a1", "apport", 5000, _d(2031, 5, 1)),
            _tx("p1", "prélèvement", 2000, _d(2031, 5, 2))]
    snap = _snapshot(transactions=rows, reconciliations=[
        {"id": "r1", "account_id": "ops1", "status": "complétée", "period_end": _d(2031, 4, 30)},
        {"id": "r2", "account_id": "ops1", "status": "brouillon", "period_end": _d(2031, 5, 31)}])
    view = fs.present(_compute(snap, "2031-05"))
    roll = {r["label"]: r["values"][0] for r in
            next(s for s in view["sections"] if s["key"] == "avoir")["rows"]}
    assert roll["Apports de l'avocat"] == 5000
    assert roll["Prélèvements de l'avocat"] == -2000
    assert roll["Écart non expliqué"] == 0
    assert "Opérations A : concilié jusqu'au 30 avril 2031" in view["floors"]
    assert "Carte A : jamais concilié" in view["floors"]


# ══════════════════════════════════════════════════════════════════════════
# 11. Review of lot B — the cases the first tests did not pin
# ══════════════════════════════════════════════════════════════════════════


def test_a_reversed_payment_reopens_the_receivable_on_its_own_day():
    inv = _invoice("i001", _d(2031, 5, 1), fees=10000)
    pay = _tx("a1", "encaissement_facture", inv["amount_due"], _d(2031, 5, 10),
              invoice_id="i001")
    snap = _snapshot(transactions=[pay, _correction("c1", pay, _d(2031, 6, 3))],
                     invoices=[inv])
    resolved = {r["id"]: r for r in al.resolve_corrections(snap.admin_transactions)}
    assert resolved["c1"]["invoice_id"] == "i001"
    may = _compute(snap, "2031-05").sections["bilan"]["closing"]
    june = _compute(snap, "2031-06").sections["bilan"]["closing"]
    assert (may.receivables, may.unattached) == (0, 0)
    assert (june.receivables, june.unattached) == (inv["amount_due"], 0)


def test_a_voided_duplicate_never_blocks_the_live_match():
    invoices = [_invoice("i001", _d(2031, 5, 1), number="25170101", status="annulée"),
                _invoice("i002", _d(2031, 5, 2), number="251701-01")]
    trust_rows = [_fee("t1", 5000, _d(2031, 5, 10), invoice_external_ref="251701-01")]
    rows = [_tx("r1", "recette_autre", 5000, _d(2031, 5, 10), trust_transaction_id="t1")]
    st = _compute(_snapshot(transactions=rows, invoices=invoices, trust_rows=trust_rows),
                  "2031-05")
    assert st.sections["bilan"]["closing"].receivables == invoices[1]["amount_due"] - 5000
    assert "rattachements_presumes" in _points(st)
    assert "numero_ambigu" not in _points(st)


def test_attribution_reads_the_legacy_ref_and_an_unknown_id_is_absent():
    invoices = [_invoice("i001", _d(2031, 5, 1), number="2031-F002",
                         legacy_ref="facture:251601-01")]
    trust_rows = [_fee("t1", 5000, _d(2031, 5, 10), invoice_external_ref="251601-01"),
                  _fee("t2", 700, _d(2031, 5, 11), invoice_id="gone")]
    rows = [_tx("r1", "recette_autre", 5000, _d(2031, 5, 10), trust_transaction_id="t1"),
            _tx("r2", "recette_autre", 700, _d(2031, 5, 11), trust_transaction_id="t2")]
    st = _compute(_snapshot(transactions=rows, invoices=invoices, trust_rows=trust_rows),
                  "2031-05")
    assert _points(st)["rattachements_presumes"]["count"] == 1
    assert _points(st)["factures_absentes"]["amount"] == 700
    assert st.sections["flux"]["lines"]["encaissements_factures_absentes"] == 700


def test_a_legacy_invoice_without_amount_due_owes_its_total_less_its_provision():
    inv = _invoice("i001", _d(2031, 5, 1), fees=10000, retainer=1000)
    inv["amount_due"] = None
    st = _compute(_snapshot(invoices=[inv]), "2031-05")
    assert st.sections["bilan"]["closing"].receivables == inv["total"] - 1000
    assert "factures_incoherentes" in _points(st)
    assert st.sections["avoir"]["unexplained"] == 0


def test_offsetting_card_legs_across_the_boundary_are_still_listed():
    rows = [_tx("pa1", "paiement_carte", 1000, _d(2031, 5, 31), direction="déboursé",
                related_transaction_id="pa2"),
            _tx("pa2", "paiement_carte", 1000, _d(2031, 6, 1), account="card1",
                direction="recette", related_transaction_id="pa1"),
            _tx("pb1", "paiement_carte", 1000, _d(2031, 6, 1), direction="déboursé",
                related_transaction_id="pb2"),
            _tx("pb2", "paiement_carte", 1000, _d(2031, 5, 31), account="card1",
                direction="recette", related_transaction_id="pb1"),
            _tx("pc1", "paiement_carte", 500, _d(2031, 5, 10), direction="déboursé",
                related_transaction_id="pc2"),
            _tx("pc2", "paiement_carte", 500, _d(2031, 5, 10), account="card1",
                direction="recette", related_transaction_id="pc1")]
    st = _compute(_snapshot(transactions=rows), "2031-05")
    assert st.sections["avoir"]["defects"]["card"] == 0
    assert sorted(_points(st)["ecart_card"]["ids"]) == ["pa1", "pb2"]


def test_a_tax_refund_booked_as_other_income_is_listed():
    rows = [_tx("r1", "recette_autre", 2500, _d(2031, 5, 7),
                counterparty="Revenu Québec — remboursement"),
            _tx("v1", "virement_interne_sortant", 900, _d(2031, 5, 8),
                counterparty="Remise TPS TVQ")]
    point = _points(_compute(_snapshot(transactions=rows), "2031-05"))["remise_possible"]
    assert sorted(point["ids"]) == ["r1", "v1"]


def test_a_malformed_fee_amount_never_crashes_the_statements():
    trust_rows = [_fee("t1", None, _d(2031, 5, 5), invoice_external_ref="X-1")]
    trust_rows[0]["amount"] = "beaucoup"
    st = _compute(_snapshot(trust_rows=trust_rows), "2031-05")
    assert "resultats" in st.sections


def test_movements_that_cancel_are_not_called_no_movement():
    rent = _tx("e1", "dépense", 11498, _d(2031, 5, 2), category="loyer")
    snap = _snapshot(transactions=[rent, _correction("c1", rent, _d(2031, 5, 9))])
    rows = [r["label"] for r in next(s for s in fs.present(_compute(snap, "2031-05"))["sections"]
                                    if s["key"] == "flux")["rows"]]
    assert "Aucun mouvement dans la période" not in rows
    assert "Des mouvements qui s'annulent dans la période" in rows


@pytest.mark.parametrize("damage, sentence", [
    ({"net_amount": 869.5}, "Montant illisible (net_amount)"),
    ({"date": None}, "n'a pas de date"),
    ({"kind": "retrait"}, "porte une nature inconnue du registre"),
    ({"account_id": ""}, "ne nomme aucun compte"),
])
def test_each_snapshot_guard_says_why_in_french(damage, sentence):
    row = _tx("e1", "dépense", 1000, _d(2031, 5, 2), category="loyer")
    row.update(damage)
    st = _compute(_snapshot(transactions=[row]), "2031-05")
    assert sentence in st.unavailable["resultats"]
    assert "admin row" not in st.unavailable["resultats"]


def test_an_unknown_account_type_or_an_orphan_trust_entry_says_why():
    accounts = ACCOUNTS + [{"id": "odd", "name": "Bizarre", "account_type": "épargne"}]
    st = _compute(_snapshot(accounts=accounts), "2031-05")
    assert "a un type inconnu" in st.unavailable["resultats"]
    orphan = [{"id": "t1", "account_id": "ghost", "direction": "recette", "amount": 1,
               "status": "compensée", "date": _d(2031, 5, 1)}]
    st = _compute(_snapshot(trust_rows=orphan), "2031-05")
    assert "nomme un compte introuvable" in st.unavailable["fideicommis"]


def test_the_trust_memo_counts_entries_in_circulation():
    rows = [{"id": "t1", "account_id": "tr1", "direction": "recette", "amount": 10000,
             "status": "compensée", "date": _d(2031, 4, 1)},
            {"id": "t2", "account_id": "tr1", "direction": "recette", "amount": 1000,
             "status": "en_circulation", "date": _d(2031, 5, 2)}]
    (_account, opening, closing), = _compute(_snapshot(trust_rows=rows),
                                             "2031-05").sections["fideicommis"]["accounts"]
    assert (opening, closing) == (10000, 11000)


def test_points_never_say_none_over_an_unread_collection():
    snap = _snapshot()
    snap.trust_transactions = None
    view = fs.present(_compute(snap, "2031-05"))
    labels = [r["label"] for r in view["points"]["rows"]]
    assert labels[0].startswith("Liste incomplète — Lecture impossible (registre du fidéicommis)")
    assert "Aucun point à vérifier." not in labels


def test_unread_reconciliations_are_said_never_silent():
    snap = _snapshot()
    snap.admin_reconciliations = None
    view = fs.present(_compute(snap, "2031-05"))
    assert view["floors"] == [
        "Conciliations indisponibles — Lecture impossible (conciliations d'administration) "
        "— réessayez dans un instant."]
    assert view["floors_heading"] == fs.FLOORS_HEADING


def test_point_rows_name_the_records():
    invoices = [_invoice(f"i{n:03d}", _d(2031, 5, n + 1), status="brouillon",
                         number=f"2031-F{n:03d}") for n in range(7)]
    view = fs.present(_compute(_snapshot(invoices=invoices), "2031-05"))
    (row,) = [r for r in view["points"]["rows"] if r["label"].startswith("Factures en brouillon")]
    assert row["note"] == ("7 inscription(s) : 2031-F000, 2031-F001, 2031-F002, 2031-F003, "
                           "2031-F004 et 2 autre(s)")


def _rows(view, key):
    return next(s for s in view["sections"] if s["key"] == key)["rows"]


@pytest.mark.parametrize("seed", range(10))
def test_every_displayed_statement_foots(seed):
    snap = _random_world(random.Random(91 + seed))
    for slug in ("2031-07", "2032-T2", "2032"):
        st = _compute(snap, slug)
        view = fs.present(st)
        roll = _rows(view, "avoir")
        level1 = [r["values"][0] for r in roll if r["level"] == 1]
        by_label = {r["label"]: r["values"][0] for r in roll}
        assert sum(level1) == by_label["Avoir net attendu"] - by_label["Avoir net au début"]
        taxes = {r["label"]: r["values"][0] for r in _rows(view, "taxes")}
        lines = [r["values"][0] for r in _rows(view, "taxes") if r["level"] == 1]
        assert taxes["Solde au début"] + sum(lines) == taxes["Solde à la fin (à remettre)"]
        flux = _rows(view, "flux")
        moves = [r["values"][0] for r in flux if r["level"] == 1 and r["values"][0] is not None]
        ends = {r["label"]: r["values"][0] for r in flux}
        assert ends["Encaisse au début"] + sum(moves) == ends["Encaisse à la fin"]
        bilan = {r["label"]: r["values"] for r in _rows(view, "bilan")}
        opening, closing = st.sections["bilan"]["opening"], st.sections["bilan"]["closing"]
        assert bilan["Comptes clients"] == [opening.receivables, closing.receivables]
        assert bilan["Avoir net (actif − passif)"] == [opening.net, closing.net]
        income = {r["label"]: r["values"][0] for r in _rows(view, "resultats")}
        assert income["Bénéfice net"] == st.sections["resultats"]["net_income"]


@pytest.mark.parametrize("seed", range(20))
def test_the_figures_agree_with_the_ledgers_own_authorities(seed):
    snap = _random_world(random.Random(5150 + seed))
    resolved = al.resolve_corrections(snap.admin_transactions)
    for slug in ("2031-05", "2031-T4", "2032"):
        st = _compute(snap, slug)
        period, as_of = st.period, st.as_of

        def in_period(row):
            day = row["date"].date()
            return period.start <= day <= as_of

        sliced = [r for r in resolved if in_period(r)]
        stamped = [{**r, "kind": "correction", "reverses_kind": r["kind"]}
                   if r["sign"] < 0 else r for r in sliced]
        statement = al.results_statement(stamped)
        income = st.sections["resultats"]
        assert sum(income["cost_totals"].values()) == sum(statement["expenses_net"].values())
        taxes = st.sections["taxes"]
        assert (taxes["cti"], taxes["rti"]) == (statement["gst"], statement["qst"])
        for aid, cents in st.sections["bilan"]["closing"].cash.items():
            assert cents == sum(al.admin_delta(r["direction"], r["amount"])
                                for r in snap.admin_transactions
                                if r["account_id"] == aid and r["date"].date() <= as_of)
