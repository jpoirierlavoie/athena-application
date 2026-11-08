"""Tests for utils/financial_statements_pdf.py — the « États financiers » PDF.

The module renders pre-labelled sections and computes nothing, so these
tests hand it synthetic sections (a fictional year, 2031, and invented
amounts) and read the document back with pypdf: NotoSerif only (the deploy
gate's « no Helvetica » doctrine), the header's mentions, every section,
blank-never-zero for a ``None`` value, an unavailable section that prints
its sentence and none of its figures, the Montréal-time footer on every
page, sections kept whole when they fit and flowing when they do not, the
contract refusals, and the failure path that logs the exception's type only.
"""

import io
import logging
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reportlab.platypus import Paragraph, Table  # noqa: E402

from utils import financial_statements_pdf as ffs  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402

# 03:30 UTC on March 1st is 22:30 on February 28th in Montréal (EST, UTC-5:
# daylight saving time starts on March 9th, 2031).
GENERATED_AT = datetime(2031, 3, 1, 3, 30, tzinfo=timezone.utc)
UNAVAILABLE = ("Le solde de TPS/TVQ n'a pu être établi : une écriture est "
               "incomplète.")
FAILURE_LINE = "financial statements PDF generation failed: {}"

# The no-break spaces format_cents_fr writes (U+00A0) and the narrow one
# (U+202F) read as plain spaces, in the PDF's text as in the expectations.
_SPACES = {0xA0: " ", 0x202F: " "}


def _norm(text: str) -> str:
    return text.translate(_SPACES)


def _money(cents: int) -> str:
    return _norm(format_cents_fr(cents))


def _row(label, values=(), *, level=0, style="line", note=""):
    return {"label": label, "values": list(values), "level": level,
            "style": style, "note": note}


def _section(key, title, columns, rows, *, notes=(), unavailable=""):
    return {"key": key, "title": title, "columns": list(columns),
            "rows": list(rows), "notes": list(notes),
            "unavailable": unavailable}


def _statements():
    """A one-column section, a two-column one and an unavailable one."""
    return [
        _section("resultats", "État des résultats", ["Période"], [
            _row("Produits", [None], style="heading"),
            _row("Honoraires", [1_234_567], level=1),
            _row("Débours refacturés", [23_456], level=1),
            _row("Total des produits", [1_258_023], style="subtotal"),
            _row("Charges", style="heading"),       # a heading may hold []
            _row("Loyer", [150_011], level=1),
            _row("Logiciels", [4_321], level=1,
                 note="Dont abonnements mensuels"),
            _row("Écart de change", [-12_345], level=2),
            _row("Total des charges", [141_987], style="subtotal"),
            _row("Résultat net", [1_116_036], style="total"),
        ], notes=["Les débours non refacturés sont exclus."]),
        _section("bilan", "Bilan", ["Début", "Fin"], [
            _row("Actif", style="heading"),
            _row("Encaisse", [4_512_345, 5_123_456], level=1),
            _row("Total de l'actif", [4_512_345, 5_123_456], style="total"),
        ]),
        _section("taxes", "TPS et TVQ", ["Période"], [
            _row("TPS perçue", [777_777]),
        ], notes=["Note 31415"], unavailable=UNAVAILABLE),
    ]


def _build(sections=None, **over):
    kw = dict(
        sections=_statements() if sections is None else sections,
        period_label="octobre 2031",
        span="du 1er octobre 2031 au 31 octobre 2031",
        as_of_line="Chiffres arrêtés au 31 octobre 2031.",
        firm_name="Cabinet d'essai",
        generated_at=GENERATED_AT,
        filename="etats_financiers_2031-10.pdf",
    )
    kw.update(over)
    return ffs.build_financial_statements_pdf(**kw)


def _story(sections, **over):
    kw = dict(sections=sections, period_label="octobre 2031", span="",
              as_of_line="", firm_name="")
    kw.update(over)
    return ffs._build_story(**kw)


def _pages(resp) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(resp.data))
    return [_norm(page.extract_text()) for page in reader.pages]


def _text(resp) -> str:
    return "\n".join(_pages(resp))


def _flowables(story):
    """Every flowable of the story, KeepTogether contents included."""
    for flowable in story:
        yield flowable
        inner = getattr(flowable, "_content", None)
        if inner:
            yield from _flowables(inner)


def _tables(story) -> list:
    return [f for f in _flowables(story) if isinstance(f, Table)]


def _paragraphs(cell) -> list:
    """A cell's Paragraphs: a cell is a string, a flowable or a list of
    them (reportlab turns a flowable cell into a tuple once wrapped)."""
    if isinstance(cell, Paragraph):
        return [cell]
    if isinstance(cell, (list, tuple)):
        return [p for c in cell for p in _paragraphs(c)]
    return []


