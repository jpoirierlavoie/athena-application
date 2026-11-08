"""« États financiers » — the firm's management statements for one period.

LETTER portrait, NotoSerif only. The module RENDERS what it is handed and
computes nothing: the sections arrive pre-labelled and pre-ordered, their
amounts in integer cents, and it imports no model. Its rules are the
page's own:

* a section is ``{"key", "title", "columns", "rows", "notes",
  "unavailable"}``: one or two figure columns (« Période », or « Début » /
  « Fin »), its rows, the notes printed under it, and ``unavailable``, a
  French sentence when the section could not be established. That sentence
  prints IN PLACE of the rows and of the notes (a note may quote a figure),
  and such a section needs no columns: a statement that could not be
  computed says so, it never prints a part of itself or a zero;
* a row is ``{"label", "values", "level", "style", "note"}``: one value per
  column, in cents, and ``None`` prints BLANK, never « 0,00 $ » (« sans
  objet » and « nul » are different claims); ``level`` (0, 1 or 2) indents
  the label; ``style`` is ``line``, ``heading`` (bold, no figures: its
  ``values`` may be empty), ``subtotal`` (bold, a thin rule above the
  figures), ``total`` (bold, a heavier rule) or ``note`` (small muted text
  across the width, its values ignored); ``note`` is a small line printed
  under the label;
* what breaks that contract (a section without one or two columns; a row
  with an unknown style or level, a value count other than the column
  count, a value that is not integer cents) fails the whole document with
  the French 500 below: a figure printed under the wrong heading, or a
  float rounded on its way to the page, is worse than no page.

The figure columns are as wide as the widest amount of the whole document
(or column heading, within reason), never narrower than
``_MONEY_MIN_WIDTH``, so an amount is never clipped,
wrapped or ellipsised (a money Paragraph cannot break at its no-break
spaces, and ``splitLongWords`` is off), and every section's last column
shares one right edge. The label column takes the rest and wraps. A
section that fits on one page is never split across two; a longer one
flows from where it starts, and its title sits in its table's first row,
with the column headings, so both repeat at the top of every page it runs
onto.

Font trap (documented in export_pdf.py): importing utils.export_pdf is what
registers NotoSerif; SimpleDocTemplate needs ``initialFontName``, every
table style names its FONTNAME, and the footer calls ``canvas.setFont``
explicitly, or the « no Helvetica » assertion fires. NotoSerif has no
comparison signs, arrows or check marks: no string here uses them.
"""

import io
import logging
import math
from datetime import datetime
from functools import partial
from typing import Optional
from xml.sax.saxutils import escape

