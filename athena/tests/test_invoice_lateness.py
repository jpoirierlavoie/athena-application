"""Lateness is a fact, with ONE authority — lot 2 of « Factures : retard,
radiation, intérêts » (the lawyer's decisions of 2026-11-11; invariant I6).

``models/invoice.is_overdue`` decides it: an issued invoice (envoyée or
en_retard) with a balance owing, whose due date, prorogued on the CIVIL
calendar (a Saturday, a Sunday or a public holiday rolls to the next business
day), fell before today in Montréal. ``days_overdue`` and
``first_overdue_day`` count and date it; ``available_transitions`` offers
« en_retard » only while overdue and « envoyée » back — « Retirer le retard »
— only while not; ``update_status_report`` judges with ``today_mtl`` INSIDE
its transaction and names the first late day when it refuses; the web list
and sheet show what the routes computed.

Every « today » is FROZEN at the one seam (``tests/_frozen_clock`` over
``utils.deadlines``) on fixed dates of a fictitious year — never a widened
offset — and the holidays come from ``utils.deadlines``' own calendar.
Through the real model, routes and templates, on the shared fake Firestore
(``tests/_fake_firestore``); every amount is invented.
"""

import itertools
import logging
import os
import pathlib
import re
import sys
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import admin_ledger, expense, time_entry, trust  # noqa: F401
    from models import invoice as invoice_model
    import routes.admin_ledger as admin_ledger_routes
    import routes.dossiers as dossiers_routes
    import routes.invoices as invoices_routes
    import routes.parties as parties_routes

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tests._frozen_clock import freeze_deadlines  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils import deadlines  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc
INV = "inv-retard-1"
ETAG = "cccccccc-1111-4222-8333-444444444444"


def _utc_midnight(day: date) -> datetime:
    """A date-only field as the models store it: midnight UTC."""
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _invoice(status: str = "envoyée", due: date | None = date(2031, 3, 3),
             **over) -> dict:
    """An issued invoice of a fictitious year, its amounts invented."""
    doc = {
        "id": INV, "invoice_number": "2031-F017", "status": status,
        "dossier_id": "dos-7", "dossier_file_number": "2031-007",
        "dossier_title": "Exemple c. Témoin", "client_id": "cli-7",
        "client_name": "Cliente Exemple",
        "date": datetime(2031, 2, 3, tzinfo=UTC),
        "due_date": _utc_midnight(due) if due else None,
        "subtotal": 75000, "gst_rate": 500, "gst_amount": 3750,
        "qst_rate": 9975, "qst_amount": 7481, "total": 86231,
        "retainer_applied": 0, "amount_due": 86231, "amount_paid": 0,
        "paid_date": None, "etag": ETAG,
    }
    doc.update(over)
    return doc


# ══════════════════════════════════════════════════════════════════════
# 1. The rule — pure
# ══════════════════════════════════════════════════════════════════════


def test_the_lateness_calendar_is_the_civil_one():
    assert invoice_model.LATENESS_REGIME == deadlines.CIVIL


def test_a_saturday_due_date_is_payable_until_monday_and_late_on_tuesday():
    saturday = date(2031, 3, 1)
    assert saturday.weekday() == 5
    inv = _invoice(due=saturday)
    assert invoice_model.first_overdue_day(inv) == date(2031, 3, 4)
    for today, late, days in ((date(2031, 3, 1), False, 0),
                              (date(2031, 3, 2), False, 0),
                              (date(2031, 3, 3), False, 0),   # Monday: payable
                              (date(2031, 3, 4), True, 1),    # Tuesday: late
                              (date(2031, 3, 10), True, 7)):
        assert invoice_model.is_overdue(inv, today=today) is late, today
        assert invoice_model.days_overdue(inv, today=today) == days, today


def test_a_public_holiday_rolls_to_the_next_business_day():
    """Good Friday 2031, taken from utils.deadlines' own calendar, is followed
    by a weekend and Easter Monday — four non-business days in a row: due on
    the Friday, the invoice is payable until Tuesday and late on Wednesday."""
    holidays = deadlines.get_quebec_holidays(2031, regime=deadlines.CIVIL)
    good_friday = date(2031, 4, 11)
    assert good_friday in holidays and good_friday.weekday() == 4
    assert good_friday + timedelta(days=3) in holidays      # Easter Monday
    inv = _invoice(due=good_friday)
    assert invoice_model.first_overdue_day(inv) == date(2031, 4, 16)
    assert not invoice_model.is_overdue(inv, today=date(2031, 4, 15))
    assert invoice_model.is_overdue(inv, today=date(2031, 4, 16))
    assert invoice_model.days_overdue(inv, today=date(2031, 4, 16)) == 1


def test_a_weekday_holiday_rolls_one_day():
    """La Fête nationale 2031 (a Tuesday, from the calendar): payable until
    Wednesday, late on Thursday."""
    fete = date(2031, 6, 24)
    assert fete in deadlines.get_quebec_holidays(2031, regime=deadlines.CIVIL)
    inv = _invoice(due=fete)
    assert not invoice_model.is_overdue(inv, today=date(2031, 6, 25))
    assert invoice_model.is_overdue(inv, today=date(2031, 6, 26))


