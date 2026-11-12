"""The write-off (« radiation ») — lot 3a of « Factures : retard, radiation,
intérêts », part 1: the model and its guards (the lawyer's decisions D2, D3
and D6 of 2026-11-11, and the defaults adopted with them).

A write-off is a dated EVENT on the invoice (``write_offs``), never a
register entry: ``amount_paid`` is never touched. ``models/invoice`` holds
the derived readers (``write_off_events``, ``standing_write_offs``,
``written_off_of``, ``settlement_label``, ``write_off_refusal``,
``write_off_tax_parts``); ``balance_of`` subtracts what stands written off;
ONE private settlement rule serves ``payment_updates`` and the two doors,
``write_off_remainder`` (« Radier le solde ») and ``reverse_write_off``
(« Annuler la radiation »); and the guards elsewhere refuse what a standing
radiation forbids — the void, « Rouvrir », a payment in either register (the
fee payment's composite included). Check 13 of
``scripts/verify_admin_integrity`` watches invariants I3 and I4; the
connector reaches neither door (``mcp/disclosure``).

Every « today » is FROZEN at the one seam (``tests/_frozen_clock``) on fixed
days of a fictitious year — never a widened offset — and the holidays come
from ``utils.deadlines``' own calendar. Through the real models on the
shared fake Firestore (``tests/_fake_firestore``); every amount is invented.
"""

import logging
import os
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
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
    from models import concurrency, fee_payment, provenance, trust
    from models import invoice as invoice_model
    from scripts import verify_admin_integrity as vai

from google.api_core import exceptions as gexc  # noqa: E402

from tests._accounting_history import FEE_PAYEE  # noqa: E402
from tests._fake_firestore import install  # noqa: E402
from tests._frozen_clock import freeze_deadlines  # noqa: E402
from utils import deadlines  # noqa: E402
from utils import logging_setup  # noqa: E402

UTC = timezone.utc
INV = "inv-radiation-1"
ETAG = "dddddddd-1111-4222-8333-444444444444"
# Monday 10 March 2031, 11:00 in Montréal (EDT): the invoice below, due on
# 3 February, has been late since the 4th.
TODAY = datetime(2031, 3, 10, 15, 0, tzinfo=UTC)
# The fictitious invoice's figures: 750,00 $ of fees, TPS 5 %, TVQ 9,975 %.
SUBTOTAL, GST, QST, TOTAL = 75000, 3750, 7481, 86231


def _utc_midnight(day: date) -> datetime:
    """A date-only field as the models store it: midnight UTC."""
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _invoice(**over) -> dict:
    """An issued invoice of a fictitious year, overdue on TODAY."""
    doc = {
        "id": INV, "invoice_number": "2031-F042", "status": "en_retard",
        "dossier_id": "dos-9", "dossier_file_number": "2031-009",
        "dossier_title": "Exemple c. Témoin", "client_id": "cli-9",
        "client_name": "Cliente Exemple",
        "date": datetime(2031, 1, 3, tzinfo=UTC),
        "due_date": _utc_midnight(date(2031, 2, 3)),
        "subtotal": SUBTOTAL, "gst_rate": 500, "gst_amount": GST,
        "qst_rate": 9975, "qst_amount": QST, "total": TOTAL,
        "retainer_applied": 0, "amount_due": TOTAL, "amount_paid": 0,
        "paid_date": None, "etag": ETAG,
    }
    doc.update(over)
    return doc


def _credit_note(eid: str, day: date) -> str:
    """The credit note number a fixture radiation carries (lot 3b): the year
    of its day, then the digits ending its id — « rad-2 » → « 2031-NC002 »,
    so two radiations of one invoice never share one."""
    digits = "".join(ch for ch in eid if ch.isdigit()) or "1"
    return f"{day.year}-NC{int(digits):03d}"


def _radiation(eid: str = "rad-1", *, amount: int = TOTAL, gst: int = GST,
               qst: int = QST, claimed: bool = True,
               day: date = date(2031, 3, 5), **over) -> dict:
    doc = {
        "id": eid, "kind": "radiation", "date": _utc_midnight(day),
        "amount": amount, "gst_amount": gst if claimed else 0,
        "qst_amount": qst if claimed else 0, "tax_claimed": claimed,
        "reason": "", "reverses_id": "",
        "credit_note_number": _credit_note(eid, day),
        "created_at": datetime(2031, 3, 5, 15, tzinfo=UTC), "created_via": "web",
    }
    doc.update(over)
    return doc


def _contre_passation(of: dict, eid: str = "cp-1",
                      day: date = date(2031, 3, 6), **over) -> dict:
    doc = {
        "id": eid, "kind": "contre_passation", "date": _utc_midnight(day),
        **{name: of[name] for name in ("amount", "gst_amount", "qst_amount",
                                       "tax_claimed")},
        "reason": "", "reverses_id": of["id"],
        "created_at": datetime(2031, 3, 6, 15, tzinfo=UTC), "created_via": "web",
    }
    doc.update(over)
    return doc


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    freeze_deadlines(monkeypatch, TODAY)
    return install(monkeypatch, *_fake_modules())


def _seed(fake, **over) -> dict:
    doc = _invoice(**over)
    fake.seed(f"invoices/{INV}", doc)
    return doc


def _stored(fake) -> dict:
    return fake.peek(f"invoices/{INV}")


def _write_off(fake, **kwargs):
    kwargs.setdefault("expected_etag", _stored(fake)["etag"])
    return invoice_model.write_off_remainder(INV, **kwargs)


def _reverse(fake, event_id: str, **kwargs):
    kwargs.setdefault("expected_etag", _stored(fake)["etag"])
    return invoice_model.reverse_write_off(INV, event_id, **kwargs)


def _invoice_lines(caplog) -> list[dict]:
    return [r.json_fields for r in caplog.records if r.name == "pallas.invoice"]


# ══════════════════════════════════════════════════════════════════════
# 1. The readers — pure
# ══════════════════════════════════════════════════════════════════════


def test_the_vocabulary_is_the_two_kinds():
    assert invoice_model.WRITE_OFF_KINDS == ("radiation", "contre_passation")
    assert invoice_model.WRITE_OFF_REASON_MAX_LENGTH == 500


def test_a_legacy_invoice_has_nothing_written_off():
    """Absent on every invoice until its first write-off, read as []."""
    inv = _invoice(amount_paid=30000)
    assert "write_offs" not in inv
    assert invoice_model.write_off_events(inv) == []
    assert invoice_model.standing_write_offs(inv) == []
    assert invoice_model.written_off_of(inv) == 0
    assert invoice_model.balance_of(inv) == TOTAL - 30000
    assert invoice_model.written_off_of({**inv, "write_offs": []}) == 0


def test_a_standing_radiation_comes_off_the_balance_and_its_cancellation_puts_it_back():
    rad = _radiation(amount=56231, gst=2445, qst=4878)
    inv = _invoice(amount_paid=30000, write_offs=[rad])
    assert invoice_model.standing_write_offs(inv) == [rad]
    assert invoice_model.written_off_of(inv) == 56231
    assert invoice_model.balance_of(inv) == 0
    cancelled = {**inv, "write_offs": [rad, _contre_passation(rad)]}
    assert invoice_model.standing_write_offs(cancelled) == []
    assert invoice_model.written_off_of(cancelled) == 0
    assert invoice_model.balance_of(cancelled) == 56231
    # Two radiations, the first cancelled: only the second stands.
    second = _radiation("rad-2", amount=56231, gst=2445, qst=4878,
                        day=date(2031, 3, 7))
    again = {**inv, "write_offs": [rad, _contre_passation(rad), second]}
    assert invoice_model.standing_write_offs(again) == [second]


