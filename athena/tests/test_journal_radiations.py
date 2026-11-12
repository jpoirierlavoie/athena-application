"""« Journal des radiations et intérêts » — lot 3c of « Factures : retard,
radiation, intérêts » (the lawyer's decision B of 2026-11-11): a DERIVED
journal, with a page, a CSV and a PDF, that stores nothing.

The derivation (``models/financial_statements.write_off_journal``) is
tested pure, on snapshots built by hand with the statements' own test
builders — and against the statements themselves on seeded random worlds:
the journal's totals ARE the period's « Créances irrécouvrables » and its
two TPS/TVQ bad-debt rows, in-progress periods included. The page, the
exports and the hub's link run through the real routes, service, models
and templates on the shared fake Firestore, « today » frozen at the one
seam (``tests/_frozen_clock``) on a day of the fictitious year 2031; every
name and amount is invented.
"""

import csv
import io
import logging
import os
import pathlib
import random
import re
import sys
from datetime import date, datetime, timedelta, timezone
from html import unescape
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import financial_statements as fs
    from models import full_reads
    from models import invoice as invoice_model
    import routes.admin_ledger as admin_routes
    import routes.auth_routes as auth_routes
    import routes.comptabilite as comptabilite_routes
    import routes.invoices as invoices_routes
    import routes.journal_radiations as journal_routes
    import routes.trust as trust_routes

from flask import Flask  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402

from tests import test_compiled_classes as compiled  # noqa: E402
from tests import test_financial_statements as tfs  # noqa: E402
from tests._fake_firestore import install  # noqa: E402
from tests._frozen_clock import freeze_deadlines  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils import journal_radiations_pdf as sheet  # noqa: E402
from utils import periods  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.html_attr import jsattr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc
# Thursday 20 March 2031, 11:00 in Montréal: March is in progress.
TODAY = datetime(2031, 3, 20, 15, 0, tzinfo=UTC)
URL = "/comptabilite/radiations/"

# Two invented invoices: 750,00 $ and 500,00 $ of fees, TPS 5 %, TVQ 9,975 %.
A_FEES, A_GST, A_QST, A_TOTAL = 75000, 3750, 7481, 86231
B_FEES, B_GST, B_QST, B_TOTAL = 50000, 2500, 4988, 57488


def _d(month: int, day: int, year: int = 2031) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def _plain(text: str) -> str:
    return text.replace("\N{NO-BREAK SPACE}", " ").replace("\N{NARROW NO-BREAK SPACE}", " ")


def _money(cents: int) -> str:
    return _plain(format_cents_fr(cents))


def _radiation(eid: str, day: datetime, amount: int, number: str, *,
               gst: int = 0, qst: int = 0, reason: str = "") -> dict:
    """A radiation as ``invoice.write_off_remainder`` writes it."""
    return {"id": eid, "kind": "radiation", "date": day, "amount": amount,
            "gst_amount": gst, "qst_amount": qst, "tax_claimed": bool(gst or qst),
            "reason": reason, "reverses_id": "", "credit_note_number": number,
            "created_at": day + timedelta(hours=15), "created_via": "web"}


def _cancel(eid: str, radiation: dict, day: datetime, reason: str = "") -> dict:
    """The contre_passation of *radiation*: its four figures, no number."""
    return {"id": eid, "kind": "contre_passation", "date": day,
            **{k: radiation[k] for k in ("amount", "gst_amount", "qst_amount",
                                         "tax_claimed")},
            "reason": reason, "reverses_id": radiation["id"],
            "created_at": day + timedelta(hours=15), "created_via": "web"}


R1 = _radiation("r1", _d(3, 5), A_TOTAL, "2031-NC002", gst=A_GST, qst=A_QST,
                reason="Cliente introuvable depuis janvier")
R2 = _radiation("r2", _d(2, 20), B_TOTAL, "2031-NC001",
                reason="Débiteur insolvable")
C2 = _cancel("c2", R2, _d(3, 12), reason="Paiement reçu après tout")


def _invoice(iid: str, number: str, client: str, day: datetime, *, fees: int,
             gst: int, qst: int, status: str = "payée", write_offs=None) -> dict:
    total = fees + gst + qst
    doc = {"id": iid, "invoice_number": number, "status": status, "date": day,
           "client_name": client, "dossier_id": "dos-7",
           "dossier_file_number": "2031-007", "subtotal_fees": fees,
           "subtotal_expenses": 0, "subtotal": fees, "gst_amount": gst,
           "qst_amount": qst, "total": total, "retainer_applied": 0,
           "amount_due": total, "amount_paid": 0}
    if write_offs is not None:
        doc["write_offs"] = write_offs
    return doc


