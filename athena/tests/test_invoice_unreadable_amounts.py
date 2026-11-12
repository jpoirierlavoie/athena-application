"""An unreadable stored amount takes nothing down — the four defects of
2026-11-12, fixed on the lawyer's decision.

An invoice stores its amounts in integer cents. One holding anything else —
« 150,00 $ », None, an empty string: a hand edit in the console, a script
gone wrong — made ``invoice.balance_of`` and ``available_transitions``
raise, and ``int()``'s own error QUOTES the stored value, which every
traceback carried into Cloud Logging (CLAUDE.md « Do not log PII »). The
model names such a field (``unreadable_balance_field`` for the balance,
``unreadable_transition_field`` for the transitions); every surface now asks
it first and names the amount in French, never its value:

1. the model — the void judges its amounts through the authority, and the
   SEND of a brouillon whose balance cannot be read is refused by name
   (reversing lot 2's « an unreadable amount_due never locks the send »);
2. the web — the invoice's sheet and the list render « illisible » and offer
   neither a status change nor a payment;
3. the connector — its invoice reads refuse by name, ``update_invoice``
   refuses before any write, and its money reader fails naming the field
   alone;
4. the logs — the stored value reaches no record, message, exc_text or
   traceback, on any path above, a write that fails AFTER its commit
   included.

Through the real models, routes, templates, handlers and JSON-RPC endpoint,
on the shared fake Firestore (``tests/_fake_firestore``); every « today » is
frozen at the one seam (``tests/_frozen_clock``) in a fictitious year,
2031, and every amount is invented.
"""

import html
import itertools
import logging
import os
import pathlib
import re
import sys
import traceback
from datetime import date, datetime, timezone
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
    import mcp.bearer as bearer
    import mcp.handlers as handlers
    import mcp.store as store
    import mcp.tools as tools
    import routes.admin_ledger as admin_ledger_routes
    import routes.dossiers as dossiers_routes
    import routes.invoices as invoices_routes
    import routes.parties as parties_routes

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tests._frozen_clock import freeze_deadlines  # noqa: E402
from tests.test_mcp_jsonrpc import _make_app, _rpc, _token_doc  # noqa: E402
from tests.test_mcp_output_schemas import _conforms  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc
INV = "inv-illisible-1"
NUMBER = "2031-F041"
ETAG = "aaaaaaaa-1111-4222-8333-444444444444"
#: Tuesday 11 March 2031 at noon UTC — 08:00 in Montréal. An invoice due
#: Monday 3 March is late since Tuesday the 4th: 8 days.
NOON = datetime(2031, 3, 11, 12, tzinfo=UTC)
TODAY = date(2031, 3, 11)
#: The stored values the logs and pages are searched for. Each is a
#: formatted amount a hand could type, with an ORDINARY space: the house's
#: formatter writes a no-break space, so no figure the code renders can
#: match it by accident.
TEXT = "150,00 $"
OTHER_TEXT = "1 234,56 $"
_SEARCHED = (TEXT, OTHER_TEXT)


def _midnight(day: date) -> datetime:
    """A date-only field as the models store it: midnight UTC."""
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _invoice(status: str = "envoyée", **over) -> dict:
    """An invoice of 2031 billing one time entry, every amount invented:
    640,00 $ of fees, 32,00 $ of TPS, 63,84 $ of TVQ, 735,84 $ due — on
    Monday 3 March."""
    doc = {
        "id": INV, "invoice_number": NUMBER, "status": status,
        "dossier_id": "dos-41", "dossier_file_number": "2031-041",
        "dossier_title": "Exemple c. Témoin", "client_id": "cli-41",
        "client_name": "Cliente Exemple",
        "date": datetime(2031, 2, 3, tzinfo=UTC),
        "due_date": _midnight(date(2031, 3, 3)),
        "subtotal_fees": 64000, "subtotal_expenses": 0, "subtotal": 64000,
        "gst_rate": 500, "gst_amount": 3200,
        "qst_rate": 9975, "qst_amount": 6384, "total": 73584,
        "retainer_applied": 0, "amount_due": 73584, "amount_paid": 0,
        "paid_date": None, "etag": ETAG,
    }
    doc.update(over)
    return doc


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n in ("dav.sync",
                                                 "mcp.write_support"))
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    world = install(monkeypatch, *_fake_modules())
    freeze_deadlines(monkeypatch, NOON)
    return world


def _seed(fake, doc: dict) -> dict:
    """*doc*, its one line and the time entry it bills (invoiced, naming it)
    — what a void would release. Returns the invoice as stored."""
    iid = doc["id"]
    fake.seed(f"invoices/{iid}", doc)
    fake.seed(f"invoices/{iid}/lineitems/li-{iid}", {
        "id": f"li-{iid}", "type": "fee", "source_id": f"te-{iid}",
        "date": datetime(2031, 1, 20, tzinfo=UTC), "description": "Rédaction",
        "hours": 2.0, "rate": 32000, "amount": 64000, "taxable": True})
    fake.seed(f"timeentries/te-{iid}", {
        "id": f"te-{iid}", "dossier_id": doc.get("dossier_id", ""),
        "date": datetime(2031, 1, 20, tzinfo=UTC), "hours": 2.0,
        "rate": 32000, "amount": 64000, "billable": True, "invoiced": True,
        "invoice_id": iid, "etag": f"te-{iid}-e0"})
    fake.reset_logs()
    return fake.peek(f"invoices/{iid}")


