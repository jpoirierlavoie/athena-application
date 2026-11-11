"""htmx keeps no page snapshot, and Back reloads the page (2026-11-11).

Left at its defaults, htmx 2 saves the page being left, whole, in
localStorage at every URL push (``hx-push-url``: the shared pager, the
filter bars, the dossier tabs): up to ten pages of client names, amounts and
balances, kept past logout. Back restores that copy, however stale. On a
miss htmx fetches the URL with a bare XMLHttpRequest that carries
``HX-Request`` but not the App Check token (it skips ``htmx:configRequest``,
where appcheck-boot adds the token), so ``security._verify_app_check``
refuses it (401) and the old page stays under the new URL.

``templates/base.html`` sets ``historyCacheSize`` to 0 (nothing saved) and
``refreshOnHistoryMiss`` (Back and Forward between pushed URLs reload the
URL as a full page, Rule 8; offline, the service worker answers with
/offline, as for any page). htmx deletes the list it saved before only at
its next URL push, which a visit may never make, so an inline script deletes
it at every page load. These tests pin, on every page that loads htmx, the
setting, its place in ``<head>`` and that purge; and the vendored htmx's
handling of both keys and of the list's name: a version that drops or
renames one turns the setting into a silent no-op. Each check also runs on a
planted input it must flag. Only a browser sees the effect itself: it was
checked in headless Chrome on the real templates, against a control without
the setting (2026-11-11).
"""

import json
import pathlib
import re
import sys
from html.parser import HTMLParser

import pytest
from flask import Flask, render_template_string

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

from utils.icons import ms  # noqa: E402

TEMPLATE_ROOTS = (_ATHENA / "templates", _ATHENA / "client" / "templates")
VENDOR = _ATHENA / "static" / "vendor"

# What every page that loads htmx declares, exactly (types included: JSON
# ``true``, never ``1``).
HTMX_CONFIG = {"historyCacheSize": 0, "refreshOnHistoryMiss": True}

# The localStorage key under which htmx keeps its page copies.
HISTORY_KEY = "htmx-history-cache"

# A template loads htmx when it names a vendored htmx build.
_HTMX_BUILD = re.compile(r"vendor/(htmx-\d+\.\d+\.\d+(?:\.min)?\.js)")

# The purge: the key removed inside a try, so a browser that refuses storage
# throws nothing.
_PURGE = re.compile(
    r"try\s*\{\s*localStorage\.removeItem\(\s*(['\"])" + re.escape(HISTORY_KEY)
    + r"\1\s*\)\s*;?\s*\}\s*catch\b")

# What the vendored build must contain for the setting to work: htmx
# 2.0.4's minified source, read on 2026-11-11. A new version is checked in a
# browser again (Back reloads, localStorage holds no HISTORY_KEY), then its
# markers replace these.
SUPPORT_MARKERS = {
    "reads the meta": 'meta[name="htmx-config"]',
    "knows both keys": "historyCacheSize:10,refreshOnHistoryMiss:false",
    "saves nothing at zero and deletes the stored list":
        f'historyCacheSize<=0){{localStorage.removeItem("{HISTORY_KEY}")',
    "reloads on a miss": "refreshOnHistoryMiss){window.location.reload(",
}


