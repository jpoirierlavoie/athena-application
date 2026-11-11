"""Bibliothèque → Modèles (2026-11-11): contract models, the kind « modele ».

A contract model is a ``doc_templates`` record of kind « modele »: the
lawyer downloads it and adapts it in Word. The web lists it apart, on the
Modèles list (``/modeles/``), never on the Gabarits list; its page offers
the download, never a generation; the generation popup never offers it and
its three views refuse it. The connector sees it like any template (the
lawyer's choice) — those reads and the fill refusal are pinned with the
connector's own tests (test_mcp_template_reads, test_mcp_generation,
test_mcp_template_writes).

Real routes and models over the shared fake Firestore and the fake bucket.
"""

import io
import os
import pathlib
import re
import sys
import zipfile
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

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.icons import ms  # noqa: E402

UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"
UTC = timezone.utc

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/></Types>'
)


def docx(text: str) -> bytes:
    body = (
        '<?xml version="1.0"?><w:document xmlns:w="http://schemas.'
        'openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r>'
        f'<w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("word/document.xml", body)
    return buf.getvalue()


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if n.startswith("models.") and getattr(m, "db", None) is not None]


@pytest.fixture()
def store(monkeypatch):
    db = install(monkeypatch, *_fake_modules())
    bucket = FakeBucket()
    monkeypatch.setattr(tpl.storage, "bucket", lambda: bucket)
    return db, bucket


def _create(name, kind="gabarit", text="{{dossier.titre}}"):
    data = docx(text)
    doc, errors = tpl.create_template(
        io.BytesIO(data), "fichier.docx", len(data),
        {"name": name, "category": "autre", "kind": kind}, UID)
    assert errors == [], errors
    return doc["id"]


