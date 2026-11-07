"""The redirect and link guard: ONE door for every request-supplied target.

Request-supplied redirect and link targets (``return_to``, the login's
``next``) go through ``security.safe_internal_redirect`` where they are
read, never rendered or redirected raw (CLAUDE.md, Security Rules « Inputs
and errors »; Change Impact 4 and 5). This module owns every test of that
guard:

1. The door's decision table, row by row: an ABSENT target is the
   fallback and logs nothing; a control character left once stripped is
   refused; the refusals that predate it keep their reasons; an internal
   path is kept, stripped. The door's answer passes it again silently, so
   a value checked where it is read never logs twice.
2. Rule A, static: every read of ``return_to`` / ``next`` in the main
   service is the DIRECT first argument of the door, no whole request
   mapping is handed on unread (``**request.args``, ``ctx.update(…)``),
   the door is security.py's one function, imported from there by its
   callers, and no template or page script reads a target from the
   request — proved on planted cases, synthetic and planted into the real
   route modules.
3. The pages and the handlers, through the REAL routes, templates and
   models over ``tests/_fake_firestore.py``: a crafted « Retour » target is
   replaced SILENTLY by the page's ordinary link (one security log line, no
   banner); an internal one is kept; a refused save never reflects a posted
   one; a control character lands on the handler's own fallback instead of
   a 500 after the commit; one crafted value is ONE log line, never two.

The page table is DERIVED from the route sources (a GET view that reads
``return_to``), so a new page cannot escape it. The door's reasons are
derived from ``security.py``, so a new reason cannot escape the decision
table or the OBSERVABILITY.md row.
"""

import ast
import html as html_module
import json
import logging
import os
import pathlib
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Optional
from unittest import mock
from urllib.parse import parse_qs, quote, urlsplit

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync
    from models import budget as budget_model
    from models import dossier as dossier_model
    from models import expense as expense_model
    from models import hearing as hearing_model
    from models import invoice as invoice_model
    from models import note as note_model
    from models import task as task_model
    from models import time_entry as time_entry_model
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
    from security import limiter, safe_internal_redirect

from flask import Flask, url_for  # noqa: E402
from werkzeug.test import create_environ  # noqa: E402
from werkzeug.utils import redirect as werkzeug_redirect  # noqa: E402

# The edit-form bench: its cases know a valid and a refused payload for
# every edit form (precedent: tests/test_register_provenance_display.py).
from tests.test_edit_conflict_web import (  # noqa: E402
    CASES as _EDIT_CASES,
    _etags,
    _seed_document,
)
from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.markdown_docx import markdown_to_safe_html  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

# Loaded for its side effect: the fake store is installed on every LOADED
# module holding a `db`, the DAV sync layer included (a bump reaches it).
_ = (dav_sync,)

UTC = timezone.utc
DT = datetime(2026, 3, 4, tzinfo=UTC)
_DOOR = "safe_internal_redirect"
_EVENT = "redirect_rejected"


def _rejections(records) -> list[str]:
    """The reasons of the ``redirect_rejected`` lines, in order."""
    return [
        r.json_fields["reason"]
        for r in records
        if r.name == "pallas.security"
        and getattr(r, "json_fields", {}).get("event") == _EVENT
    ]


def _logged(caplog, call: Callable):
    """``call()``'s result and the rejection reasons it logged — only its."""
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="pallas.security"):
        result = call()
    return result, _rejections(caplog.records)


# ══════════════════════════════════════════════════════════════════════
# 1. The door's decision table
# ══════════════════════════════════════════════════════════════════════

_FALLBACK = "/repli"


@dataclass(frozen=True)
class Row:
    id: str
    target: object
    answer: str                 # what the door returns
    reasons: tuple = ()         # what it logs: nothing, or ONE reason


DECISIONS = [
    # An ABSENT target — what every form opened from its own list posts —
    # is no refusal: the fallback, and not a line.
    Row("absent-none", None, _FALLBACK),
    Row("absent-not-a-string", 42, _FALLBACK),
    Row("absent-empty", "", _FALLBACK),
    Row("absent-spaces", "   ", _FALLBACK),
    Row("absent-blank-controls", "\t\r\n", _FALLBACK),
    # A control character LEFT once stripped (C0, DEL, C1): refused. A CR or
    # LF used to reach the Location header, which Werkzeug refuses — a 500
    # after the write had committed.
    Row("control-lf", "/taches/\nx", _FALLBACK, ("control_character",)),
    Row("control-cr", "/taches/\rx", _FALLBACK, ("control_character",)),
    Row("control-tab", "/taches/\tx", _FALLBACK, ("control_character",)),
    Row("control-nul", "/taches/\x00x", _FALLBACK, ("control_character",)),
    Row("control-escape", "/taches/\x1bx", _FALLBACK, ("control_character",)),
    Row("control-unit-separator", "/taches/\x1fx", _FALLBACK,
        ("control_character",)),
    Row("control-del", "/taches/\x7fx", _FALLBACK, ("control_character",)),
    Row("control-c1-first", "/taches/\x80x", _FALLBACK,
        ("control_character",)),
    Row("control-c1-nel", "/taches/\x85x", _FALLBACK, ("control_character",)),
    Row("control-c1-last", "/taches/\x9fx", _FALLBACK,
        ("control_character",)),
    # The refusals that predate the control-character check, reasons and
    # order unchanged.
    Row("external-https", "https://evil.example/x", _FALLBACK,
        ("not_internal_path",)),
    Row("external-padded", "  https://evil.example/x", _FALLBACK,
        ("not_internal_path",)),
    Row("protocol-relative", "//evil.example/x", _FALLBACK,
        ("not_internal_path",)),
    Row("javascript", "javascript:alert(1)", _FALLBACK,
        ("not_internal_path",)),
    Row("data", "data:text/html,x", _FALLBACK, ("not_internal_path",)),
    Row("bare-host", "evil.example/x", _FALLBACK, ("not_internal_path",)),
    # The door judges the characters it is given, never a decoded or
    # normalized form: « %2F » and « ／ » (U+FF0F) are no slash, so a target
    # starting with them is no path (after a real leading « / », below, they
    # are path characters a browser keeps on the app's origin). strip()
    # takes Unicode spaces too (U+3000): what they hid is protocol-relative.
    Row("encoded-protocol-relative", "%2F%2Fevil.example", _FALLBACK,
        ("not_internal_path",)),
    Row("unicode-space-then-protocol-relative", "\u3000//evil.example",
        _FALLBACK, ("not_internal_path",)),
    Row("fullwidth-solidus-pair", "\uff0f\uff0fevil.example", _FALLBACK,
        ("not_internal_path",)),
    Row("backslash", "/\\evil.example/x", _FALLBACK, ("backslash_in_path",)),
    # urlsplit drops tab, CR and LF as a browser does: these read as
    # « //evil.example » and are refused as the HOST they become — the
    # scheme/netloc check runs BEFORE the control-character one.
    Row("tab-smuggled-host", "/\t/evil.example/x", _FALLBACK,
        ("scheme_or_netloc_present",)),
    Row("lf-smuggled-host", "/\n/evil.example/x", _FALLBACK,
        ("scheme_or_netloc_present",)),
    # A smuggled host urlsplit cannot even parse (an unclosed IPv6 bracket)
    # used to raise ValueError out of the door: refused, never a 500.
    Row("unparsable-smuggled-host", "/\t/[evil.example", _FALLBACK,
        ("scheme_or_netloc_present",)),
    # An internal path: kept, stripped.
    Row("internal", "/dossiers/d1", "/dossiers/d1"),
    Row("internal-query", "/dossiers/d1?tab=documents&page=2",
        "/dossiers/d1?tab=documents&page=2"),
    Row("internal-padded", "  /dossiers/d1\n", "/dossiers/d1"),
    Row("internal-root", "/", "/"),
    Row("internal-accented", "/notes/?q=Ébène", "/notes/?q=Ébène"),
    Row("internal-encoded-slashes", "/%2F%2Fevil.example",
        "/%2F%2Fevil.example"),
    Row("internal-fullwidth-solidus", "/\uff0f/evil.example",
        "/\uff0f/evil.example"),
]