def _failure_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == ffs.__name__]


# ═══════════════════════════════════════════════════════════════════════════
# The document
# ═══════════════════════════════════════════════════════════════════════════


def test_document_renders_letter_portrait_in_notoserif_only():
    from pypdf import PdfReader
    from reportlab.lib.pagesizes import LETTER

    resp = _build()
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.headers["Content-Disposition"] == (
        'attachment; filename="etats_financiers_2031-10.pdf"')
    assert resp.data.startswith(b"%PDF")
    # Font purity — the footer is drawn on every page, so an implicit
    # Helvetica drawString would be caught here.
    assert b"NotoSerif" in resp.data
    assert b"Helvetica" not in resp.data
    page = PdfReader(io.BytesIO(resp.data)).pages[0]
    assert (float(page.mediabox.width), float(page.mediabox.height)) == LETTER


def test_the_text_carries_the_header_every_section_and_its_figures():
    text = _text(_build())
    assert ffs.TITLE == "États financiers"
    assert "États financiers" in text
    assert ("États de gestion non audités — établis à partir des "
            "inscriptions de Pallas Athéna") in text
    assert "Cabinet d'essai" in text
    assert ("Période : octobre 2031, du 1er octobre 2031 au 31 octobre "
            "2031") in text
    assert "Chiffres arrêtés au 31 octobre 2031." in text
    for title in ("État des résultats", "Bilan", "TPS et TVQ"):
        assert title in text
    assert "Débours refacturés" in text
    assert "Dont abonnements mensuels" in text           # a row's note
    assert "Les débours non refacturés sont exclus." in text  # a section's
    assert "Début" in text and "Fin" in text
    assert _money(1_258_023) in text and _money(5_123_456) in text
    # A negative amount keeps format_cents_fr's leading « - ».
    assert _money(-12_345) == "-123,45 $"
    assert "-123,45 $" in text


def test_the_footer_prints_montreal_time_and_the_page_number():
    text = _text(_build())
    assert "Généré le 2031-02-28 22:30" in text
    assert "2031-03-01 03:30" not in text            # never the UTC instant
    assert "Page 1" in text


def test_a_none_value_prints_blank_never_zero():
    sections = [
        _section("resultats", "État des résultats", ["Période"], [
            _row("Honoraires", [123_456]),
            _row("Subvention à recevoir", [None]),
        ]),
        _section("bilan", "Bilan", ["Début", "Fin"], [
            _row("Comptes clients", [None, 98_765]),
        ]),
    ]
    text = _text(_build(sections))
    assert "Subvention à recevoir" in text and "Comptes clients" in text
    assert _money(98_765) in text
    # No amount of this document is zero: a None printed as one would show.
    assert "0,00 $" not in text
    first, second = _tables(_story(sections))
    assert first._cellvalues[2][1] == ""
    assert second._cellvalues[1][1] == ""


def test_an_unavailable_section_prints_its_sentence_and_none_of_its_figures():
    text = _text(_build())
    assert "TPS et TVQ" in text
    assert UNAVAILABLE in text
    assert "TPS perçue" not in text
    assert _money(777_777) not in text
    assert "Note 31415" not in text      # its notes may quote figures


def test_an_unavailable_section_needs_no_columns():
    resp = _build([_section("fideicommis", "Fidéicommis", [], [],
                            unavailable="Le solde n'a pu être établi.")])
    assert resp.status_code == 200
    assert "Le solde n'a pu être établi." in _text(resp)


def test_a_note_row_prints_its_text_across_the_width_and_never_its_values():
    rows = [
        _row("Honoraires", [123_456]),
        _row("Établis selon la méthode de caisse.", [424_242], style="note"),
    ]
    text = _text(_build([_section("resultats", "Résultats", ["Période"],
                                  rows)]))
    assert "Établis selon la méthode de caisse." in text
    assert _money(424_242) not in text
    assert ("SPAN", (0, 2), (-1, 2)) in ffs._table_commands(rows, 1)


def test_a_long_section_flows_from_page_one_with_its_title_row_repeated():
    rows = [_row(f"Poste {i:03d}", [1_000 * i + 7]) for i in range(1, 91)]
    pages = _pages(_build([_section("long", "Grand livre abrégé",
                                    ["Période"], rows)]))
    assert len(pages) >= 2
    # It starts under the header: page 1 is not left holding the header
    # alone, as KeepTogether would leave it.
    assert "Poste 001" in pages[0]
    assert "Page 1" in pages[0] and "Page 2" in pages[1]
    for page in pages[1:]:
        assert "Grand livre abrégé" in page and "Période" in page
        assert "Généré le 2031-02-28 22:30" in page
    text = "\n".join(pages)
    assert all(f"Poste {i:03d}" in text for i in range(1, 91))