INVOICE_A = _invoice("inv-a", "2031-F001", "Cliente Exemple Alpha", _d(1, 10),
                     fees=A_FEES, gst=A_GST, qst=A_QST, write_offs=[R1])
INVOICE_B = _invoice("inv-b", "2031-F002", "Client Exemple Bêta", _d(1, 15),
                     fees=B_FEES, gst=B_GST, qst=B_QST, status="en_retard",
                     write_offs=[R2, C2])
INVOICE_C = _invoice("inv-c", "2031-F003", "Cliente Exemple Gamma", _d(2, 3),
                     fees=20000, gst=1000, qst=1995, status="envoyée")


# ══════════════════════════════════════════════════════════════════════════
# 1. The derivation — pure
# ══════════════════════════════════════════════════════════════════════════


def _snapshot(*invoices) -> fs.Snapshot:
    return fs.Snapshot(invoices=[dict(i) for i in invoices])


def _journal(snapshot, slug: str, today: date = date(2031, 3, 20)) -> fs.WriteOffJournal:
    period, error = periods.parse_period(slug, today=today, books_start=fs.BOOKS_START)
    assert period is not None, error
    return fs.write_off_journal(snapshot, period, today=today)


def test_the_columns_are_one_table_and_the_money_ones_derived():
    keys = [key for key, _label, _side in fs.JOURNAL_COLUMNS]
    assert len(keys) == len(set(keys))
    assert fs.JOURNAL_MONEY_KEYS == ("bad_debt", "gst", "qst", "receivables")
    assert [side for _k, _l, side in fs.JOURNAL_COLUMNS if side] == [
        "Débit", "Débit", "Débit", "Crédit"]
    fields = set(fs.JournalEntry.__dataclass_fields__)
    assert set(fs.JOURNAL_MONEY_KEYS) <= fields
    # The sheet's geometry covers exactly the model's columns.
    assert set(sheet.GEOMETRY) == set(keys)


def test_a_radiation_closes_like_a_journal_entry():
    journal = _journal(_snapshot(INVOICE_A), "2031-03")
    assert journal.unavailable == ""
    (entry,) = journal.entries
    assert entry == fs.JournalEntry(
        invoice_id="inv-a", invoice_number="2031-F001",
        client_name="Cliente Exemple Alpha", event_id="r1", kind="radiation",
        day=date(2031, 3, 5), credit_note_number="2031-NC002",
        bad_debt=A_FEES, gst=A_GST, qst=A_QST, receivables=A_TOTAL,
        standing=True, reason="Cliente introuvable depuis janvier")
    # Débits = crédit, on every row.
    assert entry.bad_debt + entry.gst + entry.qst == entry.receivables
    assert journal.totals == {"bad_debt": A_FEES, "gst": A_GST, "qst": A_QST,
                              "receivables": A_TOTAL}


def test_a_cancellation_is_the_reverse_entry_on_its_own_day_naming_its_note():
    snap = _snapshot(INVOICE_B)
    (radiation,) = _journal(snap, "2031-02").entries
    assert (radiation.kind, radiation.day, radiation.credit_note_number) == (
        "radiation", date(2031, 2, 20), "2031-NC001")
    # Its tax was not claimed: the whole amount is the bad debt.
    assert (radiation.bad_debt, radiation.gst, radiation.qst,
            radiation.receivables) == (B_TOTAL, 0, 0, B_TOTAL)
    # Cancelled since: its credit note reads « annulée », as it stands today.
    assert radiation.standing is False
    (cancel,) = _journal(snap, "2031-03").entries
    assert (cancel.kind, cancel.day, cancel.event_id) == (
        "contre_passation", date(2031, 3, 12), "c2")
    assert cancel.credit_note_number == "2031-NC001"
    assert (cancel.bad_debt, cancel.gst, cancel.qst, cancel.receivables) == (
        -B_TOTAL, 0, 0, -B_TOTAL)
    assert cancel.standing is False
    assert cancel.reason == "Paiement reçu après tout"
    quarter = _journal(snap, "2031-T1")
    assert [e.event_id for e in quarter.entries] == ["r2", "c2"]
    assert quarter.totals == {"bad_debt": 0, "gst": 0, "qst": 0, "receivables": 0}