@pytest.mark.parametrize("row", DECISIONS, ids=[r.id for r in DECISIONS])
def test_the_door_answers_each_row(caplog, row):
    answer, reasons = _logged(
        caplog, lambda: safe_internal_redirect(row.target, _FALLBACK))
    assert answer == row.answer
    assert reasons == list(row.reasons)
    for record in caplog.records:
        if getattr(record, "json_fields", {}).get("event") == _EVENT:
            # One WARNING carrying exactly the event and its reason: never
            # the URL, which is attacker-chosen (« Do not log PII »).
            assert record.levelno == logging.WARNING
            assert record.getMessage() == _EVENT
            assert record.json_fields == {"event": _EVENT,
                                          "reason": row.reasons[0]}


@pytest.mark.parametrize("row", DECISIONS, ids=[r.id for r in DECISIONS])
def test_the_doors_answer_passes_again_silently(caplog, row):
    """A route validates where it reads, then may hand the CLEAN value to
    a second check: that check must neither change it nor log a line."""
    first = safe_internal_redirect(row.target, _FALLBACK)
    first_blank = safe_internal_redirect(row.target, "")
    (again, again_blank), reasons = _logged(caplog, lambda: (
        safe_internal_redirect(first, _FALLBACK),
        safe_internal_redirect(first_blank, ""),
    ))
    assert (again, again_blank) == (first, first_blank)
    assert reasons == []


def _door_reasons() -> set[str]:
    """Every reason ``safe_internal_redirect`` can log, read from its
    source — the single authority the table and the docs must follow."""
    tree = ast.parse((_ATHENA / "security.py").read_text(encoding="utf-8"))
    (door,) = [n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == _DOOR]
    return {
        node.args[0].value
        for node in ast.walk(door)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_log_redirect_rejection"
    }


def test_the_table_covers_every_reason_and_every_kind_of_answer():
    reasons = _door_reasons()
    assert reasons >= {"not_internal_path", "control_character"}
    assert {r for row in DECISIONS for r in row.reasons} == reasons
    assert any(not row.reasons and row.answer == _FALLBACK
               for row in DECISIONS), "no absent row"
    assert any(not row.reasons and row.answer != _FALLBACK
               for row in DECISIONS), "no accepted row"


def test_every_reason_is_documented_in_the_observability_row():
    text = (_ATHENA / "OBSERVABILITY.md").read_text(encoding="utf-8")
    (row,) = [line for line in text.splitlines()
              if line.startswith(f"| `{_EVENT}` |")]
    for reason in sorted(_door_reasons()):
        assert f'"{reason}"' in row, reason


@pytest.mark.parametrize("character, location", [
    ("\n", None),
    ("\r", None),
    ("\t", "/taches/x"),
    ("\x00", "/taches/%00x"),
    ("\x1b", "/taches/%1Bx"),
    ("\x7f", "/taches/%7Fx"),
    ("\x85", "/taches/%C2%85x"),
], ids=["lf", "cr", "tab", "nul", "escape", "del", "c1-nel"])
def test_what_a_location_header_makes_of_a_control_character(
        character, location):
    """Why the door refuses EVERY control character, as OBSERVABILITY.md's
    ``control_character`` says, for the Werkzeug in the lock: a CR or LF
    makes it refuse the header (``None``: a 500 after the write had
    committed), a tab is silently dropped and the rest are percent-encoded
    into another path. None lands where the link said."""
    target = f"/taches/{character}x"
    if location is None:
        with pytest.raises(ValueError):
            werkzeug_redirect(target)
    else:
        sent = werkzeug_redirect(target).get_wsgi_headers(create_environ("/"))
        assert sent["Location"] == location


# ══════════════════════════════════════════════════════════════════════
# 2. Rule A, static: every read goes through the door, where it is read
# ══════════════════════════════════════════════════════════════════════

# The request-supplied navigation targets. A read of either key — .get /
# .getlist / .pop with the key given first or as ``key=``, or a subscript
# — is a read of a target, whatever the mapping: request.args,
# request.form, request.values, an alias such as ``f = request.form``, a
# dict built from them. Deliberately conservative: a read the guard cannot
# prove request-free must be wrapped anyway. A key that is not a constant
# (``k = "return_to"`` then ``request.args.get(k)``) is beyond a syntactic
# sweep: a review item, not something this guard can prove absent.
_NAV_FIELDS = frozenset({"return_to", "next"})
_READ_METHODS = frozenset({"get", "getlist", "pop"})


