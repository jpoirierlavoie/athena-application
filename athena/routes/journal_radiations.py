"""« Journal des radiations et intérêts » — lot 3c of « Factures : retard,
radiation, intérêts » (the lawyer's decision B of 2026-11-11).

A DERIVED journal, stored nowhere: GET only, read-only, behind
``@login_required``, under the accounting area — ``/comptabilite/radiations/``,
its own blueprint, as the statements once lived at ``/comptabilite/etats/``
(the hub's own module reads nothing but its two snapshots) — and linked from
the Comptabilité hub. One row per write-off event of a month, a quarter or a
calendar year (``?periode=2031-03``, ``2031-T1``, ``2031``: the statements'
slugs, ``utils/periods.parse_period``), the current month by default, as
the other journals open on their latest entries. Each row is closed like a
journal entry, and the period's totals are the statements' own figures, by
construction: rows and totals are ``models/financial_statements
.write_off_journal``'s, over the invoices that ``services/financial_statements
.build_write_off_journal`` reads. This module labels them; it decides
nothing.

The page reads newest first, fifteen rows a page (``pagination.paginate``,
the journals' precedent of 2026-11-10). The period is a plain GET form and
links, as on the statements page; only the pagination bar is htmx — a
fragment of the rows' region (Rule 8), its period pinned in the bar's
``hx-vals``, never read from a form the reader may have changed. The CSV and
the PDF carry the whole period, oldest first, without the reason: free text,
on the page only, as the fee journal's exports carry none. A period whose
invoices cannot be read whole says « indisponible » with its reason, never
an empty journal, and its exports are refused — a redirect to the page with
a closed ``?erreur=`` code, the statements' way.

The labels of a kind and of a credit note's state are the invoice sheet's
(``routes/invoices``): one vocabulary for the same events. The route
computes every flag the page reads — the period in progress, its last day —
and the template compares no date (Rule 5).

No log line of its own: the read logs through the statements' reader
(« financial statements: read failed », « … read truncated », « … read
transaction failed », ``collection`` « invoices »), and the PDF builder
names a failed build by its class alone.
"""

from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, url_for

from auth import login_required
from models import financial_statements as fs
from pagination import paginate
from routes._helpers import is_htmx
from routes.invoices import (
    CREDIT_NOTE_CANCELLED,
    CREDIT_NOTE_STANDING,
    WRITE_OFF_KIND_LABELS,
)
from services import financial_statements as statements_service
from utils import deadlines, periods
from utils.format_fr import format_date_fr

journal_radiations_bp = Blueprint("journal_radiations", __name__,
                                  url_prefix="/comptabilite/radiations")
_TEMPLATE = "comptabilite/radiations.html"
_ROWS_TEMPLATE = "comptabilite/_radiations_rows.html"
#: The region the pagination bar swaps — the rows partial's own content.
_ROWS_TARGET = "#radiations-rows"
#: The exports, by their ``<fmt>``.
EXPORT_FORMATS = ("csv", "pdf")

#: The closed ``?erreur=`` codes the page shows — any other shows nothing.
_ERRORS = {
    "export_indisponible": (
        "L'exportation n'a pas été produite : le journal est indisponible "
        "pour cette période — la raison figure ci-dessous."
    ),
}


def _period_or_refusal(today):
    """``(period, "")`` for the request's ``periode`` — the current month
    when absent — or ``(None, the French refusal)``: one gate for both."""
    slug = request.args.get("periode", "") or periods.month_of(today).slug
    return periods.parse_period(slug, today=today, books_start=fs.BOOKS_START)


def _navigation(period, today) -> dict:
    nav = {"slug": period.slug, "previous": None, "next": None}
    if periods.has_previous(period, fs.BOOKS_START):
        nav["previous"] = periods.previous(period).slug
    if periods.has_next(period, today):
        nav["next"] = periods.following(period).slug
    return nav


def _capitalised(text: str) -> str:
    return text[:1].upper() + text[1:]


def _heading(journal: fs.WriteOffJournal) -> dict:
    """The period's name, its span and the last day the journal covers —
    computed here, so the template compares no date."""
    if journal.in_progress:
        as_of_line = (f"Période en cours : les écritures jusqu'au "
                      f"{format_date_fr(journal.as_of)} inclusivement.")
    else:
        as_of_line = f"Arrêté au {format_date_fr(journal.as_of)}."
    return {"period_label": _capitalised(periods.label_fr(journal.period)),
            "span": _capitalised(periods.span_fr(journal.period)),
            "as_of_line": as_of_line}


