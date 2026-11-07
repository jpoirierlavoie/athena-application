r"""Every class a page can render exists in the compiled stylesheet.

Change Impact item 6 (CLAUDE.md): « a class missing from the compiled CSS
silently does not apply ». The per-feature tests each check the classes of
one block; none reads the shells (base.html's body and owl, the login page,
the portal's base) or a class only a theme needs (``bg-canvas``,
``bg-paper``, ``scheme-light``, ``dark:invert``, ``dark:ring-*``). This sweep
reads every template of both services and every Python module of Tailwind's
scan set, so a forgotten recompile fails here, whatever the page.

What counts as a class, per template file (Jinja comments dropped first):

* the text of every ``class="…"`` attribute (whitespace allowed around the
  ``=``) and of Alpine's six ``x-transition:enter…`` / ``leave…`` class
  attributes, Jinja removed: the text of each ``{% if %}`` / ``{% elif %}``
  / ``{% else %}`` branch stays, since its tokens are classes too;
* the string values of every ``{{ … }}`` in such an attribute, read with
  Jinja's own parser: ternary branches, ``and`` / ``or`` operands, ``~``
  pieces, ``|default``, ``.get()`` defaults, ``[k]`` and ``.k`` lookups,
  ``.split()`` items, ``loop.cycle()`` arguments, and what the same file
  binds a name to: a ``{% set %}`` and its block form, a ``{% with %}``, a
  ``namespace()`` attribute, a ``{% for %}`` over a literal list or tuple
  (item k of each row binds target k) — never a comparison operand
  (``level == 'over'``), a dict key or a lookup key;
* the string literals of every Alpine ``:class`` / ``x-bind:class``
  expression, and the bare keys of an object one (``{ hidden: !open }``),
  except a comparison operand (``t.type === 'error'``) or a call argument
  (``includes('…')``); Jinja inside a literal is read as above;
* what JavaScript writes into ``.className`` or passes to
  ``.classList.add/remove/toggle/replace`` or ``.setAttribute('class', …)``,
  in a ``<script>`` body (the portal builds rows in JS) or in an attribute
  value (an Alpine ``@click``), never read past the end of either;
* the ``classes`` argument of every ``ms()`` icon.

Per Python module of routes/ and models/ (Tailwind's other @source lines):
every token of a ``class="…"`` in an HTML fragment string, and the
palette-shaped tokens (``bg-blue-100``, ``hover:text-red-700``) of every
other string constant (``_KYC_BADGES``, ``STEP_STATUS_COLORS``), whose
other words may be prose.

No template token is filtered out as « not a class »: Jinja and Alpine are
parsed, never split on spaces, so their syntax never becomes a token, and
every token that does not compile is a finding. A class assembled at runtime
(``'text-' ~ tone``) surfaces as its pieces, which do not compile — rightly,
since Tailwind never compiles it either.

What the sweep knows it cannot read, it reports: an unquoted class
attribute, a template literal, a ``{{ }}`` it cannot isolate, and a
``{{ }}`` that prints nothing although it reads a name the same file binds
to a literal (``m.values()|first``). Out of reach, and not reported: a
value computed at run time (what a route passes, a loop variable over its
data, an f-string placeholder), a macro parameter, another file's
``{% set %}``, an Alpine ``x-data`` property that a ``:class`` names
(``:class="tone"``), a class JavaScript writes any other way, and in Python
a class outside a fragment that is not palette-shaped. A page that never
loads the artifact (``NOT_STYLED_BY_THE_ARTIFACT``) is not swept.

A class is compiled when a selector of the artifact names it: ``.`` + the
class escaped as Tailwind writes it (CSS.escape) + a selector boundary, so
``bg-gray-5`` never passes on ``.bg-gray-50`` while variants pass on their
suffixes (``.hover\:bg-gray-50:hover``, ``.md\:flex`` inside ``@media``,
``.dark\:invert:where(…)``). Only rule preludes are searched: a class named
in a comment or a declaration value does not exist. A ``dark:`` class
counts only in a prelude that a ``[data-theme=…]`` condition scopes, where
the dark variant compiles it, and any other class only outside one: a
hand-written dark-only rule (``.bg-white:is(.shadow-lg,.shadow-xl)``) never
stands in for a missing utility.

The scanners are linear ``str.find`` loops; there is no regex (CWE-1333).
"""

import dataclasses
import os
import pathlib
import string
import sys
from collections import defaultdict

import pytest
from jinja2 import Environment, TemplateSyntaxError, nodes

ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, os.fspath(ATHENA))

from tests import _css  # noqa: E402

TEMPLATE_ROOTS = (ATHENA / "templates", ATHENA / "client" / "templates")
# The rest of Tailwind's scan set (app.input.css's @source lines), written
# out like TEMPLATE_ROOTS, never read from those lines: the sweep must not
# shrink with the scan set it checks.
PYTHON_ROOTS = (ATHENA / "routes", ATHENA / "models")
VENDOR = ATHENA / "static" / "vendor"

# Hooks that no rule styles, so the artifact never holds them. The list can
# only shrink: an entry the templates stop using, or that the artifact starts
# compiling, fails test_the_allowlist_holds_only_live_uncompiled_hooks.
ALLOWED_UNCOMPILED = {
    "notranslate": "<html>'s opt-out marker for translation extensions",
    "gabarit-ctx": "hx-include selector of the gabarit dialog's context inputs",
    "gabarit-slot": "hx-include selector of the gabarit dialog's field inputs",
}

# Pages that never load the artifact, so the sweep leaves them out: their
# classes are their own <style>'s, and a Tailwind class there could never
# apply. The list can only shrink: an entry that is gone, or that may load
# the artifact, fails test_pages_left_out_never_load_the_artifact.
NOT_STYLED_BY_THE_ARTIFACT = {
    "templates/offline.html": "precached by sw.js; styled by its own <style>",
}

# Where a class comes from (the module docstring describes each).
SOURCES = ("class", "class {{ }}", ":class", "script", "ms()", "python")