def _call_name(call: ast.Call) -> Optional[str]:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _nav_reads(source: str) -> list[tuple[int, str, bool]]:
    """Every read of a navigation field: (line, field, wrapped).

    Wrapped means: the read IS the first positional argument of a call to
    the door — not nested in another expression, not the fallback, not a
    keyword argument."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree)
               for child in ast.iter_child_nodes(node)}
    found = []
    for node in ast.walk(tree):
        field = None
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _READ_METHODS):
            key = (node.args[0] if node.args else
                   next((k.value for k in node.keywords if k.arg == "key"),
                        None))
            if isinstance(key, ast.Constant) and key.value in _NAV_FIELDS:
                field = key.value
        elif (isinstance(node, ast.Subscript)
              and isinstance(node.ctx, ast.Load)
              and isinstance(node.slice, ast.Constant)
              and node.slice.value in _NAV_FIELDS):
            field = node.slice.value
        if field is None:
            continue
        parent = parents.get(node)
        wrapped = (isinstance(parent, ast.Call)
                   and _call_name(parent) == _DOOR
                   and bool(parent.args) and parent.args[0] is node)
        found.append((node.lineno, field, wrapped))
    return found


def _raw_reads(source: str) -> list[tuple[int, str]]:
    return [(line, field) for line, field, wrapped in _nav_reads(source)
            if not wrapped]


# A whole request mapping handed on — spread into a call (a template's
# context: ``**request.args``) or merged into a mapping
# (``ctx.update(request.args)``) — carries every key it holds,
# ``return_to`` and ``next`` included, past the door unread. A mapping is
# request.args / form / values, a name assigned from one, its
# ``.to_dict()`` or ``.copy()``, ``dict(…)`` / ``MultiDict(…)`` of one, or
# a dict display spreading one (``{**record, **request.form}``). A named
# argument is no spread (``form=request.form.to_dict()``; the account
# forms re-rendering a refused edit, ``account=merged``): the template
# reads the fields the route names.
_REQUEST_MAPPINGS = frozenset({"args", "form", "values"})


def _is_request_mapping(node: ast.AST, aliases: set) -> bool:
    if isinstance(node, ast.Dict):
        return any(key is None and _is_request_mapping(value, aliases)
                   for key, value in zip(node.keys, node.values))
    if isinstance(node, ast.Call):
        if (isinstance(node.func, ast.Attribute)
                and node.func.attr in ("to_dict", "copy")):
            return _is_request_mapping(node.func.value, aliases)
        if (isinstance(node.func, ast.Name)
                and node.func.id in ("dict", "MultiDict")):
            return any(_is_request_mapping(a, aliases) for a in node.args)
        return False
    if isinstance(node, ast.Name):
        return node.id in aliases
    return (isinstance(node, ast.Attribute)
            and node.attr in _REQUEST_MAPPINGS
            and isinstance(node.value, ast.Name)
            and node.value.id == "request")


def _mapping_spreads(source: str) -> list[int]:
    """The lines handing a whole request mapping on: a ``**`` spread into
    a call, or an argument of ``.update(…)``."""
    tree = ast.parse(source)
    aliases = {target.id
               for node in ast.walk(tree) if isinstance(node, ast.Assign)
               and _is_request_mapping(node.value, set())
               for target in node.targets if isinstance(target, ast.Name)}
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        handed = [k.value for k in node.keywords if k.arg is None]
        if isinstance(node.func, ast.Attribute) and node.func.attr == "update":
            handed += node.args
        if any(_is_request_mapping(value, aliases) for value in handed):
            found.append(node.lineno)
    return found


def _rule_a_lines(source: str) -> list[int]:
    """Every line the static guard refuses: a raw read or a spread."""
    return sorted([line for line, _ in _raw_reads(source)]
                  + _mapping_spreads(source))


def _main_service_modules() -> list[pathlib.Path]:
    """The main service's Python: everything under athena/ but the tests,
    the scripts (no request) and the portal (its own service, which may
    not import ``security`` — see the dedicated test below)."""
    return sorted(
        p for p in _ATHENA.rglob("*.py")
        if p.relative_to(_ATHENA).parts[0] not in ("tests", "scripts",
                                                   "client")
    )


def test_every_read_of_a_target_goes_through_the_door_where_it_is_read():
    raw, wrapped_fields = {}, set()
    for path in _main_service_modules():
        reads = _nav_reads(path.read_text(encoding="utf-8"))
        wrapped_fields |= {field for _, field, ok in reads if ok}
        bad = [(line, field) for line, field, ok in reads if not ok]
        if bad:
            raw[str(path.relative_to(_ATHENA))] = bad
    assert raw == {}, (
        "Read a return_to / next ONLY as the first argument of "
        "security.safe_internal_redirect, where it is read — never rendered "
        f"or redirected raw: {raw}")
    # Not vacuous: the sweep does see the reads it accepts.
    assert wrapped_fields == _NAV_FIELDS


def test_no_request_mapping_is_handed_on_whole():
    spreads = {}
    for path in _main_service_modules():
        lines = _mapping_spreads(path.read_text(encoding="utf-8"))
        if lines:
            spreads[str(path.relative_to(_ATHENA))] = lines
    assert spreads == {}, (
        "Hand a template or a context the fields it needs, never a whole "
        "request mapping: it carries return_to / next past the door: "
        f"{spreads}")


def test_the_portal_reads_no_navigation_target():
    """The portal may not import the main service's ``security``: a
    navigation target read there — or a whole request mapping handed on —
    would need its own door. None exists."""
    found = {}
    for path in sorted((_ATHENA / "client").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        hits = ([line for line, _, _ in _nav_reads(source)]
                + _mapping_spreads(source))
        if hits:
            found[str(path.relative_to(_ATHENA))] = hits
    assert found == {}


_RAW_PLANTS = {
    "render-args": 'render_template("p.html", '
                   'return_to=request.args.get("return_to", ""))',
    "redirect-form": 'return redirect(request.form.get("return_to"))',
    "form-alias": 'f = request.form\nreturn_to = f.get("return_to", "")',
    "subscript": 'return_to = request.form["return_to"]',
    "getlist-next": 'nxt = request.args.getlist("next")[0]',
    "values-next": 'nxt = request.values.get("next") or "/"',
    "pop": 'nxt = dict(request.args).pop("next", "")',
    "another-function": 'x = sanitize(request.args.get("return_to"))',
    "door-fallback": 'x = safe_internal_redirect("/x", '
                     'request.args.get("return_to"))',
    "door-keyword": 'x = safe_internal_redirect(target='
                    'request.args.get("return_to"), fallback="")',
    "door-nested": 'x = safe_internal_redirect('
                   'request.args.get("return_to").strip(), "")',
    "door-after-or": 'x = request.form.get("return_to") or '
                     'safe_internal_redirect("", "/")',
    "keyword-key": 'x = request.args.get(key="return_to", default="")',
}

_SPREAD_PLANTS = {
    "spread-to_dict": 'render_template("p.html", **request.args.to_dict())',
    "spread-mapping": 'render_template("p.html", **request.args)',
    "spread-alias": 'f = request.form\nrender_template("p.html", **f)',
    "update-to_dict": 'ctx.update(request.args.to_dict())',
    "update-dict": 'ctx.update(dict(request.values))',
    "spread-dict-display": 'render_template("p.html", '
                           '**{**ctx, **request.args})',
    "spread-merged-alias": 'ctx = {**base, **request.form.copy()}\n'
                           'render_template("p.html", **ctx)',
}

_CLEAN_PLANTS = {
    "door-bare": 'x = safe_internal_redirect('
                 'request.args.get("return_to"), "")',
    "door-attribute": 'x = security.safe_internal_redirect('
                      'request.form.get("next"), url_for("dashboard.index"))',
    "door-subscript": 'x = safe_internal_redirect(request.form["return_to"],'
                      ' "")',
    "store": 'ctx["return_to"] = clean',
    "keyword-to-url_for": 'url_for("notes.note_detail", note_id=n, '
                          'return_to=clean or None)',
    "another-field": 'dossier_id = request.args.get("dossier_id", "")',
    "a-parameter": 'def f(return_to: str, next: str = ""):\n    return 1',
    "door-keyword-key": 'x = safe_internal_redirect('
                        'request.args.get(key="return_to"), "")',
    "named-mapping": 'render_template("p.html", form=request.form.to_dict())',
    "named-merged-record": 'merged = {**account, **request.form.to_dict()}\n'
                           'render_template("p.html", account=merged)',
    "spread-context": 'render_template("p.html", **ctx)',
    "update-by-name": 'ctx.update(return_to=clean)',
}


@pytest.mark.parametrize("snippet", list(_RAW_PLANTS.values()),
                         ids=list(_RAW_PLANTS))
def test_the_guard_flags_a_planted_raw_read(snippet):
    assert len(_raw_reads(snippet)) == 1, snippet


@pytest.mark.parametrize("snippet", list(_SPREAD_PLANTS.values()),
                         ids=list(_SPREAD_PLANTS))
def test_the_guard_flags_a_whole_request_mapping_handed_on(snippet):
    assert len(_mapping_spreads(snippet)) == 1, snippet


@pytest.mark.parametrize("snippet", list(_CLEAN_PLANTS.values()),
                         ids=list(_CLEAN_PLANTS))
def test_the_guard_accepts_a_wrapped_read_and_what_is_no_read(snippet):
    assert _rule_a_lines(snippet) == [], snippet


@pytest.mark.parametrize("module, wrapped, raw", [
    # A GET page's « Retour » link, rendered raw again.
    ("routes/tasks.py",
     'safe_internal_redirect(request.args.get("return_to"), "")',
     'request.args.get("return_to", "")'),
    # A POST handler's read, redirected raw again.
    ("routes/notes.py",
     'safe_internal_redirect(request.form.get("return_to"), "")',
     'request.form.get("return_to", "")'),
    # The login's « next », back to its own hand-written check.
    ("routes/auth_routes.py",
     'safe_internal_redirect(\n            request.args.get("next"), '
     'url_for("dashboard.index")\n        )',
     '_safe_next(request.args.get("next", "")) or url_for("dashboard.index")'),
])
def test_the_guard_flags_a_raw_read_planted_in_a_real_module(
        module, wrapped, raw):
    source = (_ATHENA / module).read_text(encoding="utf-8")
    assert _raw_reads(source) == []
    assert wrapped in source, "the plant no longer matches the module"
    planted = source.replace(wrapped, raw, 1)
    line = source[:source.index(wrapped)].count("\n") + 1
    field = "next" if "next" in raw else "return_to"
    assert _raw_reads(planted) == [(line, field)]


_TASK_DETAIL_READ = ('ctx["return_to"] = safe_internal_redirect('
                     'request.args.get("return_to"), "")')


@pytest.mark.parametrize("module, anchor, plant, culprit", [
    # The page's query string merged whole into its context.
    ("routes/tasks.py", _TASK_DETAIL_READ,
     _TASK_DETAIL_READ + "\n    ctx.update(request.args.to_dict())",
     "ctx.update(request.args"),
    # The page's read, its key given as a keyword.
    ("routes/tasks.py", _TASK_DETAIL_READ,
     'ctx["return_to"] = request.args.get(key="return_to", default="")',
     'request.args.get(key="return_to"'),
    # A NEW page spreading its query string into the template: it reads no
    # key, so the page table below cannot derive it either.
    ("routes/budgets.py", '@budgets_bp.route("/historique")',
     '@budgets_bp.route("/apercu")\n@login_required\n'
     'def budget_apercu() -> str:\n'
     '    return render_template("budgets/history.html", versions=[], '
     '**request.args.to_dict())\n\n\n@budgets_bp.route("/historique")',
     "**request.args.to_dict()"),
], ids=["context-update", "keyword-key", "new-page-spread"])
def test_the_guard_flags_an_evasion_planted_in_a_real_module(
        module, anchor, plant, culprit):
    source = (_ATHENA / module).read_text(encoding="utf-8")
    assert _rule_a_lines(source) == []
    assert source.count(anchor) == 1, "the plant no longer matches the module"
    planted = source.replace(anchor, plant, 1)
    assert planted.count(culprit) == 1
    line = planted[:planted.index(culprit)].count("\n") + 1
    assert _rule_a_lines(planted) == [line]


# The sweeps above, like the CodeQL barrier (.github/codeql/
# athena-python-queries), trust the door's NAME: any other callable bound
# to that name would pass both. One definition, security.py's, and every
# module that calls the door imports it from there, under that name.
_DOOR_HOME = "security.py"


def _door_use(rel: str, source: str) -> tuple[list[tuple[int, str]], bool]:
    """In module ``rel``: what else could pass for the door, as (line,
    how), and whether the module calls it."""
    tree = ast.parse(source)
    found, imported, called = [], False, False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            if node.name == _DOOR and not (
                    rel == _DOOR_HOME and isinstance(node, ast.FunctionDef)
                    and node in tree.body):
                found.append((node.lineno, "defined"))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if node.id == _DOOR:
                found.append((node.lineno, "assigned"))
        elif isinstance(node, ast.arg):
            if node.arg == _DOOR:
                found.append((node.lineno, "a parameter"))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) != _DOOR:
                    continue
                if (isinstance(node, ast.ImportFrom) and node.level == 0
                        and node.module == "security"
                        and alias.name == _DOOR):
                    imported = True
                else:
                    found.append((node.lineno, "imported from elsewhere"))
        elif isinstance(node, ast.Call) and _call_name(node) == _DOOR:
            called = True
    if called and rel != _DOOR_HOME and not imported:
        found.append((0, "called, not imported from security"))
    return found, called


def test_the_door_is_security_pys_one_function_imported_by_its_callers():
    impostors, callers = {}, set()
    for path in _main_service_modules():
        rel = path.relative_to(_ATHENA).as_posix()
        found, called = _door_use(rel, path.read_text(encoding="utf-8"))
        if found:
            impostors[rel] = found
        if called and rel != _DOOR_HOME:
            callers.add(rel)
    assert impostors == {}, (
        "The door is security.safe_internal_redirect, imported by that name "
        f"— no other function may carry it: {impostors}")
    # Not vacuous: the sweep does see the modules that call the door.
    assert callers, "no module calls the door: the sweep went blind"


_IMPOSTORS = {
    "local-def": 'from security import safe_internal_redirect\n'
                 'def safe_internal_redirect(target, fallback):\n'
                 '    return target',
    "lambda": 'from security import safe_internal_redirect\n'
              'safe_internal_redirect = lambda target, fallback: target',
    "method": 'class Pages:\n'
              '    def safe_internal_redirect(self, target, fallback):\n'
              '        return target',
    "foreign-import": 'from utils.urls import safe_internal_redirect',
    "renamed-import": 'from utils.urls import check as '
                      'safe_internal_redirect',
    "star-import": 'from utils.urls import *\n'
                   'x = safe_internal_redirect(request.args.get("next"), "/")',
}

_GENUINE = {
    "imported": 'from security import safe_internal_redirect\n'
                'x = safe_internal_redirect(request.args.get("next"), "/")',
    "imported-among-others": 'from security import limiter, '
                             'safe_internal_redirect, sanitize\n'
                             'x = safe_internal_redirect('
                             'request.form.get("return_to"), "")',
}


@pytest.mark.parametrize("snippet", list(_IMPOSTORS.values()),
                         ids=list(_IMPOSTORS))
def test_the_guard_flags_another_function_passing_for_the_door(snippet):
    found, _called = _door_use("routes/plant.py", snippet)
    assert len(found) == 1, snippet


@pytest.mark.parametrize("snippet", list(_GENUINE.values()),
                         ids=list(_GENUINE))
def test_the_guard_accepts_the_door_imported_from_security(snippet):
    assert _door_use("routes/plant.py", snippet) == ([], True), snippet


# Templates and page scripts: no template reads a target from the request
# (it renders the route's validated ``return_to``), and no page script
# reads one from the URL (it would navigate past the server's door).
_JINJA_TAG = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)
_REQUEST_DATA = re.compile(r"\brequest\s*\.\s*(?:args|form|values)\b")
_NAV_NAME = re.compile(r"\b(?:return_to|next)\b")
_SCRIPT = re.compile(r"<script\b[^>]*>(.*?)</script\b[^>]*>", re.S | re.I)
_SCRIPT_READ = re.compile(r"\.get\(\s*['\"](?:return_to|next)['\"]\s*\)")


def _template_reads(text: str, *, is_script: bool = False) -> list[str]:
    found = []
    if not is_script:
        found += [tag for tag in _JINJA_TAG.findall(text)
                  if _REQUEST_DATA.search(tag) and _NAV_NAME.search(tag)]
    scripts = [text] if is_script else _SCRIPT.findall(text)
    for script in scripts:
        found += _SCRIPT_READ.findall(script)
    return found


def test_no_template_or_page_script_reads_a_target_from_the_request():
    found = {}
    for path in sorted(_ATHENA.rglob("*")):
        rel = path.relative_to(_ATHENA)
        if rel.parts[0] == "tests" or "vendor" in rel.parts:
            continue
        if path.suffix == ".html":
            reads = _template_reads(path.read_text(encoding="utf-8"))
        elif path.suffix == ".js":
            reads = _template_reads(path.read_text(encoding="utf-8"),
                                    is_script=True)
        else:
            continue
        if reads:
            found[str(rel)] = reads
    assert found == {}


@pytest.mark.parametrize("text", [
    "<a href=\"{{ request.args.get('return_to') }}\">Retour</a>",
    "<a href=\"{{ request.args['next'] or '/' }}\">x</a>",
    "{% set retour = request.values.return_to or url_for('x') %}",
    "<input value=\"{{ request.form.get('return_to', '') }}\">",
    ("<script nonce=\"n\">window.location = new URLSearchParams("
     "location.search).get('return_to');</script>"),
    "<script>const n = params.get(\"next\");</script>",
], ids=["href-args", "subscript-next", "set-values", "hidden-form",
        "script-searchparams", "script-next"])
def test_the_template_guard_flags_a_planted_read(text):
    assert _template_reads(text) != [], text


@pytest.mark.parametrize("text", [
    "<a href=\"{{ return_to or url_for('tasks.task_list') }}\">Retour</a>",
    "<input type=\"hidden\" name=\"return_to\" value=\"{{ return_to or '' }}\">",
    ("{% if request.args.get('erreur') %}{{ request.args.get('erreur') }}"
     "{% endif %}"),
    "<script>window.location = this.champ('return_to') || repli;</script>",
], ids=["route-value", "hidden-field", "another-field", "hidden-field-js"])
def test_the_template_guard_accepts_the_routes_value(text):
    assert _template_reads(text) == [], text


# ══════════════════════════════════════════════════════════════════════
# 3. The pages and the handlers — real routes, templates and models
# ══════════════════════════════════════════════════════════════════════

_BLUEPRINTS = (
    auth_routes.auth_bp, budgets_routes.budgets_bp,
    dashboard_routes.dashboard_bp, doc_templates_routes.doc_templates_bp,
    documents_routes.documents_bp, dossiers_routes.dossiers_bp,
    hearings_routes.hearings_bp, invoices_routes.invoices_bp,
    notes_routes.notes_bp, parties_routes.parties_bp,
    protocols_routes.protocols_bp, tasks_routes.tasks_bp,
    time_expenses_routes.time_expenses_bp,
)


def _fake_modules() -> list:
    """Every module holding the Firestore client — derived, so a model a
    route starts to read tomorrow cannot reach the mocked client."""
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, *_fake_modules())


@pytest.fixture
def app(db, monkeypatch):
    # The document sheet signs a download URL: no Storage here.
    monkeypatch.setattr(documents_routes, "get_signed_url",
                        lambda *a, **k: None)
    flask_app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                      static_folder=str(_ATHENA / "static"))
    flask_app.secret_key = "t"
    flask_app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms,
                                       csp_nonce="n")
    flask_app.jinja_env.filters.update(
        to_mtl=to_mtl, phone=format_phone_display,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=lambda v: v, markdown=markdown_to_safe_html,
    )
    for bp in _BLUEPRINTS:
        flask_app.register_blueprint(bp)
    return flask_app


@pytest.fixture
def client(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _new_task(dossier_id: str) -> str:
    doc, errors = task_model.create_task(
        {"dossier_id": dossier_id, "title": "Préparer la requête"})
    assert errors == [], errors
    return doc["id"]


def _new_note(dossier_id: str) -> str:
    doc, errors = note_model.create_note(
        {"dossier_id": dossier_id, "title": "Recherche",
         "content": "Premier jet.", "category": "recherche"})
    assert errors == [], errors
    return doc["id"]


def _new_hearing(dossier_id: str) -> str:
    doc, errors = hearing_model.create_hearing({
        "dossier_id": dossier_id, "title": "Audience",
        "hearing_type": "audience", "status": "confirmée",
        "start_datetime": datetime(2026, 10, 15, 13, tzinfo=UTC),
        "end_datetime": datetime(2026, 10, 15, 14, tzinfo=UTC)})
    assert errors == [], errors
    return doc["id"]


def _new_time_entry(dossier_id: str) -> str:
    doc, errors = time_entry_model.create_time_entry({
        "dossier_id": dossier_id, "date": DT, "description": "Premier",
        "hours": 1.5, "rate": 30000, "billable": True})
    assert errors == [], errors
    return doc["id"]


def _new_expense(dossier_id: str) -> str:
    doc, errors = expense_model.create_expense({
        "dossier_id": dossier_id, "date": DT, "description": "Premier",
        "amount": 5000, "category": "timbre_judiciaire", "taxable": False})
    assert errors == [], errors
    return doc["id"]


def _new_series_occurrence(dossier_id: str) -> str:
    """The first occurrence of a fresh two-hearing chain."""
    occurrences, errors = hearing_model.create_hearing_series({
        "dossier_id": dossier_id, "title": "Audience",
        "hearing_type": "audience", "status": "confirmée",
        "start_datetime": datetime(2026, 10, 15, 13, tzinfo=UTC),
        "end_datetime": datetime(2026, 10, 15, 14, tzinfo=UTC),
    }, "hebdomadaire", count=2)
    assert errors == [], errors
    return occurrences[0]["id"]


def _new_document(db, dossier_id: str) -> str:
    """The shared bench's document — its record only (no stored file)."""
    return _seed_document(db, dossier_id)


