"""« Journal des honoraires » — the Barreau-model fee journal export.

Pins the sheet's shape (legal landscape, 14 columns since lot 3a's
« Radié », no grouping band), the disbursement split the invoice document
does not store, and the one property the practitioner asked for explicitly:
a row never folds onto itself and never runs into its neighbour.
"""

import csv
import io
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

from reportlab.lib.pagesizes import LEGAL, landscape
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics

with mock.patch("google.cloud.firestore.Client"):
    import routes.invoices as invoices_routes
    from models.invoice import expense_split

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from utils import journal_pdf  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402


def _row(**over) -> dict:
    row = {
        "date": "2026-08-01",
        "reference": "2026-001",
        "client": "M. Jean Tremblay",
        "numero": "2026-001-03",
        "honoraires": 150000,
        "debours_tx": 10500,
        "debours_ntx": 5000,
        "sous_total": 165500,
        "tps": 8025,
        "tvq": 16010,
        "total": 189535,
        "recu": 0,
        "radie": 0,
        "solde": 189535,
        "annulee": False,
    }
    row.update(over)
    return row


def _widths() -> list[float]:
    usable = landscape(LEGAL)[0] - 2 * 10 * mm
    return [col.ratio * usable for col in journal_pdf.COLUMNS]


# ── The sheet's shape ───────────────────────────────────────────────────────


def test_fourteen_columns_in_the_requested_order():
    # Date · Client · N/Réf · N° de note — the Barreau model's own order;
    # « Radié » (lot 3a) just before the « Solde » that subtracts it.
    assert [c.label for c in journal_pdf.COLUMNS] == [
        "Date", "Client", "N/Réf", "N° de note", "Honoraires",
        "Débours TX", "Débours NTX", "Sous-total",
        "TPS", "TVQ", "Total", "Sommes reçues", "Radié", "Solde",
    ]


def test_headers_are_single_line():
    # The shortened labels all fit on one line now — a uniform header band.
    for col in journal_pdf.COLUMNS:
        assert "\n" not in col.label, col.label


def test_column_ratios_spend_the_whole_sheet():
    # The widths must fill the legal sheet — that is what keeps rows on one
    # line (« ajusté la largeur afin de remplir la grandeur de la feuille »).
    assert abs(sum(c.ratio for c in journal_pdf.COLUMNS) - 1.0) < 1e-9


def test_money_columns_are_the_ten_summed_ones():
    money = [c for c in journal_pdf.COLUMNS if c.money]
    assert len(money) == len(journal_pdf.MONEY_KEYS) == 10
    # MONEY_KEYS is DERIVED from COLUMNS — the totals row can only ever sum
    # the money columns that are actually printed.
    assert journal_pdf.MONEY_KEYS == tuple(c.key for c in money)
    assert journal_pdf.TEXT_COLUMN_COUNT == 14 - 10


def test_each_value_lands_under_its_own_column():
    """The net that was missing: cells are built from each column's KEY, so
    re-ordering COLUMNS can never shift a row's content sideways. Feed a row
    whose every field is distinctive and check where each one lands."""
    widths = _widths()
    row = _row(
        date="1111-11-11", client="CLIENT-MARKER", reference="REF-MARKER",
        numero="NUM-MARKER", honoraires=101, debours_tx=202, debours_ntx=303,
        sous_total=404, tps=505, tvq=606, total=707, recu=808, radie=1010,
        solde=909,
    )
    cells = journal_pdf._row_cells(row, widths)
    by_label = {
        col.label: cell for col, cell in zip(journal_pdf.COLUMNS, cells)
    }
    assert by_label["Date"] == "1111-11-11"
    assert by_label["Client"] == "CLIENT-MARKER"
    assert by_label["N/Réf"] == "REF-MARKER"
    assert by_label["N° de note"] == "NUM-MARKER"
    assert by_label["Honoraires"] == format_cents_fr(101)
    assert by_label["Débours TX"] == format_cents_fr(202)
    assert by_label["Débours NTX"] == format_cents_fr(303)
    assert by_label["Sommes reçues"] == format_cents_fr(808)
    assert by_label["Radié"] == format_cents_fr(1010)
    assert by_label["Solde"] == format_cents_fr(909)