def _with(doc: dict, field: str, value) -> dict:
    """*doc* with *field* stored as *value*, or absent (``_ABSENT``)."""
    out = dict(doc)
    if value is _ABSENT:
        out.pop(field, None)
    else:
        out[field] = value
    return out


_ABSENT = object()

#: Every kind of value a Firestore field can hand back — the lateness lot's
#: list (tests/test_invoice_lateness), NaN and infinite doubles included.
_STORED_AMOUNTS = [0, 57312, -1, "57312", " 12 ", 573.12, True, None, "",
                   "1 234,56", "573.12", TEXT, float("inf"), float("nan"),
                   b"12", b"x", [], {}, datetime(2031, 3, 3, tzinfo=UTC)]


def _carries(record: logging.LogRecord, needle: str) -> bool:
    """Whether *needle* is anywhere in *record*: its message, arguments,
    structured fields, exception text, or its traceback rendered with every
    chained exception."""
    parts = [logging.Formatter().format(record), record.getMessage(),
             repr(record.args), repr(getattr(record, "json_fields", None)),
             record.exc_text or ""]
    if record.exc_info and record.exc_info[1] is not None:
        parts.append("".join(traceback.format_exception(*record.exc_info)))
    return any(needle in part for part in parts)


def _assert_no_record_carries(caplog, *needles: str) -> None:
    for record in caplog.records:
        for needle in needles:
            assert not _carries(record, needle), (
                needle, record.name, record.getMessage())


def _pallas_invoice_lines(caplog) -> list[dict]:
    return [r.json_fields for r in caplog.records if r.name == "pallas.invoice"]


# ══════════════════════════════════════════════════════════════════════
# 1. One sentence, one name per field
# ══════════════════════════════════════════════════════════════════════


def test_every_balance_field_has_its_french_name():
    """The refusals name the field in French, never its value: one name per
    amount the balance reads — a field added to the balance needs its own
    (the write-offs lot will add one)."""
    assert set(invoice_model.BALANCE_FIELD_LABELS) == set(
        invoice_model._BALANCE_FIELDS)
    assert invoice_model.BALANCE_FIELD_LABELS == {
        "amount_due": "montant dû", "amount_paid": "montant payé",
        "write_offs": "radiations"}


def test_the_sentence_names_the_invoice_and_the_field_never_the_value():
    assert invoice_model.unreadable_amount_message(
        "amount_paid", "son solde et son statut ne peuvent pas être jugés") == (
        "Le montant payé enregistré sur cette facture est illisible : son "
        "solde et son statut ne peuvent pas être jugés. La facture doit "
        "d'abord être réparée.")
    assert invoice_model.unreadable_amount_message(
        "amount_due", "x", number=NUMBER) == (
        "Le montant dû enregistré sur la facture 2031-F041 est illisible : x. "
        "La facture doit d'abord être réparée.")


# ══════════════════════════════════════════════════════════════════════
# 2. The model — the send (defect 4a, reversing lot 2)
# ══════════════════════════════════════════════════════════════════════

_SEND_REFUSAL = (
    "une fois envoyée, son solde ne pourrait jamais être jugé, et rien n'a "
    "été écrit")


@pytest.mark.parametrize("field,value", [
    ("amount_due", TEXT), ("amount_due", None), ("amount_due", ""),
    ("amount_due", float("nan")), ("amount_paid", None), ("amount_paid", ""),
], ids=["due-text", "due-none", "due-empty", "due-nan", "paid-none",
        "paid-empty"])
def test_a_brouillon_whose_balance_cannot_be_read_is_never_sent(
        fake, caplog, field, value):
    """Sent, the invoice would be an issued one whose balance — and so its
    lateness — can never be judged. Lot 2 let a brouillon's unreadable
    amount_due through (no transition reads it); the connector's answer then
    failed AFTER the commit, its ERROR traceback quoting the value. A None
    or empty amount_paid reads as « no payment » for a brouillon's
    transitions, but balance_of reads it strictly too: refused alike — the
    rule is the balance's authority. Nothing is written."""
    before = _seed(fake, _invoice(status="brouillon", **{field: value}))
    with caplog.at_level(logging.DEBUG):
        written, errors = invoice_model.update_status_report(
            INV, "envoyée", expected_etag=ETAG)
    assert written is None
    assert errors == [invoice_model.unreadable_amount_message(
        field, _SEND_REFUSAL)]
    assert fake.peek(f"invoices/{INV}") == before and fake.commits == []
    (line,) = _pallas_invoice_lines(caplog)
    assert (line["event"], line["operation"], line["reason"]) == (
        "invoice_refused", "status", "montant_illisible")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    _assert_no_record_carries(caplog, *_SEARCHED)


def test_the_send_s_refusal_in_full():
    """The sentence the lawyer and Claude read, word for word."""
    assert invoice_model.unreadable_amount_message("amount_due", _SEND_REFUSAL) == (
        "Le montant dû enregistré sur cette facture est illisible : une fois "
        "envoyée, son solde ne pourrait jamais être jugé, et rien n'a été "
        "écrit. La facture doit d'abord être réparée.")