def test_entries_are_oldest_first_and_the_totals_sum_the_money_columns():
    journal = _journal(_snapshot(INVOICE_A, INVOICE_B, INVOICE_C), "2031-T1")
    assert [(e.day.isoformat(), e.event_id) for e in journal.entries] == [
        ("2031-02-20", "r2"), ("2031-03-05", "r1"), ("2031-03-12", "c2")]
    for key in fs.JOURNAL_MONEY_KEYS:
        assert journal.totals[key] == sum(getattr(e, key) for e in journal.entries)
    assert journal.totals == {"bad_debt": A_FEES, "gst": A_GST, "qst": A_QST,
                              "receivables": A_TOTAL}


def test_the_journal_takes_the_statements_predicate():
    """Never billed (a brouillon, an annulée), the events never count; an
    event dated before its invoice counts from the invoice's day, under
    its own date; one dated after « today » waits for its day."""
    early = _radiation("e1", _d(1, 28), 1000, "2031-NC003")
    future = _radiation("f1", _d(3, 25), 2000, "2031-NC004")
    snap = _snapshot(
        _invoice("inv-d", "2031-F004", "Brouillon Exemple", _d(1, 5), fees=1000,
                 gst=0, qst=0, status="brouillon",
                 write_offs=[_radiation("d1", _d(3, 2), 1000, "2031-NC005")]),
        _invoice("inv-e", "2031-F005", "Annulée Exemple", _d(1, 5), fees=1000,
                 gst=0, qst=0, status="annulée",
                 write_offs=[_radiation("x1", _d(3, 2), 1000, "2031-NC006")]),
        _invoice("inv-f", "2031-F006", "Datée Exemple", _d(2, 4), fees=1000,
                 gst=0, qst=0, write_offs=[early]),
        _invoice("inv-g", "2031-F007", "Future Exemple", _d(1, 5), fees=2000,
                 gst=0, qst=0, write_offs=[future]))
    assert _journal(snap, "2031-01").entries == []
    (entry,) = _journal(snap, "2031-02").entries
    assert (entry.event_id, entry.day) == ("e1", date(2031, 1, 28))
    assert _journal(snap, "2031-03").entries == []          # 25 March is ahead
    in_april = _journal(snap, "2031-03", today=date(2031, 4, 2))
    assert [e.event_id for e in in_april.entries] == ["f1"]


def test_an_in_progress_period_stops_at_today():
    journal = _journal(_snapshot(INVOICE_A), "2031-03", today=date(2031, 3, 20))
    assert journal.in_progress and journal.as_of == date(2031, 3, 20)
    closed = _journal(_snapshot(INVOICE_A), "2031-02", today=date(2031, 3, 20))
    assert not closed.in_progress and closed.as_of == date(2031, 2, 28)


@pytest.mark.parametrize("damage, sentence", [
    ({"write_offs": "pas une liste"}, "Radiations illisibles sur la facture 2031-F001"),
    ({"write_offs": [dict(R1, amount=1.5)]}, "Radiations illisibles sur la facture 2031-F001"),
    ({"amount_paid": "12,00"}, "Montant illisible (amount_paid)"),
])
def test_an_unreadable_invoice_makes_the_journal_unavailable(damage, sentence):
    """One unreadable invoice — any, even outside the period — and no
    total is computed: the statements' own reading."""
    journal = _journal(_snapshot(dict(INVOICE_A, **damage), INVOICE_B), "2031-02")
    assert journal.unavailable.startswith(sentence)
    assert journal.entries == [] and journal.totals == {}


@pytest.mark.parametrize("failure, sentence", [
    ("read", "Lecture impossible (factures) — réessayez dans un instant."),
    ("truncated", "Lecture tronquée (factures) : trop d'inscriptions pour un "
                  "calcul complet."),
])
def test_a_failed_or_truncated_read_is_unavailable_never_empty(failure, sentence):
    snap = fs.Snapshot()
    snap.failures["invoices"] = failure
    journal = _journal(snap, "2031-03")
    assert journal.unavailable == sentence
    assert journal.entries == [] and journal.totals == {}


#: Periods of the random worlds: closed ones (today far ahead) and periods
#: in progress, whose span stops at today — the span the statements take.
_CLOSED = tuple((slug, date(2033, 6, 15)) for slug in tfs._ROLLFORWARD_PERIODS)
_IN_PROGRESS = (("2032-03", date(2032, 3, 15)), ("2032-T1", date(2032, 3, 15)),
                ("2032", date(2032, 3, 15)), ("2031-08", date(2031, 8, 9)))