# Classes only the dark theme uses: no per-feature test reads them.
THEME_CLASSES = ("bg-canvas", "bg-paper", "scheme-light",
                 "dark:invert", "dark:ring-1", "dark:ring-gray-300")

# Parses only, never renders; autoescape=True as Flask renders a .html
# template (Bandit B701, CodeQL py/jinja2/autoescape-false).
_JINJA = Environment(autoescape=True)
# Attributes whose value is a class list, text plus Jinja: HTML's own, and
# the classes Alpine's x-transition applies at each stage.
_CLASS_TEXT_ATTRIBUTES = ("class",) + tuple(
    "x-transition:" + stage for stage in (
        "enter", "enter-start", "enter-end", "leave", "leave-start", "leave-end"))
# Attributes whose value is an Alpine expression that yields classes.
_CLASS_EXPRESSION_ATTRIBUTES = (":class", "x-bind:class")
_ATTRIBUTE_NAME_CHARS = ":@._-"
_COMPARISONS = frozenset({"===", "!==", "==", "!=", "<", ">", "<=", ">="})
_CLASSLIST_WRITERS = ("add", "remove", "toggle", "replace")
# HTML's and CSS's whitespace: space, tab, line feed, form feed, return.
_WHITESPACE = " " + chr(9) + chr(10) + chr(12) + chr(13)
_SELECTOR_BOUNDARY = frozenset("{:,)[.>~+" + _WHITESPACE)
# What scopes a prelude to a theme: the dark variant (app.input.css's
# @custom-variant dark) compiles every dark: class under it.
_THEME_SCOPE = "[data-theme="
# Tailwind's colour utilities and shades: a Python string that is not an
# HTML fragment gives only tokens of their shape, never a word of prose.
_PALETTE_UTILITIES = frozenset({
    "bg", "text", "border", "ring", "divide", "outline", "from", "via", "to",
    "fill", "stroke", "accent", "caret", "decoration", "placeholder", "shadow"})
_PALETTE_SHADES = frozenset(["50", "950"] + [str(n) for n in range(100, 1000, 100)])
# Lower-cases ASCII only, so offsets hold whatever else the text holds.
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


# ── The compiled stylesheet ────────────────────────────────────────────────


def _artifact(vendor: pathlib.Path) -> pathlib.Path:
    """The one compiled stylesheet: a stale copy beside it would let this
    sweep read the wrong file."""
    found = sorted(vendor.glob("app.*.css"))
    assert len(found) == 1, f"expected one app.*.css, found {[p.name for p in found]}"
    return found[0]


def selector_text(css: str) -> list[str]:
    """Every rule prelude of a stylesheet (the selectors and at-rule
    conditions before each ``{``), in order, each closed by its ``{``.
    Comments, strings and declarations drop out."""
    kept, i = [], 0
    while True:
        start = css.find("/*", i)
        if start < 0:
            kept.append(css[i:])
            break
        kept.append(css[i:start])
        end = css.find("*/", start + 2)
        i = len(css) if end < 0 else end + 2
    css = "".join(kept)
    preludes, start, quote, i = [], 0, "", 0
    while i < len(css):
        char = css[i]
        if char == "\\":
            i += 2
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "{":
            preludes.append(css[start:i + 1])
            start = i + 1
        elif char in ";}":
            start = i + 1
        i += 1
    return preludes


@dataclasses.dataclass(frozen=True)
class Selectors:
    """A stylesheet's rule preludes, joined per scope: ``themed``, those a
    ``[data-theme=…]`` condition scopes; ``plain``, all the others."""

    plain: str
    themed: str


def compiled_selectors(css: str) -> Selectors:
    """The preludes of a stylesheet, split by scope (whitespace aside, so a
    formatted ``[ data-theme = … ]`` is scoped too)."""
    scoped = {False: [], True: []}
    for prelude in selector_text(css):
        scoped[_THEME_SCOPE in "".join(prelude.split())].append(prelude)
    return Selectors(plain="".join(scoped[False]), themed="".join(scoped[True]))


def css_escape(token: str) -> str:
    r"""The class as a selector spells it: CSS.escape, which Tailwind ports
    and Lightning CSS keeps. A backslash before every ASCII character outside
    [A-Za-z0-9_-] (``md:flex`` → ``md\:flex``), a code point for a leading
    digit (``2xl:flex`` → ``\32 xl\:flex``)."""
    out = []
    for i, char in enumerate(token):
        code = ord(char)
        leading_digit = "0" <= char <= "9" and (i == 0 or (i == 1 and token[0] == "-"))
        if code < 0x20 or code == 0x7F or leading_digit:
            out.append(f"\\{code:x} ")
        elif token == "-":
            out.append("\\-")
        elif code >= 0x80 or char.isalnum() or char in "-_":
            out.append(char)
        else:
            out.append("\\" + char)
    return "".join(out)


def is_compiled(token: str, selectors: Selectors) -> bool:
    """A prelude of the token's scope names the class: ``.`` + its escaped
    spelling, starting a class (no backslash before the dot) and ending at a
    selector boundary, never inside a longer class it prefixes. A ``dark:``
    class is looked for in the themed preludes, any other in the plain
    ones."""
    text = selectors.themed if "dark" in _css.split_variants(token)[0] else selectors.plain
    needle = "." + css_escape(token)
    i = text.find(needle)
    while i >= 0:
        end = i + len(needle)
        if (i == 0 or text[i - 1] != "\\") and (
                end == len(text) or text[end] in _SELECTOR_BOUNDARY):
            return True
        i = text.find(needle, i + 1)
    return False


# ── Python ─────────────────────────────────────────────────────────────────


def is_palette_shaped(token: str) -> bool:
    """A colour utility of Tailwind's palette, whatever its variants and
    opacity: ``bg-blue-100``, ``hover:text-red-700``, ``border-x-gray-200``;
    not ``bg-white/80`` (no shade)."""
    utility = _css.split_variants(token)[1].partition("/")[0]
    parts = utility.split("-")
    return (len(parts) >= 3 and parts[0] in _PALETTE_UTILITIES
            and parts[-1] in _PALETTE_SHADES)