def test_the_send_is_judged_exactly_on_the_balance_s_authority(fake):
    """On every pair of stored amounts, an absent key included: a brouillon
    is refused by name exactly when the model cannot read its balance — on
    the transitions' field first (« son changement de statut »), else on
    the balance's (« une fois envoyée ») — and sent otherwise. Nothing more
    is refused, nothing less."""
    values = [_ABSENT, *_STORED_AMOUNTS]
    for due, paid in itertools.product(values, repeat=2):
        doc = _with(_with(_invoice(status="brouillon"), "amount_due", due),
                    "amount_paid", paid)
        _seed(fake, doc)
        _written, errors = invoice_model.update_status_report(
            INV, "envoyée", expected_etag=ETAG)
        moves = invoice_model.unreadable_transition_field(doc)
        balance = invoice_model.unreadable_balance_field(doc)
        if moves is not None:
            assert errors == [invoice_model.unreadable_amount_message(
                moves, "son changement de statut ne peut pas être jugé, et "
                "rien n'a été écrit")], doc
        elif balance is not None:
            assert errors == [invoice_model.unreadable_amount_message(
                balance, _SEND_REFUSAL)], doc
        else:
            assert errors == [], doc
            assert fake.peek(f"invoices/{INV}")["status"] == "envoyée", doc


# ══════════════════════════════════════════════════════════════════════
# 3. The model — the void (defect 3)
# ══════════════════════════════════════════════════════════════════════

_VOID_REFUSAL = "son annulation ne peut pas être jugée, et rien n'a été annulé"


@pytest.mark.parametrize("status,field,value", [
    ("envoyée", "amount_due", TEXT),
    ("envoyée", "amount_paid", TEXT),
    ("en_retard", "amount_paid", None),
    ("envoyée", "amount_paid", ""),
    ("brouillon", "amount_paid", OTHER_TEXT),
    ("brouillon", "amount_paid", float("inf")),
], ids=["sent-due-text", "sent-paid-text", "late-paid-none",
        "sent-paid-empty", "draft-paid-text", "draft-paid-infinite"])
def test_the_void_refuses_by_name_what_its_authority_cannot_read(
        fake, caplog, status, field, value):
    """void_invoice_report read amount_paid itself, int(… or 0): a value it
    could not read fell into « Erreur lors de l'annulation », the ERROR
    traceback quoting it, and an issued invoice's None or empty one read as
    no payment — the void committed, and the connector's answer then failed
    after the commit. It asks the authority now, and refuses by name before
    anything is written: the invoice, its source and the store untouched."""
    before = _seed(fake, _invoice(status=status, **{field: value}))
    source = fake.peek(f"timeentries/te-{INV}")
    with caplog.at_level(logging.DEBUG):
        report, errors = invoice_model.void_invoice_report(
            INV, expected_etag=ETAG, reason="Émise par erreur.")
    assert report is None
    assert errors == [invoice_model.unreadable_amount_message(
        field, _VOID_REFUSAL)]
    assert fake.peek(f"invoices/{INV}") == before
    assert fake.peek(f"timeentries/te-{INV}") == source
    assert fake.commits == []
    (line,) = _pallas_invoice_lines(caplog)
    assert (line["event"], line["operation"], line["reason"]) == (
        "invoice_refused", "void", "montant_illisible")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    _assert_no_record_carries(caplog, *_SEARCHED)


def test_the_void_s_refusal_in_full():
    assert invoice_model.unreadable_amount_message("amount_paid", _VOID_REFUSAL) == (
        "Le montant payé enregistré sur cette facture est illisible : son "
        "annulation ne peut pas être jugée, et rien n'a été annulé. La "
        "facture doit d'abord être réparée.")


@pytest.mark.parametrize("paid,shown", [(12345, "123,45"), ("12345", "123,45")],
                         ids=["cents", "digits"])
def test_a_recorded_payment_still_refuses_the_void_naming_its_amount(
        fake, paid, shown):
    """_payment_recorded says « a payment is recorded »: the refusal still
    names the amount, formatted — a stored value int() reads, a string of
    digits included, as before."""
    _seed(fake, _invoice(status="envoyée", amount_paid=paid))
    report, errors = invoice_model.void_invoice_report(INV, expected_etag=ETAG)
    assert report is None
    (error,) = errors
    assert error.startswith(
        f"Cette facture porte un encaissement de {format_cents_fr(12345)} : "
        "contre-passez l'écriture dans « Administration »")
    assert shown in error
    assert fake.commits == []


def test_the_void_is_judged_exactly_on_the_transitions_authority(fake):
    """On every status the void takes and every pair of stored amounts, an
    absent key included: refused by name exactly when
    unreadable_transition_field names a field — the field it names —;
    else refused while a payment is recorded (_payment_recorded); else
    voided, its source released. The void judges what its rule reads and
    nothing more: a brouillon's amount_due, which it never reads, refuses
    nothing HERE (the connector refuses it before the model, since its
    answer reads the balance: section 5)."""
    values = [_ABSENT, *_STORED_AMOUNTS]
    for status in ("brouillon", "envoyée", "en_retard"):
        for due, paid in itertools.product(values, repeat=2):
            doc = _with(_with(_invoice(status=status), "amount_due", due),
                        "amount_paid", paid)
            _seed(fake, doc)
            report, errors = invoice_model.void_invoice_report(
                INV, expected_etag=ETAG)
            field = invoice_model.unreadable_transition_field(doc)
            if field is not None:
                assert errors == [invoice_model.unreadable_amount_message(
                    field, _VOID_REFUSAL)], (status, doc)
            elif invoice_model._payment_recorded(doc):
                assert len(errors) == 1 and errors[0].startswith(
                    "Cette facture porte un encaissement de "), (status, doc)
            else:
                assert errors == [], (status, doc)
                assert fake.peek(f"invoices/{INV}")["status"] == "annulée"
                assert report["released_time_entry_ids"] == [f"te-{INV}"]


# ══════════════════════════════════════════════════════════════════════
# 4. The web — the sheet and the list say « illisible » (defect 1)
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def client(fake):
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl,
        # main.py's filter, as it registers it.
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


