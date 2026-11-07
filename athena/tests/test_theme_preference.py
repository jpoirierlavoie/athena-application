"""« Affichage » — the per-device display theme, from its words to its cookie.

Plan §4 « tests/test_theme_preference.py » of the dark mode:

* the vocabulary of ``utils/theme.py`` and its exact-string parser;
* ``routes/_helpers.theme_context``: no request, each cookie, forged ones;
* ``<html data-theme>`` and ``<meta name="color-scheme">`` as ``base.html``
  and ``auth/login.html`` render them, and their absence where the processor
  is not registered and in the portal's base, which stays light;
* the two routes of the « Affichage » tab: guards, method inventory,
  refusals, the cookie's attributes, and the cookie's survival at logout;
* the registration in ``main.py`` (on its syntax tree) and the page source.

Every app here is a BARE Flask app, the ``_app()`` pattern of
``tests/test_settings_routes.py``, that registers ``theme_context`` itself
before its first request (Flask refuses a context processor after it).
``main`` is never imported: ``test_main_registers_the_theme_processor``
says why.

Three traps the assertions step around:

* ``base.html`` calls ``csrf_token()``, which stores a token in the session,
  so most responses here carry ``Set-Cookie: session=…``. « No theme
  cookie » is checked on the theme cookie, picked by NAME, never as « no
  Set-Cookie ».
* Jinja autoescapes the apostrophe of the refusal sentence: the body holds
  ``d&#39;affichage``, asserted verbatim.
* Some refused values are substrings of what the page shows anyway
  (« Sombre » is a label, « sombr » begins every ``value="sombre"`` and
  `` sombre`` sits in two descriptions), so « never echoed » is COUNTED
  against the plain page: a refusal may show a value only as often as the
  GET page plus the refusal sentence do. The per-request CSRF token and CSP
  nonce are neutralised first, since a random token can contain « 42 ».

Each checker returns a list of problems and is run once on a synthetic
failing input (the ``test_checker_*`` tests): a guard that cannot fail
proves nothing.
"""

import ast
import io
import logging
import os
import sys
import textwrap
import warnings
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.cookies import SimpleCookie
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest
from markupsafe import escape

_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ATHENA)

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import routes.auth_routes as ra
    import routes.settings as rs

from flask import Flask, has_request_context, request  # noqa: E402
from werkzeug.wrappers import Response  # noqa: E402

from routes._helpers import theme_context  # noqa: E402
from security import init_security  # noqa: E402
from utils.theme import (  # noqa: E402
    COLOR_SCHEMES,
    DEFAULT_THEME,
    DESCRIPTIONS,
    LABELS,
    THEME_COOKIE,
    THEME_COOKIE_MAX_AGE,
    VALID_THEMES,
    parse_theme,
    theme_from,
)

PAGE = "/parametres/affichage"
SAVE = "/parametres/affichage/enregistrer"
LOGIN = "/auth/login"

SAVED = "Affichage enregistré pour cet appareil."
REFUSAL = (
    "Ce choix d'affichage est inconnu : choisissez Automatique, Clair ou Sombre."
)
# The same sentence as the body carries it: Jinja's autoescape writes the
# apostrophe as a numeric character reference.
REFUSAL_HTML = (
    "Ce choix d&#39;affichage est inconnu : choisissez Automatique, Clair ou Sombre."
)

CRLF = chr(13) + chr(10)  # what a textarea, or a hand-made POST, appends

# parse_theme refuses each one: markup, padding, case, a trailing CRLF, a
# prefix, the empty string and two non-strings.
REFUSED = [
    pytest.param("<b>sombre</b>", id="markup"),
    pytest.param(" sombre", id="leading-space"),
    pytest.param("Sombre", id="capitalised"),
    pytest.param("sombre" + CRLF, id="trailing-crlf"),
    pytest.param("sombr", id="prefix"),
    pytest.param("", id="empty"),
    pytest.param(None, id="none"),
    pytest.param(42, id="int"),
]

# The same values as a browser can post them: None is a missing field, and
# 42 arrives as the text « 42 ».
POSTED_REFUSALS = [
    pytest.param("<b>sombre</b>", id="markup"),
    pytest.param(" sombre", id="leading-space"),
    pytest.param("Sombre", id="capitalised"),
    pytest.param("sombre" + CRLF, id="trailing-crlf"),
    pytest.param("sombr", id="prefix"),
    pytest.param("", id="empty"),
    pytest.param("42", id="number"),
    pytest.param(None, id="missing-field"),
]

FORGED_COOKIES = [
    pytest.param("SOMBRE", id="upper-case"),
    pytest.param("", id="empty"),
    pytest.param("x" * 5000, id="oversized"),
]


# ── Apps and clients ─────────────────────────────────────────────────────