def python_classes(source: str) -> list[str]:
    """The classes a Python module writes, once each, in order: every token
    of a ``class="…"`` in an HTML fragment string, and the palette-shaped
    tokens of every other string constant."""
    found = []
    for constant in _css.python_strings(source):
        if "class=" in constant:
            found += [token for value in _css.fragment_attribute_values(constant)
                      for token in value.split()]
        else:
            found += [token for token in constant.split() if is_palette_shaped(token)]
    return list(dict.fromkeys(found))


# ── Jinja ──────────────────────────────────────────────────────────────────


def _without_jinja_comments(text: str) -> str:
    """The text with every ``{# … #}`` blanked; newlines stay, so line
    numbers hold."""
    kept, i = [], 0
    while True:
        start = text.find("{#", i)
        if start < 0:
            kept.append(text[i:])
            return "".join(kept)
        end = text.find("#}", start + 2)
        end = len(text) if end < 0 else end + 2
        kept.append(text[i:start])
        kept.append("\n" * text.count("\n", start, end))
        i = end


def _after_jinja(text: str, i: int) -> int:
    """The index after the ``{{ … }}`` or ``{% … %}`` opening at i, or i
    when none opens there."""
    if text.startswith("{{", i):
        closer = "}}"
    elif text.startswith("{%", i):
        closer = "%}"
    else:
        return i
    end = text.find(closer, i + 2)
    return len(text) if end < 0 else end + 2


class _Seq(tuple):
    """A tuple or list value: per item, the values that item can take."""


class _Map(tuple):
    """A dict value: every value its entries can take (keys are lookups)."""


def _values(node, sets: dict, seen: frozenset = frozenset()) -> list:
    """The values a Jinja expression can take, as far as a static read can
    tell. A comparison or a test yields a boolean, never a class; a name
    resolves through what the file binds it to, and any other name,
    attribute or call is unknowable here and yields nothing."""
    if isinstance(node, nodes.Const):
        return [node.value] if isinstance(node.value, str) else []
    if isinstance(node, (nodes.Tuple, nodes.List)):
        return [_Seq(_values(item, sets, seen) for item in node.items)]
    if isinstance(node, nodes.Dict):
        return [_Map(v for pair in node.items for v in _values(pair.value, sets, seen))]
    if isinstance(node, nodes.CondExpr):
        other = [] if node.expr2 is None else _values(node.expr2, sets, seen)
        return _values(node.expr1, sets, seen) + other
    if isinstance(node, (nodes.And, nodes.Or, nodes.Add, nodes.Mod)):
        return _values(node.left, sets, seen) + _values(node.right, sets, seen)
    if isinstance(node, nodes.Concat):
        return [v for part in node.nodes for v in _values(part, sets, seen)]
    if isinstance(node, nodes.Name):
        if node.name in seen:
            return []
        return [v for assigned in sets.get(node.name, ())
                for v in _values(assigned, sets, seen | {node.name})]
    if isinstance(node, nodes.Filter):
        found = [] if node.node is None else _values(node.node, sets, seen)
        if node.name in ("default", "d"):
            found += [v for arg in node.args[:1] for v in _values(arg, sets, seen)]
        return found
    if isinstance(node, nodes.Getitem):
        index = node.arg.value if isinstance(node.arg, nodes.Const) else None
        found = []
        for value in _values(node.node, sets, seen):
            if isinstance(value, _Map):
                found += value
            elif isinstance(value, _Seq) and isinstance(index, int):
                found += value[index] if -len(value) <= index < len(value) else []
            elif isinstance(value, _Seq):
                found += [v for item in value for v in item]
        return found
    if isinstance(node, nodes.Getattr):
        return [v for m in _values(node.node, sets, seen) if isinstance(m, _Map) for v in m]
    if (isinstance(node, nodes.Call) and isinstance(node.node, nodes.Name)
            and node.node.name == "namespace"):
        return [_Map(v for kw in node.kwargs for v in _values(kw.value, sets, seen))]
    if isinstance(node, nodes.Call) and isinstance(node.node, nodes.Getattr):
        method, owner = node.node.attr, node.node.node
        if method == "get":
            found = [v for m in _values(owner, sets, seen) if isinstance(m, _Map) for v in m]
            return found + [v for arg in node.args[1:2] for v in _values(arg, sets, seen)]
        if method == "split":
            return [_Seq([part] for part in s.split())
                    for s in _values(owner, sets, seen) if isinstance(s, str)]
        if method == "cycle" and isinstance(owner, nodes.Name) and owner.name == "loop":
            return [v for arg in node.args for v in _values(arg, sets, seen)]
    return []


def _strings(values):
    """Every string inside some values, however nested."""
    for value in values:
        if isinstance(value, str):
            yield value
        elif isinstance(value, _Seq):
            for item in value:
                yield from _strings(item)
        else:
            yield from _strings(value)


def _bindings(tree) -> dict:
    """{name: [expression]} of what a template binds each name to: a
    ``{% set %}`` (its block form binds the text and ``{{ }}`` of its body),
    a ``{% with %}``, a ``namespace()`` attribute, and a ``{% for %}`` over a
    literal list or tuple, whose item k of each row binds target k. A name
    bound twice keeps both: a static read cannot tell which is in scope."""
    sets = defaultdict(list)
    for assign in tree.find_all(nodes.Assign):
        _bind(sets, assign.target, assign.node)
    for block in tree.find_all(nodes.AssignBlock):
        for output in block.find_all(nodes.Output):
            for part in output.nodes:
                _bind(sets, block.target, nodes.Const(part.data)
                      if isinstance(part, nodes.TemplateData) else part)
    for scope in tree.find_all(nodes.With):
        for target, value in zip(scope.targets, scope.values):
            _bind(sets, target, value)
    for loop in tree.find_all(nodes.For):
        if isinstance(loop.iter, (nodes.List, nodes.Tuple)):
            for row in loop.iter.items:
                _bind(sets, loop.target, row)
    return sets