def _new_cancelled_invoice(db, dossier_id: str, inv_id: str = "inv1") -> str:
    """An « annulée » invoice: the one state whose sheet carries the
    delete form (and its hidden return_to), and that the delete accepts."""
    db.seed(f"invoices/{inv_id}", {
        **invoice_model._default_doc(), "id": inv_id,
        "invoice_number": "2026-F001", "dossier_id": dossier_id,
        "client_id": "p1", "client_name": "Jean Tremblay",
        "date": DT, "due_date": datetime(2026, 4, 3, tzinfo=UTC),
        "status": "annulée", "created_at": DT, "updated_at": DT,
        "etag": "e-inv-1",
    })
    return inv_id


def _seed_dossier(db) -> str:
    dossier, errors = dossier_model.create_dossier({
        "file_number": "2026-001", "title": "Tremblay c. Lavoie",
        "clients": [{"id": "p1", "name": "Jean Tremblay",
                     "roles": ["demandeur"]}],
    })
    assert errors == [], errors
    return dossier["id"]


@pytest.fixture
def ids(db) -> dict:
    """One record of every kind a page opens."""
    did = _seed_dossier(db)
    return {
        "dossier": did,
        "task": _new_task(did),
        "note": _new_note(did),
        "hearing": _new_hearing(did),
        "time_entry": _new_time_entry(did),
        "expense": _new_expense(did),
        "document": _new_document(db, did),
        "invoice": _new_cancelled_invoice(db, did),
    }