def test_pdf_is_legal_landscape_and_font_pure():
    resp = journal_pdf.build_journal_pdf(
        [_row()], subtitle="Toutes les factures", filename="j.pdf"
    )
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data.startswith(b"%PDF")
    assert b"NotoSerif" in resp.data
    assert b"Helvetica" not in resp.data
    # Legal landscape: 1008 × 612 pt, as /MediaBox in the page dictionary.
    assert b"1008" in resp.data and b"612" in resp.data


def test_title_and_no_grouping_band():
    resp = journal_pdf.build_journal_pdf(
        [_row()], subtitle="", filename="j.pdf"
    )
    assert resp.status_code == 200
    # The Barreau sheet's top band is deliberately dropped.
    labels = [col.label for col in journal_pdf.COLUMNS]
    assert "Facturation" not in labels
    assert "DÉTAIL DE LA FACTURE" not in labels
    assert journal_pdf.TITLE == "JOURNAL DES HONORAIRES"


def test_empty_journal_still_renders():
    resp = journal_pdf.build_journal_pdf([], subtitle="", filename="j.pdf")
    assert resp.status_code == 200
    assert resp.data.startswith(b"%PDF")


# ── No row folds onto itself, none overflows ────────────────────────────────


def test_every_cell_fits_its_column_even_with_extreme_values():
    widths = _widths()
    rows = [
        _row(),
        _row(  # a very long client name and seven-figure amounts
            client="Les Entreprises Internationales Tremblay, Gagnon, "
                   "Lavoie & Associés (Québec) Limitée",
            honoraires=987_654_321, debours_tx=123_456_789,
            debours_ntx=98_765_432, sous_total=1_209_876_542,
            tps=60_493_827, tvq=120_684_784, total=1_391_055_153,
            recu=1_000_000_000, radie=1_209_876_542, solde=391_055_153,
        ),
        _row(annulee=True),
    ]
    for row in rows:
        for cell, width in zip(journal_pdf._row_cells(row, widths), widths):
            assert "\n" not in cell, cell          # never wraps
            drawn = pdfmetrics.stringWidth(
                cell, journal_pdf._FONT, journal_pdf._SIZE
            )
            assert drawn <= width - 2 * journal_pdf._PAD, (cell, drawn, width)


def test_every_header_fits_its_column():
    """Lot 3a's « Radié » narrowed the money columns (0.070 → 0.066): each
    header, in the header band's bold, must still fit its column — « Sommes
    reçues » is the widest, and a narrower share would run it into
    « Radié »."""
    for col, width in zip(journal_pdf.COLUMNS, _widths()):
        drawn = pdfmetrics.stringWidth(
            col.label, journal_pdf._FONT_BOLD, journal_pdf._SIZE
        )
        assert drawn <= width - 2 * journal_pdf._PAD, (col.label, drawn, width)


def test_long_client_name_is_clipped_not_wrapped():
    widths = _widths()
    cells = journal_pdf._row_cells(_row(client="X" * 400), widths)
    client = cells[1]          # Client is the second column now
    assert client.endswith("…")
    assert len(client) < 400


def test_client_is_the_only_clippable_column():
    """Clipping a client name is cosmetic; clipping an identifier or an
    AMOUNT in a book of account makes it false."""
    clippable = [c.label for c in journal_pdf.COLUMNS if c.clip]
    assert clippable == ["Client"]


def test_identifiers_and_amounts_are_never_clipped():
    widths = _widths()
    row = _row(
        reference="UNE-RÉFÉRENCE-DE-DOSSIER-ABSURDEMENT-LONGUE",
        numero="2026-F0001 (une note héritée très longue)",
        **{k: 999_999_999_99 for k in journal_pdf.MONEY_KEYS},
    )
    cells = journal_pdf._row_cells(row, widths)
    for col, cell in zip(journal_pdf.COLUMNS, cells):
        if col.clip:
            continue
        assert not cell.endswith("…"), (col.label, cell)
        if col.money:
            assert cell == format_cents_fr(999_999_999_99)