def _flat(page: str) -> str:
    """The page's text: tags dropped, entities read, every run of
    whitespace — the formatter's no-break spaces included — one space."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())


def _row(page: str, number: str) -> str:
    start = page.index(number)
    return page[start:page.index("</a>", start)]


_SHEET_NOTICE = "son solde et son statut ne peuvent pas être jugés"


@pytest.mark.parametrize("status,field,value,issued", [
    ("envoyée", "amount_paid", TEXT, True),
    ("en_retard", "amount_due", None, True),
    ("brouillon", "amount_due", "", False),
    ("payée", "amount_paid", None, False),
    ("annulée", "amount_paid", OTHER_TEXT, False),
], ids=["sent-paid-text", "late-due-none", "draft-due-empty",
        "paid-paid-none", "void-paid-text"])
def test_the_sheet_says_illisible_and_offers_no_status_nor_payment(
        fake, client, caplog, status, field, value, issued):
    """The sheet answered 500 — balance_of and available_transitions raised,
    the error log quoting the value. The route computes no balance, lateness
    or transition for it now: « illisible » on the « Solde » line (and the
    « Retard » line where the model judges lateness — an issued invoice),
    the notice naming the field, no status change, no payment hint. Its
    other actions stay: the brouillon's correction, the deletion of a voided
    invoice."""
    _seed(fake, _invoice(status=status, **{field: value}))
    with caplog.at_level(logging.DEBUG):
        response = client.get(f"/factures/{INV}")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    text = _flat(page)
    assert invoice_model.unreadable_amount_message(field, _SHEET_NOTICE) in text
    assert "Solde illisible" in text
    assert ("Retard illisible" in text) is issued
    assert f"/factures/{INV}/status" not in page
    assert f"/factures/{INV}/void" not in page
    assert "Un encaissement se saisit dans" not in text
    assert ("Modifier le brouillon" in text) is (status == "brouillon")
    assert (f"/factures/{INV}/delete" in page) is (status == "annulée")
    for needle in _SEARCHED:
        assert needle not in page
    _assert_no_record_carries(caplog, *_SEARCHED)


def test_the_sheet_s_notice_in_full(fake, client):
    _seed(fake, _invoice(amount_paid=TEXT))
    text = _flat(client.get(f"/factures/{INV}").get_data(as_text=True))
    assert ("Le montant payé enregistré sur cette facture est illisible : son "
            "solde et son statut ne peuvent pas être jugés. La facture doit "
            "d'abord être réparée.") in text


def test_one_unreadable_amount_never_hides_the_other_s_figure(fake, client):
    """Each amount the sheet prints itself — « Montant dû à l'émission »
    (a retainer invoice), « Encaissé » — is asked of the model ALONE: an
    unreadable amount_paid leaves the readable amount_due's figure, both
    unreadable show both « illisible », and no unreadable value ever
    reaches the cents filter, whose int() would raise."""
    _seed(fake, _invoice(retainer_applied=20000, amount_due=53584,
                         amount_paid=TEXT))
    text = _flat(client.get(f"/factures/{INV}").get_data(as_text=True))
    assert "Montant dû à l'émission 535,84 $" in text
    assert "Encaissé illisible" in text
    _seed(fake, _invoice(retainer_applied=20000, amount_due="",
                         amount_paid=OTHER_TEXT))
    response = client.get(f"/factures/{INV}")
    assert response.status_code == 200
    text = _flat(response.get_data(as_text=True))
    assert "Montant dû à l'émission illisible" in text
    assert "Encaissé illisible" in text
    # The notice names the FIRST field the balance reads.
    assert invoice_model.unreadable_amount_message(
        "amount_due", _SHEET_NOTICE) in text
    # Without a retainer, an unreadable amount_due still shows on its line.
    _seed(fake, _invoice(amount_due=None))
    text = _flat(client.get(f"/factures/{INV}").get_data(as_text=True))
    assert "Montant dû à l'émission illisible" in text


@pytest.mark.parametrize("status", invoice_model.VALID_STATUSES)
def test_the_sheet_never_fails_on_a_stored_amount(fake, client, status):
    """On every status and every kind of stored amount: 200, and
    « illisible » exactly when the model cannot read the balance."""
    for field in ("amount_due", "amount_paid"):
        for value in (57312, TEXT, None, "", float("inf"), float("nan"),
                      [57312], b"x"):
            doc = _invoice(status=status, **{field: value})
            _seed(fake, doc)
            response = client.get(f"/factures/{INV}")
            assert response.status_code == 200, (status, field, value)
            text = _flat(response.get_data(as_text=True))
            unreadable = invoice_model.unreadable_balance_field(doc)
            assert ("Solde illisible" in text) is (unreadable is not None), (
                status, field, value)


def test_a_normal_invoice_s_sheet_is_unchanged(fake, client):
    """The control: a readable, late invoice shows its balance, its
    « Retard » line, « Marquer en retard » and the « Administration »
    hint — and no « illisible »."""
    _seed(fake, _invoice())
    page = client.get(f"/factures/{INV}").get_data(as_text=True)
    text = _flat(page)
    assert "Solde 735,84 $" in text
    assert "Retard 8 jours (depuis le 2031-03-04)" in text
    assert "Marquer en retard" in text and f"/factures/{INV}/void" in page
    assert "Un encaissement se saisit dans" in text
    assert "illisible" not in text


def test_the_list_row_says_illisible_without_echue(fake, client, caplog):
    """The list answered 500 for the whole page. The row of an invoice whose
    balance cannot be read says « illisible » where its balance would be,
    and no « Échue · N j »; its neighbours are unchanged; a voided one shows
    its total, as every voided row. The htmx fragment is the same partial."""
    _seed(fake, _invoice(id="inv-a", invoice_number="2031-F051",
                         amount_paid=TEXT,
                         date=datetime(2031, 1, 21, tzinfo=UTC)))
    _seed(fake, _invoice(id="inv-b", invoice_number="2031-F052",
                         date=datetime(2031, 1, 22, tzinfo=UTC)))
    _seed(fake, _invoice(id="inv-c", invoice_number="2031-F053",
                         status="annulée", amount_due=None,
                         date=datetime(2031, 1, 23, tzinfo=UTC)))
    with caplog.at_level(logging.DEBUG):
        response = client.get("/factures/")
        fragment = client.get("/factures/", headers={"HX-Request": "true"})
    for page in (response.get_data(as_text=True),
                 fragment.get_data(as_text=True)):
        unreadable = _flat(_row(page, "2031-F051"))
        assert "illisible" in unreadable and "Échue" not in unreadable
        assert "sur 735,84 $" in unreadable
        assert "Échue · 8 j" in _flat(_row(page, "2031-F052"))
        voided = _flat(_row(page, "2031-F053"))
        assert "illisible" not in voided and "735,84 $" in voided
        for needle in _SEARCHED:
            assert needle not in page
    assert response.status_code == fragment.status_code == 200
    _assert_no_record_carries(caplog, *_SEARCHED)


# ══════════════════════════════════════════════════════════════════════
# 5. The connector — reads refuse by name, update_invoice before any write
#    (defects 2 and 4)
# ══════════════════════════════════════════════════════════════════════

_READ_REFUSAL = (
    "son solde et son retard ne peuvent pas être lus, et rien n'est rendu")
_WRITE_REFUSAL = "son solde ne peut pas être lu, et rien n'a été écrit"


def test_the_connector_s_refusals_in_full():
    assert handlers._UNREADABLE_READ == _READ_REFUSAL
    assert handlers._UNREADABLE_WRITE == _WRITE_REFUSAL
    assert invoice_model.unreadable_amount_message(
        "amount_paid", _READ_REFUSAL, number=NUMBER) == (
        "Le montant payé enregistré sur la facture 2031-F041 est illisible : "
        "son solde et son retard ne peuvent pas être lus, et rien n'est "
        "rendu. La facture doit d'abord être réparée.")
    assert invoice_model.unreadable_amount_message(
        "amount_due", _WRITE_REFUSAL, number=NUMBER) == (
        "Le montant dû enregistré sur la facture 2031-F041 est illisible : "
        "son solde ne peut pas être lu, et rien n'a été écrit. La facture "
        "doit d'abord être réparée.")


@pytest.mark.parametrize("status", invoice_model.VALID_STATUSES)
def test_get_invoice_refuses_by_name_or_answers_never_fails(fake, status):
    """get_invoice built its row through int() and available_transitions: a
    raw error, « internal error » to Claude and the value in the endpoint's
    traceback. On every status and every kind of stored amount it now
    refuses by name exactly when the model cannot read the balance, and
    answers its unchanged contract otherwise."""
    for field in ("amount_due", "amount_paid"):
        for value in (57312, TEXT, None, "", float("inf"), [57312], b"x"):
            doc = _invoice(status=status, **{field: value})
            _seed(fake, doc)
            unreadable = invoice_model.unreadable_balance_field(doc)
            if unreadable is None:
                payload = handlers.get_invoice({"invoice_id": INV})
                assert payload["found"] is True
                _conforms("get_invoice", payload)
                continue
            with pytest.raises(tools.ToolArgumentError) as refused:
                handlers.get_invoice({"invoice_id": INV})
            assert str(refused.value) == invoice_model.unreadable_amount_message(
                unreadable, _READ_REFUSAL, number=NUMBER), (field, value)
            assert refused.value.reason == "invoice_refused"
            assert fake.commits == []


@pytest.mark.parametrize("args", [{}, {"dossier_id": "dos-41"},
                                  {"status_group": "impayée"}],
                         ids=["register", "dossier", "unpaid"])
def test_list_invoices_refuses_by_name(fake, args):
    """The register refuses its whole page — never a page short of a row,
    nor a row short of its balance — naming the invoice and the field."""
    _seed(fake, _invoice(id="inv-ok", invoice_number="2031-F040"))
    _seed(fake, _invoice(amount_paid=TEXT))
    with pytest.raises(tools.ToolArgumentError) as refused:
        handlers.list_invoices(dict(args))
    assert str(refused.value) == invoice_model.unreadable_amount_message(
        "amount_paid", _READ_REFUSAL, number=NUMBER)
    assert TEXT not in str(refused.value)


def test_get_billing_snapshot_refuses_by_name_before_its_total(fake):
    """The snapshot sums the outstanding balances over EVERY issued invoice
    before it lists fifty: it refuses by name before the sum — never a total
    without the invoice, never int()'s raw error."""
    _seed(fake, _invoice(id="inv-ok", invoice_number="2031-F040"))
    _seed(fake, _invoice(status="en_retard", amount_due=None))
    with pytest.raises(tools.ToolArgumentError) as refused:
        handlers.get_billing_snapshot({})
    assert str(refused.value) == invoice_model.unreadable_amount_message(
        "amount_due", _READ_REFUSAL, number=NUMBER)