# ── The pages that render a return_to ────────────────────────────────


@dataclass(frozen=True)
class Page:
    id: str
    endpoint: str
    args: Callable[[dict], dict]          # ids -> url_for arguments


PAGES = [
    Page("budget_form", "budgets.budget_form",
         lambda i: {"dossier_id": i["dossier"]}),
    Page("budget_history", "budgets.budget_history",
         lambda i: {"dossier_id": i["dossier"]}),
    Page("document_detail", "documents.document_detail",
         lambda i: {"document_id": i["document"]}),
    Page("document_edit", "documents.document_edit",
         lambda i: {"document_id": i["document"]}),
    Page("document_upload_form", "documents.document_upload_form",
         lambda i: {"dossier_id": i["dossier"]}),
    Page("hearing_new", "hearings.hearing_new", lambda i: {}),
    Page("hearing_detail", "hearings.hearing_detail",
         lambda i: {"hearing_id": i["hearing"]}),
    Page("hearing_edit", "hearings.hearing_edit",
         lambda i: {"hearing_id": i["hearing"]}),
    Page("invoice_new_selector", "invoices.invoice_new", lambda i: {}),
    Page("invoice_new_dossier", "invoices.invoice_new",
         lambda i: {"dossier_id": i["dossier"]}),
    Page("invoice_detail", "invoices.invoice_detail",
         lambda i: {"invoice_id": i["invoice"]}),
    Page("note_new", "notes.note_new", lambda i: {}),
    Page("note_detail", "notes.note_detail",
         lambda i: {"note_id": i["note"]}),
    Page("note_edit", "notes.note_edit", lambda i: {"note_id": i["note"]}),
    Page("task_new", "tasks.task_new", lambda i: {}),
    Page("task_detail", "tasks.task_detail",
         lambda i: {"task_id": i["task"]}),
    Page("task_edit", "tasks.task_edit", lambda i: {"task_id": i["task"]}),
    Page("time_entry_new", "time_expenses.time_entry_new", lambda i: {}),
    Page("time_entry_edit", "time_expenses.time_entry_edit",
         lambda i: {"entry_id": i["time_entry"]}),
    Page("time_entry_phase_edit", "time_expenses.time_entry_phase_edit",
         lambda i: {"entry_id": i["time_entry"]}),
    Page("expense_new", "time_expenses.expense_new", lambda i: {}),
    Page("expense_edit", "time_expenses.expense_edit",
         lambda i: {"expense_id": i["expense"]}),
    Page("expense_phase_edit", "time_expenses.expense_phase_edit",
         lambda i: {"expense_id": i["expense"]}),
]
_PAGE_IDS = [p.id for p in PAGES]

# A crafted « Retour » target of each refusal reason, each carrying a
# marker no page prints otherwise.
CRAFTED = [
    ("javascript:alert('rt31337')", "not_internal_path"),
    ("https://evil.example/rt31337", "not_internal_path"),
    ("//evil.example/rt31337", "not_internal_path"),
    ("/\\evil.example/rt31337", "backslash_in_path"),
    ("/\t/evil.example/rt31337", "scheme_or_netloc_present"),
    ("/taches/\nrt31337", "control_character"),
]
_CRAFTED_IDS = ["javascript", "https", "protocol-relative", "backslash",
                "tab-smuggled-host", "control-character"]


def _url(app, page: Page, ids: dict, **extra) -> str:
    with app.test_request_context():
        return url_for(page.endpoint, **page.args(ids), **extra)


def _with_return_to(url: str, value: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}return_to={quote(value, safe='')}"


def _blueprint_names(tree: ast.Module) -> dict[str, str]:
    """{module variable: blueprint name} for ``x = Blueprint("name", …)``."""
    names = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)
                and _call_name(node.value) == "Blueprint"
                and node.value.args
                and isinstance(node.value.args[0], ast.Constant)):
            names[node.targets[0].id] = node.value.args[0].value
    return names