def _rows(journal: fs.WriteOffJournal) -> list[dict]:
    """The journal's entries, oldest first, as the page, the CSV and the
    PDF read them — keyed by ``fs.JOURNAL_COLUMNS``' keys, money in cents,
    the date a date-only value (``isoformat``, never ``to_mtl``), plus what
    the page alone reads: the invoice's id (its link), whether the credit
    note stands (its badge) and the reason."""
    return [{
        "date": e.day.isoformat(),
        "credit_note_number": e.credit_note_number,
        "invoice_number": e.invoice_number,
        "invoice_id": e.invoice_id,
        "client": e.client_name,
        "kind": WRITE_OFF_KIND_LABELS[e.kind],
        "bad_debt": e.bad_debt,
        "gst": e.gst,
        "qst": e.qst,
        "receivables": e.receivables,
        "state": CREDIT_NOTE_STANDING if e.standing else CREDIT_NOTE_CANCELLED,
        "standing": e.standing,
        "reason": e.reason,
    } for e in journal.entries]


def _band() -> list[tuple[str, int]]:
    """The heading band of the page's table: ``(side, colspan)`` for each
    run of columns on one side of the entry (« Débit », « Crédit », '')."""
    runs: list[list] = []
    for _key, _label, side in fs.JOURNAL_COLUMNS:
        if runs and runs[-1][0] == side:
            runs[-1][1] += 1
        else:
            runs.append([side, 1])
    return [(side, span) for side, span in runs]


def _context(journal: fs.WriteOffJournal, period) -> dict:
    rows = list(reversed(_rows(journal)))          # newest first
    rows, pagination = paginate(rows, request.args.get("page", 1, type=int))
    pagination.update(url=url_for("journal_radiations.index"),
                      target=_ROWS_TARGET, extra_vals={"periode": period.slug})
    return {
        "journal": journal,
        "heading": _heading(journal),
        "rows": rows,
        "pagination": pagination,
        "count": len(journal.entries),
        "columns": fs.JOURNAL_COLUMNS,
        "band": _band(),
        # No total over a journal that could not be read (« indisponible »).
        "totals": [] if journal.unavailable else [
            {"label": label, "side": side, "value": journal.totals[key]}
            for key, label, side in fs.JOURNAL_COLUMNS if side],
        "slug": period.slug,
        "entry_sentence": fs.JOURNAL_ENTRY_SENTENCE,
        "totals_sentence": fs.JOURNAL_TOTALS_SENTENCE,
        "interest_sentence": fs.JOURNAL_INTEREST_SENTENCE,
    }


def _refused(refusal: str, today):
    """A malformed, too early or not yet started period: 400, no figure —
    the statements' answer."""
    return render_template(_TEMPLATE, journal=None, refusal=refusal,
                           options=periods.options(today, fs.BOOKS_START),
                           nav=None, error=""), 400


@journal_radiations_bp.route("/")
@login_required
def index():
    today = deadlines.today_mtl()
    period, refusal = _period_or_refusal(today)
    if period is None:
        return _refused(refusal, today)
    journal = statements_service.build_write_off_journal(period, today=today)
    ctx = _context(journal, period)
    if is_htmx():
        return render_template(_ROWS_TEMPLATE, **ctx)
    return render_template(
        _TEMPLATE, refusal="", options=periods.options(today, fs.BOOKS_START),
        nav=_navigation(period, today),
        error=_ERRORS.get(request.args.get("erreur", ""), ""), **ctx)


@journal_radiations_bp.route("/export/<fmt>")
@login_required
def export(fmt: str):
    """The CSV or the PDF of the whole period, oldest first — refused while
    the journal is « indisponible »: an export of a failed read would read
    as an empty, complete journal."""
    if fmt not in EXPORT_FORMATS:
        return "Format non supporté", 400
    today = deadlines.today_mtl()
    period, refusal = _period_or_refusal(today)
    if period is None:
        return _refused(refusal, today)
    journal = statements_service.build_write_off_journal(period, today=today)
    if journal.unavailable:
        return redirect(url_for("journal_radiations.index", periode=period.slug,
                                erreur="export_indisponible"))
    rows = _rows(journal)
    if fmt == "csv":
        from utils.export_csv import export_csv

        return export_csv(
            rows=rows,
            columns=[(key, label) for key, label, _side in fs.JOURNAL_COLUMNS],
            filename=f"journal-radiations_{period.slug}.csv",
            cents_fields=list(fs.JOURNAL_MONEY_KEYS),
        )
    # Lazy: the builder imports reportlab.
    from utils.journal_radiations_pdf import build_write_off_journal_pdf

    heading = _heading(journal)
    return build_write_off_journal_pdf(
        rows, columns=fs.JOURNAL_COLUMNS,
        subtitle_lines=[
            f"Période : {periods.label_fr(period)}, {periods.span_fr(period)}",
            heading["as_of_line"], fs.JOURNAL_ENTRY_SENTENCE,
            fs.JOURNAL_TOTALS_SENTENCE, fs.JOURNAL_INTEREST_SENTENCE],
        generated_at=deadlines.now_utc(),
        filename=f"journal-radiations_{period.slug}.pdf",
    )