def test_art_82_days_are_business_days_for_an_invoice():
    """26 December 2031 is a Friday: a business day on the CIVIL calendar, a
    holiday only in civil procedure (art. 82 C.p.c.). Due that day, the
    invoice is late on Saturday the 27th — on the procedural calendar it
    would be payable until Monday the 29th."""
    boxing_day = date(2031, 12, 26)
    assert boxing_day.weekday() == 4
    assert deadlines.effective_due(boxing_day) == date(2031, 12, 29)
    inv = _invoice(due=boxing_day)
    assert invoice_model.is_overdue(inv, today=date(2031, 12, 27))
    assert invoice_model.days_overdue(inv, today=date(2031, 12, 29)) == 3


def test_the_evening_band_is_still_the_due_day_in_montreal(monkeypatch):
    """21:30 in Montréal on Monday 3 March 2031 (EST) is already the 4th in
    UTC: the invoice due on the 3rd is NOT late until Montréal's midnight —
    judged on today_mtl, never on a UTC day. Pinned at the minute before
    Montréal's midnight and at the midnight itself: whatever the sweep's
    hour (09:00 since 2026-11-12), lateness begins there."""
    inv = _invoice(due=date(2031, 3, 3))
    freeze_deadlines(monkeypatch, datetime(2031, 3, 4, 2, 30, tzinfo=UTC))
    assert deadlines.today_utc() == date(2031, 3, 4)
    assert deadlines.today_mtl() == date(2031, 3, 3)
    assert not invoice_model.is_overdue(inv, today=deadlines.today_mtl())
    freeze_deadlines(monkeypatch, datetime(2031, 3, 4, 4, 59, tzinfo=UTC))
    assert deadlines.today_mtl() == date(2031, 3, 3)          # 23:59 EST
    assert not invoice_model.is_overdue(inv, today=deadlines.today_mtl())
    freeze_deadlines(monkeypatch, datetime(2031, 3, 4, 5, 0, tzinfo=UTC))
    assert deadlines.today_mtl() == date(2031, 3, 4)          # 00:00 EST
    assert invoice_model.is_overdue(inv, today=deadlines.today_mtl())


@pytest.mark.parametrize("paid", [86231, 90000], ids=["settled", "overpaid"])
def test_nothing_owing_is_never_late(paid):
    inv = _invoice(due=date(2031, 3, 3), amount_paid=paid)
    assert not invoice_model.is_overdue(inv, today=date(2031, 9, 1))
    assert invoice_model.days_overdue(inv, today=date(2031, 9, 1)) == 0
    # A provision that covered everything: nothing was ever owed.
    covered = _invoice(due=date(2031, 3, 3), amount_due=0)
    assert not invoice_model.is_overdue(covered, today=date(2031, 9, 1))


@pytest.mark.parametrize("status", ["brouillon", "payée", "annulée", "", "inconnu"])
def test_no_status_but_an_issued_one_is_ever_late(status):
    inv = _invoice(status=status, due=date(2031, 3, 3))
    assert not invoice_model.is_overdue(inv, today=date(2031, 9, 1))
    assert invoice_model.days_overdue(inv, today=date(2031, 9, 1)) == 0


@pytest.mark.parametrize("status", ["envoyée", "en_retard"])
def test_both_issued_statuses_are_late_past_their_due_date(status):
    inv = _invoice(status=status, due=date(2031, 3, 3))
    assert invoice_model.is_overdue(inv, today=date(2031, 3, 4))


@pytest.mark.parametrize("due", [None, "2031-03-03", 20310303],
                         ids=["none", "a-string", "a-number"])
def test_no_readable_due_date_is_never_late(due):
    inv = _invoice(due=None)
    inv["due_date"] = due
    assert not invoice_model.is_overdue(inv, today=date(2031, 9, 1))
    assert invoice_model.first_overdue_day(inv) is None
    inv.pop("due_date")
    assert not invoice_model.is_overdue(inv, today=date(2031, 9, 1))


@pytest.mark.parametrize("today", [None, datetime(2031, 3, 4, tzinfo=UTC),
                                   "2031-03-04"],
                         ids=["none", "an-instant", "a-string"])
def test_today_is_a_montreal_day_named_by_the_caller(today):
    """The rule never reads a clock of its own: None would let
    deadlines.is_past_due read the wall clock behind the caller's back."""
    inv = _invoice(due=date(2031, 3, 3))
    for call in (lambda: invoice_model.is_overdue(inv, today=today),
                 lambda: invoice_model.days_overdue(inv, today=today),
                 lambda: invoice_model.available_transitions(inv, today=today),
                 lambda: invoice_model.overdue_among([inv], today=today)):
        with pytest.raises(TypeError):
            call()
    # Every status, not only the issued ones.
    with pytest.raises(TypeError):
        invoice_model.available_transitions(_invoice(status="payée"), today=today)


def test_today_is_a_required_keyword():
    inv = _invoice()
    with pytest.raises(TypeError):
        invoice_model.available_transitions(inv)              # noqa
    with pytest.raises(TypeError):
        invoice_model.is_overdue(inv)                          # noqa


def test_the_sweep_order_is_the_longest_late_first():
    """overdue_among — the sweep's and the preview's one selection."""
    a = _invoice(due=date(2031, 3, 3), id="b-late")
    b = _invoice(due=date(2031, 2, 3), id="z-latest")
    c = _invoice(due=date(2031, 3, 3), id="a-late")
    on_time = _invoice(due=date(2031, 4, 3), id="on-time")
    paid = _invoice(due=date(2031, 2, 3), id="paid", amount_paid=86231)
    late, unjudged = invoice_model.overdue_among(
        [a, on_time, b, paid, c], today=date(2031, 3, 10))
    assert [inv["id"] for inv in late] == ["z-latest", "a-late", "b-late"]
    assert unjudged == []