@pytest.mark.parametrize("seed", range(20))
def test_the_totals_are_the_statements_figures_on_random_worlds(seed):
    """By construction, on the statements' own random worlds (write-offs
    standing and cancelled, claimed or not, across period boundaries, on
    invoices never billed, dated before their invoice): the journal's totals
    are the income's « Créances irrécouvrables », the TPS/TVQ table's two
    bad-debt rows and their sum, and its rows are « Redressements »' events,
    in the same order."""
    snap = tfs._random_world(random.Random(31_0311 + seed))
    for slug, today in _CLOSED + _IN_PROGRESS:
        period = tfs._period(slug, today)
        st = fs.compute(snap, period, today=today,
                        now=datetime(2033, 6, 15, 14, tzinfo=UTC), books_start=tfs.BOOKS)
        journal = fs.write_off_journal(snap, period, today=today)
        assert st.unavailable == {} and journal.unavailable == "", (seed, slug)
        income, taxes = st.sections["resultats"], st.sections["taxes"]
        assert journal.totals["bad_debt"] == income["bad_debts"], (seed, slug)
        assert journal.totals["gst"] == taxes["bad_debt_gst"], (seed, slug)
        assert journal.totals["qst"] == taxes["bad_debt_qst"], (seed, slug)
        assert journal.totals["receivables"] == (
            income["bad_debts"] + taxes["bad_debt_gst"] + taxes["bad_debt_qst"])
        assert [e.event_id for e in journal.entries] == [
            event.id for _inv, event in st.sections["redressements"]["write_offs"]]
        for e in journal.entries:
            assert e.bad_debt + e.gst + e.qst == e.receivables
        # And as the statements PRINT them.
        view = {s["key"]: s for s in fs.present(st)["sections"]}
        printed = {r["key"]: r["values"] for s in view.values() for r in s["rows"]}
        if journal.totals["bad_debt"]:
            assert printed["creances_irrecouvrables"] == [journal.totals["bad_debt"]]
        else:
            assert "creances_irrecouvrables" not in printed
        if journal.totals["gst"] or journal.totals["qst"]:
            assert printed["tps_creances_irrecouvrables"] == [-journal.totals["gst"]]
            assert printed["tvq_creances_irrecouvrables"] == [-journal.totals["qst"]]


def test_the_random_worlds_hold_rows_of_every_kind():
    """Not vacuous: the worlds above carry radiations and contre-passations,
    standing and cancelled, inside the periods compared."""
    kinds, states = set(), set()
    for seed in range(20):
        snap = tfs._random_world(random.Random(31_0311 + seed))
        for slug, today in _CLOSED:
            journal = fs.write_off_journal(snap, tfs._period(slug, today), today=today)
            kinds |= {e.kind for e in journal.entries}
            states |= {e.standing for e in journal.entries}
    assert kinds == set(invoice_model.WRITE_OFF_KINDS) and states == {True, False}


# ══════════════════════════════════════════════════════════════════════════
# 2. The sheet — utils/journal_radiations_pdf.py
# ══════════════════════════════════════════════════════════════════════════


def _sheet_row(**over) -> dict:
    row = {"date": "2031-03-05", "credit_note_number": "2031-NC002",
           "invoice_number": "2031-F001", "client": "Cliente Exemple Alpha",
           "kind": "Radiation", "bad_debt": A_FEES, "gst": A_GST, "qst": A_QST,
           "receivables": A_TOTAL, "state": "en vigueur"}
    row.update(over)
    return row


def test_the_geometry_spends_the_sheet_and_only_the_client_clips():
    assert abs(sum(ratio for ratio, _clip in sheet.GEOMETRY.values()) - 1.0) < 1e-9
    assert [k for k, (_r, clip) in sheet.GEOMETRY.items() if clip] == ["client"]


def test_the_band_spans_the_debit_and_the_credit_columns():
    assert sheet.band_spans(fs.JOURNAL_COLUMNS) == [("Débit", 5, 7), ("Crédit", 8, 8)]