_RAD = _radiation()
# Each shape a malformed array can take — never counted as 0.
_MALFORMED = {
    "none": None,
    "a-record": {},
    "a-text": "radiation",
    "a-number": 0,
    "not-a-record": ["radiation"],
    "no-id": [{**_RAD, "id": ""}],
    "duplicate-id": [_RAD, {**_RAD, "date": _utc_midnight(date(2031, 3, 6))}],
    "unknown-kind": [{**_RAD, "kind": "remise"}],
    "text-amount": [{**_RAD, "amount": "862,31"}],
    "float-amount": [{**_RAD, "amount": 862.31}],
    # A bool is no amount — even where every other figure would agree.
    "bool-amount": [{**_RAD, "amount": True, "gst_amount": 0, "qst_amount": 0,
                     "tax_claimed": False}],
    "bool-tax": [{**_RAD, "gst_amount": False}],
    "zero-amount": [{**_RAD, "amount": 0, "gst_amount": 0, "qst_amount": 0}],
    "negative-tax": [{**_RAD, "gst_amount": -1}],
    "taxes-beyond-amount": [{**_RAD, "amount": 100, "gst_amount": 60, "qst_amount": 60}],
    "unclaimed-but-taxed": [{**_RAD, "tax_claimed": False}],
    "claim-not-a-bool": [{**_RAD, "tax_claimed": "oui"}],
    "naive-date": [{**_RAD, "date": datetime(2031, 3, 5)}],
    "time-of-day": [{**_RAD, "date": datetime(2031, 3, 5, 14, tzinfo=UTC)}],
    "text-date": [{**_RAD, "date": "2031-03-05"}],
    "radiation-reversing": [{**_RAD, "reverses_id": "x"}],
    "link-not-a-text": [{**_RAD, "reverses_id": None}],
    "orphan-contre-passation": [_contre_passation(_RAD)],
    "contre-passation-first": [_contre_passation(_RAD), _RAD],
    "cancelled-twice": [_RAD, _contre_passation(_RAD),
                        _contre_passation(_RAD, "cp-2")],
    "other-figures": [_RAD, {**_contre_passation(_RAD), "amount": TOTAL - 1}],
    "other-claim": [_RAD, {**_contre_passation(_RAD), "tax_claimed": False,
                           "gst_amount": 0, "qst_amount": 0}],
    "cancelled-before-written": [_RAD, _contre_passation(_RAD, day=date(2031, 3, 4))],
    "cancelling-a-cancellation": [_RAD, _contre_passation(_RAD),
                                  _contre_passation(_contre_passation(_RAD), "cp-2")],
    # Lot 3b: a radiation's credit note number — « AAAA-NC### », the year of
    # its own date, once per invoice — and none on a contre_passation.
    "unnumbered": [{k: v for k, v in _RAD.items() if k != "credit_note_number"}],
    "number-not-a-text": [{**_RAD, "credit_note_number": 1}],
    "number-blank": [{**_RAD, "credit_note_number": ""}],
    "number-an-invoices": [{**_RAD, "credit_note_number": "2031-F001"}],
    "number-two-digits": [{**_RAD, "credit_note_number": "2031-NC01"}],
    "number-lowercase": [{**_RAD, "credit_note_number": "2031-nc001"}],
    "number-short-year": [{**_RAD, "credit_note_number": "31-NC001"}],
    "number-padded": [{**_RAD, "credit_note_number": " 2031-NC001"}],
    "number-trailing": [{**_RAD, "credit_note_number": "2031-NC001\n"}],
    "number-other-digits": [{**_RAD, "credit_note_number": "2031-NC\u0661\u0662\u0663"}],
    "number-other-year": [{**_RAD, "credit_note_number": "2030-NC001"}],
    "number-twice": [_RAD, _radiation("rad-2", credit_note_number="2031-NC001")],
    "numbered-contre-passation": [_RAD, {**_contre_passation(_RAD),
                                         "credit_note_number": "2031-NC002"}],
    "blank-numbered-contre-passation": [_RAD, {**_contre_passation(_RAD),
                                               "credit_note_number": ""}],
}


@pytest.mark.parametrize("shape", sorted(_MALFORMED), ids=sorted(_MALFORMED))
def test_a_malformed_array_raises_wherever_it_is_read(shape):
    """Never counted as 0 (« A failed read never passes for absence »): the
    one judge raises, and so does every figure built on it — the balance
    among them, whose unreadable field is then named ``write_offs``."""
    inv = _invoice(write_offs=_MALFORMED[shape])
    for read in (invoice_model.write_off_events, invoice_model.standing_write_offs,
                 invoice_model.written_off_of, invoice_model.balance_of):
        with pytest.raises(invoice_model.WriteOffsUnreadable):
            read(inv)
    assert invoice_model.unreadable_balance_field(inv) == "write_offs"


def test_the_defect_names_the_position_never_a_value():
    inv = _invoice(write_offs=[_RAD, {**_RAD, "id": "rad-2", "amount": "1 234,56"}])
    with pytest.raises(invoice_model.WriteOffsUnreadable) as exc:
        invoice_model.write_off_events(inv)
    assert str(exc.value) == ("événement n° 2 : montant illisible (des cents "
                              "entiers, positifs)")
    assert "1 234,56" not in str(exc.value)


def test_the_label_says_how_a_payee_was_settled():
    rad = _radiation(amount=56231, gst=2445, qst=4878)
    assert invoice_model.settlement_label(_invoice(status="payée", amount_paid=TOTAL)) == "Payée"
    assert invoice_model.settlement_label(
        _invoice(status="payée", write_offs=[_radiation()])) == "Soldée par radiation"
    assert invoice_model.settlement_label(
        _invoice(status="payée", amount_paid=30000, write_offs=[rad])) == (
        "Payée en partie — solde radié")
    # A cancelled radiation settles nothing: a hand-set « payée » reads « Payée ».
    assert invoice_model.settlement_label(_invoice(
        status="payée", write_offs=[_RAD, _contre_passation(_RAD)])) == "Payée"
    # Any other status reads as its own label.
    assert invoice_model.settlement_label(_invoice(write_offs=[rad])) == "En retard"
    assert invoice_model.settlement_label(_invoice(status="envoyée")) == "Envoyée"


# ══════════════════════════════════════════════════════════════════════
# 2. When a remainder may be written off — the lateness authority
# ══════════════════════════════════════════════════════════════════════


def test_the_refusal_is_empty_exactly_when_the_invoice_is_overdue():
    days = [date(2031, 2, 3) + timedelta(days=n) for n in range(0, 60, 3)]
    cases = [_invoice(), _invoice(status="envoyée"), _invoice(status="brouillon"),
             _invoice(status="payée"), _invoice(status="annulée"),
             _invoice(amount_paid=TOTAL), _invoice(due_date=None),
             _invoice(status="payée", write_offs=[_radiation()])]
    for inv in cases:
        for today in days:
            overdue = invoice_model.is_overdue(inv, today=today)
            refusal = invoice_model.write_off_refusal(inv, today=today)
            assert (refusal == "") is overdue, (inv["status"], today)


def test_the_refusal_names_why():
    late = date(2031, 3, 10)
    assert "brouillon" in invoice_model.write_off_refusal(
        _invoice(status="brouillon"), today=late)
    assert "annulée" in invoice_model.write_off_refusal(
        _invoice(status="annulée"), today=late)
    assert "« Soldée par radiation »" in invoice_model.write_off_refusal(
        _invoice(status="payée", write_offs=[_radiation()]), today=late)
    assert "plus de solde dû" in invoice_model.write_off_refusal(
        _invoice(amount_paid=TOTAL), today=late)
    assert "pas de date d'échéance" in invoice_model.write_off_refusal(
        _invoice(due_date=None), today=late)


def test_a_saturday_due_date_is_written_off_only_from_tuesday():
    saturday = date(2031, 3, 1)
    assert saturday.weekday() == 5
    inv = _invoice(due_date=_utc_midnight(saturday))
    for today in (date(2031, 3, 1), date(2031, 3, 2), date(2031, 3, 3)):
        refusal = invoice_model.write_off_refusal(inv, today=today)
        assert "à compter du 2031-03-04" in refusal, today
    assert invoice_model.write_off_refusal(inv, today=date(2031, 3, 4)) == ""


def test_today_is_a_required_montreal_day():
    with pytest.raises(TypeError):
        invoice_model.write_off_refusal(_invoice())            # noqa
    with pytest.raises(TypeError):
        invoice_model.write_off_refusal(_invoice(), today=TODAY)


# ══════════════════════════════════════════════════════════════════════
# 3. The TPS/TVQ parts — D3
# ══════════════════════════════════════════════════════════════════════


def test_the_tax_parts_are_each_tax_in_proportion_of_the_total():
    """Hand-computed: 56 231 × 3 750 ÷ 86 231 = 2 445,36… → 2 445;
    56 231 × 7 481 ÷ 86 231 = 4 878,34… → 4 878."""
    assert invoice_model.write_off_tax_parts(_invoice(), 56231) == (2445, 4878)
    assert invoice_model.write_off_tax_parts(_invoice(), TOTAL) == (GST, QST)


def test_a_retainer_invoice_takes_its_fraction_of_the_total_never_of_amount_due():
    """With 200,00 $ of retainer applied, amount_due < total: the fraction of
    the remainder 662,31 $ is taken on the 862,31 $ BILLED — 2 880 and
    5 746 —, where amount_due as the denominator would claim the invoice's
    whole TPS and TVQ (3 750 and 7 481) on less than the whole."""
    inv = _invoice(retainer_applied=20000, amount_due=66231)
    assert invoice_model.write_off_tax_parts(inv, 66231) == (2880, 5746)