def _get_views_reading_return_to() -> set[str]:
    """Every GET view of the main service whose body reads ``return_to``,
    as ``blueprint.endpoint`` — derived from the route sources."""
    endpoints = set()
    for path in _main_service_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        blueprints = _blueprint_names(tree)
        for fn in tree.body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            for deco in fn.decorator_list:
                if not (isinstance(deco, ast.Call)
                        and isinstance(deco.func, ast.Attribute)
                        and deco.func.attr in ("route", "get")
                        and isinstance(deco.func.value, ast.Name)
                        and deco.func.value.id in blueprints):
                    continue
                kwargs = {k.arg: k.value for k in deco.keywords}
                methods = (ast.literal_eval(kwargs["methods"])
                           if "methods" in kwargs else ["GET"])
                if "GET" not in methods:
                    continue
                reads = _nav_reads(ast.unparse(fn))
                if any(field == "return_to" for _, field, _ in reads):
                    name = (ast.literal_eval(kwargs["endpoint"])
                            if "endpoint" in kwargs else fn.name)
                    endpoints.add(f"{blueprints[deco.func.value.id]}.{name}")
    return endpoints


def test_the_page_table_is_every_get_view_that_reads_a_return_to():
    derived = _get_views_reading_return_to()
    assert derived, "the derivation found no page: it went blind"
    assert derived == {p.endpoint for p in PAGES}


@pytest.mark.parametrize("page", PAGES, ids=_PAGE_IDS)
def test_a_page_opened_without_return_to_logs_nothing(
        caplog, app, client, ids, page):
    resp, reasons = _logged(caplog, lambda: client.get(_url(app, page, ids)))
    assert resp.status_code == 200
    assert reasons == []


@pytest.mark.parametrize("crafted, reason", CRAFTED, ids=_CRAFTED_IDS)
@pytest.mark.parametrize("page", PAGES, ids=_PAGE_IDS)
def test_a_crafted_return_to_gives_the_page_its_ordinary_link(
        caplog, app, client, ids, page, crafted, reason):
    """Byte for byte the page opened without one — the « Retour » href,
    its label, every hidden field and every link carrying it on — and ONE
    log line naming the refusal. No banner: only a crafted link does it."""
    url = _url(app, page, ids)
    plain = client.get(url)
    assert plain.status_code == 200
    resp, reasons = _logged(
        caplog, lambda: client.get(_with_return_to(url, crafted)))
    assert resp.status_code == 200
    assert resp.get_data(as_text=True) == plain.get_data(as_text=True)
    assert reasons == [reason]


@pytest.mark.parametrize("page", PAGES, ids=_PAGE_IDS)
def test_an_internal_return_to_is_kept(caplog, app, client, ids, page):
    """The fix does not over-block: every in-app value is an absolute path
    url_for built, a query included."""
    internal = f"/dossiers/{ids['dossier']}?tab=documents&page=2"
    resp, reasons = _logged(caplog, lambda: client.get(
        _with_return_to(_url(app, page, ids), internal)))
    assert resp.status_code == 200
    assert f'="{html_module.escape(internal)}"' in resp.get_data(as_text=True)
    assert reasons == []


# ── The edit forms: opened from a crafted link, saved, refused ───────

# The edit forms of the shared bench that carry a return_to — checked
# against the rendered pages below, so this list cannot drift.
_RETURN_TO_FORMS = ("time_entry", "time_entry_phase", "expense",
                    "expense_phase", "note", "task", "document", "hearing")
EDIT_FORMS = [c for c in _EDIT_CASES if c.id in _RETURN_TO_FORMS]
_RETURN_TO_INPUT = re.compile(r'name="return_to" value="([^"]*)"')


def test_every_return_to_form_is_one_of_the_shared_benchs_cases():
    assert set(_RETURN_TO_FORMS) <= {c.id for c in _EDIT_CASES}


@pytest.mark.parametrize("case", _EDIT_CASES, ids=[c.id for c in _EDIT_CASES])
def test_an_edit_form_renders_a_return_to_exactly_when_listed(
        db, client, case):
    dossier_id = _seed_dossier(db)
    record_id = case.seed(db, dossier_id)
    page = client.get(case.edit_url(record_id))
    assert page.status_code == 200
    renders = 'name="return_to"' in page.get_data(as_text=True)
    assert renders == (case.id in _RETURN_TO_FORMS)


@pytest.mark.parametrize("case", EDIT_FORMS, ids=[c.id for c in EDIT_FORMS])
def test_a_form_opened_from_a_crafted_link_saves_without_a_second_line(
        caplog, db, client, case):
    """ONE line for one crafted value: the page refuses it as it renders
    (its hidden field left blank), and the save of that page — posting
    the blank — logs nothing and lands where a save without one lands."""
    dossier_id = _seed_dossier(db)
    record_id = case.seed(db, dossier_id)
    page, reasons = _logged(caplog, lambda: client.get(_with_return_to(
        case.edit_url(record_id), "https://evil.example/rt31337")))
    assert reasons == ["not_internal_path"]
    html = page.get_data(as_text=True)
    assert set(_RETURN_TO_INPUT.findall(html)) == {""}
    form = {**case.form(dossier_id, case.marker),
            "expected_etag": _etags(html)[0], "return_to": ""}
    resp, reasons = _logged(
        caplog, lambda: client.post(case.post_url(record_id), data=form))
    assert resp.status_code == 302
    assert "evil" not in resp.headers["Location"]
    assert reasons == []


@pytest.mark.parametrize("case", EDIT_FORMS, ids=[c.id for c in EDIT_FORMS])
def test_a_refused_edit_does_not_reflect_a_crafted_return_to(
        caplog, db, client, case):
    dossier_id = _seed_dossier(db)
    record_id = case.seed(db, dossier_id)
    refused = case.invalid(dossier_id, case.marker)
    plain = client.post(case.post_url(record_id), data=refused)
    resp, reasons = _logged(caplog, lambda: client.post(
        case.post_url(record_id),
        data={**refused, "return_to": "https://evil.example/rt31337"}))
    assert resp.status_code == plain.status_code == 200
    assert resp.get_data(as_text=True) == plain.get_data(as_text=True)
    assert reasons == ["not_internal_path"]


# ── The other refusals that re-render the posted return_to ───────────


@dataclass(frozen=True)
class Refusal:
    id: str
    url: Callable[[dict], str]
    form: Callable[[dict], dict]
    prepare: Optional[Callable] = None     # (db, ids) -> None
    status: int = 200


def _budget_form(i: dict, **over) -> dict:
    return {"dossier_id": i["dossier"], "hourly_rate": "275,00",
            "note": "Hypothèse A",
            "lines_json": json.dumps(
                [{"sous_phase": "PRE-01", "hours": 7.5, "frais": "120,00"}]),
            **over}


def _seed_budget(db, i: dict) -> None:
    _doc, errors = budget_model.create_budget({
        "dossier_id": i["dossier"], "hourly_rate": 27500, "note": "v1",
        "lines": [{"sous_phase": "PRE-01", "hours": 7.5, "frais": 12000}],
    })
    assert errors == [], errors


def _invoice_entry(db, i: dict, kind: str) -> None:
    """The row is carried to an invoice: its delete is refused, at 200, on
    its own form."""
    path = (f"timeentries/{i['time_entry']}" if kind == "time"
            else f"expenses/{i['expense']}")
    db.external_write(path, {**db.peek(path), "invoiced": True,
                             "invoice_id": "inv1"})


