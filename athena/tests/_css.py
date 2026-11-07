"""Stdlib helpers for the stylesheet guards (``tests/test_theme_palette.py``).

Three parts, none of which imports Flask, the app or a third-party package:

* **Reading CSS**: the hand-written ``static/src/app.input.css`` and the
  minified Lightning CSS artifact ``static/vendor/app.*.css``. The artifact
  is one long line: attribute values lose their quotes
  (``[data-theme=sombre]``), media features lose their spaces
  (``(prefers-color-scheme:dark)``), numbers lose their leading zero
  (``.01``), colours may be shortened (``#fff``) and rules are regrouped per
  ``@media``. So a rule is found by structure, never by its spelling:
  comments are stripped, braces are matched by a scanner that skips quoted
  strings and backslash escapes, and colours are compared as numbers.
* **Colour math**: CSS Color 4 / Björn Ottosson's OKLab and OKLCH, the sRGB
  transfer function, WCAG 2 relative luminance and contrast, and ΔE_OK; and
  the finders of literal colours (hex, colour functions, named keywords).
* **Reading class lists**: the class sets of the templates (per element and
  per Jinja or Alpine branch) and of the Python string constants that
  Tailwind's scan set compiles.

Every scanner is a single forward pass (``str.find`` loops, or one character
at a time). There is no regular expression in this module, so nothing here
can backtrack (CWE-1333).
"""

import ast
import math
from dataclasses import dataclass, field
from itertools import product
from typing import Iterator

# ══════════════════════════════════════════════════════════════════════
# 1. Reading CSS
# ══════════════════════════════════════════════════════════════════════

_BACKSLASH = "\\"


def _skip_quoted(text: str, i: int) -> int:
    """Index just past the quoted string that opens at ``text[i]``.

    A backslash escapes the next character, as in CSS, Jinja, JavaScript
    and Python string literals."""
    quote = text[i]
    j = i + 1
    n = len(text)
    while j < n:
        c = text[j]
        if c == _BACKSLASH:
            j += 2
        elif c == quote:
            return j + 1
        else:
            j += 1
    raise ValueError(f"unterminated string at offset {i}")


def strip_comments(css: str) -> str:
    """``css`` with each ``/* … */`` comment replaced by one space.

    Quoted strings are skipped whole, so a ``/*`` inside one is not a
    comment, and a brace inside a comment (``app.input.css`` has some)
    never reaches the brace scanner. An unclosed comment raises."""
    out = []
    start = 0
    i = 0
    n = len(css)
    while i < n:
        c = css[i]
        if c == _BACKSLASH:
            i += 2
        elif c in "\"'":
            i = _skip_quoted(css, i)
        elif c == "/" and css.startswith("*", i + 1):
            end = css.find("*/", i + 2)
            if end < 0:
                raise ValueError(f"unclosed comment at offset {i}")
            out.append(css[start:i])
            out.append(" ")
            i = start = end + 2
        else:
            i += 1
    out.append(css[start:])
    return "".join(out)


@dataclass
class Rule:
    """One ``prelude { … }`` block: a selector list or an at-rule.

    ``declarations`` are the ``;``-separated statements directly inside the
    block (``name:value`` declarations, or a block-less at-rule such as
    ``@slot``), stripped. ``index`` is the block's rank in document order
    (the root, which holds the top level, is -1)."""

    prelude: str
    declarations: list = field(default_factory=list)
    children: list = field(default_factory=list)
    index: int = -1