def _bind(sets: dict, target, value) -> None:
    """Bind a name, a namespace attribute, or a tuple item by item."""
    if isinstance(target, nodes.Name):
        sets[target.name].append(value)
    elif isinstance(target, nodes.NSRef):
        sets[target.name].append(nodes.Dict([nodes.Pair(nodes.Const(target.attr), value)]))
    elif isinstance(target, nodes.Tuple) and isinstance(value, (nodes.Tuple, nodes.List)):
        for item, part in zip(target.items, value.items):
            _bind(sets, item, part)


def _is_literal(node) -> bool:
    """A constant, or a list, tuple or dict of literals."""
    if isinstance(node, nodes.Const):
        return True
    if isinstance(node, (nodes.Tuple, nodes.List)):
        return all(_is_literal(item) for item in node.items)
    if isinstance(node, nodes.Dict):
        return all(_is_literal(pair.key) and _is_literal(pair.value) for pair in node.items)
    return False


def _literal_names(sets: dict) -> frozenset:
    """The names a file binds to a literal that holds a class-like value:
    a ``{{ }}`` that reads one and prints nothing went unfollowed."""
    return frozenset(name for name, values in sets.items() if any(
        _is_literal(value) and any(s.split() for s in _strings(_values(value, {})))
        for value in values))


def _expression_classes(expression: str, sets: dict) -> tuple[list[str], set]:
    """The classes a ``{{ … }}`` (delimiters included) can print, and the
    names it reads."""
    tree = _JINJA.parse(expression)
    tokens = [token for output in tree.find_all(nodes.Output)
              for node in output.nodes if not isinstance(node, nodes.TemplateData)
              for string in _strings(_values(node, sets)) for token in string.split()]
    return tokens, {name.name for name in tree.find_all(nodes.Name)}


def _class_text(text: str, sets: dict, literal_names=frozenset()) -> tuple[list, list]:
    """``(tokens, lost)`` of a class text. ``tokens``: (token, came from a
    ``{{ }}``) for the literal text of every Jinja branch and what every
    ``{{ }}`` can print, ``{% %}`` tags dropped. ``lost``: every ``{{ }}``
    that prints nothing although it reads one of ``literal_names``."""
    literal, found, lost, start, i = [], [], [], 0, 0
    while i < len(text):
        after = _after_jinja(text, i)
        if after == i:
            i += 1
            continue
        literal.append(text[start:i])
        if text.startswith("{{", i):
            tokens, names = _expression_classes(text[i:after], sets)
            found += [(token, True) for token in tokens]
            if not tokens and names & literal_names:
                lost.append(text[i:after])
        i = start = after
    literal.append(text[start:])
    return [(t, False) for t in " ".join(literal).split()] + found, lost


# ── HTML and JavaScript ────────────────────────────────────────────────────


def _attributes(text: str, names=None):
    """(offset, name, value offset, value) of every ``name="value"`` or
    ``name='value'`` a template writes (an HTML or Alpine attribute, or a
    Jinja keyword argument), whitespace allowed around the ``=``; the value
    is None when unquoted, and Jinja in it may hold either quote. Given
    ``names``, only those, and only their values are skipped: no other
    value can hide one."""
    n = len(text)
    i = text.find("=")
    while i >= 0:
        resume, start = i + 1, i
        while start and text[start - 1] in _WHITESPACE:
            start -= 1
        stop = start
        while start and (text[start - 1].isalnum()
                         or text[start - 1] in _ATTRIBUTE_NAME_CHARS):
            start -= 1
        name = text[start:stop]
        if name and (names is None or name in names):
            j = resume
            while j < n and text[j] in _WHITESPACE:
                j += 1
            quote = text[j:j + 1]
            if quote in ('"', "'"):
                k = j + 1
                while k < n and text[k] != quote:
                    skipped = _after_jinja(text, k)
                    k = skipped if skipped != k else k + 1
                yield start, name, j + 1, text[j + 1:k]
                resume = k + 1
            else:
                yield start, name, j, None
        i = text.find("=", resume)


def _script_bodies(text: str) -> list[tuple[int, int]]:
    """(start, end) of the body of every ``<script>`` element, whatever the
    case of its tags."""
    lowered = text.translate(_ASCII_LOWER)
    found, i = [], lowered.find("<script")
    while i >= 0:
        opened = lowered.find(">", i)
        if opened < 0:
            break
        end = lowered.find("</script", opened)
        end = len(text) if end < 0 else end
        found.append((opened + 1, end))
        i = lowered.find("<script", end)
    return found


def _js_chunks(text: str):
    """(offset, code) of every piece of JavaScript in a template: each
    ``<script>`` body, then each quoted attribute value (an Alpine
    ``@click``, ``x-init``…), so no statement is read past its piece. A
    script's own ``name = "…"`` counts as an attribute too: its string is
    read as code once more."""
    for start, end in _script_bodies(text):
        yield start, text[start:end]
    for _, _, start, value in _attributes(text):
        if value is not None:
            yield start, value


def _js_tokens(code: str, start: int = 0):
    """(kind, text) tokens of JavaScript from ``start``: "str" (a quoted
    literal's content), "template" (a backtick literal's), "jinja" (a
    ``{{ }}`` or ``{% %}`` in code), "name", "op". Whitespace and comments
    drop out; Jinja inside a literal stays part of it."""
    i, n = start, len(code)
    while i < n:
        char = code[i]
        after = _after_jinja(code, i)
        if after != i:
            yield "jinja", code[i:after]
            i = after
        elif char in "'\"`":
            j = i + 1
            while j < n and code[j] != char:
                skipped = _after_jinja(code, j)
                j = skipped if skipped != j else j + (2 if code[j] == "\\" else 1)
            yield ("template" if char == "`" else "str"), code[i + 1:j]
            i = j + 1
        elif code.startswith("//", i):
            end = code.find("\n", i)
            i = n if end < 0 else end
        elif code.startswith("/*", i):
            end = code.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif char.isspace():
            i += 1
        elif char.isalnum() or char in "_$":
            j = i + 1
            while j < n and (code[j].isalnum() or code[j] in "_$"):
                j += 1
            yield "name", code[i:j]
            i = j
        elif char in "=!<>":
            j = i + 1
            while j < n and code[j] in "=!<>":
                j += 1
            yield "op", code[i:j]
            i = j
        else:
            yield "op", char
            i += 1