REFUSALS = [
    Refusal("task_create", lambda i: "/taches/",
            lambda i: {"dossier_id": i["dossier"], "title": ""}),
    Refusal("note_create", lambda i: "/notes/",
            lambda i: {"dossier_id": i["dossier"], "title": "",
                       "content": "x", "category": "recherche"}),
    Refusal("hearing_create", lambda i: "/audiences/",
            lambda i: {"dossier_id": i["dossier"], "title": "",
                       "hearing_type": "audience", "status": "confirmée",
                       "start_date": "2026-10-15", "start_time": "09:00",
                       "end_time": "10:00"}),
    Refusal("time_entry_create", lambda i: "/temps/",
            lambda i: {"dossier_id": i["dossier"], "date": "2026-03-04",
                       "description": "x", "hours": "0", "rate": "300"}),
    Refusal("expense_create", lambda i: "/temps/depenses",
            lambda i: {"dossier_id": i["dossier"], "date": "2026-03-04",
                       "description": "x", "category": "timbre_judiciaire",
                       "amount": "0"}),
    Refusal("invoice_create_no_dossier", lambda i: "/factures/",
            lambda i: {"dossier_id": "introuvable"}),
    Refusal("invoice_create_nothing_selected", lambda i: "/factures/",
            lambda i: {"dossier_id": i["dossier"],
                       "invoice_date": "2026-03-04"}),
    Refusal("budget_create_invalid", lambda i: "/budgets/",
            lambda i: _budget_form(i, lines_json="[]"), status=400),
    Refusal("budget_create_stale", lambda i: "/budgets/",
            lambda i: _budget_form(i, base_version="0"),
            prepare=_seed_budget),
    Refusal("time_entry_delete_invoiced",
            lambda i: f"/temps/{i['time_entry']}/delete", lambda i: {},
            prepare=lambda db, i: _invoice_entry(db, i, "time")),
    Refusal("expense_delete_invoiced",
            lambda i: f"/temps/depenses/{i['expense']}/delete", lambda i: {},
            prepare=lambda db, i: _invoice_entry(db, i, "expense")),
]


@pytest.mark.parametrize("case", REFUSALS, ids=[c.id for c in REFUSALS])
def test_a_refusal_does_not_reflect_a_crafted_return_to(
        caplog, db, client, ids, case):
    """The refusal re-renders the page as it would without one — the
    crafted value in no href, no hidden field — and logs ONE line."""
    if case.prepare:
        case.prepare(db, ids)
    plain = client.post(case.url(ids), data=case.form(ids))
    assert plain.status_code == case.status
    resp, reasons = _logged(caplog, lambda: client.post(
        case.url(ids),
        data={**case.form(ids), "return_to": "https://evil.example/rt31337"}))
    assert resp.status_code == case.status
    assert resp.get_data(as_text=True) == plain.get_data(as_text=True)
    assert reasons == ["not_internal_path"]


# ── The saves that land on the door's answer ─────────────────────────


@dataclass(frozen=True)
class Save:
    id: str
    collection: str
    fresh: Callable              # (db, ids) -> the record id the POST takes
    url: Callable[[str], str]    # record id -> POST url
    form: Callable[[dict], dict]  # ids -> a payload the handler saves
    # How the landing carries the target: the Location itself, or — the
    # note pages — the ``return_to`` of the note's own page it lands on.
    threads: bool = False


_SAVES = [
    Save("task_create", "tasks", lambda db, i: "", lambda r: "/taches/",
         lambda i: {"dossier_id": i["dossier"], "title": "Nouvelle",
                    "priority": "normale", "status": "à_faire",
                    "category": "autre"}),
    Save("task_update", "tasks", lambda db, i: _new_task(i["dossier"]),
         lambda r: f"/taches/{r}",
         lambda i: {"dossier_id": i["dossier"], "title": "Modifiée",
                    "priority": "normale", "status": "à_faire",
                    "category": "autre"}),
    Save("task_delete", "tasks", lambda db, i: _new_task(i["dossier"]),
         lambda r: f"/taches/{r}/delete", lambda i: {}),
    Save("note_create", "notes", lambda db, i: "", lambda r: "/notes/",
         lambda i: {"dossier_id": i["dossier"], "title": "Nouvelle",
                    "content": "x", "category": "recherche"}, threads=True),
    Save("note_update", "notes", lambda db, i: _new_note(i["dossier"]),
         lambda r: f"/notes/{r}",
         lambda i: {"dossier_id": i["dossier"], "title": "Modifiée",
                    "content": "y", "category": "recherche"}, threads=True),
    Save("note_delete", "notes", lambda db, i: _new_note(i["dossier"]),
         lambda r: f"/notes/{r}/delete", lambda i: {}),
    Save("hearing_create", "hearings", lambda db, i: "",
         lambda r: "/audiences/",
         lambda i: {"dossier_id": i["dossier"], "title": "Nouvelle",
                    "hearing_type": "audience", "status": "confirmée",
                    "start_date": "2026-10-15", "start_time": "09:00",
                    "end_time": "10:00", "reminder_minutes": "1440",
                    "modalite": "présentiel"}),
    Save("hearing_update", "hearings",
         lambda db, i: _new_hearing(i["dossier"]),
         lambda r: f"/audiences/{r}",
         lambda i: {"dossier_id": i["dossier"], "title": "Modifiée",
                    "hearing_type": "audience", "status": "confirmée",
                    "start_date": "2026-10-15", "start_time": "09:00",
                    "end_time": "10:00", "reminder_minutes": "1440",
                    "modalite": "présentiel"}),
    Save("hearing_delete", "hearings",
         lambda db, i: _new_hearing(i["dossier"]),
         lambda r: f"/audiences/{r}/delete", lambda i: {}),
    Save("hearing_delete_chain", "hearings",
         lambda db, i: _new_series_occurrence(i["dossier"]),
         lambda r: f"/audiences/{r}/delete", lambda i: {"scope": "suivantes"}),
    Save("hearing_unlink", "hearings",
         lambda db, i: _new_series_occurrence(i["dossier"]),
         lambda r: f"/audiences/{r}/detacher", lambda i: {}),
    Save("time_entry_create", "timeentries", lambda db, i: "",
         lambda r: "/temps/",
         lambda i: {"dossier_id": i["dossier"], "date": "2026-03-04",
                    "description": "Nouvelle", "hours": "1.5",
                    "rate": "300", "billable": "on"}),
    Save("time_entry_update", "timeentries",
         lambda db, i: _new_time_entry(i["dossier"]),
         lambda r: f"/temps/{r}",
         lambda i: {"dossier_id": i["dossier"], "date": "2026-03-04",
                    "description": "Modifiée", "hours": "2",
                    "rate": "300", "billable": "on"}),
    Save("time_entry_phase_update", "timeentries",
         lambda db, i: _new_time_entry(i["dossier"]),
         lambda r: f"/temps/{r}/phase",
         lambda i: {"phase": "CTS", "sous_phase": "CTS-02"}),
    Save("time_entry_delete", "timeentries",
         lambda db, i: _new_time_entry(i["dossier"]),
         lambda r: f"/temps/{r}/delete", lambda i: {}),
    Save("expense_create", "expenses", lambda db, i: "",
         lambda r: "/temps/depenses",
         lambda i: {"dossier_id": i["dossier"], "date": "2026-03-04",
                    "description": "Nouvelle",
                    "category": "timbre_judiciaire", "amount": "50"}),
    Save("expense_update", "expenses",
         lambda db, i: _new_expense(i["dossier"]),
         lambda r: f"/temps/depenses/{r}",
         lambda i: {"dossier_id": i["dossier"], "date": "2026-03-04",
                    "description": "Modifiée",
                    "category": "timbre_judiciaire", "amount": "60"}),
    Save("expense_phase_update", "expenses",
         lambda db, i: _new_expense(i["dossier"]),
         lambda r: f"/temps/depenses/{r}/phase",
         lambda i: {"phase": "CTS", "sous_phase": "CTS-02"}),
    Save("expense_delete", "expenses",
         lambda db, i: _new_expense(i["dossier"]),
         lambda r: f"/temps/depenses/{r}/delete", lambda i: {}),
    Save("document_update", "documents",
         lambda db, i: _new_document(db, i["dossier"]),
         lambda r: f"/documents/{r}/edit",
         lambda i: {"display_name": "Lettre", "category": "correspondance",
                    "tags": "", "notes_internes": "Modifiées",
                    "document_date": "", "analyse_presente": "1"}),
    Save("document_delete", "documents",
         lambda db, i: _new_document(db, i["dossier"]),
         lambda r: f"/documents/{r}/delete", lambda i: {}),
    Save("invoice_delete", "invoices",
         lambda db, i: _new_cancelled_invoice(db, i["dossier"], "inv2"),
         lambda r: f"/factures/{r}/delete", lambda i: {}),
    Save("budget_create", "budgets", lambda db, i: "",
         lambda r: "/budgets/", _budget_form),
]
_SAVE_IDS = [s.id for s in _SAVES]

