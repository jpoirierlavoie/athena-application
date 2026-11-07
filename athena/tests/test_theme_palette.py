"""Dark mode's palette: the one remap, and the guards that keep it whole.

Architecture Rule 10, Change Impact Assessment item 6. Dark mode is ONE
redefinition of the ``--color-*`` variables, written once in
``static/src/app.input.css`` (``:root { @variant dark { … } }``) and
compiled into two screen-only branches, one per dark theme of
``utils/theme.py``. Every colour utility, every Jinja and Python colour map
and every route-built fragment reads its colour through those variables, so
no template carries a dark colour of its own.

No browser runs here. From the files, this module proves:

1. the compiled artifact carries the source's dark declarations, in both
   branches, and that token block declares nothing but tokens;
2. every shade the artifact uses has a dark value or is declared
   never-remapped (black and the chromatic 400/500 signal band only), and
   every dark value is still used;
3. the branches reach only a document whose ``<html data-theme>`` names a
   dark theme, on screen only, and the light rule precedes them;
4. every background/text pair the templates and Python sources write keeps,
   in dark, at least its light contrast (capped at WCAG AA's 4.5), and every
   background written outside a class list is paired or reasoned;
5. the dark tokens lie inside sRGB, and red, orange and amber stay apart;
6. no literal colour (hex, colour function or named keyword) bypasses the
   tokens, and only ``paper`` compiles to a literal (``@theme inline``);
7. ``dark:`` only adds what the remap cannot (three utilities), and so do
   the hand-written dark rules (``DARK_BLOCKS``);
8. ``offline.html``'s own dark block equals the tokens, writes no literal
   and overrides every colour its light rules write;
9. each checker, fed a small broken input, reports it.

Each checker RETURNS its problems, so section 9 can run it on a synthetic
failure. The artifact is the minified Lightning CSS output (one line,
unquoted attribute values, unspaced media features, ``.01``-style numbers,
shortened hex, rules regrouped per ``@media``), so rules are found by
structure and colours compared as numbers, through ``tests/_css.py``'s
linear scanners: no regular expression here (CWE-1333).

What the derived pair scan cannot see, hence ``NESTED_PAIRS`` and the
three-surface rule: a parent's background under a child's text; a Jinja
name set in another file; a class added by JavaScript (a guard fails if a
script, or any attribute but a class list, names a palette class).
"""

import functools
import math
import os
import pathlib
import sys
from collections import defaultdict
from itertools import combinations, product

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, os.fspath(_ATHENA))

from tests import _css  # noqa: E402
from utils.theme import COLOR_SCHEMES, VALID_THEMES  # noqa: E402

_SOURCE = "static/src/app.input.css"
_OFFLINE = "templates/offline.html"

# The themes whose color-scheme includes dark: the keys the dark branches
# must name, and only those (utils/theme.py is the single source).
DARK_THEMES = frozenset(
    t for t in VALID_THEMES if "dark" in COLOR_SCHEMES[t].split())
# The theme that follows the device ("light dark"): the only key allowed
# under prefers-color-scheme.
FOLLOWS_DEVICE = frozenset(
    t for t in VALID_THEMES
    if sorted(COLOR_SCHEMES[t].split()) == ["dark", "light"])

# Shades the dark block leaves out on purpose, keeping their light value:
# - black: the bg-black/40 modal scrims, which must stay a darkening veil;
# - the 400/500 signal band: dots, bars and icons read by hue, which keep the
#   values the lawyer knows (the parity check below still measures them).
#   indigo-500 is NOT here: hover:text-indigo-500 links need it lighter, so
#   it is the one 500 shade the dark block remaps.
#   Only chromatic 400/500 shades may join (check_never_remapped): a text,
#   border or surface shade kept at its light value would leave dark text,
#   or a white sheet, on a dark page.
SIGNAL = frozenset({
    "red-400", "red-500", "orange-400", "amber-400", "amber-500",
    "green-400", "green-500", "teal-500", "blue-500", "indigo-400",
    "purple-500",
})
NEVER_REMAPPED = frozenset({"black"}) | SIGNAL
SIGNAL_STEPS = frozenset({"400", "500"})
# Tailwind's grey families: never a signal, however light or dark.
NEUTRAL_FAMILIES = frozenset({"slate", "gray", "zinc", "neutral", "stone"})

# The portal's templates are in Tailwind's scan set but never go dark (its
# base.html renders no data-theme): they do not constrain the palette.
PORTAL_TEMPLATES = "client/templates"

# Tolerances: a compiled declaration equals its source (float noise only);
# a dark pair may sit at most this far below its target (rounding).
SAME_COLOR = 1e-6
CONTRAST_SLACK = 0.005
AA = 4.5


# ══════════════════════════════════════════════════════════════════════
# Reading the files (cached: every test reads the same bytes)
# ══════════════════════════════════════════════════════════════════════


@functools.lru_cache(maxsize=None)
def _read(relpath: str) -> str:
    return (_ATHENA / relpath).read_text(encoding="utf-8")


@functools.lru_cache(maxsize=None)
def _artifact_text() -> str:
    found = sorted((_ATHENA / "static" / "vendor").glob("app.*.css"))
    assert len(found) == 1, f"exactly one compiled stylesheet expected: {found}"
    return found[0].read_text(encoding="utf-8")


@functools.lru_cache(maxsize=None)
def _artifact_tree() -> _css.Rule:
    return _css.parse_css(_artifact_text())


@functools.lru_cache(maxsize=None)
def _source_tree() -> _css.Rule:
    return _css.parse_css(_read(_SOURCE))


def scan_set_of(source_css: str, base: pathlib.Path) -> tuple:
    """The directories the ``@source`` lines of ``source_css`` name (each
    resolved against ``base``), as paths relative to athena/.

    Only a quoted path to a directory under athena/ is read. A glob, a
    ``not`` path or an ``inline()`` source would need its own reader: it
    raises ``ValueError`` rather than let the guards scan less than Tailwind
    compiles. So does a stylesheet with no ``@source`` at all. The one
    exception is ``@source not inline(...)``: it only REMOVES candidates (the
    icon size names that collide with Tailwind's margin utilities, pinned by
    tests/test_icons.py), adding no file and no class to the scan set."""
    dirs = []
    for statement in _css.parse_css(source_css).declarations:
        if not statement.startswith("@source"):
            continue
        written = statement[len("@source"):].strip()
        if written.startswith("not inline(") and written.endswith(")"):
            continue
        target = _css.unquote(written)
        path = (base / target).resolve()
        if target == written or any(c in target for c in "*?[]{}!") \
                or not path.is_dir() \
                or not path.is_relative_to(_ATHENA):
            raise ValueError(f"an @source this guard cannot read: {statement}")
        dirs.append(path.relative_to(_ATHENA).as_posix())
    if not dirs:
        raise ValueError("the stylesheet declares no @source")
    return tuple(dirs)


@functools.lru_cache(maxsize=None)
def _scan_set() -> tuple:
    """Tailwind's complete scan set: the directories of app.input.css's
    ``@source`` lines, relative to athena/."""
    return scan_set_of(_read(_SOURCE), (_ATHENA / _SOURCE).parent)