def _class_literals(tokens: list) -> list[tuple[str, str]]:
    """(kind, text) of the class texts of a JavaScript expression: its
    literals and Jinja insertions, except a comparison operand or a call
    argument, plus the bare keys of an object."""
    found, brackets = [], []
    for k, (kind, text) in enumerate(tokens):
        before = tokens[k - 1] if k else ("op", "")
        after = tokens[k + 1] if k + 1 < len(tokens) else ("op", "")
        if kind == "op" and text in ("(", "[", "{"):
            call = text == "(" and (before[0] in ("name", "jinja") or before[1] in (")", "]"))
            brackets.append("call" if call else text)
        elif kind == "op" and text in (")", "]", "}"):
            if brackets:
                brackets.pop()
        elif kind in ("str", "template", "jinja"):
            compared = (before[0] == "op" and before[1] in _COMPARISONS) or (
                after[0] == "op" and after[1] in _COMPARISONS)
            if not compared and brackets[-1:] != ["call"]:
                found.append((kind, text))
        elif (kind == "name" and brackets[-1:] == ["{"]
              and before in (("op", "{"), ("op", ",")) and after == ("op", ":")):
            found.append(("str", text))
    return found


def _js_until(code: str, start: int, stop: str):
    """The tokens from ``start`` up to the first ``stop`` operator outside
    brackets, or to a bracket closing one that opened before ``start``; split
    into top-level arguments when ``stop`` is ")"."""
    groups, current, depth = [], [], 0
    for kind, text in _js_tokens(code, start):
        if kind == "op" and depth == 0 and (text == stop or text in (")", "]", "}")):
            break
        if kind == "op" and text == "," and depth == 0 and stop == ")":
            groups.append(current)
            current = []
            continue
        if kind == "op" and text in ("(", "[", "{"):
            depth += 1
        elif kind == "op" and text in (")", "]", "}"):
            depth -= 1
        current.append((kind, text))
    return groups + [current]


def _script_class_literals(code: str):
    """(offset, kind, text) of what a piece of JavaScript writes into
    ``.className`` (a statement ends at its semicolon, or with the piece),
    passes to a ``.classList`` writer (``toggle`` takes its first argument
    only) or sets as its ``class`` attribute."""
    i = code.find(".className")
    while i >= 0:
        j = i + len(".className")
        while code[j:j + 1] in (" ", "\t"):
            j += 1
        if code.startswith("+=", j):
            j += 1
        if code.startswith("=", j) and not code.startswith("==", j):
            for kind, literal in _class_literals(_js_until(code, j + 1, ";")[0]):
                yield i, kind, literal
        i = code.find(".className", i + 1)
    i = code.find(".classList.")
    while i >= 0:
        j = k = i + len(".classList.")
        while k < len(code) and (code[k].isalnum() or code[k] == "_"):
            k += 1
        method = code[j:k]
        while code[k:k + 1] in (" ", "\t"):
            k += 1
        if method in _CLASSLIST_WRITERS and code.startswith("(", k):
            arguments = _js_until(code, k + 1, ")")
            for argument in arguments[:1] if method == "toggle" else arguments:
                for kind, literal in _class_literals(argument):
                    yield i, kind, literal
        i = code.find(".classList.", i + 1)
    i = code.find(".setAttribute(")
    while i >= 0:
        arguments = _js_until(code, i + len(".setAttribute("), ")")
        if len(arguments) > 1 and arguments[0] == [("str", "class")]:
            for kind, literal in _class_literals(arguments[1]):
                yield i, kind, literal
        i = code.find(".setAttribute(", i + 1)


# ── The sweep ──────────────────────────────────────────────────────────────


@dataclasses.dataclass
class Sweep:
    """``where``: each class and its uses ("path:line" in a template, the
    path of a Python module); ``sources``: each of SOURCES and the classes
    it gave; ``problems``: what the sweep knows it could not read."""

    where: dict = dataclasses.field(default_factory=lambda: defaultdict(list))
    sources: dict = dataclasses.field(default_factory=lambda: defaultdict(set))
    problems: list = dataclasses.field(default_factory=list)

    def add(self, tokens, source: str, location: str) -> None:
        for token in tokens:
            self.where[token].append(location)
            self.sources[source].add(token)


def sweep_files(files) -> Sweep:
    """Every class of the given (path, text) templates (.html) and Python
    modules (.py), with where each is used, which source gave it, and what
    could not be read."""
    sweep = Sweep()
    for name, text in files:
        if name.endswith(".html"):
            _sweep_template(sweep, name, text)
        elif name.endswith(".py"):
            sweep.add(python_classes(text), "python", name)
        else:
            raise ValueError(f"{name}: neither a template nor a Python module")
    return sweep