def test_every_cell_header_and_band_fits_its_column():
    """The widest values a row can hold — an eight-figure amount, negative;
    a legacy note number; « Annulation de radiation »; a four-digit credit
    note — and every heading, in the band's bold, fit without clipping."""
    widths = sheet.widths(fs.JOURNAL_COLUMNS)
    big = -12_345_678_90
    rows = [_sheet_row(),
            _sheet_row(invoice_number="2026-001-100", credit_note_number="2031-NC1000",
                       kind=invoices_routes.WRITE_OFF_KIND_LABELS["contre_passation"],
                       state=invoices_routes.CREDIT_NOTE_CANCELLED,
                       **{k: big for k in fs.JOURNAL_MONEY_KEYS})]
    for row in rows:
        for (key, _label, _side), cell, width in zip(
                fs.JOURNAL_COLUMNS, sheet.row_cells(row, fs.JOURNAL_COLUMNS, widths), widths):
            assert not cell.endswith("…"), (key, cell)
            drawn = pdfmetrics.stringWidth(cell, sheet._FONT, sheet._SIZE)
            assert drawn <= width - 2 * sheet._PAD, (key, cell, drawn, width)
    totals = sheet.totals_cells(rows * 4, fs.JOURNAL_COLUMNS, widths)
    for (key, _label, side), cell, width in zip(fs.JOURNAL_COLUMNS, totals, widths):
        if side:
            drawn = pdfmetrics.stringWidth(cell, sheet._FONT_BOLD, sheet._SIZE)
            assert drawn <= width - 2 * sheet._PAD, (key, cell, drawn, width)
    for (key, label, _side), width in zip(fs.JOURNAL_COLUMNS, widths):
        drawn = pdfmetrics.stringWidth(label, sheet._FONT_BOLD, sheet._SIZE)
        assert drawn <= width - 2 * sheet._PAD, (label, drawn, width)
    for side, first, last in sheet.band_spans(fs.JOURNAL_COLUMNS):
        drawn = pdfmetrics.stringWidth(side, sheet._FONT_BOLD, sheet._SIZE)
        assert drawn <= sum(widths[first:last + 1]) - 2 * sheet._PAD


def test_a_long_client_name_is_clipped_never_wrapped():
    widths = sheet.widths(fs.JOURNAL_COLUMNS)
    cells = sheet.row_cells(_sheet_row(client="X" * 400), fs.JOURNAL_COLUMNS, widths)
    client = cells[[k for k, _l, _s in fs.JOURNAL_COLUMNS].index("client")]
    assert client.endswith("…") and len(client) < 400


def test_each_value_lands_under_its_own_column_and_the_totals_sum_them():
    widths = sheet.widths(fs.JOURNAL_COLUMNS)
    row = _sheet_row(bad_debt=101, gst=202, qst=303, receivables=606)
    by_key = dict(zip([k for k, _l, _s in fs.JOURNAL_COLUMNS],
                      sheet.row_cells(row, fs.JOURNAL_COLUMNS, widths)))
    assert by_key == {"date": "2031-03-05", "credit_note_number": "2031-NC002",
                      "invoice_number": "2031-F001", "client": "Cliente Exemple Alpha",
                      "kind": "Radiation", "bad_debt": format_cents_fr(101),
                      "gst": format_cents_fr(202), "qst": format_cents_fr(303),
                      "receivables": format_cents_fr(606), "state": "en vigueur"}
    other = _sheet_row(bad_debt=-1, gst=-2, qst=-3, receivables=-6)
    cells = sheet.totals_cells([row, other], fs.JOURNAL_COLUMNS, widths)
    assert cells[0] == "TOTAL — 2 écritures" and cells[1:5] == [""] * 4
    assert cells[5:9] == [format_cents_fr(100), format_cents_fr(200),
                          format_cents_fr(300), format_cents_fr(600)]
    assert cells[9] == ""
    assert sheet.totals_cells([row], fs.JOURNAL_COLUMNS, widths)[0] == "TOTAL — 1 écriture"


def test_an_empty_journal_still_renders():
    resp = sheet.build_write_off_journal_pdf(
        [], columns=fs.JOURNAL_COLUMNS, subtitle_lines=["Période : mars 2031"],
        generated_at=TODAY, filename="j.pdf")
    assert resp.status_code == 200 and resp.data.startswith(b"%PDF")
    assert b"Helvetica" not in resp.data


# ══════════════════════════════════════════════════════════════════════════
# 3. The page, the CSV and the PDF — through the real routes
# ══════════════════════════════════════════════════════════════════════════


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    freeze_deadlines(monkeypatch, TODAY)
    f = install(monkeypatch, *_fake_modules())
    for inv in (INVOICE_A, INVOICE_B, INVOICE_C):
        f.seed(f"invoices/{inv['id']}", inv)
    return f


def _app() -> Flask:
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl, jsattr=jsattr,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "")
    for bp in (journal_routes.journal_radiations_bp, comptabilite_routes.comptabilite_bp,
               trust_routes.trust_bp, admin_routes.admin_bp,
               invoices_routes.invoices_bp, auth_routes.auth_bp):
        app.register_blueprint(bp)
    return app


@pytest.fixture
def client(fake):
    c = _app().test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _page(client, path: str = URL, status: int = 200, **kwargs) -> str:
    resp = client.get(path, **kwargs)
    assert resp.status_code == status, (path, resp.status_code, resp.location)
    return _plain(unescape(resp.get_data(as_text=True)))