def test_a_readable_register_is_unchanged(fake):
    """The control: readable invoices list, newest first, and conform; the
    snapshot sums their balances."""
    _seed(fake, _invoice(id="inv-ok", invoice_number="2031-F040",
                         date=datetime(2031, 2, 4, tzinfo=UTC)))
    _seed(fake, _invoice())
    payload = handlers.list_invoices({})
    assert [r["invoice_number"] for r in payload["items"]] == [
        "2031-F040", NUMBER]
    _conforms("list_invoices", payload)
    snapshot = handlers.get_billing_snapshot({})
    assert snapshot["outstanding_cents"] == 2 * 73584


def _void_args(**over) -> dict:
    return {"invoice_id": INV, "status": "annulée",
            "void_reason": "Émise par erreur.", "expected_etag": ETAG, **over}


@pytest.mark.parametrize("status,field,value", [
    ("brouillon", "amount_due", TEXT),
    ("brouillon", "amount_paid", ""),
    ("envoyée", "amount_paid", None),
    ("en_retard", "amount_due", OTHER_TEXT),
], ids=["draft-due-text", "draft-paid-empty", "sent-paid-none",
        "late-due-text"])
def test_the_connector_s_void_refuses_before_any_commit(
        fake, caplog, status, field, value):
    """update_invoice(status « annulée ») committed the void, then failed
    building its answer — which reads the balance — or fell into the raw
    error: « ENREGISTRÉE — NE PAS RÉESSAYER » over a void the caller never
    saw, the traceback quoting the value. The handler refuses by name
    before the model now: the invoice, its source, the store untouched."""
    before = _seed(fake, _invoice(status=status, **{field: value}))
    source = fake.peek(f"timeentries/te-{INV}")
    with caplog.at_level(logging.DEBUG), pytest.raises(
            tools.ToolArgumentError) as refused:
        handlers.update_invoice(_void_args())
    assert str(refused.value) == invoice_model.unreadable_amount_message(
        field, _WRITE_REFUSAL, number=NUMBER)
    assert refused.value.reason == "invoice_refused"
    assert fake.peek(f"invoices/{INV}") == before
    assert fake.peek(f"timeentries/te-{INV}") == source
    assert fake.commits == []
    _assert_no_record_carries(caplog, *_SEARCHED)