def _app(*, theme=True, csrf_on=False, secure=None) -> Flask:
    """The bare app of ``tests/test_settings_routes.py``, plus what this
    feature needs: the theme processor (unless ``theme=False``) and the
    reCAPTCHA key that ``auth/login.html`` renders through ``|tojson``."""
    from utils.icons import ms as _ms

    app = Flask(__name__, template_folder=os.path.join(_ATHENA, "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["REQUIRE_MFA"] = True
    app.config["AUTHORIZED_USER_EMAIL"] = "test@example.com"
    app.config["RATELIMIT_ENABLED"] = False
    app.config["WTF_CSRF_ENABLED"] = csrf_on
    # login.html renders `config.RECAPTCHA_ENTERPRISE_SITE_KEY|tojson`
    # unconditionally, and `tojson` raises on Undefined; "" is what Config
    # holds when the key is unset.
    app.config["RECAPTCHA_ENTERPRISE_SITE_KEY"] = ""
    if secure is not None:
        app.config["SESSION_COOKIE_SECURE"] = secure
    init_security(app)
    app.jinja_env.globals["ms"] = _ms
    app.jinja_env.globals["firebase_api_key"] = "clef-test"
    app.jinja_env.globals["firebase_project_id"] = "test-project"
    app.jinja_env.globals["firebase_app_id"] = "1:2:web:3"
    app.register_blueprint(rs.settings_bp)
    app.register_blueprint(ra.auth_bp)
    if theme:
        # main.py's registration, made before the first request.
        app.context_processor(theme_context)
    return app


def _login(client):
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


@pytest.fixture()
def web():
    return _login(_app().test_client())


@pytest.fixture()
def anon():
    return _app().test_client()


def _text(response) -> str:
    return response.data.decode("utf-8")


def _set_theme_cookie(client, value: str) -> None:
    """``client.set_cookie``, quiet about the oversized forged value:
    Werkzeug warns past 4093 bytes, and that size is the point."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        client.set_cookie(THEME_COOKIE, value)


def _defaults() -> dict:
    return {
        "ui_theme": DEFAULT_THEME,
        "ui_color_scheme": COLOR_SCHEMES[DEFAULT_THEME],
    }


def _read(*parts: str) -> str:
    with io.open(os.path.join(_ATHENA, *parts), encoding="utf-8") as fh:
        return fh.read()


# ── Checkers (each one is proven on a failing input at the end) ─────────

class _Tags(HTMLParser):
    """Every start tag in document order, as ``(tag, attrs, form)``: the
    raw attribute list (a duplicate shows twice) and the ``action`` of the
    ``<form>`` the tag sits in (None outside any form)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list = []
        self._form = None

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            self._form = dict(attrs).get("action") or ""
        self.tags.append((tag, attrs, self._form))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None


def _tags(body: str) -> list:
    parser = _Tags()
    parser.feed(body)
    parser.close()
    return parser.tags


def _values(attrs, name: str) -> list[str]:
    """Every value of attribute ``name``; a valueless one reads as ""."""
    return [value or "" for key, value in attrs if key == name]


def _color_scheme_metas(tags: list) -> list:
    return [
        (position, attrs)
        for position, (tag, attrs, _form) in enumerate(tags)
        if tag == "meta"
        and [v.lower() for v in _values(attrs, "name")] == ["color-scheme"]
    ]


def _theme_markup_problems(body: str, theme: str) -> list[str]:
    """Why ``body`` does not render ``theme``: one ``data-theme`` on
    ``<html>`` and on nothing else, one color-scheme meta with the theme's
    value, and that meta BEFORE the stylesheet — it sets the browser's
    canvas before the CSS applies."""
    tags = _tags(body)
    html = [attrs for tag, attrs, _form in tags if tag == "html"]
    if len(html) != 1:
        return [f"expected one <html> start tag, found {len(html)}"]
    problems = []
    on_html = _values(html[0], "data-theme")
    if on_html != [theme]:
        problems.append(f"<html> data-theme values {on_html}, expected [{theme!r}]")
    elsewhere = sorted(
        {tag for tag, attrs, _form in tags
         if tag != "html" and _values(attrs, "data-theme")}
    )
    if elsewhere:
        problems.append(f"data-theme also on {elsewhere}: only <html> carries it")
    metas = _color_scheme_metas(tags)
    if len(metas) != 1:
        problems.append(f"expected one color-scheme meta, found {len(metas)}")
        return problems
    position, attrs = metas[0]
    content = _values(attrs, "content")
    if content != [COLOR_SCHEMES[theme]]:
        problems.append(
            f"color-scheme content {content}, expected "
            f"[{COLOR_SCHEMES[theme]!r}] for {theme!r}"
        )
    sheets = [
        i for i, (tag, attrs, _form) in enumerate(tags)
        if tag == "link"
        and "stylesheet" in " ".join(_values(attrs, "rel")).lower().split()
    ]
    if not sheets:
        problems.append("no <link rel=stylesheet> to order the meta against")
    elif position > sheets[0]:
        problems.append(
            "the color-scheme meta comes AFTER the stylesheet link: it must "
            "set the canvas before the CSS loads (no white flash)"
        )
    return problems


def _no_theme_markup_problems(body: str) -> list[str]:
    """Why ``body`` is not theme-free: no ``data-theme`` anywhere and no
    color-scheme meta — the light rendering an absent attribute means."""
    tags = _tags(body)
    problems = []
    if "data-theme" in body:
        carriers = sorted(
            {tag for tag, attrs, _form in tags if _values(attrs, "data-theme")}
        )
        problems.append(f"data-theme in the body (tags: {carriers or 'none, text'})")
    metas = _color_scheme_metas(tags)
    if metas:
        problems.append(f"{len(metas)} color-scheme meta(s) rendered")
    return problems


def _portal_problems(source: str) -> list[str]:
    """Why a portal template could darken: a ``data-theme`` anywhere, or a
    color-scheme meta that admits dark (the portal has no dark tokens, so
    the browser would draw dark controls and canvas on a light page)."""
    problems = []
    if "data-theme" in source:
        problems.append("data-theme in a portal template")
    for _position, attrs in _color_scheme_metas(_tags(source)):
        if any("dark" in value.split() for value in _values(attrs, "content")):
            problems.append(f"color-scheme meta {_values(attrs, 'content')} admits dark")
    return problems


def _button_problems(body: str, current: str) -> list[str]:
    """Why the theme buttons are not VALID_THEMES in order, ``current`` the
    only one pressed, each a submit of the one POST form to the save route,
    which carries a hidden, non-empty ``csrf_token``."""
    tags = _tags(body)
    buttons = [
        (attrs, form) for tag, attrs, form in tags
        if tag == "button" and _values(attrs, "name") == ["theme"]
    ]
    problems = []
    values = [" ".join(_values(attrs, "value")) for attrs, _form in buttons]
    if values != list(VALID_THEMES):
        problems.append(f"theme button values {values}, expected {list(VALID_THEMES)}")
    pressed = [
        " ".join(_values(attrs, "value")) for attrs, _form in buttons
        if _values(attrs, "aria-pressed") == ["true"]
    ]
    if pressed != [current]:
        problems.append(f"aria-pressed=true on {pressed}, expected [{current!r}]")
    states = sorted(
        {" ".join(_values(attrs, "aria-pressed")) for attrs, _form in buttons}
        - {"true", "false"}
    )
    if states:
        problems.append(f"aria-pressed values {states}: each button says true or false")
    not_submit = [
        " ".join(_values(attrs, "value")) for attrs, _form in buttons
        if _values(attrs, "type") != ["submit"]
    ]
    if not_submit:
        problems.append(f"buttons {not_submit} are not type=submit: one tap must apply")
    outside = [
        " ".join(_values(attrs, "value")) for attrs, form in buttons if form != SAVE
    ]
    if outside:
        problems.append(f"buttons {outside} do not submit the form posting to {SAVE}")
    forms = [
        attrs for tag, attrs, _form in tags
        if tag == "form" and _values(attrs, "action") == [SAVE]
    ]
    if len(forms) != 1:
        problems.append(f"expected one form posting to {SAVE}, found {len(forms)}")
    elif [m.lower() for m in _values(forms[0], "method")] != ["post"]:
        problems.append(f"the save form's method is {_values(forms[0], 'method')}, not post")
    tokens = [
        attrs for tag, attrs, form in tags
        if tag == "input" and form == SAVE and _values(attrs, "name") == ["csrf_token"]
    ]
    if (
        len(tokens) != 1
        or _values(tokens[0], "type") != ["hidden"]
        or _values(tokens[0], "value") in ([], [""])
    ):
        problems.append("the save form must carry one hidden, non-empty csrf_token")
    return problems


def _form_csrf_token(body: str) -> str:
    """The token the save form carries (the page's own)."""
    tokens = [
        " ".join(_values(attrs, "value")) for tag, attrs, form in _tags(body)
        if tag == "input" and form == SAVE and _values(attrs, "name") == ["csrf_token"]
    ]
    assert len(tokens) == 1 and tokens[0], f"the save form's csrf_token: {tokens}"
    return tokens[0]


def _per_request_tokens(body: str) -> set[str]:
    """The CSRF token and CSP nonces in ``body``: random on every render."""
    tokens = set()
    for tag, attrs, _form in _tags(body):
        if tag == "meta" and _values(attrs, "name") == ["csrf-token"]:
            tokens.update(v for v in _values(attrs, "content") if v)
        tokens.update(v for v in _values(attrs, "nonce") if v)
    return tokens


def _neutralised(body: str) -> str:
    for token in sorted(_per_request_tokens(body), key=len, reverse=True):
        body = body.replace(token, "[per-request]")
    return body


def _echo_problems(refusal_body: str, plain_body: str, value: str) -> list[str]:
    """The forms of ``value`` (raw, and as autoescape writes it) that the
    refusal page shows MORE often than the plain page plus the refusal
    sentence do: an echo of the refused value."""
    if not value:
        raise ValueError("the empty string occurs everywhere: nothing to count")
    refusal, plain = _neutralised(refusal_body), _neutralised(plain_body)
    problems = []
    for needle in sorted({value, str(escape(value))}):
        allowed = plain.count(needle) + REFUSAL_HTML.count(needle)
        shown = refusal.count(needle)
        if shown > allowed:
            problems.append(
                f"{needle!r} occurs {shown} times; the plain page and the "
                f"refusal sentence account for {allowed}: the value is echoed"
            )
    return problems


def _theme_set_cookies(response) -> list[str]:
    """The Set-Cookie headers that set the THEME cookie, picked by name:
    a render also sets ``session`` (``csrf_token()`` writes into it)."""
    return [
        header for header in response.headers.getlist("Set-Cookie")
        if header.split(";", 1)[0].split("=", 1)[0].strip() == THEME_COOKIE
    ]


_COOKIE_ATTRIBUTES = {"expires", "max-age", "secure", "httponly", "path", "samesite"}


def _theme_cookie_problems(header: str, *, value: str) -> list[str]:
    """Why ``header`` is not the theme cookie the plan specifies: ``value``,
    ``Path=/``, no ``Domain`` (host-only), ``HttpOnly``, ``SameSite=Lax``,
    ``Max-Age`` of 400 days (``Expires`` allowed), and ALWAYS ``Secure`` —
    never read from configuration: production is HTTPS end to end, and a
    flag that depends on a setting reads as an insecure cookie."""
    problems = []
    pair, _sep, rest = header.partition(";")
    if pair.strip() != f"{THEME_COOKIE}={value}":
        problems.append(f"name=value is {pair.strip()!r}, expected {THEME_COOKIE}={value}")
    names = [
        part.split("=", 1)[0].strip().lower()
        for part in rest.split(";") if part.strip()
    ]
    unexpected = sorted(set(names) - _COOKIE_ATTRIBUTES)
    if unexpected:
        problems.append(
            f"unexpected attribute(s) {unexpected}: never a Domain (the "
            "cookie stays host-only, off the portal's host)"
        )
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        problems.append(f"attribute(s) {repeated} repeated")
    jar = SimpleCookie()
    jar.load(header)
    if THEME_COOKIE not in jar:
        return problems + [f"SimpleCookie cannot parse {header!r}"]
    morsel = jar[THEME_COOKIE]
    if morsel.value != value:
        problems.append(f"value {morsel.value!r}, expected {value!r}")
    if morsel["path"] != "/":
        problems.append(f"Path {morsel['path']!r}, expected '/'")
    if morsel["domain"]:
        problems.append(f"Domain {morsel['domain']!r}: the cookie must stay host-only")
    if morsel["httponly"] is not True:
        problems.append("HttpOnly is missing")
    if str(morsel["samesite"]).lower() != "lax":
        problems.append(f"SameSite {morsel['samesite']!r}, expected Lax")
    if str(morsel["max-age"]).strip() != str(THEME_COOKIE_MAX_AGE):
        problems.append(f"Max-Age {morsel['max-age']!r}, expected {THEME_COOKIE_MAX_AGE}")
    if not morsel["secure"]:
        problems.append("Secure is missing")
    return problems


def _strip_jinja_comments(raw: str) -> str:
    """The source without its ``{# … #}`` comments: the split idiom of
    ``tests/test_settings_routes.py``, never a regex (a lazy alternation
    over an arbitrary body cost this repo 43 s of ReDoS, twice)."""
    return "".join(
        bloc.split("#}", 1)[-1] if i else bloc
        for i, bloc in enumerate(raw.split("{#"))
    )


_QUOTES = "'" + '"'


def _included_templates(source: str) -> list[str]:
    """The names in ``{% include … %}`` tags, found with ``str.find``."""
    names, start = [], 0
    while True:
        at = source.find("{%", start)
        if at < 0:
            return names
        end = source.find("%}", at)
        if end < 0:
            return names
        words = source[at + 2:end].strip().strip("-").split()
        if len(words) >= 2 and words[0] == "include":
            names.append(words[1].strip(_QUOTES))
        start = end + 2


def _page_sources(name: str) -> dict:
    """``name`` and every template it includes, transitively, comments
    stripped. ``{% extends %}`` is not followed: base.html carries htmx and
    scripts for every page."""
    sources, todo = {}, [name]
    while todo:
        current = todo.pop()
        if current in sources:
            continue
        sources[current] = _strip_jinja_comments(_read("templates", *current.split("/")))
        todo.extend(_included_templates(sources[current]))
    return sources


def _no_htmx_no_script_problems(source: str) -> list[str]:
    lowered = source.lower()  # HTML attribute and tag names ignore case
    return [
        f"{needle!r} in the template source"
        for needle in ("hx-", "<script") if needle in lowered
    ]


def _registration_problems(source: str) -> list[str]:
    """Why ``source`` (main.py) does not wire the theme processor: its
    ``create_app`` must itself import ``theme_context`` from
    ``routes._helpers``, then call ``app.context_processor(theme_context)``."""
    tree = ast.parse(source)
    factories = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_app"
    ]
    if len(factories) != 1:
        return [f"expected one top-level def create_app, found {len(factories)}"]
    imports, calls = [], []
    # DIRECT statements of create_app only: a registration under an `if`,
    # a `try` or a nested def may never run.
    for node in factories[0].body:
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "routes._helpers"
            and node.level == 0
            and any(alias.name == "theme_context"
                    and alias.asname in (None, "theme_context")
                    for alias in node.names)
        ):
            imports.append(node.lineno)
        elif (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "context_processor"
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "app"
            and len(node.value.args) == 1
            and not node.value.keywords
            and isinstance(node.value.args[0], ast.Name)
            and node.value.args[0].id == "theme_context"
        ):
            calls.append(node.lineno)
    problems = []
    if not imports:
        problems.append("create_app never runs `from routes._helpers import theme_context`")
    if not calls:
        problems.append("create_app never calls `app.context_processor(theme_context)`")
    if imports and calls and min(calls) < min(imports):
        problems.append("create_app registers theme_context before importing it")
    return problems