def _rows_of(page: str) -> list[str]:
    body = page[page.index("<tbody"):page.index("</tbody>")]
    return re.findall(r"<tr>(.*?)</tr>", body, flags=re.S)


def _fail_invoice_queries(monkeypatch, fake) -> None:
    server = fake._fake_server
    real = server.run_query

    def failing(request, metadata=None, **kwargs):
        sq = request["structured_query"]._pb
        if any(f.collection_id == "invoices" for f in sq.from_):
            raise gexc.ServiceUnavailable("injected query failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", failing)


def test_the_page_opens_on_the_current_month_newest_first(client):
    page = _page(client)
    assert "Journal des radiations et intérêts" in page
    assert "Mars 2031 — Du 1er mars 2031 au 31 mars 2031" in page
    assert "Période en cours : les écritures jusqu'au 20 mars 2031 inclusivement." in page
    assert '<option value="2031-03" selected>mars 2031</option>' in page
    cancel, radiation = _rows_of(page)
    # The cancellation (12 March) first: the reverse entry, in negative,
    # naming the credit note it cancels.
    for needle in ("2031-03-12", "2031-NC001", "2031-F002", "Client Exemple Bêta",
                   "Annulation de radiation", _money(-B_TOTAL), "annulée",
                   "Paiement reçu après tout", 'href="/factures/inv-b"'):
        assert needle in cancel, needle
    for needle in ("2031-03-05", "2031-NC002", "2031-F001", "Cliente Exemple Alpha",
                   ">Radiation<", _money(A_FEES), _money(A_GST), _money(A_QST),
                   _money(A_TOTAL), "en vigueur", "Cliente introuvable depuis janvier",
                   'href="/factures/inv-a"'):
        assert needle in radiation, needle
    assert "2031-F003" not in page                      # no write-off: no row
    # The period's totals, as the statements carry them.
    for label, value in (("Débit — Créances irrécouvrables", A_FEES - B_TOTAL),
                         ("Débit — TPS à payer", A_GST), ("Débit — TVQ à payer", A_QST),
                         ("Crédit — Comptes clients", A_TOTAL - B_TOTAL)):
        assert re.search(re.escape(label) + r"</p>\s*<p[^>]*>" + re.escape(_money(value)),
                         page), label
    assert fs.JOURNAL_ENTRY_SENTENCE in page
    assert fs.JOURNAL_INTEREST_SENTENCE in page
    assert "2 écritures dans la période" in page
    assert 'href="/comptabilite/radiations/export/csv?periode=2031-03"' in page
    assert 'href="/comptabilite/radiations/export/pdf?periode=2031-03"' in page


def test_a_closed_period_reads_its_own_rows(client):
    page = _page(client, URL + "?periode=2031-02")
    assert "Arrêté au 28 février 2031." in page
    (row,) = _rows_of(page)
    assert "2031-NC001" in row and "Débiteur insolvable" in row
    assert "annulée" in row and _money(B_TOTAL) in row
    assert 'href="/comptabilite/radiations/?periode=2031-01"' in page
    assert 'href="/comptabilite/radiations/?periode=2031-03"' in page


def test_an_empty_period_says_so_in_french(client):
    page = _page(client, URL + "?periode=2031-01")
    assert "Aucune radiation ni annulation de radiation dans cette période." in page
    assert "0 écriture dans la période" in page
    assert "<tbody" not in page


@pytest.mark.parametrize("slug, refusal", [
    ("2031-13", "La période demandée est invalide"),
    ("2031-04", "Cette période n'est pas encore commencée."),
    ("2024-12", "Les livres commencent le 1er janvier 2025"),
])
@pytest.mark.parametrize("path", [URL, URL + "export/csv", URL + "export/pdf"])
def test_a_refused_period_answers_400_with_no_figure(client, slug, refusal, path):
    page = _page(client, f"{path}?periode={slug}", status=400)
    assert refusal in page
    assert "Débit — Créances irrécouvrables" not in page and "<tbody" not in page


def test_an_unknown_format_is_refused(client):
    assert client.get(URL + "export/xlsx?periode=2031-03").status_code == 400


def test_a_failed_read_says_unavailable_never_empty(client, fake, monkeypatch, caplog):
    _fail_invoice_queries(monkeypatch, fake)
    caplog.set_level(logging.ERROR, logger="pallas.unexpected")
    page = _page(client, URL + "?periode=2031-03")
    assert ("Journal indisponible — Lecture impossible (factures) — réessayez "
            "dans un instant.") in page
    assert "Aucune radiation" not in page and "<tbody" not in page
    assert "export/csv" not in page and "export/pdf" not in page
    assert "0,00 $" not in page
    # The statements' own read-failure line: ids and types only.
    lines = [r.json_fields for r in caplog.records if hasattr(r, "json_fields")]
    assert [(line["collection"], line["error_type"]) for line in lines] == [
        ("invoices", "ServiceUnavailable")]


