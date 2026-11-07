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

Also home to ``theme_context``, the template context processor of the
display theme: ``main.py`` registers it, and so can a test's bare app, with
no import of ``main``.
"""

from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import Response, has_request_context, redirect, request
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