from flask import Response
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import (
    CondPageBreak,
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from tz import to_mtl
from utils import export_pdf as _export_pdf  # registers NotoSerif at import
from utils.format_fr import format_cents_fr

logger = logging.getLogger(__name__)

TITLE = "États financiers"
UNAUDITED_MENTION = (
    "États de gestion non audités — établis à partir des inscriptions de "
    "Pallas Athéna"
)

#: The row styles, and the ones that print bold.
ROW_STYLES = ("line", "subtotal", "total", "heading", "note")
BOLD_STYLES = frozenset({"heading", "subtotal", "total"})
#: The label indentation levels.
ROW_LEVELS = (0, 1, 2)
#: A section prints one or two figure columns.
MAX_COLUMNS = 2

_FONT = _export_pdf.FONT
_FONT_BOLD = _export_pdf.FONT_BOLD

_INK = colors.HexColor("#111827")
_MUTED = colors.HexColor("#6B7280")
_RULE = colors.HexColor("#D1D5DB")

_SIDE = 18 * mm
_TOP = 18 * mm
_BOTTOM = 18 * mm
# SimpleDocTemplate's frame keeps reportlab's default padding (6 points a
# side): a table as wide as the margins would overhang it, centred, by 6
# points on each side. The test suite pins that default.
_FRAME_PAD = 6
_USABLE_WIDTH = LETTER[0] - 2 * _SIDE - 2 * _FRAME_PAD
_FRAME_HEIGHT = LETTER[1] - _TOP - _BOTTOM - 2 * _FRAME_PAD

_SIZE = 9              # labels and figures
_LEADING = 11.5
_SMALL = 7.5           # notes, row notes, notices
_SMALL_LEADING = 9.5
_INDENT = 12           # points per label level
_LABEL_PAD = 6         # right padding of the label column
_MONEY_PAD = 6         # left padding of a figure column (right padding: 0)
# Holds « -99 999 999,99 $ » in bold at _SIZE with its padding; a wider
# amount widens every figure column of the document.
_MONEY_MIN_WIDTH = 84
_HEADING_SIZE = 8      # the column headings, bold
# A column heading up to this wide (« Au 30 septembre 2026 ») widens the
# figure columns to stay on one line; a longer one wraps instead of
# squeezing the labels.
_HEADING_FIT_MAX = 130
_SUBTOTAL_RULE = 0.5
_TOTAL_RULE = 1.2
# A section taller than a page starts on the current page only when this
# much room is left: its title row and a few rows, never a lone heading.
_LONG_SECTION_MIN_START = 6 * (_LEADING + 4)


def _text(value) -> str:
    return "" if value is None else str(value)


def _styles() -> dict:
    base = getSampleStyleSheet()
    base["Normal"].fontName = _FONT

    def style(name: str, **kw) -> ParagraphStyle:
        kw.setdefault("textColor", _INK)
        return ParagraphStyle(f"FinStat{name}", parent=base["Normal"], **kw)

    return {
        "title": style("Title", fontName=_FONT_BOLD, fontSize=16, leading=20),
        "firm": style("Firm", fontSize=11, leading=14),
        "period": style("Period", fontName=_FONT_BOLD, fontSize=10,
                        leading=13),
        "meta": style("Meta", fontSize=9, leading=12),
        "mention": style("Mention", fontSize=8, leading=10.5,
                         textColor=_MUTED),
        "section": style("Section", fontName=_FONT_BOLD, fontSize=11,
                         leading=14),
        "column": style("Column", fontName=_FONT_BOLD, fontSize=_HEADING_SIZE,
                        leading=10, textColor=_MUTED, alignment=TA_RIGHT),
        "label": style("Label", fontSize=_SIZE, leading=_LEADING),
        "label_bold": style("LabelBold", fontName=_FONT_BOLD,
                            fontSize=_SIZE, leading=_LEADING),
        "money": style("Money", fontSize=_SIZE, leading=_LEADING,
                       alignment=TA_RIGHT, splitLongWords=0),
        "money_bold": style("MoneyBold", fontName=_FONT_BOLD,
                            fontSize=_SIZE, leading=_LEADING,
                            alignment=TA_RIGHT, splitLongWords=0),
        "small": style("Small", fontSize=_SMALL, leading=_SMALL_LEADING,
                       textColor=_MUTED),
        "unavailable": style("Unavailable", fontSize=_SIZE,
                             leading=_LEADING),
    }


def _check_section(section: dict) -> None:
    """Raise ``ValueError`` on a section this page cannot print faithfully.

    The message names the section's key and the row's index, never a label
    or an amount; it is not logged either way (the failure path logs the
    exception's type only).
    """
    key = section.get("key")
    if _text(section.get("unavailable")):
        return          # only its title and that sentence print
    columns = section.get("columns")
    if (not isinstance(columns, (list, tuple))
            or not 1 <= len(columns) <= MAX_COLUMNS):
        raise ValueError(f"section {key!r}: one or two columns expected")
    for index, row in enumerate(section.get("rows") or []):
        style = row.get("style")
        if style not in ROW_STYLES:
            raise ValueError(f"section {key!r}, row {index}: unknown style")
        level = row.get("level", 0)
        if (isinstance(level, bool) or not isinstance(level, int)
                or level not in ROW_LEVELS):
            raise ValueError(f"section {key!r}, row {index}: unknown level")
        if style == "note":
            continue            # its values are ignored
        values = row.get("values") or []
        if style == "heading" and not values:
            continue
        if len(values) != len(columns):
            raise ValueError(
                f"section {key!r}, row {index}: {len(values)} values for "
                f"{len(columns)} columns"
            )
        for value in values:
            if value is not None and (isinstance(value, bool)
                                      or not isinstance(value, int)):
                raise ValueError(
                    f"section {key!r}, row {index}: integer cents expected"
                )


def _money_width(sections: list[dict]) -> float:
    """The one width of every figure column: the widest amount the document
    prints, in bold, or column heading (up to ``_HEADING_FIT_MAX``), plus
    its padding — never under ``_MONEY_MIN_WIDTH``."""
    widest = 0.0
    for section in sections:
        if _text(section.get("unavailable")):
            continue
        for heading in section.get("columns") or []:
            widest = max(widest, min(_HEADING_FIT_MAX, pdfmetrics.stringWidth(
                _text(heading), _FONT_BOLD, _HEADING_SIZE)))
        for row in section.get("rows") or []:
            if row.get("style") == "note":
                continue
            for value in row.get("values") or []:
                if value is not None:
                    widest = max(widest, pdfmetrics.stringWidth(
                        format_cents_fr(value), _FONT_BOLD, _SIZE))
    return max(_MONEY_MIN_WIDTH, math.ceil(widest) + _MONEY_PAD + 1)


def _label_cell(row: dict, styles: dict):
    """The label, and the row's note under it when it has one."""
    if row["style"] == "note":
        weight = styles["small"]
    elif row["style"] in BOLD_STYLES:
        weight = styles["label_bold"]
    else:
        weight = styles["label"]
    label = Paragraph(escape(_text(row.get("label"))), weight)
    note = _text(row.get("note"))
    if not note:
        return label
    return [label, Paragraph(escape(note), styles["small"])]


def _value_cells(row: dict, column_count: int, styles: dict) -> list:
    """One cell per column: the amount, or BLANK for ``None`` — never
    « 0,00 $ ». A note row's values are ignored; a heading's may be empty."""
    values = row.get("values") or []
    if row["style"] == "note" or not values:
        return [""] * column_count
    weight = styles["money_bold" if row["style"] in BOLD_STYLES else "money"]
    return ["" if value is None
            else Paragraph(escape(format_cents_fr(value)), weight)
            for value in values]


def _table_commands(rows: list[dict], column_count: int) -> list[tuple]:
    """The section table's style: row 0 holds the title and the column
    headings, row ``i + 1`` holds ``rows[i]``. Every FONTNAME is explicit
    (the cell style's default would select Helvetica before each cell)."""
    cmds: list[tuple] = [
        ("FONTNAME", (0, 0), (-1, -1), _FONT),
        ("FONTSIZE", (0, 0), (-1, -1), _SIZE),
        ("TEXTCOLOR", (0, 0), (-1, -1), _INK),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("VALIGN", (0, 0), (-1, 0), "BOTTOM"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (0, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, -1), _LABEL_PAD),
        ("LEFTPADDING", (1, 0), (-1, -1), _MONEY_PAD),
        ("RIGHTPADDING", (1, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, 0), 0),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, _RULE),
    ]
    if rows:
        cmds.append(("TOPPADDING", (0, 1), (-1, 1), 4))
    for offset, row in enumerate(rows):
        r = offset + 1
        style = row["style"]
        level = row.get("level", 0)
        if level:
            cmds.append(("LEFTPADDING", (0, r), (0, r), level * _INDENT))
        if style in BOLD_STYLES:
            cmds.append(("FONTNAME", (0, r), (-1, r), _FONT_BOLD))
        if style == "heading" and offset:
            cmds.append(("TOPPADDING", (0, r), (-1, r), 7))
        elif style == "subtotal":
            cmds += [
                ("TOPPADDING", (0, r), (-1, r), 4),
                ("LINEABOVE", (1, r), (-1, r), _SUBTOTAL_RULE, _INK),
            ]
        elif style == "total":
            cmds += [
                ("TOPPADDING", (0, r), (-1, r), 5),
                ("LINEABOVE", (1, r), (-1, r), _TOTAL_RULE, _INK),
            ]
        elif style == "note":
            cmds.append(("SPAN", (0, r), (-1, r)))
    return cmds