def test_a_legacy_voided_note_number_still_fits_its_column():
    # « 2026-F001 (ann.) » is the widest note number the sheet can hold —
    # the column is budgeted for it rather than clipping an identifier.
    widths = _widths()
    cells = journal_pdf._row_cells(
        _row(numero="2026-F001", reference="", annulee=True), widths
    )
    idx = [c.key for c in journal_pdf.COLUMNS].index("numero")
    assert cells[idx] == "2026-F001 (ann.)"
    drawn = pdfmetrics.stringWidth(
        cells[idx], journal_pdf._FONT, journal_pdf._SIZE
    )
    assert drawn <= widths[idx] - 2 * journal_pdf._PAD, (drawn, widths[idx])


def test_voided_invoice_is_marked_in_the_note_number():
    widths = _widths()
    live = journal_pdf._row_cells(_row(), widths)[3]
    void = journal_pdf._row_cells(_row(annulee=True), widths)[3]
    assert "(ann.)" in void and "(ann.)" not in live
    # Marked AFTER the prefix is dropped — « 03 (ann.) », not the whole number.
    assert void == "03 (ann.)"


# ── The note number drops the file-number prefix ────────────────────────────


def test_note_number_drops_the_dossier_prefix():
    # The N/Réf column already carries « 2026-001 ».
    assert journal_pdf._short_note_number("2026-001-03", "2026-001") == "03"
    # Past 99 the suffix widens — still all digits, still dropped.
    assert journal_pdf._short_note_number("2026-001-100", "2026-001") == "100"


def test_note_number_is_left_whole_when_the_prefix_is_not_the_file():
    # A legacy YYYY-FNNN number: the prefix is the YEAR, deducible from no
    # other column.
    assert journal_pdf._short_note_number("2026-F001", "2026-001") == "2026-F001"
    # No N/Réf: the note number is the row's only identification.
    assert journal_pdf._short_note_number("2026-F001", "") == "2026-F001"
    # The free-form file-number trap: « 2026 » is a legal file number, and a
    # naive strip would read « 2026-F001 » as « F001 ».
    assert journal_pdf._short_note_number("2026-F001", "2026") == "2026-F001"
    # A prefix/reference divergence (rename race, DAV import stray space).
    assert (
        journal_pdf._short_note_number("2025-014-01", "2026-001")
        == "2025-014-01"
    )


def test_note_number_survives_dashes_inside_the_file_number():
    # A rsplit/split("-") would fall apart here; startswith does not.
    assert journal_pdf._short_note_number("A-B-C-07", "A-B-C") == "07"


# ── Arithmetic ──────────────────────────────────────────────────────────────


def test_totals_row_sums_exactly_what_is_displayed():
    widths = _widths()
    rows = [_row(), _row(honoraires=50000, sous_total=50000, total=50000)]
    cells = journal_pdf._totals_cells(rows, widths)
    assert cells[0].startswith("TOTAL — 2 factures")
    assert cells[4] == format_cents_fr(150000 + 50000)   # honoraires
    assert cells[7] == format_cents_fr(165500 + 50000)   # sous-total


def test_totals_label_is_singular_for_one_invoice():
    assert journal_pdf._totals_cells([_row()], _widths())[0].startswith(
        "TOTAL — 1 facture"
    )


def test_expense_split_always_ties_back_to_the_stored_subtotal():
    invoice = {"subtotal_expenses": 15500}
    items = [
        {"type": "expense", "amount": 10500, "taxable": True},
        {"type": "expense", "amount": 5000, "taxable": False},
        {"type": "fee", "amount": 150000, "taxable": True},   # excluded
    ]
    tx, ntx = expense_split(invoice, items)
    assert (tx, ntx) == (10500, 5000)
    assert tx + ntx == invoice["subtotal_expenses"]


def test_expense_split_falls_back_to_taxable_when_items_are_unreadable():
    # list_line_items fails open to [] — the row must still tie to the
    # invoice's own subtotal rather than silently under-report it.
    invoice = {"subtotal_expenses": 15500}
    tx, ntx = expense_split(invoice, [])
    assert (tx, ntx) == (15500, 0)
    assert tx + ntx == invoice["subtotal_expenses"]


# ── Lot 3a: what stands written off — the journal's « Radié », the CSV ────
# A fictitious year and invented amounts (the repository is public).