# ── Vocabulary ───────────────────────────────────────────────────────────

def test_the_theme_keys_are_pinned():
    """The keys are STORED values: in every device's cookie, and in the
    ``[data-theme=…]`` selectors of ``static/src/app.input.css``. A renamed
    key silently resets every device that chose it to the default and turns
    its dark mode off: change a label, never a key."""
    assert VALID_THEMES == ("auto", "clair", "sombre"), (
        f"VALID_THEMES is {VALID_THEMES}: a theme key is a stored value "
        "(cookies, CSS selectors) and never changes"
    )
    assert len(set(VALID_THEMES)) == len(VALID_THEMES)


@pytest.mark.parametrize(
    "name,mapping",
    [
        pytest.param("LABELS", LABELS, id="labels"),
        pytest.param("DESCRIPTIONS", DESCRIPTIONS, id="descriptions"),
        pytest.param("COLOR_SCHEMES", COLOR_SCHEMES, id="color-schemes"),
    ],
)
def test_each_map_covers_exactly_the_keys(name, mapping):
    assert set(mapping) == set(VALID_THEMES), (
        f"{name} keys {sorted(mapping)} differ from VALID_THEMES {sorted(VALID_THEMES)}"
    )


def test_the_default_is_a_key_and_clair_opts_out_of_forced_dark():
    assert DEFAULT_THEME in VALID_THEMES, f"DEFAULT_THEME {DEFAULT_THEME!r} is no key"
    # « only light »: an explicit Clair also refuses Chrome's automatic dark
    # theme, and so the Android TWA's, which runs in Chrome.
    assert COLOR_SCHEMES["clair"] == "only light", COLOR_SCHEMES["clair"]


