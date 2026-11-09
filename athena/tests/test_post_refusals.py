"""No POST route loses a refusal (the refusals lot, 2026-11-06).

Before this lot a refused delete, move or pin — a Firestore blip, an entry
locked since the page was shown, a mandatory step — redirected exactly
like a success: the lawyer landed on the list or the dossier tab, the
record (and its copy on the phone) still there, and believed it gone.
Under htmx the same routes answered a 4xx fragment htmx never swaps
(Rule 8), and the refusals built by hand in ``routes/hearings.py`` went
through ``markupsafe.escape`` — HTML escaping, no URL encoding: « n'a »
became « n&#39;a », whose ``&`` ended the parameter (« Cet élément n »).

1. The helpers (``routes/_helpers``): ``redirect_after_post`` — a 302 for a
   plain form, a 200 carrying ``HX-Redirect`` and no ``Location`` for htmx
   (the browser's XHR follows a 302 by itself, so a header on one never
   reached htmx), ``follow=True`` keeping the 302 for the in-place swaps;
   ``with_query`` — a banner percent-encoded into a target's query.
2. Guard (a), static, over EVERY POST route of every blueprint of the web
   app (``routes/*.py`` and the portal's ``client/``) and every module-
   level helper they share: no htmx branch — nor any branch of a view a
   template ``hx-post``s to — answers a 4xx (a tuple, ``abort``,
   ``Response``/``make_response``, or a helper that answers one); no
   refusal path redirects like a success; no banner query is built by hand
   (f-string, ``+``, ``%``, ``str.format``) nor HTML-escaped. Its coverage
   is checked against Flask's own routing — the blueprints of routes/, and
   every POST rule of the two REAL apps — and it is proved on planted
   cases, synthetic and planted into the real modules. Beside it, the
   POSTs that still take a FAIL-OPEN read's None for an absence (X1) are a
   declared list that may only shrink.
3. Guard (b): ``HX-Redirect`` is set by the helper alone, in any case.
4. The answer's SHAPE: every POST these tests make under htmx gets the
   helper's 200 + ``HX-Redirect`` and no ``Location`` (``_landing``), but
   the declared in-place swaps — the only views passing ``follow=True``,
   pinned from the source; every success block of the lot, and every
   other answer of its routes no test here posted under htmx, answers
   htmx with the plain form's own target (two tables).
5. The sites — real routes, templates and models over
   ``tests/_fake_firestore.py``: where each refusal lands, that the page
   SAYS it, that nothing was written (no tombstone, no CTag bump, no audit
   row), that a failed read is logged with its ids, and that the DAV order
   of a deletion is unchanged (tombstone → CTag → journal, Change Impact
   2).

« Fails on the old code » names what a test pins of this lot's fix;
« fails on round 1 » what its review added (the second round of the lot).
"""

import ast
import functools
import importlib
import io
import logging
import os
import pathlib
import re
import sys
import textwrap
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Callable, NamedTuple, Optional
from unittest import mock
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from google.api_core import exceptions as gexc
from markupsafe import escape

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

from tests._real_app_probe import run_probe  # noqa: E402

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync
    from models import admin_ledger as al
    from models import document as document_model
    from models import dossier as dossier_model
    from models import expense as expense_model
    from models import hearing as hearing_model
    from models import note as note_model
    from models import partie as partie_model
    from models import protocol as protocol_model
    from models import task as task_model
    from models import time_entry as time_entry_model
    from models import trust as trust_model
    import routes.admin_ledger as admin_routes
    import routes.auth_routes as auth_routes
    import routes.budgets as budgets_routes
    import routes.dashboard as dashboard_routes
    import routes.doc_templates as doc_templates_routes
    import routes.documents as documents_routes
    import routes.dossiers as dossiers_routes
    import routes.hearings as hearings_routes
    import routes.invoices as invoices_routes
    import routes.notes as notes_routes
    import routes.parties as parties_routes
    import routes.protocols as protocols_routes
    import routes.tasks as tasks_routes
    import routes.time_expenses as time_expenses_routes
    import routes.trust as trust_routes
    from routes._helpers import redirect_after_post, with_query
    from security import safe_internal_redirect

from flask import Blueprint, Flask, request  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.html_attr import jsattr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.markdown_docx import markdown_to_safe_html  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

# Loaded for its side effect: the fake store reaches the DAV sync layer.
_ = (dav_sync,)

UTC = timezone.utc
DT = datetime(2026, 3, 4, tzinfo=UTC)
HX = {"HX-Request": "true"}

# The model's sentences, spelled out: comparing to the module's constants
# would raise on the old code instead of failing on what is pinned.
READ_UNAVAILABLE = ("Cet élément n'a pas pu être lu — une panne passagère. "
                    "Rien n'a été enregistré : réessayez dans un instant.")
DELETE_FAILED = "Erreur lors de la suppression. Veuillez réessayer."
NOT_A_SERIES = "Cette audience ne fait pas partie d'une série."
MANDATORY_STEP = "Les étapes obligatoires ne peuvent pas être supprimées."
PIN_REFUSED = ("Le changement d'épinglage n'a pas pu être confirmé — "
               "vérifiez l'état ci-dessus, puis réessayez au besoin.")
LOCKED_SINCE = "Cette écriture a été verrouillée depuis l'affichage"
NOT_DELETED_RETRY = "La suppression n'a pas abouti : l'écriture est toujours"
ABANDON_UNCONFIRMED = "L'abandon de la conciliation n'a pas pu être confirmé"

# A header value a WSGI server can send: latin-1 without a control
# character (gunicorn refuses a character past U+00FF — after the commit).
_HEADER_SAFE = re.compile(r"[ \t\x21-\x7e\x80-\xff]*")


# ══════════════════════════════════════════════════════════════════════
# 1. The helpers
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def bench():
    """A bare app whose one POST answers through the helper."""
    app = Flask(__name__)

    @app.post("/x")
    def x():
        # Read through the door like any route's target (CodeQL n° 903).
        target = with_query(
            safe_internal_redirect(request.form.get("target"), "/taches/t1"),
            erreur=request.form.get("erreur", ""))
        return redirect_after_post(
            target, follow=request.form.get("follow") == "1")

    return app.test_client()


def test_a_plain_form_gets_a_302_and_no_hx_redirect(bench):
    resp = bench.post("/x", data={"erreur": READ_UNAVAILABLE})
    assert resp.status_code == 302
    assert "HX-Redirect" not in resp.headers
    query = parse_qs(urlsplit(resp.headers["Location"]).query)
    assert query == {"erreur": [READ_UNAVAILABLE]}


def test_htmx_gets_a_200_carrying_hx_redirect_and_no_location(bench):
    """Fails on the old code's shape (a 302 carrying HX-Redirect): the XHR
    follows a 302 by itself, and htmx reads the headers of what it got."""
    resp = bench.post("/x", data={"erreur": READ_UNAVAILABLE}, headers=HX)
    assert resp.status_code == 200
    assert "Location" not in resp.headers
    assert resp.get_data() == b""
    hx = resp.headers["HX-Redirect"]
    assert _HEADER_SAFE.fullmatch(hx) and hx.isascii()
    assert parse_qs(urlsplit(hx).query) == {"erreur": [READ_UNAVAILABLE]}


def test_follow_keeps_the_302_under_htmx(bench):
    resp = bench.post("/x", data={"erreur": "Refus.", "follow": "1"},
                      headers=HX)
    assert resp.status_code == 302
    assert "HX-Redirect" not in resp.headers


def test_with_query_merges_into_the_targets_own_query():
    target = "/parties/p1?gerer=1&erreur=ancienne#preuves-identite"
    merged = urlsplit(with_query(target, erreur="", message="Fait."))
    assert merged.fragment == "preuves-identite"
    assert parse_qs(merged.query) == {"gerer": ["1"], "message": ["Fait."]}
    replaced = urlsplit(with_query("/fideicommis/?compte=a&erreur=x",
                                   erreur="Nouvelle."))
    assert parse_qs(replaced.query) == {"compte": ["a"],
                                        "erreur": ["Nouvelle."]}
    # No banner: the very target, byte for byte.
    assert with_query("/dossiers/d1?tab=audiences") == (
        "/dossiers/d1?tab=audiences")


def test_an_apostrophe_travels_whole():
    """Fails on the old hearings code: escape() is no URL encoding, and
    « n&#39;a » ended the parameter at its « & »."""
    target = with_query("/audiences/h1", erreur=READ_UNAVAILABLE)
    assert "&#39;" not in target
    assert parse_qs(urlsplit(target).query) == {"erreur": [READ_UNAVAILABLE]}
    old = f"/audiences/h1?erreur={escape(READ_UNAVAILABLE)}"
    assert parse_qs(urlsplit(old).query)["erreur"] == ["Cet élément n"]


# ══════════════════════════════════════════════════════════════════════
# 2. Guard (a): no refusal path redirects like a success
# ══════════════════════════════════════════════════════════════════════
#
# A static sweep, path-sensitive at the level of one function. It follows
# each path through the ``if`` / ``try`` statements of a POST view (and of
# every module-level helper) and tracks what the path knows of the OUTCOME
# a model call answered — bound to ``success`` / ``ok`` / ``deleted``
# (truthy: it worked) or to a name SHAPED like a refusal (truthy: refused):
# ``error(s)``, ``erreur(s)``, any ``<word>_error(s)`` / ``<word>_erreur(s)``,
# ``refus``, ``refusal(s)``, ``problem(s)`` — a pattern, never a list, so a
# refusal named otherwise is judged too:
#
#   S  the write happened;
#   R  it was refused;
#   G  « already gone » — the store says the record is absent: a test
#      against a ``*_NOT_FOUND`` sentence (which the model must answer only
#      after a STRICT read — the sweep cannot see that, the site tests do),
#      or a name EVERY assignment of which is a STRICT read (``*_strict``)
#      that answered None. The intent of a deletion is fulfilled: the
#      list, quietly, is the decided landing.
#
# At every ``return`` of a redirect (``redirect``, ``redirect_after_post``
# or a module helper that answers one):
#
#   * on a refusal path (R, never S), the redirect must SAY it — a banner
#     keyword (``erreur``, ``avertissement``, ``message``, ``*_erreur``),
#     a banner key of a dict it spreads, or the refusal itself; a refusal
#     merely HANDED to a module helper says nothing unless the helper puts
#     that parameter in a banner;
#   * out of an ``except`` handler, the same — and on the branch testing a
#     name an ``except`` handler set to None (``except: x = None`` then
#     ``if x is None:`` — a failed read passing for an absence);
#   * on a path where both are possible (S and R), the target must depend
#     on the outcome or say it — the old « target = return_to or the list »
#     shared by both;
#   * an htmx branch — ``if is_htmx()``, after ``if not is_htmx(): …
#     return``, or anywhere in a view a template ``hx-post``s to unless
#     provably not htmx — never answers a 4xx: a ``(body, N)`` tuple,
#     ``Response(…, status=N)``, ``make_response(body, N)``, ``abort(N)``,
#     or a module helper that answers one; nor Flask's bare ``redirect()``
#     (a 302 the XHR follows and swaps whole): ``redirect_after_post``,
#     whose ``follow=True`` only the declared swaps pass (section 4).
#
# And no banner query (``…?erreur=…``) is built by hand — an f-string, a
# ``+`` or ``%`` operand, a ``str.format`` template — nor a banner value
# passed through ``escape()`` (HTML escaping is no URL encoding: « n'a »
# was cut at its apostrophe).
#
# Out of its reach, deliberately: mcp/ (JSON-RPC, and OAuth's RFC 6749
# redirects to the CLIENT's callback carrying ``error=`` codes); main.py's
# app-level ``/csp-report`` (the browser's CSP violation reports, answered
# 204 without a page — no form, no lawyer); dav/ (WebDAV methods — no
# POST); a GET view (its refusals are not POSTs). Every POST rule of the
# two REAL apps is checked against that scope below.

_HTMX_CALLS = frozenset({"is_htmx", "_is_htmx"})
_REDIRECTORS = frozenset({"redirect", "redirect_after_post"})
_SUCCESS_NAMES = frozenset({"success", "ok", "deleted"})
_REFUSAL_NAME = re.compile(
    r"^(?:\w+_)?(?:errors?|erreurs?)$|^refus(?:al)?s?$|^problems?$")
_HAND_BUILT = re.compile(r"\b(?:erreur|message|avertissement|\w+_erreur)=")
_S, _R, _G = "S", "R", "G"
_ANY = frozenset({_S, _R, _G})


def _is_refusal_name(name) -> bool:
    return isinstance(name, str) and bool(_REFUSAL_NAME.match(name))


def _is_outcome_name(name) -> bool:
    return name in _SUCCESS_NAMES or _is_refusal_name(name)


def _call_name(call: ast.Call) -> Optional[str]:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _names(node: ast.AST) -> set:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _banner_key(key) -> bool:
    return isinstance(key, str) and (
        key in ("erreur", "avertissement", "message")
        or key.endswith("_erreur"))


def _int_constant(node) -> Optional[int]:
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    return None


def _refusal_ref(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return _is_refusal_name(node.id)
    if isinstance(node, ast.Subscript):
        if (isinstance(node.value, ast.Name)
                and _is_refusal_name(node.value.id)):
            return True
        return (isinstance(node.slice, ast.Constant)
                and _is_refusal_name(node.slice.value))
    return False


def _names_not_found(node: ast.AST) -> bool:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return bool(node.elts) and all(_names_not_found(e) for e in node.elts)
    ident = (node.id if isinstance(node, ast.Name)
             else node.attr if isinstance(node, ast.Attribute) else "")
    return ident.endswith("NOT_FOUND")


def _assigns(fn: ast.FunctionDef) -> dict:
    """{name: [every value assigned to it]} — a store into ``name[key]``
    counts as a dict display ``{key: value}`` (``params["erreur"] = …``)."""
    out: dict = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            pairs = [(t, node.value) for t in node.targets]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            pairs = [(node.target, node.value)]
        else:
            continue
        for target, value in pairs:
            for n in ast.walk(target):
                if isinstance(n, ast.Name):
                    out.setdefault(n.id, []).append(value)
                elif (isinstance(n, ast.Subscript)
                      and isinstance(n.value, ast.Name)):
                    out.setdefault(n.value.id, []).append(
                        ast.Dict(keys=[n.slice], values=[value]))
    return out


def _status_of(value, assigns: Optional[dict] = None,
               depth: int = 0) -> Optional[int]:
    """The HTTP status a returned value answers, where the source says it:
    a ``(body, N)`` tuple, ``make_response(body, N)``, ``Response(body,
    N)``, any call's ``status=N`` — or a local name standing for one."""
    if isinstance(value, ast.Tuple) and len(value.elts) >= 2:
        return _int_constant(value.elts[1])
    if isinstance(value, ast.Call):
        if (_call_name(value) in ("make_response", "Response")
                and len(value.args) >= 2):
            return _int_constant(value.args[1])
        for k in value.keywords:
            if k.arg == "status":
                return _int_constant(k.value)
        return None
    if isinstance(value, ast.Name) and assigns and depth < 3:
        for v in assigns.get(value.id, []):
            status = _status_of(v, assigns, depth + 1)
            if status is not None:
                return status
    return None


def _abort_status(stmt) -> Optional[int]:
    """``abort(N)`` as a statement: it raises the answer N."""
    if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
            and _call_name(stmt.value) == "abort" and stmt.value.args):
        return _int_constant(stmt.value.args[0])
    return None


class _Module:
    """One module's functions, and which of them answer a redirect — or a
    4xx (each a fixpoint: a helper answering another helper's answer)."""

    def __init__(self, tree: ast.Module):
        self.functions = {f.name: f for f in tree.body
                          if isinstance(f, ast.FunctionDef)}
        self.helpers: set = set()
        self.answers_4xx: set = set()
        for found, answers in ((self.helpers, self.is_redirect),
                               (self.answers_4xx, self.is_4xx)):
            grew = True
            while grew:
                grew = False
                for name, fn in self.functions.items():
                    if name in found:
                        continue
                    assigns = _assigns(fn)
                    if any(isinstance(n, ast.Return) and n.value is not None
                           and answers(n.value, assigns)
                           for n in ast.walk(fn)):
                        found.add(name)
                        grew = True

    def is_redirect(self, value, assigns, depth=0) -> bool:
        if isinstance(value, ast.Call):
            return _call_name(value) in _REDIRECTORS | self.helpers
        if isinstance(value, ast.Name) and depth < 3:
            return any(self.is_redirect(v, assigns, depth + 1)
                       for v in assigns.get(value.id, []))
        return False

    def is_bare_redirect(self, value, assigns, depth=0) -> bool:
        """Flask's own ``redirect(…)``: a 302 under htmx too, which the
        browser's XHR follows by itself and swaps whole."""
        if isinstance(value, ast.Call):
            return _call_name(value) == "redirect"
        if isinstance(value, ast.Name) and depth < 3:
            return any(self.is_bare_redirect(v, assigns, depth + 1)
                       for v in assigns.get(value.id, []))
        return False

    def is_4xx(self, value, assigns) -> bool:
        status = _status_of(value, assigns)
        if status is not None:
            return status >= 400
        return (isinstance(value, ast.Call)
                and _call_name(value) in self.answers_4xx)

    def _reaches(self, answer, assigns, hit: Callable) -> bool:
        """``hit(node, handed)`` holds for a node of the answer — or of a
        value its local names may stand for. *handed*: the node is only
        part of what a module helper is HANDED (a refusal passed to a
        helper says nothing by itself: the helper must put that parameter
        in a banner — a banner keyword or key built into its argument, a
        target already carrying it, still counts)."""
        seen: set = set()
        pending = [(answer, False)]
        while pending:
            node, handed = pending.pop()
            if hit(node, handed):
                return True
            if isinstance(node, ast.Name):
                if (node.id, handed) not in seen:
                    seen.add((node.id, handed))
                    pending.extend((value, handed)
                                   for value in assigns.get(node.id, []))
                continue
            if (isinstance(node, ast.Call)
                    and _call_name(node) in self.functions):
                if self._helper_banners(node):
                    return True
                handed = True
            pending.extend((child, handed)
                           for child in ast.iter_child_nodes(node))
        return False

    def _helper_banners(self, call: ast.Call) -> bool:
        """The helper puts a parameter this call passes in a banner."""
        fn = self.functions[_call_name(call)]
        params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
        passed = ({a.arg for a in fn.args.args[:len(call.args)]}
                  | {k.arg for k in call.keywords})
        return any(isinstance(n, ast.keyword) and _banner_key(n.arg)
                   and _names(n.value) & params & passed
                   for n in ast.walk(fn))

    def says(self, answer, assigns) -> bool:
        """The redirect names the outcome: a banner keyword, a banner key
        of a dict display, the refusal itself (never one merely handed to
        a module helper)."""
        return self._reaches(answer, assigns, lambda n, handed: (
            (isinstance(n, ast.keyword) and _banner_key(n.arg))
            or (isinstance(n, ast.Dict) and any(
                isinstance(k, ast.Constant) and _banner_key(k.value)
                for k in n.keys))
            or (not handed and isinstance(n, ast.Name)
                and _is_refusal_name(n.id))))

    def depends(self, answer, assigns) -> bool:
        return self._reaches(answer, assigns, lambda n, handed: (
            not handed and isinstance(n, ast.Name)
            and _is_outcome_name(n.id)))