def test_a_section_that_fits_on_a_page_is_never_split():
    """Sweep the first section's length across page 1's end: the second
    section starts on page 1 while it fits there, then moves WHOLE to page
    2 — never its title on one page and its last row on the next."""
    short = _section("court", "Section courte", ["Période"],
                     [_row(f"Rubrique {i}", [i * 111]) for i in range(1, 9)])
    stayed = moved = False
    for n in range(20, 41):
        first = _section("long", "Section longue", ["Période"],
                         [_row(f"Poste {i:03d}", [i * 1_001])
                          for i in range(1, n + 1)])
        pages = _pages(_build([first, short]))
        title = [i for i, p in enumerate(pages) if "Section courte" in p]
        last = [i for i, p in enumerate(pages) if "Rubrique 8" in p]
        assert len(title) == 1 and title == last, n
        end_of_first = [i for i, p in enumerate(pages)
                        if f"Poste {n:03d}" in p]
        if title == end_of_first:
            stayed = True
        else:
            moved = True
    assert stayed and moved          # the sweep crossed page 1's end


def test_a_300_character_label_wraps_and_keeps_its_figure():
    filler = "Provision pour honoraires à recevoir sur mandats en cours "
    label = (filler * 6)[:300 - len(" TERMINUS")] + " TERMINUS"
    assert len(label) == 300
    unbroken = "X" * 300                 # one word: split, never an error
    sections = [_section("bilan", "Bilan", ["Fin"], [
        _row(label, [-4_321_987], level=2), _row(unbroken, [1_234])])]
    resp = _build(sections)
    assert resp.status_code == 200
    text = _text(resp)
    assert "TERMINUS" in text
    assert _money(-4_321_987) in text and _money(1_234) in text
    (table,) = _tables(_story(sections))
    (para,) = _paragraphs(table._cellvalues[1][0])
    _, height = para.wrap(table._argW[0] - ffs._LABEL_PAD - 2 * ffs._INDENT,
                          10_000)
    assert height >= 3 * ffs._LEADING    # several lines, not one clipped


def test_text_is_escaped_never_parsed_as_markup():
    raws = ("Résultats <essai> & cie", "Frais <divers> & autres",
            "Voir <b>annexe</b>", "Note <i>brute</i> & entière",
            "Cabinet <test> & associés", "Avis <font>libre</font>")
    sections = [_section("resultats", raws[0], ["Période"], [
        _row(raws[1], [123_456], note=raws[2])], notes=[raws[3]])]
    resp = _build(sections, firm_name=raws[4], notices=[raws[5]])
    assert resp.status_code == 200
    text = _text(resp)
    for raw in raws:
        assert raw in text


def test_figure_columns_hold_the_widest_amount_of_the_document():
    from reportlab.pdfbase import pdfmetrics

    def width_of(cents):
        return pdfmetrics.stringWidth(format_cents_fr(cents), ffs._FONT_BOLD,
                                      ffs._SIZE)

    # The floor holds ten million dollars, negative and bold.
    assert width_of(-9_999_999_999) + ffs._MONEY_PAD <= ffs._MONEY_MIN_WIDTH
    big = -123_456_789_012
    sections = [
        _section("resultats", "Résultats", ["Période"],
                 [_row("Honoraires", [1_234])]),
        _section("bilan", "Bilan", ["Début", "Fin"],
                 [_row("Encaisse", [big, 5])]),
    ]
    width = ffs._money_width(sections)
    assert width_of(big) + ffs._MONEY_PAD <= width
    # One width for every figure column of every section: the last columns
    # share a right edge, and each table spans the frame exactly.
    tables = _tables(_story(sections))
    assert [t._argW[1:] for t in tables] == [[width], [width, width]]
    assert all(sum(t._argW) == pytest.approx(ffs._USABLE_WIDTH)
               for t in tables)
    assert _money(big) in _text(_build(sections))


def test_figure_columns_fit_a_dated_heading_on_one_line():
    from reportlab.pdfbase import pdfmetrics

    heading = "Au 30 septembre 2031"
    sections = [_section("bilan", "Bilan", [heading, "Au 31 décembre 2031"],
                         [_row("Encaisse", [1_234, 5_678])])]
    width = ffs._money_width(sections)
    assert width > ffs._MONEY_MIN_WIDTH          # the heading widened it
    assert (pdfmetrics.stringWidth(heading, ffs._FONT_BOLD, ffs._HEADING_SIZE)
            + ffs._MONEY_PAD <= width)
    # A heading past the cap wraps rather than squeezing the labels.
    sprawling = "Solde rapproché au dernier jour de la période " * 3
    capped = ffs._money_width([_section("x", "X", [sprawling],
                                        [_row("Ligne", [1_234])])])
    assert capped <= ffs._HEADING_FIT_MAX + ffs._MONEY_PAD + 1