def _section_flowables(section: dict, money_width: float, styles: dict,
                       usable_width: float) -> list:
    title = Paragraph(escape(_text(section.get("title"))), styles["section"])
    unavailable = _text(section.get("unavailable"))
    if unavailable:
        # The title, then the sentence: never a figure, never a note.
        return [
            title,
            HRFlowable(width="100%", thickness=0.5, color=_RULE,
                       spaceBefore=4, spaceAfter=4),
            Paragraph(escape(unavailable), styles["unavailable"]),
        ]
    columns = list(section["columns"])
    rows = list(section.get("rows") or [])
    widths = ([usable_width - len(columns) * money_width]
              + [money_width] * len(columns))
    data = [[title] + [Paragraph(escape(_text(c)), styles["column"])
                       for c in columns]]
    for row in rows:
        data.append([_label_cell(row, styles)]
                    + _value_cells(row, len(columns), styles))
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle(_table_commands(rows, len(columns))))
    flow: list = [table]
    for note in section.get("notes") or []:
        flow += [Spacer(1, 3), Paragraph(escape(_text(note)), styles["small"])]
    return flow


def _kept_together(flow: list, usable_width: float) -> list:
    """A section that fits on one page is never split: KeepTogether moves
    it whole to the next page. One taller than a page would be moved to a
    fresh page all the same (KeepTogether's own rule), leaving the page
    before it half empty — page 1 holding only the header, for a long
    first section — so it flows instead, its table repeating its title row
    on every page, once a few rows' room is left."""
    height = sum(
        f.wrap(usable_width, _FRAME_HEIGHT)[1]
        + f.getSpaceBefore() + f.getSpaceAfter()
        for f in flow
    )
    if height <= _FRAME_HEIGHT:
        return [KeepTogether(flow)]
    return [CondPageBreak(_LONG_SECTION_MIN_START)] + flow


