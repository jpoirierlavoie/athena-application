"""« Journal des radiations et intérêts » — the write-off journal's sheet.

Lot 3c of « Factures : retard, radiation, intérêts » (the lawyer's decision
B of 2026-11-11): legal paper, landscape, a SIBLING of the « Journal des
honoraires » (``utils/journal_pdf.py``) and of the same proven shape — one
row per write-off event of the period, every cell a plain string that never
wraps, money in fr-CA, a totals row that sums exactly the money columns
printed. Its columns are ``models/financial_statements.JOURNAL_COLUMNS``,
which the route hands over (this module imports no model): the ORDER and
the labels live there alone, the geometry here (:data:`GEOMETRY`, keyed by
column; a test pins the two together). Above the headings, a band names
the side of the entry each money column stands on, « Débit » or
« Crédit »: the entry's structure is what this journal shows.

Only the client's name may be ellipsised: a date, a number or an amount
cut short is a FALSE one in a book of account, so their columns are budgeted
past the widest value they can hold (a test measures that). The write-off's
reason, free text, never reaches the sheet — as the fee journal prints none.

Font trap (documented in export_pdf.py): importing utils.export_pdf is what
registers NotoSerif; SimpleDocTemplate needs ``initialFontName``, and the
page callbacks must call ``canvas.setFont`` explicitly, or the deploy
gate's « no Helvetica » assertion fires.
"""

import io
import logging
from datetime import datetime
from xml.sax.saxutils import escape

from flask import Response
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LEGAL, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from tz import to_mtl
from utils import export_pdf as _export_pdf  # registers NotoSerif at import
from utils.format_fr import format_cents_fr

logger = logging.getLogger(__name__)

TITLE = "JOURNAL DES RADIATIONS ET INTÉRÊTS"
EMPTY_SENTENCE = "Aucune radiation ni annulation de radiation dans cette période."

#: Each column's share of the usable width, and whether it may be
#: ellipsised — keyed by the model's column keys. The ratios sum to 1, which
#: is what keeps a row on one line; the client's name alone clips.
GEOMETRY = {
    "date": (0.065, False),
    "credit_note_number": (0.085, False),
    "invoice_number": (0.080, False),
    "client": (0.220, True),
    "kind": (0.105, False),
    "bad_debt": (0.105, False),
    "gst": (0.085, False),
    "qst": (0.085, False),
    "receivables": (0.095, False),
    "state": (0.075, False),
}

_FONT = _export_pdf.FONT
_FONT_BOLD = _export_pdf.FONT_BOLD
_SIZE = 7
_PAD = 3  # left/right cell padding, mirrored in the width budget
_SIDE_MARGIN = 10 * mm

_INK = colors.HexColor("#111827")
_MUTED = colors.HexColor("#6B7280")
_BAND = colors.HexColor("#F9FAFB")
_RULE = colors.HexColor("#D1D5DB")


def usable_width() -> float:
    """The width the columns share: legal landscape, less both margins."""
    return landscape(LEGAL)[0] - 2 * _SIDE_MARGIN


def widths(columns) -> list[float]:
    """Each column's width, in *columns*' order."""
    return [GEOMETRY[key][0] * usable_width() for key, _label, _side in columns]


def _fit(text: str, width: float) -> str:
    """Clip *text* to *width*, ellipsised — never wrapped, never
    overflowing. Applied to the client's column alone (:data:`GEOMETRY`)."""
    text = str(text or "")
    usable = width - 2 * _PAD
    if pdfmetrics.stringWidth(text, _FONT, _SIZE) <= usable:
        return text
    ellipsis = "…"
    while text and pdfmetrics.stringWidth(text + ellipsis, _FONT, _SIZE) > usable:
        text = text[:-1]
    return text + ellipsis


def _cell(row: dict, key: str, side: str) -> str:
    if side:
        return format_cents_fr(int(row.get(key) or 0))
    return str(row.get(key) or "")


def row_cells(row: dict, columns, col_widths: list[float]) -> list[str]:
    """Every cell of *row*, read by its column's KEY — never positional."""
    return [_fit(_cell(row, key, side), width) if GEOMETRY[key][1]
            else _cell(row, key, side)
            for (key, _label, side), width in zip(columns, col_widths)]


def band_spans(columns) -> list[tuple[str, int, int]]:
    """``(side, first, last)`` for each run of money columns on one side of
    the entry, in order — the band above the headings."""
    spans: list[tuple[str, int, int]] = []
    for index, (_key, _label, side) in enumerate(columns):
        if not side:
            continue
        if spans and spans[-1][0] == side and spans[-1][2] == index - 1:
            spans[-1] = (side, spans[-1][1], index)
        else:
            spans.append((side, index, index))
    return spans


