"""Shared route-layer helpers (August 2026 audit consolidation).

Blueprint-free on purpose: nothing here registers a route, touches ``g``,
or imports ``models`` at module level (model imports stay lazy inside the
functions so importing this module never constructs the Firestore client).

Consolidates helpers the blueprints had each copy-pasted — and, in two
cases, had already let DRIFT apart (the audit's motivating findings):

* ``parse_date_input`` — 8 module-level ``_parse_date`` copies plus a
  NESTED re-copy inside ``routes/invoices._export_filters``.
* ``is_htmx`` — 13 identical copies.
* ``dossier_search_fragment`` — the guard/query/bound/empty-state of the
  7 dossier-autocomplete endpoints (each blueprint keeps its row builder:
  the per-site delta — an extra ``data-`` attribute, an hx- button — is
  the part that legitimately differs, and trust's ``data-clients`` row
  literal is pinned by a source grep in ``tests/test_trust.py``).
* ``enrich_dossier_labels`` — 4 copies of ``_enrich_dossier_info`` with
  THREE accidentally-divergent behaviors for an unresolvable dossier_id.
  The divergence is now a declared parameter: ``strict=True`` refuses
  (the notes doctrine — an unresolvable id must never silently reclass a
  note under « Général »); ``strict=False`` blanks the id to
  ``blank_value`` (tasks store ``None`` for « no dossier », hearings
  ``""`` — both falsy, per the ``collection_for`` routing rule).

And the answer of a POST (the refusals lot, 2026-11-06), which a dozen
blueprints had each hand-written — some of them wrongly:

* ``redirect_after_post`` — Rule 8's redirect: a plain form gets a 302,
  htmx a 200 carrying ``HX-Redirect`` and no ``Location`` (the browser's
  XHR follows a 302 by itself, so htmx never saw a header set on it, and
  htmx swaps no 4xx). The ONE place the routes set ``HX-Redirect``
  (``tests/test_post_refusals.py``, guard b).
* ``with_query`` — a banner merged into a target's query string, percent-
  encoded (never ``markupsafe.escape``, which cut « n'a » at its
  apostrophe), moved here from ``routes/trust._redirect_with``.
* ``counterparty_suggestions`` — the counterparty field's suggestions, for
  both registers' entry forms (lot 6 of 2026-11-11).

Also home to ``theme_context``, the template context processor of the
display theme: ``main.py`` registers it, and so can a test's bare app, with
no import of ``main``.
"""

from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import (
    Response, has_request_context, make_response, redirect, render_template, request,
)
from markupsafe import escape
from werkzeug.urls import iri_to_uri

from utils.theme import COLOR_SCHEMES, THEME_COOKIE, theme_from


def is_htmx() -> bool:
    """True when the request came from htmx (fragment expected)."""
    return request.headers.get("HX-Request") == "true"


def with_query(target: str, **params: str) -> str:
    """``target`` with ``params`` MERGED into its query string.

    A key named in ``params`` REPLACES the target's own (and is dropped
    when its value is empty, so a stale ``?erreur=`` riding in a
    ``return_to`` never survives); the other keys and the ``#fragment``
    are kept. Everything is percent-encoded — never HTML-escaped: an
    apostrophe travels whole. With no ``params`` the target is returned
    unchanged. ``target`` must already be internal (``url_for``, or a
    value read through ``security.safe_internal_redirect``)."""
    if not params:
        return target
    parts = urlsplit(target)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in params]
    kept += [(k, v) for k, v in params.items() if v]
    return urlunsplit(parts._replace(query=urlencode(kept)))


def redirect_after_post(target: str, *, follow: bool = False) -> Response:
    """The answer of a POST that lands on ``target`` (Rule 8).

    A plain form gets a 302. htmx gets a 200 carrying ``HX-Redirect`` and
    no ``Location``: the browser's XHR follows a 302 by itself, so a header
    set on one never reaches htmx, and htmx swaps no 4xx fragment. The
    header value is ``iri_to_uri``'d, as Werkzeug does for ``Location``:
    a character past U+00FF (a ``return_to`` naming « ā ») cannot ride a
    header, and the server would fail the answer after the write had
    committed.

    ``follow=True`` keeps the 302 under htmx too: the XHR follows it with
    its ``HX-Request`` header, and the target view answers the very
    fragment the form's ``hx-target`` expects — an in-place swap, where
    ``HX-Redirect`` would reload the whole page: the task list's checkbox,
    and the document browser's folder forms should htmx ever post them
    (rendered inside Alpine ``x-if`` templates htmx never processes, they
    post without it today). ``tests/test_post_refusals.py`` pins the views
    that pass it.
    """
    if is_htmx() and not follow:
        return Response(status=200, headers={"HX-Redirect": iri_to_uri(target)})
    return redirect(target)