def _build_story(
    *, sections: list[dict], period_label: str, span: str, as_of_line: str,
    firm_name: str, notices: Optional[list[str]] = None,
    usable_width: float = _USABLE_WIDTH,
) -> list:
    """The flowable list — separate from doc.build so tests can inspect it
    (PDF streams are compressed). Raises ``ValueError`` on a section that
    breaks the contract, before anything is laid out."""
    for section in sections:
        _check_section(section)
    styles = _styles()

    story: list = [Paragraph(escape(TITLE), styles["title"])]
    if firm_name:
        story += [Spacer(1, 2), Paragraph(escape(firm_name), styles["firm"])]
    # periods.label_fr and span_fr are lower case (« octobre 2026 », « du 1er
    # octobre 2026 au 31 octobre 2026 »): they read as one line after
    # « Période : », each kept verbatim.
    period = ", ".join(part for part in (period_label, span) if part)
    if period:
        story += [Spacer(1, 6),
                  Paragraph(escape(f"Période : {period}"), styles["period"])]
    if as_of_line:
        story += [Spacer(1, 1), Paragraph(escape(as_of_line), styles["meta"])]
    story += [
        Spacer(1, 4),
        Paragraph(escape(UNAUDITED_MENTION), styles["mention"]),
        HRFlowable(width="100%", thickness=0.5, color=_RULE,
                   spaceBefore=8, spaceAfter=2),
    ]

    money_width = _money_width(sections)
    for section in sections:
        story.append(Spacer(1, 12))
        story += _kept_together(
            _section_flowables(section, money_width, styles, usable_width),
            usable_width,
        )

    if notices:
        story.append(Spacer(1, 10))
        for notice in notices:
            story += [Spacer(1, 4),
                      Paragraph(escape(_text(notice)), styles["small"])]
    return story


def _footer(canvas, doc, generated: str) -> None:
    """« Généré le … » left, « Page N » right, on the content's edges.
    setFont is EXPLICIT — a drawString without it would select Helvetica."""
    canvas.saveState()
    canvas.setFont(_FONT, 7)
    canvas.setFillColor(_MUTED)
    canvas.drawString(doc.leftMargin + _FRAME_PAD, 10 * mm, generated)
    canvas.drawRightString(
        doc.pagesize[0] - doc.rightMargin - _FRAME_PAD, 10 * mm,
        f"Page {doc.page}",
    )
    canvas.restoreState()


def build_financial_statements_pdf(
    *, sections: list[dict], period_label: str, span: str, as_of_line: str,
    firm_name: str, generated_at: datetime, filename: str,
    notices: Optional[list[str]] = None,
) -> Response:
    """Render the statements of one period.

    *sections* follow the module docstring's contract, in print order.
    *period_label* and *span* print as « Période : {period_label}, {span} »,
    *as_of_line* under them, *firm_name* under the title when non-empty.
    *generated_at* is the tz-aware UTC instant of the export, printed in
    Montréal time in every page's footer. *notices* print after the
    sections, small and muted.
    """
    buffer = io.BytesIO()
    try:
        story = _build_story(
            sections=sections, period_label=period_label, span=span,
            as_of_line=as_of_line, firm_name=firm_name, notices=notices,
        )
        generated = "Généré le " + to_mtl(generated_at).strftime(
            "%Y-%m-%d %H:%M")
        doc = SimpleDocTemplate(
            buffer,
            pagesize=LETTER,
            topMargin=_TOP,
            bottomMargin=_BOTTOM,
            leftMargin=_SIDE,
            rightMargin=_SIDE,
            title=TITLE,
            initialFontName=_FONT,
        )
        footer = partial(_footer, generated=generated)
        doc.build(story, onFirstPage=footer, onLaterPages=footer)
    except Exception as exc:
        # Never log the message — it can embed labels or amounts.
        logger.warning(
            "financial statements PDF generation failed: %s",
            type(exc).__name__,
        )
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