class _Page(HTMLParser):
    """A page's htmx-config metas (each with whether it sits in <head>), the
    htmx builds its scripts load, and its inline scripts (each with whether
    it carries a nonce)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_head = False
        self._script: list[str] | None = None
        self._script_nonce = False
        self.metas: list[tuple[str, bool]] = []
        self.htmx_scripts: list[str] = []
        self.inline_scripts: list[tuple[str, bool]] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "head":
            self._in_head = True
        elif tag == "meta" and attributes.get("name") == "htmx-config":
            self.metas.append((attributes.get("content") or "", self._in_head))
        elif tag == "script" and _HTMX_BUILD.search(attributes.get("src") or ""):
            self.htmx_scripts.append(attributes["src"])
        elif tag == "script" and "src" not in attributes:
            self._script = []
            self._script_nonce = bool(attributes.get("nonce"))

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)

    def handle_endtag(self, tag):
        if tag == "head":
            self._in_head = False
        elif tag == "script" and self._script is not None:
            self.inline_scripts.append(("".join(self._script), self._script_nonce))
            self._script = None


def _parse(page: str) -> _Page:
    parsed = _Page()
    parsed.feed(page)
    parsed.close()
    return parsed


def config_problems(page: str) -> list[str]:
    """What keeps a page from declaring ``HTMX_CONFIG``: empty when it
    declares exactly that, once, in its <head>."""
    metas = _parse(page).metas
    if len(metas) != 1:
        return [f"{len(metas)} htmx-config metas, expected exactly one"]
    content, in_head = metas[0]
    problems = [] if in_head else ["the htmx-config meta is outside <head>"]
    try:
        config = json.loads(content)
    except ValueError:
        return problems + [f"the htmx-config meta is not JSON: {content!r}"]
    if json.dumps(config, sort_keys=True) != json.dumps(HTMX_CONFIG, sort_keys=True):
        problems.append(f"htmx-config is {config!r}, expected {HTMX_CONFIG!r}")
    return problems


def purge_problems(page: str) -> list[str]:
    """Empty when an inline script that carries a nonce (the CSP blocks any
    other) removes ``HISTORY_KEY`` at load, inside a try."""
    scripts = _parse(page).inline_scripts
    if any(nonce and _PURGE.search(text) for text, nonce in scripts):
        return []
    if any(_PURGE.search(text) for text, _nonce in scripts):
        return [f"the {HISTORY_KEY} purge sits in a script without a nonce"]
    return [f"no inline script removes {HISTORY_KEY} inside a try"]


def htmx_loaders(roots=TEMPLATE_ROOTS, base: pathlib.Path = _ATHENA) -> dict[str, str]:
    """Every template that loads htmx, by its path under ``base``, with its
    source minus Jinja comments (a commented-out meta declares nothing)."""
    found = {}
    for root in roots:
        for path in sorted(root.rglob("*.html")):
            source = re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"),
                            flags=re.S)
            if _HTMX_BUILD.search(source):
                found[path.relative_to(base).as_posix()] = source
    return found


def page_problems(page: str) -> list[str]:
    return config_problems(page) + purge_problems(page)


def missing_support(source: str) -> list[str]:
    """The markers of ``SUPPORT_MARKERS`` a vendored htmx build lacks."""
    return [what for what, marker in SUPPORT_MARKERS.items() if marker not in source]


def _vendored_builds() -> set[str]:
    return {match.group(1) for source in htmx_loaders().values()
            for match in _HTMX_BUILD.finditer(source)}


def _render_base() -> str:
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    with app.test_request_context("/"):
        return render_template_string(
            '{% extends "base.html" %}{% block content %}<p>page</p>{% endblock %}')


_GOOD = ("<meta name=\"htmx-config\" "
         "content='{\"historyCacheSize\": 0, \"refreshOnHistoryMiss\": true}'>")
_PURGE_SCRIPT = ("<script nonce=\"{{ csp_nonce }}\">"
                 "try { localStorage.removeItem('htmx-history-cache'); } catch (e) {}"
                 "</script>")


# ── The real templates and the vendored build ──────────────────────────────


def test_every_page_that_loads_htmx_turns_its_snapshots_off():
    loaders = htmx_loaders()
    # The main service's layout loads htmx; the portal loads none.
    assert list(loaders) == ["templates/base.html"]
    assert {name: page_problems(source) for name, source in loaders.items()} == {
        "templates/base.html": []}


def test_the_rendered_page_declares_it_once_in_its_head_and_purges_on_load():
    page = _render_base()
    assert config_problems(page) == []
    assert purge_problems(page) == []
    parsed = _parse(page)
    # Under the one htmx it loads, and the purge under the request's nonce.
    assert len(parsed.htmx_scripts) == 1
    assert [nonce for text, nonce in parsed.inline_scripts if _PURGE.search(text)] == [True]
    assert 'nonce="n"' in page


def test_the_vendored_htmx_reads_both_keys_and_keeps_its_list_under_that_name():
    builds = _vendored_builds()
    assert builds == {"htmx-2.0.4.min.js"}
    for build in builds:
        assert missing_support((VENDOR / build).read_text(encoding="utf-8")) == []


# ── Each check flags what it guards against ────────────────────────────────


def test_a_layout_loading_htmx_without_the_setting_is_flagged(tmp_path):
    root = tmp_path / "templates"
    (root / "sub").mkdir(parents=True)
    script = '<script defer src="/static/vendor/htmx-2.0.4.min.js"></script>'
    (root / "sub" / "bare.html").write_text(
        f"<html><head><title>t</title></head><body>{script}</body></html>",
        encoding="utf-8")
    # Commented out, the meta declares nothing and the purge runs nowhere.
    (root / "commented.html").write_text(
        f"<html><head>{{# {_GOOD} #}}</head>"
        f"<body>{{# {_PURGE_SCRIPT} #}}{script}</body></html>",
        encoding="utf-8")
    (root / "no_purge.html").write_text(
        f"<html><head>{_GOOD}</head><body>{script}</body></html>",
        encoding="utf-8")
    (root / "kept.html").write_text(
        f"<html><head>{_GOOD}</head><body>{_PURGE_SCRIPT}{script}</body></html>",
        encoding="utf-8")
    (root / "no_htmx.html").write_text("<p>rien</p>", encoding="utf-8")
    no_meta = "0 htmx-config metas, expected exactly one"
    no_purge = "no inline script removes htmx-history-cache inside a try"
    loaders = htmx_loaders((root,), base=tmp_path)
    assert {name: page_problems(source) for name, source in loaders.items()} == {
        "templates/commented.html": [no_meta, no_purge],
        "templates/kept.html": [],
        "templates/no_purge.html": [no_purge],
        "templates/sub/bare.html": [no_meta, no_purge],
    }


@pytest.mark.parametrize("content, problem", [
    ('{"historyCacheSize": 10, "refreshOnHistoryMiss": true}', "expected"),
    ('{"historyCacheSize": 0}', "expected"),
    ('{"refreshOnHistoryMiss": true}', "expected"),
    ('{"historyCacheSize": 0, "refreshOnHistoryMiss": 1}', "expected"),
    ('{"historyCacheSize": 0, "refreshOnHistoryMiss": true,}', "not JSON"),
    ("", "not JSON"),
])
def test_a_wrong_setting_is_flagged(content, problem):
    page = f"<html><head><meta name=\"htmx-config\" content='{content}'></head></html>"
    problems = config_problems(page)
    assert len(problems) == 1 and problem in problems[0], problems


def test_a_setting_outside_the_head_or_declared_twice_is_flagged():
    assert config_problems(f"<html><head>{_GOOD}</head><body></body></html>") == []
    assert config_problems(f"<html><head></head><body>{_GOOD}</body></html>") == [
        "the htmx-config meta is outside <head>"]
    assert config_problems(f"<html><head>{_GOOD}{_GOOD}</head></html>") == [
        "2 htmx-config metas, expected exactly one"]


@pytest.mark.parametrize("script, problem", [
    ('<script nonce="n">try { localStorage.removeItem("htmx-history-cache"); }'
     " catch (e) {}</script>", None),
    ("<script>try { localStorage.removeItem('htmx-history-cache'); } catch (e) {}"
     "</script>", "the htmx-history-cache purge sits in a script without a nonce"),
    ('<script nonce="n">localStorage.removeItem(\'htmx-history-cache\');</script>',
     "no inline script removes htmx-history-cache inside a try"),
    ("<script nonce=\"n\">try { localStorage.removeItem('htmx-history'); }"
     " catch (e) {}</script>",
     "no inline script removes htmx-history-cache inside a try"),
    ("<script nonce=\"n\">try { localStorage.getItem('htmx-history-cache'); }"
     " catch (e) {}</script>",
     "no inline script removes htmx-history-cache inside a try"),
    ("<script nonce=\"n\" src=\"/static/x.js\">try { localStorage.removeItem("
     "'htmx-history-cache'); } catch (e) {}</script>",
     "no inline script removes htmx-history-cache inside a try"),
])
def test_a_missing_or_blocked_purge_is_flagged(script, problem):
    page = f"<html><head></head><body>{script}</body></html>"
    assert purge_problems(page) == ([] if problem is None else [problem])


def test_a_build_without_either_key_or_under_another_list_name_is_flagged():
    real = (VENDOR / "htmx-2.0.4.min.js").read_text(encoding="utf-8")
    assert missing_support(real.replace("refreshOnHistoryMiss", "reloadOnMiss")) == [
        "knows both keys", "reloads on a miss"]
    assert missing_support(real.replace("historyCacheSize", "historySize")) == [
        "knows both keys", "saves nothing at zero and deletes the stored list"]
    assert missing_support(real.replace('meta[name="htmx-config"]', "")) == [
        "reads the meta"]
    assert missing_support(real.replace(HISTORY_KEY, "htmx-history-v2")) == [
        "saves nothing at zero and deletes the stored list"]