def test_the_parts_round_half_up_once():
    """2,5 → 3 (half-even would give 2) and 4,975 → 5."""
    inv = {"total": 2000, "gst_amount": 100, "qst_amount": 199}
    assert invoice_model.write_off_tax_parts(inv, 50) == (3, 5)
    assert invoice_model.write_off_tax_parts(inv, 10) == (1, 1)
    for amount in range(0, 2001, 7):
        expected = tuple(
            int((Decimal(amount) * Decimal(tax) / Decimal(2000))
                .quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            for tax in (100, 199))
        assert invoice_model.write_off_tax_parts(inv, amount) == expected


def test_a_zero_total_has_no_tax_part():
    assert invoice_model.write_off_tax_parts(
        {"total": 0, "gst_amount": 0, "qst_amount": 0}, 0) == (0, 0)


@pytest.mark.parametrize("over", [
    {"total": None}, {"gst_amount": "37,50"}, {"qst_amount": 74.81},
    {"total": True, "gst_amount": 0, "qst_amount": 0}, {"gst_amount": -1},
    {"gst_amount": 90000},
], ids=["no-total", "text-tps", "float-tvq", "bool-total", "negative", "beyond"])
def test_an_unreadable_tax_figure_raises_never_a_guess(over):
    with pytest.raises(ValueError):
        invoice_model.write_off_tax_parts(_invoice(**over), 1000)
    with pytest.raises(ValueError):
        invoice_model.write_off_tax_parts(_invoice(), 10.5)


# ══════════════════════════════════════════════════════════════════════
# 4. « Radier le solde » — the door
# ══════════════════════════════════════════════════════════════════════


def test_the_whole_remainder_is_written_off_and_settles_the_invoice(fake, caplog):
    _seed(fake, amount_paid=30000)
    fake.seed("admin_transactions/enc-1", {
        "id": "enc-1", "kind": "encaissement_facture", "invoice_id": INV,
        "amount": 30000, "status": "compensée"})
    fake.reset_logs()
    with caplog.at_level(logging.INFO, logger="pallas.invoice"):
        report, errors = _write_off(fake, reason="Client introuvable.")
    assert errors == []
    stored = _stored(fake)
    (event,) = stored["write_offs"]
    assert event == report["write_off"]
    assert (event["kind"], event["amount"], event["gst_amount"],
            event["qst_amount"], event["tax_claimed"]) == (
        "radiation", 56231, 2445, 4878, True)
    assert event["date"] == _utc_midnight(date(2031, 3, 10))
    assert (event["reason"], event["reverses_id"]) == ("Client introuvable.", "")
    # Lot 3b: its credit note's number, the year's first.
    assert event["credit_note_number"] == "2031-NC001"
    assert stored["status"] == "payée"
    assert stored["amount_paid"] == 30000            # never touched
    assert invoice_model.balance_of(stored) == 0
    assert invoice_model.settlement_label(stored) == "Payée en partie — solde radié"
    # ONE commit — the year's credit-note counter WITH the invoice (lot 3b;
    # the invoice alone before it: pin rewritten deliberately); every read
    # inside the transaction.
    assert len(fake.commits) == 1
    assert sorted(fake.commits[0].ops) == [("set", "counters/notes-credit-2031"),
                                           ("update", f"invoices/{INV}")]
    assert fake.peek("counters/notes-credit-2031")["seq"] == 1
    assert fake.reads_outside_transactions() == []
    # The report is the invoice AS WRITTEN.
    assert report["invoice"]["etag"] == stored["etag"] != ETAG
    assert report["invoice"]["status"] == "payée"
    (line,) = _invoice_lines(caplog)
    assert line == {"event": "invoice_written_off", "outcome": "success",
                    "invoice_id": INV, "write_off_id": event["id"],
                    "tax_claimed": True, "via": "script", "status": "payée",
                    "dossier_id": "dos-9"}


def test_nothing_paid_reads_soldee_par_radiation(fake):
    _seed(fake)
    report, errors = _write_off(fake)
    assert errors == []
    stored = _stored(fake)
    assert stored["write_offs"][0]["amount"] == TOTAL
    assert (stored["write_offs"][0]["gst_amount"],
            stored["write_offs"][0]["qst_amount"]) == (GST, QST)
    assert invoice_model.settlement_label(stored) == "Soldée par radiation"


def test_the_tax_unclaimed_carries_no_part(fake):
    _seed(fake)
    _report, errors = _write_off(fake, claim_tax=False)
    assert errors == []
    event = _stored(fake)["write_offs"][0]
    assert (event["amount"], event["gst_amount"], event["qst_amount"],
            event["tax_claimed"]) == (TOTAL, 0, 0, False)


def test_a_retainer_invoice_is_written_off_on_its_total(fake):
    _seed(fake, retainer_applied=20000, amount_due=66231)
    _report, errors = _write_off(fake)
    assert errors == []
    event = _stored(fake)["write_offs"][0]
    assert (event["amount"], event["gst_amount"], event["qst_amount"]) == (
        66231, 2880, 5746)


def test_the_writer_is_stamped_from_its_context_never_an_argument(fake):
    _seed(fake)
    with provenance.writing_via("web"):
        report, errors = _write_off(fake)
    assert errors == []
    stored = _stored(fake)
    event = stored["write_offs"][0]
    assert event["created_via"] == "web" and stored["updated_via"] == "web"
    assert event["created_at"] == stored["updated_at"]
    assert stored["etag"] != ETAG
    assert "etag" not in event and "updated_via" not in event   # never edited
    assert report["invoice"]["updated_via"] == "web"


def test_the_reason_is_stored_with_lf_breaks_and_judged_as_stored(fake):
    """A textarea posts CRLF; the reason is stored with LF and judged as
    STORED: 200 lines of « a » are 601 characters as posted, 401 as stored."""
    _seed(fake)
    posted = "a\r\n" * 200 + "b"
    assert len(posted) > invoice_model.WRITE_OFF_REASON_MAX_LENGTH
    _report, errors = _write_off(fake, reason="  " + posted + "\r\n")
    assert errors == []
    assert _stored(fake)["write_offs"][0]["reason"] == "a\n" * 200 + "b"


@pytest.mark.parametrize("reason,needle", [
    ("Le client <b>refuse</b>", "chevrons"),
    ("x" * 501, "501"),
    (None, "un texte"),
    (12, "un texte"),
], ids=["chevrons", "too-long", "none", "a-number"])
def test_a_reason_sanitize_would_alter_is_refused_never_stripped(fake, caplog, reason, needle):
    _seed(fake)
    fake.reset_logs()
    with caplog.at_level(logging.INFO, logger="pallas.invoice"):
        report, errors = invoice_model.write_off_remainder(
            INV, expected_etag=ETAG, reason=reason)
    assert report is None and needle in errors[0]
    assert "write_offs" not in _stored(fake)
    assert fake.reads == [] and fake.commits == []
    (line,) = _invoice_lines(caplog)
    assert (line["operation"], line["reason"]) == ("write_off", "motif_invalide")


def test_the_version_is_required(fake, caplog):
    _seed(fake)
    fake.reset_logs()
    with caplog.at_level(logging.INFO, logger="pallas.invoice"):
        report, errors = invoice_model.write_off_remainder(INV, expected_etag=None)
    assert (report, errors) == (None, [invoice_model.WRITE_OFF_VERSION_REQUIRED])
    assert fake.reads == [] and fake.commits == []
    assert _invoice_lines(caplog)[-1]["reason"] == "version_requise"
    with pytest.raises(TypeError):
        invoice_model.write_off_remainder(INV)                  # noqa


def test_claim_tax_is_a_bool(fake):
    _seed(fake)
    with pytest.raises(TypeError):
        invoice_model.write_off_remainder(INV, expected_etag=ETAG, claim_tax="oui")
    assert fake.commits == []


def test_a_stale_version_writes_nothing(fake, caplog):
    _seed(fake)
    fake.reset_logs()
    with caplog.at_level(logging.INFO, logger="pallas.invoice"):
        report, errors = invoice_model.write_off_remainder(
            INV, expected_etag="eeeeeeee-1111-4222-8333-444444444444")
    assert (report, errors) == (None, [concurrency.STALE_ETAG_ERROR])
    assert "write_offs" not in _stored(fake) and fake.commits == []
    assert _invoice_lines(caplog)[-1]["reason"] == "stale_etag"


@pytest.mark.parametrize("invoice_id", ["", "a/b", None, ".."],
                         ids=["blank", "slashed", "none", "dots"])
def test_an_id_that_names_no_invoice_reads_nothing(fake, invoice_id):
    report, errors = invoice_model.write_off_remainder(invoice_id, expected_etag=ETAG)
    assert (report, errors) == (None, ["Facture introuvable."])
    assert fake.reads == []


def test_a_missing_invoice_is_refused(fake):
    report, errors = invoice_model.write_off_remainder("inv-absente", expected_etag=ETAG)
    assert (report, errors) == (None, ["Facture introuvable."])


@pytest.mark.parametrize("instant,day", [
    (datetime(2031, 3, 1, 15, tzinfo=UTC), "2031-03-04"),     # the Saturday
    (datetime(2031, 3, 3, 20, tzinfo=UTC), "2031-03-04"),     # payable Monday
    (datetime(2031, 3, 4, 2, 30, tzinfo=UTC), "2031-03-04"),  # 21:30 EST, the 3rd
], ids=["saturday", "monday", "evening-band"])
def test_not_yet_overdue_on_a_saturday_due_date(fake, monkeypatch, caplog, instant, day):
    """Due Saturday 1 March 2031, payable until Monday the 3rd, late on
    Tuesday the 4th — judged on today_mtl, read INSIDE the transaction, on
    the frozen clock; 02:30 UTC on the 4th is still the 3rd in Montréal."""
    _seed(fake, status="envoyée", due_date=_utc_midnight(date(2031, 3, 1)))
    freeze_deadlines(monkeypatch, instant)
    fake.reset_logs()
    with caplog.at_level(logging.INFO, logger="pallas.invoice"):
        report, errors = _write_off(fake)
    assert report is None and f"à compter du {day}" in errors[0]
    assert "write_offs" not in _stored(fake) and fake.commits == []
    assert _invoice_lines(caplog)[-1]["reason"] == "pas_en_retard"
    freeze_deadlines(monkeypatch, datetime(2031, 3, 4, 5, 0, tzinfo=UTC))
    _report, errors = _write_off(fake)
    assert errors == [] and _stored(fake)["status"] == "payée"


def test_a_holiday_due_date_is_written_off_only_once_late(fake, monkeypatch):
    """Good Friday 2031 (utils.deadlines' own calendar), then the weekend and
    Easter Monday: payable until Tuesday 15 April, late on the 16th."""
    good_friday = date(2031, 4, 11)
    holidays = deadlines.get_quebec_holidays(2031, regime=deadlines.CIVIL)
    assert good_friday in holidays and good_friday + timedelta(days=3) in holidays
    _seed(fake, status="envoyée", due_date=_utc_midnight(good_friday))
    freeze_deadlines(monkeypatch, datetime(2031, 4, 15, 16, tzinfo=UTC))
    report, errors = _write_off(fake)
    assert report is None and "à compter du 2031-04-16" in errors[0]
    freeze_deadlines(monkeypatch, datetime(2031, 4, 16, 16, tzinfo=UTC))
    _report, errors = _write_off(fake)
    assert errors == []
    assert _stored(fake)["write_offs"][0]["date"] == _utc_midnight(date(2031, 4, 16))


@pytest.mark.parametrize("over,needle", [
    ({"status": "payée", "amount_paid": TOTAL}, "« Payée »"),
    ({"status": "brouillon"}, "brouillon"),
    ({"status": "annulée"}, "annulée"),
    ({"amount_paid": TOTAL}, "plus de solde dû"),
], ids=["payee", "brouillon", "annulee", "settled"])
def test_only_an_overdue_invoice_is_written_off(fake, over, needle):
    _seed(fake, **over)
    report, errors = _write_off(fake)
    assert report is None and needle in errors[0]
    assert fake.commits == []


def test_the_registers_must_agree_with_the_amount_paid(fake, caplog):
    """(a) check 8's drift: the invoice records 300,00 $ that no standing
    encaissement backs — writing off « the rest » would write off a sum the
    registers do not say is owed."""
    _seed(fake, amount_paid=30000)
    fake.reset_logs()
    with caplog.at_level(logging.INFO, logger="pallas.invoice"):
        report, errors = _write_off(fake)
    assert report is None and "contrôle d'intégrité n° 8" in errors[0]
    assert "write_offs" not in _stored(fake) and fake.commits == []
    assert _invoice_lines(caplog)[-1]["reason"] == "encaissements_discordants"
    # A reversed encaissement backs nothing either.
    fake.seed("admin_transactions/enc-1", {
        "id": "enc-1", "kind": "encaissement_facture", "invoice_id": INV,
        "amount": 30000, "status": "compensée", "reversed_by_id": "rev-1"})
    _report, errors = _write_off(fake)
    assert "contrôle d'intégrité n° 8" in errors[0]
    # One that stands — the witness.
    fake.seed("admin_transactions/enc-2", {
        "id": "enc-2", "kind": "encaissement_facture", "invoice_id": INV,
        "amount": 30000, "status": "en_circulation"})
    _report, errors = _write_off(fake)
    assert errors == []


def test_a_register_row_more_than_the_invoice_records_is_drift_too(fake):
    _seed(fake, amount_paid=20000)
    fake.seed("admin_transactions/enc-1", {
        "id": "enc-1", "kind": "encaissement_facture", "invoice_id": INV,
        "amount": 30000, "status": "compensée"})
    _report, errors = _write_off(fake)
    assert "contrôle d'intégrité n° 8" in errors[0]
    # A row whose amount is no integer cents adds up to nothing: drift too.
    unreadable = [{"kind": "encaissement_facture", "amount": True,
                   "status": "compensée"}]
    refusal = invoice_model._register_drift(_invoice(amount_paid=1), unreadable, [])
    assert refusal is not None and refusal.reason == "encaissements_discordants"


def test_a_trust_fee_payment_needs_its_linked_encaissement(fake, caplog):
    """(b): a standing trust fee payment names the invoice with no standing
    encaissement linked to it — trust, register and invoice disagree."""
    _seed(fake, amount_paid=30000)
    fake.seed("trust_transactions/ttx-1", {
        "id": "ttx-1", "purpose": "virement_honoraires", "invoice_id": INV,
        "amount": 30000, "status": "compensée"})
    fake.seed("admin_transactions/enc-1", {
        "id": "enc-1", "kind": "encaissement_facture", "invoice_id": INV,
        "amount": 30000, "status": "compensée"})          # no link
    fake.reset_logs()
    with caplog.at_level(logging.INFO, logger="pallas.invoice"):
        report, errors = _write_off(fake)
    assert report is None and "fidéicommis" in errors[0]
    assert "write_offs" not in _stored(fake) and fake.commits == []
    assert _invoice_lines(caplog)[-1]["reason"] == "fideicommis_sans_encaissement"
    # Linked, it stands: the witness.
    fake.seed("admin_transactions/enc-1", {
        "id": "enc-1", "kind": "encaissement_facture", "invoice_id": INV,
        "amount": 30000, "status": "compensée", "trust_transaction_id": "ttx-1"})
    _report, errors = _write_off(fake)
    assert errors == []
    # A reversed trust fee payment is no drift.
    assert invoice_model._register_drift(
        _invoice(), [], [{"id": "t2", "purpose": "virement_honoraires",
                          "status": "annulée"}]) is None


@pytest.mark.parametrize("over,label", [
    ({"amount_due": "862,31"}, "Le montant dû"),
    ({"amount_paid": None}, "Le montant payé"),
    ({"write_offs": [{**_RAD, "amount": "1 000,00"}]}, "Les radiations"),
], ids=["amount_due", "amount_paid", "write_offs"])
def test_an_unreadable_amount_is_refused_by_name(fake, caplog, over, label):
    _seed(fake, **over)
    fake.reset_logs()
    with caplog.at_level(logging.INFO):
        report, errors = _write_off(fake)
    assert report is None and errors[0].startswith(label)
    assert fake.commits == []
    assert _invoice_lines(caplog)[-1]["reason"] == "montant_illisible"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_an_unreadable_tax_figure_is_refused_when_claimed(fake):
    _seed(fake, total=None)
    report, errors = _write_off(fake)
    assert report is None and "TPS ou la TVQ" in errors[0]
    _report, errors = _write_off(fake, claim_tax=False)       # nothing to read
    assert errors == []


# ══════════════════════════════════════════════════════════════════════
# 5. « Annuler la radiation » — the reversal, settle and reopen
# ══════════════════════════════════════════════════════════════════════


def test_a_cancellation_reopens_en_retard_and_a_second_is_refused(fake, caplog):
    _seed(fake)
    report, _ = _write_off(fake, reason="Irrécouvrable.")
    radiation = report["write_off"]
    with caplog.at_level(logging.INFO, logger="pallas.invoice"):
        report, errors = _reverse(fake, radiation["id"], reason="Le client a payé.")
    assert errors == []
    stored = _stored(fake)
    first, cancel = stored["write_offs"]
    assert first == radiation                          # never edited
    assert cancel == report["write_off"]
    assert (cancel["kind"], cancel["reverses_id"], cancel["reason"]) == (
        "contre_passation", radiation["id"], "Le client a payé.")
    assert [cancel[k] for k in ("amount", "gst_amount", "qst_amount", "tax_claimed")] == [
        radiation[k] for k in ("amount", "gst_amount", "qst_amount", "tax_claimed")]
    # Lot 3b: the cancellation takes no number, and moves no counter; the
    # radiation keeps its own.
    assert "credit_note_number" not in cancel
    assert first["credit_note_number"] == "2031-NC001"
    assert fake.peek("counters/notes-credit-2031")["seq"] == 1
    assert cancel["date"] == _utc_midnight(date(2031, 3, 10))
    assert stored["status"] == "en_retard"
    assert invoice_model.balance_of(stored) == TOTAL
    assert invoice_model.settlement_label(stored) == "En retard"
    (line,) = [l for l in _invoice_lines(caplog)
               if l["event"] == "invoice_write_off_reversed"]
    assert line == {"event": "invoice_write_off_reversed", "outcome": "success",
                    "invoice_id": INV, "write_off_id": cancel["id"],
                    "reverses_id": radiation["id"], "tax_claimed": True,
                    "via": "script", "status": "en_retard", "dossier_id": "dos-9"}
    # A second cancellation of the same radiation: refused, nothing written.
    before = _stored(fake)
    report, errors = _reverse(fake, radiation["id"])
    assert report is None and "déjà annulée" in errors[0]
    assert _stored(fake) == before
    # A cancellation is never cancelled; an unknown id names nothing.
    for unknown in (cancel["id"], "rad-inconnue", ""):
        report, errors = _reverse(fake, unknown)
        assert report is None and "introuvable" in errors[0], unknown
    assert _stored(fake) == before


def test_written_off_again_once_cancelled(fake):
    """The way back when the client pays after all: cancel the radiation,
    record the payment, then write off any new remainder. Here none was
    received: the remainder is written off again, by a second radiation."""
    _seed(fake)
    first, _ = _write_off(fake)
    _reverse(fake, first["write_off"]["id"])
    report, errors = _write_off(fake)
    assert errors == []
    stored = _stored(fake)
    assert [e["kind"] for e in stored["write_offs"]] == [
        "radiation", "contre_passation", "radiation"]
    assert invoice_model.standing_write_offs(stored) == [report["write_off"]]
    assert stored["status"] == "payée" and invoice_model.balance_of(stored) == 0


def test_the_reversal_refuses_a_stale_version_and_requires_one(fake):
    _seed(fake)
    report, _ = _write_off(fake)
    event_id = report["write_off"]["id"]
    before = _stored(fake)
    assert invoice_model.reverse_write_off(INV, event_id, expected_etag=ETAG) == (
        None, [concurrency.STALE_ETAG_ERROR])
    assert invoice_model.reverse_write_off(INV, event_id, expected_etag=None) == (
        None, [invoice_model.WRITE_OFF_VERSION_REQUIRED])
    _r, errors = invoice_model.reverse_write_off(
        INV, event_id, expected_etag=before["etag"], reason="<i>non</i>")
    assert "chevrons" in errors[0]
    assert _stored(fake) == before


def test_settle_and_reopen_through_both_registers(fake):
    """A payment of 300,00 $, the rest written off; the payment's cheque
    bounces — its contre-passation REDUCES the payment, still allowed, and
    reopens the invoice « en_retard » (it carries a write-off); the radiation
    cancelled, the whole debt is back."""
    _seed(fake, status="envoyée")
    _seed_registers(fake)
    entry, errs = al.create_transaction(_encaissement(30000))
    assert errs == [], errs
    assert _stored(fake)["amount_paid"] == 30000
    report, errors = _write_off(fake)
    assert errors == [] and report["write_off"]["amount"] == 56231
    assert _stored(fake)["status"] == "payée"
    _rev, errs = al.reverse_transaction(entry["id"], "Chèque sans provision.")
    assert errs == [], errs
    stored = _stored(fake)
    assert stored["amount_paid"] == 0 and stored["status"] == "en_retard"
    assert invoice_model.balance_of(stored) == 30000      # 862,31 − 562,31
    assert len(invoice_model.standing_write_offs(stored)) == 1
    _r, errors = _reverse(fake, report["write_off"]["id"])
    assert errors == []
    stored = _stored(fake)
    assert stored["status"] == "en_retard" and invoice_model.balance_of(stored) == TOTAL


def test_amount_paid_is_never_touched_and_check_8_stays_exact(fake, monkeypatch, capsys):
    _seed(fake, status="envoyée")
    _seed_registers(fake)
    al.create_transaction(_encaissement(30000))
    _write_off(fake)
    assert _stored(fake)["amount_paid"] == al.sum_invoice_receipts(INV) == 30000
    install(monkeypatch, vai, fake=fake)
    problems, notes = vai.collect()
    assert problems == [] and notes == []


# ══════════════════════════════════════════════════════════════════════
# 6. A commit whose answer was lost — never « nothing written »
# ══════════════════════════════════════════════════════════════════════


def _land_then(fake, monkeypatch, exc, marker: str = f"/invoices/{INV}") -> dict:
    """The next commit touching *marker* APPLIES, then its caller is answered
    *exc* — the real client and its real ``transactional`` loop see what
    production would (tests/test_fee_payment's helper)."""
    server = fake._fake_server
    real_commit = server.commit
    state = {"armed": True}

    def _commit(request, metadata=None, **kwargs):
        response = real_commit(request, metadata=metadata, **kwargs)
        writes = [getattr(w, "_pb", w) for w in request.get("writes") or []]
        if state["armed"] and any(marker in server._write_name(w) for w in writes):
            state["armed"] = False
            raise exc
        return response

    monkeypatch.setattr(server, "commit", _commit)
    return state


@pytest.mark.parametrize("door", ["write_off", "reversal"])
def test_a_retry_over_its_own_landed_commit_writes_once_and_says_so(
        fake, monkeypatch, caplog, door):
    """The commit LANDS, its answer is lost, the retried commit is answered
    « Aborted »: the decorator re-runs the body over the landed write. Read as
    a stale version (its etag moved) it said « nothing was written » over a
    write that stands; the door finds THIS call's event and answers it."""
    _seed(fake)
    radiation = None
    if door == "reversal":
        radiation = _write_off(fake)[0]["write_off"]
    _land_then(fake, monkeypatch, gexc.Aborted("the retried commit of a landed transaction"))
    with caplog.at_level(logging.INFO, logger="pallas.invoice"):
        report, errors = (_write_off(fake) if door == "write_off"
                          else _reverse(fake, radiation["id"]))
    assert errors == []
    stored = _stored(fake)
    expected = 1 if door == "write_off" else 2
    assert len(stored["write_offs"]) == expected            # written ONCE
    assert report["write_off"] == stored["write_offs"][-1]
    # Lot 3b: one number consumed, by the radiation's one commit.
    assert fake.peek("counters/notes-credit-2031")["seq"] == 1
    event = ("invoice_written_off" if door == "write_off"
             else "invoice_write_off_reversed")
    assert [l["event"] for l in _invoice_lines(caplog)] == [event]


def test_a_commit_that_landed_then_expired_is_settled_by_a_read_back(
        fake, monkeypatch, caplog):
    _seed(fake)
    _land_then(fake, monkeypatch, gexc.DeadlineExceeded("answer lost"))
    with caplog.at_level(logging.INFO):
        report, errors = _write_off(fake)
    assert errors == [] and report["write_off"] == _stored(fake)["write_offs"][0]
    (failed,) = [r for r in caplog.records
                 if r.getMessage() == "invoice write-off failed"]
    assert failed.json_fields == {"event": "unexpected", "invoice_id": INV,
                                  "error_type": "DeadlineExceeded",
                                  "commit_attempted": True}
    assert not failed.exc_info
    assert [l["event"] for l in _invoice_lines(caplog)] == ["invoice_written_off"]


def _failing_commit(fake, monkeypatch, *, read_back_fails: bool) -> None:
    """The invoice's commit fails WITHOUT applying; the read-back after it
    fails too when *read_back_fails*."""
    server = fake._fake_server
    real_get = server.batch_get_documents
    state = {"failed": False}

    def _hook(info):
        if any(path == f"invoices/{INV}" for _op, path in info.ops):
            state["failed"] = True
            raise gexc.DeadlineExceeded("commit lost")

    def _get(request, metadata=None, **kwargs):
        if read_back_fails and state["failed"]:
            raise RuntimeError("store down")
        return real_get(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", _get)
    fake.add_commit_hook(_hook)


def test_a_failed_commit_that_did_not_land_says_nothing_was_written(fake, monkeypatch, caplog):
    _seed(fake)
    _failing_commit(fake, monkeypatch, read_back_fails=False)
    with caplog.at_level(logging.INFO, logger="pallas.invoice"):
        report, errors = _write_off(fake)
    assert (report, errors) == (None, [invoice_model.WRITE_OFF_NOT_WRITTEN])
    assert "write_offs" not in _stored(fake)
    # Lot 3b: the counter commits with the invoice or not at all.
    assert fake.peek("counters/notes-credit-2031") is None
    assert _invoice_lines(caplog) == []


def test_an_unsettled_commit_is_uncertain_never_nothing_written(fake, monkeypatch):
    _seed(fake)
    _failing_commit(fake, monkeypatch, read_back_fails=True)
    report, errors = _write_off(fake)
    assert (report, errors) == (None, [concurrency.WRITE_OUTCOME_UNCERTAIN_ERROR])
    assert concurrency.is_outcome_uncertain(errors)
    assert "rien n'a été écrit" not in errors[0]


def test_a_read_that_fails_before_any_commit_says_nothing_was_written(fake, monkeypatch):
    _seed(fake)
    server = fake._fake_server

    def _boom(request, metadata=None, **kwargs):
        raise RuntimeError("store down")

    monkeypatch.setattr(server, "run_query", _boom)
    report, errors = _write_off(fake)
    assert (report, errors) == (None, [invoice_model.WRITE_OFF_NOT_WRITTEN])
    assert fake.commits == []


# ══════════════════════════════════════════════════════════════════════
# 7. The log lines carry no amount and no reason
# ══════════════════════════════════════════════════════════════════════


def test_no_line_carries_an_amount_or_the_reason(fake, caplog):
    secret = "Motif confidentiel de la radiation"
    _seed(fake)
    with caplog.at_level(logging.DEBUG):
        report, _ = _write_off(fake, reason=secret)
        _reverse(fake, report["write_off"]["id"], reason=secret + " (annulée)")
        _write_off(fake, expected_etag=ETAG, reason=secret)          # refused: stale
    event = report["write_off"]
    figures = {str(event["amount"]), str(event["gst_amount"]), str(event["qst_amount"]),
               "862,31", "862.31"}
    for record in caplog.records:
        text = logging.Formatter().format(record) + repr(getattr(record, "json_fields", {}))
        assert secret not in text, record.getMessage()
        for figure in figures:
            assert figure not in text, (figure, record.getMessage())


def test_the_helper_is_typed_no_extra_field_can_ride_along():
    import inspect
    import typing

    params = inspect.signature(logging_setup.log_invoice_write_off).parameters
    assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    assert set(params) == {"event", "invoice_id", "write_off_id", "tax_claimed",
                           "via", "status", "dossier_id", "reverses_id"}
    events = set(typing.get_args(logging_setup.InvoiceWriteOffEvent))
    assert events == {"invoice_written_off", "invoice_write_off_reversed"}
    assert events <= set(typing.get_args(logging_setup.InvoiceEvent))


# ══════════════════════════════════════════════════════════════════════
# 8. The guards elsewhere — the void, « Rouvrir », both registers
# ══════════════════════════════════════════════════════════════════════


def _written_off(fake, **over) -> dict:
    """A « payée » invoice settled by a radiation of its whole remainder."""
    return _seed(fake, status="payée", write_offs=[_radiation()], **over)


def _reopened(fake) -> dict:
    """An « en_retard » invoice still carrying a standing radiation of
    562,31 $ — its 300,00 $ payment reversed since: 300,00 $ owed."""
    return _seed(fake, write_offs=[_radiation(amount=56231, gst=2445, qst=4878)])


@pytest.mark.parametrize("state", ["payee", "reopened"])
def test_the_void_refuses_while_a_radiation_stands(fake, caplog, state):
    (_written_off if state == "payee" else _reopened)(fake)
    before = _stored(fake)
    fake.reset_logs()
    with caplog.at_level(logging.INFO, logger="pallas.invoice"):
        report, errors = invoice_model.void_invoice_report(INV, expected_etag=ETAG)
    assert report is None
    assert "Annuler la radiation" in errors[0]
    assert _stored(fake) == before and fake.commits == []
    assert _invoice_lines(caplog)[-1]["reason"] == "radiation_inscrite"


def test_the_void_names_an_unreadable_array_by_its_own_code(fake, caplog):
    _seed(fake, write_offs=None)
    with caplog.at_level(logging.INFO, logger="pallas.invoice"):
        report, errors = invoice_model.void_invoice_report(INV, expected_etag=ETAG)
    assert report is None and errors[0].startswith("Les radiations")
    assert _invoice_lines(caplog)[-1]["reason"] == "radiations_illisibles"


def test_a_cancelled_radiation_does_not_block_the_void(fake):
    _seed(fake, write_offs=[_RAD, _contre_passation(_RAD)], subtotal=0)
    report, errors = invoice_model.void_invoice_report(INV, expected_etag=ETAG)
    assert errors == [] and _stored(fake)["status"] == "annulée"


def test_rouvrir_and_annuler_close_while_a_radiation_stands():
    today = date(2031, 3, 10)
    settled = _invoice(status="payée", write_offs=[_radiation()])
    assert invoice_model.available_transitions(settled, today=today) == ()
    reopened = _invoice(write_offs=[_radiation(amount=56231, gst=2445, qst=4878)])
    assert invoice_model.available_transitions(reopened, today=today) == ()
    # The witnesses: cancelled, the doors are what they were.
    cancelled = [_RAD, _contre_passation(_RAD)]
    assert invoice_model.available_transitions(
        _invoice(status="payée", write_offs=cancelled), today=today) == ("envoyée",)
    assert invoice_model.available_transitions(
        _invoice(write_offs=cancelled), today=today) == ("annulée",)


def test_rouvrir_is_refused_naming_the_way_back(fake, caplog):
    _written_off(fake)
    before = _stored(fake)
    with caplog.at_level(logging.INFO, logger="pallas.invoice"):
        ok, error = invoice_model.update_status(INV, "envoyée", expected_etag=ETAG)
    assert ok is False and "Annuler la radiation" in error
    assert _stored(fake) == before
    assert _invoice_lines(caplog)[-1]["reason"] == "radiation_inscrite"


def test_a_malformed_array_refuses_a_payees_status_change_by_name(fake, caplog):
    _seed(fake, status="payée", write_offs={"radiation": 1})
    with caplog.at_level(logging.INFO, logger="pallas.invoice"):
        ok, error = invoice_model.update_status(INV, "envoyée", expected_etag=ETAG)
    assert ok is False
    assert error == ("Les radiations enregistrées sur cette facture sont "
                     "illisibles : son changement de statut ne peut pas être "
                     "jugé, et rien n'a été écrit. La facture doit d'abord être "
                     "réparée.")
    assert _invoice_lines(caplog)[-1]["reason"] == "montant_illisible"


def _seed_registers(fake) -> None:
    fake.seed("admin_accounts/ops1", {
        "id": "ops1", "name": "Opérations", "status": "actif",
        "account_type": "opérations", "ledger_balance": 0, "etag": "a0"})
    fake.seed("trust_accounts/acc1", {
        "id": "acc1", "name": "Général", "status": "actif",
        "account_type": "général", "book_balance": 200000, "bank_balance": 200000,
        "etag": "t0"})
    fake.seed("dossiers/dos-9", {
        "id": "dos-9", "file_number": "2031-009", "title": "Exemple c. Témoin",
        "client_ids": ["cli-9"], "clients": [{"id": "cli-9", "name": "Cliente Exemple"}],
        "trust_balance": 200000, "trust_balance_by_client": {"cli-9": 200000},
        "trust_cleared_by_client": {"cli-9": 200000}})


def _encaissement(amount: int) -> dict:
    return {"account_id": "ops1", "kind": "encaissement_facture",
            "invoice_id": INV, "amount": amount, "method": "virement",
            "counterparty": "Cliente Exemple",
            "date": _utc_midnight(date(2031, 3, 8)),
            "description": "", "reference": ""}


def _fee(amount: int = 10000) -> dict:
    return {"account_id": "acc1", "direction": "déboursé", "amount": amount,
            "purpose": "virement_honoraires", "method": "chèque",
            "counterparty": FEE_PAYEE, "dossier_id": "dos-9", "client_id": "cli-9",
            "date": _utc_midnight(date(2031, 3, 10)), "invoice_id": INV,
            "description": "", "reference": "2031"}


def _registers(fake) -> dict:
    return {"admin": fake.peek_collection("admin_transactions"),
            "trust": fake.peek_collection("trust_transactions"),
            "ops1": fake.peek("admin_accounts/ops1"),
            "acc1": fake.peek("trust_accounts/acc1"),
            "dossier": fake.peek("dossiers/dos-9"),
            "invoice": _stored(fake)}


@pytest.mark.parametrize("state", ["payee", "reopened"])
def test_an_encaissement_is_refused_while_a_radiation_stands(fake, state):
    """Before « facture_non_émise »: a radiation leaves the invoice « payée »,
    and « not issued » would send the lawyer the wrong way. On the reopened
    invoice — « en_retard », 300,00 $ owed — the payment would otherwise
    pass every other guard."""
    (_written_off if state == "payee" else _reopened)(fake)
    _seed_registers(fake)
    before = _registers(fake)
    report: dict = {}
    entry, errors = al.create_transaction(_encaissement(10000), _report_out=report)
    assert entry is None
    assert errors == [al._ABORT_MESSAGES["facture_radiee"]]
    assert "Annuler la radiation" in errors[0]
    assert report["reason"] == "facture_radiee"
    assert _registers(fake) == before


@pytest.mark.parametrize("state", ["payee", "reopened"])
def test_a_fee_payment_is_refused_while_a_radiation_stands(fake, state):
    """The composite (models/fee_payment) runs the trust stage first: its
    guard refuses, nothing is written anywhere — trust, administration or
    invoice."""
    (_written_off if state == "payee" else _reopened)(fake)
    _seed_registers(fake)
    before = _registers(fake)
    report: dict = {}
    result, errors = fee_payment.create_fee_payment(
        _fee(), admin_account_id="ops1", _report_out=report)
    assert result is None
    assert errors == [trust._abort_message("facture_radiee")]
    assert "Annuler la radiation" in errors[0]
    assert (report["reason"], report["side"]) == ("facture_radiee", "fidéicommis")
    assert _registers(fake) == before


def test_the_administration_stage_backs_the_trust_one_in_the_composite(fake, monkeypatch):
    """models/fee_payment calls BOTH stage functions: with the trust stage
    made blind to the radiation, the administration stage's own guard still
    refuses the combined fee payment — and nothing is written."""
    _reopened(fake)
    _seed_registers(fake)
    real_stage = trust._stage_create

    def _blind(txn, ctx, reads, now):
        invoice = {k: v for k, v in reads["invoice"].items() if k != "write_offs"}
        return real_stage(txn, ctx, {**reads, "invoice": invoice}, now)

    monkeypatch.setattr(trust, "_stage_create", _blind)
    before = _registers(fake)
    report: dict = {}
    result, errors = fee_payment.create_fee_payment(
        _fee(), admin_account_id="ops1", _report_out=report)
    assert result is None and "Annuler la radiation" in errors[0]
    assert (report["reason"], report["side"]) == ("facture_radiee", "administration")
    assert _registers(fake) == before


def test_without_a_radiation_the_same_payments_pass(fake):
    """The witness: on an overdue invoice with nothing written off, the same
    encaissement and fee payment go through."""
    _seed(fake)
    _seed_registers(fake)
    _entry, errors = al.create_transaction(_encaissement(10000))
    assert errors == []
    result, errors = fee_payment.create_fee_payment(_fee(), admin_account_id="ops1")
    assert errors == [] and result["invoice_after"]["amount_paid"] == 20000


# ══════════════════════════════════════════════════════════════════════
# 9. get_invoice_summary
# ══════════════════════════════════════════════════════════════════════


def test_the_dossier_summary_counts_what_was_written_off(fake):
    """A « payée » invoice counts total − written_off as paid; what stands
    written off is no longer outstanding."""
    rows = {
        "a": _invoice(id="a", status="payée", write_offs=[_radiation()]),
        "b": _invoice(id="b", status="payée", total=50000, amount_due=50000,
                      amount_paid=20000,
                      write_offs=[_radiation(amount=30000, claimed=False)]),
        "c": _invoice(id="c", total=40000, amount_due=40000,
                      write_offs=[_radiation(amount=15000, claimed=False)]),
        "d": _invoice(id="d", status="brouillon", total=10000, amount_due=10000),
        "e": _invoice(id="e", status="annulée", total=99999, amount_due=99999,
                      write_offs=[_RAD, _contre_passation(_RAD)]),
    }
    for doc_id, doc in rows.items():
        fake.seed(f"invoices/{doc_id}", doc)
    summary = invoice_model.get_invoice_summary("dos-9")
    assert summary == {
        "count": 4,
        "total_invoiced": TOTAL + 50000 + 40000 + 10000,
        "total_paid": 0 + 20000,
        "total_written_off": TOTAL + 30000 + 15000,
        "total_outstanding": (40000 - 15000) + 10000,
    }


# ══════════════════════════════════════════════════════════════════════
# 10. The connector reaches neither door — the disclosure sweep
# ══════════════════════════════════════════════════════════════════════


def test_the_disclosure_sweep_catches_a_call_to_either_door():
    """mcp/disclosure's « invoice_paid » promise — its text unchanged —
    forbids both doors; a call planted in a connector module, in each
    reference form, or in a service a connector module reaches, is caught."""
    from mcp import disclosure
    from tests.test_mcp_disclosure import services_reached, violations

    never = next(n for n in disclosure.NEVERS if n.key == "invoice_paid")
    assert {"write_off_remainder", "reverse_write_off"} <= set(never.forbidden)
    assert never.fr == "marquer une facture <strong>payée</strong> sans paiement inscrit"
    assert never.en == "mark an invoice payée by a status change"
    for door in ("write_off_remainder", "reverse_write_off"):
        for planted in (f"invoice_model.{door}('i', expected_etag='e')\n",
                        f"from models.invoice import {door}\n",
                        f"getattr(invoice_model, '{door}')('i')\n",
                        f"door = invoice_model.{door}\n"):
            found = violations({"mcp/probe.py": planted})
            assert f"mcp/probe.py: invoice_paid forbids {door}" in found, (planted, found)
        reached = services_reached({"mcp/probe.py": "from services import relay\n"},
                                   read={"relay": f"invoice_model.{door}('i')\n"})
        assert f"services/relay.py: invoice_paid forbids {door}" in violations(reached)


# ══════════════════════════════════════════════════════════════════════
# 11. Integrity check 13 (scripts/verify_admin_integrity)
# ══════════════════════════════════════════════════════════════════════


def _check13(*invoices) -> tuple[list, list]:
    return vai._check_write_offs([dict(inv) for inv in invoices])


def test_check_13_is_quiet_on_what_the_doors_write():
    """Each radiation its own credit note number, as the counter gives them
    (lot 3b) — the cancelled ones keeping theirs."""
    fourth, fifth = _radiation("rad-4"), _radiation("rad-5")
    assert _check13(
        _invoice(status="payée", write_offs=[_radiation()]),
        _invoice(status="payée", amount_paid=30000,
                 write_offs=[_radiation("rad-2", amount=56231, gst=2445, qst=4878)]),
        _invoice(write_offs=[_radiation("rad-3", amount=56231, gst=2445, qst=4878)]),
        _invoice(write_offs=[fourth, _contre_passation(fourth)]),
        _invoice(status="annulée", write_offs=[fifth, _contre_passation(fifth)]),
        _invoice(status="payée", amount_paid=TOTAL),
        _invoice(status="envoyée", amount_paid=30000),
        _invoice(status="payée"),                        # hand-set, nothing recorded
        _invoice(status="brouillon"),
    ) == ([], [])


def test_check_13_names_a_credit_note_number_borne_by_two_invoices():
    """Lot 3b: the judge sees one invoice at a time, so a number borne by
    the radiations of two invoices is check 13's to name — a cancelled
    radiation's number counts too: it is never reused."""
    first = _radiation(day=date(2031, 3, 5))
    again = _radiation(day=date(2031, 4, 2))           # « 2031-NC001 » too
    problems, notes = _check13(
        _invoice(id="a", invoice_number="2031-F040", status="payée", write_offs=[first]),
        _invoice(id="b", invoice_number="2031-F041",
                 write_offs=[again, _contre_passation(again, day=date(2031, 4, 3))]),
        _invoice(id="c", invoice_number="2031-F042", status="payée",
                 write_offs=[_radiation("rad-2")]))
    assert notes == []
    assert problems == [
        "note de crédit 2031-NC001: portée par les radiations de 2 factures "
        "(facture 2031-F040, facture 2031-F041) — un numéro de note de crédit "
        "ne sert qu'une fois, même annulé"]


def test_check_13_names_a_malformed_array():
    problems, _notes = _check13(_invoice(id="x", invoice_number="2031-F099",
                                         write_offs=_MALFORMED["orphan-contre-passation"]))
    (line,) = problems
    assert line.startswith("facture 2031-F099: radiations mal formées — événement n° 1")


def test_check_13_names_more_written_off_than_was_owed():
    problems, _ = _check13(_invoice(status="payée", amount_paid=30000,
                                    write_offs=[_radiation()]))
    assert any("le radié dépasse ce qui restait dû" in p for p in problems)


def test_check_13_names_a_radiation_standing_on_a_voided_invoice():
    problems, _ = _check13(_invoice(status="annulée", write_offs=[_radiation()]))
    assert any("radiation debout sur une facture « annulée »" in p for p in problems)


@pytest.mark.parametrize("over", [
    {"status": "en_retard", "write_offs": [_radiation()]},           # settled, not payée
    {"status": "payée", "write_offs": [_RAD, _contre_passation(_RAD)]},  # payée, owed
    {"status": "envoyée", "amount_paid": TOTAL},                      # settled, not payée
], ids=["settled-en-retard", "cancelled-payee", "paid-envoyee"])
def test_check_13_holds_payee_iff_settled_whenever_a_settlement_is_recorded(over):
    problems, _ = _check13(_invoice(**over))
    assert any("(I4)" in p for p in problems), problems


def test_a_hand_set_payee_with_a_partial_payment_is_a_note():
    """The one state the model leaves on purpose: the lawyer's « payée »,
    which payment_updates never touches."""
    problems, notes = _check13(_invoice(status="payée", amount_paid=30000))
    assert problems == []
    (note,) = notes
    assert "posée à la main" in note and "présument ce solde réglé" in note


def test_check_13_runs_with_the_script(fake, monkeypatch, capsys):
    _seed(fake, write_offs=_MALFORMED["cancelled-twice"])
    install(monkeypatch, vai, fake=fake)
    code = vai.main()
    out = capsys.readouterr().out
    assert code == 1
    assert "radiations mal formées — événement n° 3 : radiation déjà contre-passée" in out
    fake.seed(f"invoices/{INV}", _invoice(status="payée", write_offs=[_radiation()]))
    assert vai.main() == 0


# ══════════════════════════════════════════════════════════════════════
# 12. The one-shot repair scripts' predicate
# ══════════════════════════════════════════════════════════════════════


def test_a_repair_script_refuses_any_write_off_or_interest_charge():
    assert invoice_model.REPAIR_REFUSED_KEYS == ("write_offs", "interest_charges")
    assert invoice_model.repair_script_refusal(_invoice()) == ""
    assert invoice_model.repair_script_refusal(
        _invoice(write_offs=[], interest_charges=[])) == ""
    for over in ({"write_offs": [_radiation()]},
                 {"write_offs": [_RAD, _contre_passation(_RAD)]},   # cancelled too
                 {"write_offs": None}, {"write_offs": "x"}):
        assert "une radiation" in invoice_model.repair_script_refusal(_invoice(**over))
    assert "un avis d'intérêts" in invoice_model.repair_script_refusal(
        _invoice(interest_charges=[{"n": 1}]))
    assert invoice_model.repair_script_refusal(
        _invoice(write_offs=[_RAD], interest_charges=[{"n": 1}])).startswith(
        "porte une radiation et un avis d'intérêts")


# ══════════════════════════════════════════════════════════════════════
# 13. The credit note's number (lot 3b — the lawyer's decision 8)
# ══════════════════════════════════════════════════════════════════════

INV2 = "inv-radiation-2"
STALE = "eeeeeeee-1111-4222-8333-444444444444"


def _seed_second(fake) -> None:
    """A second overdue invoice of the same fictitious year."""
    fake.seed(f"invoices/{INV2}", _invoice(id=INV2, invoice_number="2031-F043"))


def _write_off_second(fake):
    return invoice_model.write_off_remainder(
        INV2, expected_etag=fake.peek(f"invoices/{INV2}")["etag"])


def _counter(fake, year: int):
    return fake.peek(f"counters/notes-credit-{year}")


def test_two_write_offs_in_a_year_take_nc001_and_nc002(fake):
    _seed(fake)
    _seed_second(fake)
    first, errors = _write_off(fake)
    assert errors == []
    second, errors = _write_off_second(fake)
    assert errors == []
    assert [first["write_off"]["credit_note_number"],
            second["write_off"]["credit_note_number"]] == ["2031-NC001", "2031-NC002"]
    assert [fake.peek(f"invoices/{i}")["write_offs"][0]["credit_note_number"]
            for i in (INV, INV2)] == ["2031-NC001", "2031-NC002"]
    assert _counter(fake, 2031)["seq"] == 2
    assert vai._check_write_offs([_stored(fake), fake.peek(f"invoices/{INV2}")]) == ([], [])


def test_a_cancelled_radiation_keeps_its_number_never_reused(fake):
    """« Annuler la radiation », then « Radier le solde » again: the second
    radiation takes the NEXT number; the first keeps its own for ever, its
    credit note « annulée » — derived, its radiation no longer standing."""
    _seed(fake)
    first, _ = _write_off(fake)
    _reverse(fake, first["write_off"]["id"])
    second, errors = _write_off(fake)
    assert errors == []
    stored = _stored(fake)
    assert [(e["kind"], e.get("credit_note_number")) for e in stored["write_offs"]] == [
        ("radiation", "2031-NC001"), ("contre_passation", None),
        ("radiation", "2031-NC002")]
    assert [e["credit_note_number"] for e in invoice_model.standing_write_offs(stored)] == [
        "2031-NC002"]
    assert _counter(fake, 2031)["seq"] == 2


def test_a_refused_write_off_consumes_no_number(fake):
    """Refused before the transaction (no version), inside it (stale, not
    overdue): the counter is never even written — the next write-off takes
    the year's first number."""
    _seed(fake, status="envoyée", due_date=_utc_midnight(date(2031, 4, 3)))
    assert _write_off(fake)[1][0].startswith("L'échéance de cette facture n'est pas passée")
    _seed(fake)
    assert _write_off(fake, expected_etag=STALE) == (None, [concurrency.STALE_ETAG_ERROR])
    assert invoice_model.write_off_remainder(INV, expected_etag=None) == (
        None, [invoice_model.WRITE_OFF_VERSION_REQUIRED])
    assert _counter(fake, 2031) is None
    report, errors = _write_off(fake)
    assert errors == [] and report["write_off"]["credit_note_number"] == "2031-NC001"


def test_the_year_turns_on_montreals_clock(fake, monkeypatch):
    """The Montréal year of the radiation's date, never the UTC year: at
    23:30 on 31 December in Montréal it is already 1 January in UTC."""
    _seed(fake)
    _seed_second(fake)
    freeze_deadlines(monkeypatch, datetime(2032, 1, 1, 4, 30, tzinfo=UTC))
    late, errors = _write_off(fake)
    assert errors == []
    freeze_deadlines(monkeypatch, datetime(2032, 1, 1, 5, 30, tzinfo=UTC))
    early, errors = _write_off_second(fake)
    assert errors == []
    assert (late["write_off"]["date"], late["write_off"]["credit_note_number"]) == (
        _utc_midnight(date(2031, 12, 31)), "2031-NC001")
    assert (early["write_off"]["date"], early["write_off"]["credit_note_number"]) == (
        _utc_midnight(date(2032, 1, 1)), "2032-NC001")
    assert (_counter(fake, 2031)["seq"], _counter(fake, 2032)["seq"]) == (1, 1)


def test_the_series_rolls_past_999(fake):
    fake.seed("counters/notes-credit-2031", {"seq": 999})
    _seed(fake)
    report, errors = _write_off(fake)
    assert errors == [] and report["write_off"]["credit_note_number"] == "2031-NC1000"
    assert invoice_model.standing_write_offs(_stored(fake)) == [report["write_off"]]
    assert _counter(fake, 2031)["seq"] == 1000


def test_an_unreadable_counter_writes_nothing_never_a_guessed_number(fake, caplog):
    fake.seed("counters/notes-credit-2031", {"seq": "dix"})
    _seed(fake)
    before = _stored(fake)
    with caplog.at_level(logging.INFO):
        report, errors = _write_off(fake)
    assert (report, errors) == (None, [invoice_model.WRITE_OFF_NOT_WRITTEN])
    assert _stored(fake) == before
    assert _counter(fake, 2031) == {"seq": "dix"}
    (failed,) = [r for r in caplog.records
                 if r.getMessage() == "invoice write-off failed"]
    assert failed.json_fields["commit_attempted"] is False