@pytest.mark.parametrize("field,value", [
    ("amount_due", TEXT), ("amount_paid", None), ("amount_paid", ""),
], ids=["due-text", "paid-none", "paid-empty"])
def test_the_connector_s_send_reaches_the_model_s_refusal(
        fake, caplog, field, value):
    """brouillon → envoyée over an unreadable balance COMMITTED the send,
    then answered CommittedWriteError, run_write's ERROR traceback quoting
    the amount. The model refuses the send now, by name, and its sentence
    reaches the caller — nothing written."""
    before = _seed(fake, _invoice(status="brouillon", **{field: value}))
    with caplog.at_level(logging.DEBUG), pytest.raises(
            tools.ToolArgumentError) as refused:
        handlers.update_invoice({"invoice_id": INV, "status": "envoyée",
                                 "expected_etag": ETAG})
    assert str(refused.value) == invoice_model.unreadable_amount_message(
        field, _SEND_REFUSAL)
    assert refused.value.reason == "invoice_refused"
    assert fake.peek(f"invoices/{INV}") == before and fake.commits == []
    (line,) = _pallas_invoice_lines(caplog)
    assert line["reason"] == "montant_illisible"
    _assert_no_record_carries(caplog, *_SEARCHED)


@pytest.mark.parametrize("status,args", [
    ("envoyée", {"status": "envoyée"}),
    ("en_retard", {"status": "en_retard"}),
    ("annulée", {"status": "annulée", "void_reason": "Émise par erreur."}),
], ids=["sent-again", "late-again", "void-again"])
def test_a_same_status_call_refuses_by_name(fake, caplog, status, args):
    """Asking the status the invoice already has wrote nothing and answered
    the no-op — whose payload read the amounts: a raw ValueError quoting the
    value. Refused by name now, before anything is written."""
    before = _seed(fake, _invoice(status=status, amount_paid=TEXT))
    with caplog.at_level(logging.DEBUG), pytest.raises(
            tools.ToolArgumentError) as refused:
        handlers.update_invoice({"invoice_id": INV, "expected_etag": ETAG,
                                 **args})
    assert str(refused.value) == invoice_model.unreadable_amount_message(
        "amount_paid", _WRITE_REFUSAL, number=NUMBER)
    assert fake.peek(f"invoices/{INV}") == before and fake.commits == []
    _assert_no_record_carries(caplog, *_SEARCHED)


def test_a_draft_correction_refuses_by_name(fake, caplog):
    """The correction itself reads no amount, but its answer does: it
    COMMITTED, then failed building it. Refused by name before any write."""
    before = _seed(fake, _invoice(status="brouillon", amount_due=TEXT))
    with caplog.at_level(logging.DEBUG), pytest.raises(
            tools.ToolArgumentError) as refused:
        handlers.update_invoice({"invoice_id": INV, "notes": "Corrigée.",
                                 "expected_etag": ETAG})
    assert str(refused.value) == invoice_model.unreadable_amount_message(
        "amount_due", _WRITE_REFUSAL, number=NUMBER)
    assert fake.peek(f"invoices/{INV}") == before and fake.commits == []
    _assert_no_record_carries(caplog, *_SEARCHED)