def test_the_cookie_name_lifetime_default_and_schemes_are_pinned():
    """Stored or decided values, pinned like the keys. The cookie NAME sits
    in every device's jar: a rename silently resets every device to the
    default. The lifetime is the browsers' 400-day cap, which the page's
    « environ 13 mois » states. « Automatique » is the decided default, and
    each meta value is the plan's: Auto admits dark, Sombre is dark."""
    assert THEME_COOKIE == "athena_theme", THEME_COOKIE
    assert THEME_COOKIE_MAX_AGE == 400 * 24 * 60 * 60, THEME_COOKIE_MAX_AGE
    assert DEFAULT_THEME == "auto", DEFAULT_THEME
    assert COLOR_SCHEMES == {
        "auto": "light dark",
        "clair": "only light",
        "sombre": "dark",
    }, COLOR_SCHEMES


def test_every_color_scheme_value_is_well_formed():
    """A misspelt keyword (« ligth ») makes the browser ignore the meta
    without a word."""
    for theme, value in COLOR_SCHEMES.items():
        words = value.split()
        assert words and set(words) <= {"normal", "light", "dark", "only"}, (theme, value)
        assert value == " ".join(words), f"{theme}: stray whitespace in {value!r}"


@pytest.mark.parametrize("raw", REFUSED)
def test_parse_theme_refuses_all_but_an_exact_key(raw):
    """Refused, never repaired: no strip, no case folding, no sanitizing."""
    assert parse_theme(raw) is None, f"parse_theme accepted {raw!r}"
    assert theme_from(raw) == DEFAULT_THEME, f"theme_from({raw!r}) did not fall back"


@pytest.mark.parametrize("key", VALID_THEMES)
def test_parse_theme_accepts_each_key_exactly(key):
    assert parse_theme(key) == key
    assert theme_from(key) == key


# ── theme_context ────────────────────────────────────────────────────────

def test_theme_context_without_a_request_gives_the_defaults():
    """A template rendered from service code (an app context, no request)
    has no cookie to read. Flask runs app processors on every render, so
    the processor must answer there, never raise."""
    app = Flask(__name__)
    with app.app_context():
        assert not has_request_context(), "the test needs NO request context"
        assert theme_context() == _defaults()


def test_theme_context_without_the_cookie_gives_the_defaults():
    with Flask(__name__).test_request_context("/"):
        assert theme_context() == _defaults()


@pytest.mark.parametrize("theme", VALID_THEMES)
def test_theme_context_returns_the_cookie_theme(theme):
    headers = {"Cookie": f"{THEME_COOKIE}={theme}"}
    with Flask(__name__).test_request_context("/", headers=headers):
        assert theme_context() == {
            "ui_theme": theme,
            "ui_color_scheme": COLOR_SCHEMES[theme],
        }


@pytest.mark.parametrize("forged", FORGED_COOKIES)
def test_theme_context_turns_a_forged_cookie_into_the_default(forged):
    headers = {"Cookie": f"{THEME_COOKIE}={forged}"}
    with Flask(__name__).test_request_context("/", headers=headers):
        assert request.cookies.get(THEME_COOKIE) == forged, (
            "the forged value never reached the request: the test proves nothing"
        )
        assert theme_context() == _defaults()


# ── Rendering ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("theme", VALID_THEMES)
def test_base_html_renders_the_cookie_theme(web, theme):
    web.set_cookie(THEME_COOKIE, theme)
    r = web.get(PAGE)
    assert r.status_code == 200, r.status_code
    assert _theme_markup_problems(_text(r), theme) == [], (
        f"base.html (via {PAGE}) with the cookie {theme!r}"
    )
    assert _theme_set_cookies(r) == [], "a GET wrote the theme cookie"


