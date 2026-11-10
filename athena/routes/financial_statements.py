"""États financiers — the page and its PDF (lot B, 2026-11-07), under the
« Finances » menu since 2026-11-10 (``/finances/``, its own entry in
``base.html``, no longer a card of the Comptabilité page).

GET only and read-only, behind ``@login_required``: the firm's income
statement, cash flow, balance sheet, equity rollforward, TPS/TVQ table and
memos for one month, quarter or calendar year (``?periode=2026-10``,
``2026-T3``, ``2026``), computed by ``models/financial_statements`` over one
read-only snapshot (``services/financial_statements``).

Deliberately NOT htmx — the declared exception to Architecture Rule 8: the
period selector is a plain GET form and prev / next are links, so every
period is a full page load. An htmx history restore carries no App Check
token (``security.py`` enforces it on htmx requests) and would get a silent
401 in production, leaving another period's figures on screen.

The slug is the one query value the figures depend on (``erreur`` only
picks a closed banner): refused in French at 400, with no figure, when it
is malformed, before the books or not yet started
(``utils/periods.parse_period``). The PDF is refused — a redirect to the
page with a closed ``?erreur=`` code — when one of the three statements is
unavailable: a statement document missing one of them would read as
complete.

The page's first address, ``/comptabilite/etats/`` (and its ``/pdf``),
answers a 302 to the new one (``etats_anciens_bp``) carrying ``periode``
alone, which the page validates as ever: a bookmark keeps working, and no
other query value is carried anywhere.
"""

from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, url_for

from auth import login_required
from models import financial_statements as fs
from services import financial_statements as statements_service
from utils import deadlines, periods
from utils.logging_setup import log_admin_ledger_event

etats_bp = Blueprint("etats_financiers", __name__, url_prefix="/finances")
#: The page's first address (2026-11-07 to 2026-11-10): redirects only.
etats_anciens_bp = Blueprint("etats_financiers_anciens", __name__,
                             url_prefix="/comptabilite/etats")
_TEMPLATE = "finances/etats.html"

#: The closed ``?erreur=`` codes the page shows — any other shows nothing.
_ERRORS = {
    "pdf_indisponible": (
        "Le PDF n'a pas été produit : un état qu'il doit contenir est "
        "indisponible pour cette période — la raison figure dans sa section, "
        "ci-dessous."
    ),
}


def _period_or_refusal(today):
    """``(period, "")`` for the request's ``periode`` — the last complete
    month when absent — or ``(None, the French refusal)``."""
    slug = request.args.get("periode", "")
    if slug == "":
        return periods.default_period(today, fs.BOOKS_START), ""
    return periods.parse_period(slug, today=today, books_start=fs.BOOKS_START)


def _navigation(period, today) -> dict:
    nav = {"slug": period.slug, "previous": None, "next": None}
    if periods.has_previous(period, fs.BOOKS_START):
        nav["previous"] = periods.previous(period).slug
    if periods.has_next(period, today):
        nav["next"] = periods.following(period).slug
    return nav


@etats_bp.route("/")
@login_required
def index():
    today = deadlines.today_mtl()
    options = periods.options(today, fs.BOOKS_START)
    period, refusal = _period_or_refusal(today)
    if period is None:
        return render_template(_TEMPLATE, view=None,
                               refusal=refusal, options=options, nav=None,
                               error=""), 400
    _statements, view, _counts = statements_service.build(
        period, today=today, now=deadlines.now_utc())
    return render_template(
        _TEMPLATE, view=view, refusal="", options=options,
        nav=_navigation(period, today),
        error=_ERRORS.get(request.args.get("erreur", ""), ""))


@etats_bp.route("/pdf")
@login_required
def pdf():
    today = deadlines.today_mtl()
    period, refusal = _period_or_refusal(today)
    if period is None:
        return render_template(_TEMPLATE, view=None,
                               refusal=refusal,
                               options=periods.options(today, fs.BOOKS_START),
                               nav=None, error=""), 400
    statements, view, counts = statements_service.build(
        period, today=today, now=deadlines.now_utc())
    if fs.pdf_refusal(statements):
        return redirect(url_for("etats_financiers.index", periode=period.slug,
                                erreur="pdf_indisponible"))
    # Lazy: the builder imports reportlab; a test spies on it here.
    from utils.cabinet import cabinet_dict
    from utils.financial_statements_pdf import build_financial_statements_pdf

    cabinet = cabinet_dict()
    firm = cabinet.get("organisation") or cabinet.get("nom") or ""
    log_admin_ledger_event("admin_export", format="pdf",
                           document="etats_financiers", period_kind=period.kind,
                           counts=counts)
    # The PDF header composes « Période : <label>, <span> »: the lower-case
    # forms, where the page's headings take the capitalised ones.
    return build_financial_statements_pdf(
        sections=view["sections"] + [view["points"]],
        period_label=periods.label_fr(period), span=periods.span_fr(period),
        as_of_line=view["as_of_line"], firm_name=firm,
        generated_at=statements.now,
        filename=f"etats-financiers_{period.slug}.pdf",
        notices=[view["computed_line"], view["floors_heading"], *view["floors"]],
    )


def _moved(endpoint: str):
    """A 302 to *endpoint*, carrying ``periode`` alone — the page refuses a
    malformed one at 400, as for any request."""
    slug = request.args.get("periode", "")
    return redirect(url_for(endpoint, periode=slug or None))


@etats_anciens_bp.route("/")
@login_required
def ancien_index():
    return _moved("etats_financiers.index")


@etats_anciens_bp.route("/pdf")
@login_required
def ancien_pdf():
    return _moved("etats_financiers.pdf")