def test_a_readable_invoice_still_moves_and_voids(fake):
    """The control: the same calls over readable amounts write, and their
    answers conform."""
    _seed(fake, _invoice(status="brouillon"))
    sent = handlers.update_invoice({"invoice_id": INV, "status": "envoyée",
                                    "expected_etag": ETAG})
    assert sent["outcome"] == "applied"
    _conforms("update_invoice", sent)
    again = handlers.update_invoice({"invoice_id": INV, "status": "envoyée",
                                     "expected_etag": sent["entity"]["etag"]})
    assert again["outcome"] == "unchanged"
    voided = handlers.update_invoice(_void_args(
        expected_etag=sent["entity"]["etag"]))
    assert voided["outcome"] == "applied"
    assert voided["released_time_entry_ids"] == [f"te-{INV}"]
    _conforms("update_invoice", voided)


# ══════════════════════════════════════════════════════════════════════
# 6. No traceback quotes a stored amount — the field-only money reader
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("value,needle", [
    (TEXT, "150,00"), (OTHER_TEXT, "1 234,56"), (b"1 234,56", "1 234,56"),
    ([57312], "57312"), ({"montant": 57312}, "57312"),
    (float("inf"), None), (float("nan"), None),
], ids=["text", "other-text", "bytes", "list", "map", "infinite", "nan"])
def test_the_money_reader_fails_naming_the_field_alone(value, needle):
    """_money's int() error quoted the stored value. UnreadableAmount's
    message is the field's name alone, raised OUTSIDE the failed conversion:
    no chained context, no cause — its rendered traceback holds the field
    and nothing of the value."""
    with pytest.raises(handlers.UnreadableAmount) as raised:
        handlers._money({}, "amount_paid", value)
    error = raised.value
    assert isinstance(error, ValueError)
    assert error.args == ("amount_paid",) and str(error) == "amount_paid"
    assert error.__context__ is None and error.__cause__ is None
    rendered = "".join(traceback.format_exception(error))
    assert "UnreadableAmount: amount_paid" in rendered
    if needle is not None:
        assert needle not in rendered


def test_the_money_reader_reads_what_it_always_read():
    """Unchanged for every value int() reads: absent, None or empty as 0."""
    for value in (0, 57312, -1, "57312", " 12 ", 573.12, True, None, "",
                  b"12", "0"):
        payload: dict = {}
        handlers._money(payload, "total", value)
        assert payload["total_cents"] == int(value or 0), value
        assert handlers._cents("total", value) == int(value or 0), value


def test_the_handlers_balance_and_transitions_fail_naming_the_field_alone():
    """The handlers' balance and transitions reads go through the model's
    names first: field-only on failure, the model's own answers otherwise."""
    bad = _invoice(amount_paid=TEXT)
    with pytest.raises(handlers.UnreadableAmount) as balance:
        handlers._invoice_balance(bad)
    with pytest.raises(handlers.UnreadableAmount) as moves:
        handlers._connector_transitions(bad, today=TODAY)
    for raised in (balance, moves):
        assert str(raised.value) == "amount_paid"
        assert raised.value.__context__ is None
        assert "150,00" not in "".join(traceback.format_exception(raised.value))
    good = _invoice()
    assert handlers._invoice_balance(good) == invoice_model.balance_of(good) == 73584
    assert handlers._connector_transitions(good, today=TODAY) == [
        "en_retard", "annulée"]


def test_a_void_failing_after_its_commit_logs_the_field_never_the_value(
        fake, monkeypatch, caplog):
    """The guarantee « even after a commit ». Between the handler's read and
    the model's transaction, a hand edit in the console stores a formatted
    amount_due on a brouillon (its etag untouched, as such an edit leaves
    it): the void's own rule never reads a brouillon's amount_due, so the
    void lands, and its answer fails — honest about the commit
    (CommittedWriteError, « ne pas réessayer »). run_write's ERROR line
    keeps its traceback: it names the field, never the value."""
    _seed(fake, _invoice(status="brouillon"))
    door = invoice_model.void_invoice_report

    def _console_edit_then_void(*args, **kwargs):
        fake.external_write(f"invoices/{INV}", {
            **fake.peek(f"invoices/{INV}"), "amount_due": TEXT})
        return door(*args, **kwargs)

    monkeypatch.setattr(invoice_model, "void_invoice_report",
                        _console_edit_then_void)
    with caplog.at_level(logging.DEBUG), pytest.raises(
            tools.CommittedWriteError):
        handlers.update_invoice(_void_args())
    assert fake.peek(f"invoices/{INV}")["status"] == "annulée"
    (line,) = [r for r in caplog.records
               if r.getMessage() == "mcp write failed after its commit point"]
    assert line.exc_info is not None and line.levelno == logging.ERROR
    assert "UnreadableAmount: amount_due" in logging.Formatter().format(line)
    _assert_no_record_carries(caplog, *_SEARCHED)


def test_a_send_failing_after_its_commit_on_a_tax_amount_logs_the_field(
        fake, caplog):
    """The send's answer reads the tax amounts AFTER its commit, for its
    warnings — no transition reads them, so nothing refuses before. That
    failure stays honest about the commit, and its traceback names the field
    alone (_cents), never the value."""
    _seed(fake, _invoice(status="brouillon", gst_amount=TEXT))
    with caplog.at_level(logging.DEBUG), pytest.raises(
            tools.CommittedWriteError):
        handlers.update_invoice({"invoice_id": INV, "status": "envoyée",
                                 "expected_etag": ETAG})
    assert fake.peek(f"invoices/{INV}")["status"] == "envoyée"
    (line,) = [r for r in caplog.records
               if r.getMessage() == "mcp write failed after its commit point"]
    assert "UnreadableAmount: gst_amount" in logging.Formatter().format(line)
    _assert_no_record_carries(caplog, *_SEARCHED)