def _strict_read(name: str, assigns: dict) -> bool:
    """EVERY value the name is given is a strict read: one ``except: x =
    None`` makes its None a failed read, never an absence."""
    values = assigns.get(name, [])
    return bool(values) and all(
        isinstance(v, ast.Call) and (_call_name(v) or "").endswith("_strict")
        for v in values)


def _swallowed(fn: ast.FunctionDef) -> set:
    """The names an ``except`` handler sets to None."""
    out = set()
    for handler in (n for n in ast.walk(fn)
                    if isinstance(n, ast.ExceptHandler)):
        for stmt in handler.body:
            if isinstance(stmt, ast.Assign):
                pairs = []
                for target in stmt.targets:
                    pairs += (list(zip(target.elts, stmt.value.elts))
                              if isinstance(target, ast.Tuple)
                              and isinstance(stmt.value, ast.Tuple)
                              else [(target, stmt.value)])
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                pairs = [(stmt.target, stmt.value)]
            else:
                continue
            for name, value in pairs:
                if (isinstance(name, ast.Name)
                        and isinstance(value, ast.Constant)
                        and value.value is None):
                    out.add(name.id)
    return out


def _tests_absence_of(test, names: set) -> bool:
    """``if not x:`` / ``if x is None:`` on one of *names*."""
    if (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
            and isinstance(test.operand, ast.Name)):
        return test.operand.id in names
    return (isinstance(test, ast.Compare) and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Is)
            and isinstance(test.left, ast.Name) and test.left.id in names
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is None)


def _pinned_by_any(flags: list) -> Optional[bool]:
    """Every operand holds: one that pins the request pins it."""
    if True in flags:
        return True
    if False in flags:
        return False
    return None


def _agreed_by_all(flags: list) -> Optional[bool]:
    """One operand decides: only what they all pin is pinned."""
    if flags and all(f is True for f in flags):
        return True
    if flags and all(f is False for f in flags):
        return False
    return None


def _split(test, assigns):
    """What a branch test says: (outcomes on its body, on its orelse —
    None: no constraint; htmx on its body, on its orelse — True: an htmx
    request, False: provably not one, None: either)."""
    if isinstance(test, ast.Call) and _call_name(test) in _HTMX_CALLS:
        return None, None, True, False
    if isinstance(test, ast.Name):
        if _is_refusal_name(test.id):
            return {_R, _G}, {_S}, None, None
        if test.id in _SUCCESS_NAMES:
            return {_S}, {_R, _G}, None, None
        return None, None, None, None
    if isinstance(test, ast.Subscript) and _refusal_ref(test):
        return {_R, _G}, {_S}, None, None
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        body, orelse, htmx_body, htmx_orelse = _split(test.operand, assigns)
        return orelse, body, htmx_orelse, htmx_body
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        left, op, right = test.left, test.ops[0], test.comparators[0]
        if _names_not_found(right):
            if isinstance(op, (ast.Eq, ast.In)):
                return {_G}, {_S, _R}, None, None
            if isinstance(op, (ast.NotEq, ast.NotIn)):
                return {_S, _R}, {_G}, None, None
        if _refusal_ref(left) and isinstance(op, (ast.Eq, ast.In)):
            return {_R, _G}, None, None, None
        if (isinstance(op, ast.Is) and isinstance(right, ast.Constant)
                and right.value is None and isinstance(left, ast.Name)
                and _strict_read(left.id, assigns)):
            return {_G}, None, None, None
        return None, None, None, None
    if isinstance(test, ast.Call):
        args = list(test.args) + [k.value for k in test.keywords]
        if any(_refusal_ref(a) or (isinstance(a, (ast.List, ast.Tuple))
                                   and any(_refusal_ref(e) for e in a.elts))
               for a in args):
            return {_R}, None, None, None
        return None, None, None, None
    if isinstance(test, ast.BoolOp):
        parts = [_split(v, assigns) for v in test.values]
        bodies = [p[0] for p in parts]
        orelses = [p[1] for p in parts]
        htmx_bodies = [p[2] for p in parts]
        htmx_orelses = [p[3] for p in parts]
        if isinstance(test.op, ast.And):
            # Every operand holds on the body; one fails on the orelse.
            body = None
            for b in bodies:
                if b is not None:
                    body = set(b) if body is None else body & b
            orelse = (set().union(*orelses)
                      if all(o is not None for o in orelses) else None)
            return (body, orelse, _pinned_by_any(htmx_bodies),
                    _agreed_by_all(htmx_orelses))
        body = (set().union(*bodies)
                if all(b is not None for b in bodies) else None)
        orelse = None
        for o in orelses:
            if o is not None:
                orelse = set(o) if orelse is None else orelse & o
        return (body, orelse, _agreed_by_all(htmx_bodies),
                _pinned_by_any(htmx_orelses))
    return None, None, None, None


def _binding(stmt) -> Optional[str]:
    """How a statement binds an outcome name: "new" (a call answers a new
    outcome), _S / _R (``success = True`` / ``False``), "keep" (derived
    data: ``deleted = legs or []``), or None."""
    if isinstance(stmt, ast.Assign):
        targets, value = stmt.targets, stmt.value
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets, value = [stmt.target], stmt.value
    else:
        return None
    named = {n.id for t in targets for n in ast.walk(t)
             if isinstance(n, ast.Name) and _is_outcome_name(n.id)}
    if not named:
        return None
    if isinstance(value, ast.Call):
        return "new"
    if (isinstance(value, ast.Constant) and isinstance(value.value, bool)
            and named <= _SUCCESS_NAMES):
        return _S if value.value else _R
    return "keep"


def _sweep_function(fn: ast.FunctionDef, module: _Module,
                    refusals_seen: Optional[list] = None,
                    htmx_view: bool = False) -> list:
    """*htmx_view*: a template ``hx-post``s to this view — every path is
    htmx until a test proves it is not."""
    assigns = _assigns(fn)
    swallowed = _swallowed(fn)
    found = []

    def check(ret, state):
        outcomes, bound, htmx, in_except = state
        if refusals_seen is not None and bound and outcomes & {_R}:
            refusals_seen.append((fn.name, ret.lineno))
        if ret.value is not None and module.is_4xx(ret.value, assigns):
            if htmx:
                found.append((ret.lineno, fn.name, "4xx-under-htmx"))
            return
        if ret.value is None or not module.is_redirect(ret.value, assigns):
            return
        answer = ret.value
        if htmx and module.is_bare_redirect(answer, assigns):
            found.append((ret.lineno, fn.name, "302-under-htmx"))
        if outcomes == {_G}:
            return
        if bound and _R in outcomes and _S not in outcomes:
            if not module.says(answer, assigns):
                found.append((ret.lineno, fn.name, "refusal-without-reason"))
        elif in_except and not module.says(answer, assigns):
            found.append((ret.lineno, fn.name, "except-without-reason"))
        elif (bound and _R in outcomes and _S in outcomes
              and not module.depends(answer, assigns)
              and not module.says(answer, assigns)):
            found.append((ret.lineno, fn.name, "refusal-shares-a-success"))

    def narrow(state, allowed, htmx):
        outcomes, bound, h, in_except = state
        if allowed is not None:
            outcomes = outcomes & allowed
            if not outcomes:
                return None             # a path no outcome can take
        return (frozenset(outcomes), bound, h if htmx is None else htmx,
                in_except)

    def walk(stmts, states):
        states = set(states)
        for stmt in stmts:
            if not states:
                break
            if isinstance(stmt, ast.Return):
                for state in states:
                    check(stmt, state)
                return set()
            if isinstance(stmt, ast.Raise):
                return set()
            status = _abort_status(stmt)
            if status is not None and status >= 400:
                if any(h for _o, _b, h, _e in states):
                    found.append((stmt.lineno, fn.name, "4xx-under-htmx"))
                return set()
            if isinstance(stmt, ast.If):
                body, orelse, htmx_body, htmx_orelse = _split(stmt.test,
                                                              assigns)
                on_body = {s for s in (narrow(st, body, htmx_body)
                                       for st in states) if s}
                on_orelse = {s for s in (narrow(st, orelse, htmx_orelse)
                                         for st in states) if s}
                if _tests_absence_of(stmt.test, swallowed):
                    # The absence a failed read was coerced into.
                    on_body = {(o, b, h, True) for o, b, h, _e in on_body}
                states = walk(stmt.body, on_body) | walk(stmt.orelse,
                                                         on_orelse)
            elif isinstance(stmt, ast.Try):
                after = walk(stmt.body, states)
                for handler in stmt.handlers:
                    ends = walk(handler.body, {(o, b, h, True)
                                               for o, b, h, _e in states})
                    after |= {(o, b, h, False) for o, b, h, _e in ends}
                after = walk(stmt.orelse, after)
                states = walk(stmt.finalbody, after)
            elif isinstance(stmt, (ast.For, ast.While, ast.With)):
                states = states | walk(stmt.body, states)
                states = walk(getattr(stmt, "orelse", []), states)
            elif not isinstance(stmt, (ast.FunctionDef, ast.ClassDef)):
                how = _binding(stmt)
                if how == "new":
                    states = {(_ANY, True, h, e) for _o, _b, h, e in states}
                elif how in (_S, _R):
                    states = {(frozenset({how}), True, h, e)
                              for _o, _b, h, e in states}
                elif how == "keep":
                    states = {(o, True, h, e) for o, _b, h, e in states}
        return states

    walk(fn.body, {(_ANY, False, htmx_view, False)})
    return found


def _route_decorators(fn: ast.FunctionDef) -> list:
    return [d for d in fn.decorator_list if isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and d.func.attr in ("route", "get", "post")
            and isinstance(d.func.value, ast.Name)]


def _is_post(deco: ast.Call) -> bool:
    if deco.func.attr == "post":
        return True
    methods = next((ast.literal_eval(k.value) for k in deco.keywords
                    if k.arg == "methods"), ["GET"])
    return deco.func.attr == "route" and "POST" in methods


def _swept(tree: ast.Module) -> list:
    """The POST views of the module and its helpers (every module-level
    function that is no view): a GET view is no POST."""
    return [fn for fn in tree.body if isinstance(fn, ast.FunctionDef)
            and (not _route_decorators(fn)
                 or any(_is_post(d) for d in _route_decorators(fn)))]


def _banner_values(node) -> list:
    """The values a banner is given at *node*: a banner keyword (never
    ``render_template``'s, a page's context — autoescaped there), a banner
    key of a dict display, a store into ``params["erreur"]``."""
    if isinstance(node, ast.Call) and _call_name(node) != "render_template":
        return [k.value for k in node.keywords if _banner_key(k.arg)]
    if isinstance(node, ast.Dict):
        return [v for k, v in zip(node.keys, node.values)
                if isinstance(k, ast.Constant) and _banner_key(k.value)]
    if isinstance(node, ast.Assign):
        return [node.value for t in node.targets
                if isinstance(t, ast.Subscript)
                and isinstance(t.slice, ast.Constant)
                and _banner_key(t.slice.value)]
    return []


def _hand_built(tree: ast.Module) -> list:
    """Every banner query built by hand — an f-string, a ``+`` or ``%``
    operand, a ``str.format`` template naming ``erreur=`` & co. — and
    every banner value passed through ``escape()``."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            templates = node.values
        elif isinstance(node, ast.BinOp) and isinstance(node.op,
                                                        (ast.Add, ast.Mod)):
            templates = [node.left, node.right]
        elif (isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)
              and node.func.attr == "format"):
            templates = [node.func.value]
        else:
            templates = []
        if any(isinstance(t, ast.Constant) and isinstance(t.value, str)
               and _HAND_BUILT.search(t.value) for t in templates):
            found.append((node.lineno, "", "hand-built-query"))
        for value in _banner_values(node):
            if any(isinstance(n, ast.Call) and _call_name(n) == "escape"
                   for n in ast.walk(value)):
                found.append((value.lineno, "", "escaped-banner"))
    return found


def lost_refusals(source: str, refusals_seen: Optional[list] = None,
                  htmx_views: frozenset = frozenset()) -> list:
    """Every place guard (a) refuses in *source*: (line, function, rule).
    *refusals_seen* collects every return the sweep reached on a path where
    a refusal is possible — what it judged, accepted or not. *htmx_views*:
    the views of the module a template ``hx-post``s to."""
    tree = ast.parse(source)
    module = _Module(tree)
    found = []
    for fn in _swept(tree):
        found += _sweep_function(fn, module, refusals_seen,
                                 fn.name in htmx_views)
    found += _hand_built(tree)
    return sorted(set(found))


def _web_modules() -> list:
    """The web app's route modules — every blueprint of the main service's
    HTML app, and the portal's."""
    return sorted(list((_ATHENA / "routes").glob("*.py"))
                  + list((_ATHENA / "client").rglob("*.py")))


_HX_WRITE = re.compile(
    r"""hx-(?:post|put|patch|delete)\s*=\s*["']\{\{\s*url_for\(\s*"""
    r"""["']([\w.]+)["']""")


def _blueprints_of(tree: ast.Module) -> dict:
    """{module variable: blueprint name} of the module's Blueprint(…)s."""
    return {
        node.targets[0].id: node.value.args[0].value
        for node in tree.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Call)
        and _call_name(node.value) == "Blueprint"
        and node.value.args and isinstance(node.value.args[0], ast.Constant)
    }


def _live_htmx_views() -> dict:
    """{routes/<module>.py: {view names}} — the views a template
    ``hx-post``s (puts, patches, deletes) to: htmx IS their caller."""
    endpoints = set()
    for page in (_ATHENA / "templates").rglob("*.html"):
        endpoints |= set(_HX_WRITE.findall(page.read_text(encoding="utf-8")))
    views: dict = {}
    for path in sorted((_ATHENA / "routes").glob("*.py")):
        names = set(_blueprints_of(
            ast.parse(path.read_text(encoding="utf-8"))).values())
        for endpoint in endpoints:
            bp, _, view = endpoint.partition(".")
            if bp in names:
                views.setdefault(path.name, set()).add(view)
    return views


def _htmx_views_of(path: pathlib.Path, live: dict) -> frozenset:
    if path.parent.name != "routes":
        return frozenset()
    return frozenset(live.get(path.name, ()))


def test_no_post_route_loses_a_refusal_or_answers_htmx_a_4xx():
    live = _live_htmx_views()
    # Not blind: the templates' hx-post targets are read — the task list's
    # checkbox and the generation popup among them.
    assert "task_toggle" in live.get("tasks.py", set()), live
    assert "generate" in live.get("doc_templates.py", set()), live
    found, judged = {}, {}
    for path in _web_modules():
        seen: list = []
        hits = lost_refusals(path.read_text(encoding="utf-8"), seen,
                             _htmx_views_of(path, live))
        if hits:
            found[str(path.relative_to(_ATHENA))] = hits
        judged[path.name] = {fn for fn, _line in seen}
    assert found == {}, (
        "A refusal must land where it is SAID (the record's page and its "
        "banner — routes/_helpers.redirect_after_post), never redirect like "
        f"a success, and htmx never gets a 4xx fragment: {found}")
    # Not vacuous: the sweep did reach the refusal paths of this lot's
    # sites, and judged them.
    for module, function in [
            ("tasks.py", "task_delete"), ("tasks.py", "task_toggle"),
            ("hearings.py", "hearing_delete"),
            ("hearings.py", "_delete_chain_from"),
            ("hearings.py", "hearing_unlink"),
            ("protocols.py", "protocol_delete"),
            ("protocols.py", "step_delete"), ("notes.py", "note_pin"),
            ("admin_ledger.py", "entry_delete"),
            ("admin_ledger.py", "reconciliation_abandon"),
            ("documents.py", "document_move"),
            ("documents.py", "document_move_bulk"),
            ("documents.py", "folder_move"),
            ("time_expenses.py", "time_entry_delete"),
            ("time_expenses.py", "expense_delete"),
            ("invoices.py", "invoice_draft_update"),
            ("parties.py", "kyc_confirm"),
            ("trust.py", "reconciliation_abandon")]:
        assert function in judged[module], (module, function)