@pytest.mark.parametrize("field,value", [
    ("amount_due", "1 234,56"), ("amount_paid", "150,00 $"),
], ids=["amount_due", "amount_paid"])
def test_an_unreadable_amount_is_named_never_judged(caplog, field, value):
    """Named by its id, the exception's class and the amount's FIELD — never
    by a traceback, whose last line quoted the stored value (int()'s own
    error) into Cloud Logging at every run (review of lot 2: no log carries
    an amount, CLAUDE.md « Do not log PII »)."""
    good = _invoice(due=date(2031, 3, 3), id="good")
    bad = _invoice(due=date(2031, 3, 3), id="bad", **{field: value})
    with caplog.at_level(logging.ERROR):
        late, unjudged = invoice_model.overdue_among(
            [bad, good], today=date(2031, 3, 10))
    assert [inv["id"] for inv in late] == ["good"] and unjudged == ["bad"]
    (record,) = [r for r in caplog.records
                 if r.getMessage() == "invoice lateness unreadable"]
    assert record.json_fields == {"event": "unexpected", "invoice_id": "bad",
                                  "error_type": "ValueError", "field": field}
    assert not record.exc_info and record.exc_text is None
    assert value not in logging.Formatter().format(record)


def test_an_unreadable_write_off_array_is_named_never_judged(caplog):
    """Lot 3a: the balance reads the write-off events too — a malformed
    array is named ``write_offs`` by the same line, never judged, never
    quoted (its amount is no log field)."""
    good = _invoice(due=date(2031, 3, 3), id="good")
    bad = _invoice(due=date(2031, 3, 3), id="bad",
                   write_offs=[{**_RAD, "amount": "1 234,56"}])
    with caplog.at_level(logging.ERROR):
        late, unjudged = invoice_model.overdue_among(
            [bad, good], today=date(2031, 3, 10))
    assert [inv["id"] for inv in late] == ["good"] and unjudged == ["bad"]
    (record,) = [r for r in caplog.records
                 if r.getMessage() == "invoice lateness unreadable"]
    assert record.json_fields == {"event": "unexpected", "invoice_id": "bad",
                                  "error_type": "WriteOffsUnreadable",
                                  "field": "write_offs"}
    assert "1 234,56" not in logging.Formatter().format(record)


# Every kind of value a Firestore field can hand back — a NaN and an infinite
# double included (int() raises ValueError and OverflowError on them).
_STORED_AMOUNTS = [0, 57312, -1, "57312", " 12 ", 573.12, True, None, "",
                   "1 234,56", "573.12", "150,00 $", float("inf"),
                   float("nan"), b"12", b"x", [], {},
                   datetime(2031, 3, 3, tzinfo=UTC)]


_ABSENT = object()


def _utc_event(day: int, hour: int = 0) -> datetime:
    return datetime(2031, 3, day, hour, tzinfo=UTC)


_RAD = {"id": "rad-1", "kind": "radiation", "date": _utc_event(5),
        "amount": 1000, "gst_amount": 43, "qst_amount": 87,
        "tax_claimed": True, "reason": "", "reverses_id": ""}
_CP = {**_RAD, "id": "cp-1", "kind": "contre_passation", "date": _utc_event(6),
       "reverses_id": "rad-1"}

#: Every shape ``write_offs`` can hold (lot 3a): absent (read as []), the
#: empty list, None, a value that is no list, well-formed events — standing,
#: cancelled — and each kind of defect, an orphan contre_passation among them.
_WRITE_OFF_SHAPES = {
    "absent": _ABSENT,
    "empty": [],
    "none": None,
    "a-record": {"radiation": 1000},
    "a-text": "radiation",
    "standing": [_RAD],
    "cancelled": [_RAD, _CP],
    "text-amount": [{**_RAD, "amount": "10,00"}],
    "bool-amount": [{**_RAD, "amount": True}],
    "unknown-kind": [{**_RAD, "kind": "remise"}],
    "orphan-contre-passation": [_CP],
    "cancelled-twice": [_RAD, _CP, {**_CP, "id": "cp-2"}],
    "time-of-day": [{**_RAD, "date": _utc_event(5, 14)}],
}


def _first_unreadable(inv: dict) -> object:
    """The first field of balance_of's reading order whose prefix of the
    invoice balance_of cannot read — None when it reads them all."""
    order = invoice_model._BALANCE_FIELDS
    for i, name in enumerate(order):
        prefix = {k: inv[k] for k in order[:i + 1] if k in inv}
        try:
            invoice_model.balance_of(prefix)
        except Exception:
            return name
    return None


