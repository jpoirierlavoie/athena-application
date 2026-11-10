"""A component's method reads its own fields through ``$refs`` or ``$root`` —
never by searching under ``this.$el`` (2026-11-10).

In Alpine 3, ``$el`` is the element whose expression is being evaluated, not
the component's root: a METHOD run by ``@change`` on a nested
``<input type="file">`` sees that input as ``this.$el``, and
``this.$el.querySelector(...)`` searches INSIDE it — finding nothing. The
administration entry's receipt widget read its CSRF token that way: from
2026-08-13 to 2026-11-10 every upload went out with an empty ``X-CSRFToken``,
production refused each one (« The CSRF token is missing »), and no test saw
it — the test apps run without CSRFProtect, where an empty token passes. The
documents form read its fields the same way, safely only because its one
caller ran on the root itself.

So the rule is textual and absolute: no template's code says
``this.$el.querySelector`` (or ``…All``). Read a field with ``x-ref`` and
``this.$refs.<name>``, or with ``this.$root.querySelector(...)``. Jinja
comments are not code and are skipped; an inline handler's bare ``$el`` means
the element it sits on and is not concerned.
"""

import pathlib
import re

from tests.test_template_inline_scripts import _without_jinja_comments

ATHENA = pathlib.Path(__file__).resolve().parent.parent
_TEMPLATE_ROOTS = (ATHENA / "templates", ATHENA / "client" / "templates")
_TRAP = re.compile(r"this\.\$el\.querySelector")   # matches …All too


def trap_lines(text: str) -> list[int]:
    """The 1-based lines of *text* (Jinja comments blanked) that search
    under ``this.$el``."""
    code, _comments = _without_jinja_comments(text)
    return [n for n, line in enumerate(code.splitlines(), 1)
            if _TRAP.search(line)]


def test_no_component_method_searches_under_this_el():
    offenders = []
    for root in _TEMPLATE_ROOTS:
        for path in sorted(root.rglob("*.html")):
            for n in trap_lines(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(ATHENA).as_posix()}:{n}")
    assert not offenders, offenders


def test_the_sweep_catches_the_line_that_broke_the_receipts():
    # The receipt widget's helper as it stood until 2026-11-10 (5a4004d).
    planted = ("champ(nom) { var el = this.$el.querySelector('[name=' + "
               "JSON.stringify(nom) + ']'); return el ? el.value : ''; },")
    assert trap_lines(planted) == [1]
    assert trap_lines("x\nvar all = this.$el.querySelectorAll('input');") == [2]
    # What the fix and the house pattern say instead.
    assert trap_lines("'X-CSRFToken': this.$refs.csrf.value") == []
    assert trap_lines("const el = this.$root.querySelector('[name=\"x\"]');") == []
    # A Jinja comment that names the trap is not code.
    assert trap_lines("{# jamais this.$el.querySelector #}\nok") == []