def _app(*blueprints):
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    app.jinja_env.filters.update(to_mtl=to_mtl)
    for bp in blueprints:
        app.register_blueprint(bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = UID
        s["email"] = "juriste@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return client


@pytest.fixture()
def web(store):
    return _app(rdt.doc_templates_bp, rdt.modeles_bp)


def _html(web, url, **kw) -> str:
    resp = web.get(url, **kw)
    assert resp.status_code == 200, (url, resp.status_code)
    return resp.get_data(as_text=True)


HX = {"HX-Request": "true"}


# ══════════════════════════════════════════════════════════════════════
# 1. The two lists
# ══════════════════════════════════════════════════════════════════════


def test_the_gabarits_list_never_shows_a_modele(store, web):
    _create("Lettre type")
    _create("Contrat de services", kind="modele", text="Texte libre")
    page = _html(web, "/gabarits/")
    assert "Lettre type" in page
    assert "Contrat de services" not in page
    for url in ("/gabarits/?category=autre", "/gabarits/?q=Contrat"):
        assert "Contrat de services" not in _html(web, url, headers=HX)


def test_the_modeles_list_shows_only_modeles(store, web):
    _create("Lettre type")
    modele = _create("Contrat de services", kind="modele", text="Texte libre")
    page = _html(web, "/modeles/")
    assert "Contrat de services" in page and "Lettre type" not in page
    assert "<title>Modèles — Pallas Athena</title>" in page
    rows = _html(web, "/modeles/?q=services", headers=HX)
    assert "<h1" not in rows                      # the rows partial alone
    assert f"/gabarits/{modele}" in rows
    assert "Contrat de services" not in _html(web, "/modeles/?q=bail",
                                              headers=HX)


def test_a_modele_row_offers_its_download_never_a_generation(store, web):
    modele = _create("Contrat de services", kind="modele", text="Texte")
    page = _html(web, "/modeles/")
    assert f'href="/gabarits/{modele}/download"' in page
    assert 'aria-label="Télécharger"' in page and 'title="Télécharger"' in page
    assert "/gabarits/generer" not in page
    # No gabarit-only parts: no category (« Autre »), no field count.
    assert "Autre" not in page and "champ" not in page


def test_each_list_offers_its_own_new_form_and_filters(store, web):
    modeles = _html(web, "/modeles/")
    assert 'href="/gabarits/new?kind=modele"' in modeles
    assert 'aria-label="Nouveau modèle"' in modeles       # the phone's FAB
    assert 'name="category"' not in modeles
    assert "Aucun modèle pour le moment" in modeles
    gabarits = _html(web, "/gabarits/")
    assert 'href="/gabarits/new"' in gabarits
    assert 'aria-label="Nouveau gabarit"' in gabarits
    assert 'name="category"' in gabarits


# ══════════════════════════════════════════════════════════════════════
# 2. The form: a new modèle, a move between the two lists
# ══════════════════════════════════════════════════════════════════════


def test_the_new_modele_form_preselects_the_type_and_autre(store, web):
    page = _html(web, "/gabarits/new?kind=modele")
    assert re.search(r'value="modele"\s+checked', page)
    assert re.search(r'<option value="autre" selected', page)
    assert "Nouveau modèle" in page and "Téléverser le modèle" in page
    assert 'href="/modeles/"' in page                  # back and cancel


def test_an_unknown_kind_falls_back_to_a_new_gabarit(store, web):
    page = _html(web, "/gabarits/new?kind=n_importe_quoi")
    assert re.search(r'value="gabarit"\s+checked', page)
    assert "Nouveau gabarit" in page and "Nouveau modèle" not in page
    assert 'href="/modeles/"' not in page


def test_uploading_a_modele_from_the_form(store, web):
    db, _ = store
    data = docx("Bail type")
    resp = web.post("/gabarits/", data={
        "name": "Bail commercial", "description": "", "category": "autre",
        "kind": "modele", "file": (io.BytesIO(data), "bail.docx"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 302, resp.get_data(as_text=True)
    [stored] = db.peek_collection("doc_templates").values()
    assert stored["kind"] == "modele"
    assert resp.headers["Location"].endswith(f"/gabarits/{stored['id']}")


def test_a_gabarit_moves_to_modeles_and_back_through_its_form(store, web):
    db, _ = store
    tid = _create("Convention d'honoraires")
    for kind in ("modele", "gabarit"):
        etag = db.peek(f"doc_templates/{tid}")["etag"]
        resp = web.post(f"/gabarits/{tid}", data={
            "name": "Convention d'honoraires", "description": "",
            "category": "autre", "kind": kind, "expected_etag": etag,
        })
        assert resp.status_code == 302, resp.get_data(as_text=True)
        assert db.peek(f"doc_templates/{tid}")["kind"] == kind
        on_modeles = f"/gabarits/{tid}" in _html(web, "/modeles/")
        on_gabarits = f"/gabarits/{tid}" in _html(web, "/gabarits/")
        assert (on_modeles, on_gabarits) == (kind == "modele", kind == "gabarit")


def test_the_active_template_can_never_become_a_modele(store):
    db, _ = store
    tid = _create("Note", kind="note_honoraires", text="{{facture.numero}}")
    tpl.set_active_template(tid, par="juriste", expected_etag=None)
    _, errors, _ = tpl.update_template(tid, {"kind": "modele"})
    assert errors
    assert db.peek(f"doc_templates/{tid}")["kind"] == "note_honoraires"


# ══════════════════════════════════════════════════════════════════════
# 3. A modèle's page and its delete
# ══════════════════════════════════════════════════════════════════════


def test_a_modele_page_offers_its_download_never_a_generation(store, web):
    modele = _create("Contrat de services", kind="modele", text="Sans champ")
    page = _html(web, f"/gabarits/{modele}")
    assert "Télécharger le modèle" in page
    assert "Générer un document" not in page
    assert 'href="/modeles/"' in page                  # back to its list
    assert "Champs (" not in page and "Aucun champ détecté" not in page
    assert "Supprimer ce modèle ?" in page
    assert "qui ne seront jamais" not in page
    # A gabarit's page is unchanged.
    gabarit = _create("Lettre type")
    assert "Générer un document" in _html(web, f"/gabarits/{gabarit}")


def test_a_modele_holding_fields_says_they_are_never_filled(store, web):
    modele = _create("Contrat à trous", kind="modele",
                     text="{{client.nom_complet}}")
    page = _html(web, f"/gabarits/{modele}")
    assert "qui ne seront jamais" in page and "« Gabarit »" in page


def test_deleting_a_modele_lands_on_the_modeles_list(store, web):
    db, _ = store
    modele, gabarit = _create("Contrat", kind="modele"), _create("Lettre")
    resp = web.post(f"/gabarits/{modele}/delete")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/modeles/")
    assert db.peek(f"doc_templates/{modele}") is None
    resp = web.post(f"/gabarits/{gabarit}/delete")
    assert resp.headers["Location"].endswith("/gabarits/")


# ══════════════════════════════════════════════════════════════════════
# 4. Never generated
# ══════════════════════════════════════════════════════════════════════


def test_the_generation_popup_never_offers_a_modele(store, web):
    _create("Lettre type")
    _create("Contrat de services", kind="modele")
    popup = _html(web, "/gabarits/generer", headers=HX)
    assert "Lettre type" in popup
    assert "Contrat de services" not in popup


@pytest.mark.parametrize("url", [
    "/gabarits/generer?template_id={tid}&fixed=1",
    "/gabarits/generer/champs?template_id={tid}",
])
def test_the_popup_refuses_a_modele(store, web, url):
    modele = _create("Contrat", kind="modele")
    assert rdt.MODELE_NOT_GENERATED in _html(
        web, url.format(tid=modele), headers=HX)


def test_generating_a_modele_is_refused_and_writes_nothing(
    store, web, monkeypatch
):
    db, bucket = store
    modele = _create("Contrat", kind="modele")
    objects = dict(bucket.objects)
    events = []
    monkeypatch.setattr(rdt, "log_template_event",
                        lambda event, **kw: events.append((event, kw)))
    monkeypatch.setattr(rdt, "get_template_bytes",
                        lambda *_a: pytest.fail("a modèle is never filled"))
    # The popup (htmx): its refusal, at 200.
    resp = web.post("/gabarits/generer", data={"template_id": modele},
                    headers=HX)
    assert resp.status_code == 200
    assert rdt.MODELE_NOT_GENERATED in resp.get_data(as_text=True)
    # The plain POST: back on the modèle's page, the refusal in its banner.
    resp = web.post("/gabarits/generer", data={"template_id": modele})
    assert resp.status_code == 302
    assert rdt.MODELE_NOT_GENERATED in _html(web, resp.headers["Location"])
    assert events == [("generation_failed",
                       {"template_id": modele, "reason": "kind_refused"})] * 2
    assert db.peek_collection("documents") == {}
    assert dict(bucket.objects) == objects