def test_the_unreadable_field_is_exactly_what_balance_of_cannot_read():
    """unreadable_balance_field names a field exactly when balance_of raises
    — the FIRST it reads, amount_due before amount_paid before (lot 3a) the
    write-off events — on every pair of stored values and every shape of the
    array, an absent key included (balance_of reads amounts as 0, the array
    as []). Extended deliberately by lot 3a, never loosened: the pairs of
    stored values are the same, each now crossed with every shape."""
    assert invoice_model._BALANCE_FIELDS == ("amount_due", "amount_paid",
                                             "write_offs")
    values = [_ABSENT, *_STORED_AMOUNTS]
    for due, paid in itertools.product(values, repeat=2):
        for shape in _WRITE_OFF_SHAPES.values():
            inv = {key: value for key, value in (("amount_due", due),
                                                 ("amount_paid", paid),
                                                 ("write_offs", shape))
                   if value is not _ABSENT}
            try:
                invoice_model.balance_of(inv)
            except Exception:
                first = _first_unreadable(inv)
                assert first is not None, inv
                assert invoice_model.unreadable_balance_field(inv) == first, inv
            else:
                assert invoice_model.unreadable_balance_field(inv) is None, inv


# ══════════════════════════════════════════════════════════════════════
# 2. The transitions, both ways — pure
# ══════════════════════════════════════════════════════════════════════


def test_marquer_en_retard_is_offered_only_while_overdue():
    inv = _invoice(status="envoyée", due=date(2031, 3, 1))     # a Saturday
    assert invoice_model.available_transitions(inv, today=date(2031, 3, 3)) == (
        "annulée",)
    assert invoice_model.available_transitions(inv, today=date(2031, 3, 4)) == (
        "en_retard", "annulée")
    settled = _invoice(status="envoyée", due=date(2031, 3, 1), amount_paid=86231)
    assert "en_retard" not in invoice_model.available_transitions(
        settled, today=date(2031, 9, 1))


def test_retirer_le_retard_is_offered_only_while_not_overdue():
    inv = _invoice(status="en_retard", due=date(2031, 3, 1))
    # Marked before the due date: the correction is offered.
    assert invoice_model.available_transitions(inv, today=date(2031, 3, 3)) == (
        "envoyée", "annulée")
    # Overdue: lateness is a fact.
    assert invoice_model.available_transitions(inv, today=date(2031, 3, 4)) == (
        "annulée",)


def test_the_status_door_names_a_field_exactly_when_the_transitions_cannot_read():
    """unreadable_transition_field — what update_status_report refuses by
    name — names a field exactly when available_transitions, the authority
    the door then consults, cannot read the invoice: on every status and
    every pair of stored amounts, an absent key included. An issued
    invoice's lateness reads its balance (the first field balance_of cannot
    read — strictly: a None or empty amount_paid is unreadable there); every
    status reads whether a payment is recorded (amount_paid — absent, None
    or empty read as none, where the balance read has not already refused
    it: third review of lot 2). So nothing falls into the door's
    generic failure, whose traceback quoted the stored value, and nothing
    more is refused: a brouillon's amount_due, read by no transition, locks
    none (second review of lot 2).

    Extended deliberately by lot 3a, never loosened: every pair is now
    crossed with every shape of ``write_offs``. An issued invoice's balance
    reads the array (``unreadable_balance_field``); a payée's transitions
    read it after its amount_paid — whether a radiation stands closes
    « Rouvrir »; a brouillon's and an annulée's never do, so a malformed
    array locks none of theirs."""
    values = [_ABSENT, *_STORED_AMOUNTS]
    for status in (*invoice_model.VALID_STATUSES, ""):
        for due, paid in itertools.product(values, repeat=2):
            for shape in _WRITE_OFF_SHAPES.values():
                inv = _invoice(status=status)
                for key, value in (("amount_due", due), ("amount_paid", paid),
                                   ("write_offs", shape)):
                    if value is _ABSENT:
                        inv.pop(key, None)
                    else:
                        inv[key] = value
                field = invoice_model.unreadable_transition_field(inv)
                try:
                    invoice_model.available_transitions(inv, today=date(2031, 3, 10))
                except Exception:
                    assert field is not None, inv
                    if status in ("envoyée", "en_retard"):
                        assert field == invoice_model.unreadable_balance_field(inv), inv
                    elif status == "payée":
                        try:
                            invoice_model._payment_recorded(inv)
                        except Exception:
                            assert field == "amount_paid", inv
                        else:
                            assert field == "write_offs", inv
                    else:
                        assert field == "amount_paid", inv
                else:
                    assert field is None, inv


# ══════════════════════════════════════════════════════════════════════
# 3. The status write — on the fake store
# ══════════════════════════════════════════════════════════════════════


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    return install(monkeypatch, *_fake_modules())


def _at(monkeypatch, instant: datetime) -> None:
    freeze_deadlines(monkeypatch, instant)


def test_a_premature_en_retard_is_refused_naming_the_first_late_day(
        fake, monkeypatch, caplog):
    fake.seed(f"invoices/{INV}", _invoice(due=date(2031, 3, 1)))
    _at(monkeypatch, datetime(2031, 3, 3, 15, tzinfo=UTC))     # Monday
    before = fake.peek(f"invoices/{INV}")
    fake.reset_logs()
    with caplog.at_level(logging.INFO, logger="pallas.invoice"):
        written, errors = invoice_model.update_status_report(
            INV, "en_retard", expected_etag=ETAG)
    assert written is None
    (error,) = errors
    assert "n'est pas passée" in error
    assert "à compter du 2031-03-04" in error
    assert fake.peek(f"invoices/{INV}") == before and fake.commits == []
    (line,) = [r.json_fields for r in caplog.records if r.name == "pallas.invoice"]
    assert (line["event"], line["reason"]) == ("invoice_refused", "pas_en_retard")