def theme_context() -> dict[str, str]:
    """``ui_theme`` and ``ui_color_scheme`` for ``<html data-theme>`` and the
    color-scheme meta of ``base.html`` and ``auth/login.html``.

    Request-bound: reads only the theme cookie, never Firestore, and never
    raises — the 404, 500 and CSRF pages render through it. Outside a request
    (a template rendered from service code) there is no cookie to read, so
    the default applies. The keys are distinctive on purpose: a value passed
    to ``render_template`` under the same name would silently win.
    """
    raw = request.cookies.get(THEME_COOKIE) if has_request_context() else None
    theme = theme_from(raw)
    return {"ui_theme": theme, "ui_color_scheme": COLOR_SCHEMES[theme]}


def parse_date_input(value: str) -> Optional[datetime]:
    """Parse an HTML date input (YYYY-MM-DD) into a UTC datetime.

    Midnight UTC — the repo's date-only storage convention (render such
    values with ``strftime``/``date_str``, never ``to_mtl``).
    """
    if not value or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


_SEARCH_TOO_SHORT = (
    '<div class="px-3 py-2 text-sm text-gray-500">'
    "Tapez au moins 2 caractères…</div>"
)
_SEARCH_NO_MATCH = (
    '<div class="px-3 py-2 text-sm text-gray-500">Aucun dossier trouvé</div>'
)
_SEARCH_LIST_CLASS = "divide-y divide-gray-100"


def standard_dossier_row(d: dict, extra_attrs: str = "") -> str:
    """The default autocomplete <li>: data- attributes + n° / titre spans.

    ``extra_attrs`` is a pre-escaped attribute string appended to the <li>
    (e.g. time_expenses' ``data-dossier-rate``).
    """
    dossier_id = escape(d["id"])
    file_number = escape(d.get("file_number", ""))
    title = escape(d.get("title", ""))
    return (
        f'<li class="px-3 py-2 cursor-pointer hover:bg-gray-50 text-sm"'
        f' data-dossier-id="{dossier_id}"'
        f' data-dossier-file-number="{file_number}"'
        f' data-dossier-title="{title}"{extra_attrs}>'
        f'<span class="font-medium text-gray-900">{file_number}</span>'
        f'<span class="text-gray-500 ml-1">{title}</span></li>'
    )


def dossier_search_fragment(
    q: str,
    row_html: Callable[[dict], str] = standard_dossier_row,
    *,
    list_class: str = _SEARCH_LIST_CLASS,
) -> str:
    """Shared body of the dossier-autocomplete endpoints.

    Owns the ≥2-char guard, the bounded ``list_dossiers(search=q)[:10]``
    query, the two French empty states and the <ul> wrapper; ``row_html``
    builds one row (fragments bypass Jinja autoescaping — escape every
    interpolated value with ``markupsafe.escape``).
    """
    from models.dossier import list_dossiers

    q = (q or "").strip()
    if len(q) < 2:
        return _SEARCH_TOO_SHORT
    dossiers = list_dossiers(search=q)[:10]
    if not dossiers:
        return _SEARCH_NO_MATCH
    parts = [f'<ul class="{list_class}">']
    parts.extend(row_html(d) for d in dossiers)
    parts.append("</ul>")
    return "\n".join(parts)


def enrich_dossier_labels(
    data: dict,
    *,
    strict: bool = False,
    blank_value: Optional[str] = "",
    not_found_error: str = "Dossier introuvable. Choisissez-le depuis la recherche.",
    resolver: Optional[Callable[[str], Optional[dict]]] = None,
) -> tuple[dict, list[str]]:
    """Attach the denormalized dossier labels; return ``(data, errors)``.

    ``strict=True``: an unresolvable ``dossier_id`` REFUSES with
    ``not_found_error`` (the notes doctrine — blanking would silently
    reclass the record under « Général »). ``strict=False``: it is blanked
    to ``blank_value`` along with the labels, so no stale label lingers.
    An EMPTY id is never an error — it means « no dossier », normalized to
    ``blank_value`` (``strict`` keeps the empty string as-is).

    ``resolver``: the calling blueprint passes its OWN module-global
    ``get_dossier`` so the tests' monkeypatch seam
    (``<routes module>.get_dossier``) keeps intercepting; the default is
    the model function (lazy import — never at module load).
    """
    if resolver is None:
        from models.dossier import get_dossier as resolver

    dossier_id = (data.get("dossier_id") or "").strip()
    data["dossier_id"] = dossier_id if strict else (dossier_id or blank_value)
    if not dossier_id:
        data["dossier_file_number"] = ""
        data["dossier_title"] = ""
        return data, []

    dossier = resolver(dossier_id)
    if dossier:
        data["dossier_file_number"] = dossier.get("file_number", "")
        data["dossier_title"] = dossier.get("title", "")
        return data, []

    if strict:
        return data, [not_found_error]
    data["dossier_id"] = blank_value
    data["dossier_file_number"] = ""
    data["dossier_title"] = ""
    return data, []



# ── Counterparty suggestions (lot 6 of 2026-11-11) ──────────────────────────
#: Every answer carries it: the page drops any answer without it — an expired
#: session's sign-in page, an error — and says the suggestions are
#: unavailable instead.
SUGGESTION_HEADER = "X-Athena-Fragment"
SUGGESTION_HEADER_VALUE = "contreparties"