def _sweep_template(sweep: Sweep, name: str, raw: str) -> None:
    """Add one template's classes to the sweep."""
    tree = _JINJA.parse(raw, name=name)
    sets = _bindings(tree)
    literal_names = _literal_names(sets)
    text = _without_jinja_comments(raw)

    def read(class_text: str, where: str) -> list[tuple[str, bool]]:
        try:
            tokens, lost = _class_text(class_text, sets, literal_names)
        except TemplateSyntaxError as exc:
            sweep.problems.append(f"{where}: cannot isolate a {{{{ }}}} ({exc.message})")
            return []
        sweep.problems.extend(f"{where}: cannot follow {expression}" for expression in lost)
        return tokens

    def read_js(literals, where: str, origin: str, source: str) -> None:
        for kind, literal in literals:
            if kind == "template":
                sweep.problems.append(f"{where}: template literal in {origin}")
            else:
                sweep.add([token for token, _ in read(literal, where)], source, where)

    def at(offset: int) -> str:
        line = text.count("\n", 0, offset) + 1
        return f"{name}:{line}"

    for offset, attribute, _, value in _attributes(
            text, _CLASS_TEXT_ATTRIBUTES + _CLASS_EXPRESSION_ATTRIBUTES):
        where = at(offset)
        if value is None:
            sweep.problems.append(f"{where}: unquoted {attribute} attribute")
        elif attribute in _CLASS_TEXT_ATTRIBUTES:
            tokens = read(value, where)
            sweep.add([token for token, expression in tokens if not expression], "class", where)
            sweep.add([token for token, expression in tokens if expression], "class {{ }}", where)
        else:
            read_js(_class_literals(list(_js_tokens(value))), where, attribute, ":class")
    for start, code in _js_chunks(text):
        for offset, kind, literal in _script_class_literals(code):
            read_js([(kind, literal)], at(start + offset), "JavaScript", "script")
    for call in tree.find_all(nodes.Call):
        if isinstance(call.node, nodes.Name) and call.node.name == "ms":
            arguments = call.args[2:3] + [kw.value for kw in call.kwargs if kw.key == "classes"]
            sweep.add([token for argument in arguments
                       for string in _strings(_values(argument, sets))
                       for token in string.split()],
                      "ms()", f"{name}:{call.lineno}")


def uncompiled(sweep: Sweep, selectors: Selectors, allowed=ALLOWED_UNCOMPILED) -> dict:
    """{class: where it is used} for every class no selector of its scope
    names, the allowed hooks aside."""
    return {token: where for token, where in sorted(sweep.where.items())
            if token not in allowed and not is_compiled(token, selectors)}


def stale_allowlist(allowed, sweep: Sweep, selectors: Selectors) -> list[str]:
    """The allowed hooks that no template uses any more, or that the
    artifact now compiles."""
    return sorted(token for token in allowed
                  if token not in sweep.where or is_compiled(token, selectors))


def may_load_the_artifact(page: str) -> bool:
    """A template links the compiled stylesheet, or extends, includes or
    imports another template, which may."""
    layouts = (nodes.Extends, nodes.Include, nodes.Import, nodes.FromImport)
    return "vendor/app." in _without_jinja_comments(page) or any(
        True for _ in _JINJA.parse(page).find_all(layouts))


def _sources() -> list[tuple[str, str]]:
    """(path, text) of every template the artifact styles and every Python
    module of the scan set."""
    paths = [path for root in TEMPLATE_ROOTS for path in sorted(root.rglob("*.html"))]
    paths += [path for root in PYTHON_ROOTS for path in sorted(root.rglob("*.py"))]
    named = [(path.relative_to(ATHENA).as_posix(), path) for path in paths]
    return [(name, path.read_text(encoding="utf-8")) for name, path in named
            if name not in NOT_STYLED_BY_THE_ARTIFACT]


@pytest.fixture(scope="module")
def sweep() -> Sweep:
    return sweep_files(_sources())


@pytest.fixture(scope="module")
def selectors() -> Selectors:
    return compiled_selectors(_artifact(VENDOR).read_text(encoding="utf-8"))


# ── The sources against the artifact ───────────────────────────────────────


def test_every_class_a_page_can_render_is_compiled(sweep, selectors):
    """A class absent from the artifact silently does not apply: recompile
    (CLAUDE.md « Regenerating the stylesheet »)."""
    missing = uncompiled(sweep, selectors)
    assert not missing, (
        "absent from the compiled stylesheet (a dark: class counts only under"
        " [data-theme=…], any other only outside it): " + "; ".join(
            f"{token} ({', '.join(where[:3])})" for token, where in missing.items()))


def test_every_class_construct_is_readable(sweep):
    """What the sweep knows it cannot read is reported, never skipped."""
    assert sweep.problems == []


def test_the_allowlist_holds_only_live_uncompiled_hooks(sweep, selectors):
    assert stale_allowlist(ALLOWED_UNCOMPILED, sweep, selectors) == []
    # The check catches an entry no template uses and one the artifact
    # compiles.
    assert stale_allowlist({"notranslate", "never-used", "flex"}, sweep, selectors) == [
        "flex", "never-used"]


def test_the_sweep_reads_both_trees_and_every_source(sweep):
    """Non-vacuity: the whole tree, both services, the Python modules, every
    source of classes, and the theme's classes; never a page left out."""
    assert len(sweep.where) > 400, len(sweep.where)
    assert set(THEME_CLASSES) <= set(sweep.where), sorted(set(THEME_CLASSES) - set(sweep.where))
    assert [s for s in SOURCES if sweep.sources[s]] == list(SOURCES)
    files = {where.rsplit(":", 1)[0] for found in sweep.where.values() for where in found}
    assert {name.split("/", 1)[0] for name in files} == {
        root.relative_to(ATHENA).parts[0] for root in TEMPLATE_ROOTS + PYTHON_ROOTS}
    assert not files & set(NOT_STYLED_BY_THE_ARTIFACT)


def test_pages_left_out_never_load_the_artifact():
    for name in NOT_STYLED_BY_THE_ARTIFACT:
        path = ATHENA / name
        assert path.is_file(), f"{name} is gone: drop it from NOT_STYLED_BY_THE_ARTIFACT"
        assert not may_load_the_artifact(path.read_text(encoding="utf-8")), (
            f"{name} may load the artifact: drop it from NOT_STYLED_BY_THE_ARTIFACT")
    # The check catches a link to the artifact, the real one included, and a
    # template that may bring one.
    assert may_load_the_artifact((ATHENA / "templates" / "base.html").read_text(encoding="utf-8"))
    assert may_load_the_artifact(
        "<link rel=\"stylesheet\" href=\"{{ url_for('static', filename='vendor/app.0.css') }}\">")
    for layout in ('{% extends "base.html" %}', '{% include "x.html" %}',
                   '{% import "m.html" as m %}', '{% from "m.html" import ms %}'):
        assert may_load_the_artifact(layout), layout
    assert not may_load_the_artifact("<style>.container{}</style>{# vendor/app.x.css #}")