@pytest.mark.parametrize("over,needle", [
    ({"due": None}, "pas de date d'échéance"),
    ({"amount_paid": 86231}, "plus de solde dû"),
], ids=["no-due-date", "nothing-owing"])
def test_en_retard_without_a_debt_or_a_date_is_refused(fake, monkeypatch, over, needle):
    fake.seed(f"invoices/{INV}", _invoice(**over))
    _at(monkeypatch, datetime(2031, 9, 1, 15, tzinfo=UTC))
    before = fake.peek(f"invoices/{INV}")
    fake.reset_logs()
    _written, errors = invoice_model.update_status_report(INV, "en_retard")
    assert needle in errors[0]
    assert fake.peek(f"invoices/{INV}") == before and fake.commits == []


def test_an_overdue_invoice_is_marked_and_the_line_says_why(fake, monkeypatch, caplog):
    fake.seed(f"invoices/{INV}", _invoice(due=date(2031, 3, 1)))
    _at(monkeypatch, datetime(2031, 3, 4, 15, tzinfo=UTC))     # Tuesday
    with caplog.at_level(logging.INFO, logger="pallas.invoice"):
        written, errors = invoice_model.update_status_report(
            INV, "en_retard", expected_etag=ETAG)
    assert errors == []
    stored = fake.peek(f"invoices/{INV}")
    assert stored["status"] == "en_retard" == written["status"]
    assert stored["etag"] != ETAG and stored["updated_via"] == "script"
    (line,) = [r.json_fields for r in caplog.records if r.name == "pallas.invoice"]
    assert line["event"] == "invoice_status_changed"
    assert (line["from_status"], line["to_status"]) == ("envoyée", "en_retard")
    assert (line["cause"], line["via"]) == ("manuel", "script")


def test_retirer_le_retard_on_an_overdue_invoice_is_refused(fake, monkeypatch, caplog):
    fake.seed(f"invoices/{INV}", _invoice(status="en_retard", due=date(2031, 3, 1)))
    _at(monkeypatch, datetime(2031, 3, 6, 15, tzinfo=UTC))
    before = fake.peek(f"invoices/{INV}")
    fake.reset_logs()
    with caplog.at_level(logging.INFO, logger="pallas.invoice"):
        ok, error = invoice_model.update_status(INV, "envoyée", expected_etag=ETAG)
    assert ok is False
    assert "en retard depuis le 2031-03-04" in error
    assert "Retirer le retard" in error
    assert fake.peek(f"invoices/{INV}") == before and fake.commits == []
    (line,) = [r.json_fields for r in caplog.records if r.name == "pallas.invoice"]
    assert line["reason"] == "retard_avere"


def test_retirer_le_retard_corrects_a_mark_made_before_the_due_date(fake, monkeypatch):
    fake.seed(f"invoices/{INV}", _invoice(status="en_retard", due=date(2031, 3, 1)))
    _at(monkeypatch, datetime(2031, 3, 3, 15, tzinfo=UTC))     # still payable
    ok, error = invoice_model.update_status(INV, "envoyée", expected_etag=ETAG)
    assert (ok, error) == (True, "")
    assert fake.peek(f"invoices/{INV}")["status"] == "envoyée"


def test_the_write_judges_on_montreals_day_in_the_evening_band(fake, monkeypatch):
    """At 21:30 in Montréal on the due day (02:30 UTC the next day), the
    status write refuses « en_retard »; from Montréal's midnight — 05:00
    UTC, hours before the sweep's 09:00 run — it takes it."""
    fake.seed(f"invoices/{INV}", _invoice(due=date(2031, 3, 3)))
    _at(monkeypatch, datetime(2031, 3, 4, 2, 30, tzinfo=UTC))
    ok, error = invoice_model.update_status(INV, "en_retard")
    assert ok is False and "à compter du 2031-03-04" in error
    _at(monkeypatch, datetime(2031, 3, 4, 5, 0, tzinfo=UTC))
    assert invoice_model.update_status(INV, "en_retard") == (True, "")


def test_lateness_is_judged_on_the_invoice_read_inside_the_transaction(
        fake, monkeypatch):
    """A write that lands between the transaction's read and its commit (the
    due date corrected later) aborts it; the re-run judges the NEW invoice
    and refuses — never the version read first."""
    fake.seed(f"invoices/{INV}", _invoice(due=date(2031, 3, 1)))
    _at(monkeypatch, datetime(2031, 3, 10, 15, tzinfo=UTC))
    fired = []

    def _due_date_moves(info):
        if not fired and any(p == f"invoices/{INV}" for _k, p in info.ops):
            fired.append(1)
            fake.external_write(f"invoices/{INV}", {
                **fake.peek(f"invoices/{INV}"),
                "due_date": _utc_midnight(date(2031, 4, 1)), "etag": ETAG})

    remove = fake.add_commit_hook(_due_date_moves)
    try:
        ok, error = invoice_model.update_status(INV, "en_retard")
    finally:
        remove()
    assert fired and ok is False
    assert "à compter du 2031-04-02" in error
    assert fake.peek(f"invoices/{INV}")["status"] == "envoyée"


