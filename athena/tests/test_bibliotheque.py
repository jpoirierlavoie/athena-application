"""The Bibliothèque's tab bar (2026-11-11): Gabarits · Modèles · Documents.

One include, ``templates/bibliotheque/_onglets.html``, is the one list of
the tabs, in the lawyer's order (decision of 2026-11-11). Its paths are
literal — base.html's rule, since test apps that register a single
blueprint render these pages — so these tests also prove each path reaches
its list view, with no redirect. Real routes over the shared fake
Firestore.
"""

import os
import pathlib
import re
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import models.doc_template as tpl
    import routes.doc_templates as rdt
    import routes.documents as rdocs

from flask import Flask  # noqa: E402
from jinja2 import Environment, FileSystemLoader  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.icons import ms  # noqa: E402

UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"
_TEMPLATES = _ATHENA / "templates"
INCLUDE = "bibliotheque/_onglets.html"

#: The lawyer's order: (key, label, path, the list view the path reaches).
TABS = [
    ("gabarits", "Gabarits", "/gabarits/", "doc_templates.template_list"),
    ("modeles", "Modèles", "/modeles/", "modeles.modele_list"),
    ("documents", "Documents", "/documents/", "documents.document_list"),
]

_NAV = re.compile(r'<nav aria-label="Sections de la bibliothèque".*?</nav>',
                  re.S)
_LINK = re.compile(r'<a href="([^"]+)"([^>]*)>([^<]*)</a>')


def _tabs(html: str) -> list:
    """(path, label, current) of the ONE tab bar on the page."""
    navs = _NAV.findall(html)
    assert len(navs) == 1, len(navs)
    return [(href, label.strip(), 'aria-current="page"' in attrs)
            for href, attrs, label in _LINK.findall(navs[0])]


def _render(onglet: str) -> str:
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)))
    return env.get_template(INCLUDE).render(onglet=onglet)


def _current(tabs: list) -> list:
    return [href for href, _, current in tabs if current]


# ══════════════════════════════════════════════════════════════════════
# 1. The include
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("key, label, path, endpoint", TABS)
def test_the_tabs_are_in_the_lawyers_order_and_mark_one(
    key, label, path, endpoint
):
    tabs = _tabs(_render(key))
    assert [(href, text) for href, text, _ in tabs] == [
        (p, lbl) for _, lbl, p, _ in TABS]
    assert _current(tabs) == [path]


def test_an_unknown_key_marks_no_tab():
    """The check above catches a page that names a wrong tab: nothing is
    marked, so « exactly one » fails."""
    assert _current(_tabs(_render("inconnu"))) == []


def test_each_tab_path_reaches_its_list_view_without_a_redirect():
    app = Flask(__name__)
    for bp in (rdt.doc_templates_bp, rdt.modeles_bp, rdocs.documents_bp):
        app.register_blueprint(bp)
    adapter = app.url_map.bind("localhost")
    for _, _, path, endpoint in TABS:
        assert adapter.match(path, method="GET")[0] == endpoint, path


def test_only_the_two_list_pages_include_the_tab_bar():
    """The Gabarits and Modèles lists share gabarits/list.html."""
    include = re.compile(r'\{%-?\s*include\s+"' + re.escape(INCLUDE) + '"')
    hits = sorted(
        p.relative_to(_TEMPLATES).as_posix()
        for p in _TEMPLATES.rglob("*.html")
        if include.search(p.read_text(encoding="utf-8")))
    assert hits == ["documents/list.html", "gabarits/list.html"]


# ══════════════════════════════════════════════════════════════════════
# 2. The three pages
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture()
def web(monkeypatch):
    modules = [m for n, m in sorted(sys.modules.items())
               if n.startswith("models.") and getattr(m, "db", None) is not None]
    install(monkeypatch, *modules)
    monkeypatch.setattr(tpl.storage, "bucket", lambda: FakeBucket())
    app = Flask(__name__, template_folder=str(_TEMPLATES),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    app.jinja_env.filters.update(to_mtl=to_mtl)
    for bp in (rdt.doc_templates_bp, rdt.modeles_bp, rdocs.documents_bp):
        app.register_blueprint(bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = UID
        s["email"] = "juriste@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=timezone.utc)
    return client


@pytest.mark.parametrize("key, label, path, endpoint", TABS)
def test_each_page_shows_the_bar_and_marks_its_own_tab(
    web, key, label, path, endpoint
):
    resp = web.get(path)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert '<h1 class="text-xl font-semibold text-gray-900">Bibliothèque</h1>' in html
    assert f"<title>{label} — Bibliothèque — Pallas Athena</title>" in html
    assert _current(_tabs(html)) == [path]
    # An htmx filter swap answers its rows alone: never a second bar.
    partial = web.get(path, headers={"HX-Request": "true"})
    assert "Sections de la bibliothèque" not in partial.get_data(as_text=True)


@pytest.mark.parametrize("path, label", [
    ("/gabarits/", "Nouveau gabarit"),
    ("/modeles/", "Nouveau modèle"),
    ("/documents/", "Téléverser"),
])
def test_each_pages_phone_button_is_named(web, path, label):
    html = web.get(path).get_data(as_text=True)
    fab = re.search(r'<a [^>]*class="[^"]*md:hidden fixed[^"]*"[^>]*>'
                    r'|<div class="md:hidden fixed[^"]*">\s*<a [^>]*>', html)
    assert fab, path
    assert f'aria-label="{label}"' in fab.group(0), fab.group(0)
    assert f'title="{label}"' in fab.group(0), fab.group(0)