def test_there_is_exactly_one_compiled_stylesheet(tmp_path):
    assert _artifact(VENDOR).name.startswith("app.")
    with pytest.raises(AssertionError):
        _artifact(tmp_path)
    (tmp_path / "app.aaaaaaaa.css").write_text("", encoding="utf-8")
    (tmp_path / "app.bbbbbbbb.css").write_text("", encoding="utf-8")
    with pytest.raises(AssertionError):
        _artifact(tmp_path)


# ── The checkers, on synthetic input ───────────────────────────────────────


def test_css_escape_spells_classes_as_the_artifact_does():
    assert css_escape("text-gray-900") == "text-gray-900"
    assert css_escape("md:flex") == r"md\:flex"
    assert css_escape("bg-black/40") == r"bg-black\/40"
    assert css_escape("py-0.5") == r"py-0\.5"
    assert css_escape("max-h-[80vh]") == r"max-h-\[80vh\]"
    assert css_escape("bg-[#FAFAFA]") == r"bg-\[\#FAFAFA\]"
    assert css_escape("w-[50%]") == r"w-\[50\%\]"
    assert css_escape("2xl:flex") == r"\32 xl\:flex"
    assert css_escape("-mt-0.5") == r"-mt-0\.5"


def test_the_matcher_needs_the_whole_class_in_a_selector(selectors):
    spaces = (" ", chr(9), chr(10), chr(12), chr(13))
    # On the real artifact first: a prefix or a bare variant never passes.
    assert is_compiled("bg-gray-50", selectors) and not is_compiled("bg-gray-5", selectors)
    assert is_compiled("dark:invert", selectors) and not is_compiled("dark", selectors)
    synthetic = compiled_selectors(
        "/*! v4 .ghost */@layer utilities{"
        ".bg-gray-50{background-color:var(--color-gray-50)}"
        r".py-0\.5{padding-block:calc(var(--spacing) * .5)}"
        r".w-1\.x{width:1px}"
        r"@media (hover:hover){.hover\:bg-gray-50:hover{opacity:1}}"
        r"@media (min-width:48rem){.md\:flex{display:flex}}}"
        r"@media screen{.dark\:invert:where([data-theme=sombre],[data-theme=sombre] *)"
        "{--tw-invert:invert(100%)}}"
        r".group-hover\:opacity-100:is(:where(.group):hover *){opacity:1}"
        '@font-face{src:url("x.woff2")}.icon{background:url(sprite.svg)}'
        # A brace in a string opens no rule, after an escaped quote either.
        '.a{content:".ghost2{"}' r'.a{content:"\".ghost3{"}'
        # A formatted stylesheet: whitespace between a class and its brace.
        + "".join(f".ws{ord(space)}{space}{{color:red}}" for space in spaces))
    for present in ("bg-gray-50", "py-0.5", "hover:bg-gray-50", "md:flex", "dark:invert",
                    "group-hover:opacity-100", "group", "icon",
                    *(f"ws{ord(space)}" for space in spaces)):
        assert is_compiled(present, synthetic), present
    # Missing; a prefix of a longer class; a variant without its class; a
    # class after an escaped dot; one named in a comment, a string or a
    # declaration only.
    for absent in ("bg-gray-100", "bg-gray-5", "gray-50", "hover", "dark", "x",
                   "ghost", "woff2", "svg", "ghost2", "ghost3"):
        assert not is_compiled(absent, synthetic), absent


def test_a_dark_only_rule_never_stands_in_for_a_utility():
    """The floating-surface rule of app.input.css names bg-white, shadow-lg
    and shadow-xl under [data-theme=…]: each still needs its own rule."""
    synthetic = compiled_selectors(
        ".shadow-lg{--tw-shadow:0 10px 15px #0001}"
        r"@media screen{.bg-white:is(.shadow-lg,.shadow-xl):where([data-theme=sombre],"
        "[data-theme=sombre] *){outline:1px solid red}"
        # Formatted: the scope reads through the spaces.
        r".dark\:ring-1:where([ data-theme = sombre ]) {--tw-ring:1px}}"
        r".dark\:invert{--tw-invert:invert(100%)}")
    assert is_compiled("shadow-lg", synthetic) and is_compiled("dark:ring-1", synthetic)
    # Named by the dark-only rule alone; a dark: class outside any theme
    # scope would apply in light too.
    for absent in ("shadow-xl", "bg-white", "dark:invert"):
        assert not is_compiled(absent, synthetic), absent
    found = sweep_files([("floating.html", '<div class="bg-white shadow-lg shadow-xl"></div>')])
    assert uncompiled(found, synthetic) == {
        "bg-white": ["floating.html:1"], "shadow-xl": ["floating.html:1"]}