@pytest.mark.parametrize("status,target,field,value,label", [
    ("envoyée", "en_retard", "amount_due", "1 234,56", "montant dû"),
    ("envoyée", "en_retard", "amount_paid", "150,00 $", "montant payé"),
    ("en_retard", "envoyée", "amount_due", None, "montant dû"),
], ids=["mark-due", "mark-paid", "unmark-none"])
def test_an_unreadable_amount_refuses_the_status_write_by_name(
        fake, monkeypatch, caplog, status, target, field, value, label):
    """Lateness reads an issued invoice's balance inside the status
    transaction: a stored amount it cannot read is REFUSED, the field named
    in French (reason montant_illisible) — no longer the generic « Erreur.
    Veuillez réessayer. », whose ERROR traceback quoted the stored value
    (int()'s own error; review of lot 2). Nothing is written."""
    fake.seed(f"invoices/{INV}", _invoice(status=status, due=date(2031, 3, 1),
                                          **{field: value}))
    _at(monkeypatch, datetime(2031, 3, 10, 15, tzinfo=UTC))
    before = fake.peek(f"invoices/{INV}")
    fake.reset_logs()
    with caplog.at_level(logging.INFO):
        written, errors = invoice_model.update_status_report(
            INV, target, expected_etag=ETAG)
    assert written is None
    (error,) = errors
    assert error.startswith(f"Le {label} enregistré sur cette facture est "
                            "illisible : son retard ne peut pas être jugé")
    assert fake.peek(f"invoices/{INV}") == before and fake.commits == []
    (line,) = [r.json_fields for r in caplog.records if r.name == "pallas.invoice"]
    assert (line["event"], line["invoice_id"], line["reason"]) == (
        "invoice_refused", INV, "montant_illisible")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    if value is not None:
        assert not [r for r in caplog.records
                    if value in logging.Formatter().format(r)]


@pytest.mark.parametrize("status,value", [
    ("brouillon", "150,00 $"), ("payée", "1 234,56"),
    ("brouillon", [57312]), ("payée", float("inf")),
], ids=["brouillon-text", "payee-text", "brouillon-list", "payee-infinite"])
def test_an_unreadable_amount_paid_refuses_any_status_write_by_name(
        fake, monkeypatch, caplog, status, value):
    """Every transition reads whether a payment is recorded — a brouillon's
    and a payée's too, whose balance lateness never reads
    (available_transitions: amount_paid). An amount_paid it could not read
    fell into the generic « Erreur. Veuillez réessayer. », its ERROR
    traceback quoting the stored value, though the first review's fix said
    no legacy amount locked another transition: only amount_due's half of
    that held (second review of lot 2; the read predates the lot). Refused
    by NAME now, as on an issued invoice — reason montant_illisible, no
    traceback, nothing written."""
    fake.seed(f"invoices/{INV}", _invoice(status=status, amount_paid=value))
    _at(monkeypatch, datetime(2031, 3, 10, 15, tzinfo=UTC))
    before = fake.peek(f"invoices/{INV}")
    fake.reset_logs()
    with caplog.at_level(logging.INFO):
        written, errors = invoice_model.update_status_report(
            INV, "envoyée", expected_etag=ETAG)
    assert written is None
    assert errors == [
        "Le montant payé enregistré sur cette facture est illisible : son "
        "changement de statut ne peut pas être jugé, et rien n'a été écrit. "
        "La facture doit d'abord être réparée."]
    assert fake.peek(f"invoices/{INV}") == before and fake.commits == []
    (line,) = [r.json_fields for r in caplog.records if r.name == "pallas.invoice"]
    assert (line["event"], line["invoice_id"], line["reason"]) == (
        "invoice_refused", INV, "montant_illisible")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    if isinstance(value, str):
        assert not [r for r in caplog.records
                    if value in logging.Formatter().format(r)]


@pytest.mark.parametrize("paid", ["absent", None, "", 0],
                         ids=["absent", "none", "empty", "zero"])
def test_a_hand_set_payee_reopens_whatever_reads_as_no_payment(
        fake, monkeypatch, paid):
    """The by-name refusal reads a payée's amount_paid as
    available_transitions does: absent, None, empty or zero is NO recorded
    payment — what a « payée » set by hand before 2026-08-17 may carry —, so
    « Rouvrir la facture » still reopens it (second review of lot 2: refuse
    what cannot be read). Reopened, it is « envoyée » with that same amount,
    and there lateness reads it through balance_of, strictly: a None or
    empty one is refused by name
    (test_an_issued_invoices_none_or_empty_amount_paid_is_refused_by_name —
    third review of lot 2: this docstring said nothing that always could
    was locked)."""
    doc = _invoice(status="payée")
    if paid == "absent":
        del doc["amount_paid"]
    else:
        doc["amount_paid"] = paid
    fake.seed(f"invoices/{INV}", doc)
    _at(monkeypatch, datetime(2031, 3, 10, 15, tzinfo=UTC))
    assert invoice_model.update_status(INV, "envoyée", expected_etag=ETAG) == (
        True, "")
    assert fake.peek(f"invoices/{INV}")["status"] == "envoyée"


def test_a_brouillon_is_sent_whatever_its_amount_due_reads(fake, monkeypatch):
    """RENAMED and narrowed by the second review of lot 2 (it was « …whatever
    its amounts read », which only amount_due bore out). No transition reads
    a brouillon's amount_due — lateness reads an issued invoice's balance —,
    so an unreadable one locks nothing: « brouillon → envoyée » proceeds,
    before lot 2 as after. Its amount_paid is another matter: every
    transition reads it, and an unreadable one is refused by name
    (test_an_unreadable_amount_paid_refuses_any_status_write_by_name)."""
    fake.seed(f"invoices/{INV}", _invoice(status="brouillon",
                                          amount_due="1 234,56"))
    _at(monkeypatch, datetime(2031, 3, 10, 15, tzinfo=UTC))
    assert invoice_model.update_status(INV, "envoyée", expected_etag=ETAG) == (
        True, "")
    assert fake.peek(f"invoices/{INV}")["status"] == "envoyée"