def test_a_truncated_read_is_unavailable(client, monkeypatch):
    monkeypatch.setattr(full_reads, "FULL_READ_CAP", 2)
    page = _page(client, URL + "?periode=2031-03")
    assert "Journal indisponible — Lecture tronquée (factures)" in page
    assert "<tbody" not in page


def test_an_unreadable_write_off_array_is_unavailable(client, fake):
    fake.seed("invoices/inv-c", dict(INVOICE_C, write_offs=[{"id": "z"}]))
    page = _page(client, URL + "?periode=2031-03")
    assert "Journal indisponible — Radiations illisibles sur la facture 2031-F003" in page
    assert "<tbody" not in page


@pytest.mark.parametrize("fmt", ["csv", "pdf"])
def test_the_exports_are_refused_while_unavailable(client, fake, monkeypatch, fmt):
    _fail_invoice_queries(monkeypatch, fake)
    resp = client.get(f"{URL}export/{fmt}?periode=2031-03")
    assert resp.status_code == 302
    assert resp.location.endswith(
        "/comptabilite/radiations/?periode=2031-03&erreur=export_indisponible")
    page = _page(client, URL + "?periode=2031-03&erreur=export_indisponible")
    assert "L'exportation n'a pas été produite" in page


def test_an_unknown_error_code_shows_nothing(client):
    page = _page(client, URL + "?periode=2031-03&erreur=<b>x</b>")
    assert 'role="alert"' not in page


def test_the_csv_carries_the_whole_period_oldest_first_without_the_reason(client):
    resp = client.get(URL + "export/csv?periode=2031-T1")
    assert resp.status_code == 200 and resp.mimetype == "text/csv"
    assert 'filename="journal-radiations_2031-T1.csv"' in resp.headers["Content-Disposition"]
    text = resp.get_data(as_text=True).lstrip("\ufeff")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == [label for _key, label, _side in fs.JOURNAL_COLUMNS]
    assert rows[1:] == [
        ["2031-02-20", "2031-NC001", "2031-F002", "Client Exemple Bêta", "Radiation",
         "574.88", "0.00", "0.00", "574.88", "annulée"],
        ["2031-03-05", "2031-NC002", "2031-F001", "Cliente Exemple Alpha", "Radiation",
         "750.00", "37.50", "74.81", "862.31", "en vigueur"],
        ["2031-03-12", "2031-NC001", "2031-F002", "Client Exemple Bêta",
         "Annulation de radiation", "-574.88", "0.00", "0.00", "-574.88", "annulée"],
    ]
    for reason in ("insolvable", "introuvable", "Paiement reçu"):
        assert reason not in text


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    return _plain("\n".join(p.extract_text() or ""
                            for p in PdfReader(io.BytesIO(data)).pages))


def test_the_pdf_is_legal_landscape_font_pure_and_without_the_reason(client):
    resp = client.get(URL + "export/pdf?periode=2031-03")
    assert resp.status_code == 200 and resp.mimetype == "application/pdf"
    assert 'filename="journal-radiations_2031-03.pdf"' in resp.headers["Content-Disposition"]
    data = resp.data
    assert data.startswith(b"%PDF") and b"NotoSerif" in data and b"Helvetica" not in data
    assert b"1008" in data and b"612" in data          # legal landscape
    text = _pdf_text(data)
    for needle in ("JOURNAL DES RADIATIONS ET INTÉRÊTS",
                   "Période : mars 2031, du 1er mars 2031 au 31 mars 2031",
                   "Débit", "Crédit", "Créances irrécouvrables", "Comptes clients",
                   "2031-NC002", "2031-NC001", "Annulation de radiation",
                   _money(-B_TOTAL), "TOTAL — 2 écritures", _money(A_TOTAL - B_TOTAL),
                   "Généré le 2031-03-20 11:00 (heure de Montréal)"):
        assert needle in text, needle
    for reason in ("insolvable", "introuvable", "Paiement reçu"):
        assert reason not in text