def parse_css(css: str) -> Rule:
    """Parse ``css`` (comments included) into a tree of ``Rule`` blocks.

    One forward pass. Quoted strings and backslash escapes (``.dark\\:invert``,
    ``.bg-\\[\\#fff\\]``) are skipped, so a brace inside them is not
    structural, and a ``;`` inside parentheses (``url(data:…;…)``) does not
    end a declaration. Unbalanced braces raise ``ValueError``."""
    css = strip_comments(css)
    root = Rule("")
    stack = [root]
    start = 0
    depth = 0
    count = 0
    i = 0
    n = len(css)
    while i < n:
        c = css[i]
        if c == _BACKSLASH:
            i += 2
            continue
        if c in "\"'":
            i = _skip_quoted(css, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif c == "{":
            rule = Rule(css[start:i].strip(), index=count)
            count += 1
            stack[-1].children.append(rule)
            stack.append(rule)
            start = i + 1
            depth = 0
        elif c == "}":
            if len(stack) == 1:
                raise ValueError(f"unbalanced '}}' at offset {i}")
            tail = css[start:i].strip()
            if tail:
                stack[-1].declarations.append(tail)
            stack.pop()
            start = i + 1
            depth = 0
        elif c == ";" and depth == 0:
            statement = css[start:i].strip()
            if statement:
                stack[-1].declarations.append(statement)
            start = i + 1
        i += 1
    if len(stack) != 1:
        raise ValueError(f"unclosed '{{' after {stack[-1].prelude!r}")
    tail = css[start:].strip()
    if tail:
        root.declarations.append(tail)
    return root


def walk(rule: Rule, ancestors: tuple = ()) -> Iterator[tuple]:
    """``(ancestors, rule)`` for every block under ``rule``, in document
    order; ``ancestors`` runs from the outermost block to the parent."""
    for child in rule.children:
        yield ancestors, child
        yield from walk(child, ancestors + (child,))


def declaration_map(rule: Rule) -> dict:
    """``{property: value}`` of the block's own declarations (the last one
    of a name wins, as in the cascade). Block-less at-rules are left out."""
    out = {}
    for statement in rule.declarations:
        if statement.startswith("@"):
            continue
        name, sep, value = statement.partition(":")
        if sep:
            out[name.strip()] = value.strip()
    return out


def blank_strings(text: str) -> str:
    """``text`` with the contents of its quoted strings removed (the quotes
    stay): a file name or a font feature never reads as a colour."""
    out = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == _BACKSLASH:
            i += 2
        elif c in "\"'":
            end = _skip_quoted(text, i)
            out.append(text[start:i + 1])
            out.append(c)
            i = start = end
        else:
            i += 1
    out.append(text[start:])
    return "".join(out)


def squash(text: str) -> str:
    """``text`` without any whitespace: ``@media screen and
    (prefers-color-scheme: dark)`` and its minified twin squash alike."""
    return "".join(text.split())


def unquote(value: str) -> str:
    """A CSS value without its surrounding quotes, if it has any."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def attribute_values(selector: str, attribute: str) -> list:
    """The values the selector gives ``[attribute=…]``, quoted or not, in
    order; a bare ``[attribute]`` (presence) yields ``None``."""
    out = []
    needle = "[" + attribute
    i = selector.find(needle)
    while i >= 0:
        j = i + len(needle)
        end = selector.find("]", j)
        if end < 0:
            raise ValueError(f"unclosed attribute selector in {selector!r}")
        rest = selector[j:end].strip()
        if not rest:
            out.append(None)
        elif rest.startswith("="):
            out.append(unquote(rest[1:]))
        # Anything else (``[data-themed]``, ``[attr~=…]``) is another
        # attribute or another operator: not this one's value.
        i = selector.find(needle, end)
    return out


def var_references(text: str, prefix: str = "--") -> list:
    """The custom property names referenced through ``var(`` in ``text``
    that start with ``prefix``, in order (with repeats)."""
    out = []
    i = text.find("var(")
    while i >= 0:
        j = i + 4
        while j < len(text) and text[j].isspace():
            j += 1
        k = j
        while k < len(text) and text[k] not in ",) \t\r\n":
            k += 1
        name = text[j:k]
        if name.startswith(prefix):
            out.append(name)
        i = text.find("var(", k)
    return out


def _is_identifier_char(c: str) -> bool:
    return c.isalnum() or c in "-_"


def property_var_references(text: str, prefix: str = "--") -> list:
    """``(property, name)`` for every ``var(name…)`` of ``text`` whose name
    starts with ``prefix``. ``property`` is the last ``identifier:`` written
    before the reference (lower-cased; ``""`` when none is), so a CSS
    declaration list, or one a JavaScript or Jinja expression builds
    (``'…px); background: var(--color-green-100)'``), attributes each
    reference to its declaration. One forward pass."""
    out = []
    current = ""
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("var(", i) and not (
                i and _is_identifier_char(text[i - 1])):
            j = i + 4
            while j < n and text[j].isspace():
                j += 1
            k = j
            while k < n and text[k] not in ",) \t\r\n":
                k += 1
            if text[j:k].startswith(prefix):
                out.append((current, text[j:k]))
            i = k
        elif _is_identifier_char(text[i]):
            j = i + 1
            while j < n and _is_identifier_char(text[j]):
                j += 1
            k = j
            while k < n and text[k].isspace():
                k += 1
            if k < n and text[k] == ":":
                current = text[i:j].lower()
            i = j
        else:
            i += 1
    return out


def unescape_identifier(text: str) -> str:
    """A CSS identifier with its backslash escapes resolved (``dark\\:invert``
    gives ``dark:invert``). Hex escapes do not occur in Tailwind's class
    selectors and are kept as the escaped character's text."""
    out = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == _BACKSLASH and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def class_names_in_selector(selector: str) -> list:
    """The class names (unescaped) of every ``.class`` in the selector."""
    out = []
    i = 0
    n = len(selector)
    while i < n:
        c = selector[i]
        if c == _BACKSLASH:
            i += 2
            continue
        if c in "\"'":
            i = _skip_quoted(selector, i)
            continue
        if c == ".":
            j = i + 1
            while j < n:
                d = selector[j]
                if d == _BACKSLASH:
                    j += 2
                elif d.isalnum() or d in "-_" or ord(d) > 127:
                    j += 1
                else:
                    break
            if j > i + 1:
                out.append(unescape_identifier(selector[i + 1:j]))
            i = j
            continue
        i += 1
    return out


# ══════════════════════════════════════════════════════════════════════
# 2. Colour math
# ══════════════════════════════════════════════════════════════════════
#
# Colours travel as OKLab triples (L, a, b), L from 0 to 1. Contrast is
# computed the way an sRGB screen shows the colour: linear sRGB is CLIPPED
# to [0, 1] per channel before the WCAG luminance, since a channel outside
# that range cannot be displayed. Tailwind's 400/500 shades and its reds
# and indigos lie outside sRGB (a P3 screen shows them unclipped); the dark
# tokens are kept inside it (``in_srgb_gamut``), so their contrast is the
# same on every screen.

_SRGB_TO_LMS = (
    (0.4122214708, 0.5363325363, 0.0514459929),
    (0.2119034982, 0.6806995451, 0.1073969566),
    (0.0883024619, 0.2817188376, 0.6299787005),
)
_LMS_TO_OKLAB = (
    (0.2104542553, 0.7936177850, -0.0040720468),
    (1.9779984951, -2.4285922050, 0.4505937099),
    (0.0259040371, 0.7827717662, -0.8086757660),
)
_OKLAB_TO_LMS = (
    (1.0, 0.3963377774, 0.2158037573),
    (1.0, -0.1055613458, -0.0638541728),
    (1.0, -0.0894841775, -1.2914855480),
)
_LMS_TO_SRGB = (
    (4.0767416621, -3.3077115913, 0.2309699292),
    (-1.2684380046, 2.6097574011, -0.3413193965),
    (-0.0041960863, -0.7034186147, 1.7076147010),
)


def _mul(matrix, vector) -> tuple:
    return tuple(sum(m * v for m, v in zip(row, vector)) for row in matrix)


def _cbrt(x: float) -> float:
    return math.copysign(abs(x) ** (1.0 / 3.0), x)


def oklch_to_oklab(lightness: float, chroma: float, hue: float) -> tuple:
    """OKLCH (L from 0 to 1, hue in degrees) to OKLab."""
    h = math.radians(hue)
    return (lightness, chroma * math.cos(h), chroma * math.sin(h))


def oklab_to_linear_srgb(lab) -> tuple:
    """OKLab to linear sRGB, unclipped (Ottosson's inverse matrices)."""
    lms = tuple(v ** 3 for v in _mul(_OKLAB_TO_LMS, lab))
    return _mul(_LMS_TO_SRGB, lms)


def linear_srgb_to_oklab(rgb) -> tuple:
    """Linear sRGB to OKLab (Ottosson's forward matrices)."""
    lms = tuple(_cbrt(v) for v in _mul(_SRGB_TO_LMS, rgb))
    return _mul(_LMS_TO_OKLAB, lms)


def srgb_to_linear(v: float) -> float:
    """The sRGB transfer function, decoded (0..1 to 0..1)."""
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def linear_to_srgb(v: float) -> float:
    """The sRGB transfer function, encoded (0..1 to 0..1)."""
    return v * 12.92 if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055


def _clip(v: float) -> float:
    return min(1.0, max(0.0, v))


def hex_to_oklab(text: str) -> tuple:
    """``#rgb``, ``#rgba``, ``#rrggbb`` or ``#rrggbbaa`` to OKLab. An alpha
    below 1 raises: a token is an opaque colour."""
    digits = text.strip().lstrip("#")
    if len(digits) in (3, 4):
        digits = "".join(d * 2 for d in digits)
    if len(digits) not in (6, 8) or any(
            d not in "0123456789abcdefABCDEF" for d in digits):
        raise ValueError(f"not a hex colour: {text!r}")
    if len(digits) == 8 and digits[6:].lower() != "ff":
        raise ValueError(f"translucent colour: {text!r}")
    channels = [int(digits[k:k + 2], 16) / 255 for k in (0, 2, 4)]
    return linear_srgb_to_oklab([srgb_to_linear(c) for c in channels])


def oklab_to_hex(lab) -> str:
    """OKLab to ``#rrggbb``: linear sRGB clipped to [0, 1], encoded, and
    rounded to 8 bits per channel."""
    rgb = oklab_to_linear_srgb(lab)
    return "#" + "".join(
        f"{round(linear_to_srgb(_clip(c)) * 255):02x}" for c in rgb)


def hex_channels(text: str) -> tuple:
    """The three 8-bit channels of an opaque hex colour."""
    digits = text.strip().lstrip("#")
    if len(digits) in (3, 4):
        digits = "".join(d * 2 for d in digits)
    if len(digits) not in (6, 8):
        raise ValueError(f"not a hex colour: {text!r}")
    return tuple(int(digits[k:k + 2], 16) for k in (0, 2, 4))


def _css_number(token: str, percent_scale: float) -> float:
    token = token.strip().lower()
    if token == "none":
        return 0.0
    if token.endswith("%"):
        return float(token[:-1]) / 100 * percent_scale
    if token.endswith("deg"):
        return float(token[:-3])
    return float(token)


_NAMED = {"white": "#fff", "black": "#000"}


def parse_color(text: str) -> tuple:
    """A CSS colour to OKLab: hex, ``oklch(…)``, ``oklab(…)``, or the
    keywords ``white``/``black``. Numbers may be ``.01``-style, lightness a
    percentage or a number. An alpha below 1, or any other form, raises."""
    value = text.strip().lower()
    if value in _NAMED:
        value = _NAMED[value]
    if value.startswith("#"):
        return hex_to_oklab(value)
    for name in ("oklch(", "oklab("):
        if value.startswith(name) and value.endswith(")"):
            body, _, alpha = value[len(name):-1].partition("/")
            if alpha.strip() and _css_number(alpha, 1.0) < 1.0:
                raise ValueError(f"translucent colour: {text!r}")
            parts = body.replace(",", " ").split()
            if len(parts) != 3:
                raise ValueError(f"not a colour: {text!r}")
            lightness = _css_number(parts[0], 1.0)
            if name == "oklch(":
                return oklch_to_oklab(lightness, _css_number(parts[1], 0.4),
                                      _css_number(parts[2], 1.0))
            return (lightness, _css_number(parts[1], 0.4),
                    _css_number(parts[2], 0.4))
    raise ValueError(f"not a colour this helper reads: {text!r}")


def relative_luminance(lab) -> float:
    """WCAG 2 relative luminance of an OKLab colour as an sRGB screen shows
    it: linear sRGB clipped to [0, 1], then 0.2126 R + 0.7152 G + 0.0722 B."""
    r, g, b = (_clip(c) for c in oklab_to_linear_srgb(lab))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(lab1, lab2) -> float:
    """WCAG 2 contrast ratio, (L1 + 0.05) / (L2 + 0.05), lighter first."""
    y1, y2 = sorted((relative_luminance(lab1), relative_luminance(lab2)),
                    reverse=True)
    return (y1 + 0.05) / (y2 + 0.05)


def delta_e_ok(lab1, lab2) -> float:
    """ΔE_OK: the Euclidean distance in OKLab (L from 0 to 1)."""
    return math.dist(lab1, lab2)


def in_srgb_gamut(lab, tolerance: float = 1e-3) -> bool:
    """True when every linear sRGB channel lies in [0, 1] within
    ``tolerance``, so sRGB and P3 screens show the colour alike."""
    return all(-tolerance <= c <= 1 + tolerance
               for c in oklab_to_linear_srgb(lab))


COLOR_FUNCTIONS = ("rgb(", "rgba(", "hsl(", "hsla(", "hwb(", "lab(",
                   "lch(", "oklab(", "oklch(", "color(")


def color_literals(text: str, functions=COLOR_FUNCTIONS) -> list:
    """The literal colours written in ``text``: ``#hex`` (3, 4, 6 or 8 hex
    digits, ending at a non-word character) and the colour functions in
    ``functions`` (case-insensitive, never the tail of a longer name, so
    ``color-mix(`` is not ``color(``). Named keywords are not detected here:
    ``named_colors`` finds them."""
    lowered = text.lower()
    found = []
    i = lowered.find("#")
    while i >= 0:
        j = i + 1
        while j < len(lowered) and lowered[j] in "0123456789abcdef":
            j += 1
        boundary = j >= len(lowered) or not (
            lowered[j].isalnum() or lowered[j] in "_-")
        if j - i - 1 in (3, 4, 6, 8) and boundary:
            found.append(text[i:j])
        i = lowered.find("#", j)
    for name in functions:
        i = lowered.find(name)
        while i >= 0:
            before = lowered[i - 1] if i else " "
            if not (before.isalnum() or before in "-_"):
                found.append(text[i:i + len(name)])
            i = lowered.find(name, i + len(name))
    return found


# The named colours of CSS Color 4 (§6.1, 148 keywords). ``transparent``
# and ``currentcolor`` are left out: neither writes a colour of its own.
NAMED_COLORS = frozenset((
    "aliceblue antiquewhite aqua aquamarine azure beige bisque black "
    "blanchedalmond blue blueviolet brown burlywood cadetblue chartreuse "
    "chocolate coral cornflowerblue cornsilk crimson cyan darkblue "
    "darkcyan darkgoldenrod darkgray darkgreen darkgrey darkkhaki "
    "darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon "
    "darkseagreen darkslateblue darkslategray darkslategrey darkturquoise "
    "darkviolet deeppink deepskyblue dimgray dimgrey dodgerblue firebrick "
    "floralwhite forestgreen fuchsia gainsboro ghostwhite gold goldenrod "
    "gray green greenyellow grey honeydew hotpink indianred indigo ivory "
    "khaki lavender lavenderblush lawngreen lemonchiffon lightblue "
    "lightcoral lightcyan lightgoldenrodyellow lightgray lightgreen "
    "lightgrey lightpink lightsalmon lightseagreen lightskyblue "
    "lightslategray lightslategrey lightsteelblue lightyellow lime "
    "limegreen linen magenta maroon mediumaquamarine mediumblue "
    "mediumorchid mediumpurple mediumseagreen mediumslateblue "
    "mediumspringgreen mediumturquoise mediumvioletred midnightblue "
    "mintcream mistyrose moccasin navajowhite navy oldlace olive "
    "olivedrab orange orangered orchid palegoldenrod palegreen "
    "paleturquoise palevioletred papayawhip peachpuff peru pink plum "
    "powderblue purple rebeccapurple red rosybrown royalblue saddlebrown "
    "salmon sandybrown seagreen seashell sienna silver skyblue slateblue "
    "slategray slategrey snow springgreen steelblue tan teal thistle "
    "tomato turquoise violet wheat white whitesmoke yellow yellowgreen"
).split())


def named_colors(text: str) -> list:
    """The named colours (``NAMED_COLORS``, any case) written in ``text`` as
    whole identifiers, in order. An identifier is a maximal run of letters,
    digits, ``-`` and ``_``, so ``--color-white``, ``bg-red-500`` and
    ``12tan`` each read as one identifier, never as ``white``, ``red`` or
    ``tan``. One forward pass."""
    found = []
    i = 0
    n = len(text)
    while i < n:
        if _is_identifier_char(text[i]):
            j = i + 1
            while j < n and _is_identifier_char(text[j]):
                j += 1
            if text[i:j].lower() in NAMED_COLORS:
                found.append(text[i:j])
            i = j
        else:
            i += 1
    return found


# ══════════════════════════════════════════════════════════════════════
# 3. Reading class lists
# ══════════════════════════════════════════════════════════════════════


def strip_jinja_comments(text: str) -> str:
    """``text`` without its ``{# … #}`` comments: Jinja removes them before
    anything else, whatever they contain. Each comment leaves the newlines
    it held, so every line after it keeps its number. An unclosed comment
    raises."""
    out = []
    start = 0
    i = text.find("{#")
    while i >= 0:
        end = text.find("#}", i + 2)
        if end < 0:
            raise ValueError(f"unclosed Jinja comment at offset {i}")
        out.append(text[start:i])
        out.append("\n" * text.count("\n", i, end + 2))
        start = end + 2
        i = text.find("{#", start)
    out.append(text[start:])
    return "".join(out)


def _is_jinja_open(text: str, i: int) -> bool:
    return text.startswith("{{", i) or text.startswith("{%", i)


def jinja_end(text: str, i: int) -> int:
    """Index just past the ``{{ … }}`` or ``{% … %}`` block opening at
    ``text[i]``; string literals inside the block are skipped whole."""
    close = "}}" if text[i + 1] == "{" else "%}"
    j = i + 2
    n = len(text)
    while j < n:
        c = text[j]
        if c in "\"'":
            j = _skip_quoted(text, j)
        elif c == close[0] and text.startswith(close, j):
            return j + 2
        else:
            j += 1
    raise ValueError(f"unclosed Jinja block at offset {i}")


def string_literals(code: str) -> list:
    """The contents of the quoted string literals in a Jinja, JavaScript or
    Python expression, in order (``\\'`` unescaped). Backtick literals count,
    their ``${…}`` parts read as a space."""
    out = []
    i = 0
    n = len(code)
    while i < n:
        c = code[i]
        if c in "\"'`":
            end = _skip_quoted(code, i)
            body = code[i + 1:end - 1]
            if c == "`":
                body = _blank_template_parts(body)
            out.append(_unescape(body))
            i = end
        else:
            i += 1
    return out


def _blank_template_parts(body: str) -> str:
    out = []
    start = 0
    i = body.find("${")
    while i >= 0:
        end = body.find("}", i)
        if end < 0:
            break
        out.append(body[start:i])
        out.append(" ")
        start = end + 1
        i = body.find("${", start)
    out.append(body[start:])
    return "".join(out)


def _unescape(body: str) -> str:
    out = []
    i = 0
    while i < len(body):
        if body[i] == _BACKSLASH and i + 1 < len(body):
            out.append(body[i + 1])
            i += 2
        else:
            out.append(body[i])
            i += 1
    return "".join(out)


def identifiers(code: str) -> set:
    """The names (``[A-Za-z_][A-Za-z0-9_]*``) of an expression, string
    literals excluded."""
    names = set()
    i = 0
    n = len(code)
    while i < n:
        c = code[i]
        if c in "\"'`":
            i = _skip_quoted(code, i)
        elif c.isalpha() or c == "_":
            j = i + 1
            while j < n and (code[j].isalnum() or code[j] == "_"):
                j += 1
            names.add(code[i:j])
            i = j
        elif c.isdigit():
            j = i + 1
            while j < n and (code[j].isalnum() or code[j] in "._"):
                j += 1
            i = j
        else:
            i += 1
    return names


@dataclass
class Tag:
    """A start tag: its lower-cased name, its attributes as ``(name,
    value)`` pairs in order (``value`` keeps its Jinja; ``None`` for a bare
    attribute) and its 1-based line."""

    name: str
    attrs: list
    line: int

    def values(self, *names) -> list:
        return [v for k, v in self.attrs if k in names and v is not None]


@dataclass
class Markup:
    """What ``scan_markup`` reads: the start tags in document order, the
    Jinja blocks outside tags, and the bodies of ``<script>`` and
    ``<style>`` elements."""

    tags: list
    jinja: list
    raw: dict


def _quoted_value_end(text: str, j: int, quote: str) -> int:
    """Index of the closing ``quote`` of an attribute value starting at
    ``j``, skipping Jinja blocks (a Jinja string may hold the same quote)."""
    q = text.find(quote, j)
    while True:
        if q < 0:
            raise ValueError(f"unterminated attribute value at offset {j}")
        b = text.find("{", j, q)
        while b >= 0 and not _is_jinja_open(text, b):
            b = text.find("{", b + 1, q)
        if b < 0:
            return q
        j = jinja_end(text, b)
        if j > q:
            q = text.find(quote, j)


def _parse_tag(text: str, p: int, line: int) -> tuple:
    n = len(text)
    k = p + 1
    while k < n and (text[k].isalnum() or text[k] in "-:"):
        k += 1
    name = text[p + 1:k].lower()
    attrs = []
    while True:
        while k < n and text[k].isspace():
            k += 1
        if k >= n:
            raise ValueError(f"unterminated <{name}> tag at line {line}")
        c = text[k]
        if c == ">":
            return Tag(name, attrs, line), k + 1
        if text.startswith("/>", k):
            return Tag(name, attrs, line), k + 2
        if _is_jinja_open(text, k):
            k = jinja_end(text, k)
            continue
        s = k
        while k < n:
            d = text[k]
            if d.isspace() or d in "=>" or text.startswith("/>", k) \
                    or (d == "{" and _is_jinja_open(text, k)):
                break
            k += 1
        attr = text[s:k].lower()
        if not attr:
            k += 1
            continue
        t = k
        while t < n and text[t].isspace():
            t += 1
        if t >= n or text[t] != "=":
            attrs.append((attr, None))
            continue
        t += 1
        while t < n and text[t].isspace():
            t += 1
        if t < n and text[t] in "\"'":
            end = _quoted_value_end(text, t + 1, text[t])
            attrs.append((attr, text[t + 1:end]))
            k = end + 1
        else:
            j = t
            while j < n and not text[j].isspace() and text[j] != ">":
                j = jinja_end(text, j) if _is_jinja_open(text, j) else j + 1
            attrs.append((attr, text[t:j]))
            k = j


def scan_markup(text: str) -> Markup:
    """Read a Jinja/HTML template, Jinja comments stripped first.

    One forward pass over the text: a Jinja block outside a tag is kept
    whole (``<`` inside it is not markup), ``<!-- … -->``, end tags and
    doctypes are skipped, each start tag is parsed into its attributes
    (quoted values may hold Jinja, which may hold the same quote), and a
    ``<script>`` or ``<style>`` body is kept raw, never read as markup."""
    text = strip_jinja_comments(text)
    tags, jinja = [], []
    raw = {"script": [], "style": []}
    n = len(text)
    i = 0
    line = 1
    counted = 0
    next_lt = text.find("<")
    next_brace = text.find("{")
    while True:
        if 0 <= next_lt < i:
            next_lt = text.find("<", i)
        if 0 <= next_brace < i:
            next_brace = text.find("{", i)
        candidates = [p for p in (next_lt, next_brace) if p >= 0]
        if not candidates:
            break
        p = min(candidates)
        if p == next_brace:
            if _is_jinja_open(text, p):
                end = jinja_end(text, p)
                jinja.append(text[p:end])
                i = end
            else:
                i = p + 1
            continue
        line += text.count("\n", counted, p)
        counted = p
        if text.startswith("<!--", p):
            end = text.find("-->", p + 4)
            i = n if end < 0 else end + 3
            continue
        following = text[p + 1:p + 2]
        if following in ("/", "!"):
            end = text.find(">", p)
            i = n if end < 0 else end + 1
            continue
        if not following.isalpha():
            i = p + 1
            continue
        tag, i = _parse_tag(text, p, line)
        tags.append(tag)
        if tag.name in raw:
            close = text.find("</" + tag.name, i)
            if close < 0:
                raise ValueError(f"unclosed <{tag.name}> at line {line}")
            raw[tag.name].append(text[i:close])
            i = close
    return Markup(tags, jinja, raw)


def jinja_segments(value: str) -> list:
    """An attribute value cut into ``("text", …)``, ``("expr", …)`` and
    ``("stmt", …)`` segments; Jinja blocks keep their delimiters stripped
    (and their ``-``/``+`` whitespace markers)."""
    out = []
    start = 0
    i = value.find("{")
    while i >= 0:
        if _is_jinja_open(value, i):
            if i > start:
                out.append(("text", value[start:i]))
            end = jinja_end(value, i)
            kind = "expr" if value[i + 1] == "{" else "stmt"
            out.append((kind, value[i + 2:end - 2].strip("-+ \t\r\n")))
            start = end
            i = value.find("{", start)
        else:
            i = value.find("{", i + 1)
    if start < len(value):
        out.append(("text", value[start:]))
    return out


def jinja_set_values(blocks) -> dict:
    """``{name: [string literals]}`` of the ``{% set name = … %}`` blocks
    among ``blocks`` (raw ``{% … %}`` texts). A name set twice keeps the
    literals of both: the scanner cannot tell which one is in scope."""
    out = {}
    for block in blocks:
        if not block.startswith("{%"):
            continue
        body = block[2:-2].strip("-+ \t\r\n")
        if not body.startswith("set ") and not body.startswith("set\t"):
            continue
        target, sep, expr = body[4:].partition("=")
        name = target.strip()
        if not sep or not name.isidentifier():
            continue
        out.setdefault(name, []).extend(string_literals(expr))
    return out


def _word(code: str, word: str) -> bool:
    return word in identifiers(code)


def expression_alternatives(expr: str, names: dict) -> tuple:
    """``(alternatives, always)`` of a Jinja expression used as classes.

    The alternatives are its string literals plus the literals of every
    ``{% set %}`` name it reads (a map's values, a string's value). An
    ``if`` without ``else`` adds the empty alternative. ``always`` is true
    when the expression can only produce its single alternative (``{{
    tab_base }}``), which then belongs to the element's static classes."""
    alternatives = list(string_literals(expr))
    for name in sorted(identifiers(expr) & set(names)):
        alternatives.extend(names[name])
    conditional = _word(expr, "if")
    if conditional and not _word(expr, "else"):
        alternatives.append("")
    if not alternatives:
        return [""], True
    return alternatives, (len(alternatives) == 1 and not conditional)


def _branch_alternatives(segments: list, names: dict, cap: int) -> list:
    """Every class string one pass through ``segments`` can produce: the
    product of each nested ``{% if %}`` chain and multi-valued expression.
    More than ``cap`` combinations raise: the attribute needs a look."""
    combos = [""]
    i = 0
    while i < len(segments):
        kind, body = segments[i]
        if kind == "text":
            options = [body]
            i += 1
        elif kind == "expr":
            options = expression_alternatives(body, names)[0]
            i += 1
        else:
            keyword = (body.split() or [""])[0]
            if keyword != "if":
                i += 1
                continue
            options, i = _if_chain(segments, i, names, cap)
        combos = [c + " " + o for c, o in product(combos, options)]
        if len(combos) > cap:
            raise ValueError(f"more than {cap} class combinations")
    return combos


def _if_chain(segments: list, i: int, names: dict, cap: int) -> tuple:
    """The alternatives of the ``{% if %}`` chain starting at
    ``segments[i]``, and the index just past its ``{% endif %}``."""
    branches = [[]]
    has_else = False
    depth = 0
    i += 1
    while i < len(segments):
        kind, body = segments[i]
        keyword = (body.split() or [""])[0] if kind == "stmt" else ""
        if keyword == "if":
            depth += 1
        elif keyword == "endif":
            if depth == 0:
                break
            depth -= 1
        elif keyword in ("elif", "else") and depth == 0:
            has_else = has_else or keyword == "else"
            branches.append([])
            i += 1
            continue
        branches[-1].append(segments[i])
        i += 1
    else:
        raise ValueError("{% if %} without {% endif %} in a class attribute")
    options = []
    for branch in branches:
        options.extend(_branch_alternatives(branch, names, cap))
    if not has_else:
        options.append("")
    return options, i + 1


def class_value_sets(value: str, names: dict, cap: int = 64) -> tuple:
    """``(static, groups)`` of a ``class`` attribute value.

    ``static`` is the class text every rendering carries: the text outside
    any Jinja branch, plus the expressions that can only produce one value.
    ``groups`` lists, per Jinja branch point (an ``{% if %}`` chain, or a
    multi-valued expression such as a ternary or a map lookup), the class
    strings it can add, one of which applies at a time."""
    static = []
    groups = []
    segments = jinja_segments(value)
    i = 0
    while i < len(segments):
        kind, body = segments[i]
        if kind == "text":
            static.append(body)
            i += 1
        elif kind == "expr":
            alternatives, always = expression_alternatives(body, names)
            if always:
                static.append(alternatives[0])
            else:
                groups.append(alternatives)
            i += 1
        else:
            keyword = (body.split() or [""])[0]
            if keyword == "if":
                options, i = _if_chain(segments, i, names, cap)
                groups.append(options)
            else:
                i += 1
    return " ".join(static), groups


def alpine_class_literals(value: str, names: dict) -> list:
    """The class strings an Alpine ``:class`` expression can add: each of
    its quoted literals, with the Jinja inside a literal resolved through
    ``names`` (``'{{ tab_active }}'``)."""
    out = []
    for literal in string_literals(value):
        static, groups = class_value_sets(literal, names)
        out.append(static)
        for group in groups:
            out.extend(static + " " + alt for alt in group)
    return out


def fragment_attribute_values(text: str, attribute: str = "class") -> list:
    """The values of every ``attribute="…"`` / ``attribute='…'`` written in
    a string (an HTML fragment built in Python), never the tail of a longer
    name (``:class``, ``data-class``). A value cut off by the end of the
    string runs to its end."""
    out = []
    for quote in "\"'":
        needle = attribute + "=" + quote
        i = text.find(needle)
        while i >= 0:
            before = text[i - 1] if i else " "
            start = i + len(needle)
            end = text.find(quote, start)
            if end < 0:
                end = len(text)
            if not (before.isalnum() or before in "-_:@."):
                out.append(text[start:end])
            i = text.find(needle, end)
    return out


def python_strings(source: str) -> list:
    """Every string constant of a Python module, read with ``ast`` (never
    imported). An f-string is rebuilt with each placeholder as a space; its
    literal parts are not repeated on their own."""
    tree = ast.parse(source)
    parts = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts.update(id(v) for v in node.values)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in parts:
                out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            out.append("".join(
                v.value if isinstance(v, ast.Constant) else " "
                for v in node.values))
    return out


# Every ASCII character that cannot occur in a palette class token, plus
# the typographic punctuation of French prose and the no-break spaces,
# reads as a separator.
_TOKEN_SEPARATORS = str.maketrans({
    c: " " for c in (
        [chr(k) for k in range(128)
         if not (chr(k).isalnum() or chr(k) in "-:/[]_!#%")]
        + [chr(k) for k in (0xA0, 0xAB, 0xBB, 0x2013, 0x2014, 0x2019,
                            0x201C, 0x201D, 0x2026, 0x202F)]
    )
})


def class_tokens(text: str) -> list:
    """The class-like tokens of ``text``: maximal runs of letters, digits
    and ``-:/[]_!#%``. Quotes, braces, parentheses, ``=``, ``<``, ``>``,
    commas, dots and whitespace separate them."""
    return text.translate(_TOKEN_SEPARATORS).split()


def split_variants(token: str) -> tuple:
    """``("hover", "md")``-style variant tuple and the utility of a class
    token (``md:hover:bg-gray-50`` gives ``(("md", "hover"), "bg-gray-50")``);
    the ``!`` important marker is dropped."""
    parts = token.split(":")
    return tuple(parts[:-1]), parts[-1].strip("!")