@pytest.mark.parametrize("status,target,due", [
    ("envoyée", "en_retard", date(2031, 3, 1)),     # overdue: the mark
    ("en_retard", "envoyée", date(2031, 4, 1)),     # an early mark: its correction
], ids=["mark", "unmark"])
@pytest.mark.parametrize("paid", [None, ""], ids=["none", "empty"])
def test_an_issued_invoices_none_or_empty_amount_paid_is_refused_by_name(
        fake, monkeypatch, caplog, status, target, due, paid):
    """THIRD review of lot 2. update_status_report's docstring said an
    absent, None or empty amount_paid « reads as no payment, as it always
    did » — on every status. Only a brouillon's, a payée's or an annulée's
    does: an issued invoice's lateness reads it through balance_of,
    strictly, so a None or empty one is refused montant_illisible on both
    moves between « envoyée » and « en_retard », which went through on it
    before lot 2 — where a « payée » set by hand and reopened with
    amount_paid None lands. An ABSENT one reads as 0 there too: the same
    move is judged. The behaviour dates from the lot; this pins it, and the
    texts now say so (test_the_texts_say_where_an_empty_amount_paid_is_refused)."""
    fake.seed(f"invoices/{INV}", _invoice(status=status, due=due,
                                          amount_paid=paid))
    _at(monkeypatch, datetime(2031, 3, 10, 15, tzinfo=UTC))
    before = fake.peek(f"invoices/{INV}")
    fake.reset_logs()
    with caplog.at_level(logging.INFO):
        written, errors = invoice_model.update_status_report(
            INV, target, expected_etag=ETAG)
    assert written is None
    assert errors == [
        "Le montant payé enregistré sur cette facture est illisible : son "
        "retard ne peut pas être jugé, et rien n'a été écrit. La facture doit "
        "d'abord être réparée."]
    assert fake.peek(f"invoices/{INV}") == before and fake.commits == []
    (line,) = [r.json_fields for r in caplog.records if r.name == "pallas.invoice"]
    assert (line["event"], line["invoice_id"], line["reason"]) == (
        "invoice_refused", INV, "montant_illisible")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    # The key absent: read as 0, the move is judged — and allowed here.
    absent = _invoice(status=status, due=due)
    del absent["amount_paid"]
    fake.seed(f"invoices/{INV}", absent)
    written, errors = invoice_model.update_status_report(
        INV, target, expected_etag=ETAG)
    assert errors == [] and written["status"] == target


def test_the_texts_say_where_an_empty_amount_paid_is_refused():
    """THIRD review of lot 2. update_status_report's docstring said, of every
    status, that an absent, None or empty amount_paid « reads as no payment,
    as it always did »; on an issued invoice a None or empty one is refused
    montant_illisible (test_an_issued_invoices_none_or_empty_amount_paid_is_
    refused_by_name). The docstring and OBSERVABILITY.md's invoice_refused
    row now say which statuses read it as none, and where it is refused."""
    doc = " ".join(invoice_model.update_status_report.__doc__.split())
    assert ("the absent, None or empty ``amount_paid`` of a brouillon, a payée "
            "or an annulée reads as no payment") in doc
    assert ("An ISSUED invoice's None or empty ``amount_paid`` is refused, by "
            "contrast (an absent one reads as 0)") in doc
    rows = (_ATHENA / "OBSERVABILITY.md").read_text(encoding="utf-8")
    (row,) = [line for line in rows.splitlines()
              if line.startswith("| `invoice_refused` |")]
    assert ("which `balance_of` reads strictly for its lateness — a None or "
            "empty `amount_paid` is unreadable there") in row
    assert "or the `amount_paid` of any other status" in row


@pytest.mark.parametrize("status,cause", [
    ("en_retard", "automatique"), ("en_retard", ""),
    ("envoyée", "echeance"), ("annulée", "echeance"),
])
def test_a_cause_outside_the_vocabulary_raises_before_any_read(fake, status, cause):
    fake.seed(f"invoices/{INV}", _invoice(due=date(2031, 3, 1)))
    fake.reset_logs()
    with pytest.raises(ValueError):
        invoice_model.update_status_report(INV, status, cause=cause)
    assert fake.reads == [] and fake.commits == []


def test_the_cause_vocabulary():
    assert invoice_model.STATUS_CAUSES == ("manuel", "echeance")


