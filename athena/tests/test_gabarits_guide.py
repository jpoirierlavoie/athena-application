"""The guide to the gabarit fields, in the application (2026-11-11).

``athena/GABARITS_PLACEHOLDERS.md`` — the French guide that
``tests/test_gabarits_placeholders_doc.py`` pins against the code — is the
page the Gabarits list opens (the « info » button), at ``/gabarits/aide``,
rendered by the application's one Markdown pipeline. That pipeline prints
every line break (``nl2br``), so the guide is written one line per paragraph
and per list item; a wrapped sentence would print broken.

Real routes and templates over the shared fake Firestore.
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
    import security

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.icons import ms  # noqa: E402

UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"
LABEL = "Guide des champs des gabarits"


@pytest.fixture()
def web(monkeypatch):
    modules = [m for n, m in sorted(sys.modules.items())
               if n.startswith("models.") and getattr(m, "db", None) is not None]
    install(monkeypatch, *modules)
    monkeypatch.setattr(tpl.storage, "bucket", lambda: FakeBucket())
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    app.jinja_env.filters.update(to_mtl=to_mtl)
    for bp in (rdt.doc_templates_bp, rdt.modeles_bp):
        app.register_blueprint(bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = UID
        s["email"] = "juriste@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=timezone.utc)
    return client


# ══════════════════════════════════════════════════════════════════════
# 1. The document
# ══════════════════════════════════════════════════════════════════════


def test_the_page_shows_the_document_the_code_pins():
    from tests import test_gabarits_placeholders_doc as pin
    assert rdt.GUIDE_PATH == pathlib.Path(pin._DOC_PATH).resolve()
    assert rdt.GUIDE_PATH.parent == _ATHENA        # inside the deploy root
    assert rdt.GUIDE_PATH.is_file()


# A line that STARTS a block: a heading, a table row, a list item, a fence,
# a rule. Anything else right after a prose line continues that paragraph.
_BLOCK_START = re.compile(r"(#{1,6}\s|\||[-*+]\s|\d+[.)]\s|```|---|\*\*\*)")


def _wrapped_lines(text: str) -> list:
    """The lines that continue the previous line's paragraph — each would
    print as a visible line break."""
    wrapped = []
    prev_prose = False
    prev_quoted = False
    in_fence = in_comment = False
    for raw in text.splitlines():
        if in_comment or raw.lstrip().startswith("<!--"):
            in_comment = "-->" not in raw
            prev_prose = False
            continue
        if raw.lstrip().startswith("```"):
            in_fence = not in_fence
            prev_prose = False
            continue
        if in_fence:
            continue
        quoted = raw.lstrip().startswith(">")
        line = raw.lstrip()[1:] if quoted else raw
        body = line.strip()
        if not body:
            prev_prose = False
            continue
        starts_block = bool(_BLOCK_START.match(body)) and line == line.lstrip()
        nested_item = bool(_BLOCK_START.match(body)) and body[:1] in "-*+"
        if prev_prose and quoted == prev_quoted and not (starts_block or nested_item):
            wrapped.append(raw)
        prev_prose = not body.startswith(("#", "|", "```", "---"))
        prev_quoted = quoted
    return wrapped


def test_the_guide_is_written_one_line_per_paragraph():
    wrapped = _wrapped_lines(rdt.GUIDE_PATH.read_text(encoding="utf-8"))
    assert not wrapped, wrapped[:5]


def test_the_wrap_check_catches_a_wrapped_line():
    assert _wrapped_lines("Une phrase coupée\nen deux lignes.\n") == [
        "en deux lignes."]
    assert _wrapped_lines("- un élément\n  qui continue\n") == ["  qui continue"]
    assert _wrapped_lines("> une citation\n> coupée\n") == ["> coupée"]
    assert not _wrapped_lines(
        "# Titre\nUn paragraphe.\n\n| a | b |\n|---|---|\n| c | d |\n\n"
        "- un\n- deux\n  - trois\n\n> une ligne\n\n---\n"
        "<!-- note\npour les développeurs -->\n")


# ══════════════════════════════════════════════════════════════════════
# 2. The page and the way to it
# ══════════════════════════════════════════════════════════════════════


def test_the_page_shows_the_guide(web):
    resp = web.get("/gabarits/aide")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert "<title>Champs des gabarits — Bibliothèque — Pallas Athena</title>" in page
    assert "<h1>Champs des gabarits" in page
    # A field name breaks after its dots and underscores, never mid-word,
    # and a wide table scrolls in its own box.
    assert "<code>{{dossier.<wbr>titre}}</code>" in page
    assert "<code>{{dossier.<wbr>reference_<wbr>interne}}</code>" in page
    assert '<div class="overflow-x-auto"><table>' in page
    assert page.count("<table>") == page.count('<div class="overflow-x-auto"><table>')
    assert 'href="/gabarits/"' in page                     # back to the list
    assert "<script>" not in page.split('class="note-content', 1)[1]


def test_only_the_gabarits_list_offers_the_guide(web):
    gabarits = web.get("/gabarits/").get_data(as_text=True)
    assert 'href="/gabarits/aide"' in gabarits
    assert f'aria-label="{LABEL}"' in gabarits and f'title="{LABEL}"' in gabarits
    assert "/gabarits/aide" not in web.get("/modeles/").get_data(as_text=True)


def test_the_upload_form_links_to_the_guide(web):
    page = web.get("/gabarits/new").get_data(as_text=True)
    assert 'href="/gabarits/aide"' in page
    assert "GABARITS_PLACEHOLDERS" not in page


def test_every_literal_route_under_gabarits_is_reserved():
    """The 10 MB upload cap reads a one-segment POST under /gabarits/ as a
    template id unless its segment is reserved: every literal route is."""
    app = Flask(__name__)
    app.register_blueprint(rdt.doc_templates_bp)
    literal = set()
    for rule in app.url_map.iter_rules():
        parts = rule.rule.rstrip("/").split("/")
        if parts[:2] == ["", "gabarits"] and len(parts) == 3 and "<" not in parts[2]:
            literal.add(parts[2])
    assert literal == security._TEMPLATE_RESERVED_SEGMENTS
    assert not security._is_template_upload_path("/gabarits/aide")