# (posted return_to, the reasons it logs, whether the door keeps it)
_VARIANTS = {
    "blank": ("", [], False),
    "crafted": ("https://evil.example/rt31337", ["not_internal_path"], False),
    # Used to commit, THEN answer 500: Werkzeug refuses a newline in the
    # Location header — and a retry duplicated the write.
    "control-character": ("/taches/\nrt31337", ["control_character"], False),
    "internal": ("/dossiers/{dossier}?tab=temps&page=2", [], True),
}


@pytest.fixture
def chain_ahead(monkeypatch):
    """A Montréal day frozen BEFORE the bench's chain (2026-10-15, -22):
    « Cette occurrence et les suivantes » then deletes it — the save this
    table means. Left on the real clock, the chain is past, nothing is
    deleted, and since the refusals lot that outcome lands on the hearing
    itself (« Aucune occurrence à venir à supprimer. »), not where a save
    lands. Frozen, never a widened offset (CLAUDE.md, Rule 5)."""
    monkeypatch.setattr(hearings_routes, "today_mtl",
                        lambda: date(2026, 10, 1))


def _post_save(db, client, save: Save, ids: dict, return_to: str,
               headers: Optional[dict] = None):
    """POST the save on a fresh record: (response, the ids of the records
    it was about — the one it saved or deleted, the one it created)."""
    record_id = save.fresh(db, ids)
    before = set(db.peek_collection(save.collection))
    resp = client.post(save.url(record_id),
                       data={**save.form(ids), "return_to": return_to},
                       headers=headers or {})
    created = set(db.peek_collection(save.collection)) - before
    return resp, created | ({record_id} - {""}), created


# The banner a landing may carry is not its target: how it is appended
# (routes/_helpers.with_query) is tests/test_post_refusals.py's.
_BANNER_KEYS = frozenset({"message", "erreur"})


def _target(save: Save, location: str, own_ids: set) -> tuple:
    """Where the landing sends the lawyer, the ids of the records this one
    POST was about masked (each variant saves its own fresh record): the
    path and its query (banner aside) — for the note pages, the path and
    the return_to it threads on."""
    parts = urlsplit(location)
    path = parts.path
    for own_id in own_ids:
        path = path.replace(own_id, "<own>")
    query = parse_qs(parts.query, keep_blank_values=True)
    if save.threads:
        return path, query.get("return_to", [""])[0]
    return path, {k: v for k, v in query.items() if k not in _BANNER_KEYS}


@pytest.mark.usefixtures("chain_ahead")
@pytest.mark.parametrize("variant", list(_VARIANTS), ids=list(_VARIANTS))
@pytest.mark.parametrize("save", _SAVES, ids=_SAVE_IDS)
def test_a_save_lands_on_the_doors_answer(
        caplog, db, client, ids, save, variant):
    """The redirect follows the value validated where it was read: a
    blank, crafted or control-character target lands where a save
    without one lands (and only a refused one logs, once); an internal
    one is honoured. Never a 500 — the record is written exactly once."""
    value, logged, kept = _VARIANTS[variant]
    value = value.format(**ids)
    blank_resp, blank_own, _ = _post_save(db, client, save, ids, "")
    assert blank_resp.status_code == 302
    (resp, own, created), reasons = _logged(
        caplog, lambda: _post_save(db, client, save, ids, value))
    assert resp.status_code == 302
    assert reasons == logged
    if save.id.endswith("_create"):
        assert len(created) == 1
    landing = _target(save, resp.headers["Location"], own)
    baseline = _target(save, blank_resp.headers["Location"], blank_own)
    if not kept:
        assert landing == baseline
    elif save.threads:
        assert landing == (baseline[0], value)
    else:
        assert landing == _target(save, value, set())


# The budget form is a plain full-page POST: its handler sends no
# HX-Redirect at all.
_HTMX_SAVES = [s for s in _SAVES if s.id != "budget_create"]


@pytest.mark.usefixtures("chain_ahead")
@pytest.mark.parametrize("save", _HTMX_SAVES, ids=[s.id for s in _HTMX_SAVES])
def test_htmx_is_sent_where_the_door_says_never_to_a_crafted_target(
        caplog, db, client, ids, save):
    """Only the HX-Redirect TARGET is asserted: its status and Location
    shape belong to the htmx convention, not to this guard."""
    blank_resp, blank_own, _ = _post_save(db, client, save, ids, "")
    (resp, own, _created), reasons = _logged(caplog, lambda: _post_save(
        db, client, save, ids, "https://evil.example/rt31337",
        headers={"HX-Request": "true"}))
    assert reasons == ["not_internal_path"]
    hx = resp.headers.get("HX-Redirect", "")
    assert "evil" not in hx
    assert (_target(save, hx, own)
            == _target(save, blank_resp.headers["Location"], blank_own))


# ── One crafted value is one line, on every branch ───────────────────


def test_a_refused_note_deletion_logs_one_line_for_one_crafted_return_to(
        caplog, db, client, ids):
    """The théorie de la cause refuses a delete racing an edit; the refusal
    goes back to the note. It used to validate the SAME value twice (once
    for the list, once for the note's « Retour »): two lines for one."""
    nid = "0c7e2f1a-2b3c-4d5e-8f60-718293a4b5c6"
    analyse = {
        **note_model._default_doc(), "id": nid, "dossier_id": ids["dossier"],
        "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. Lavoie",
        "title": note_model.ANALYSE_TITLE,
        "content": note_model._ANALYSE_SEED, "category": "stratégie",
        "dateless": True, "is_analyse": True, "vjournal_uid": "uid-analyse",
        "created_at": DT, "updated_at": DT, "etag": "e0",
    }
    db.seed(f"notes/{nid}", analyse)

    def rival(info):
        if ("delete", f"notes/{nid}") in info.ops:
            remove()
            db.external_write(f"notes/{nid}", {
                **analyse, "content": "Édité ailleurs.", "etag": "e-rival"})

    remove = db.add_commit_hook(rival)
    resp, reasons = _logged(caplog, lambda: client.post(
        f"/notes/{nid}/delete",
        data={"return_to": "https://evil.example/rt31337"}))
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert urlsplit(location).path == f"/notes/{nid}"
    assert parse_qs(urlsplit(location).query) == {
        "suppression_erreur": ["modifiee"]}
    assert reasons == ["not_internal_path"]
    assert db.peek(f"notes/{nid}") is not None


# ── The login's « next » ─────────────────────────────────────────────


@pytest.fixture
def login(app, monkeypatch):
    """The token check succeeds: only the ``next`` it answers is tested.

    The route's rate limit is off: the shared limiter keeps the state the
    last ``init_app`` of the session gave it, so these posts could
    otherwise meet another module's « 5 per minute »."""
    monkeypatch.setattr(auth_routes, "verify_and_create_session",
                        lambda token: (True, ""))
    monkeypatch.setattr(limiter, "enabled", False)
    return app.test_client()


@pytest.mark.parametrize("query, redirect, reasons", [
    ("", "/", []),
    ("?next=", "/", []),
    ("?next=/dossiers/%3Fstatut%3Dactif", "/dossiers/?statut=actif", []),
    ("?next=https://evil.example/rt31337", "/", ["not_internal_path"]),
    ("?next=//evil.example/rt31337", "/", ["not_internal_path"]),
    ("?next=/%09/evil.example", "/", ["scheme_or_netloc_present"]),
    ("?next=/dossiers/%0Art31337", "/", ["control_character"]),
    # Stripped, as everywhere else: the hand-written check it replaced
    # neither stripped a padded path nor caught urlsplit's ValueError on an
    # unclosed bracket — a 500 after the session had been created.
    ("?next=%20/dossiers/%20", "/dossiers/", []),
    ("?next=%20//[x", "/", ["not_internal_path"]),
], ids=["absent", "blank", "internal", "https", "protocol-relative",
        "tab-smuggled-host", "control-character", "padded-internal",
        "padded-unparsable-host"])
def test_the_login_next_goes_through_the_door(
        caplog, login, query, redirect, reasons):
    """The login page posts no ``next`` today; whatever reaches it is the
    door's to judge — the dashboard otherwise, one line for a refusal."""
    resp, logged = _logged(caplog, lambda: login.post(
        f"/auth/verify-token{query}", data={"id_token": "jeton"}))
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "redirect": redirect}
    assert logged == reasons
