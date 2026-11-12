"""No Alpine expression opens with a statement keyword.

Alpine 3 (the vendored ``static/vendor/alpinejs-3.15.12.min.js``) compiles a
directive's expression as ``__self.result = <expression>`` inside
``with (scope) { … }``. Only an expression opening with ``if (…)`` or with
``let`` / ``const`` is first wrapped in an async function. One that opens
with any other statement — ``var``, ``for``, ``return``… — is a
SyntaxError: Alpine logs « Alpine Expression Error: Unexpected token 'var' »
and the directive NEVER runs. Nothing on the server sees it.

The administration entry form carried three such handlers from 5a4004d
(2026-08-13) to 2026-11-11: its x-init (the account type read at load), the
invoice select's @change (the amount filled from the balance) and the
dossier results' @click (the dossier picked) — none ever ran (checked in
headless Chrome with the vendored file, beside the same expressions opened
with ``let``; a read-only census found no stored dossier lost). This sweep
keeps every template free of them, and proves it catches one.
"""

import pathlib
import re

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
_ROOTS = (_ATHENA / "templates", _ATHENA / "client" / "templates")
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
# Every Alpine directive that takes an expression: x-…, @event, :binding.
_DIRECTIVE = re.compile(
    r"""(?<![\w:-])(x-[\w:.-]+|@[A-Za-z][\w.:-]*|:[A-Za-z][\w.:-]*)"""
    r"""\s*=\s*(["'])(.*?)\2""",
    re.S)
_STATEMENT = re.compile(
    r"\s*(var|for|while|do|try|switch|return|throw|with|break|continue)\b")


def statement_led_directives(text: str) -> list[str]:
    """The directives of *text* whose expression opens with a statement
    keyword, Jinja comments set aside."""
    text = _JINJA_COMMENT.sub("", text)
    return [f"{m.group(1)}={m.group(3)[:60]!r}" for m in _DIRECTIVE.finditer(text)
            if _STATEMENT.match(m.group(3))]


@pytest.mark.parametrize("snippet, caught", [
    ('<div x-init="var a = 1">', True),
    ("<div @click='return x'>", True),
    ('<select x-on:change.debounce="\n  for (;;) {}">', True),
    ('<div :class="var x">', True),
    ('<div x-init="let a = 1">', False),
    ('<div x-init="const a = 1; var b = 2">', False),
    ('<div @click="if (x) { y() }">', False),
    # Only the opening statement fails: one after it is compiled whole.
    ('<div @click="doThing(); var z = 1">', False),
    # A Jinja comment is never rendered.
    ('{# @click="var x" #}<div>', False),
    # « var » as the start of a word is an expression.
    ('<div @click="variable = 1">', False),
    # An SVG namespace is no Alpine binding.
    ('<svg xmlns:xlink="var">', False),
])
def test_the_sweep_catches_a_statement_led_expression(snippet, caught):
    assert bool(statement_led_directives(snippet)) is caught


def test_no_template_opens_an_alpine_expression_with_a_statement():
    found = []
    scanned = 0
    for root in _ROOTS:
        for path in sorted(root.rglob("*.html")):
            scanned += 1
            for hit in statement_led_directives(path.read_text(encoding="utf-8")):
                found.append(f"{path.relative_to(_ATHENA).as_posix()}: {hit}")
    assert scanned > 50  # the sweep reads the real templates
    assert found == []