def _suggestion_response(body: str) -> Response:
    resp = make_response(body, 200)
    resp.headers[SUGGESTION_HEADER] = SUGGESTION_HEADER_VALUE
    return resp


def types_ecritures_url() -> str:
    """The « Types d'écritures » page (lot 7) — "" in an app that registers
    no accounting hub (a test's bare app): a form never fails over an aid."""
    from flask import url_for
    from werkzeug.routing import BuildError

    try:
        return url_for("comptabilite.types_ecritures")
    except BuildError:
        return ""


def no_counterparty_suggestions() -> Response:
    """An empty list: a type whose field suggests nothing (read nothing)."""
    return _suggestion_response("")


def _contact_roles_of(sources) -> tuple:
    from utils import contact_roles
    from utils import contreparties as cp
    roles: list[str] = []
    if cp.SOURCE_CLIENTS in sources:
        roles.append("client")
    if cp.SOURCE_SUPPLIERS in sources:
        roles.extend(contact_roles.SUPPLIER_ROLES)
    if cp.SOURCE_PROFESSIONALS in sources:
        roles.extend(contact_roles.PROFESSIONAL_ROLES)
    return tuple(dict.fromkeys(roles))


def counterparty_suggestions(register: str, kind: str, sources, typed: str, *,
                             dossier_id: str = "", invoice_id: str = "") -> Response:
    """The suggestions of a counterparty field whose type suggests from
    *sources* (``utils/contreparties``), as the fragment the field shows.

    The names the entry points to — the invoice's client, the dossier's
    clients or parties — and the fixed names show on focus; the contacts
    (clients, suppliers, professionals) are searched from
    ``contreparties.MIN_TYPED`` typed characters (``partie.list_by_roles_
    strict``). A pick writes ``civilite.payee_name`` for a contact (never a
    title) and a snapshot name without its title. Every read is strict: a
    failure answers « Suggestions indisponibles — saisissez le nom. », never
    an empty list, and logs the register and the type only — never what
    was typed. An id Firestore cannot address counts as none. Always 200,
    with ``SUGGESTION_HEADER``. The field stays free text: nothing here
    refuses a name."""
    from models import dossier as dossier_model
    from models import invoice as invoice_model
    from models import partie as partie_model
    from models.record_ids import is_addressable_id
    from utils import civilite, contact_roles
    from utils import contreparties as cp
    from utils.logging_setup import log_unexpected

    sources = tuple(sources)
    typed = (typed or "")[:200]
    pinned: list[dict] = []
    fixed: list[dict] = []
    contacts: list[dict] = []
    truncated = False
    try:
        if cp.SOURCE_INVOICE_CLIENT in sources and is_addressable_id(invoice_id):
            invoice = invoice_model.get_invoice_strict(invoice_id) or {}
            if invoice.get("client_name"):
                pinned.append({"name": civilite.strip_prefix(invoice["client_name"]),
                               "detail": "client de la facture"})
            dossier_id = dossier_id or invoice.get("dossier_id") or ""
        groups = []
        if cp.SOURCE_DOSSIER_CLIENTS in sources or cp.SOURCE_DOSSIER_PARTIES in sources:
            groups.append("clients")
        if cp.SOURCE_DOSSIER_PARTIES in sources:
            groups.append("opposing_parties")
        if groups and is_addressable_id(dossier_id):
            dossier = dossier_model.get_dossier_strict(dossier_id) or {}
            for group in groups:
                for party in dossier.get(group) or []:
                    name = civilite.strip_prefix(party.get("name") or "")
                    if name:
                        pinned.append({"name": name, "detail": "ce dossier"})
        if cp.SOURCE_TRUST_ACCOUNT in sources:
            fixed.append({"name": cp.TRUST_ACCOUNT_NAME})
        if cp.SOURCE_TAX_AUTHORITIES in sources:
            fixed.extend({"name": name} for name in cp.TAX_AUTHORITIES)
        roles = _contact_roles_of(sources)
        if roles and len(cp.fold(typed)) >= cp.MIN_TYPED:
            rows, truncated = partie_model.list_by_roles_strict(roles)
            for row in rows:
                contacts.append({
                    "name": civilite.payee_name(row),
                    "detail": contact_roles.role_label(row.get("contact_role") or ""),
                    "category": row.get("categorie_depense_defaut") or "",
                    "alternates": [civilite.as_text(row.get(k, "")) for k in (
                        "trade_name", "organization_name", "first_name", "last_name")],
                })
            contacts.sort(key=lambda c: cp.fold(c["name"]))
    except Exception:
        log_unexpected("counterparty suggestions failed", register=register, kind=kind)
        return _suggestion_response(render_template(
            "components/_suggestions_contrepartie.html", unavailable=True))
    rows, more = cp.rank(pinned, fixed, contacts, typed)
    return _suggestion_response(render_template(
        "components/_suggestions_contrepartie.html", rows=rows, more=more or truncated))