def test_the_pages_are_fifteen_rows_and_the_bar_pins_the_period(client, fake):
    for n in range(1, 18):
        inv = _invoice(f"inv-p{n:02d}", f"2031-F1{n:02d}", f"Client Exemple {n:02d}",
                       _d(1, 6), fees=1000 + n, gst=0, qst=0,
                       write_offs=[_radiation(f"p{n:02d}", _d(3, n), 1000 + n,
                                              f"2031-NC1{n:02d}")])
        fake.seed(f"invoices/{inv['id']}", inv)
    page = _page(client, URL + "?periode=2031-03")
    first = _rows_of(page)
    assert len(first) == 15
    assert "2031-03-17" in first[0]                     # newest first
    assert "19 écritures dans la période" in page
    assert '"page": "2", "periode": "2031-03"' in page
    # The bar's request: a fragment of the rows' region, its totals the
    # period's whole.
    fragment = _page(client, URL + "?periode=2031-03&page=2",
                     headers={"HX-Request": "true"})
    assert "<html" not in fragment and "<form" not in fragment
    second = _rows_of(fragment)
    assert [re.search(r"2031-03-\d\d", r).group(0) for r in second] == [
        "2031-03-04", "2031-03-03", "2031-03-02", "2031-03-01"]
    assert "Débit — Créances irrécouvrables" in fragment


def test_every_read_is_one_read_only_transaction(client, fake):
    fake.reset_logs()
    _page(client, URL + "?periode=2031-03")
    client.get(URL + "export/csv?periode=2031-03")
    assert fake.reads_outside_transactions() == []
    assert [op for c in fake.commits for op in c.ops] == []


def test_the_routes_are_get_only_and_ask_for_a_login(fake):
    rules = [r for r in _app().url_map.iter_rules()
             if r.endpoint.startswith("journal_radiations.")]
    assert sorted(r.rule for r in rules) == [
        "/comptabilite/radiations/", "/comptabilite/radiations/export/<fmt>"]
    for rule in rules:
        assert rule.methods <= {"GET", "HEAD", "OPTIONS"}, rule
    anonymous = _app().test_client()
    for path in (URL, URL + "export/csv", URL + "export/pdf"):
        resp = anonymous.get(path + "?periode=2031-03")
        assert resp.status_code == 302 and "/auth/login" in resp.location


def test_the_hub_links_to_the_journal(client, monkeypatch):
    monkeypatch.setattr(comptabilite_routes.trust, "get_firm_trust_snapshot",
                        lambda: {"accounts": [], "total_held_cents": 0})
    monkeypatch.setattr(comptabilite_routes.al, "get_firm_admin_snapshot",
                        lambda: {"accounts": []})
    hub = _page(client, "/comptabilite/")
    assert re.search(r'<a href="/comptabilite/radiations/" class="[^"]*\bpy-3\b[^"]*">'
                     r"Journal des radiations et intérêts</a>", hub)
    page = _page(client)
    assert 'href="/comptabilite/"' in page


def test_the_markup_uses_compiled_classes_and_44_px_targets(client):
    """Change Impact item 6: every class the page renders compiles. Rule 9:
    each control is py-3 around a text-sm label (12 + 20 + 12 px) — the
    invoice link of a row too —, and the arrows p-3 around a 21 px glyph."""
    def _main(path: str) -> str:
        page = _page(client, path)
        return page[page.index("<main"):page.index("</main>")]

    main = _main(URL + "?periode=2031-T1")              # both badges
    february = _main(URL + "?periode=2031-02")          # both arrows
    empty = _main(URL + "?periode=2031-01")
    classes = {c for block in (main, february, empty)
               for attr in re.findall(r'class="([^"]+)"', block) for c in attr.split()}
    assert {"py-3", "divide-y", "bg-amber-100", "bg-gray-200", "min-w-[12rem]"} <= classes
    css = compiled._artifact(_ATHENA / "static" / "vendor").read_text(encoding="utf-8")
    selectors = compiled.compiled_selectors(css)
    assert sorted(c for c in classes if not compiled.is_compiled(c, selectors)) == []
    for label in ("Comptabilité", "Afficher", "CSV", "PDF"):
        tag = re.search(r'<(?:a|button)\b[^>]*class="([^"]*)"[^>]*>\s*(?:<span[^>]*>[^<]*'
                        r"</span>\s*)?" + re.escape(label) + r"\s*</", main)
        assert tag and {"py-3", "text-sm"} <= set(tag.group(1).split()), label
    links = re.findall(r'<a href="/factures/[^"]+" class="([^"]*)"', main)
    assert links and all({"inline-block", "py-3"} <= set(c.split()) for c in links)
    assert "text-sm" in main[main.index("<table"):main.index("<thead")]
    for name in ("Période précédente", "Période suivante"):
        tag = re.search(r'class="([^"]*)" aria-label="' + name, february)
        assert tag and "p-3" in tag.group(1).split(), name