@functools.lru_cache(maxsize=None)
def _scan_files() -> tuple:
    """``(relpath, text)`` of every file of the scan set (bytecode aside)."""
    out = []
    for folder in _scan_set():
        for path in sorted((_ATHENA / folder).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                out.append((path.relative_to(_ATHENA).as_posix(),
                            path.read_text(encoding="utf-8")))
    return tuple(out)


def _parity_files() -> list:
    """The scan set without the portal's templates."""
    assert PORTAL_TEMPLATES in _scan_set(), _scan_set()
    return [(p, t) for p, t in _scan_files()
            if not p.startswith(PORTAL_TEMPLATES + "/")]


# ══════════════════════════════════════════════════════════════════════
# The palettes
# ══════════════════════════════════════════════════════════════════════


def _token_blocks(tree: _css.Rule) -> list:
    """The source's ``:root { @variant dark { … } }`` blocks."""
    return [rule for ancestors, rule in _css.walk(tree)
            if _css.squash(rule.prelude) == "@variantdark"
            and len(ancestors) == 1
            and _css.squash(ancestors[0].prelude) == ":root"]


def _theme_keys(prelude: str) -> set:
    """The theme values a selector names in ``[data-theme=…]``."""
    return {v for v in _css.attribute_values(prelude, "data-theme")
            if v is not None}


def _dark_rules(tree: _css.Rule) -> list:
    """``(ancestors, rule, keys)`` of every compiled rule keyed on a theme
    value: the token branches, the floating-surface rule, ``dark:``."""
    return [(ancestors, rule, _theme_keys(rule.prelude))
            for ancestors, rule in _css.walk(tree)
            if _theme_keys(rule.prelude)]


def _token_branches(tree: _css.Rule) -> dict:
    """``{key: [rules]}``: the compiled ``:root`` rules keyed on a theme
    that declare ``--color-*`` (the token block's branches)."""
    out = defaultdict(list)
    for _, rule, keys in _dark_rules(tree):
        if rule.prelude.startswith(":root") and any(
                name.startswith("--color-")
                for name in _css.declaration_map(rule)):
            for key in keys:
                out[key].append(rule)
    return out


def _colors_of(rule: _css.Rule) -> dict:
    return {name[len("--color-"):]: _css.parse_color(value)
            for name, value in _css.declaration_map(rule).items()
            if name.startswith("--color-")}


@functools.lru_cache(maxsize=None)
def light_palette() -> dict:
    """``{shade: OKLab}`` of the artifact's ``@layer theme`` block (canvas,
    white and black included)."""
    for ancestors, rule in _css.walk(_artifact_tree()):
        if [a.prelude for a in ancestors] == ["@layer theme"] \
                and _css.squash(rule.prelude) == ":root,:host":
            return _colors_of(rule)
    raise AssertionError("no @layer theme :root,:host block in the artifact")


@functools.lru_cache(maxsize=None)
def dark_palette() -> dict:
    """``{shade: OKLab}`` of the source's dark token block."""
    blocks = _token_blocks(_source_tree())
    assert len(blocks) == 1, f"{len(blocks)} dark token blocks in {_SOURCE}"
    return _colors_of(blocks[0])


@functools.lru_cache(maxsize=None)
def inline_palette() -> dict:
    """``{shade: OKLab}`` of the colour utilities that compile to a literal
    (``@theme inline``: ``bg-paper``), the same in every theme."""
    return inline_colors_of(_artifact_tree())


def inline_colors_of(tree: _css.Rule) -> dict:
    """``{shade: OKLab}`` of the ``bg-*``/``text-*`` utilities of a compiled
    tree whose colour is a literal rather than a ``var()``."""
    out = {}
    for ancestors, rule in _css.walk(tree):
        names = _css.class_names_in_selector(rule.prelude)
        if [a.prelude for a in ancestors] != ["@layer utilities"] \
                or len(names) != 1 or _css.squash(rule.prelude) != "." + names[0]:
            continue
        for prefix, prop in (("bg-", "background-color"), ("text-", "color")):
            value = _css.declaration_map(rule).get(prop, "")
            if names[0].startswith(prefix) and value and "var(" not in value:
                try:
                    out[names[0][len(prefix):]] = _css.parse_color(value)
                except ValueError:
                    pass  # transparent, currentcolor, an arbitrary value
    return out


def light_of(shade: str):
    return inline_palette().get(shade, light_palette().get(shade))


def dark_of(shade: str):
    if shade in inline_palette():
        return inline_palette()[shade]
    if shade in dark_palette():
        return dark_palette()[shade]
    if shade in NEVER_REMAPPED:
        return light_palette().get(shade)
    return None


def _palette_shades() -> set:
    return set(light_palette()) | set(inline_palette())


# ══════════════════════════════════════════════════════════════════════
# 1. Source = artifact
# ══════════════════════════════════════════════════════════════════════


def _compare_declarations(source: dict, compiled: dict, key: str) -> list:
    problems = []
    where = f"[data-theme={key}] branch"
    for name in sorted(set(source) ^ set(compiled)):
        side = "source only" if name in source else "compiled only"
        problems.append(f"{where}: {name} is {side}")
    for name in sorted(set(source) & set(compiled)):
        a, b = source[name], compiled[name]
        if "var(" in b:
            problems.append(f"{where}: {name} compiles to a var(): {b}")
        elif name.startswith("--color-"):
            try:
                gap = _css.delta_e_ok(_css.parse_color(a), _css.parse_color(b))
            except ValueError as exc:
                problems.append(f"{where}: {name}: {exc}")
                continue
            if gap > SAME_COLOR:
                problems.append(f"{where}: {name} is {b}, the source says {a}")
        elif name == "color-scheme":
            if sorted(a.split()) != sorted(b.split()):
                problems.append(f"{where}: color-scheme {b!r}, source {a!r}")
        elif _css.squash(a) != _css.squash(b):
            problems.append(f"{where}: {name} is {b!r}, the source says {a!r}")
    return problems


def check_source_matches_artifact(source_css: str, artifact_css: str,
                                  dark_themes) -> list:
    """The source's dark token block, declaration for declaration, in the
    compiled branch of each dark theme, with no ``var()`` anywhere in it and
    nothing but ``color-scheme`` and ``--color-*`` tokens: any other
    declaration there would restyle every element of a dark page."""
    blocks = _token_blocks(_css.parse_css(source_css))
    if len(blocks) != 1:
        return [f"the source must hold exactly one ':root {{ @variant dark "
                f"{{ … }} }}' block, found {len(blocks)}"]
    source = _css.declaration_map(blocks[0])
    problems = [
        f"source: {name} uses var(), which would resolve against the "
        f"remapped tokens themselves (chains, cycles)"
        for name, value in sorted(source.items()) if "var(" in value]
    problems += [
        f"source: {name} in the token block is not a token (only "
        "color-scheme and --color-* belong there)"
        for name in sorted(source)
        if name != "color-scheme" and not name.startswith("--color-")]
    if source.get("color-scheme") != "dark":
        problems.append(f"source: color-scheme is {source.get('color-scheme')!r},"
                        " not 'dark'")
    branches = _token_branches(_css.parse_css(artifact_css))
    for key in sorted(dark_themes):
        rules = branches.get(key, [])
        if len(rules) != 1:
            problems.append(f"{len(rules)} compiled ':root' token branch(es) "
                            f"keyed on [data-theme={key}], expected 1")
            continue
        problems += _compare_declarations(
            source, _css.declaration_map(rules[0]), key)
    for key in sorted(set(branches) - set(dark_themes)):
        problems.append(f"a token branch is keyed on [data-theme={key}], "
                        "which utils/theme.py does not make dark")
    return problems


def test_the_compiled_branches_carry_the_source_dark_block():
    problems = check_source_matches_artifact(
        _read(_SOURCE), _artifact_text(), DARK_THEMES)
    assert not problems, "\n".join(problems)
    # Non-vacuous: the block is there, with its scheme and its tokens.
    assert DARK_THEMES and len(dark_palette()) > 20, sorted(dark_palette())


# ══════════════════════════════════════════════════════════════════════
# 2. Coverage, both ways
# ══════════════════════════════════════════════════════════════════════


def referenced_colors(artifact_css: str) -> set:
    """The shades the artifact reads through ``var(--color-*)``, outside
    the dark token branches themselves."""
    tree = _css.parse_css(artifact_css)
    branches = {id(r) for rules in _token_branches(tree).values() for r in rules}
    statements = list(tree.declarations)
    for _, rule in _css.walk(tree):
        if id(rule) not in branches:
            statements.extend(rule.declarations)
    return {name[len("--color-"):] for statement in statements
            for name in _css.var_references(statement, "--color-")}


def check_coverage(referenced: set, dark: set, never_remapped: set) -> list:
    """``referenced − dark == never_remapped`` exactly, and no dark value
    goes unused: a new shade without a dark value, or a stale entry on
    either side, is a problem."""
    problems = []
    for shade in sorted(referenced - dark - never_remapped):
        problems.append(
            f"--color-{shade} is used but has no dark value: add one to the "
            f"dark block of {_SOURCE} (only a chromatic 400/500 signal shade "
            "may instead join SIGNAL)")
    for shade in sorted(dark - referenced):
        problems.append(f"--color-{shade} has a dark value but nothing uses "
                        "it any more: remove it from the dark block")
    for shade in sorted(never_remapped - referenced):
        problems.append(f"NEVER_REMAPPED lists {shade}, which nothing uses: "
                        "remove it")
    for shade in sorted(never_remapped & dark):
        problems.append(f"{shade} is remapped AND listed in NEVER_REMAPPED")
    return problems


def test_every_used_shade_has_a_dark_value_and_none_is_stale():
    referenced = referenced_colors(_artifact_text())
    problems = check_coverage(referenced, set(dark_palette()), NEVER_REMAPPED)
    assert not problems, "\n".join(problems)
    assert len(referenced) > len(NEVER_REMAPPED), sorted(referenced)


def check_never_remapped(never_remapped) -> list:
    """Only black and chromatic 400/500 shades may keep their light value:
    listing any other shade would silence the coverage check while the
    shade stays light on a dark page."""
    problems = []
    for shade in sorted(set(never_remapped) - {"black"}):
        family, _, step = shade.rpartition("-")
        if step not in SIGNAL_STEPS or not family \
                or family in NEUTRAL_FAMILIES:
            problems.append(
                f"NEVER_REMAPPED lists {shade}: only black and the chromatic "
                "400/500 signal shades keep their light value in dark — give "
                f"it a dark value in {_SOURCE} instead")
    return problems


def test_never_remapped_holds_only_black_and_the_signal_band():
    assert check_never_remapped(NEVER_REMAPPED) == []
    assert NEVER_REMAPPED - SIGNAL == {"black"}


# ══════════════════════════════════════════════════════════════════════
# 3. Scope
# ══════════════════════════════════════════════════════════════════════


def _media_queries(ancestors) -> list:
    return [_css.squash(a.prelude)[len("@media"):] for a in ancestors
            if a.prelude.startswith("@media")]


def _screen_only(queries) -> bool:
    """True when one enclosing ``@media`` matches screens only: every query
    of its comma-separated list starts with ``screen`` (``screen,print``
    would reach print too)."""
    return any(q and all(part.startswith("screen") for part in q.split(","))
               for q in queries)


def check_scope(artifact_css: str, dark_themes, follows_device) -> list:
    """Dark only on screen, only under a dark theme's ``[data-theme]``,
    only the device-following theme under ``prefers-color-scheme``, and
    the explicit light rule first."""
    problems = []
    tree = _css.parse_css(artifact_css)

    light = [(a, r) for a, r in _css.walk(tree)
             if _css.squash(r.prelude) == ":root:where([data-theme])"]
    if len(light) != 1:
        problems.append(f"{len(light)} ':root:where([data-theme])' light "
                        "rules, expected 1")
    else:
        ancestors, rule = light[0]
        scheme = _css.declaration_map(rule).get("color-scheme", "")
        if sorted(scheme.split()) != ["light", "only"]:
            problems.append(f"the light rule sets color-scheme {scheme!r}, "
                            "not 'only light'")
        if _media_queries(ancestors):
            problems.append("the light rule sits inside @media: print and "
                            "Clair must get it too")

    seen, under_device, elsewhere = set(), set(), set()
    for ancestors, rule, keys in _dark_rules(tree):
        if len(keys) != 1:
            problems.append(f"{rule.prelude}: names several themes {sorted(keys)}")
            continue
        (key,) = keys
        seen.add(key)
        queries = _media_queries(ancestors)
        if not _screen_only(queries):
            problems.append(f"{rule.prelude}: not inside a screen-only "
                            "'@media screen' block (print would go dark)")
        if any("prefers-color-scheme:dark" in q for q in queries):
            under_device.add(key)
        else:
            elsewhere.add(key)
    if seen != set(dark_themes):
        problems.append(f"the dark rules are keyed on {sorted(seen)}; "
                        f"utils/theme.py makes {sorted(dark_themes)} dark")
    if under_device != set(follows_device):
        problems.append(f"{sorted(under_device)} sit under prefers-color-"
                        f"scheme; only {sorted(follows_device)} follows the "
                        "device")
    for key in sorted(under_device & elsewhere):
        problems.append(f"[data-theme={key}] is dark both with and without "
                        "the device's preference")

    branches = [r for rules in _token_branches(tree).values() for r in rules]
    if not branches:
        problems.append("no compiled ':root' token branch")
    elif len(light) == 1 and not all(light[0][1].index < r.index
                                     for r in branches):
        problems.append("the light rule must come BEFORE the token branches: "
                        "at equal specificity the later rule wins")

    media = [r for _, r in _css.walk(tree)
             if r.prelude.startswith("@media")
             and "prefers-color-scheme" in r.prelude]
    total = _css.strip_comments(artifact_css).count("prefers-color-scheme")
    if total != len(media):
        problems.append(f"prefers-color-scheme appears {total} times but "
                        f"heads {len(media)} @media blocks")
    for block in media:
        if _css.squash(block.prelude) != "@mediascreenand(prefers-color-scheme:dark)":
            problems.append(f"unexpected media query {block.prelude!r}")
        for _, rule in _css.walk(block):
            keys = _theme_keys(rule.prelude)
            if rule.declarations and not (keys and keys <= set(follows_device)):
                problems.append(
                    f"{rule.prelude!r} under {block.prelude!r} is not keyed on "
                    f"[data-theme={'|'.join(sorted(follows_device))}]: it "
                    "would reach the portal")

    for _, rule in _css.walk(tree):
        if "dark" in _css.class_names_in_selector(rule.prelude):
            problems.append(f"{rule.prelude!r}: a .dark class selector "
                            "(Tailwind's class strategy, never used here)")
    return problems


def test_dark_reaches_only_dark_themes_on_screen():
    assert FOLLOWS_DEVICE and FOLLOWS_DEVICE <= DARK_THEMES, (
        DARK_THEMES, FOLLOWS_DEVICE)
    problems = check_scope(_artifact_text(), DARK_THEMES, FOLLOWS_DEVICE)
    assert not problems, "\n".join(problems)


# ══════════════════════════════════════════════════════════════════════
# 4. Parity, derived from the source
# ══════════════════════════════════════════════════════════════════════

# Translucent utilities are composited over whatever lies beneath, which a
# class scan cannot know: they are skipped, each for a stated reason. A new
# one fails the parity test until it is listed (a decision, not a blind
# spot); one no longer used must leave the list.
TRANSLUCENT = {
    "bg-black/40": "modal scrims: a veil over the page that carries no "
                   "text; black is never remapped",
    "bg-gray-50/50": "the month grid's filler cells outside the month: empty",
    "bg-green-50/50": "protocol step cards: a half tint laid over the card "
                      "(bg-white), whose text sits in children; the text "
                      "sweep measures that text on the card itself, which "
                      "the tint barely shifts",
    "bg-red-50/50": "protocol step cards (as bg-green-50/50)",
    "bg-amber-50/50": "protocol step cards (as bg-green-50/50)",
}

# Pairs no single class list holds: a background on one element, the text
# on another. Each entry is pinned to the source it describes by witnesses,
# so the list fails instead of going stale. A witness is (file, needle),
# looked up with comments stripped and whitespace removed, or (file,
# selector, property, value) for a hand-written rule of app.input.css.
_TOAST = (("templates/base.html",
           ":class=\"t.type === 'error' ? 'bg-red-600' : 'bg-gray-900'\""),
          ("templates/base.html",
           'class="text-white text-sm px-4 py-2 rounded-lg shadow-lg '
           'animate-fade-in"'))
_NOTE_TEXT = ("templates/notes/detail.html",
              'class="note-content text-sm text-gray-700"')
# The task row: its swipe paints the whole row green-100 (a :style), under
# every text colour of the row that has no background of its own.
_TR = "templates/tasks/_task_rows.html"
_SWIPE = (_TR, "background: var(--color-green-100)")
_DUE_DATE = (_TR, "'text-red-600 font-semibold' if is_overdue else "
                  "'text-gray-500'")
NESTED_PAIRS = (
    ("green-500", "white",
     "done checkbox: the check icon's text-white on the box's bg-green-500",
     ((_TR, "border-green-500 bg-green-500"),
      (_TR, "ms('check', 12, 'text-white')"))),
    ("green-400", "white",
     "the same check icon on the box's hover:bg-green-400",
     ((_TR, "hover:bg-green-400"),)),
    ("green-100", "gray-900",
     "task-row swipe: the row's :style background under the title",
     (_SWIPE, (_TR, 'class="text-sm font-medium text-gray-900 truncate"'))),
    ("green-100", "indigo-600",
     "task-row swipe under the dossier number",
     (_SWIPE, (_TR, 'class="text-xs text-indigo-600 font-medium"'))),
    ("green-100", "gray-500", "task-row swipe under the due date",
     (_SWIPE, _DUE_DATE)),
    ("green-100", "red-600", "task-row swipe under the overdue date",
     (_SWIPE, _DUE_DATE)),
    ("gray-100", "gray-700",
     ".note-content code and pre under the note's text-gray-700",
     ((_SOURCE, ".note-content code", "background", "var(--color-gray-100)"),
      (_SOURCE, ".note-content pre", "background", "var(--color-gray-100)"),
      _NOTE_TEXT)),
    ("gray-50", "gray-700",
     ".note-content th under the note's text-gray-700",
     ((_SOURCE, ".note-content th", "background", "var(--color-gray-50)"),
      _NOTE_TEXT)),
    ("white", "gray-500", ".note-content blockquote text on the note's card",
     ((_SOURCE, ".note-content blockquote", "color", "var(--color-gray-500)"),
      ("templates/notes/detail.html",
       '<div class="bg-white rounded-xl border border-gray-200 p-5 mb-4">'
       '<div class="note-content'))),
    ("white", "indigo-600", ".note-content links on the note's card",
     ((_SOURCE, ".note-content a", "color", "var(--color-indigo-600)"),)),
    ("gray-900", "white", "toast: the :class bg-gray-900 under the static "
     "text-white", _TOAST),
    ("red-600", "white", "error toast: the :class bg-red-600 under the "
     "static text-white", _TOAST),
)

# Every text colour used anywhere is also checked on these surfaces: the
# card, the page and the subtle panel, the three a text most often sits on
# without its element saying so.
SURFACES = ("white", "canvas", "gray-50")

# Pairs allowed to fail, each with its reason. It can only shrink: an entry
# whose pair passes, or is no longer written, fails the test. None today —
# even the deliberately low-contrast disabled bg-gray-300 + text-white keeps
# its light ratio (1.47 light, 2.11 dark).
EXEMPT: dict = {}


# The most class lists one element may combine: its dynamic sources (each
# Jinja branch point, each :class) multiplied. Every element today stays
# well below it; above it the element needs a look, not a slower scan.
COMBINATIONS_CAP = 64


def class_sets_of_markup(text: str, where: str,
                         cap: int = COMBINATIONS_CAP) -> list:
    """``(where, classes)`` for every class list a template can render.

    Per element: its static classes joined to each combination of its
    dynamic sources (one alternative of every Jinja branch point, ternary
    or map value, and one literal, or none, of every ``:class``), so a
    background and a text colour written in two sources still meet; and
    each alternative alone. Plus every string literal of a Jinja block
    outside a tag (the ``{% set %}`` maps and strings, ``ms()``'s class
    argument). More than ``cap`` combinations on one element raise."""
    markup = _css.scan_markup(text)
    names = _css.jinja_set_values(markup.jinja)
    out = []
    for tag in markup.tags:
        static, groups = _css.class_value_sets(
            " ".join(tag.values("class")), names)
        sources = [list(dict.fromkeys(group)) for group in groups]
        for value in tag.values(":class", "x-bind:class"):
            # Alpine adds one of the literals to the static classes, or none.
            sources.append(list(dict.fromkeys(
                _css.alpine_class_literals(value, names) + [""])))
        at = f"{where}:{tag.line}"
        count = math.prod(len(source) for source in sources)
        if count > cap:
            raise ValueError(f"{at}: {count} class combinations, more than "
                             f"{cap}: simplify the element's dynamic classes")
        for choice in product(*sources):
            out.append((at, " ".join((static,) + choice)))
        for source in sources:
            out.extend((at, alt) for alt in source)
    for block in markup.jinja:
        for literal in _css.string_literals(block):
            out.append((f"{where} (Jinja literal)", literal))
    return out


def class_sets_of_python(text: str, where: str) -> list:
    """Every string constant (read by ``ast``): the ``class="…"`` values of
    an HTML fragment, or the whole string (``_KYC_BADGES``,
    ``STEP_STATUS_COLORS``)."""
    out = []
    for string in _css.python_strings(text):
        values = (_css.fragment_attribute_values(string)
                  if "class=" in string else [string])
        out.extend((where, value) for value in values)
    return out


def class_sets_of(files) -> list:
    out = []
    for where, text in files:
        if where.endswith(".html"):
            out.extend(class_sets_of_markup(text, where))
        elif where.endswith(".py"):
            out.extend(class_sets_of_python(text, where))
        else:
            raise AssertionError(f"{where}: a scan-set file this guard cannot "
                                 "read; extend class_sets_of")
    return out


@functools.lru_cache(maxsize=None)
def _utility_order() -> dict:
    """``{class: rank}`` of the compiled utility rules: of two colours of
    one kind in one class list, the later rule wins."""
    order = {}
    for ancestors, rule in _css.walk(_artifact_tree()):
        if ancestors and ancestors[0].prelude == "@layer utilities":
            names = _css.class_names_in_selector(rule.prelude)
            if names:
                order[names[0]] = rule.index
    return order


def pairs_of(class_sets, shades, order) -> tuple:
    """``(pairs, translucent)``: ``{(bg, text): {where}}`` paired within one
    state of each class list, and ``{token: {where}}`` of the skipped
    translucent utilities.

    A state is a variant stack: the base (none), ``hover:``,
    ``peer-checked:``, ``disabled:``… A state's background pairs with its
    own text colour, else the base one, and its text with its own
    background, else the base one. ``dark:`` utilities are left to the
    allowlist test."""
    pairs = defaultdict(set)
    translucent = defaultdict(set)
    for where, classes in class_sets:
        found = defaultdict(list)
        for token in _css.class_tokens(classes):
            variants, utility = _css.split_variants(token)
            if "dark" in variants:
                continue
            for kind in ("bg-", "text-"):
                if not utility.startswith(kind):
                    continue
                shade, slash, _ = utility[len(kind):].partition("/")
                if shade not in shades:
                    continue
                if slash:
                    translucent[":".join(variants + (utility,))].add(where)
                else:
                    found[(variants, kind)].append(
                        ":".join(variants + (utility,)))

        def winner(state, kind):
            tokens = found.get((state, kind))
            if not tokens:
                return None
            token = max(tokens, key=lambda t: order.get(t, -1))
            return token.rsplit(":", 1)[-1][len(kind):]

        base = (winner((), "bg-"), winner((), "text-"))
        for state in {state for state, _ in found}:
            bg = winner(state, "bg-") or base[0]
            text = winner(state, "text-") or base[1]
            if bg and text:
                pairs[(bg, text)].add(where)
    return pairs, translucent


def text_colors_of(files, shades) -> dict:
    """``{shade: {where}}`` of every ``text-*`` palette colour written
    anywhere in ``files`` (any variant; Jinja comments aside), and every
    ``color:`` a hand-written rule of app.input.css reads from a token."""
    out = defaultdict(set)
    for where, text in files:
        if where.endswith(".py"):
            text = "\n".join(_css.python_strings(text))
        else:
            text = _css.strip_jinja_comments(text)
        for token in _css.class_tokens(text):
            variants, utility = _css.split_variants(token)
            shade = utility[len("text-"):]
            if utility.startswith("text-") and shade in shades \
                    and "dark" not in variants:
                out[shade].add(where)
    return out


def _hand_written_text_colors() -> dict:
    out = defaultdict(set)
    for ancestors, rule in _css.walk(_source_tree()):
        if ancestors or rule.prelude.startswith("@") or rule.prelude == ":root":
            continue
        value = _css.declaration_map(rule).get("color", "")
        for name in _css.var_references(value, "--color-"):
            out[name[len("--color-"):]].add(f"{_SOURCE}: {rule.prelude}")
    return out


def _selectors(prelude: str) -> list:
    """The selectors of a selector list, split at the commas outside
    parentheses (``.a:is(.b, .c)`` is one selector), stripped."""
    out = []
    depth = 0
    start = 0
    for i, c in enumerate(prelude):
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif c == "," and depth == 0:
            out.append(prelude[start:i].strip())
            start = i + 1
    out.append(prelude[start:].strip())
    return out


def _witness_holds(witness) -> bool:
    if len(witness) == 2:
        relpath, needle = witness
        text = _read(relpath)
        text = (_css.strip_comments(text) if relpath.endswith(".css")
                else _css.strip_jinja_comments(text))
        return _css.squash(needle) in _css.squash(text)
    relpath, selector, prop, value = witness
    for _, rule in _css.walk(_css.parse_css(_read(relpath))):
        selectors = {_css.squash(s) for s in _selectors(rule.prelude)}
        declared = _css.declaration_map(rule).get(prop, "")
        if _css.squash(selector) in selectors \
                and _css.squash(declared) == _css.squash(value):
            return True
    return False


# Backgrounds written outside class lists (a hand-written rule of
# app.input.css, a style or :style value) are not derived: each must be the
# background of a NESTED_PAIRS entry whose witness names its selector (or
# its file), or be listed here with the reason no text needs pairing with
# it. A new one fails until it is paired or listed; one no longer written
# must leave the list. Keys: (selector, shade) for app.input.css, (file,
# shade) for markup.
UNPAIRED_BACKGROUNDS = {
    ("body", "canvas"):
        "the page itself: canvas is one of the SURFACES, on which every "
        "text colour is measured",
    (".skeleton", "gray-200"): "the loading placeholder: it holds no text",
    (".dossier-subnav-wrap::after", "gray-50"):
        "the tab strip's right-edge fade: a pseudo-element with no text",
}
_STYLE_VALUE_ATTRIBUTES = ("style", ":style", "x-bind:style")


def _is_background(prop: str) -> bool:
    return prop == "background" or prop.startswith("background-")


def token_backgrounds(source_css: str, files) -> tuple:
    """``(rules, markup)``: ``{(selector, shade)}`` of the ``var(--color-*)``
    read by a background declaration of a hand-written rule of
    ``source_css`` (the @theme blocks and the dark token block aside), and
    ``{(file, shade)}`` of those a style, ``:style`` or ``x-bind:style``
    value of ``files`` reads under a background property (or under no
    property at all: a computed style object counts too)."""
    rules, markup = set(), set()

    def visit(parent, selector):
        for rule in parent.children:
            squashed = _css.squash(rule.prelude)
            if squashed.startswith("@theme") or (
                    squashed == "@variantdark"
                    and _css.squash(parent.prelude) == ":root"):
                continue
            here = selector if rule.prelude.startswith("@") \
                else _selectors(rule.prelude)
            for prop, value in _css.declaration_map(rule).items():
                if _is_background(prop):
                    for name in _css.var_references(value, "--color-"):
                        rules.update((s, name[len("--color-"):]) for s in here)
            visit(rule, here)

    visit(_css.parse_css(source_css), ["(top level)"])
    for where, text in files:
        for _, attribute, value in _style_values(where, text):
            if attribute not in _STYLE_VALUE_ATTRIBUTES:
                continue
            for prop, name in _css.property_var_references(value, "--color-"):
                if not prop or _is_background(prop):
                    markup.add((where, name[len("--color-"):]))
    return rules, markup


def check_token_backgrounds(source_css: str, files, nested,
                            unpaired: dict) -> list:
    """Each background ``token_backgrounds`` finds is the background of a
    ``nested`` pair that names its selector (a 4-part witness on a
    background property) or its file (a 2-part witness), or is a key of
    ``unpaired``; every key of ``unpaired`` is still written."""
    rules, markup = token_backgrounds(source_css, files)
    paired = set()
    for bg, _, _, witnesses in nested:
        for witness in witnesses:
            if len(witness) == 4 and _is_background(witness[2]):
                paired.add((_css.squash(witness[1]), bg))
            elif len(witness) == 2:
                paired.add((witness[0], bg))
    listed = {(_css.squash(where), shade) for where, shade in unpaired}
    problems = []
    for selector, shade in sorted(rules):
        key = (_css.squash(selector), shade)
        if key not in paired and key not in listed:
            problems.append(
                f"{_SOURCE}: {selector} paints a var(--color-{shade}) "
                "background that no NESTED_PAIRS entry pairs with the text "
                "above it: add the pair, or list it in UNPAIRED_BACKGROUNDS "
                "with the reason no text needs it")
    for where, shade in sorted(markup):
        if (where, shade) not in paired and (where, shade) not in listed:
            problems.append(
                f"{where}: a style paints a var(--color-{shade}) background "
                "that no NESTED_PAIRS entry pairs with the text above it: add "
                "the pair, or list it in UNPAIRED_BACKGROUNDS with its reason")
    written = {(_css.squash(s), shade) for s, shade in rules} | markup
    for where, shade in sorted(unpaired):
        if (_css.squash(where), shade) not in written:
            problems.append(f"UNPAIRED_BACKGROUNDS lists ({where!r}, "
                            f"{shade!r}), which nothing paints any more: "
                            "remove it")
    return problems


def check_parity(pairs: dict, light, dark, exempt: dict) -> tuple:
    """``(problems, checked)``: each pair's dark contrast must reach
    ``min(4.5, light contrast)`` (less a rounding slack); ``light`` and
    ``dark`` map a shade to OKLab, or None when unknown. An exemption whose
    pair passes, or is not written any more, is a problem too."""
    problems = []
    failing = set()
    checked = 0
    for (bg, text), where in sorted(pairs.items()):
        colours = (light(bg), light(text), dark(bg), dark(text))
        if any(c is None for c in colours):
            problems.append(f"bg-{bg} + text-{text}: a shade without a light "
                            "or dark value (see the coverage test)")
            continue
        checked += 1
        light_ratio = _css.contrast(colours[0], colours[1])
        dark_ratio = _css.contrast(colours[2], colours[3])
        if dark_ratio < min(AA, light_ratio) - CONTRAST_SLACK:
            failing.add((bg, text))
            if (bg, text) not in exempt:
                problems.append(
                    f"bg-{bg} + text-{text}: dark {dark_ratio:.2f} < "
                    f"min({AA}, light {light_ratio:.2f}) — "
                    + ", ".join(sorted(where)[:3]))
    for key in sorted(set(exempt) - failing):
        problems.append(f"EXEMPT {key} no longer fails, or is no longer "
                        "written: remove the exemption")
    return problems, checked


@functools.lru_cache(maxsize=None)
def _derived_pairs() -> tuple:
    files = _parity_files()
    shades = _palette_shades()
    pairs, translucent = pairs_of(class_sets_of(files), shades,
                                  _utility_order())
    texts = text_colors_of(files, shades)
    for shade, where in _hand_written_text_colors().items():
        texts[shade] |= where
    return pairs, translucent, texts


def all_pairs() -> dict:
    """The derived pairs, NESTED_PAIRS, and every text colour on each of
    the SURFACES."""
    derived, _, texts = _derived_pairs()
    pairs = defaultdict(set)
    for key, where in derived.items():
        pairs[key] |= where
    for bg, text, why, _ in NESTED_PAIRS:
        pairs[(bg, text)].add(f"NESTED_PAIRS: {why}")
    for text, where in texts.items():
        for surface in SURFACES:
            pairs[(surface, text)].add(
                f"text-{text} on the bg-{surface} surface "
                f"(e.g. {sorted(where)[0]})")
    return pairs


def test_every_colour_pair_keeps_its_light_contrast_in_dark():
    problems, checked = check_parity(all_pairs(), light_of, dark_of, EXEMPT)
    assert not problems, (
        f"{checked} pairs checked, {len(problems)} problem(s):\n"
        + "\n".join(problems))
    # The scan must never go silently (nearly) empty.
    assert checked > 100, f"only {checked} pairs checked: the scan went empty"


def test_the_pair_scan_sees_branches_states_and_python_maps():
    """Non-vacuous: pairs that only a branch-, state- or AST-aware scan
    finds are found where they are written."""
    derived, _, texts = _derived_pairs()
    expected = {
        # An Alpine :class ternary combined with the static text-white.
        ("red-600", "white"): "templates/base.html",
        # A hover: background with the base text colour.
        ("gray-800", "white"): "templates/",
        # A peer-checked: pill.
        ("gray-900", "white"): "templates/parties/form.html",
        # routes/parties.py _KYC_BADGES, read by ast.
        ("orange-100", "orange-700"): "routes/parties.py",
        # models/protocol.py PROTOCOL_TYPE_COLORS.
        ("indigo-100", "indigo-700"): "models/protocol.py",
        # routes/invoices.py's error box, an HTML fragment.
        ("red-50", "red-600"): "routes/invoices.py",
    }
    for pair, origin in expected.items():
        assert any(w.startswith(origin) for w in derived.get(pair, ())), (
            pair, sorted(derived.get(pair, ()))[:5])
    assert len(texts) > 20, sorted(texts)


def test_translucent_utilities_are_each_accounted_for():
    _, translucent, _ = _derived_pairs()
    unlisted = {t: sorted(w)[:3] for t, w in translucent.items()
                if t not in TRANSLUCENT}
    assert not unlisted, (
        "translucent colour utilities the pair scan skips without a stated "
        f"reason (add each to TRANSLUCENT): {unlisted}")
    stale = sorted(set(TRANSLUCENT) - set(translucent))
    assert not stale, f"TRANSLUCENT lists utilities nothing writes: {stale}"


def test_nested_pairs_still_describe_the_source():
    stale = [(bg, text, witness) for bg, text, _, witnesses in NESTED_PAIRS
             for witness in witnesses if not _witness_holds(witness)]
    assert not stale, (
        "NESTED_PAIRS entries whose source changed — update the pair and "
        f"its witness: {stale}")


def test_backgrounds_outside_class_lists_are_each_accounted_for():
    files = [(p, t) for p, t in _parity_files()
             if p.endswith(".html") or p.endswith(".py")]
    problems = check_token_backgrounds(_read(_SOURCE), files, NESTED_PAIRS,
                                       UNPAIRED_BACKGROUNDS)
    assert not problems, "\n".join(problems)
    # Non-vacuous: the note's code background and the task-row swipe are
    # found, and found paired.
    rules, markup = token_backgrounds(_read(_SOURCE), files)
    assert (".note-content code", "gray-100") in rules, sorted(rules)
    assert (_TR, "green-100") in markup, sorted(markup)


# Attributes the pair scan reads as class lists; a palette class written in
# any other attribute (x-data, @click, data-*) or in a script escapes it.
CLASS_ATTRIBUTES = frozenset({"class", ":class", "x-bind:class"})


def palette_classes_outside_class_lists(files, shades) -> tuple:
    """``(found, scripts)``: ``(where, token)`` of every ``bg-*``/``text-*``
    palette class a template writes in a ``<script>`` body or in an
    attribute other than ``CLASS_ATTRIBUTES``, and the number of script
    bodies read."""
    found = []
    scripts = 0
    for where, text in files:
        if not where.endswith(".html"):
            continue
        markup = _css.scan_markup(text)
        scripts += len(markup.raw["script"])
        places = [(f"{where} <script>", body) for body in markup.raw["script"]]
        places += [(f"{where}:{tag.line} {name}", value)
                   for tag in markup.tags for name, value in tag.attrs
                   if value and name not in CLASS_ATTRIBUTES]
        for place, value in places:
            for token in _css.class_tokens(value):
                _, utility = _css.split_variants(token)
                for kind in ("bg-", "text-"):
                    shade = utility[len(kind):].partition("/")[0]
                    if utility.startswith(kind) and shade in shades:
                        found.append((place, token))
    return found, scripts


def test_no_script_or_other_attribute_names_a_palette_class():
    """The pair scan reads class lists only: a palette class toggled by a
    script, or held in x-data for a later :class, would escape it. Pair it
    in NESTED_PAIRS, then relax this."""
    found, scripts = palette_classes_outside_class_lists(
        _parity_files(), _palette_shades())
    assert not found, found
    # Non-vacuous: the templates' scripts were read.
    assert scripts > 0, "no <script> body read: the scan went empty"


# ══════════════════════════════════════════════════════════════════════
# 5. Gamut and distinctness
# ══════════════════════════════════════════════════════════════════════

# Red, orange and amber carry the statuses that must never be confused (a
# conflict against an unverified identity, an overdue hearing against a
# postponed one). At the same step their dark values stay at least this far
# apart in OKLab. The 50 step (alert fills) cannot reach 0.06 inside sRGB at
# a fill's lightness, so it only has to beat its light distance and 0.03;
# its borders and text, which do meet 0.06, tell the alerts apart.
WARM = ("red", "orange", "amber")
WARM_STEPS = (100, 600, 700, 800)
WARM_FLOOR = 0.06
TINT_STEP = 50
TINT_FLOOR = 0.03


def check_gamut_and_distinctness(light: dict, dark: dict) -> tuple:
    """``(problems, compared)``: every dark token inside sRGB within 1e-3,
    and the warm families apart at each step both declare."""
    problems = []
    for shade, lab in sorted(dark.items()):
        if not _css.in_srgb_gamut(lab):
            rgb = ", ".join(f"{c:.3f}" for c in _css.oklab_to_linear_srgb(lab))
            problems.append(f"--color-{shade} lies outside sRGB (linear "
                            f"{rgb}): sRGB and P3 screens would differ")
    compared = 0
    for step in WARM_STEPS + (TINT_STEP,):
        here = 0
        for a, b in combinations(WARM, 2):
            first, second = f"{a}-{step}", f"{b}-{step}"
            if first not in dark or second not in dark:
                continue
            gap = _css.delta_e_ok(dark[first], dark[second])
            floor = WARM_FLOOR
            if step == TINT_STEP:
                floor = max(TINT_FLOOR,
                            _css.delta_e_ok(light[first], light[second]))
            here += 1
            if gap < floor:
                problems.append(f"{first} / {second}: delta E (OKLab) "
                                f"{gap:.3f} < {floor:.3f} in dark")
        if not here:
            problems.append(f"step {step}: no two warm families declare it "
                            "(the comparison went empty)")
        compared += here
    return problems, compared


def test_dark_tokens_are_in_srgb_and_warm_statuses_stay_apart():
    problems, compared = check_gamut_and_distinctness(
        light_palette(), dark_palette())
    assert not problems, "\n".join(problems)
    assert compared >= len(WARM_STEPS) + 1, compared


# ══════════════════════════════════════════════════════════════════════
# 6. Literal colours
# ══════════════════════════════════════════════════════════════════════

# Graph email bodies: mail clients ignore classes and <style>, so their
# colours are inline by necessity, and an email never follows the app's
# theme. Each must still write a colour (else the exemption is stale).
EMAIL_BODIES = {
    "templates/reception/_accuse_bordereau.html":
        "Graph email body (accusé de réception): inline styles only",
    "templates/reception/_confirmation_intake.html":
        "Graph email body (confirmation d'ouverture): inline styles only",
}
# offline.html is served by the service worker without the stylesheet: its
# own <style> is its only CSS (section 8 holds its dark block to the tokens).
STYLE_ELEMENT_ALLOWED = frozenset({_OFFLINE})
# Attributes whose value can write a colour: style, and the SVG and HTML
# presentation attributes (every inline icon reads currentColor today),
# each also bound by Alpine.
STYLE_ATTRIBUTES = tuple(
    prefix + name
    for name in ("style", "fill", "stroke", "stop-color", "flood-color",
                 "lighting-color", "color", "bgcolor")
    for prefix in ("", ":", "x-bind:"))


def literal_colours(text: str, functions=_css.COLOR_FUNCTIONS) -> list:
    """Every colour ``text`` writes as a literal: hex, the colour functions
    in ``functions`` and the CSS named keywords (``transparent`` and
    ``currentColor`` are not literals). Custom properties are read too: an
    arbitrary ``ring-[red]`` compiles to ``--tw-ring-color:red``."""
    return _css.color_literals(text, functions) + _css.named_colors(text)


def check_arbitrary_values(artifact_css: str) -> list:
    """No arbitrary-value utility (``bg-[#…]``, ``text-[rgb(…)]``,
    ``bg-[red]``) compiles a colour: it would bypass the remap. Sizes
    (``text-[8px]``) are fine."""
    problems = []
    functions = _css.COLOR_FUNCTIONS + ("color-mix(",)
    for _, rule in _css.walk(_css.parse_css(artifact_css)):
        if "\\[" not in rule.prelude:
            continue
        for statement in rule.declarations:
            found = literal_colours(_css.blank_strings(statement), functions)
            if found:
                problems.append(f"{rule.prelude}{{{statement}}}: an arbitrary "
                                "value writes a colour; use a palette utility")
    return problems


def _style_values(where: str, text: str) -> list:
    """``(line, attribute, value)`` of every ``STYLE_ATTRIBUTES`` value of a
    file: the start tags of a template, the HTML fragments a Python module
    builds (line 0: an ``ast`` string has no line of its own here)."""
    if where.endswith(".py"):
        return [(0, name, value) for string in _css.python_strings(text)
                for name in STYLE_ATTRIBUTES if name + "=" in string
                for value in _css.fragment_attribute_values(string, name)]
    return [(tag.line, name, value)
            for tag in _css.scan_markup(text).tags
            for name, value in tag.attrs
            if name in STYLE_ATTRIBUTES and value]


def check_markup_literals(files, exempt: dict) -> list:
    """No literal colour in a ``STYLE_ATTRIBUTES`` value, no ``<style>``
    element outside offline.html; Jinja comments aside."""
    problems = []
    for where, text in files:
        if where in exempt:
            continue
        for line, name, value in _style_values(where, text):
            found = literal_colours(value)
            if found:
                problems.append(f"{where}:{line}: {name} writes {found}; use "
                                "a palette class or var(--color-*)")
        if where.endswith(".html") and where not in STYLE_ELEMENT_ALLOWED:
            for tag in _css.scan_markup(text).tags:
                if tag.name == "style":
                    problems.append(f"{where}:{tag.line}: a <style> element")
    known = dict(files)
    for where in sorted(exempt):
        if where not in known:
            problems.append(f"exempt {where} does not exist: remove it")
        elif not any(literal_colours(value)
                     for _, _, value in _style_values(where, known[where])):
            problems.append(f"exempt {where} writes no colour any more: "
                            "remove the exemption")
    return problems


def check_source_literals(source_css: str) -> list:
    """In app.input.css, colours are written only in ``@theme`` blocks and
    the dark token block; every other rule reads ``var(--color-*)``."""
    problems = []
    tree = _css.parse_css(source_css)

    def visit(parent, depth):
        for rule in parent.children:
            squashed = _css.squash(rule.prelude)
            if squashed.startswith("@theme"):
                continue
            if squashed == "@variantdark" and depth == 1 \
                    and _css.squash(parent.prelude) == ":root":
                continue
            for statement in rule.declarations:
                found = literal_colours(_css.blank_strings(statement))
                if found:
                    problems.append(f"{rule.prelude}: {statement}")
            visit(rule, depth + 1)

    for statement in tree.declarations:
        if literal_colours(_css.blank_strings(statement)):
            problems.append(f"top level: {statement}")
    visit(tree, 0)
    return problems


# The colours compiled as literals (@theme inline), the same in every theme:
# the document preview's sheet only, which stays white. Any other would keep
# its light value on a dark page with no remap and no pair measured on it,
# so a new one must be added here deliberately, with its reason.
INLINE_COLORS = {"paper": "#ffffff"}


def check_inline_channel(source_css: str, artifact_css: str,
                         expected: dict) -> list:
    """The source's ``@theme inline`` blocks declare exactly the
    ``--color-*`` of ``expected``, and the artifact's literal colour
    utilities are exactly ``expected`` (``{shade: #rrggbb}``)."""
    problems = []
    declared = sorted(
        name for _, rule in _css.walk(_css.parse_css(source_css))
        if rule.prelude.split()[:1] == ["@theme"]
        and "inline" in rule.prelude.split()[1:]
        for name in _css.declaration_map(rule))
    wanted = sorted("--color-" + shade for shade in expected)
    if declared != wanted:
        problems.append(f"the source's @theme inline blocks declare "
                        f"{declared}, expected {wanted}")
    compiled = {shade: _css.oklab_to_hex(lab) for shade, lab
                in inline_colors_of(_css.parse_css(artifact_css)).items()}
    if compiled != expected:
        problems.append(f"the artifact compiles the literal colours "
                        f"{compiled}, expected {expected}")
    return problems


def test_no_arbitrary_value_utility_compiles_a_colour():
    problems = check_arbitrary_values(_artifact_text())
    assert not problems, "\n".join(problems)
    # Non-vacuous: arbitrary values exist and were read.
    assert "\\[" in _artifact_text()


def test_markup_writes_no_literal_colour():
    files = [(p, t) for p, t in _scan_files()
             if p.endswith(".html") or p.endswith(".py")]
    problems = check_markup_literals(files, EMAIL_BODIES)
    assert not problems, "\n".join(problems)
    assert any(p.startswith(PORTAL_TEMPLATES + "/") for p, _ in files)


def test_the_stylesheet_source_writes_colours_only_as_tokens():
    problems = check_source_literals(_read(_SOURCE))
    assert not problems, "\n".join(problems)


def test_only_paper_compiles_to_a_literal_colour():
    problems = check_inline_channel(_read(_SOURCE), _artifact_text(),
                                    INLINE_COLORS)
    assert not problems, "\n".join(problems)


# ══════════════════════════════════════════════════════════════════════
# 7. The dark: allowlist and the hand-written dark rules
# ══════════════════════════════════════════════════════════════════════

# dark: may only ADD what the remap cannot, never recolour: the owl's
# inversion, and the ring that marks the selected pill of the hearing
# form's forum control (its white-on-gray-50 lift vanishes in dark).
DARK_ALLOWED = frozenset({"dark:invert", "dark:ring-1", "dark:ring-gray-300"})


def dark_utilities_in(text: str) -> set:
    out = set()
    for token in _css.class_tokens(text):
        variants, utility = _css.split_variants(token)
        if "dark" in variants and utility:
            out.add(":".join(variants + (utility,)))
    return out


def check_dark_allowlist(files, artifact_css: str, allowed) -> list:
    """The ``dark:`` utilities of the scan set (comments included: Tailwind
    compiles those too) and of the artifact are exactly ``allowed``."""
    problems = []
    used = defaultdict(set)
    for where, text in files:
        for utility in dark_utilities_in(text):
            used[utility].add(where)
    for utility in sorted(set(used) - set(allowed)):
        problems.append(f"{utility} ({', '.join(sorted(used[utility])[:3])}): "
                        "dark: only adds what the remap cannot, never "
                        "recolours — give the shade a dark value instead")
    for utility in sorted(set(allowed) - set(used)):
        problems.append(f"{utility} is allowed but used nowhere: remove it "
                        "from DARK_ALLOWED")
    compiled = set()
    for _, rule in _css.walk(_css.parse_css(artifact_css)):
        for name in _css.class_names_in_selector(rule.prelude):
            if "dark" in _css.split_variants(name)[0]:
                compiled.add(name)
    for utility in sorted(compiled - set(allowed)):
        problems.append(f"the artifact compiles .{utility}, outside the "
                        "allowlist")
    for utility in sorted((set(allowed) & set(used)) - compiled):
        problems.append(f"{utility} is used but not compiled: recompile the "
                        "stylesheet (CLAUDE.md « Regenerating the stylesheet »)")
    return problems


def test_dark_variant_only_adds_what_the_remap_cannot():
    problems = check_dark_allowlist(_scan_files(), _artifact_text(),
                                    DARK_ALLOWED)
    assert not problems, "\n".join(problems)


# The hand-written dark rules of app.input.css (its `@variant dark` blocks),
# by squashed parent selector, with the properties each sets. The token
# block (None) is the remap itself, held declaration by declaration to its
# compiled branches in section 1. Any other dark rule is a dark: recolour
# by another name (Rule 10): it fails until it is listed here, with its
# reason, and the artifact must compile exactly what the source writes.
TOKEN_BLOCK = ":root"
DARK_BLOCKS = {
    TOKEN_BLOCK: None,
    # Floating surfaces (bg-white + shadow-lg/xl): an inset hairline and a
    # deeper shadow, which no token can give (a 10%-black shadow vanishes on
    # a dark page, and --tw-shadow-color does not inherit).
    ".bg-white:is(.shadow-lg,.shadow-xl)":
        frozenset({"outline", "outline-offset", "--tw-shadow-color"}),
}


def _source_dark_blocks(tree: _css.Rule) -> dict:
    """``{squashed parent selector: (selector, declarations, extras)}`` of
    the ``@variant dark`` blocks of a source tree: the parent selector as
    written, ``{property: value}`` of the blocks' declarations, and what
    else they hold (a nested rule, an at-rule statement)."""
    out = {}
    for ancestors, rule in _css.walk(tree):
        if _css.squash(rule.prelude) != "@variantdark":
            continue
        shown = ancestors[-1].prelude if ancestors else "(top level)"
        _, declarations, extras = out.setdefault(
            _css.squash(shown), (shown, {}, []))
        declarations.update(_css.declaration_map(rule))
        extras.extend(s for s in rule.declarations if s.startswith("@"))
        extras.extend(child.prelude for _, child in _css.walk(rule))
    return out


def check_dark_blocks(source_css: str, artifact_css: str, dark_themes,
                      inventory: dict) -> list:
    """The source's ``@variant dark`` blocks are exactly ``inventory``'s,
    each setting exactly its listed properties; every compiled rule keyed on
    a theme comes from one of them or is a ``dark:`` utility (section 7's
    allowlist); and each listed block other than the token block compiles,
    under every dark theme, to the declarations its source writes (Lightning
    CSS may add a fallback beside one, never change or drop it)."""
    problems = []
    blocks = _source_dark_blocks(_css.parse_css(source_css))
    for parent in sorted(set(blocks) - set(inventory)):
        shown, declarations, _ = blocks[parent]
        problems.append(
            f"{_SOURCE}: a hand-written dark rule for {shown} "
            f"({', '.join(sorted(declarations))}): dark only remaps the "
            "tokens (Rule 10) — give the shade a dark value instead, or list "
            "the rule in DARK_BLOCKS with its reason")
    for parent in sorted(set(inventory) - set(blocks)):
        problems.append(f"DARK_BLOCKS lists {parent}, which {_SOURCE} no "
                        "longer writes: remove it")
    for parent in sorted(set(inventory) & set(blocks)):
        _, declarations, extras = blocks[parent]
        for extra in extras:
            problems.append(f"{parent}: its dark block holds {extra!r}; "
                            "write declarations only")
        allowed = inventory[parent]
        if allowed is None:
            continue
        for name in sorted(set(declarations) - allowed):
            problems.append(f"{parent}: its dark block sets {name}, which "
                            "DARK_BLOCKS does not list")
        for name in sorted(allowed - set(declarations)):
            problems.append(f"DARK_BLOCKS lists {name} for {parent}, which "
                            "its dark block no longer sets: remove it")

    compiled = defaultdict(lambda: defaultdict(set))
    for _, rule, keys in _dark_rules(_css.parse_css(artifact_css)):
        head, sep, _ = _css.squash(rule.prelude).rpartition(
            ":where([data-theme")
        if sep and head in inventory:
            for key in keys:
                for name, value in _css.declaration_map(rule).items():
                    compiled[(head, key)][name].add(_css.squash(value))
        elif not any("dark" in _css.split_variants(name)[0]
                     for name in _css.class_names_in_selector(rule.prelude)):
            problems.append(f"the artifact compiles a dark rule "
                            f"{rule.prelude!r} that no DARK_BLOCKS entry and "
                            "no dark: utility accounts for")
    for parent in sorted(set(inventory) & set(blocks)):
        if inventory[parent] is None:
            continue
        source = {name: _css.squash(value)
                  for name, value in blocks[parent][1].items()}
        for key in sorted(dark_themes):
            got = compiled.get((parent, key), {})
            where = f"[data-theme={key}] {parent}"
            for name in sorted(set(source) - set(got)):
                problems.append(f"{where}: {name} is source only")
            for name in sorted(set(got) - set(source)):
                problems.append(f"{where}: {name} is compiled only")
            for name in sorted(set(source) & set(got)):
                if source[name] not in got[name]:
                    problems.append(f"{where}: {name} compiles to "
                                    f"{sorted(got[name])}, the source says "
                                    f"{source[name]}")
    return problems


def test_hand_written_dark_rules_are_inventoried_and_compiled_as_written():
    problems = check_dark_blocks(_read(_SOURCE), _artifact_text(),
                                 DARK_THEMES, DARK_BLOCKS)
    assert not problems, "\n".join(problems)


# ══════════════════════════════════════════════════════════════════════
# 8. offline.html
# ══════════════════════════════════════════════════════════════════════


def check_offline(html: str, dark: dict) -> list:
    """offline.html follows the device only (precached, and the theme cookie
    is HttpOnly): its color-scheme meta says so, its dark block declares
    token-named properties equal to the dark tokens (±1 per 8-bit channel),
    its rules read only those, and read each of them; outside those token
    declarations the dark block writes no literal colour; and every colour
    a light rule writes, the dark block overrides (same selector, same
    property)."""
    problems = []
    markup = _css.scan_markup(html)
    metas = [dict(tag.attrs) for tag in markup.tags if tag.name == "meta"]
    if not any(m.get("name") == "color-scheme"
               and m.get("content") == "light dark" for m in metas):
        problems.append('<meta name="color-scheme" content="light dark"> '
                        "is missing")
    if len(markup.raw["style"]) != 1:
        return problems + [f"{len(markup.raw['style'])} <style> elements, "
                           "expected 1"]
    tree = _css.parse_css(markup.raw["style"][0])
    blocks = [r for _, r in _css.walk(tree)
              if _css.squash(r.prelude) == "@media(prefers-color-scheme:dark)"]
    if len(blocks) != 1:
        return problems + [f"{len(blocks)} '@media (prefers-color-scheme: "
                           "dark)' blocks, expected 1"]
    declared = {}
    for rule in blocks[0].children:
        if _css.squash(rule.prelude) == ":root":
            declared.update((name, value) for name, value
                            in _css.declaration_map(rule).items()
                            if name.startswith("--color-"))
    if not declared:
        problems.append("the dark block declares no --color-* property")
    for name, value in sorted(declared.items()):
        shade = name[len("--color-"):]
        if shade not in dark:
            problems.append(f"{name} has no dark token to equal")
            continue
        expected = _css.oklab_to_hex(dark[shade])
        try:
            got = _css.hex_channels(value)
        except ValueError:
            problems.append(f"{name}: {value!r} is not a hex colour")
            continue
        if any(abs(g - e) > 1
               for g, e in zip(got, _css.hex_channels(expected))):
            problems.append(f"{name} is {value}; the dark token is {expected}")
    read = set()
    light, overridden = {}, set()
    for ancestors, rule in _css.walk(tree):
        inside = any(a is blocks[0] for a in ancestors)
        is_root = bool(ancestors) and ancestors[-1] is blocks[0] \
            and _css.squash(rule.prelude) == ":root"
        for statement in rule.declarations:
            name = statement.partition(":")[0].strip()
            if is_root and name.startswith("--color-"):
                continue  # a token: held to the dark palette above
            for ref in _css.var_references(statement, "--color-"):
                read.add(ref)
                if not inside:
                    problems.append(f"{rule.prelude}: reads {ref} outside the "
                                    "dark block, where nothing declares it")
                elif ref not in declared:
                    problems.append(f"{rule.prelude}: reads {ref}, which the "
                                    "dark block does not declare")
            found = literal_colours(_css.blank_strings(statement))
            key = (_css.squash(rule.prelude), name)
            if inside:
                overridden.add(key)
                if found:
                    problems.append(f"{rule.prelude}: {statement} writes "
                                    f"{found} in the dark block; read a token")
            elif found and not rule.prelude.startswith("@"):
                light[key] = f"{rule.prelude} {{ {name} }}"
    for name in sorted(set(declared) - read):
        problems.append(f"{name} is declared but no rule of the dark block "
                        "reads it")
    for key in sorted(set(light) - overridden):
        problems.append(f"{light[key]} writes a light colour the dark block "
                        "never overrides")
    return problems


def test_offline_page_dark_block_equals_the_tokens():
    problems = check_offline(_read(_OFFLINE), dark_palette())
    assert not problems, "\n".join(problems)


# ══════════════════════════════════════════════════════════════════════
# Colour math against published values
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("oklch, hex_", [
    # Tailwind v4's published hex values for its oklch palette.
    ("oklch(55.1% .027 264.364)", "#6a7282"),   # gray-500
    ("oklch(21% .034 264.665)", "#101828"),     # gray-900
    ("oklch(51.1% .262 276.966)", "#4f39f6"),   # indigo-600
    ("oklch(57.7% .245 27.325)", "#e7000b"),    # red-600 (clipped)
    ("oklch(98.5% 0 0)", "#fafafa"),
])
def test_oklch_converts_to_the_published_hex(oklch, hex_):
    assert _css.oklab_to_hex(_css.parse_color(oklch)) == hex_


def test_contrast_and_distance_match_wcag_and_oklab():
    black, white = _css.parse_color("#000"), _css.parse_color("white")
    assert abs(_css.contrast(black, white) - 21) < 1e-5
    # WCAG's classic boundary grey: #767676 on white is 4.54:1.
    assert round(_css.contrast(_css.parse_color("#767676"), white), 2) == 4.54
    assert abs(_css.delta_e_ok(black, white) - 1) < 1e-6
    # Out of sRGB, the luminance is the clipped colour's, as an sRGB screen
    # shows it: red-600's equals that of its published (clipped) hex.
    red = _css.parse_color("oklch(57.7% .245 27.325)")
    assert not _css.in_srgb_gamut(red)
    assert abs(_css.relative_luminance(red)
               - _css.relative_luminance(_css.parse_color("#e7000b"))) < 1e-3
    assert _css.in_srgb_gamut(_css.parse_color("#15171c"))


# ══════════════════════════════════════════════════════════════════════
# 9. Every checker catches a synthetic failure
# ══════════════════════════════════════════════════════════════════════

_BRANCHES = (
    ":root:where([data-theme]){color-scheme:light only}"
    "@media screen{:root:where([data-theme=sombre],[data-theme=sombre] *)"
    "{color-scheme:dark;--color-gray-50:oklch(23.5% .01 262)}}"
    "@media screen and (prefers-color-scheme:dark){:root:where("
    "[data-theme=auto],[data-theme=auto] *){color-scheme:dark;"
    "--color-gray-50:oklch(23.5% .01 262)}}"
)
_TWO = frozenset({"auto", "sombre"})
_ONE = frozenset({"auto"})


def test_guard_brace_scanner_skips_comments_and_strings_and_refuses_imbalance():
    tree = _css.parse_css('/* { */ x{content:"}";y:url(data:a;b)}'
                          "@media screen{z{c:d}}")
    assert [r.prelude for _, r in _css.walk(tree)] == ["x", "@media screen", "z"]
    assert tree.children[0].declarations == ['content:"}"', "y:url(data:a;b)"]
    for broken in ("a{b:c", "a{b:c}}", "/* open", 'a{content:"x}'):
        with pytest.raises(ValueError):
            _css.parse_css(broken)


def test_guard_source_artifact_drift_is_caught():
    source = (":root{@variant dark{color-scheme:dark;"
              "--color-gray-50:oklch(23.5% .010 262)}}")
    assert check_source_matches_artifact(source, _BRANCHES, _TWO) == []
    drifted = _BRANCHES.replace("oklch(23.5% .01 262)}}@media",
                                "oklch(24% .01 262)}}@media", 1)
    problems = check_source_matches_artifact(source, drifted, _TWO)
    assert len(problems) == 1 and "data-theme=sombre" in problems[0], problems
    sombre_only = _BRANCHES.split("@media screen and")[0]
    assert any("data-theme=auto" in p for p in
               check_source_matches_artifact(source, sombre_only, _TWO))
    chained = source.replace("oklch(23.5% .010 262)", "var(--color-gray-100)")
    assert any("var()" in p for p in
               check_source_matches_artifact(chained, _BRANCHES, _TWO))
    # A declaration that is not a token, in the block and in both compiled
    # branches alike, still fails: it would restyle every dark element.
    restyled = source.replace("color-scheme:dark;",
                              "color-scheme:dark;color:#e5e7eb;")
    problems = check_source_matches_artifact(
        restyled, _BRANCHES.replace("color-scheme:dark;",
                                    "color-scheme:dark;color:#e5e7eb;"), _TWO)
    assert len(problems) == 1 and "color in the token block is not a token" \
        in problems[0], problems


def test_guard_coverage_catches_new_and_stale_shades():
    assert check_coverage({"gray-50", "black"}, {"gray-50"}, {"black"}) == []
    problems = check_coverage({"gray-50", "pink-500", "black"},
                              {"gray-50", "teal-50"}, {"black", "red-400"})
    assert len(problems) == 3, problems
    assert "pink-500 is used" in problems[0]
    assert "teal-50 has a dark value" in problems[1]
    assert "red-400" in problems[2]
    assert check_coverage({"black"}, {"black"}, {"black"}) != []
    assert referenced_colors(_BRANCHES + ".a{color:var(--color-pink-500)}") \
        == {"pink-500"}
    # A text, surface or neutral shade never joins the signal band.
    problems = check_never_remapped(
        {"black", "red-400", "teal-500", "gray-500", "red-600", "white",
         "slate-400"})
    assert [p.split(":")[0] for p in problems] == [
        "NEVER_REMAPPED lists gray-500", "NEVER_REMAPPED lists red-600",
        "NEVER_REMAPPED lists slate-400", "NEVER_REMAPPED lists white"
    ], problems


@pytest.mark.parametrize("broken, needle", [
    # The light rule after the branches: it would win at equal specificity.
    (_BRANCHES.split("}", 1)[1] + ":root:where([data-theme]){color-scheme:"
     "only light}", "BEFORE"),
    # A bare prefers-color-scheme rule: it would darken the portal.
    (_BRANCHES + "@media (prefers-color-scheme:dark){:root{--color-white:#000}}",
     "would reach the portal"),
    # Tailwind's .dark class strategy.
    (_BRANCHES + ":where(.dark,.dark *){--color-white:#000}", ".dark class"),
    # A branch outside @media screen: print would go dark.
    (_BRANCHES.replace("@media screen{", "@media all{"), "print"),
    # A screen query that also lists print.
    (_BRANCHES.replace("@media screen{", "@media screen,print{", 1), "print"),
    # A key utils/theme.py does not make dark.
    (_BRANCHES.replace("sombre", "nuit"), "keyed on"),
    # The light rule under @media: print keeps the dark meta's scheme.
    ("@media screen{" + _BRANCHES.split("}", 1)[0] + "}}"
     + _BRANCHES.split("}", 1)[1], "inside @media"),
])
def test_guard_scope_catches_each_leak(broken, needle):
    assert check_scope(_BRANCHES, _TWO, _ONE) == []
    problems = check_scope(broken, _TWO, _ONE)
    assert any(needle in p for p in problems), problems


def test_guard_pair_scan_keeps_branches_apart_and_parity_catches_a_drop():
    template = (
        '<p class="p-2 {% if a %}bg-gray-900 text-white{% else %}'
        'bg-white text-gray-900{% endif %}">x</p>'
        '<div class="text-white" :class="err ? \'bg-red-600\' : '
        '\'bg-gray-900\'"></div>'
        '<a class="bg-white text-gray-700 hover:bg-gray-800"></a>')
    shades = {"white", "gray-700", "gray-800", "gray-900", "red-600"}
    pairs, _ = pairs_of(class_sets_of_markup(template, "t.html"), shades, {})
    assert set(pairs) == {("gray-900", "white"), ("white", "gray-900"),
                          ("red-600", "white"), ("white", "gray-700"),
                          ("gray-800", "gray-700")}, set(pairs)
    python = "\n".join((
        'BADGES = {"x": "bg-blue-100 text-blue-700"}',
        "ERROR = '<p class=\"bg-red-50 text-red-600\">'",
    ))
    pairs, _ = pairs_of(class_sets_of_python(python, "m.py"),
                        {"blue-100", "blue-700", "red-50", "red-600"}, {})
    assert set(pairs) == {("blue-100", "blue-700"), ("red-50", "red-600")}

    # A background and a text colour from two dynamic sources of one
    # element still meet: two Jinja ternaries, or an {% if %} and a :class.
    split = (
        "<span class=\"px-2 {{ 'bg-amber-500' if w else 'bg-white' }} "
        "{{ 'text-amber-800' if w else 'text-gray-700' }}\">!</span>\n"
        "<i class=\"px-2 {% if w %}bg-amber-500{% endif %}\" "
        ":class=\"h ? 'text-amber-800' : ''\"></i>")
    pairs, _ = pairs_of(class_sets_of_markup(split, "s.html"),
                        {"amber-500", "amber-800", "white", "gray-700"}, {})
    assert {k: sorted(v) for k, v in pairs.items()} == {
        ("amber-500", "amber-800"): ["s.html:1", "s.html:2"],
        ("amber-500", "gray-700"): ["s.html:1"],
        ("white", "amber-800"): ["s.html:1"],
        ("white", "gray-700"): ["s.html:1"],
    }, dict(pairs)
    # An element whose sources multiply past the cap needs a look.
    seven = " ".join(f"{{{{ 'bg-red-50' if x{k} else '' }}}}"
                     for k in range(7))
    with pytest.raises(ValueError) as caught:
        class_sets_of_markup(f'<p class="{seven}"></p>', "c.html")
    assert "c.html:1: 128 class combinations" in str(caught.value)

    light = {"white": _css.parse_color("#fff"),
             "gray-900": _css.parse_color("#101828")}
    dark = {"white": _css.parse_color("#15171c"),
            "gray-900": _css.parse_color("#1b1e23")}
    problems, checked = check_parity({("gray-900", "white"): {"t.html:1"}},
                                     light.get, dark.get, {})
    assert checked == 1 and len(problems) == 1, problems
    assert problems[0].startswith("bg-gray-900 + text-white: dark 1.0"), problems
    assert problems[0].endswith("t.html:1"), problems
    problems, _ = check_parity({("gray-900", "white"): {"t.html:1"}},
                               light.get, light.get, {("white", "white"): "x"})
    assert problems and "EXEMPT" in problems[0], problems


def test_guard_gamut_and_distinctness_catch_a_collision():
    light, dark = dict(light_palette()), dict(dark_palette())
    assert check_gamut_and_distinctness(light, dark)[0] == []
    dark["orange-600"] = dark["red-600"]
    dark["green-50"] = _css.parse_color("oklch(70% .3 145)")
    problems, _ = check_gamut_and_distinctness(light, dark)
    assert any("green-50" in p and "sRGB" in p for p in problems), problems
    assert any("red-600 / orange-600" in p for p in problems), problems


def test_guard_literal_scans_catch_each_bypass():
    artifact = (".bg-\\[\\#123456\\]{background-color:#123456}"
                ".text-\\[8px\\]{font-size:8px}"
                # A named keyword survives minification when it is shorter.
                ".bg-\\[red\\]{background-color:red}"
                ".ring-\\[tan\\]{--tw-ring-color:tan}")
    problems = check_arbitrary_values(artifact)
    assert [p.split("{")[0] for p in problems] == [
        ".bg-\\[\\#123456\\]", ".bg-\\[red\\]", ".ring-\\[tan\\]"], problems

    files = [
        ("templates/a.html", '<div style="color: #fff">'),
        ("templates/b.html", "<style>p{}</style>"),
        (_OFFLINE, "<style>p{color:#000}</style>"),
        ("templates/c.html", "<div :style=\"'background: rgb(1 2 3)'\"></div>"),
        ("templates/d.html", '<div style="width:5%">'),
        # A Jinja comment is not markup; a width is not a colour; a token
        # named after a keyword is not that keyword.
        ("templates/e.html", '{# a <style> in a comment #}<i style="width:5%">'
                             '<b style="color: var(--color-white)">'),
        ("routes/x.py", "F = '<p style=\"color:#abc\">'"),
        # Named keywords, SVG presentation attributes, a Python :style.
        ("templates/f.html", '<p style="color: White">'),
        ("templates/g.html", '<svg><circle fill="#4f46e5" stroke="none"/>'
                             '<path fill="currentColor"/></svg>'),
        ("templates/h.html", "<rect :fill=\"on ? 'navy' : 'none'\"/>"),
        ("routes/y.py", "H = \"<i :style='color: navy'></i>\""),
    ]
    problems = check_markup_literals(files, {"templates/d.html": "stale"})
    assert [p.split(" writes ")[0] for p in problems] == [
        "templates/a.html:1: style", "templates/b.html:1: a <style> element",
        "templates/c.html:1: :style", "routes/x.py:0: style",
        "templates/f.html:1: style", "templates/g.html:1: fill",
        "templates/h.html:1: :fill", "routes/y.py:0: :style",
        "exempt templates/d.html",
    ], problems
    assert problems[-1] == ("exempt templates/d.html writes no colour any "
                            "more: remove the exemption"), problems

    source = ("@theme{--color-canvas:#FAFAFA}:root{@variant dark{"
              "--color-canvas:oklch(16% .008 262)}}"
              ".ok{background:var(--color-gray-50);font-feature-settings:'#abc'}"
              ".ok2{color:var(--color-white);animation:x 1s ease-in-out}"
              ".bad{border-color:#d1d5db}"
              ".note-content mark{background:yellow}"
              ".note-content a{color:Blue}")
    problems = check_source_literals(source)
    assert [p.split(": ")[0] for p in problems] == [
        ".bad", ".note-content mark", ".note-content a"], problems

    # The keyword scan reads whole identifiers only, in one pass.
    assert _css.named_colors("--color-white bg-red-500 12tan Tan;red") == [
        "Tan", "red"]
    assert len(_css.NAMED_COLORS) == 148
    assert not {"transparent", "currentcolor"} & _css.NAMED_COLORS


def test_guard_dark_allowlist_catches_a_recolouring_variant():
    files = [("t.html", '<p class="dark:bg-gray-900 dark:invert">')]
    artifact = (".dark\\:bg-gray-900:where([data-theme=sombre],"
                "[data-theme=sombre] *){background-color:#000}")
    problems = check_dark_allowlist(files, artifact, {"dark:invert"})
    assert any(p.startswith("dark:bg-gray-900") for p in problems), problems
    assert any("compiles .dark:bg-gray-900" in p for p in problems), problems
    assert any("dark:invert is used but not compiled" in p
               for p in problems), problems
    assert check_dark_allowlist([], "", {"dark:invert"}) == [
        "dark:invert is allowed but used nowhere: remove it from DARK_ALLOWED"]


def test_guard_offline_check_catches_drift():
    dark = {"canvas": _css.parse_color("oklch(16% .008 262)")}
    good = ('<meta name="color-scheme" content="light dark"><style>'
            "body{background:#FAFAFA}@media (prefers-color-scheme: dark){"
            ":root{--color-canvas:#0b0d11}body{background:var(--color-canvas)}}"
            "</style>")
    assert check_offline(good, dark) == []
    assert check_offline(good.replace("#0b0d11", "#0f0d11"), dark) == [
        "--color-canvas is #0f0d11; the dark token is #0b0d11"]
    # One step per channel is rounding, not drift.
    assert check_offline(good.replace("#0b0d11", "#0c0e12"), dark) == []
    assert any("color-scheme" in p for p in check_offline(
        good.replace("light dark", "light"), dark))
    assert any("does not declare" in p for p in check_offline(
        good.replace("var(--color-canvas)", "var(--color-white)"), dark))
    # A literal in the dark block, hex or named, bypasses the tokens.
    literal = good.replace("body{background:var(--color-canvas)}",
                           "body{background:var(--color-canvas);color:#fff}"
                           "a{color:White}")
    assert check_offline(literal, dark) == [
        "body: color:#fff writes ['#fff'] in the dark block; read a token",
        "a: color:White writes ['White'] in the dark block; read a token"]
    # A dropped dark rule leaves its light colour on the dark canvas, and
    # its token unread.
    assert check_offline(good.replace(
        "body{background:var(--color-canvas)}", ""), dark) == [
        "--color-canvas is declared but no rule of the dark block reads it",
        "body { background } writes a light colour the dark block never overrides"]
    # A light colour the dark block never overrides, its token still read.
    uncovered = good.replace("body{background:#FAFAFA}",
                             "body{background:#FAFAFA}p{color:#4b5563}")
    assert check_offline(uncovered, dark) == [
        "p { color } writes a light colour the dark block never overrides"]


_CARD_SOURCE = (":root{@variant dark{color-scheme:dark;"
                "--color-gray-50:oklch(23.5% .010 262)}}"
                ".card{@variant dark{"
                "outline:1px solid var(--color-gray-200)}}")
_CARD_ARTIFACT = _BRANCHES + (
    "@media screen{.card:where([data-theme=sombre],[data-theme=sombre] *)"
    "{outline:1px solid var(--color-gray-200)}}"
    "@media screen and (prefers-color-scheme:dark){.card:where("
    "[data-theme=auto],[data-theme=auto] *){outline:1px solid "
    "var(--color-gray-200)}}")
_CARD_BLOCKS = {":root": None, ".card": frozenset({"outline"})}


def test_guard_dark_blocks_catch_a_hand_written_recolour():
    assert check_dark_blocks(_CARD_SOURCE, _CARD_ARTIFACT, _TWO,
                             _CARD_BLOCKS) == []
    # A new dark recolour, written in the source and compiled.
    link = ".note a{@variant dark{color:var(--color-gray-400)}}"
    compiled = ("@media screen{.note a:where([data-theme=sombre],"
                "[data-theme=sombre] *){color:var(--color-gray-400)}}")
    problems = check_dark_blocks(_CARD_SOURCE + link,
                                 _CARD_ARTIFACT + compiled, _TWO, _CARD_BLOCKS)
    assert len(problems) == 2, problems
    assert problems[0].startswith(
        f"{_SOURCE}: a hand-written dark rule for .note a (color):"), problems
    assert problems[1].startswith(
        "the artifact compiles a dark rule '.note a:where("), problems
    # The compiled side alone (a [data-theme] rule written by hand).
    problems = check_dark_blocks(_CARD_SOURCE, _CARD_ARTIFACT + compiled,
                                 _TWO, _CARD_BLOCKS)
    assert len(problems) == 1 and "compiles a dark rule" in problems[0]
    # An unlisted block, a stale entry, a new property, a nested rule.
    assert any("a hand-written dark rule for .card (outline)" in p
               for p in check_dark_blocks(_CARD_SOURCE, _CARD_ARTIFACT, _TWO,
                                          {":root": None}))
    assert check_dark_blocks(_CARD_SOURCE, _CARD_ARTIFACT, _TWO,
                             {**_CARD_BLOCKS, ".gone": frozenset()}) == [
        f"DARK_BLOCKS lists .gone, which {_SOURCE} no longer writes: "
        "remove it"]
    wider = _CARD_SOURCE.replace("var(--color-gray-200)}",
                                 "var(--color-gray-200);color:red}")
    problems = check_dark_blocks(wider, _CARD_ARTIFACT, _TWO, _CARD_BLOCKS)
    assert ".card: its dark block sets color, which DARK_BLOCKS does not " \
           "list" in problems, problems
    assert check_dark_blocks(_CARD_SOURCE, _CARD_ARTIFACT, _TWO, {
        ":root": None, ".card": frozenset({"outline", "outline-offset"})}) == [
        "DARK_BLOCKS lists outline-offset for .card, which its dark block no "
        "longer sets: remove it"]
    nested = _CARD_SOURCE.replace("var(--color-gray-200)}",
                                  "var(--color-gray-200);a{color:red}}")
    assert ".card: its dark block holds 'a'; write declarations only" in \
        check_dark_blocks(nested, _CARD_ARTIFACT, _TWO, _CARD_BLOCKS)
    # The artifact must compile what the source writes, in every branch.
    drifted = _CARD_ARTIFACT.replace("1px solid var(--color-gray-200)}}@media",
                                     "2px solid var(--color-gray-200)}}@media")
    assert check_dark_blocks(_CARD_SOURCE, drifted, _TWO, _CARD_BLOCKS) == [
        "[data-theme=sombre] .card: outline compiles to "
        "['2pxsolidvar(--color-gray-200)'], the source says "
        "1pxsolidvar(--color-gray-200)"]
    sombre_only = _CARD_ARTIFACT.rsplit("@media screen and", 1)[0]
    assert "[data-theme=auto] .card: outline is source only" in \
        check_dark_blocks(_CARD_SOURCE, sombre_only, _TWO, _CARD_BLOCKS)


def test_guard_token_backgrounds_must_be_paired_or_reasoned():
    source = (".code{background:var(--color-gray-100)}"
              "body{background-color:var(--color-canvas);"
              "color:var(--color-gray-900)}")
    swipe = ("<div :style=\"dx > 0 ? 'transform: translateX(' + dx + 'px); "
             "background: var(--color-green-100)' : ''\"></div>")
    nested = (("gray-100", "gray-700", "code text",
               ((_SOURCE, ".code", "background", "var(--color-gray-100)"),)),
              ("green-100", "gray-900", "swipe", (("t.html", "x"),)))
    unpaired = {("body", "canvas"): "the page"}
    assert check_token_backgrounds(source, [("t.html", swipe)], nested,
                                   unpaired) == []
    # A new hand-written background and a new :style background.
    problems = check_token_backgrounds(
        source + ".note mark{background:var(--color-amber-500)}",
        [("t.html", swipe), ("u.html", "<p :style=\"on ? 'background: "
                                       "var(--color-amber-500)' : ''\"></p>")],
        nested, unpaired)
    assert [p.split(" paints ")[0] for p in problems] == [
        f"{_SOURCE}: .note mark", "u.html: a style"], problems
    # A colour that is not a background needs nothing; a stale entry fails.
    assert check_token_backgrounds(
        source, [("t.html", swipe),
                 ("v.html", '<p style="color: var(--color-amber-500)">')],
        nested, {**unpaired, (".gone", "gray-50"): "x"}) == [
        "UNPAIRED_BACKGROUNDS lists ('.gone', 'gray-50'), which nothing "
        "paints any more: remove it"]


def test_guard_witness_lookup_refuses_a_stale_witness():
    assert _witness_holds(("templates/base.html", "<html"))
    assert not _witness_holds(("templates/base.html", "no-such-needle"))
    code = (_SOURCE, ".note-content code", "background",
            "var(--color-gray-100)")
    assert _witness_holds(code)
    assert not _witness_holds(code[:3] + ("var(--color-gray-200)",))
    assert not _witness_holds((_SOURCE, ".note-content kbd") + code[2:])
    assert _selectors(".a:is(.b, .c), .d") == [".a:is(.b, .c)", ".d"]


def test_guard_palette_classes_outside_class_lists_are_found():
    files = [
        ("t.html", "<script>el.classList.add('bg-red-50')</script>"),
        ("u.html", "<div x-data=\"{c: 'bg-red-600 text-white'}\"></div>"),
        # Class lists are the pair scan's; a size is not a palette colour.
        ("v.html", "<p class=\"bg-red-50 text-red-600\" "
                   ":class=\"x ? 'text-white' : ''\" data-size=\"text-xs\">"),
    ]
    found, scripts = palette_classes_outside_class_lists(
        files, {"red-50", "red-600", "white"})
    assert found == [("t.html <script>", "bg-red-50"),
                     ("u.html:1 x-data", "bg-red-600"),
                     ("u.html:1 x-data", "text-white")], found
    assert scripts == 1


def test_guard_scan_set_refuses_a_source_it_cannot_read():
    base = (_ATHENA / _SOURCE).parent
    good = '@import "tailwindcss" source(none);@source "../../templates";'
    assert scan_set_of(good, base) == ("templates",)
    # An exclusion removes candidates only: the scan set is unchanged.
    excluded = good + '@source not inline("ms-{20,24}");'
    assert scan_set_of(excluded, base) == ("templates",)
    for line in ('@source "../../templates/**/*.html";',
                 '@source not "../../templates/offline.html";',
                 '@source inline("bg-red-500");',
                 '@source "../../main.py";',
                 '@source "../../..";'):
        with pytest.raises(ValueError):
            scan_set_of(good + line, base)
    with pytest.raises(ValueError):
        scan_set_of('@import "tailwindcss" source(none);', base)


def test_guard_inline_channel_catches_a_new_literal_colour():
    source = "@theme inline{--color-paper:#fff}"
    artifact = ("@layer utilities{.bg-paper{background-color:#fff}"
                ".text-gray-900{color:var(--color-gray-900)}}")
    assert check_inline_channel(source, artifact, INLINE_COLORS) == []
    problems = check_inline_channel(
        source.replace("}", ";--color-highlight:#fef08a}"),
        artifact.replace("}}", "}.bg-highlight{background-color:#fef08a}}"),
        INLINE_COLORS)
    assert len(problems) == 2, problems
    assert "'--color-highlight'" in problems[0], problems
    assert "'highlight': '#fef08a'" in problems[1], problems


def test_guard_reported_lines_survive_multi_line_jinja_comments():
    markup = _css.scan_markup(
        '{# one\ntwo\nthree #}\n<p class="a">\n{# x #}<b>')
    assert [tag.line for tag in markup.tags] == [4, 5]
    # The task rows' toggle button sits below a four-line Jinja comment.
    text = _read(_TR)
    start = text.find("<button hx-post=\"{{ url_for('tasks.task_toggle'")
    assert start > 0
    toggle = next(tag for tag in _css.scan_markup(text).tags
                  if tag.name == "button"
                  and "tasks.task_toggle" in (dict(tag.attrs).get("hx-post")
                                              or ""))
    assert toggle.line == text.count("\n", 0, start) + 1
    # Every tag of every template is reported on the line that opens it.
    wrong = []
    for where, text in _scan_files():
        if where.endswith(".html"):
            lines = text.split("\n")
            wrong += [(where, tag.line, tag.name)
                      for tag in _css.scan_markup(text).tags
                      if "<" + tag.name not in lines[tag.line - 1].lower()]
    assert not wrong, wrong[:5]