def test_the_widths_assume_reportlabs_default_frame_padding():
    import inspect

    from reportlab.platypus.frames import Frame

    params = inspect.signature(Frame.__init__).parameters
    for side in ("leftPadding", "rightPadding", "topPadding",
                 "bottomPadding"):
        assert params[side].default == ffs._FRAME_PAD, side


def test_rules_sit_above_the_figures_the_total_heavier_and_bold_rows_bold():
    rows = [
        _row("Honoraires", [100, 200]),
        _row("Sous-total", [100, 200], style="subtotal"),
        _row("Total", [100, 200], style="total"),
        _row("Rubrique", style="heading", level=1),
        _row("Détail", [1, 2], level=2),
    ]
    cmds = ffs._table_commands(rows, 2)
    rules = {c[1][1]: c for c in cmds if c[0] == "LINEABOVE"}
    assert sorted(rules) == [2, 3]
    # Above the figure columns only, the total's heavier.
    assert all(c[1][0] == 1 and c[2][0] == -1 for c in rules.values())
    assert rules[3][3] > rules[2][3]
    assert ("FONTNAME", (0, 0), (-1, -1), ffs._FONT) in cmds
    assert {c[3] for c in cmds if c[0] == "FONTNAME"} == {
        ffs._FONT, ffs._FONT_BOLD}
    bold = {c[1][1] for c in cmds
            if c[0] == "FONTNAME" and c[3] == ffs._FONT_BOLD}
    assert bold == {2, 3, 4}
    indents = {c[1][1]: c[3] for c in cmds
               if c[0] == "LEFTPADDING" and c[1] == c[2]}
    assert indents == {4: ffs._INDENT, 5: 2 * ffs._INDENT}


def test_notices_print_after_the_sections():
    notice = "Avis d'essai : une écriture sans catégorie est exclue."
    text = _text(_build(notices=[notice]))
    assert notice in text
    assert text.index(notice) > text.index("TPS et TVQ")


def test_the_firm_name_prints_under_the_title_only_when_given():
    assert "Cabinet d'essai" in _text(_build())
    texts = [f.text for f in _flowables(_story([])) if isinstance(f, Paragraph)]
    assert texts[:2] == ["États financiers", "Période : octobre 2031"]
    assert "" not in texts


# ═══════════════════════════════════════════════════════════════════════════
# Refusals and the failure path
# ═══════════════════════════════════════════════════════════════════════════


_BREACHES = {
    "three columns": _section("k", "Titre", ["A", "B", "C"],
                              [_row("Ligne", [1, 2, 3])]),
    "no column": _section("k", "Titre", [], [_row("Ligne")]),
    "value count": _section("k", "Titre", ["Période"],
                            [_row("Ligne", [1, 2])]),
    "unknown style": _section("k", "Titre", ["Période"],
                              [_row("Ligne", [1], style="totals")]),
    "unknown level": _section("k", "Titre", ["Période"],
                              [_row("Ligne", [1], level=3)]),
    "boolean level": _section("k", "Titre", ["Période"],
                              [_row("Ligne", [1], level=True)]),
    "float cents": _section("k", "Titre", ["Période"],
                            [_row("Ligne", [1.5])]),
    "boolean cents": _section("k", "Titre", ["Période"],
                              [_row("Ligne", [True])]),
    "text cents": _section("k", "Titre", ["Période"],
                           [_row("Ligne", ["100"])]),
}


@pytest.mark.parametrize("breach", sorted(_BREACHES))
def test_a_section_that_breaks_the_contract_fails_the_whole_document(
        breach, caplog):
    """A figure under the wrong heading, or a float rounded on its way to
    the page, is worse than no page: the French 500, the type logged."""
    caplog.set_level(logging.WARNING, logger=ffs.__name__)
    resp = _build([_BREACHES[breach]])
    assert resp.status_code == 500
    assert resp.get_data(as_text=True) == "Erreur lors de la génération du PDF."
    assert _failure_lines(caplog) == [FAILURE_LINE.format("ValueError")]


def test_a_build_failure_answers_500_and_logs_only_the_type(monkeypatch,
                                                            caplog):
    def boom(self, *args, **kwargs):
        raise RuntimeError("Libellé confidentiel 4242")

    monkeypatch.setattr(ffs.SimpleDocTemplate, "build", boom)
    caplog.set_level(logging.WARNING, logger=ffs.__name__)
    resp = _build()
    assert resp.status_code == 500
    assert resp.mimetype == "text/plain"
    assert resp.get_data(as_text=True) == "Erreur lors de la génération du PDF."
    assert _failure_lines(caplog) == [FAILURE_LINE.format("RuntimeError")]
    assert "confidentiel" not in caplog.text and "4242" not in caplog.text