# ══════════════════════════════════════════════════════════════════════
# 4. The web — the routes compute, the templates read (Rule 5)
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def client(fake):
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=lambda v: v,
    )
    for bp in (invoices_routes.invoices_bp, admin_ledger_routes.admin_bp,
               dossiers_routes.dossiers_bp, parties_routes.parties_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["user_email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _seed_list(fake) -> None:
    for iid, number, status, due, paid, day in (
        ("i-late", "2031-F021", "envoyée", date(2031, 3, 1), 0, 21),
        ("i-marked", "2031-F022", "en_retard", date(2031, 2, 3), 40000, 22),
        ("i-on-time", "2031-F023", "envoyée", date(2031, 4, 3), 0, 23),
        ("i-paid", "2031-F024", "payée", date(2031, 2, 3), 86231, 24),
    ):
        fake.seed(f"invoices/{iid}", _invoice(
            status=status, due=due, amount_paid=paid, id=iid,
            invoice_number=number, date=datetime(2031, 1, day, tzinfo=UTC)))


def _row(page: str, number: str) -> str:
    start = page.index(number)
    return page[start:page.index("</a>", start)]


def test_the_list_shows_echue_n_j_where_the_model_says_late(fake, client, monkeypatch):
    _seed_list(fake)
    _at(monkeypatch, datetime(2031, 3, 6, 15, tzinfo=UTC))
    page = client.get("/factures/").get_data(as_text=True)
    # Sent, due Saturday 1 March, late from Tuesday the 4th: 3 days on the 6th,
    # though nothing marked it yet.
    assert "Échue · 3 j" in _row(page, "2031-F021")
    assert "Échue · 31 j" in _row(page, "2031-F022")       # late from 4 Feb
    assert "Échue" not in _row(page, "2031-F023")
    assert "Échue" not in _row(page, "2031-F024")
    # The htmx fragment is the same partial, annotated by the same route.
    rows = client.get("/factures/", headers={"HX-Request": "true"}).get_data(
        as_text=True)
    assert "Échue · 3 j" in _row(rows, "2031-F021")


def test_the_list_template_compares_no_date():
    """Rule 5: the row reads the route's annotation, never the due date."""
    src = (_ATHENA / "templates" / "invoices" / "_invoice_rows.html").read_text(
        encoding="utf-8")
    assert "inv._days_overdue" in src
    assert "due_date" not in src


def _sheet(client, iid: str) -> str:
    return client.get(f"/factures/{iid}").get_data(as_text=True)


def test_the_sheet_shows_its_lateness_and_offers_the_mark(fake, client, monkeypatch):
    _seed_list(fake)
    _at(monkeypatch, datetime(2031, 3, 6, 15, tzinfo=UTC))
    page = _sheet(client, "i-late")
    flat = " ".join(re.sub(r"<[^>]+>", " ", page).split())
    assert "Retard 3 jours (depuis le 2031-03-04)" in flat
    assert 'value="en_retard"' in page and "Marquer en retard" in page
    assert "Retirer le retard" not in page


def test_the_sheet_of_an_invoice_not_yet_late(fake, client, monkeypatch):
    _seed_list(fake)
    _at(monkeypatch, datetime(2031, 3, 6, 15, tzinfo=UTC))
    page = _sheet(client, "i-on-time")
    flat = " ".join(re.sub(r"<[^>]+>", " ", page).split())
    assert "Retard" not in flat.replace("Retirer le retard", "")
    assert "Marquer en retard" not in page and 'value="en_retard"' not in page


def test_retirer_le_retard_shows_only_on_a_premature_mark(fake, client, monkeypatch):
    fake.seed(f"invoices/{INV}", _invoice(status="en_retard", due=date(2031, 4, 3)))
    _at(monkeypatch, datetime(2031, 3, 6, 15, tzinfo=UTC))
    page = _sheet(client, INV)
    assert "Retirer le retard" in page and 'value="envoyée"' in page
    assert "Marquer comme envoyée" not in page
    # Overdue: the button is gone, the line is there.
    _at(monkeypatch, datetime(2031, 4, 7, 15, tzinfo=UTC))
    page = _sheet(client, INV)
    assert "Retirer le retard" not in page and 'value="envoyée"' not in page
    flat = " ".join(re.sub(r"<[^>]+>", " ", page).split())
    assert "Retard 4 jours (depuis le 2031-04-04)" in flat


def test_the_sheet_s_one_day_is_singular(fake, client, monkeypatch):
    fake.seed(f"invoices/{INV}", _invoice(due=date(2031, 3, 3)))
    _at(monkeypatch, datetime(2031, 3, 4, 15, tzinfo=UTC))
    flat = " ".join(re.sub(r"<[^>]+>", " ", _sheet(client, INV)).split())
    assert "Retard 1 jour (depuis le 2031-03-04)" in flat


def test_the_pages_judge_on_montreals_day_in_the_evening_band(
        fake, client, monkeypatch):
    """At 21:30 in Montréal on the due day (02:30 UTC the next day) the sheet
    and the list say nothing of lateness and offer no « Marquer en retard »:
    the routes take today_mtl, never the UTC day."""
    fake.seed(f"invoices/{INV}", _invoice(due=date(2031, 3, 3)))
    _at(monkeypatch, datetime(2031, 3, 4, 2, 30, tzinfo=UTC))
    page = _sheet(client, INV)
    flat = " ".join(re.sub(r"<[^>]+>", " ", page).split())
    assert "Retard" not in flat and 'value="en_retard"' not in page
    assert "Échue" not in _row(client.get("/factures/").get_data(as_text=True),
                               "2031-F017")
    # Montréal's midnight (05:00 UTC): late, hours before the sweep's run.
    _at(monkeypatch, datetime(2031, 3, 4, 5, 0, tzinfo=UTC))
    assert 'value="en_retard"' in _sheet(client, INV)
    assert "Échue · 1 j" in _row(client.get("/factures/").get_data(as_text=True),
                                 "2031-F017")