def _post_views_by_ast() -> set:
    """``blueprint.endpoint`` of every POST view of routes/, from the
    source."""
    endpoints = set()
    for path in sorted((_ATHENA / "routes").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        blueprints = _blueprints_of(tree)
        for fn in tree.body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            for deco in _route_decorators(fn):
                if _is_post(deco) and deco.func.value.id in blueprints:
                    name = next((ast.literal_eval(k.value)
                                 for k in deco.keywords
                                 if k.arg == "endpoint"), fn.name)
                    endpoints.add(f"{blueprints[deco.func.value.id]}.{name}")
    return endpoints


def test_the_sweep_sees_every_post_route_flask_routes():
    """Not blind: the POST views the sweep derives from the source are
    exactly the POST endpoints Flask routes, every blueprint of routes/
    registered."""
    app = Flask(__name__)
    with mock.patch("google.cloud.firestore.Client"):
        modules = [importlib.import_module(f"routes.{p.stem}")
                   for p in sorted((_ATHENA / "routes").glob("*.py"))
                   if p.stem != "__init__"]
    blueprints = []
    for module in modules:
        for value in vars(module).values():
            if isinstance(value, Blueprint) and value not in blueprints:
                blueprints.append(value)
    for bp in blueprints:
        app.register_blueprint(bp)
    routed = {rule.endpoint for rule in app.url_map.iter_rules()
              if "POST" in rule.methods}
    assert len(blueprints) >= 20 and len(routed) >= 100
    assert _post_views_by_ast() == routed


# Every POST rule of the REAL apps — ``main.app`` (``create_app``: every
# blueprint, the connector's, the app-level routes) and the portal's
# ``client.wsgi:app`` — with the source of its view. In a subprocess: both
# build their app at import (logging handlers, OpenTelemetry, Firebase),
# state this suite must not inherit (``tests/test_provenance.py``). Behind
# tests/_real_app_probe.PRELUDE, like every real-app probe: the child loads
# no conftest, and its Firestore client must stay a mock for the whole run.
_REAL_POST_RULES_PROBE = textwrap.dedent('''
    import inspect, json, os

    import main
    import client.wsgi as portal
    rows = []
    for label, app in (("main", main.app), ("portail", portal.app)):
        for rule in app.url_map.iter_rules():
            if "POST" not in rule.methods:
                continue
            view = inspect.unwrap(app.view_functions[rule.endpoint])
            source = os.path.relpath(inspect.getsourcefile(view), os.getcwd())
            rows.append({"app": label, "endpoint": rule.endpoint,
                         "rule": rule.rule, "name": view.__name__,
                         "file": source.replace(os.sep, "/")})
    print("@@POST@@" + json.dumps(rows))
''')

# What guard (a) leaves out, deliberately (its scope note above): the
# connector's two blueprints and main.py's CSP report endpoint.
_OUT_OF_SCOPE = re.compile(r"^(?:mcp|mcp_oauth)\.|^csp_report$")


@pytest.fixture(scope="module")
def real_post_rules() -> list:
    env = {**os.environ}
    env.pop("ENV", None)
    # The portal boots only with its own session key (client/config.py) —
    # the value tests/test_portail_*.py set.
    env.setdefault("PORTAIL_SECRET_KEY", "test-portail-secret")
    return run_probe(_REAL_POST_RULES_PROBE, "@@POST@@", env=env,
                     timeout=180)


def test_every_post_rule_of_the_real_apps_is_swept_or_declared_out_of_scope(
        real_post_rules):
    """Fails on round 1's blind spot: the sweep was checked against the
    blueprints of routes/ alone — a POST added to main.py, or to a
    blueprint registered elsewhere, went unseen."""
    swept = {}
    for path in _web_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        swept[path.relative_to(_ATHENA).as_posix()] = {
            fn.name for fn in _swept(tree)}
    unswept = [r for r in real_post_rules
               if not _OUT_OF_SCOPE.search(r["endpoint"])
               and r["name"] not in swept.get(r["file"], set())]
    assert unswept == [], unswept
    # The declared scope is exactly what it says: the connector's package
    # and main.py's report endpoint — never a page a lawyer posts.
    left_out = [r for r in real_post_rules
                if _OUT_OF_SCOPE.search(r["endpoint"])]
    assert left_out and all(
        r["file"].startswith("mcp/") or (r["file"], r["rule"]) == (
            "main.py", "/csp-report")
        for r in left_out), left_out
    # Not vacuous: both apps were read, the portal's rules among them.
    apps = {r["app"] for r in real_post_rules}
    assert apps == {"main", "portail"}, apps
    assert any(r["file"].startswith("client/") for r in real_post_rules)


# ── Planted cases ────────────────────────────────────────────────────

_FLAGGED = {
    "refusal-to-the-list": (
        "def f():\n"
        "    task, errors = save()\n"
        "    if errors:\n"
        "        return redirect(url_for('x.list'))\n"
        "    return redirect(url_for('x.detail'))\n",
        "refusal-without-reason"),
    "tail-shared-by-both": (
        "def f():\n"
        "    success, error = delete()\n"
        "    if success:\n"
        "        journal()\n"
        "    target = return_to or url_for('x.list')\n"
        "    return redirect(target)\n",
        "refusal-without-reason"),
    "tail-unknown-outcome": (
        "def f():\n"
        "    success, error = delete()\n"
        "    return redirect_after_post(url_for('x.list'))\n",
        "refusal-shares-a-success"),
    "htmx-422": (
        "def f():\n"
        "    success, error = delete()\n"
        "    if _is_htmx():\n"
        "        if success:\n"
        "            return redirect_after_post(url_for('x.list'))\n"
        "        return f'<div>{error}</div>', 422\n"
        "    return redirect(url_for('x.detail', erreur=error))\n",
        "4xx-under-htmx"),
    "htmx-only-after-an-early-exit": (
        "def f():\n"
        "    if not is_htmx():\n"
        "        return redirect(url_for('x.list'))\n"
        "    return '<div>Paramètres manquants.</div>', 400\n",
        "4xx-under-htmx"),
    "htmx-and-a-refusal": (
        "def f():\n"
        "    success, error = delete()\n"
        "    if not success and _is_htmx():\n"
        "        return '<div>x</div>', 422\n"
        "    return redirect(url_for('x.detail'))\n",
        # The old step_delete: the fragment, AND the plain form's refusal
        # landing where a deletion lands.
        "4xx-under-htmx", "refusal-shares-a-success"),
    # Round 2: a 4xx however it is spelled.
    "htmx-abort": (
        "def f():\n"
        "    success, error = delete()\n"
        "    if not success and _is_htmx():\n"
        "        abort(422)\n"
        "    return redirect(url_for('x.detail', erreur=error))\n",
        "4xx-under-htmx"),
    "htmx-response-status": (
        "def f():\n"
        "    success, error = delete()\n"
        "    if not success and _is_htmx():\n"
        "        return Response(f'<div>{error}</div>', status=422)\n"
        "    return redirect(url_for('x.detail', erreur=error))\n",
        "4xx-under-htmx"),
    "htmx-make-response": (
        "def f():\n"
        "    success, error = delete()\n"
        "    if not success and _is_htmx():\n"
        "        resp = make_response(f'<div>{error}</div>', 422)\n"
        "        return resp\n"
        "    return redirect(url_for('x.detail', erreur=error))\n",
        "4xx-under-htmx"),
    "htmx-a-helper-answering-422": (
        "def _fragment(message):\n"
        "    return f'<div>{message}</div>', 422\n"
        "def f():\n"
        "    success, error = delete()\n"
        "    if not success and _is_htmx():\n"
        "        return _fragment(error)\n"
        "    return redirect(url_for('x.detail', erreur=error))\n",
        "4xx-under-htmx"),
    # Round 2: Flask's own 302 in an htmx branch (the old « 302 carrying
    # HX-Redirect », its header gone or not).
    "htmx-a-bare-redirect": (
        "def f():\n"
        "    success, error = delete()\n"
        "    if _is_htmx():\n"
        "        resp = redirect(url_for('x.detail', erreur=error))\n"
        "        return resp\n"
        "    return redirect(url_for('x.detail', erreur=error))\n",
        "302-under-htmx"),
    "except-without-reason": (
        "def f():\n"
        "    try:\n"
        "        record = read_strict()\n"
        "    except Exception:\n"
        "        return redirect(url_for('x.list'))\n"
        "    return redirect(url_for('x.detail'))\n",
        "except-without-reason"),
    # Round 2: a failed read coerced to an absence, then « already gone ».
    "a-failed-read-coerced-to-an-absence": (
        "def f():\n"
        "    try:\n"
        "        record = get_x_strict(1)\n"
        "    except Exception:\n"
        "        record = None\n"
        "    if record is None:\n"
        "        return redirect(url_for('x.list'))\n"
        "    return redirect(url_for('x.detail'))\n",
        "except-without-reason"),
    "hand-built-banner": (
        "def f():\n"
        "    return redirect(f\"{target}?erreur={escape(errors[0])}\")\n",
        "hand-built-query"),
    # Round 2: the same banner, built by hand otherwise.
    "a-banner-concatenated": (
        "def f():\n"
        "    task, errors = save()\n"
        "    if errors:\n"
        "        return redirect(url_for('x.d') + '?erreur=' + errors[0])\n"
        "    return redirect(url_for('x.list'))\n",
        "hand-built-query"),
    "a-banner-percent-formatted": (
        "def f():\n"
        "    task, errors = save()\n"
        "    if errors:\n"
        "        return redirect('%s?erreur=%s' % (url_for('x.d'), "
        "errors[0]))\n"
        "    return redirect(url_for('x.list'))\n",
        "hand-built-query"),
    "a-banner-str-format": (
        "def f():\n"
        "    task, errors = save()\n"
        "    if errors:\n"
        "        return redirect('{}?erreur={}'.format(url_for('x.d'), "
        "errors[0]))\n"
        "    return redirect(url_for('x.list'))\n",
        "hand-built-query"),
    "a-banner-escaped-in-its-keyword": (
        "def f():\n"
        "    task, errors = save()\n"
        "    if errors:\n"
        "        return redirect(url_for('x.d', erreur=escape(errors[0])))\n"
        "    return redirect(url_for('x.list'))\n",
        "escaped-banner"),
    "helper-without-banner": (
        "def _back(i):\n"
        "    return redirect_after_post(url_for('x.detail', i=i))\n"
        "def f():\n"
        "    ok, errors = delete()\n"
        "    if not ok:\n"
        "        return _back(1)\n"
        "    return _back(2)\n",
        "refusal-without-reason"),
    # Round 2: the refusal HANDED to a helper that drops it.
    "a-refusal-handed-to-a-helper-that-drops-it": (
        "def _refused(error):\n"
        "    return redirect_after_post(url_for('x.list'))\n"
        "def f():\n"
        "    success, error = delete()\n"
        "    if not success:\n"
        "        return _refused(error)\n"
        "    return redirect_after_post(url_for('x.list'))\n",
        "refusal-without-reason"),
    # Round 2: a refusal named otherwise is still a refusal.
    "an-outcome-named-problems": (
        "def f():\n"
        "    _t, problems = update()\n"
        "    if problems:\n"
        "        return redirect_after_post(url_for('x.list'))\n"
        "    return redirect_after_post(url_for('x.list'))\n",
        "refusal-without-reason"),
    "refusal-said-only-on-htmx": (
        "def f():\n"
        "    success, error = delete()\n"
        "    target = url_for('x.list')\n"
        "    if _is_htmx() and not success:\n"
        "        target = url_for('x.detail', erreur=error)\n"
        "        return redirect_after_post(target)\n"
        "    return redirect(url_for('x.list'))\n",
        "refusal-shares-a-success"),
}

_CLEAN = {
    "refusal-with-its-reason": (
        "def f():\n"
        "    task, errors = save()\n"
        "    if errors:\n"
        "        return redirect(url_for('x.detail', erreur=errors[0]))\n"
        "    return redirect(url_for('x.list'))\n"),
    "already-gone-quietly": (
        "def f():\n"
        "    success, error = delete()\n"
        "    if not success and error != TASK_NOT_FOUND:\n"
        "        return redirect_after_post(url_for('x.d', erreur=error))\n"
        "    return redirect_after_post(url_for('x.list'))\n"),
    "a-target-that-depends-on-the-outcome": (
        "def f():\n"
        "    rows, errors = move()\n"
        "    target = url_for('x.list', **({'erreur': errors[0]} if errors"
        " else {}))\n"
        "    return redirect(target)\n"),
    "a-coded-banner-built-in-a-dict": (
        "def f():\n"
        "    tree, errors = build()\n"
        "    params = {}\n"
        "    if errors:\n"
        "        params['erreur'] = 'arborescence'\n"
        "    return redirect(url_for('x.d', **params))\n"),
    "a-strict-read-that-says-absent": (
        "def f():\n"
        "    success, report = delete()\n"
        "    if not success:\n"
        "        still = get_x_strict(1)\n"
        "        if still is None:\n"
        "            return redirect(url_for('x.list'))\n"
        "        return redirect(url_for('x.d', erreur='echec'))\n"
        "    return redirect(url_for('x.list'))\n"),
    "a-helper-taking-the-code": (
        "def _back(i, code):\n"
        "    return redirect(url_for('x.d', i=i, gabarit_erreur=code))\n"
        "def f():\n"
        "    try:\n"
        "        t = read()\n"
        "    except ReadError:\n"
        "        return _back(1, 'lecture_impossible')\n"
        "    return send(t)\n"),
    "a-refusal-handed-to-a-helper-that-banners-it": (
        "def _back(i, reason):\n"
        "    return redirect_after_post(url_for('x.d', i=i, erreur=reason))\n"
        "def f():\n"
        "    success, error = delete()\n"
        "    if not success:\n"
        "        return _back(1, error)\n"
        "    return redirect_after_post(url_for('x.list'))\n"),
    "a-target-carrying-its-banner-handed-to-a-wrapper": (
        "def _redirect_to(target):\n"
        "    return redirect(target)\n"
        "def f():\n"
        "    success, error = delete()\n"
        "    if not success:\n"
        "        return _redirect_to(url_for('x.d', erreur=error))\n"
        "    return _redirect_to(url_for('x.list'))\n"),
    "a-failed-read-said-then-an-absence": (
        "def f():\n"
        "    try:\n"
        "        record = get_x_strict(1)\n"
        "    except Exception:\n"
        "        return redirect(url_for('x.d', erreur='lecture'))\n"
        "    if record is None:\n"
        "        return redirect(url_for('x.list'))\n"
        "    return redirect(url_for('x.detail'))\n"),
    "a-plain-form-re-rendered-at-400": (
        "def f():\n"
        "    entry, errors = save()\n"
        "    if errors:\n"
        "        return render_template('form.html', errors=errors), 400\n"
        "    return redirect(url_for('x.d'))\n"),
    "a-bare-redirect-on-the-plain-branch": (
        "def f():\n"
        "    if not _is_htmx():\n"
        "        return redirect(url_for('x.d'))\n"
        "    return redirect_after_post(url_for('x.d'))\n"),
    "a-page-context-named-like-a-banner": (
        "def f():\n"
        "    entry, errors = save()\n"
        "    if errors:\n"
        "        return render_template('f.html', erreur=escape(errors[0]))\n"
        "    return redirect(url_for('x.d'))\n"),
    "a-json-endpoint": (
        "def f():\n"
        "    if not ok_payload():\n"
        "        return jsonify({'erreur': 'Requête invalide.'}), 400\n"
        "    return jsonify({'ok': True})\n"),
    "a-get-view-is-no-post": (
        "@bp.route('/x')\n"
        "def f():\n"
        "    task, errors = read()\n"
        "    if errors:\n"
        "        return redirect(url_for('x.list'))\n"
        "    return render_template('x.html')\n"),
}


@pytest.mark.parametrize("case", list(_FLAGGED.values()), ids=list(_FLAGGED))
def test_the_guard_flags_a_planted_lost_refusal(case):
    snippet, *rules = case
    assert [r for _line, _fn, r in lost_refusals(snippet)] == rules, snippet


@pytest.mark.parametrize("snippet", list(_CLEAN.values()), ids=list(_CLEAN))
def test_the_guard_accepts_a_said_refusal_and_what_is_none(snippet):
    assert lost_refusals(snippet) == [], snippet


def test_a_view_a_template_posts_to_is_htmx_from_its_first_line():
    """Fails on round 1: a 4xx answered before any ``is_htmx()`` test, in
    a view htmx calls (a template's ``hx-post``), went unseen."""
    snippet = (
        "def f():\n"
        "    if not request.form.get('x'):\n"
        "        return '<div>Requis.</div>', 422\n"
        "    return render_template('rows.html')\n")
    assert lost_refusals(snippet) == []
    assert [r for _l, _f, r in lost_refusals(
        snippet, htmx_views=frozenset({"f"}))] == ["4xx-under-htmx"]
    # …but its plain-form branch, proved so, may still re-render at 400.
    plain = (
        "def f():\n"
        "    if not _is_htmx():\n"
        "        return render_template('form.html'), 400\n"
        "    return render_template('rows.html')\n")
    assert lost_refusals(plain, htmx_views=frozenset({"f"})) == []


# Each plant puts back, in the REAL module, one shape guard (a) refuses:
# (module, [(anchor, replacement), …], culprit near the finding, rules).
_TASK_REFUSAL = (
    "    if not success and error != TASK_NOT_FOUND:\n"
    "        return redirect_after_post(url_for(\n"
    "            \"tasks.task_detail\", task_id=task_id, erreur=error,\n"
    "            return_to=return_to or None,\n"
    "        ))\n")
_TASK_DELETE_ROUTE = "@tasks_bp.route(\"/<task_id>/delete\", methods=[\"POST\"])\n"
_TASK_TOGGLE_START = (
    "    task, errors, changed = set_task_completion(\n"
    "        task_id, _toggle_target(task_id)\n"
    "    )\n")
_ADMIN_REFUSAL = (
    "    if errors or not deleted:\n"
    "        return redirect_after_post(url_for(\n"
    "            \"admin_ledger.entry_detail\", tx_id=tx_id,\n"
    "            avertissement=\"suppression\"))\n")
_PROTOCOL_READ_FAILED = (
    "    except Exception as exc:\n"
    "        log_unexpected(\"protocol delete: read failed\", exc_info=False,\n"
    "                       protocol_id=sanitize_log_value(protocol_id),\n"
    "                       error_type=type(exc).__name__)\n"
    "        return redirect_after_post(url_for(\n"
    "            \"protocols.protocol_detail\", protocol_id=protocol_id,\n"
    "            erreur=READ_UNAVAILABLE_ERROR))\n")
_TIME_DOUBLE_FAULT = (
    "        return redirect_after_post(url_for(\n"
    "            \"time_expenses.time_list\", erreur=error))\n")
_MOVE_ABSENT = (
    "    if not doc:\n"
    "        return redirect_after_post(url_for(\n"
    "            \"documents.document_list\", erreur=MOVE_NOT_FOUND))\n")
_FOLDER_MOVE_REFUSAL = (
    "    if errors:\n"
    "        return redirect_after_post(url_for(\n"
    "            \"documents.document_list\", dossier_id=dossier_id,\n"
    "            folder_id=(existing or {}).get(\"parent_folder_id\") or \"\",\n"
    "            erreur=errors[0]), follow=True)\n")
_UNLINK_HEAD = (
    "    updated, errors = unlink_hearing(hearing_id)\n"
    "    if errors:\n")
_UNLINK_REFUSAL = _UNLINK_HEAD + (
    "        return _back_to_hearing(hearing_id, return_to, erreur=errors[0])\n")
_NOTE_ERROR = (
    "        f'rounded-xl\">{escape(message)}</div>'\n"
    "    )\n")

_PLANTS = {
    # The old task_delete: the refusal fell through to the list.
    "task-delete": ("routes/tasks.py", [(_TASK_REFUSAL, "")],
                    "return redirect_after_post(return_to or url_for("
                    "\"tasks.task_list\"))", ["refusal-without-reason"]),
    # The old note_pin: a refused pin sent the lawyer to the list.
    "note-pin": ("routes/notes.py",
                 [("\"notes.note_detail\", note_id=note_id, "
                   "epinglage_erreur=\"erreur\"))",
                   "\"notes.note_list\"))")],
                 "\"notes.note_list\"))", ["refusal-without-reason"]),
    # The old step_delete: a 422 fragment htmx never swaps.
    "step-delete-422": ("routes/protocols.py",
                        [("    success, error = delete_step(protocol_id, "
                          "step_id)\n",
                          "    success, error = delete_step(protocol_id, "
                          "step_id)\n    if not success and _is_htmx():\n"
                          "        return '<div>refus</div>', 422\n")],
                        "return '<div>refus</div>', 422",
                        ["4xx-under-htmx"]),
    # The old hearings banner: built by hand through escape().
    "hearing-escape": ("routes/hearings.py",
                       [("return _back_to_hearing(hearing_id, return_to, "
                         "erreur=error)",
                         "return redirect(f\"{return_to}?erreur={escape("
                         "error)}\")")],
                       "return redirect(f\"{return_to}?erreur=",
                       ["hand-built-query"]),
    # The old administration entry_delete: back on the entry, no word.
    "admin-entry-delete": ("routes/admin_ledger.py",
                           [(_ADMIN_REFUSAL,
                             "    if errors or not deleted:\n"
                             "        return redirect(url_for("
                             "\"admin_ledger.entry_detail\", tx_id=tx_id))\n")],
                           "return redirect(url_for(\"admin_ledger."
                           "entry_detail\", tx_id=tx_id))",
                           ["refusal-without-reason"]),
    # A read that fails, answered like the list (a failed read passing
    # for an absence).
    "protocol-read": ("routes/protocols.py",
                      [(_PROTOCOL_READ_FAILED,
                        "    except Exception:\n"
                        "        return redirect(url_for(\"protocols."
                        "protocol_list\"))\n")],
                      "return redirect(url_for(\"protocols.protocol_list\"))",
                      ["except-without-reason"]),
    # The double fault of a time entry's delete, back to the old silence.
    "time-entry-double-fault": ("routes/time_expenses.py",
                                [(_TIME_DOUBLE_FAULT, "")],
                                "return redirect_after_post(return_to or "
                                "url_for(\"time_expenses.time_list\"))",
                                ["refusal-without-reason"]),
    # The old document_move: a 404 fragment for htmx.
    "document-move-404": ("routes/documents.py",
                          [(_MOVE_ABSENT,
                            "    if not doc:\n"
                            "        if _is_htmx():\n"
                            "            return '<div>Document introuvable."
                            "</div>', 404\n"
                            "        return redirect(url_for("
                            "\"documents.document_list\"))\n")],
                          "return '<div>Document introuvable.</div>', 404",
                          ["4xx-under-htmx"]),
    # ── Round 2: what round 1's sweep did not see ────────────────────
    "task-delete-abort": ("routes/tasks.py",
                          [(_TASK_REFUSAL,
                            "    if not success and _is_htmx():\n"
                            "        abort(422)\n" + _TASK_REFUSAL)],
                          "abort(422)", ["4xx-under-htmx"]),
    "task-delete-response-422": ("routes/tasks.py",
                                 [(_TASK_REFUSAL,
                                   "    if not success and _is_htmx():\n"
                                   "        return Response(f'<div>{error}"
                                   "</div>', status=422)\n" + _TASK_REFUSAL)],
                                 "status=422", ["4xx-under-htmx"]),
    "folder-move-make-response": ("routes/documents.py",
                                  [(_FOLDER_MOVE_REFUSAL,
                                    "    if errors and _is_htmx():\n"
                                    "        return make_response(f'<div>"
                                    "{errors[0]}</div>', 422)\n"
                                    + _FOLDER_MOVE_REFUSAL)],
                                  "make_response(", ["4xx-under-htmx"]),
    # The checkbox's htmx refusal answered with Flask's own 302.
    "task-toggle-bare-302": ("routes/tasks.py",
                             [("        return redirect_after_post(url_for(\n"
                               "            \"tasks.task_list\", "
                               "erreur=errors[0],\n",
                               "        return redirect(url_for(\n"
                               "            \"tasks.task_list\", "
                               "erreur=errors[0],\n"),
                              ("        ), follow=True)\n", "        ))\n")],
                             "return redirect(url_for(", ["302-under-htmx"]),
    # A view a template hx-posts to: htmx before any is_htmx() test.
    "task-toggle-422-up-front": ("routes/tasks.py",
                                 [(_TASK_TOGGLE_START,
                                   "    if not task_id.strip():\n"
                                   "        return '<div>Tâche requise.</div>'"
                                   ", 422\n" + _TASK_TOGGLE_START)],
                                 "Tâche requise.", ["4xx-under-htmx"]),
    # A helper answering a 4xx fragment, returned by an hx-post view.
    "note-error-helper-422": ("routes/invoices.py",
                              [(_NOTE_ERROR, _NOTE_ERROR.replace(
                                  "    )\n", "    ), 422\n"))],
                              "return _note_error(", ["4xx-under-htmx"]),
    # A refusal named otherwise, sent to the list.
    "an-outcome-named-problems": ("routes/tasks.py",
                                  [(_TASK_DELETE_ROUTE,
                                    "@tasks_bp.route(\"/<task_id>/archiver\""
                                    ", methods=[\"POST\"])\n"
                                    "@login_required\n"
                                    "def task_archive(task_id: str) -> str:\n"
                                    "    _t, problems = update_task(task_id, "
                                    "{\"status\": \"annulée\"})\n"
                                    "    if problems:\n"
                                    "        return redirect_after_post(url_for("
                                    "\"tasks.task_list\"))\n"
                                    "    return redirect_after_post(url_for("
                                    "\"tasks.task_detail\", task_id=task_id))"
                                    "\n\n\n" + _TASK_DELETE_ROUTE)],
                                  "url_for(\"tasks.task_list\"))",
                                  ["refusal-without-reason"]),
    # The refusal handed to a helper that drops it.
    "task-delete-helper-drops-the-refusal": (
        "routes/tasks.py",
        [(_TASK_DELETE_ROUTE,
          "def _refused(error):\n"
          "    return redirect_after_post(url_for(\"tasks.task_list\"))\n\n\n"
          + _TASK_DELETE_ROUTE),
         (_TASK_REFUSAL,
          "    if not success and error != TASK_NOT_FOUND:\n"
          "        return _refused(error)\n")],
        "return _refused(error)", ["refusal-without-reason"]),
    # A failed STRICT read coerced to None, then « already gone ».
    "protocol-read-coerced-to-an-absence": (
        "routes/protocols.py",
        [(_PROTOCOL_READ_FAILED,
          "    except Exception:\n"
          "        protocol = None\n")],
        "return redirect_after_post(url_for(\"dossiers.dossier_list\"))",
        ["except-without-reason"]),
    # The unlink's banner, built by hand three other ways — and escaped.
    "unlink-banner-concatenated": (
        "routes/hearings.py",
        [(_UNLINK_REFUSAL, _UNLINK_HEAD
          + "        return redirect(url_for(\"hearings.hearing_detail\", "
          "hearing_id=hearing_id) + \"?erreur=\" + str(escape(errors[0])))\n")],
        "+ \"?erreur=\" +", ["hand-built-query"]),
    "unlink-banner-percent-formatted": (
        "routes/hearings.py",
        [(_UNLINK_REFUSAL, _UNLINK_HEAD
          + "        return redirect(\"%s?erreur=%s\" % (url_for("
          "\"hearings.hearing_detail\", hearing_id=hearing_id), "
          "errors[0]))\n")],
        "\"%s?erreur=%s\" %", ["hand-built-query"]),
    "unlink-banner-str-format": (
        "routes/hearings.py",
        [(_UNLINK_REFUSAL, _UNLINK_HEAD
          + "        return redirect(\"{}?erreur={}\".format(url_for("
          "\"hearings.hearing_detail\", hearing_id=hearing_id), "
          "errors[0]))\n")],
        ".format(", ["hand-built-query"]),
    "unlink-banner-escaped": (
        "routes/hearings.py",
        [(_UNLINK_REFUSAL, _UNLINK_HEAD
          + "        return _back_to_hearing(hearing_id, return_to, "
          "erreur=escape(errors[0]))\n")],
        "erreur=escape(errors[0])", ["escaped-banner"]),
    "folder-move-banner-concatenated": (
        "routes/documents.py",
        [(_FOLDER_MOVE_REFUSAL,
          "    if errors:\n"
          "        target = url_for(\"documents.document_list\", "
          "dossier_id=dossier_id) + \"&erreur=\" + str(escape(errors[0]))\n"
          "        return redirect_after_post(target, follow=True)\n")],
        "+ \"&erreur=\" +", ["hand-built-query"]),
}


@pytest.mark.parametrize("module, edits, culprit, rules",
                         list(_PLANTS.values()), ids=list(_PLANTS))
def test_the_guard_flags_a_lost_refusal_planted_in_a_real_module(
        module, edits, culprit, rules):
    path = _ATHENA / module
    source = path.read_text(encoding="utf-8")
    live = _htmx_views_of(path, _live_htmx_views())
    assert lost_refusals(source, htmx_views=live) == []
    planted = source
    for anchor, replacement in edits:
        assert planted.count(anchor) == 1, (
            "the plant no longer matches the module", anchor)
        planted = planted.replace(anchor, replacement, 1)
    found = lost_refusals(planted, htmx_views=live)
    assert [rule_found for _line, _fn, rule_found in found] == rules
    # …at the very statement the plant exposes.
    lines = planted.splitlines()[found[0][0] - 1:found[0][0] + 2]
    assert culprit in " ".join(lines)


# ── X1: a fail-open pre-read taken for an absence ────────────────────
#
# A POST whose FAIL-OPEN reader (``get_*``, never ``*_strict``) answered
# None — a blip as much as an absence — and that then redirects without a
# word, or answers a 404: no outcome is bound there, so guard (a) cannot
# see it, and « a failed read never passes for absence » (CLAUDE.md) says
# it must read strictly. These views are KNOWN, left to a later lot (the
# refusals lot's leftovers, X1): a NEW one fails here, and curing one
# takes it off the list.
_X1_FAIL_OPEN_PRE_READS = {
    "admin_ledger.py": {"account_edit", "api_recu", "entry_edit",
                        "entry_reverse", "reconciliation_complete",
                        "recu_verser"},
    "budgets.py": {"budget_create"},
    "doc_templates.py": {"generate", "template_update"},
    "notes.py": {"note_gabarit_docx"},
    "protocols.py": {"protocol_update"},
    "trust.py": {"account_edit", "entry_reverse", "reconciliation_complete"},
}


def _fail_open_read(value) -> bool:
    """A fail-open reader's call — or a conditional expression holding
    one (``get_dossier(i) if i else None``)."""
    if isinstance(value, ast.IfExp):
        return _fail_open_read(value.body) or _fail_open_read(value.orelse)
    name = _call_name(value) if isinstance(value, ast.Call) else ""
    return bool(name) and name.startswith("get_") and not name.endswith(
        "_strict")


def _fail_open_absences(source: str) -> set:
    """The POST views of *source* that answer a fail-open read's None —
    ``if not x:`` / ``if x is None:`` — with a redirect saying nothing, or
    with a 404."""
    tree = ast.parse(source)
    module = _Module(tree)
    found = set()
    for fn in _swept(tree):
        if not _route_decorators(fn):
            continue                        # a helper: no view of its own
        assigns = _assigns(fn)
        fail_open = {name for name, values in assigns.items()
                     if any(_fail_open_read(v) for v in values)}
        for node in ast.walk(fn):
            if not (isinstance(node, ast.If)
                    and _tests_absence_of(node.test, fail_open)):
                continue
            for stmt in node.body:
                if not isinstance(stmt, ast.Return) or stmt.value is None:
                    continue
                if ((module.is_redirect(stmt.value, assigns)
                     and not module.says(stmt.value, assigns))
                        or _status_of(stmt.value, assigns) == 404):
                    found.add(fn.name)
    return found


def test_no_new_post_takes_a_fail_open_read_for_an_absence():
    found = {}
    for path in sorted((_ATHENA / "routes").glob("*.py")):
        views = _fail_open_absences(path.read_text(encoding="utf-8"))
        if views:
            found[path.name] = views
    assert found == _X1_FAIL_OPEN_PRE_READS


def test_the_x1_check_flags_a_planted_fail_open_pre_read():
    """Proved on a plant: protocol_delete's strict pre-read put back to the
    fail-open read it replaced (a blip landing on the dossier list)."""
    source = (_ATHENA / "routes/protocols.py").read_text(encoding="utf-8")
    anchor = ("    try:\n        protocol = get_protocol_strict(protocol_id)\n"
              + _PROTOCOL_READ_FAILED)
    assert source.count(anchor) == 1
    planted = source.replace(
        anchor, "    protocol = get_protocol(protocol_id)\n", 1)
    assert _fail_open_absences(planted) - _fail_open_absences(source) == {
        "protocol_delete"}


# ══════════════════════════════════════════════════════════════════════
# 3. Guard (b): HX-Redirect is the helper's
# ══════════════════════════════════════════════════════════════════════


def _hx_redirect_sets(source: str) -> list:
    """The lines naming the header — in ANY case, as HTTP reads it (a
    string constant, never a comment or a docstring's mention of it)."""
    return [node.lineno for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value.lower() == "hx-redirect"]


def test_hx_redirect_is_set_by_the_helper_alone():
    """Fails on the old code: 51 hand-written ``HX-Redirect``, most on a
    302 the browser's XHR follows before htmx can read it."""
    found = {}
    for path in sorted((_ATHENA / "routes").glob("*.py")):
        lines = _hx_redirect_sets(path.read_text(encoding="utf-8"))
        if lines:
            found[path.name] = lines
    assert set(found) == {"_helpers.py"}, found
    assert len(found["_helpers.py"]) == 1


@pytest.mark.parametrize("plant", [
    '    resp.headers["HX-Redirect"] = target\n',
    '    return Response(status=200, headers={"HX-Redirect": target})\n',
    # Fails on round 1: the header named in lower case — the same header
    # to HTTP — passed.
    '    resp.headers["hx-redirect"] = target\n',
])
def test_guard_b_flags_a_planted_hand_written_header(plant):
    source = (_ATHENA / "routes/tasks.py").read_text(encoding="utf-8")
    anchor = "def task_delete(task_id: str) -> str:\n"
    assert source.count(anchor) == 1
    planted = source.replace(anchor, f"def _plant(target):\n{plant}\n\n"
                             + anchor, 1)
    assert len(_hx_redirect_sets(planted)) == 1


# ══════════════════════════════════════════════════════════════════════
# 4. The answer's shape: the helper's 200 under htmx, but the swaps
# ══════════════════════════════════════════════════════════════════════

# The views whose 302 htmx's XHR FOLLOWS (``redirect_after_post(…,
# follow=True)``) to swap a fragment in place: the task list's checkbox;
# the document browser's folder forms, should htmx ever post them
# (rendered by Alpine x-if, they post without it today); and its two
# orphan routes, answered like them. The ONE list of them: the source is
# pinned to it below, and ``_landing`` reads it.
_IN_PLACE_SWAPS = frozenset({
    "tasks.task_toggle", "documents.folder_create",
    "documents.folder_rename", "documents.folder_move",
    "documents.folder_delete_route", "documents.document_move_bulk",
})


def _follow_users(path_name: str, source: str) -> set:
    """``blueprint.view`` of every function of *source* passing ``follow``
    (anything but ``False``) to the helper — ``<module>:<function>`` for
    one that is no view."""
    tree = ast.parse(source)
    blueprints = _blueprints_of(tree)
    users = set()
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        if not any(isinstance(n, ast.Call)
                   and _call_name(n) == "redirect_after_post"
                   and any(k.arg == "follow" and not (
                       isinstance(k.value, ast.Constant)
                       and k.value.value is False) for k in n.keywords)
                   for n in ast.walk(fn)):
            continue
        views = [f"{blueprints[d.func.value.id]}.{fn.name}"
                 for d in _route_decorators(fn)
                 if d.func.value.id in blueprints]
        users |= set(views) or {f"{path_name}:{fn.name}"}
    return users


def test_only_the_in_place_swaps_keep_the_followed_302():
    """Pins ``follow=True`` to the declared swaps: a refusal or a success
    block switched to it would answer htmx a 302 whose target the XHR
    fetches and swaps WHOLE into the form — the bug of this lot."""
    users = set()
    for path in sorted((_ATHENA / "routes").glob("*.py")):
        users |= _follow_users(path.name, path.read_text(encoding="utf-8"))
    assert users == _IN_PLACE_SWAPS


def test_the_follow_pin_flags_a_refusal_switched_to_follow():
    source = (_ATHENA / "routes/tasks.py").read_text(encoding="utf-8")
    assert source.count(_TASK_REFUSAL) == 1
    planted = source.replace(_TASK_REFUSAL, _TASK_REFUSAL.replace(
        "        ))\n", "        ), follow=True)\n"), 1)
    assert _follow_users("tasks.py", planted) - _follow_users(
        "tasks.py", source) == {"tasks.task_delete"}


# ══════════════════════════════════════════════════════════════════════
# 5. The sites — real routes, templates and models over the fake store
# ══════════════════════════════════════════════════════════════════════

_BLUEPRINTS = (
    auth_routes.auth_bp, budgets_routes.budgets_bp,
    dashboard_routes.dashboard_bp, doc_templates_routes.doc_templates_bp,
    documents_routes.documents_bp, dossiers_routes.dossiers_bp,
    hearings_routes.hearings_bp, invoices_routes.invoices_bp,
    notes_routes.notes_bp, parties_routes.parties_bp,
    protocols_routes.protocols_bp, tasks_routes.tasks_bp,
    time_expenses_routes.time_expenses_bp, admin_routes.admin_bp,
    trust_routes.trust_bp,
)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, *_fake_modules())