def totals_cells(rows: list[dict], columns, col_widths: list[float]) -> list[str]:
    """The totals row: its label across the text columns before the first
    money column, the sum of each money column — exactly what is printed —,
    and nothing under any other column."""
    lead = next(i for i, (_k, _l, side) in enumerate(columns) if side)
    label = f"TOTAL — {len(rows)} écriture{'s' if len(rows) != 1 else ''}"
    cells = [_fit(label, sum(col_widths[:lead]))] + [""] * (lead - 1)
    for key, _label, side in columns[lead:]:
        cells.append(format_cents_fr(sum(int(r.get(key) or 0) for r in rows))
                     if side else "")
    return cells


def _page_furniture(canvas, doc, generated: str) -> None:
    canvas.saveState()
    canvas.setFont(_FONT, 6.5)          # explicit — never inherit Helvetica
    canvas.setFillColor(_MUTED)
    canvas.drawString(doc.leftMargin, 10 * mm, generated)
    canvas.drawRightString(
        doc.pagesize[0] - doc.rightMargin, 10 * mm, f"Page {doc.page}"
    )
    canvas.restoreState()


def build_write_off_journal_pdf(
    rows: list[dict], *, columns, subtitle_lines: list[str],
    generated_at: datetime, filename: str,
) -> Response:
    """Render the journal. *rows* carry text and cents, keyed by the keys of
    *columns* (``models/financial_statements.JOURNAL_COLUMNS``), oldest
    first; *generated_at* is the tz-aware UTC instant of the export,
    printed in Montréal time."""
    col_widths = widths(columns)
    lead = next(i for i, (_k, _l, side) in enumerate(columns) if side)

    base = getSampleStyleSheet()
    base["Normal"].fontName = _FONT
    title_style = ParagraphStyle(
        "RadiationsTitle", parent=base["Normal"], fontName=_FONT_BOLD,
        fontSize=13, leading=16, alignment=TA_CENTER, textColor=_INK,
    )
    subtitle_style = ParagraphStyle(
        "RadiationsSubtitle", parent=base["Normal"], fontSize=8, leading=11,
        alignment=TA_CENTER, textColor=_MUTED,
    )

    band = [""] * len(columns)
    for side, first, _last in band_spans(columns):
        band[first] = side
    data: list[list[str]] = [band, [label for _key, label, _side in columns]]
    data += [row_cells(r, columns, col_widths) for r in rows]
    if rows:
        data.append(totals_cells(rows, columns, col_widths))

    last = len(data) - 1
    style_cmds = [
        ("FONTNAME", (0, 0), (-1, -1), _FONT),
        ("FONTSIZE", (0, 0), (-1, -1), _SIZE),
        ("LEADING", (0, 0), (-1, -1), _SIZE + 1.5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), _PAD),
        ("RIGHTPADDING", (0, 0), (-1, -1), _PAD),
        ("GRID", (0, 1), (-1, -1), 0.25, _RULE),
        # The band and the headings
        ("FONTNAME", (0, 0), (-1, 1), _FONT_BOLD),
        ("BACKGROUND", (0, 1), (-1, 1), _BAND),
        ("ALIGN", (0, 0), (-1, 1), "CENTER"),
    ]
    for side, first, last_col in band_spans(columns):
        style_cmds += [
            ("SPAN", (first, 0), (last_col, 0)),
            ("BACKGROUND", (first, 0), (last_col, 0), _BAND),
            ("BOX", (first, 0), (last_col, 0), 0.25, _RULE),
        ]
    for idx, (_key, _label, side) in enumerate(columns):
        if side:
            style_cmds.append(("ALIGN", (idx, 2), (idx, -1), "RIGHT"))
    if rows:
        style_cmds += [
            ("FONTNAME", (0, last), (-1, last), _FONT_BOLD),
            ("BACKGROUND", (0, last), (-1, last), _BAND),
            ("SPAN", (0, last), (lead - 1, last)),
            ("ALIGN", (0, last), (0, last), "LEFT"),
        ]

    table = Table(data, colWidths=col_widths, repeatRows=2)
    table.setStyle(TableStyle(style_cmds))

    story = [Paragraph(escape(TITLE), title_style), Spacer(1, 4)]
    story += [Paragraph(escape(line), subtitle_style) for line in subtitle_lines if line]
    story += [Spacer(1, 8), table]
    if not rows:
        story += [Spacer(1, 10), Paragraph(escape(EMPTY_SENTENCE), subtitle_style)]

    generated = ("Généré le " + to_mtl(generated_at).strftime("%Y-%m-%d %H:%M")
                 + " (heure de Montréal)")
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(LEGAL),
        topMargin=12 * mm,
        bottomMargin=16 * mm,
        leftMargin=_SIDE_MARGIN,
        rightMargin=_SIDE_MARGIN,
        title=TITLE,
        initialFontName=_FONT,
    )
    try:
        doc.build(
            story,
            onFirstPage=lambda c, d: _page_furniture(c, d, generated),
            onLaterPages=lambda c, d: _page_furniture(c, d, generated),
        )
    except Exception as exc:
        # Never the message — it can embed a client's name.
        logger.warning("write-off journal PDF generation failed: %s",
                       type(exc).__name__)
        buffer.close()
        return Response(
            "Erreur lors de la génération du PDF.",
            status=500,
            mimetype="text/plain; charset=utf-8",
        )
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