@pytest.mark.parametrize("theme", VALID_THEMES)
def test_login_html_renders_the_cookie_theme(anon, theme):
    """Anonymous on purpose: a session redirects /auth/login to
    ``dashboard.index``, which the bare app does not register."""
    anon.set_cookie(THEME_COOKIE, theme)
    r = anon.get(LOGIN)
    assert r.status_code == 200, r.status_code
    assert _theme_markup_problems(_text(r), theme) == [], (
        f"auth/login.html with the cookie {theme!r}"
    )
    assert _theme_set_cookies(r) == [], "a GET wrote the theme cookie"


@pytest.mark.parametrize("forged", FORGED_COOKIES)
def test_a_forged_cookie_renders_the_default_theme(web, anon, forged):
    for client, path in ((web, PAGE), (anon, LOGIN)):
        _set_theme_cookie(client, forged)
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code)
        assert _theme_markup_problems(_text(r), DEFAULT_THEME) == [], path
        if path == PAGE:
            assert _button_problems(_text(r), DEFAULT_THEME) == [], path


@pytest.mark.parametrize(
    "path,logged_in",
    [pytest.param(PAGE, True, id="base"), pytest.param(LOGIN, False, id="login")],
)
def test_without_the_processor_neither_attribute_nor_meta_renders(path, logged_in):
    """An absent attribute means light, the portal's rule, so the templates
    restate no default: an app that does not register the processor renders
    neither the attribute nor the meta, even with a theme cookie set."""
    client = _app(theme=False).test_client()
    if logged_in:
        _login(client)
    client.set_cookie(THEME_COOKIE, "sombre")
    r = client.get(path)
    assert r.status_code == 200, r.status_code
    assert _no_theme_markup_problems(_text(r)) == [], path


def test_the_portal_base_never_carries_data_theme():
    """The portal loads the same stylesheet, which darkens only under
    ``<html data-theme>``: the attribute's absence is what keeps it light.
    Read as SOURCE, comments stripped, since the base's own comment names
    the rule."""
    source = _strip_jinja_comments(_read("client", "templates", "base.html"))
    assert "<html" in source, "not the portal base?"
    folder = os.path.join(_ATHENA, "client", "templates")
    for name in sorted(os.listdir(folder)):
        if name.endswith(".html"):
            portal = _strip_jinja_comments(_read("client", "templates", name))
            assert _portal_problems(portal) == [], name


# ── Routes ───────────────────────────────────────────────────────────────

def test_anonymous_get_redirects_to_login(anon):
    r = anon.get(PAGE)
    assert r.status_code == 302, r.status_code
    target = urlsplit(r.headers["Location"])
    assert target.path == LOGIN, r.headers["Location"]
    assert parse_qs(target.query).get("next") == [PAGE], r.headers["Location"]
    assert _theme_set_cookies(r) == []


def test_anonymous_post_redirects_to_login_and_sets_no_theme(anon):
    r = anon.post(SAVE, data={"theme": "sombre"})
    assert r.status_code == 302, r.status_code
    assert urlsplit(r.headers["Location"]).path == LOGIN, r.headers["Location"]
    assert _theme_set_cookies(r) == [], "an anonymous POST set the theme"
    assert anon.get_cookie(THEME_COOKIE) is None


def test_with_csrf_on_a_tokenless_post_is_refused_and_sets_no_theme():
    """The blueprint is never CSRF-exempt. The second POST, carrying the
    page's own token, goes through: the 400 came from CSRF and nothing else."""
    client = _login(_app(csrf_on=True).test_client())
    r = client.post(SAVE, data={"theme": "sombre"})
    assert r.status_code == 400, r.status_code
    assert _theme_set_cookies(r) == [], "a CSRF rejection set the theme"
    assert client.get_cookie(THEME_COOKIE) is None

    token = _form_csrf_token(_text(client.get(PAGE)))
    r = client.post(SAVE, data={"theme": "sombre", "csrf_token": token})
    assert r.status_code == 302, f"with its token the POST answered {r.status_code}"
    assert len(_theme_set_cookies(r)) == 1


def test_the_method_inventory_is_pinned():
    """A GET-only page and a POST-only save. One GET+POST view would also
    drop out of the derived tab test of ``tests/test_settings_routes.py``."""
    rules: dict = {}
    for rule in _app().url_map.iter_rules():
        rules.setdefault(rule.rule, []).append(rule)
    page, save = rules.get(PAGE, []), rules.get(SAVE, [])
    assert [r.endpoint for r in page] == ["settings.affichage"], page
    assert [r.endpoint for r in save] == ["settings.affichage_update"], save
    assert "GET" in page[0].methods, page[0].methods
    assert page[0].methods <= {"GET", "HEAD", "OPTIONS"}, page[0].methods
    assert "POST" in save[0].methods, save[0].methods
    assert save[0].methods <= {"POST", "OPTIONS"}, save[0].methods


def test_a_get_of_the_save_route_is_405_and_sets_no_theme(web):
    r = web.get(SAVE + "?theme=sombre")
    assert r.status_code == 405, r.status_code
    assert _theme_set_cookies(r) == []
    assert web.get_cookie(THEME_COOKIE) is None


def test_a_theme_in_the_page_query_string_sets_nothing(web):
    r = web.get(PAGE + "?theme=sombre")
    assert r.status_code == 200, r.status_code
    assert _theme_set_cookies(r) == [], "a GET set the theme"
    assert web.get_cookie(THEME_COOKIE) is None
    body = _text(r)
    assert _theme_markup_problems(body, DEFAULT_THEME) == []
    assert _button_problems(body, DEFAULT_THEME) == []


@pytest.mark.parametrize("name", ["next", "return_to"])
@pytest.mark.parametrize("target", ["https://evil.example", "/parametres/securite"])
def test_a_return_path_never_steers_the_redirect(web, name, target):
    """No return path at all: not an open redirect, and not an internal one
    either (``safe_internal_redirect`` would also log a rejection)."""
    r = web.post(f"{SAVE}?{name}={target}", data={"theme": "sombre", name: target})
    assert r.status_code == 302, r.status_code
    assert r.headers["Location"] == PAGE + "?ok=1", r.headers["Location"]