@pytest.fixture
def client(db, monkeypatch):
    # The document page signs a download URL: no Storage here.
    monkeypatch.setattr(documents_routes, "get_signed_url",
                        lambda *a, **k: None)
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms,
                                 csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl, phone=format_phone_display,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=jsattr, markdown=markdown_to_safe_html,
    )
    for bp in _BLUEPRINTS:
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


@pytest.fixture
def did(db) -> str:
    dossier, errors = dossier_model.create_dossier({
        "file_number": "2026-001", "title": "Tremblay c. Lavoie",
        "clients": [{"id": "p1", "name": "Jean Tremblay",
                     "roles": ["demandeur"]}],
    })
    assert errors == [], errors
    return dossier["id"]


@functools.lru_cache(maxsize=None)
def _routing() -> Flask:
    """The blueprints' routing, to name the view a test POSTed to."""
    app = Flask(__name__)
    for bp in _BLUEPRINTS:
        app.register_blueprint(bp)
    return app


def _landing(resp) -> tuple:
    """Where an answer sends the browser — the Location of a 302, or the
    HX-Redirect of htmx's 200 (which then carries no Location) — once its
    SHAPE is checked: a POST made under htmx gets the helper's 200 +
    ``HX-Redirect`` and no ``Location`` (round 2: a landing switched back
    to a bare ``redirect()`` or to ``follow=True`` answered a 302 the XHR
    follows, and passed), except a declared in-place swap, whose 302 the
    XHR follows on purpose."""
    request_ = resp.request
    if (request_.method == "POST"
            and request_.headers.get("HX-Request") == "true"):
        endpoint, _args = _routing().url_map.bind("localhost").match(
            request_.path, method="POST")
        if endpoint in _IN_PLACE_SWAPS:
            assert resp.status_code == 302, (endpoint, resp.status_code)
        else:
            assert resp.status_code == 200, (endpoint, resp.status_code)
            assert "Location" not in resp.headers, endpoint
    if resp.status_code == 302:
        assert "HX-Redirect" not in resp.headers
        target = resp.headers["Location"]
    else:
        assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
        assert "Location" not in resp.headers
        target = resp.headers["HX-Redirect"]
    parts = urlsplit(target)
    return parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}