UTC = timezone.utc


def _day(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def _event(eid: str, kind: str, day: int, reverses: str = "") -> dict:
    """A write-off event as the model's doors store it (the figures of a
    remainder of 712,31 $, its TPS and TVQ claimed)."""
    return {"id": eid, "kind": kind, "date": _day(2031, 3, day),
            "amount": 71231, "gst_amount": 3098, "qst_amount": 6180,
            "tax_claimed": True, "reason": "", "reverses_id": reverses,
            "created_at": _day(2031, 3, day), "created_via": "web"}


def _invoice(**over) -> dict:
    """A fee-only invoice of 862,31 $ (no line-item read for the journal)."""
    doc = {"id": "inv-b", "invoice_number": "2031-F008", "status": "envoyée",
           "dossier_file_number": "2031-004", "client_name": "Cliente Fictive",
           "date": _day(2031, 1, 6), "due_date": _day(2031, 2, 6),
           "subtotal_fees": 75000, "subtotal_expenses": 0, "subtotal": 75000,
           "gst_amount": 3750, "qst_amount": 7481, "total": 86231,
           "retainer_applied": 0, "amount_due": 86231, "amount_paid": 0}
    doc.update(over)
    return doc


def _written_off() -> dict:
    """150,00 $ paid, the remainder written off, that radiation cancelled,
    then written off again: ONE radiation stands."""
    return _invoice(
        id="inv-a", invoice_number="2031-F007", status="payée",
        date=_day(2031, 1, 3), amount_paid=15000,
        write_offs=[_event("rad-1", "radiation", 5),
                    _event("cp-1", "contre_passation", 6, reverses="rad-1"),
                    _event("rad-2", "radiation", 10)])


def test_the_journal_row_carries_what_stands_written_off():
    """« Radié » is the standing write-offs, taxes included — never the
    cancelled one, never its cancellation — and « Solde » subtracts it."""
    rows = invoices_routes._journal_rows([_written_off(), _invoice()])
    assert [(r["recu"], r["radie"], r["solde"]) for r in rows] == [
        (15000, 71231, 0), (0, 0, 86231)]
    widths = _widths()
    labels = [c.label for c in journal_pdf.COLUMNS]
    cells = dict(zip(labels, journal_pdf._row_cells(rows[0], widths)))
    assert cells["Radié"] == format_cents_fr(71231)
    assert cells["Solde"] == format_cents_fr(0)
    # The totals row sums it like every money column.
    totals = dict(zip(labels, journal_pdf._totals_cells(rows, widths)))
    assert totals["Radié"] == format_cents_fr(71231)
    assert totals["Solde"] == format_cents_fr(86231)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def csv_client(monkeypatch):
    """The real route on the shared fake Firestore, behind a session."""
    fake = install(monkeypatch, *_fake_modules())
    for doc in (_written_off(), _invoice(),
                _invoice(id="inv-c", invoice_number="2031-F009",
                         date=_day(2031, 1, 9),
                         write_offs=[{"id": "x", "kind": "remise"}])):
        fake.seed(f"invoices/{doc['id']}", doc)
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"))
    app.secret_key = "t"
    app.register_blueprint(invoices_routes.invoices_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return client


def test_the_csv_carries_radie_after_total_and_names_an_unreadable_array(csv_client):
    """The CSV has no « Solde »: « Radié » follows « Total ». An invoice
    whose write-offs cannot be read is named « illisible » — never a 0,
    never a failed export."""
    resp = csv_client.get("/factures/export/csv")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True).lstrip(chr(0xFEFF))
    header, *body = list(csv.reader(io.StringIO(text)))
    assert header == ["N° facture", "Date", "Dossier", "Client", "Sous-total",
                      "TPS", "TVQ", "Total", "Radié", "Statut"]
    by_number = {row[0]: dict(zip(header, row)) for row in body}
    assert by_number["2031-F007"]["Radié"] == "712.31"
    assert by_number["2031-F007"]["Statut"] == "Payée"
    assert by_number["2031-F008"]["Radié"] == "0.00"
    assert by_number["2031-F009"]["Radié"] == "illisible"