# ══════════════════════════════════════════════════════════════════════
# 7. Through the JSON-RPC endpoint — no log record carries the value
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def rpc(fake, monkeypatch):
    """tools/call through the real endpoint, under a read-and-write token."""
    monkeypatch.setattr(store, "get_token",
                        lambda h: _token_doc("athena:read athena:write"))
    monkeypatch.setattr(store, "stamp_token_last_used", lambda h: None)
    bearer.reset_brake_state()
    client = _make_app().test_client()
    ids = itertools.count(1)

    def _call(name: str, arguments: dict) -> dict:
        return _rpc(client, {
            "jsonrpc": "2.0", "id": next(ids), "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }).get_json()

    yield _call
    bearer.reset_brake_state()


def test_no_log_record_carries_the_stored_value_on_any_path(fake, rpc, caplog):
    """Every connector path over an unreadable amount — the four reads that
    build an invoice row, the same-status call, the void, the send and the
    draft correction — answers a -32602 that names the field and the
    invoice, never the value; nothing is written; and no record of any
    logger, at any level, carries the value — in its message, its fields,
    its exception text or its traceback."""
    _seed(fake, _invoice(amount_paid=TEXT))
    _seed(fake, _invoice(id="inv-draft", invoice_number="2031-F042",
                         status="brouillon", amount_due=OTHER_TEXT))
    before = {path: fake.peek(path) for path in (
        f"invoices/{INV}", "invoices/inv-draft", f"timeentries/te-{INV}",
        "timeentries/te-inv-draft")}
    calls = [
        ("get_invoice", {"invoice_id": INV}),
        ("get_invoice", {"invoice_id": "inv-draft"}),
        ("list_invoices", {}),
        ("list_invoices", {"dossier_id": "dos-41"}),
        ("get_billing_snapshot", {}),
        ("update_invoice", {"invoice_id": INV, "status": "envoyée",
                            "expected_etag": ETAG}),
        ("update_invoice", {"invoice_id": INV, "status": "en_retard",
                            "expected_etag": ETAG}),
        ("update_invoice", _void_args()),
        ("update_invoice", {"invoice_id": "inv-draft", "status": "envoyée",
                            "expected_etag": ETAG}),
        ("update_invoice", {"invoice_id": "inv-draft", "notes": "Corrigée.",
                            "expected_etag": ETAG}),
        ("update_invoice", _void_args(invoice_id="inv-draft")),
    ]
    with caplog.at_level(logging.DEBUG):
        for name, arguments in calls:
            body = rpc(name, arguments)
            assert body["error"]["code"] == -32602, (name, arguments, body)
            message = body["error"]["message"]
            assert "illisible" in message and "doit d'abord être réparée" in (
                message), (name, message)
            for needle in _SEARCHED:
                assert needle not in message
    assert {path: fake.peek(path) for path in before} == before
    _assert_no_record_carries(caplog, *_SEARCHED)
    # The write refusals are audited by their code, as every refusal.
    refused = [r.json_fields for r in caplog.records
               if getattr(r, "json_fields", {}).get("event") == "mcp_write_refused"]
    assert [f["reason"] for f in refused] == ["invoice_refused"] * 6


# ══════════════════════════════════════════════════════════════════════
# 8. The texts say where montant_illisible reaches now
# ══════════════════════════════════════════════════════════════════════


def test_the_texts_say_where_an_unreadable_amount_is_refused_now():
    """OBSERVABILITY.md's rows and the model's docstrings no longer say lot
    2's « a brouillon's unreadable amount_due refuses nothing »: the send
    and the void refuse by name, the handler refuses what its answer reads,
    and the reads refuse too. (DEPLOYMENT.md's entry is pinned beside the
    connector's other runbook pins, tests/test_mcp_billing_writes.)"""
    rows = (_ATHENA / "OBSERVABILITY.md").read_text(encoding="utf-8")
    (invoice_row,) = [line for line in rows.splitlines()
                      if line.startswith("| `invoice_refused` |")]
    assert "A brouillon's unreadable `amount_due` refuses nothing" not in (
        invoice_row)
    assert ("The SEND and the REOPEN (`status`, « brouillon → envoyée » and "
            "« payée → envoyée », the reopen of a « payée » set by hand): "
            "refused when `balance_of` cannot read the invoice at all"
            ) in invoice_row
    assert ("The VOID (`void`): refused on the same field as the transitions "
            "(`invoice.unreadable_transition_field`), before anything is "
            "written") in invoice_row
    (mcp_row,) = [line for line in rows.splitlines()
                  if line.startswith("| `mcp_write_refused` |")]
    assert ("the void, a draft correction and a call that asks the status "
            "the invoice already has by the HANDLER first") in mcp_row
    assert ("The connector's invoice READS (`get_invoice`, `list_invoices`, "
            "`get_billing_snapshot`) refuse such an invoice by name too") in (
        mcp_row)
    assert "`mcp.handlers.UnreadableAmount`" in mcp_row
    status_doc = " ".join(invoice_model.update_status_report.__doc__.split())
    assert "so an unreadable one locks none" not in status_doc
    assert ("« brouillon → envoyée » and « payée → envoyée » are refused by "
            "name (``montant_illisible``) when :func:`balance_of` cannot "
            "read the invoice") in status_doc
    assert "a « payée » set by hand reopens only when its balance can be read" in (
        status_doc)
    moves_doc = " ".join(
        invoice_model.unreadable_transition_field.__doc__.split())
    assert "locks none of theirs" not in moves_doc
    void_doc = " ".join(invoice_model.void_invoice_report.__doc__.split())
    assert "The stored amounts are judged through their authority" in void_doc