def _url(resp) -> str:
    return resp.headers.get("Location") or resp.headers["HX-Redirect"]


def _shown(html: str, sentence: str) -> bool:
    return str(escape(sentence)) in html


def _fail_reads(monkeypatch, db, path: str) -> dict:
    """Every point read of *path* fails at the transport while
    ``state["on"]`` holds — the outage a refusal is born of; turn it off
    to read the landing page."""
    server = db._fake_server
    real = server.batch_get_documents
    state = {"on": True}

    def failing(request, metadata=None, **kwargs):
        rels = [server.doc_rel(n) for n in request["documents"]]
        if state["on"] and path in rels:
            raise gexc.ServiceUnavailable("injected read failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", failing)
    return state


def _commit_raises_on(monkeypatch, db, prefix: str) -> None:
    """Every commit writing a document under *prefix* fails before
    applying anything."""
    server = db._fake_server
    real = server.commit

    def commit(request, metadata=None, **kwargs):
        for write in request.get("writes") or []:
            pb = getattr(write, "_pb", write)
            op = pb.WhichOneof("operation")
            name = pb.delete if op == "delete" else (
                pb.update.name if op == "update" else "")
            if name and server.doc_rel(name).startswith(prefix):
                raise gexc.ServiceUnavailable("injected commit failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "commit", commit)


def _dav_state(db) -> dict:
    """Every DAV sync document and its tombstones: a refusal changes none."""
    return {name: (doc, db.peek_collection(f"dav_sync/{name}/tombstones"))
            for name, doc in db.peek_collection("dav_sync").items()}


def _audit_rows(db) -> list:
    return list(db.peek_collection("audit_events").values())


def _logged(caplog, message: str) -> list:
    """The ``pallas.unexpected`` lines of *message*: their structured
    fields, and whether a traceback rode along (never: the ids only)."""
    return [({k: v for k, v in r.json_fields.items() if k != "event"},
             bool(r.exc_info or r.exc_text))
            for r in caplog.records
            if r.name == "pallas.unexpected" and r.getMessage() == message]


def _new_task(did: str, **over) -> str:
    doc, errors = task_model.create_task(
        {"dossier_id": did, "title": "Préparer la requête", **over})
    assert errors == [], errors
    return doc["id"]


def _new_hearing(did: str) -> str:
    doc, errors = hearing_model.create_hearing({
        "dossier_id": did, "title": "Audience", "hearing_type": "audience",
        "status": "confirmée",
        "start_datetime": datetime(2026, 10, 15, 13, tzinfo=UTC),
        "end_datetime": datetime(2026, 10, 15, 14, tzinfo=UTC)})
    assert errors == [], errors
    return doc["id"]


def _new_chain(did: str) -> str:
    """The first occurrence of a weekly chain: 2026-10-15, then -22."""
    rows, errors = hearing_model.create_hearing_series({
        "dossier_id": did, "title": "Audience", "hearing_type": "audience",
        "status": "confirmée",
        "start_datetime": datetime(2026, 10, 15, 13, tzinfo=UTC),
        "end_datetime": datetime(2026, 10, 15, 14, tzinfo=UTC),
    }, "hebdomadaire", count=2)
    assert errors == [], errors
    return rows[0]["id"]


def _new_protocol(did: str) -> str:
    doc, errors = protocol_model.create_protocol(
        did, "conventionnel", DT, {"title": "Protocole", "notes": ""})
    assert errors == [], errors
    return doc["id"]


def _new_step(db, pid: str, *, mandatory: bool) -> str:
    db.seed(f"protocols/{pid}/steps/s1", {
        **protocol_model._default_step(), "id": "s1", "order": 1,
        "title": "Réponse", "mandatory": mandatory,
        "deadline_date": datetime(2099, 12, 1, tzinfo=UTC),
        "created_at": DT, "updated_at": DT, "etag": "e-step-1",
    })
    return "s1"


def _new_note(did: str) -> str:
    doc, errors = note_model.create_note({
        "dossier_id": did, "title": "Recherche", "content": "Premier jet.",
        "category": "recherche"})
    assert errors == [], errors
    return doc["id"]


_GONE = "a0000000-0000-4000-8000-000000000000"


# ── The response header ──────────────────────────────────────────────


def test_an_htmx_save_names_a_non_latin1_return_to_as_a_valid_header(
        db, client, did):
    """Fails on the old code: HX-Redirect carried « ā » (U+0101) raw — a
    character no WSGI header can carry: gunicorn refuses it, after the
    task had been created."""
    resp = client.post("/taches/", data={
        "dossier_id": did, "title": "Nouvelle", "priority": "normale",
        "status": "à_faire", "category": "autre",
        "return_to": "/taches/ā"}, headers=HX)
    assert resp.status_code == 200 and "Location" not in resp.headers
    hx = resp.headers["HX-Redirect"]
    assert _HEADER_SAFE.fullmatch(hx), hx
    assert hx == "/taches/%C4%81"
    assert len(db.peek_collection("tasks")) == 1


# ── Tasks ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("headers", [{}, HX], ids=["plain", "htmx"])
def test_a_refused_task_delete_lands_on_the_task_and_says_why(
        db, client, did, monkeypatch, headers):
    """Fails on the old code: the plain form landed on the dossier tab like
    a deletion — the task, and its copy on the phone, still there — and
    htmx got a 422 fragment it never swaps."""
    tid = _new_task(did)
    dav, path = _dav_state(db), f"tasks/{tid}"
    outage = _fail_reads(monkeypatch, db, path)
    back = f"/dossiers/{did}?tab=taches"
    resp = client.post(f"/taches/{tid}/delete", data={"return_to": back},
                       headers=headers)
    assert _landing(resp) == (f"/taches/{tid}", {
        "erreur": READ_UNAVAILABLE, "return_to": back})
    outage["on"] = False
    assert db.peek(path) is not None
    assert _dav_state(db) == dav and _audit_rows(db) == []
    page = client.get(_url(resp)).get_data(as_text=True)
    assert _shown(page, READ_UNAVAILABLE)
    assert f'href="{escape(back)}"' in page          # its « Retour » kept


def test_a_task_already_gone_goes_where_a_deletion_goes_quietly(
        db, client, did):
    """Fails on the old code under htmx (a 422 fragment)."""
    back = f"/dossiers/{did}?tab=taches"
    for headers in ({}, HX):
        resp = client.post(f"/taches/{_GONE}/delete",
                           data={"return_to": back}, headers=headers)
        assert _landing(resp) == (f"/dossiers/{did}", {"tab": "taches"})


def test_the_task_page_prints_the_banner(db, client, did):
    """Fails on the old code: the page had no banner at all."""
    tid = _new_task(did)
    page = client.get(f"/taches/{tid}?erreur={quote(NOT_A_SERIES)}")
    assert _shown(page.get_data(as_text=True), NOT_A_SERIES)


def test_a_task_the_page_cannot_show_forwards_the_banner_to_the_list(
        db, client):
    """Fails on the old code: the list, the refusal dropped on the way."""
    resp = client.get(f"/taches/{_GONE}?erreur={quote(READ_UNAVAILABLE)}")
    assert _landing(resp) == ("/taches/", {"erreur": READ_UNAVAILABLE})
    rows = client.get(_url(resp), headers=HX).get_data(as_text=True)
    assert _shown(rows, READ_UNAVAILABLE)


def test_a_refused_toggle_from_the_task_page_lands_on_the_task(
        db, client, did):
    """Fails on round 1: the task page's « Marquer comme terminée » (a
    plain form) landed on the task LIST — where CANCELLED_MEANWHILE then
    sent the lawyer back to « sa fiche ». It lands on the task, which says
    why; the list's checkbox (htmx) keeps its in-place swap (pinned by
    test_the_in_place_swaps_keep_their_followed_302)."""
    tid = _new_task(did, status="annulée")
    resp = client.post(f"/taches/{tid}/toggle", data={"target": "terminée"})
    assert _landing(resp) == (f"/taches/{tid}", {
        "erreur": task_model.CANCELLED_MEANWHILE})
    assert db.peek(f"tasks/{tid}")["status"] == "annulée"
    assert _shown(client.get(_url(resp)).get_data(as_text=True),
                  task_model.CANCELLED_MEANWHILE)
    # A task gone meanwhile: its page forwards the refusal to the list.
    gone = client.post(f"/taches/{_GONE}/toggle", data={"target": "terminée"})
    path, query = _landing(gone)
    assert path == f"/taches/{_GONE}" and query["erreur"]
    forwarded = client.get(_url(gone))
    assert _landing(forwarded) == ("/taches/", {"erreur": query["erreur"]})


def _dav_calls(monkeypatch, module) -> list:
    calls: list = []
    monkeypatch.setattr(module, "record_tombstone", lambda scope, rid: (
        calls.append(("tombstone", scope, rid))))
    monkeypatch.setattr(module, "bump_ctag", lambda scope: (
        calls.append(("ctag", scope))))
    monkeypatch.setattr(module, "record_deletion", lambda kind, rid, **kw: (
        calls.append(("journal", kind, rid))))
    return calls


@pytest.mark.parametrize("kind", ["task", "hearing"])
def test_a_deletion_keeps_its_dav_order(db, client, did, monkeypatch, kind):
    """Change Impact 2: the tombstone, THEN the CTag, then one journal row
    — in the collection of the record the model deleted. Unchanged by this
    lot (it passes on the old code: it pins what must not move)."""
    if kind == "task":
        rid, url, module = _new_task(did), "/taches/{}/delete", tasks_routes
    else:
        rid, url = _new_hearing(did), "/audiences/{}/delete"
        module = hearings_routes
    calls = _dav_calls(monkeypatch, module)
    for headers in ({}, HX):
        calls.clear()
        rid = rid if headers == {} else (
            _new_task(did) if kind == "task" else _new_hearing(did))
        resp = client.post(url.format(rid), data={}, headers=headers)
        assert resp.status_code in (200, 302)
        scope = f"dossier:{did}"
        assert calls == [("tombstone", scope, rid), ("ctag", scope),
                         ("journal", kind, rid)]


@pytest.mark.parametrize("kind", ["task", "hearing"])
def test_a_refused_deletion_touches_no_dav_state(
        db, client, did, monkeypatch, kind):
    if kind == "task":
        rid, url, path = _new_task(did), "/taches/{}/delete", "tasks/{}"
        module = tasks_routes
    else:
        rid, url = _new_hearing(did), "/audiences/{}/delete"
        path, module = "hearings/{}", hearings_routes
    calls = _dav_calls(monkeypatch, module)
    _commit_raises_on(monkeypatch, db, path.format(rid))
    resp = client.post(url.format(rid), data={})
    assert _landing(resp)[1] == {"erreur": DELETE_FAILED}
    assert calls == [] and db.peek(path.format(rid)) is not None


# ── Hearings ─────────────────────────────────────────────────────────


@pytest.fixture
def before_the_chain(monkeypatch):
    """A Montréal day before the chain (Rule 5: frozen)."""
    monkeypatch.setattr(hearings_routes, "today_mtl",
                        lambda: date(2026, 10, 1))


@pytest.mark.parametrize("headers", [{}, HX], ids=["plain", "htmx"])
def test_a_refused_hearing_delete_lands_on_the_hearing_and_says_why(
        db, client, did, monkeypatch, headers):
    """Fails on the old code: the dossier tab, like a deletion; htmx 422."""
    hid = _new_hearing(did)
    dav = _dav_state(db)
    outage = _fail_reads(monkeypatch, db, f"hearings/{hid}")
    back = f"/dossiers/{did}?tab=audiences"
    resp = client.post(f"/audiences/{hid}/delete",
                       data={"return_to": back}, headers=headers)
    assert _landing(resp) == (f"/audiences/{hid}", {
        "erreur": READ_UNAVAILABLE, "return_to": back})
    outage["on"] = False
    assert db.peek(f"hearings/{hid}") is not None and _dav_state(db) == dav
    page = client.get(_url(resp)).get_data(as_text=True)
    assert _shown(page, READ_UNAVAILABLE)


@pytest.mark.parametrize("headers", [{}, HX], ids=["plain", "htmx"])
def test_an_unreadable_chain_says_the_whole_sentence_on_the_hearing(
        db, client, did, monkeypatch, before_the_chain, headers):
    """Fails on the old code: escape() cut the sentence at its apostrophe
    — « Cet élément n » — on the dossier tab, which prints no free text."""
    hid = _new_chain(did)
    outage = _fail_reads(monkeypatch, db, f"hearings/{hid}")
    back = f"/dossiers/{did}?tab=audiences"
    resp = client.post(f"/audiences/{hid}/delete",
                       data={"scope": "suivantes", "return_to": back},
                       headers=headers)
    assert _landing(resp) == (f"/audiences/{hid}", {
        "erreur": READ_UNAVAILABLE, "return_to": back})
    outage["on"] = False
    assert len(db.peek_collection("hearings")) == 2
    assert _shown(client.get(_url(resp)).get_data(as_text=True),
                  READ_UNAVAILABLE)


@pytest.mark.parametrize("headers", [{}, HX], ids=["plain", "htmx"])
def test_a_refused_chain_lands_on_the_hearing(db, client, did, monkeypatch,
                                             before_the_chain, headers):
    """Fails on the old code: the dossier tab (no free text there)."""
    hid = _new_chain(did)
    _commit_raises_on(monkeypatch, db, "hearings/")
    resp = client.post(f"/audiences/{hid}/delete", data={
        "scope": "suivantes", "return_to": f"/dossiers/{did}?tab=audiences"},
        headers=headers)
    path, query = _landing(resp)
    assert (path, query["erreur"]) == (f"/audiences/{hid}", DELETE_FAILED)
    assert len(db.peek_collection("hearings")) == 2
    assert _shown(client.get(_url(resp)).get_data(as_text=True),
                  DELETE_FAILED)


@pytest.mark.parametrize("headers", [{}, HX], ids=["plain", "htmx"])
def test_a_chain_with_nothing_ahead_says_so_on_the_hearing(
        db, client, did, monkeypatch, headers):
    """Fails on the old code: « Aucune occurrence à venir à supprimer. »
    went to the dossier tab, whose banners are closed codes — lost."""
    monkeypatch.setattr(hearings_routes, "today_mtl",
                        lambda: date(2026, 12, 1))
    hid = _new_chain(did)
    resp = client.post(f"/audiences/{hid}/delete", data={
        "scope": "suivantes", "return_to": f"/dossiers/{did}?tab=audiences"},
        headers=headers)
    path, query = _landing(resp)
    assert (path, query["message"]) == (
        f"/audiences/{hid}", "Aucune occurrence à venir à supprimer.")
    assert _shown(client.get(_url(resp)).get_data(as_text=True),
                  "Aucune occurrence à venir à supprimer.")
    assert len(db.peek_collection("hearings")) == 2 and _audit_rows(db) == []


def test_a_deleted_chain_lands_where_a_save_lands_with_its_count(
        db, client, did, before_the_chain):
    """Unchanged (it passes on the old code too): the success message,
    now merged into the return_to's own query (with_query), never appended
    by hand."""
    hid = _new_chain(did)
    back = f"/dossiers/{did}?tab=audiences"
    resp = client.post(f"/audiences/{hid}/delete",
                       data={"scope": "suivantes", "return_to": back})
    assert _landing(resp) == (f"/dossiers/{did}", {
        "tab": "audiences", "message": "Série supprimée — 2 occurrences."})
    assert db.peek_collection("hearings") == {}
    assert [r["entity_type"] for r in _audit_rows(db)] == ["hearing_series"]


def test_a_refused_unlink_says_its_whole_sentence_on_the_hearing(
        db, client, did):
    """Fails on the old code: the sentence was cut at « d' » and landed on
    a page that rendered no banner."""
    hid = _new_hearing(did)                      # in no series
    resp = client.post(f"/audiences/{hid}/detacher", data={}, headers=HX)
    assert _landing(resp) == (f"/audiences/{hid}", {"erreur": NOT_A_SERIES})
    assert _shown(client.get(_url(resp)).get_data(as_text=True),
                  NOT_A_SERIES)


def test_a_hearing_the_page_cannot_show_forwards_both_banners(db, client):
    """Fails on the old code: both dropped on the way to the calendar."""
    resp = client.get(f"/audiences/{_GONE}?erreur={quote(NOT_A_SERIES)}"
                      f"&message={quote('Rien à faire.')}")
    assert _landing(resp) == ("/audiences/", {"erreur": NOT_A_SERIES,
                                              "message": "Rien à faire."})
    page = client.get(_url(resp)).get_data(as_text=True)
    assert _shown(page, NOT_A_SERIES) and _shown(page, "Rien à faire.")


@pytest.mark.parametrize("headers", [{}, HX], ids=["plain", "htmx"])
def test_an_unreadable_hearing_is_never_unlinked_nor_called_introuvable(
        db, client, did, monkeypatch, before_the_chain, caplog, headers):
    """Fails on the old code: the fail-open reads answered « Audience
    introuvable. » about a hearing still there — on a page that printed no
    banner. Read strictly, nothing is attempted, and the page says why.
    Fails on round 1 too: the failed read left no trace in the logs (the
    fail-open reads it replaced each logged one) — now one line, the ids
    only."""
    hid = _new_chain(did)
    outage = _fail_reads(monkeypatch, db, f"hearings/{hid}")
    with caplog.at_level(logging.ERROR, logger="pallas.unexpected"):
        resp = client.post(f"/audiences/{hid}/detacher", data={},
                           headers=headers)
    assert _landing(resp) == (f"/audiences/{hid}",
                              {"erreur": READ_UNAVAILABLE})
    assert _logged(caplog, "hearing unlink: read failed") == [
        ({"hearing_id": hid, "error_type": "ServiceUnavailable"}, False)]
    outage["on"] = False
    assert db.peek(f"hearings/{hid}")["serie_id"]
    assert _shown(client.get(_url(resp)).get_data(as_text=True),
                  READ_UNAVAILABLE)


# ── Protocols and their steps ────────────────────────────────────────


@pytest.mark.parametrize("headers", [{}, HX], ids=["plain", "htmx"])
def test_a_refused_protocol_delete_lands_on_the_protocol(
        db, client, did, monkeypatch, headers):
    """Fails on the old code: the dossier, like a deletion; htmx 422."""
    pid = _new_protocol(did)
    _commit_raises_on(monkeypatch, db, f"protocols/{pid}")
    resp = client.post(f"/protocoles/{pid}/delete", headers=headers)
    assert _landing(resp) == (f"/protocoles/{pid}", {"erreur": DELETE_FAILED})
    assert db.peek(f"protocols/{pid}") is not None and _audit_rows(db) == []
    assert _shown(client.get(_url(resp)).get_data(as_text=True),
                  DELETE_FAILED)


def test_an_unreadable_protocol_is_never_called_introuvable(
        db, client, did, monkeypatch, caplog):
    """Fails on the old code: the fail-open reads answered « Protocole
    introuvable. » — and the routes the dossier list, without a word, the
    protocol still there. Fails on round 1 too: the routes' failed read,
    which refuses before the model is called, left no trace in the logs —
    now the model's own line, the ids only, from either."""
    pid = _new_protocol(did)
    outage = _fail_reads(monkeypatch, db, f"protocols/{pid}")
    ids = {"protocol_id": pid, "error_type": "ServiceUnavailable"}
    with caplog.at_level(logging.ERROR, logger="pallas.unexpected"):
        assert protocol_model.delete_protocol(pid) == (False,
                                                       READ_UNAVAILABLE)
        assert protocol_model.delete_step(pid, "s1") == (False,
                                                         READ_UNAVAILABLE)
        assert _logged(caplog, "protocol delete: read failed") == [
            (ids, False)]
        assert _logged(caplog, "protocol step delete: read failed") == [
            (ids, False)]
        for url, message in (
                (f"/protocoles/{pid}/delete", "protocol delete: read failed"),
                (f"/protocoles/{pid}/steps/s1/delete",
                 "protocol step delete: read failed")):
            for headers in ({}, HX):
                caplog.clear()
                assert _landing(client.post(url, headers=headers)) == (
                    f"/protocoles/{pid}", {"erreur": READ_UNAVAILABLE})
                assert _logged(caplog, message) == [(ids, False)]
    outage["on"] = False
    assert db.peek(f"protocols/{pid}") is not None and _audit_rows(db) == []


def test_an_id_the_store_refuses_is_an_absent_protocol(db, client, monkeypatch):
    """Fails on round 1: the strict read that now decides a deletion sent
    an id Firestore refuses (``__x__``) to the store, and its refusal came
    back « lecture impossible — réessayez » — for an id no retry will ever
    read. It is an absence (``document.is_addressable_id``): nothing read,
    « Protocole introuvable. » from the model, the list from the route."""
    server = db._fake_server
    real = server.batch_get_documents

    def refusing(request, metadata=None, **kwargs):
        # Firestore refuses a document id matching ``__.*__``.
        if any(server.doc_rel(n).rsplit("/", 1)[-1].startswith("__")
               for n in request["documents"]):
            raise gexc.InvalidArgument("invalid document id")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", refusing)
    db.reset_logs()
    assert protocol_model.get_protocol_strict("__x__") is None
    assert protocol_model.delete_protocol("__x__") == (
        False, protocol_model.PROTOCOL_NOT_FOUND)
    assert protocol_model.delete_step("__x__", "s1") == (
        False, protocol_model.PROTOCOL_NOT_FOUND)
    assert db.reads == []
    assert _landing(client.post("/protocoles/__x__/delete")) == (
        "/dossiers/", {})
    # The connector's protocol edits read through it (Change Impact 1):
    # « introuvable », never « réessayez », for such an id.
    with mock.patch("google.cloud.firestore.Client"):
        from mcp import handlers, tools
    with pytest.raises(tools.ToolArgumentError,
                       match="Protocole introuvable"):
        handlers._read_protocol("__x__")
    assert db.reads == []


def test_a_protocol_already_gone_goes_to_the_list_quietly(db, client):
    for headers in ({}, HX):
        resp = client.post(f"/protocoles/{_GONE}/delete", headers=headers)
        assert _landing(resp) == ("/dossiers/", {})


@pytest.mark.parametrize("headers", [{}, HX], ids=["plain", "htmx"])
def test_a_refused_step_delete_says_why_on_the_protocol(
        db, client, did, headers):
    """Fails on the old code: the page reloaded without a word; htmx
    422."""
    pid = _new_protocol(did)
    _new_step(db, pid, mandatory=True)
    resp = client.post(f"/protocoles/{pid}/steps/s1/delete", headers=headers)
    assert _landing(resp) == (f"/protocoles/{pid}", {"erreur": MANDATORY_STEP})
    assert db.peek(f"protocols/{pid}/steps/s1") is not None
    assert _shown(client.get(_url(resp)).get_data(as_text=True),
                  MANDATORY_STEP)


def test_a_step_deleted_or_already_gone_lands_on_its_protocol_quietly(
        db, client, did):
    pid = _new_protocol(did)
    _new_step(db, pid, mandatory=False)
    for _ in range(2):                           # deleted, then gone
        resp = client.post(f"/protocoles/{pid}/steps/s1/delete", headers=HX)
        assert _landing(resp) == (f"/protocoles/{pid}", {})
    assert db.peek(f"protocols/{pid}/steps/s1") is None


def test_a_long_update_notice_is_shown_whole_on_the_protocol(
        db, client, did, monkeypatch):
    """Fails on round 1: the protocol page cut its ``message`` at 300
    characters — and protocol_update's own notice runs past that when five
    of its branches fire at once, the cut dropping the instruction that
    ends it (« …vérifiez depuis la fiche de la t »). Before the lot it was
    printed uncapped."""
    pid = _new_protocol(did)
    report = {"moved": 1, "preserved_completed": 1, "aligned": 1,
              "diverged": 1, "failed": 1}
    notice = protocols_routes._update_notice(report)
    assert len(notice) > 300
    monkeypatch.setattr(protocols_routes.protocol_service, "update_protocol",
                        lambda *a, **k: ({"id": pid}, [], dict(report)))
    for headers in ({}, HX):
        resp = client.post(f"/protocoles/{pid}", data={
            "title": "Protocole", "notes": "", "start_date": "2026-03-05"},
            headers=headers)
        assert _landing(resp) == (f"/protocoles/{pid}", {"message": notice})
        assert _shown(client.get(_url(resp)).get_data(as_text=True), notice)


# ── Notes ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("headers", [{}, HX], ids=["plain", "htmx"])
def test_a_refused_pin_says_so_on_the_note(db, client, did, monkeypatch,
                                           headers):
    """Fails on the old code: the note list, without a word; htmx 422."""
    nid = _new_note(did)
    _commit_raises_on(monkeypatch, db, f"notes/{nid}")
    resp = client.post(f"/notes/{nid}/pin", data={"pinned": "1"},
                       headers=headers)
    assert _landing(resp) == (f"/notes/{nid}", {"epinglage_erreur": "erreur"})
    assert not db.peek(f"notes/{nid}").get("pinned")
    assert _shown(client.get(_url(resp)).get_data(as_text=True), PIN_REFUSED)


def test_a_pin_under_htmx_lands_on_the_note_whatever_the_referer(
        db, client, did):
    """Fails on the old code: a 302 to the LIST unless the Referer named
    the note."""
    nid = _new_note(did)
    resp = client.post(f"/notes/{nid}/pin", data={"pinned": "1"}, headers=HX)
    assert _landing(resp) == (f"/notes/{nid}", {})
    assert db.peek(f"notes/{nid}")["pinned"] is True


# ── Administration ───────────────────────────────────────────────────


@pytest.fixture
def entry(db) -> dict:
    db.seed("admin_accounts/ops1", {
        "id": "ops1", "name": "Opérations", "status": "actif",
        "account_type": "opérations", "ledger_balance": 0, "etag": "e0",
    })
    doc, errors = al.create_transaction({
        "account_id": "ops1", "kind": "dépense", "amount": 10000,
        "method": "virement", "counterparty": "Fournisseur fictif",
        "date": DT, "description": "", "reference": "",
        "supplier_invoice_ref": "", "category": "loyer"})
    assert errors == [], errors
    return doc


@pytest.mark.parametrize("headers", [{}, HX], ids=["plain", "htmx"])
def test_an_entry_locked_since_the_page_says_so(db, client, entry, headers):
    """Fails on the old code: back on the entry without a word — the
    connector clears entries, so the lock can come after the page."""
    path = f"admin_transactions/{entry['id']}"
    db.external_write(path, {**db.peek(path), "status": "compensée",
                             "etag": "e-cleared"})
    resp = client.post(f"/administration/{entry['id']}/supprimer",
                       headers=headers)
    assert _landing(resp) == (f"/administration/{entry['id']}",
                              {"avertissement": "suppression"})
    assert db.peek(path) is not None
    assert LOCKED_SINCE in client.get(_url(resp)).get_data(as_text=True)


def test_an_entry_whose_delete_failed_says_it_is_still_there(
        db, client, entry, monkeypatch):
    path = f"admin_transactions/{entry['id']}"
    _commit_raises_on(monkeypatch, db, path)
    resp = client.post(f"/administration/{entry['id']}/supprimer",
                       headers=HX)
    assert _landing(resp) == (f"/administration/{entry['id']}",
                              {"avertissement": "suppression"})
    page = client.get(_url(resp)).get_data(as_text=True)
    assert NOT_DELETED_RETRY in page and _audit_rows(db) == []


@pytest.mark.parametrize("headers", [{}, HX], ids=["plain", "htmx"])
def test_an_unreadable_entry_is_refused_never_a_404(db, client, entry,
                                                    monkeypatch, caplog,
                                                    headers):
    """Fails on the old code: the fail-open read answered the 404 page.
    Fails on round 1 too: the strict read's failure left no trace in the
    logs (the fail-open read's ERROR went with it) — now one line, the ids
    only."""
    path = f"admin_transactions/{entry['id']}"
    outage = _fail_reads(monkeypatch, db, path)
    with caplog.at_level(logging.ERROR, logger="pallas.unexpected"):
        resp = client.post(f"/administration/{entry['id']}/supprimer",
                           headers=headers)
    assert _landing(resp) == (f"/administration/{entry['id']}",
                              {"avertissement": "suppression"})
    assert _logged(caplog, "admin entry delete: read failed") == [
        ({"transaction_id": entry["id"], "error_type": "ServiceUnavailable"},
         False)]
    outage["on"] = False
    assert db.peek(path) is not None
    assert NOT_DELETED_RETRY in client.get(_url(resp)).get_data(as_text=True)


def test_an_entry_already_gone_goes_to_the_journal_quietly(db, client):
    """Fails on the old code: the 404 page."""
    for headers in ({}, HX):
        resp = client.post(f"/administration/{_GONE}/supprimer",
                           headers=headers)
        assert _landing(resp) == ("/administration/", {})


@pytest.mark.parametrize("headers", [{}, HX], ids=["plain", "htmx"])
@pytest.mark.parametrize("register", ["administration", "fideicommis"])
def test_a_refused_abandon_the_worksheet_cannot_show_is_said_on_the_list(
        db, client, monkeypatch, register, headers):
    """Fails on the old code: the list, without a word, as if abandoned.
    Fails on round 1 under htmx: both landings answered a bare 302, which
    the XHR follows and swaps whole — redirect_after_post now."""
    model = al if register == "administration" else trust_model
    monkeypatch.setattr(model, "delete_reconciliation",
                        lambda rec_id: (False, [DELETE_FAILED]))
    monkeypatch.setattr(model, "get_reconciliation", lambda rec_id: None)
    resp = client.post(f"/{register}/conciliations/r1/abandonner",
                       headers=headers)
    assert _landing(resp) == (f"/{register}/conciliations/",
                              {"avertissement": "abandon"})
    assert ABANDON_UNCONFIRMED in client.get(_url(resp)).get_data(
        as_text=True)
    # Gone (the model says so): the list, quietly.
    monkeypatch.setattr(model, "delete_reconciliation", lambda rec_id: (
        False, [model.RECONCILIATION_NOT_FOUND]))
    resp = client.post(f"/{register}/conciliations/r1/abandonner",
                       headers=headers)
    assert _landing(resp) == (f"/{register}/conciliations/", {})
    # Abandoned: the list too.
    monkeypatch.setattr(model, "delete_reconciliation",
                        lambda rec_id: (True, []))
    resp = client.post(f"/{register}/conciliations/r1/abandonner",
                       headers=headers)
    assert _landing(resp) == (f"/{register}/conciliations/", {})


# ── Documents ────────────────────────────────────────────────────────


def _seed_document(db, did: str, doc_id: str = "doc1") -> str:
    db.seed(f"documents/{doc_id}", {
        **document_model._default_doc(), "id": doc_id, "dossier_id": did,
        "folder_id": None, "display_name": "Lettre", "filename": "lettre.pdf",
        "file_type": "application/pdf", "category": "correspondance",
        "created_at": DT, "updated_at": DT, "etag": "e0"})
    return doc_id


def test_a_refused_move_under_htmx_lands_on_the_document(db, client, did):
    """Fails on the old code: a 422 fragment htmx never swaps."""
    _seed_document(db, did)
    resp = client.post("/documents/doc1/move",
                       data={"target_folder_id": "absent"}, headers=HX)
    assert _landing(resp) == ("/documents/doc1", {
        "erreur": "Le dossier de destination est introuvable."})


def test_moving_a_document_already_gone_says_so_on_the_browser(db, client):
    """Fails on the old code: a 404 fragment under htmx, the browser
    without a word for the plain form. Unlike a deletion, the move did not
    happen."""
    for headers in ({}, HX):
        resp = client.post("/documents/absent/move",
                           data={"target_folder_id": ""}, headers=headers)
        assert _landing(resp) == ("/documents/",
                                  {"erreur": "Document introuvable."})


@pytest.mark.parametrize("headers", [{}, HX], ids=["plain", "htmx"])
def test_a_move_whose_document_cannot_be_read_says_so(db, client, did,
                                                      monkeypatch, caplog,
                                                      headers):
    """Fails on the old code: the fail-open read sent the lawyer to the
    browser without a word. Fails on round 1 too: the strict read's
    failure left no trace in the logs (the fail-open read's WARNING went
    with it) — now one line, the ids only."""
    _seed_document(db, did)
    outage = _fail_reads(monkeypatch, db, "documents/doc1")
    with caplog.at_level(logging.ERROR, logger="pallas.unexpected"):
        resp = client.post("/documents/doc1/move",
                           data={"target_folder_id": ""}, headers=headers)
    assert _landing(resp) == ("/documents/doc1", {"erreur": READ_UNAVAILABLE})
    assert _logged(caplog, "document move: read failed") == [
        ({"document_id": "doc1", "error_type": "ServiceUnavailable"}, False)]
    outage["on"] = False
    assert _shown(client.get(_url(resp)).get_data(as_text=True),
                  READ_UNAVAILABLE)


def test_a_refused_bulk_move_says_why_on_the_browser(db, client, did):
    """Fails on the old code: the plain form dropped the refusal, htmx got
    a 422. The answer is the browser's: a 302 even under htmx, which its
    XHR follows to the browser fragment (follow=True). Fails on round 1:
    a move that moved nothing opened the TARGET folder (here one that does
    not exist), where the documents are not — now the dossier's root."""
    _seed_document(db, did)
    form = {"dossier_id": did, "document_ids": ["doc1"],
            "target_folder_id": "absent"}
    for headers in ({}, HX):
        resp = client.post("/documents/move-bulk", data=form, headers=headers)
        assert resp.status_code == 302
        path, query = _landing(resp)
        assert path == "/documents/" and query["erreur"]
        assert query["dossier_id"] == did and "folder_id" not in query
    missing = client.post("/documents/move-bulk", data={"dossier_id": did})
    assert _landing(missing)[1]["erreur"] == "Paramètres manquants."


@pytest.mark.parametrize("headers", [{}, HX], ids=["plain", "htmx"])
def test_a_refused_folder_move_lands_at_its_current_level(db, client, did,
                                                          headers):
    """Fails on the old code: the dossier's root, without a word."""
    for fid, name, parent in (("fP", "Parent", None), ("fC", "Enfant", "fP")):
        db.seed(f"folders/{fid}", {
            "id": fid, "dossier_id": did, "name": name,
            "parent_folder_id": parent, "order": 0, "etag": f"e-{fid}",
            "created_at": DT, "updated_at": DT})
    resp = client.post("/documents/folders/fC/move", data={
        "dossier_id": did, "new_parent_folder_id": "fC"}, headers=headers)
    path, query = _landing(resp)
    assert (path, query["folder_id"]) == ("/documents/", "fP")
    assert query["erreur"] and db.peek("folders/fC")["parent_folder_id"] == "fP"


@pytest.mark.parametrize("url", [
    "/documents/folders/create", "/documents/folders/f1/rename",
    "/documents/folders/f1/move", "/documents/folders/f1/delete"])
def test_a_folder_form_without_a_dossier_says_so_in_the_browser(client, url):
    """Fails on the old code: a 422 fragment under htmx, a silent redirect
    for the plain form. Followed like the XHR follows it: the browser
    fragment, its banner."""
    resp = client.post(url, data={"name": "X", "new_name": "Y"}, headers=HX)
    assert resp.status_code == 302 and "HX-Redirect" not in resp.headers
    fragment = client.get(resp.headers["Location"], headers=HX)
    assert _shown(fragment.get_data(as_text=True),
                  "Dossier juridique requis.")


def test_the_in_place_swaps_keep_their_followed_302(db, client, did):
    """Must NOT change: the task checkbox swaps the list IN PLACE — the
    XHR follows the 302 with its HX-Request header and gets the fragment
    its hx-target expects; an HX-Redirect would reload the whole page. The
    folder forms answer the same way for an htmx caller (rendered inside
    Alpine ``x-if`` templates, which htmx never processes, they post
    without htmx today). The swap predates this lot; the HX-Redirect those
    302 carried — dead, the XHR never let htmx read it — is gone."""
    first = client.post("/documents/folders/create",
                        data={"dossier_id": did, "name": "a/b"}, headers=HX)
    assert first.status_code == 302 and "HX-Redirect" not in first.headers
    erreur = _landing(first)[1]["erreur"]
    swapped = client.get(first.headers["Location"], headers=HX)
    html = swapped.get_data(as_text=True)
    assert _shown(html, erreur) and "<html" not in html
    tid = _new_task(did, status="annulée")
    resp = client.post(f"/taches/{tid}/toggle", data={"target": "terminée"},
                       headers=HX, follow_redirects=True)
    assert resp.status_code == 200 and "HX-Redirect" not in resp.headers
    html = resp.get_data(as_text=True)
    assert _shown(html, task_model.CANCELLED_MEANWHILE) and "<html" not in html


# ── Time entries and expenses ────────────────────────────────────────


@pytest.mark.parametrize("kind", ["time", "expense"])
def test_a_refused_entry_delete_under_htmx_is_said_on_its_form(
        db, client, did, kind):
    """Fails on the old code: a 422 fragment htmx never swaps."""
    if kind == "time":
        doc, errors = time_entry_model.create_time_entry({
            "dossier_id": did, "date": DT, "description": "Premier",
            "hours": 1.5, "rate": 30000, "billable": True})
        path, url = f"timeentries/{doc['id']}", f"/temps/{doc['id']}/delete"
        sentence = "Impossible de supprimer une entrée déjà facturée."
    else:
        doc, errors = expense_model.create_expense({
            "dossier_id": did, "date": DT, "description": "Premier",
            "amount": 5000, "category": "timbre_judiciaire",
            "taxable": False})
        path = f"expenses/{doc['id']}"
        url = f"/temps/depenses/{doc['id']}/delete"
        sentence = "Impossible de supprimer une dépense déjà facturée."
    assert errors == [], errors
    db.external_write(path, {**db.peek(path), "invoiced": True,
                             "invoice_id": "inv1"})
    resp = client.post(url, headers=HX)
    assert resp.status_code == 200
    assert _shown(resp.get_data(as_text=True), sentence)
    assert db.peek(path) is not None


@pytest.mark.parametrize("headers", [{}, HX], ids=["plain", "htmx"])
@pytest.mark.parametrize("kind", ["time", "expense"])
def test_a_refused_entry_delete_no_read_can_show_is_said_on_the_list(
        db, client, monkeypatch, kind, headers):
    """Fails on the old code: it fell through to the redirect of a
    deletion, without a word."""
    if kind == "time":
        monkeypatch.setattr(time_expenses_routes, "delete_time_entry",
                            lambda i: (False, DELETE_FAILED))
        monkeypatch.setattr(time_expenses_routes, "get_time_entry",
                            lambda i: None)
        url, query = "/temps/x1/delete", {"erreur": DELETE_FAILED}
    else:
        monkeypatch.setattr(time_expenses_routes, "delete_expense",
                            lambda i: (False, DELETE_FAILED))
        monkeypatch.setattr(time_expenses_routes, "get_expense",
                            lambda i: None)
        url = "/temps/depenses/x1/delete"
        query = {"tab": "depenses", "erreur": DELETE_FAILED}
    resp = client.post(url, data={"return_to": "/dossiers/d1?tab=temps"},
                       headers=headers)
    assert _landing(resp) == ("/temps/", query)


def test_the_time_list_prints_the_banner(db, client):
    page = client.get(f"/temps/?erreur={quote(DELETE_FAILED)}")
    assert _shown(page.get_data(as_text=True), DELETE_FAILED)


# ── Contacts ─────────────────────────────────────────────────────────


def _presumed_contact(db, pid: str = "p9") -> str:
    """A contact whose identity check Claude inscribed: « Confirmer » is
    offered."""
    db.seed(f"parties/{pid}", {
        **partie_model._default_doc(), "id": pid, "type": "individual",
        "contact_role": "client", "first_name": "Jean",
        "last_name": "Tremblay", "identity_verified": "vérifié",
        "identity_verified_date": DT, "identity_verified_source": "mcp",
        "created_at": DT, "updated_at": DT, "etag": f"e-{pid}"})
    return pid


@pytest.mark.parametrize("headers", [{}, HX], ids=["plain", "htmx"])
def test_a_kyc_confirmation_whose_read_failed_says_so_on_the_fiche(
        db, client, monkeypatch, caplog, headers):
    """Fails on round 1: the model's fail-open read answered a blip with
    « Contact introuvable. », which the route takes for a contact already
    gone — the contact list, quietly: the lawyer's « Confirmer » dropped
    without a word, the contact still there. Read strictly, the fiche says
    the read failed, and nothing is confirmed."""
    pid = _presumed_contact(db)
    outage = _fail_reads(monkeypatch, db, f"parties/{pid}")
    with caplog.at_level(logging.ERROR, logger="pallas.unexpected"):
        resp = client.post(f"/parties/{pid}/conformite/identite/confirmer",
                           data={"expected_etag": f"e-{pid}"},
                           headers=headers)
    assert _landing(resp) == (f"/parties/{pid}", {"erreur": READ_UNAVAILABLE})
    assert _logged(caplog, "partie KYC confirmation: read failed") == [
        ({"partie_id": pid, "error_type": "ServiceUnavailable"}, False)]
    outage["on"] = False
    stored = db.peek(f"parties/{pid}")
    assert not stored.get("identity_verified_confirmed_at")
    assert stored["etag"] == f"e-{pid}"
    assert _shown(client.get(_url(resp)).get_data(as_text=True),
                  READ_UNAVAILABLE)


def test_confirm_kyc_status_never_takes_a_failed_read_for_an_absence(
        db, monkeypatch):
    """Fails on round 1 (the model behind the route above). And an id that
    can name no contact (``document.is_addressable_id``) is an absence —
    nothing read —, never « réessayez »."""
    pid = _presumed_contact(db)
    outage = _fail_reads(monkeypatch, db, f"parties/{pid}")
    assert partie_model.confirm_kyc_status(
        pid, "identity_verified", par="juriste",
        expected_etag=f"e-{pid}") == (None, [READ_UNAVAILABLE])
    outage["on"] = False
    db.reset_logs()
    for bad in ("__x__", "a/b"):
        assert partie_model.confirm_kyc_status(
            bad, "identity_verified", par="juriste", expected_etag="e") == (
                None, [partie_model.PARTIE_NOT_FOUND])
    assert db.reads == [] and db.commits == []


# ── Every success block: htmx gets the plain form's own target ───────


class _Save(NamedTuple):
    """One success block of the lot: how to POST it on a fresh record."""
    collection: Optional[str]          # where a real create lands
    url: Callable[[str], str]          # record id -> the POST url
    form: Callable[[str, str], dict]   # (dossier id, record id) -> payload
    fresh: Callable = lambda db, did: ""          # the record saved over
    stub: Callable = lambda monkeypatch, did: None  # writer stubbed, if any
    upload: bool = False               # multipart, with a .docx


_SEQ = iter(range(1, 10_000))


def _hearing_form(did: str, rid: str = "", **over) -> dict:
    return {"dossier_id": did, "title": "Audience", "hearing_type": "audience",
            "status": "confirmée", "start_date": "2026-10-15",
            "start_time": "09:00", "end_time": "10:00",
            "reminder_minutes": "1440", "modalite": "présentiel", **over}


def _task_form(did: str, rid: str = "") -> dict:
    return {"dossier_id": did, "title": "Préparer", "priority": "normale",
            "status": "à_faire", "category": "autre"}


def _new_time_entry(db, did: str) -> str:
    doc, errors = time_entry_model.create_time_entry({
        "dossier_id": did, "date": DT, "description": "Premier",
        "hours": 1.5, "rate": 30000, "billable": True})
    assert errors == [], errors
    return doc["id"]


def _new_expense(db, did: str) -> str:
    doc, errors = expense_model.create_expense({
        "dossier_id": did, "date": DT, "description": "Premier",
        "amount": 5000, "category": "timbre_judiciaire", "taxable": False})
    assert errors == [], errors
    return doc["id"]


def _new_contact(db, did: str) -> str:
    pid = f"pc{next(_SEQ)}"
    db.seed(f"parties/{pid}", {
        **partie_model._default_doc(), "id": pid, "type": "individual",
        "contact_role": "client", "first_name": "Jean",
        "last_name": "Tremblay", "created_at": DT, "updated_at": DT,
        "etag": f"e-{pid}"})
    return pid


def _new_template(db, did: str) -> str:
    tid = f"tpl{next(_SEQ)}"
    db.seed(f"doc_templates/{tid}", {
        "id": tid, "name": "Lettre", "description": "Premier",
        "category": "correspondance", "kind": "gabarit",
        "filename": "lettre.docx", "original_filename": "lettre.docx",
        "file_size": 10, "version": 1, "placeholders": [],
        "storage_path": f"users/u1/templates/{tid}/v1/lettre.docx",
        "created_at": DT, "updated_at": DT, "etag": f"e-{tid}"})
    return tid


def _new_dossier(db, did: str) -> str:
    """An empty dossier of its own (no child: its deletion is allowed)."""
    dossier, errors = dossier_model.create_dossier({
        "file_number": f"2026-{next(_SEQ) + 100:03d}", "title": "Roy c. Roy",
        "clients": [{"id": "p1", "name": "Jean Tremblay",
                     "roles": ["demandeur"]}]})
    assert errors == [], errors
    return dossier["id"]


def _fresh_protocol(db, did: str) -> str:
    """A protocol on a dossier of its own: a dossier has one « actif »."""
    return _new_protocol(_new_dossier(db, did))


def _stub_protocol_create(monkeypatch, did):
    monkeypatch.setattr(
        protocols_routes.protocol_service, "create_protocol",
        lambda *a, **k: ({"id": "p-new"}, [], {
            "tasks_failed": 0, "tasks_linked": 0, "tasks_created": 0}))


def _stub_protocol_update(monkeypatch, did):
    monkeypatch.setattr(
        protocols_routes.protocol_service, "update_protocol",
        lambda protocol_id, *a, **k: ({"id": protocol_id}, [], {}))


def _stub_dossier_update(monkeypatch, did):
    monkeypatch.setattr(
        dossiers_routes, "update_dossier",
        lambda dossier_id, data, expected_etag=None: (
            {"id": dossier_id, "status": "actif"}, []))


def _stub_invoice_create(monkeypatch, did):
    monkeypatch.setattr(invoices_routes, "create_invoice",
                        lambda *a, **k: ({"id": "inv-new"}, []))


def _stub_template_create(monkeypatch, did):
    monkeypatch.setattr(doc_templates_routes, "create_template",
                        lambda **k: ({"id": "tpl-new"}, []))


def _freeze_before_the_series(monkeypatch, did):
    monkeypatch.setattr(hearings_routes, "today_mtl",
                        lambda: date(2026, 10, 1))


# Each success block this lot moved to the helper (its model writer real,
# or stubbed where the form is long — the ROUTE's answer is the point).
_SUCCESS_BLOCKS = {
    "tasks.task_create": _Save("tasks", lambda r: "/taches/", _task_form),
    "tasks.task_update": _Save(
        "tasks", lambda r: f"/taches/{r}", _task_form,
        fresh=lambda db, did: _new_task(did)),
    "notes.note_create": _Save(
        "notes", lambda r: "/notes/", lambda did, r: {
            "dossier_id": did, "title": "Nouvelle", "content": "x",
            "category": "recherche"}),
    "notes.note_update": _Save(
        "notes", lambda r: f"/notes/{r}", lambda did, r: {
            "dossier_id": did, "title": "Modifiée", "content": "y",
            "category": "recherche"},
        fresh=lambda db, did: _new_note(did)),
    "hearings.hearing_create": _Save(
        "hearings", lambda r: "/audiences/", _hearing_form),
    "hearings.hearing_create#série": _Save(
        "hearings", lambda r: "/audiences/", lambda did, r: _hearing_form(
            did, frequence="hebdomadaire", fin_mode="count", fin_count="2"),
        stub=_freeze_before_the_series),
    "hearings.hearing_update": _Save(
        "hearings", lambda r: f"/audiences/{r}", _hearing_form,
        fresh=lambda db, did: _new_hearing(did)),
    "time_expenses.time_entry_create": _Save(
        "timeentries", lambda r: "/temps/", lambda did, r: {
            "dossier_id": did, "date": "2026-03-04", "description": "Nouvelle",
            "hours": "1.5", "rate": "300", "billable": "on"}),
    "time_expenses.time_entry_update": _Save(
        "timeentries", lambda r: f"/temps/{r}", lambda did, r: {
            "dossier_id": did, "date": "2026-03-04", "description": "Modifiée",
            "hours": "2", "rate": "300", "billable": "on"},
        fresh=_new_time_entry),
    "time_expenses.time_entry_phase_update": _Save(
        "timeentries", lambda r: f"/temps/{r}/phase",
        lambda did, r: {"phase": "CTS", "sous_phase": "CTS-02"},
        fresh=_new_time_entry),
    "time_expenses.expense_create": _Save(
        "expenses", lambda r: "/temps/depenses", lambda did, r: {
            "dossier_id": did, "date": "2026-03-04", "description": "Nouvelle",
            "category": "timbre_judiciaire", "amount": "50"}),
    "time_expenses.expense_update": _Save(
        "expenses", lambda r: f"/temps/depenses/{r}", lambda did, r: {
            "dossier_id": did, "date": "2026-03-04", "description": "Modifiée",
            "category": "timbre_judiciaire", "amount": "60"},
        fresh=_new_expense),
    "time_expenses.expense_phase_update": _Save(
        "expenses", lambda r: f"/temps/depenses/{r}/phase",
        lambda did, r: {"phase": "CTS", "sous_phase": "CTS-02"},
        fresh=_new_expense),
    "documents.document_update": _Save(
        "documents", lambda r: f"/documents/{r}/edit", lambda did, r: {
            "display_name": "Lettre", "category": "correspondance",
            "tags": "", "notes_internes": "Modifiées", "document_date": "",
            "analyse_presente": "1"},
        fresh=lambda db, did: _seed_document(db, did, f"doc{next(_SEQ)}")),
    "dossiers.dossier_update": _Save(
        None, lambda r: f"/dossiers/{r}",
        lambda did, r: {"title": "Modifié", "status": "actif"},
        fresh=lambda db, did: did, stub=_stub_dossier_update),
    "parties.partie_create": _Save(
        "parties", lambda r: "/parties/", lambda did, r: {
            "type": "individual", "contact_role": "client",
            "first_name": "Marie", "last_name": f"Roy {next(_SEQ)}"}),
    "parties.partie_update": _Save(
        "parties", lambda r: f"/parties/{r}", lambda did, r: {
            "type": "individual", "contact_role": "client",
            "first_name": "Jean", "last_name": "Tremblay",
            "expected_etag": f"e-{r}"},
        fresh=_new_contact),
    "protocols.protocol_create": _Save(
        None, lambda r: "/protocoles/", lambda did, r: {
            "dossier_id": did, "protocol_type": "conventionnel",
            "start_date": "2026-03-04"},
        stub=_stub_protocol_create),
    "protocols.protocol_update": _Save(
        None, lambda r: f"/protocoles/{r}", lambda did, r: {
            "title": "Protocole", "notes": "", "start_date": "2026-03-04"},
        fresh=_fresh_protocol, stub=_stub_protocol_update),
    "invoices.invoice_create": _Save(
        None, lambda r: "/factures/", lambda did, r: {
            "dossier_id": did, "invoice_date": "2026-03-04",
            "selected_entries": ["x"]},
        stub=_stub_invoice_create),
    "doc_templates.template_create": _Save(
        None, lambda r: "/gabarits/", lambda did, r: {
            "name": "Lettre", "description": "", "category": "correspondance",
            "kind": "gabarit"},
        stub=_stub_template_create, upload=True),
    "doc_templates.template_update": _Save(
        "doc_templates", lambda r: f"/gabarits/{r}", lambda did, r: {
            "name": "Lettre", "description": "Second",
            "category": "correspondance", "kind": "gabarit",
            "expected_etag": f"e-{r}"},
        fresh=_new_template),
}


def _post_save(db, client, did, save: _Save, headers: dict):
    """POST the save on a fresh record: (response, the ids it was about —
    the record saved, the one created, a series it opened)."""
    record_id = save.fresh(db, did)
    before = set(db.peek_collection(save.collection)) if save.collection \
        else set()
    data = save.form(did, record_id)
    if save.upload:
        data = {**data, "file": (io.BytesIO(b"PK\x03\x04"), "lettre.docx")}
    resp = client.post(save.url(record_id), data=data, headers=headers,
                       content_type=("multipart/form-data" if save.upload
                                     else None))
    own = {record_id} - {""}
    if save.collection:
        for rid in set(db.peek_collection(save.collection)) - before:
            own.add(rid)
            serie = (db.peek(f"{save.collection}/{rid}") or {}).get("serie_id")
            if serie:
                own.add(serie)
    return resp, own


def _masked(target: str, own: set) -> tuple:
    """A landing with the ids of the records its POST was about masked
    (each mode saves its own fresh record)."""
    def mask(text: str) -> str:
        for rid in sorted(own, key=len, reverse=True):
            text = text.replace(rid, "<own>")
        return text

    parts = urlsplit(target)
    return mask(parts.path), {
        k: [mask(v) for v in vs]
        for k, vs in parse_qs(parts.query, keep_blank_values=True).items()}


@pytest.mark.parametrize("block", list(_SUCCESS_BLOCKS),
                         ids=list(_SUCCESS_BLOCKS))
def test_a_success_block_answers_htmx_with_the_plain_forms_target(
        db, client, did, monkeypatch, block):
    """Fails on a success block switched back to a bare ``redirect()``
    (htmx then gets a 302 its XHR follows, and swaps the whole page into
    the form) or to ``follow=True``: htmx gets the helper's 200 carrying
    ``HX-Redirect`` — the very target the plain form's 302 names — no
    ``Location``, no body. Round 2: lot B's htmx table read the target
    alone, for 13 of these blocks."""
    save = _SUCCESS_BLOCKS[block]
    save.stub(monkeypatch, did)
    plain, plain_own = _post_save(db, client, did, save, {})
    assert plain.status_code == 302, plain.get_data(as_text=True)[:500]
    assert "HX-Redirect" not in plain.headers
    hx, hx_own = _post_save(db, client, did, save, HX)
    assert hx.status_code == 200, (hx.status_code, hx.headers)
    assert "Location" not in hx.headers and hx.get_data() == b""
    assert (_masked(hx.headers["HX-Redirect"], hx_own)
            == _masked(plain.headers["Location"], plain_own))


def test_the_success_table_names_post_views_that_answer_through_the_helper():
    """The tables are not stale: each of their views is a POST view of
    routes/ whose source answers through ``redirect_after_post`` — itself,
    or through a module helper that does."""
    posts = _post_views_by_ast()
    answering = {}
    for path in sorted((_ATHENA / "routes").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bp = next(iter(_blueprints_of(tree).values()), None)
        functions = [fn for fn in tree.body
                     if isinstance(fn, ast.FunctionDef)]
        helpers = {fn.name for fn in functions if any(
            isinstance(n, ast.Call) and _call_name(n) == "redirect_after_post"
            for n in ast.walk(fn))}
        for fn in functions:
            if bp:
                answering[f"{bp}.{fn.name}"] = any(
                    isinstance(n, ast.Call)
                    and _call_name(n) in helpers | {"redirect_after_post"}
                    for n in ast.walk(fn))
    for block in [*_SUCCESS_BLOCKS, *_OTHER_LANDINGS]:
        endpoint = block.split("#")[0]
        assert endpoint in posts, endpoint
        assert answering[endpoint], endpoint


def _new_admin_entry(db, did: str) -> str:
    if db.peek("admin_accounts/ops1") is None:
        db.seed("admin_accounts/ops1", {
            "id": "ops1", "name": "Opérations", "status": "actif",
            "account_type": "opérations", "ledger_balance": 0, "etag": "e0"})
    doc, errors = al.create_transaction({
        "account_id": "ops1", "kind": "dépense", "amount": 10000,
        "method": "virement", "counterparty": "Fournisseur fictif",
        "date": DT, "description": "", "reference": "",
        "supplier_invoice_ref": "", "category": "loyer"})
    assert errors == [], errors
    return doc["id"]


def _fresh_protocol_with_step(db, did: str) -> str:
    pid = _fresh_protocol(db, did)
    _new_step(db, pid, mandatory=False)
    return pid


def _linked_contact(db, did: str) -> str:
    """The dossier's own client, as a contact: its deletion is refused."""
    db.seed("parties/p1", {
        **partie_model._default_doc(), "id": "p1", "type": "individual",
        "contact_role": "client", "first_name": "Jean",
        "last_name": "Tremblay", "created_at": DT, "updated_at": DT,
        "etag": "e-p1"})
    return "p1"


def _stub(module, name: str, value) -> Callable:
    return lambda monkeypatch, did: monkeypatch.setattr(module, name, value)


def _stub_dossier_create(monkeypatch, did):
    monkeypatch.setattr(dossiers_routes, "create_dossier",
                        lambda data: ({"id": "d-new", **data}, []))
    monkeypatch.setattr(dossiers_routes.folder_model, "ensure_default_tree",
                        lambda dossier_id: (SimpleNamespace(blocked=False),
                                            []))


def _commit_raised(re_read) -> Callable:
    """The dossier delete whose commit RAISED: the route settles it with
    one more strict read — *re_read*."""
    def stub(monkeypatch, did):
        monkeypatch.setattr(dossiers_routes, "delete_dossier", lambda i: (
            False, "", {"code": dossier_model.DELETE_FAILED,
                        "dossier": None}))
        monkeypatch.setattr(dossiers_routes, "get_dossier_strict", re_read)
    return stub


def _unreadable(*_args):
    raise gexc.ServiceUnavailable("injected read failure")


# The lot's other answers a test here posted only as a plain form — each
# switched back to a bare ``redirect()`` survived this module until round
# 2 (the sweep of every ``redirect_after_post`` call, mutated one at a
# time): the same pin, refusals and successes alike.
_OTHER_LANDINGS = {
    "admin_ledger.entry_delete": _Save(
        None, lambda r: f"/administration/{r}/supprimer",
        lambda did, r: {}, fresh=_new_admin_entry),
    "protocols.protocol_delete": _Save(
        "protocols", lambda r: f"/protocoles/{r}/delete", lambda did, r: {},
        fresh=lambda db, did: _new_protocol(did)),
    "protocols.step_add": _Save(
        None, lambda r: f"/protocoles/{r}/steps",
        lambda did, r: {"title": "Étape", "deadline_date": "2099-12-01"},
        fresh=_fresh_protocol,
        stub=_stub(protocols_routes.protocol_service, "add_step",
                   lambda *a, **k: ({"id": "s9"}, [], {}))),
    "protocols.step_update": _Save(
        None, lambda r: f"/protocoles/{r}/steps/s1",
        lambda did, r: {"deadline_date": "2099-12-02", "notes": "x"},
        fresh=_fresh_protocol_with_step,
        stub=_stub(protocols_routes.protocol_service, "update_step",
                   lambda *a, **k: ({"id": "s1"}, [], {}))),
    "protocols.step_complete": _Save(
        None, lambda r: f"/protocoles/{r}/steps/s1/complete",
        lambda did, r: {"status": "complétée"},
        fresh=_fresh_protocol_with_step,
        stub=_stub(protocols_routes.protocol_service, "set_step_status",
                   lambda *a, **k: (None, ["Étape refusée."], None))),
    "protocols.step_delete#protocole-introuvable": _Save(
        None, lambda r: f"/protocoles/{_GONE}/steps/s1/delete",
        lambda did, r: {}),
    "dossiers.dossier_create": _Save(
        None, lambda r: "/dossiers/", lambda did, r: {
            "title": "Nouveau", "file_number": "2026-777",
            "status": "actif"},
        stub=_stub_dossier_create),
    "dossiers.dossier_delete": _Save(
        None, lambda r: f"/dossiers/{r}/delete", lambda did, r: {},
        fresh=_new_dossier),
    "dossiers.dossier_delete#id-vide": _Save(
        None, lambda r: "/dossiers/%20/delete", lambda did, r: {}),
    "dossiers.dossier_delete#introuvable": _Save(
        None, lambda r: f"/dossiers/{_GONE}/delete", lambda did, r: {}),
    "dossiers.dossier_delete#refus": _Save(
        None, lambda r: f"/dossiers/{r}/delete", lambda did, r: {},
        fresh=lambda db, did: (_new_task(did), did)[1]),
    "dossiers.dossier_delete#incertaine": _Save(
        None, lambda r: f"/dossiers/{r}/delete", lambda did, r: {},
        fresh=lambda db, did: did, stub=_commit_raised(_unreadable)),
    "dossiers.dossier_delete#supprime-ailleurs": _Save(
        None, lambda r: f"/dossiers/{r}/delete", lambda did, r: {},
        fresh=lambda db, did: did, stub=_commit_raised(lambda i: None)),
    "parties.kyc_confirm": _Save(
        None, lambda r: f"/parties/{r}/conformite/identite/confirmer",
        lambda did, r: {"expected_etag": f"e-{r}"},
        fresh=lambda db, did: _presumed_contact(db, f"pk{next(_SEQ)}")),
    "parties.kyc_confirm#périmée": _Save(
        None, lambda r: f"/parties/{r}/conformite/identite/confirmer",
        lambda did, r: {"expected_etag": "e-autre"},
        fresh=lambda db, did: _presumed_contact(db, f"pk{next(_SEQ)}")),
    "parties.kyc_confirm#introuvable": _Save(
        None, lambda r: f"/parties/{_GONE}/conformite/identite/confirmer",
        lambda did, r: {"expected_etag": "e"}),
    "parties.partie_delete": _Save(
        "parties", lambda r: f"/parties/{r}/delete", lambda did, r: {},
        fresh=_new_contact),
    "parties.partie_delete#refus": _Save(
        None, lambda r: f"/parties/{r}/delete", lambda did, r: {},
        fresh=_linked_contact),
    # _back_to_proofs: the contact's proofs of identity (its own suite,
    # tests/test_pieces_identite_routes.py, pins the rest).
    "parties.id_proof_rename": _Save(
        None, lambda r: f"/parties/{_GONE}/pieces-identite/pr1/renommer",
        lambda did, r: {"display_name": "Passeport", "expected_etag": "e"}),
    "trust.entry_clear": _Save(
        None, lambda r: "/fideicommis/t1/compenser",
        lambda did, r: {"cleared_date": "2026-03-04"},
        stub=_stub(trust_routes.comptabilite, "compenser_fideicommis",
                   lambda ids, cleared: {"errors": ["Refus."]})),
    "trust.entry_clear_bulk": _Save(
        None, lambda r: "/fideicommis/compenser-lot",
        lambda did, r: {"tx_ids": ["t1"], "cleared_date": "2026-03-04"},
        stub=_stub(trust_routes.comptabilite, "compenser_fideicommis",
                   lambda ids, cleared: {"errors": ["Refus."]})),
    "doc_templates.template_delete": _Save(
        None, lambda r: f"/gabarits/{r}/delete", lambda did, r: {},
        fresh=_new_template,
        stub=_stub(doc_templates_routes, "delete_template",
                   lambda template_id: (True, None))),
    "doc_templates.template_delete#refus": _Save(
        None, lambda r: f"/gabarits/{r}/delete", lambda did, r: {},
        fresh=_new_template,
        stub=_stub(doc_templates_routes, "delete_template",
                   lambda template_id: (False, "Gabarit utilisé."))),
}


@pytest.mark.parametrize("landing", list(_OTHER_LANDINGS),
                         ids=list(_OTHER_LANDINGS))
def test_the_lots_other_landings_answer_htmx_with_the_plain_forms_target(
        db, client, did, monkeypatch, landing):
    """Fails on any of these answers switched back to a bare
    ``redirect()``: the same 200 + ``HX-Redirect`` — the plain form's very
    target — no ``Location``, no body."""
    save = _OTHER_LANDINGS[landing]
    save.stub(monkeypatch, did)
    plain, plain_own = _post_save(db, client, did, save, {})
    assert plain.status_code == 302, plain.get_data(as_text=True)[:500]
    assert "HX-Redirect" not in plain.headers
    hx, hx_own = _post_save(db, client, did, save, HX)
    assert hx.status_code == 200, (hx.status_code, hx.headers)
    assert "Location" not in hx.headers and hx.get_data() == b""
    assert (_masked(hx.headers["HX-Redirect"], hx_own)
            == _masked(plain.headers["Location"], plain_own))


# ── Invoices ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("headers", [{}, HX], ids=["plain", "htmx"])
def test_a_refused_draft_save_the_form_cannot_show_lands_on_the_sheet(
        client, monkeypatch, headers):
    """Fails on the old code: the invoice list, without a word. Fails on
    round 1 too: the sheet, which cannot read this invoice either (it is
    not in the store), forwarded to the list WITHOUT the refusal, and the
    list printed none — the sentence is now forwarded and printed."""
    monkeypatch.setattr(invoices_routes, "update_invoice_draft",
                        lambda *a, **k: (None, [DELETE_FAILED], False))
    monkeypatch.setattr(invoices_routes, "get_invoice", lambda i: None)
    resp = client.post("/factures/inv1/brouillon", data={"notes": "x"},
                       headers=headers)
    assert _landing(resp) == ("/factures/inv1", {"erreur": DELETE_FAILED})
    forwarded = client.get(_url(resp))
    assert _landing(forwarded) == ("/factures/", {"erreur": DELETE_FAILED})
    page = client.get(_url(resp), follow_redirects=True)
    assert page.status_code == 200
    assert _shown(page.get_data(as_text=True), DELETE_FAILED)


def test_the_invoice_list_prints_a_forwarded_refusal_sanitized(client):
    hostile = "<b>Refus</b> & suite " + "é" * 600
    page = client.get(f"/factures/?erreur={quote(hostile)}").get_data(
        as_text=True)
    assert "<b>Refus</b>" not in page
    assert "Refus &amp; suite " + "é" * 100 in page
    assert "é" * 501 not in page


# ── A banner holds ANY request text at 375 px (2026-11-08) ───────────────
# A refusal sentence wraps on its spaces; a hand-made link whose `?erreur=`
# holds one long unbroken token scrolled the phone sideways, on every page
# below. Each banner that prints request text now carries `wrap-anywhere`
# (overflow-wrap: anywhere: the one value that also lowers the min-content
# width, so the token breaks inside a flex row too — `break-words` does
# not). Fails on the old code: no banner carried it. Compiled in the
# stylesheet: tests/test_compiled_classes.py; every template's banners,
# these and the pages this fixture cannot reach (the invoice sheet, a trust
# account, Réception): tests/test_banner_wrap.py.

_UNBROKEN = "Z" * 300


def _text_parent_tags(html: str, token: str) -> list:
    """The opening tag of the element whose TEXT holds each occurrence of
    *token* — an occurrence inside a tag (an attribute) is not text."""
    tags, start = [], 0
    while True:
        i = html.find(token, start)
        if i < 0:
            return tags
        start = i + len(token)
        lt, gt = html.rfind("<", 0, i), html.rfind(">", 0, i)
        if lt > gt:
            continue
        tags.append(html[lt:gt + 1])


def _banner_pages(db, did) -> list:
    t = quote(_UNBROKEN)
    both = f"erreur={t}&message={t}"
    task, hearing = _new_task(did), _new_hearing(did)
    protocol, document = _new_protocol(did), _seed_document(db, did)
    template, contact = _new_template(db, did), _presumed_contact(db)
    return [
        (f"/taches/?erreur={t}", 1),
        (f"/taches/{task}?erreur={t}", 1),
        (f"/temps/?erreur={t}", 1),
        (f"/factures/?erreur={t}", 1),
        (f"/documents/?{both}", 2),
        (f"/documents/{document}?erreur={t}", 1),
        (f"/audiences/?{both}", 2),
        (f"/audiences/{hearing}?{both}", 2),
        (f"/parties/{contact}?{both}", 2),
        (f"/parties/{contact}?gerer=1&{both}", 2),
        (f"/protocoles/{protocol}?{both}", 2),
        (f"/gabarits/{template}?{both}", 2),
        (f"/fideicommis/?erreur={t}", 1),
    ]


def test_every_banner_wraps_a_long_unbroken_token(db, client, did):
    for url, banners in _banner_pages(db, did):
        resp = client.get(url)
        assert resp.status_code == 200, (url, resp.status_code)
        tags = _text_parent_tags(resp.get_data(as_text=True), _UNBROKEN)
        assert len(tags) >= banners, (url, len(tags))
        for tag in tags:
            classes = tag.split('class="', 1)[1].split('"', 1)[0].split()
            assert "wrap-anywhere" in classes, (url, tag)