@pytest.mark.parametrize("posted", POSTED_REFUSALS)
def test_a_refused_value_re_renders_at_200_and_changes_nothing(web, posted):
    """Refused in French at 200 (htmx swaps only 2xx; a reader sees it),
    with no cookie, no echo, and the page still in the device's CURRENT
    theme: the refused value never reaches ``ui_theme``."""
    assert str(escape(REFUSAL)) == REFUSAL_HTML
    web.set_cookie(THEME_COOKIE, "clair")
    r = web.post(SAVE, data={} if posted is None else {"theme": posted})
    assert r.status_code == 200, r.status_code
    body = _text(r)
    assert body.count(REFUSAL_HTML) == 1, (
        f"the refusal sentence, verbatim and once: {REFUSAL_HTML!r}"
    )
    assert SAVED not in body
    assert _theme_set_cookies(r) == [], f"a refusal ({posted!r}) set the theme"
    assert web.get_cookie(THEME_COOKIE).value == "clair"
    assert _theme_markup_problems(body, "clair") == []
    assert _button_problems(body, "clair") == []
    if posted:
        plain = _text(web.get(PAGE))
        assert _echo_problems(body, plain, posted) == []


def test_a_theme_in_the_save_query_string_is_refused(web):
    """``request.form``, never ``request.values``: the query string of the
    POST is not a choice."""
    r = web.post(SAVE + "?theme=sombre", data={})
    assert r.status_code == 200, r.status_code
    assert REFUSAL_HTML in _text(r)
    assert _theme_set_cookies(r) == []


def test_a_refusal_without_a_cookie_keeps_the_default_and_never_says_saved(web):
    r = web.post(SAVE + "?ok=1", data={"theme": "Sombre"})
    assert r.status_code == 200, r.status_code
    body = _text(r)
    assert REFUSAL_HTML in body
    assert SAVED not in body, "a refusal must never say « enregistré »"
    assert _theme_set_cookies(r) == []
    assert _theme_markup_problems(body, DEFAULT_THEME) == []


def test_saving_and_refusing_log_nothing(web, caplog):
    """No SettingsEvent, no log line: a device's look is not a journal
    entry (routes/settings.py, Impact item 4)."""
    with caplog.at_level(logging.INFO):
        assert web.post(SAVE, data={"theme": "sombre"}).status_code == 302
        assert web.post(SAVE, data={"theme": "<b>sombre</b>"}).status_code == 200
    assert [(rec.name, rec.getMessage()) for rec in caplog.records] == []


# ── The cookie ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "secure", [pytest.param(True, id="secure"), pytest.param(False, id="not-secure")]
)
@pytest.mark.parametrize("theme", VALID_THEMES)
def test_a_valid_choice_sets_exactly_one_well_formed_theme_cookie(theme, secure):
    """Secure whatever ``SESSION_COOKIE_SECURE`` says: both settings give the
    same cookie."""
    app = _app(secure=secure)
    assert app.config["SESSION_COOKIE_SECURE"] is secure
    client = _login(app.test_client())
    r = client.post(SAVE, data={"theme": theme})
    assert r.status_code == 302, r.status_code
    assert r.headers["Location"] == PAGE + "?ok=1", r.headers["Location"]
    headers = _theme_set_cookies(r)
    assert len(headers) == 1, f"expected exactly one theme Set-Cookie, got {headers}"
    assert _theme_cookie_problems(headers[0], value=theme) == [], headers[0]


def test_logout_sets_no_theme_cookie_and_the_login_page_keeps_the_choice(web):
    """Logout clears ``session`` and only it (``utils/theme.py``): the
    device's choice survives, and the login page that follows renders it."""
    web.set_cookie(THEME_COOKIE, "sombre")
    r = web.post("/auth/logout")
    assert r.status_code == 302, r.status_code
    assert urlsplit(r.headers["Location"]).path == LOGIN, r.headers["Location"]
    assert _theme_set_cookies(r) == [], "logout touched the theme cookie"
    kept = web.get_cookie(THEME_COOKIE)
    assert kept is not None and kept.value == "sombre", kept
    page = web.get(LOGIN)
    assert page.status_code == 200, "still logged in? a session redirects /auth/login"
    assert _theme_markup_problems(_text(page), "sombre") == []


# ── The page ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("theme", VALID_THEMES)
def test_after_a_save_the_page_renders_the_new_theme_and_says_so(web, theme):
    before = VALID_THEMES[(VALID_THEMES.index(theme) + 1) % len(VALID_THEMES)]
    web.set_cookie(THEME_COOKIE, before)
    r = web.post(SAVE, data={"theme": theme})
    assert r.status_code == 302, r.status_code
    body = _text(web.get(r.headers["Location"]))
    assert SAVED in body
    assert _theme_markup_problems(body, theme) == []
    assert _button_problems(body, theme) == []


def test_the_saved_sentence_shows_only_with_ok_1(web):
    assert SAVED in _text(web.get(PAGE + "?ok=1"))
    assert SAVED not in _text(web.get(PAGE))
    assert SAVED not in _text(web.get(PAGE + "?ok=true"))


@pytest.mark.parametrize("cookie", [pytest.param(None, id="no-cookie"), *VALID_THEMES])
def test_the_buttons_are_the_themes_with_the_current_one_pressed(web, cookie):
    if cookie is not None:
        web.set_cookie(THEME_COOKIE, cookie)
    body = _text(web.get(PAGE))
    assert _button_problems(body, cookie or DEFAULT_THEME) == []
    for theme in VALID_THEMES:
        assert LABELS[theme] in body, f"the label of {theme!r} is missing"
        assert str(escape(DESCRIPTIONS[theme])) in body, (
            f"the description of {theme!r} is missing"
        )


def test_the_affichage_page_source_has_no_htmx_and_no_script():
    """On the SOURCE, comments stripped, never on the rendered page: that
    includes ``base.html``, whose ``<body>`` carries ``hx-headers`` and whose
    footer carries scripts. The page is POST + redirect, never htmx: a 4xx
    fragment would never show, and ``/parametres`` is no App Check-exempt
    prefix."""
    sources = _page_sources("settings/affichage.html")
    assert 'name="theme"' in sources["settings/affichage.html"], "not the Affichage template?"
    assert "settings/_onglets.html" in sources, "includes are not followed"
    for name, source in sources.items():
        assert _no_htmx_no_script_problems(source) == [], name


# ── Registration ─────────────────────────────────────────────────────────

_THEME_KEYS = frozenset({"ui_theme", "ui_color_scheme"})
_NOT_SOURCE = frozenset(
    {"tests", "static", "templates", "node_modules", "__pycache__", "venv"}
)