_SYNTHETIC = """<html class="notranslate">
<div class="a1 {% if x %}a2{% elif y %}a3{% else %}a4{% endif %} a5"></div>
<p class="{{ 'b1 b2' if level == 'over' else 'b3' }}"></p>
{% set tones = {'k1': 'c1 c2', 'k2': 'c3'} %}
<span class="{{ tones.get(kind, 'c4') }}"></span>
{% set pairs = {1: ('d1', 'Label one')} %}
<b class="{{ pairs.get(n, ('d2', ''))[0] }}"></b>
<i :class="t.type === 'error' ? 'e1' : (ok ? 'e2' : '')"></i>
<i x-bind:class="{ e3: open, 'e4 e5': busy }"></i>
<i :class="items.includes('lookup') ? 'e6' : ''"></i>
{{ ms('check', 20, 'f1 f2' if done else 'f3') }}
<script>
  el.className = 'g1 ' + (s === 'skip' ? 'g2' : 'g3');
  el.classList.toggle('g4', state === 'on');
  el.classList.add('g5', 'g6');
  el.classList.remove('g7'); el.classList.replace('g8', 'g9');
  el.className += ' g10';
  el.setAttribute('class', 'g11'); el.setAttribute('title', 'Libellé');
</script>
{# <div class="h1"></div> #}
<button @click="$el.className = 'g12'">Ok</button><p class="a6">Ok</p>
<div class = "a7" x-transition:enter="a8 {{ 'b4' }}" x-transition:enter-start="a9"
     x-transition:enter-end="a10" x-transition:leave="a11"
     x-transition:leave-start="a12" x-transition:leave-end="a13"></div>
{% set w = 's1 Libellé' %}<b class="{{ w.split(' ')[0] }}"></b>
<b class="{{ c|default('s2') }}"></b><b class="{{ 's3 ' ~ c }}"></b>
{% set m1 = {'k': 's4'} %}{% set m2 = {'k': 's5'} %}<b class="{{ m1.k }} {{ m2[c] }}"></b>
<b class="{{ a and 's6' or 's7' }}"></b>
{% for k, label, cls in [('k1', 'Libellé', 'u1'), ('k2', 'Autre', 'u2')] %}
<i class="{{ cls }}"></i>{% endfor %}
{% for one in ('u3',) %}<i class="{{ one }}"></i>{% endfor %}
{% with v = 'u4' %}<i class="{{ v }}"></i>{% endwith %}
{% set blk %}u5 {{ 'u6' }}{% endset %}<i class="{{ blk }}"></i>
{% set ns = namespace(cls='u7') %}{% set ns.more = 'u8' %}<i class="{{ ns.cls }}"></i>
{% for r in rows %}<tr class="{{ loop.cycle('u9', 'u10') }}"></tr>{% endfor %}
<SCRIPT>el.className = 'g13'</SCRIPT>
"""

_SYNTHETIC_PY = "\n".join((
    '"""Prose names bg-q-50, a shade only as a word."""',
    "M = {'a': 'bg-q-100 text-q-700', 'b': 'Libellé px-2 hover:bg-q-200/40'}",
    "F = '<p class=\"q1 q2\">bg-q-300 Libellé</p>'",
    "G = f'<li class=\"q3 {extra}\" data-class=\"nope\">'",
    "H = '<i :class=\"q4\">'",
))


def test_the_extractor_reads_every_source_and_nothing_else():
    found = sweep_files([("synthetic.html", _SYNTHETIC), ("routes/synthetic.py", _SYNTHETIC_PY)])
    assert found.problems == []
    assert dict(found.sources) == {
        "class": {"notranslate", "a1", "a2", "a3", "a4", "a5", "a6", "a7", "a8", "a9", "a10",
                  "a11", "a12", "a13"},
        "class {{ }}": {"b1", "b2", "b3", "b4", "c1", "c2", "c3", "c4", "d1", "d2",
                        "s1", "s2", "s3", "s4", "s5", "s6", "s7",
                        "u1", "u2", "u3", "u4", "u5", "u6", "u7", "u8", "u9", "u10"},
        ":class": {"e1", "e2", "e3", "e4", "e5", "e6"},
        "ms()": {"f1", "f2", "f3"},
        "script": {"g1", "g2", "g3", "g4", "g5", "g6", "g7", "g8", "g9", "g10", "g11", "g12",
                   "g13"},
        "python": {"bg-q-100", "text-q-700", "hover:bg-q-200/40", "q1", "q2", "q3"},
    }
    assert found.where["a3"] == ["synthetic.html:2"]
    assert found.where["e5"] == ["synthetic.html:9"]
    assert found.where["f3"] == ["synthetic.html:11"]
    assert found.where["g4"] == ["synthetic.html:14"]
    # A statement in an attribute value; an attribute on its own line.
    assert found.where["g12"] == ["synthetic.html:21"]
    assert found.where["a10"] == ["synthetic.html:23"]
    assert found.where["q1"] == ["routes/synthetic.py"]


def test_a_class_missing_from_the_artifact_is_reported():
    found = sweep_files([("synthetic.html", _SYNTHETIC), ("routes/synthetic.py", _SYNTHETIC_PY)])
    css = "".join(f".{css_escape(token)}{{color:red}}" for token in found.where
                  if token not in ("e5", "q2"))
    assert uncompiled(found, compiled_selectors(css)) == {
        "e5": ["synthetic.html:9"], "q2": ["routes/synthetic.py"]}


def test_unreadable_constructs_are_reported():
    found = sweep_files([("odd.html", (
        "<div class=bare></div>\n"
        "<i :class=\"`x-${tone}`\"></i>\n"
        "<p class=\"{{ '}}' }}\"></p>\n"
        "{% set m = {'k': ('x1', 'x2')} %}<p class=\"{{ m.values()|first }}\"></p>\n"
        "<div x-transition:leave = bare></div>\n"
        "<script>el.className = `x-${tone}`;</script>\n"
        # A blank literal prints nothing, rightly: not a lost {{ }}.
        "{% set blank = '' %}<p class=\"{{ blank }}\"></p>\n"))])
    assert [p.split(": ", 1)[0] for p in found.problems] == [
        "odd.html:1", "odd.html:2", "odd.html:3", "odd.html:4", "odd.html:5", "odd.html:6"]
    assert "unquoted class attribute" in found.problems[0]
    assert "template literal in :class" in found.problems[1]
    assert "cannot isolate" in found.problems[2]
    assert "cannot follow {{ m.values()|first }}" in found.problems[3]
    assert "unquoted x-transition:leave attribute" in found.problems[4]
    assert "template literal in JavaScript" in found.problems[5]