def _theme_key_bindings(source: str) -> list[int]:
    """Lines where Python ``source`` hands a template a theme key: a dict
    literal key, a keyword argument or a subscript store. A later context
    processor, a ``render_template`` kwarg or a ``g`` value of that name
    would silently win over ``theme_context``."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Dict):
            lines += [key.lineno for key in node.keys
                      if isinstance(key, ast.Constant) and key.value in _THEME_KEYS]
        elif isinstance(node, ast.keyword) and node.arg in _THEME_KEYS:
            lines.append(node.value.lineno)
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value in _THEME_KEYS
        ):
            lines.append(node.lineno)
    return lines


def test_only_theme_context_hands_templates_the_theme_keys():
    found = {}
    for root, dirs, files in os.walk(_ATHENA):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _NOT_SOURCE]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with io.open(path, encoding="utf-8") as fh:
                text = fh.read()
            if "ui_theme" not in text and "ui_color_scheme" not in text:
                continue
            lines = _theme_key_bindings(text)
            if lines:
                found[os.path.relpath(path, _ATHENA).replace(os.sep, "/")] = lines
    assert sorted(found) == ["routes/_helpers.py"], found
    assert len(found["routes/_helpers.py"]) == 2, found


def test_only_the_two_shells_name_the_theme_keys():
    """A child template's top-level ``{% set ui_theme %}`` wins over the
    processor for its page, exactly like a ``render_template`` kwarg."""
    folder = os.path.join(_ATHENA, "templates")
    users = set()
    for root, _dirs, files in os.walk(folder):
        for name in files:
            if not name.endswith(".html"):
                continue
            path = os.path.join(root, name)
            with io.open(path, encoding="utf-8") as fh:
                source = _strip_jinja_comments(fh.read())
            if "ui_theme" in source or "ui_color_scheme" in source:
                users.add(os.path.relpath(path, folder).replace(os.sep, "/"))
    assert users == {"base.html", "auth/login.html"}, users


def test_checker_theme_key_bindings():
    assert _theme_key_bindings("x = 1") == []
    processor = textwrap.dedent(
        """
        def p():
            return {"ui_theme": "auto"}
        """
    )
    assert _theme_key_bindings(processor) != []
    assert _theme_key_bindings('render_template("a.html", ui_color_scheme="dark")') != []
    store = textwrap.dedent(
        """
        ctx = {}
        ctx["ui_theme"] = "clair"
        """
    )
    assert _theme_key_bindings(store) != []


def test_checker_portal():
    assert _portal_problems('<head><meta name="color-scheme" content="only light">') == []
    assert _portal_problems('<html lang="fr" data-theme="auto">') != []
    assert _portal_problems('<head><meta name="color-scheme" content="light dark">') != []


def test_checker_page_sources():
    tags = '{% include "a/b-c.html" %}x{%- include ' + "'d.html'" + ' ignore missing -%}'
    assert _included_templates(tags) == ["a/b-c.html", "d.html"]
    assert _included_templates("{% if x %}{{ y }}{% endif %}") == []


def test_main_registers_the_theme_processor():
    """Pinned on main.py's syntax tree, never through the real app.
    Importing ``main`` runs ``create_app``: the logging chain,
    OpenTelemetry and Firebase Admin. Every render through it then runs the
    Réception badge's context processor, a COUNT on the PRODUCTION
    « portail » database. The processor's behaviour is proven above on bare
    apps; this test proves that the production app wires that function."""
    assert _registration_problems(_read("main.py")) == []


# ── The checkers catch what they claim to (synthetic failing inputs) ─────

def _doc(
    *,
    html_attrs=' data-theme="sombre"',
    before='<meta name="color-scheme" content="dark">',
    after="",
    body_attrs="",
) -> str:
    return (
        f'<!DOCTYPE html><html lang="fr"{html_attrs}><head><meta charset="utf-8">'
        f'{before}<link rel="stylesheet" href="/a.css">{after}</head>'
        f"<body{body_attrs}><p>texte</p></body></html>"
    )


def test_checker_theme_markup_accepts_a_correct_document():
    assert _theme_markup_problems(_doc(), "sombre") == []


@pytest.mark.parametrize(
    "doc",
    [
        pytest.param(
            _doc(before="", after='<meta name="color-scheme" content="dark">'),
            id="meta-after-stylesheet",
        ),
        pytest.param(_doc(html_attrs=' data-theme="auto"'), id="wrong-attribute"),
        pytest.param(_doc(html_attrs=""), id="no-attribute"),
        pytest.param(
            _doc(html_attrs=' data-theme="sombre" data-theme="auto"'),
            id="duplicate-attribute",
        ),
        pytest.param(_doc(body_attrs=' data-theme="sombre"'), id="attribute-off-html"),
        pytest.param(_doc(before=""), id="no-meta"),
        pytest.param(
            _doc(before='<meta name="color-scheme" content="light dark">'),
            id="wrong-meta",
        ),
        pytest.param(
            _doc(before='<meta name="color-scheme" content="dark">' * 2),
            id="two-metas",
        ),
    ],
)
def test_checker_theme_markup_catches(doc):
    assert _theme_markup_problems(doc, "sombre") != []


def test_checker_no_theme_markup():
    assert _no_theme_markup_problems(_doc(html_attrs="", before="")) == []
    assert _no_theme_markup_problems(_doc(before="")) != []
    assert _no_theme_markup_problems(_doc(html_attrs="")) != []


def _form(
    values=VALID_THEMES,
    pressed=("clair",),
    *,
    action=SAVE,
    method="post",
    csrf=True,
    kind="submit",
) -> str:
    parts = [f'<form method="{method}" action="{action}">']
    if csrf:
        parts.append('<input type="hidden" name="csrf_token" value="jeton">')
    for value in values:
        state = "true" if value in pressed else "false"
        parts.append(
            f'<button type="{kind}" name="theme" value="{value}" '
            f'aria-pressed="{state}">{value}</button>'
        )
    parts.append("</form>")
    return "".join(parts)


def test_checker_buttons_accepts_the_canonical_form():
    assert _button_problems(_form(), "clair") == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_form(values=VALID_THEMES[::-1]), id="wrong-order"),
        pytest.param(_form(values=VALID_THEMES[:-1]), id="missing-theme"),
        pytest.param(_form(pressed=("clair", "sombre")), id="two-pressed"),
        pytest.param(_form(pressed=()), id="none-pressed"),
        pytest.param(_form(pressed=("sombre",)), id="wrong-pressed"),
        pytest.param(_form(action="/ailleurs"), id="other-form"),
        pytest.param(_form(method="get"), id="get-form"),
        pytest.param(_form(csrf=False), id="no-csrf"),
        pytest.param(_form(kind="button"), id="not-submit"),
    ],
)
def test_checker_buttons_catches(body):
    assert _button_problems(body, "clair") != []


_GOOD_COOKIE = (
    f"{THEME_COOKIE}=sombre; Expires=Wed, 01 Jan 2031 00:00:00 GMT; "
    f"Max-Age={THEME_COOKIE_MAX_AGE}; Secure; HttpOnly; Path=/; SameSite=Lax"
)


def test_checker_cookie_accepts_werkzeugs_form():
    assert _theme_cookie_problems(_GOOD_COOKIE, value="sombre") == []


@pytest.mark.parametrize(
    "header",
    [
        pytest.param(_GOOD_COOKIE + "; Domain=localhost", id="domain"),
        pytest.param(_GOOD_COOKIE.replace("; HttpOnly", ""), id="no-httponly"),
        pytest.param(_GOOD_COOKIE.replace("Path=/", "Path=/parametres"), id="narrow-path"),
        pytest.param(
            _GOOD_COOKIE.replace("SameSite=Lax", "SameSite=None"), id="samesite-none"
        ),
        pytest.param(
            _GOOD_COOKIE.replace(f"Max-Age={THEME_COOKIE_MAX_AGE}", "Max-Age=3600"),
            id="short-max-age",
        ),
        pytest.param(
            _GOOD_COOKIE.replace(f" Max-Age={THEME_COOKIE_MAX_AGE};", ""),
            id="no-max-age",
        ),
        pytest.param(_GOOD_COOKIE.replace("; Secure", ""), id="not-secure"),
        pytest.param(_GOOD_COOKIE.replace("=sombre", "=clair", 1), id="other-value"),
    ],
)
def test_checker_cookie_catches(header):
    assert _theme_cookie_problems(header, value="sombre") != []


def test_checker_theme_set_cookies_picks_by_name():
    response = Response()
    response.headers.add("Set-Cookie", "session=abc; HttpOnly; Path=/")
    response.headers.add("Set-Cookie", f"{THEME_COOKIE}_v2=sombre; Path=/")
    response.headers.add("Set-Cookie", f"x{THEME_COOKIE}=sombre; Path=/")
    assert _theme_set_cookies(response) == [], "a lookalike name was picked"
    response.headers.add("Set-Cookie", _GOOD_COOKIE)
    assert _theme_set_cookies(response) == [_GOOD_COOKIE]


def test_checker_echo():
    plain = '<button value="sombre">Sombre</button>'
    refusal = plain + f"<p>{REFUSAL_HTML}</p>"
    # The label and the sentence account for both « Sombre »: no echo.
    assert _echo_problems(refusal, plain, "Sombre") == []
    assert _echo_problems(refusal + '<input value="Sombre">', plain, "Sombre") != []
    markup = "<b>sombre</b>"
    assert _echo_problems(refusal + str(escape(markup)), plain, markup) != []
    assert _echo_problems(refusal + markup, plain, markup) != []
    # A random nonce holding « 42 » is no echo; a « 42 » in the page is one.
    plain_page = plain + '<script nonce="zzzz"></script>'
    refusal_page = refusal + '<script nonce="a42b"></script>'
    assert _echo_problems(refusal_page, plain_page, "42") == []
    assert _echo_problems(refusal_page + "<p>42</p>", plain_page, "42") != []
    with pytest.raises(ValueError):
        _echo_problems(refusal, plain, "")


def test_checker_template_source():
    assert _strip_jinja_comments("a{# x #}b{# y #}c") == "abc"
    clean = _strip_jinja_comments("{# hx-get, <script> #}<p>ok</p>")
    assert _no_htmx_no_script_problems(clean) == []
    assert _no_htmx_no_script_problems('<button hx-post="/x">') != []
    assert _no_htmx_no_script_problems('<SCRIPT src="/x.js"></SCRIPT>') != []
    assert "data-theme" not in _strip_jinja_comments('{# data-theme #}<html lang="fr">')
    assert "data-theme" in _strip_jinja_comments('{# note #}<html data-theme="auto">')


_MAIN_OK = textwrap.dedent(
    """
    def create_app():
        app = Flask(__name__)
        from routes._helpers import theme_context
        app.context_processor(theme_context)
        return app
    """
)

_MAIN_BROKEN = [
    pytest.param(
        textwrap.dedent(
            """
            def create_app():
                app = Flask(__name__)
                from routes._helpers import theme_context
                return app
            """
        ),
        id="never-registered",
    ),
    pytest.param(
        textwrap.dedent(
            """
            def create_app():
                app = Flask(__name__)
                from routes._helpers import theme_context
                def later():
                    app.context_processor(theme_context)
                return app
            """
        ),
        id="registered-in-a-nested-def",
    ),
    pytest.param(
        textwrap.dedent(
            """
            def create_app():
                app = Flask(__name__)
                from routes.helpers import theme_context
                app.context_processor(theme_context)
                return app
            """
        ),
        id="imported-from-elsewhere",
    ),
    pytest.param(
        textwrap.dedent(
            """
            from routes._helpers import theme_context
            def create_app():
                app = Flask(__name__)
                return app
            app.context_processor(theme_context)
            """
        ),
        id="outside-create_app",
    ),
    pytest.param(
        textwrap.dedent(
            """
            def create_app():
                app = Flask(__name__)
                app.context_processor(theme_context)
                from routes._helpers import theme_context
                return app
            """
        ),
        id="registered-before-imported",
    ),
    pytest.param(
        textwrap.dedent(
            """
            def create_app():
                app = Flask(__name__)
                from routes._helpers import theme_context
                if app.config.get("THEME_ENABLED"):
                    app.context_processor(theme_context)
                return app
            """
        ),
        id="registered-under-an-if",
    ),
    pytest.param(
        textwrap.dedent(
            """
            def make_app():
                app = Flask(__name__)
                from routes._helpers import theme_context
                app.context_processor(theme_context)
                return app
            """
        ),
        id="no-create_app",
    ),
]


def test_checker_registration_accepts_the_canonical_form():
    assert _registration_problems(_MAIN_OK) == []


@pytest.mark.parametrize("source", _MAIN_BROKEN)
def test_checker_registration_catches(source):
    assert _registration_problems(source) != []
