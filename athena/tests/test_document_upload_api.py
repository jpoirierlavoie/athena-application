"""Téléversement DIRECT-à-GCS du formulaire juriste (décision 2026-08-12).

Les octets ne transitent jamais par l'application — App Engine Standard
plafonne toute requête à 32 Mo (le miroir du plafond de 32 Mo par réponse
qui a frappé les téléchargements la veille) : ouverture d'une session
reprenable sous staging/{uid}/, PUT navigateur→GCS, puis finalisation qui
sniffe une sonde de 512 octets et ingère par copie côté serveur
(ingest_blob_as_document), le staging étant consommé dans les deux issues.
"""

import json
import logging
import os
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ATHENA)

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import concurrency
    import routes.documents as rd

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402


@pytest.fixture()
def web():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(rd.documents_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


@pytest.fixture()
def web_rendu(monkeypatch):
    """App qui rend RÉELLEMENT les gabarits — pour épingler le href que
    l'avocat clique, pas la source du gabarit. Bon marché : base.html
    n'appelle url_for que pour des fichiers statiques (csp_nonce non défini
    se rend vide, inoffensif) ; motif de tests/test_mcp_oauth._make_app."""
    from utils.icons import ms as _ms

    app = Flask(__name__, template_folder=os.path.join(_ATHENA, "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.jinja_env.globals["ms"] = _ms
    app.jinja_env.globals["csrf_token"] = lambda: "jeton-test"
    app.register_blueprint(rd.documents_bp)
    monkeypatch.setattr(rd, "list_dossiers", lambda: [])
    monkeypatch.setattr(
        rd, "get_dossier",
        lambda did: {"id": did, "file_number": "2026-001", "title": "Essai"},
    )
    monkeypatch.setattr(rd, "get_folder_breadcrumb", lambda d, f: [])
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


# The id segment api_televersement mints (uuid4): since lot 2A (T1) it is
# the document's RESERVED id, so the finalization refuses anything else.
_SEG = "3f2b8c1e-9a4d-4c6b-8e2f-1a2b3c4d5e6f"


def _post(web, path, payload):
    return web.post(path, data=json.dumps(payload),
                    content_type="application/json")


# ── GET /documents/upload — return_to validé au rendu ────────────────────


@pytest.mark.parametrize("cible,attendu", [
    ("/dossiers/d1?tab=documents", "/dossiers/d1?tab=documents"),
    ("https://evil.example", ""),
    ("//evil.example", ""),
    ("javascript:alert(1)", ""),
])
def test_upload_form_valide_return_to_au_rendu(web, monkeypatch, cible, attendu):
    # Le JS rejoue return_to dans window.location à la fin du téléversement.
    # Le POST multipart supprimé le passait par safe_internal_redirect — la
    # validation vit désormais AU RENDU (revue 2026-08-12 : sans elle, la
    # redirection ouverte régressait).
    monkeypatch.setattr(rd, "list_dossiers", lambda: [])
    captures: dict = {}
    monkeypatch.setattr(rd, "render_template",
                        lambda gabarit, **ctx: captures.update(ctx) or "ok")
    reponse = web.get("/documents/upload", query_string={"return_to": cible})
    assert reponse.status_code == 200
    assert captures["return_to"] == attendu


def test_les_liens_retour_et_annuler_ramenent_au_navigateur_filtre(web_rendu):
    """Le bogue signalé le 2026-08-13 : le repli du SUCCÈS était scopé, mais
    les liens « retour » et « Annuler » — rendus CÔTÉ SERVEUR — retombaient
    sur la liste PLATE (les trois entrées du navigateur ne passent aucun
    return_to). Rendu RÉEL : on épingle le href cliqué, pas la source du
    gabarit — l'épinglage de source du correctif précédent n'avait rien vu.
    NB : l'autoéchappement Jinja rend « & » en « &amp; » dans un attribut."""
    html = web_rendu.get(
        "/documents/upload?dossier_id=d1&folder_id=f1"
    ).get_data(as_text=True)
    scope = "/documents/?dossier_id=d1&amp;folder_id=f1"
    assert html.count(f'href="{scope}"') == 2      # retour + Annuler
    assert 'href="/documents/"' not in html        # jamais la liste plate


def test_les_liens_retour_et_annuler_sans_dossier_de_classement(web_rendu):
    html = web_rendu.get("/documents/upload?dossier_id=d1").get_data(as_text=True)
    assert html.count('href="/documents/?dossier_id=d1&amp;folder_id="') == 2
    assert 'href="/documents/"' not in html


def test_les_liens_retour_et_annuler_sans_dossier(web_rendu):
    # Arrivée générique (aucun dossier) : la liste plate EST l'endroit d'où
    # l'on vient — le repli scopé ne doit pas inventer un dossier.
    html = web_rendu.get("/documents/upload").get_data(as_text=True)
    assert html.count('href="/documents/"') == 2


def test_return_to_garde_priorite_aux_deux_liens(web_rendu):
    html = web_rendu.get(
        "/documents/upload?dossier_id=d1&folder_id=f1"
        "&return_to=/dossiers/d1%3Ftab%3Ddocuments"
    ).get_data(as_text=True)
    assert html.count('href="/dossiers/d1?tab=documents"') == 2
    assert "dossier_id=d1&amp;folder_id=f1" not in html


def test_le_gabarit_replie_vers_le_navigateur_filtre():
    # Après téléversement, le repli du JS vise le navigateur FILTRÉ au
    # dossier (et au dossier de classement) — la sémantique de l'ancien
    # POST multipart, restaurée le 2026-08-13. Épinglage de source.
    chemin = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "templates", "documents", "upload.html",
    )
    html = pathlib.Path(chemin).read_text(encoding="utf-8")
    assert "'?dossier_id=' + encodeURIComponent(dossierId)" in html
    assert "'&folder_id=' + encodeURIComponent(dossierClassement)" in html


# ── /documents/api/televersement ─────────────────────────────────────────


@pytest.mark.parametrize("nom", ["script.exe", "page.html", "sans_extension"])
def test_televersement_extension_refusee(web, nom):
    reponse = _post(web, "/documents/api/televersement",
                    {"name": nom, "size": 100})
    assert reponse.status_code == 422


def test_televersement_taille_refusee(web):
    trop = 201 * 1024 * 1024
    assert _post(web, "/documents/api/televersement",
                 {"name": "gros.zip", "size": trop}).status_code == 422
    assert _post(web, "/documents/api/televersement",
                 {"name": "vide.pdf", "size": 0}).status_code == 422


def test_televersement_ouvre_une_session_staging(web, monkeypatch):
    blob = mock.Mock()
    blob.create_resumable_upload_session.return_value = "https://up.example/s1"
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(rd.storage, "bucket", lambda: bucket)

    reponse = _post(web, "/documents/api/televersement", {
        "name": "pieces.zip", "size": 130 * 1024 * 1024,
        "content_type": "application/zip",
    })
    assert reponse.status_code == 200
    donnees = reponse.get_json()
    assert donnees["url"] == "https://up.example/s1"
    assert donnees["objet"].startswith("staging/u1/")
    assert donnees["objet"].endswith("/pieces.zip")
    kwargs = blob.create_resumable_upload_session.call_args.kwargs
    assert kwargs["size"] == 130 * 1024 * 1024   # GCS applique le déclaré
    assert kwargs["origin"]                       # CORS porté par la SESSION


# ── /documents/api/finaliser ─────────────────────────────────────────────


def test_finaliser_objet_etranger_400(web):
    # Le client ne nomme jamais que SES objets staging.
    assert _post(web, "/documents/api/finaliser", {
        "objet": "users/u1/dossiers/d1/documents/x/a.pdf",
        "name": "a.pdf", "dossier_id": "d1",
    }).status_code == 400
    assert _post(web, "/documents/api/finaliser", {
        "objet": "staging/autre-uid/x/a.pdf",
        "name": "a.pdf", "dossier_id": "d1",
    }).status_code == 400


def test_finaliser_ingere_et_consomme_le_staging(web, monkeypatch):
    monkeypatch.setattr(
        rd, "get_dossier",
        lambda d: {"id": "d1", "file_number": "2026-001"} if d == "d1" else None,
    )
    blob = mock.MagicMock()
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(rd.storage, "bucket", lambda: bucket)
    ingest = mock.Mock(return_value=({"id": "doc9"}, []))
    monkeypatch.setattr(rd, "ingest_blob_as_document", ingest)

    reponse = _post(web, "/documents/api/finaliser", {
        "objet": f"staging/u1/{_SEG}/pieces.zip", "name": "pieces.zip",
        "dossier_id": "d1", "category": "pièce", "tags": "a, b",
        "folder_id": "", "description": "", "display_name": "",
        "document_date": "",
    })
    assert reponse.status_code == 200
    assert reponse.get_json()["document_id"] == "doc9"
    args = ingest.call_args.args
    assert args[0] is blob and args[1] == "d1" and args[2] == "2026-001"
    assert args[3] == "pieces.zip"
    assert args[4]["tags"] == ["a", "b"]
    # Deux finalisations du même téléversement → UN document (lot 2A, T1).
    assert ingest.call_args.kwargs["document_id"] == _SEG
    blob.delete.assert_called_once()             # staging consommé


def test_finaliser_refus_consomme_le_staging_aussi(web, monkeypatch):
    monkeypatch.setattr(rd, "get_dossier",
                        lambda d: {"id": "d1", "file_number": "2026-001"})
    blob = mock.MagicMock()
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(rd.storage, "bucket", lambda: bucket)
    monkeypatch.setattr(
        rd, "ingest_blob_as_document",
        mock.Mock(return_value=(None, ["Le contenu du fichier ne "
                                       "correspond pas à son extension."])),
    )
    reponse = _post(web, "/documents/api/finaliser", {
        "objet": f"staging/u1/{_SEG}/piece.pdf", "name": "piece.pdf",
        "dossier_id": "d1",
    })
    assert reponse.status_code == 422
    assert "extension" in reponse.get_json()["erreur"]
    blob.delete.assert_called_once()             # des octets refusés non plus


@pytest.mark.parametrize("objet", [
    # The zip exports live under the same staging prefix: not an upload.
    "staging/u1/exports/3f2b8c1e-9a4d-4c6b-8e2f-1a2b3c4d5e6f/lot.zip",
    # A proof of identity's staging object (models/piece_identite): only
    # its own finalizer files it, on a contact — never as a document.
    "staging/u1/identite/3f2b8c1e-9a4d-4c6b-8e2f-1a2b3c4d5e6f/upload.pdf",
    "staging/u1/aaaa/piece.pdf",                                  # not a uuid4
    "staging/u1/3F2B8C1E-9A4D-4C6B-8E2F-1A2B3C4D5E6F/piece.pdf",  # not canonical
    "staging/u1/3f2b8c1e-9a4d-1c6b-8e2f-1a2b3c4d5e6f/piece.pdf",  # uuid1
    f"staging/u1/{_SEG}/",                                         # no name
])
def test_finaliser_refuse_un_segment_qui_n_est_pas_un_identifiant(
    web, monkeypatch, objet,
):
    """Lot 2A (T1) : le segment uuid4 du chemin staging devient l'id
    RÉSERVÉ du document. Tout autre segment est refusé AVANT de toucher
    l'objet — ni lecture, ni suppression, ni ingestion."""
    monkeypatch.setattr(rd, "get_dossier",
                        lambda d: {"id": "d1", "file_number": "2026-001"})
    bucket = mock.Mock()
    monkeypatch.setattr(rd.storage, "bucket", lambda: bucket)
    ingest = mock.Mock()
    monkeypatch.setattr(rd, "ingest_blob_as_document", ingest)

    reponse = _post(web, "/documents/api/finaliser", {
        "objet": objet, "name": "piece.pdf", "dossier_id": "d1"})

    assert reponse.status_code == 400
    assert reponse.get_json()["erreur"] == "Requête invalide."
    bucket.blob.assert_not_called()
    ingest.assert_not_called()


# ── Revue adversariale de T1 (2026-09-27) — chacun échoue sur 04722bb ────
#
# The model now REFUSES a display name or tags that `sanitize` would alter
# (lot 2A, T1). Judged only at finalization, such a refusal cost the whole
# upload — up to 200 MB, the staging object being consumed on refusal. The
# same judgement now runs at session open, before the first byte.


@pytest.mark.parametrize("champs, fragment", [
    ({"display_name": "Lettre <brouillon>"}, "chevrons"),
    ({"display_name": "x" * 301}, "300 caractères"),
    ({"tags": ", ".join(f"t{i}" for i in range(31))}, "30 au plus"),
    ({"category": "inventée"}, "Catégorie invalide"),
])
def test_televersement_juge_les_metadonnees_avant_le_premier_octet(
    web, monkeypatch, champs, fragment,
):
    bucket = mock.Mock()
    monkeypatch.setattr(rd.storage, "bucket", lambda: bucket)

    reponse = _post(web, "/documents/api/televersement", {
        "name": "piece.pdf", "size": 150 * 1024 * 1024, **champs})

    assert reponse.status_code == 422
    assert fragment in reponse.get_json()["erreur"]
    bucket.blob.assert_not_called()          # aucune session, aucun octet


def test_televersement_accepte_des_metadonnees_valides(web, monkeypatch):
    blob = mock.Mock()
    blob.create_resumable_upload_session.return_value = "https://up.example/s2"
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(rd.storage, "bucket", lambda: bucket)

    reponse = _post(web, "/documents/api/televersement", {
        "name": "piece.pdf", "size": 100, "display_name": "Lettre a < b",
        "tags": "urgent, client", "category": "pièce",
        "document_date": "2026-07-15"})

    assert reponse.status_code == 200
    assert reponse.get_json()["url"] == "https://up.example/s2"


def test_le_formulaire_envoie_les_metadonnees_a_l_ouverture():
    """Source pin: the page sends the four fields with the session-open
    request, not only with the finalization — otherwise the check above
    never sees them.

    Narrowed deliberately (2026-11, the uncertain finalization): the slice
    ran up to the finalization's URL, and traiter now reads the
    finalization's fields once, between the two — the pin would have stayed
    green with the fields gone from the session-open request. It now ends
    where that request's answer is read."""
    chemin = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "templates", "documents", "upload.html",
    )
    html = pathlib.Path(chemin).read_text(encoding="utf-8")
    debut = html.index("documents.api_televersement")
    ouverture = html[debut:html.index("ouverture.json()", debut)]
    for champ in ("category", "tags", "display_name", "document_date"):
        assert f"{champ}: this.champ('{champ}')" in ouverture, champ


def _staging_consomme(monkeypatch):
    from google.cloud.exceptions import NotFound

    blob = mock.MagicMock()
    # The text names the object, as the service's does: the client's file
    # name is its last segment.
    blob.reload.side_effect = NotFound(
        f"404 No such object: staging/u1/{_SEG}/Passeport Tremblay.pdf")
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(rd.storage, "bucket", lambda: bucket)
    monkeypatch.setattr(rd, "get_dossier",
                        lambda d: {"id": d, "file_number": "2026-001"})
    ingest = mock.Mock()
    monkeypatch.setattr(rd, "ingest_blob_as_document", ingest)
    return ingest


@pytest.fixture()
def magasin(monkeypatch):
    """The REAL document readers over the fake Firestore
    (tests/_fake_firestore: the real client, a fake server) — the replay
    branch reads the reserved id through ``get_document_strict``, and a
    mocked reader would hide which one it calls."""
    return install(monkeypatch, rd.document_model)


def _deja_verse(magasin, dossier_id="d1"):
    magasin.seed(f"documents/{_SEG}", {
        **rd.document_model._default_doc(), "id": _SEG,
        "dossier_id": dossier_id, "display_name": "Pièce", "etag": "e0",
    })


def _rejoue(web):
    return _post(web, "/documents/api/finaliser", {
        "objet": f"staging/u1/{_SEG}/Passeport Tremblay.pdf",
        "name": "Passeport Tremblay.pdf", "dossier_id": "d1"})


def test_finaliser_rejoue_rend_le_document_deja_verse(web, magasin, monkeypatch):
    """The same finalization arriving AGAIN after the first consumed the
    staging object: the reserved id says it landed. « Introuvable » would
    have invited a second upload of the same file. Rewritten deliberately
    (2026-11): the reserved id is now read STRICTLY, on the fake store, no
    longer through a patched fail-open reader."""
    ingest = _staging_consomme(monkeypatch)
    _deja_verse(magasin)

    reponse = _rejoue(web)

    assert reponse.status_code == 200
    assert reponse.get_json() == {"ok": True, "document_id": _SEG}
    ingest.assert_not_called()


@pytest.mark.parametrize("existant", [None, "autre"])
def test_finaliser_sans_document_verse_reste_introuvable(
    web, magasin, monkeypatch, caplog, existant,
):
    _staging_consomme(monkeypatch)
    if existant:
        _deja_verse(magasin, dossier_id=existant)
    caplog.set_level(logging.INFO)

    reponse = _rejoue(web)

    assert reponse.status_code == 422
    assert "introuvable" in reponse.get_json()["erreur"]
    # The class, never the traceback: the error's text names the staging
    # object, whose last segment is the client's file name.
    lignes = [(r.getMessage(), r.exc_info) for r in caplog.records
              if r.name == rd.logger.name]
    assert lignes == [("documents: staging blob reload failed: NotFound", None)]


def test_une_reprise_dont_la_lecture_echoue_est_incertaine_jamais_un_refus(
    web, magasin, monkeypatch, caplog,
):
    """Fails on the old code: the replay branch read the reserved id with
    the FAIL-OPEN ``get_document``, so a read failure passed for « no
    document » — a 422 « Fichier téléversé introuvable. Réessayez. » about
    an upload that may have landed, inviting the lawyer to file it a second
    time. Fails on round 1 too: its 503 READ_UNAVAILABLE_ERROR said « Rien
    n'a été enregistré », a refusal to the page, which re-sent the file
    through a NEW upload — a second document when this request was the
    network's replay of a finalization that had filed it. Now: the strict
    reader, a 503 that says the outcome is unknown (``incertain``, with the
    page's one sentence: the page re-posts once), one ERROR with ids only
    and no traceback, nothing filed by this call."""
    ingest = _staging_consomme(monkeypatch)
    _deja_verse(magasin)

    def _panne(*_a, **_kw):
        raise gexc.ServiceUnavailable("firestore indisponible")

    monkeypatch.setattr(magasin._fake_server, "batch_get_documents", _panne)
    caplog.set_level(logging.INFO)

    reponse = _rejoue(web)

    assert reponse.status_code == 503
    assert reponse.get_json() == {"erreur": _INCERTAIN, "incertain": True}
    assert concurrency.READ_UNAVAILABLE_ERROR not in reponse.get_data(
        as_text=True)
    ingest.assert_not_called()
    lignes = [(r.getMessage(), r.json_fields)
              for r in caplog.records if r.name == "pallas.unexpected"]
    assert lignes == [("documents: reserved document unreadable", {
        "event": "unexpected", "document_id": _SEG,
        "error_type": "ServiceUnavailable"})]
    assert not any(r.exc_info for r in caplog.records)
    assert not any("Tremblay" in r.getMessage() for r in caplog.records)


def test_un_echec_de_relecture_du_staging_ne_journalise_que_sa_classe(
    web, monkeypatch, caplog,
):
    """Fails on the old code: a staging reload failing other than NotFound
    (a Storage 503) was logged by ``logger.exception``, a traceback whose
    error text names the staging object — its last segment is the client's
    file name. Still the 422 « introuvable » (this call filed nothing); the
    one line now carries the class only."""
    blob = mock.MagicMock()
    blob.reload.side_effect = gexc.ServiceUnavailable(
        f"503 GET staging/u1/{_SEG}/Passeport Tremblay.pdf")
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(rd.storage, "bucket", lambda: bucket)
    monkeypatch.setattr(rd, "get_dossier",
                        lambda d: {"id": d, "file_number": "2026-001"})
    ingest = mock.Mock()
    monkeypatch.setattr(rd, "ingest_blob_as_document", ingest)
    caplog.set_level(logging.DEBUG)

    reponse = _rejoue(web)

    assert reponse.status_code == 422
    assert reponse.get_json() == {
        "erreur": "Fichier téléversé introuvable. Réessayez."}
    ingest.assert_not_called()
    lignes = [(r.getMessage(), r.exc_info) for r in caplog.records
              if r.name == rd.logger.name]
    assert lignes == [
        ("documents: staging blob reload failed: ServiceUnavailable", None)]
    for r in caplog.records:
        textes = (r.getMessage(), repr(r.exc_info or ""), r.exc_text or "",
                  repr(getattr(r, "json_fields", "")))
        assert not any("Tremblay" in t for t in textes), r


# ── La page : une finalisation dont la réponse se perd (2026-11) ─────────
#
# Source pins of the page's half of the replay contract above, mirroring the
# proofs-of-identity dialog (tests/test_pieces_identite_routes.py). The
# behaviour itself — a dropped answer settled by the re-post, both dropped
# left « incertain » and never re-sent, a gateway's 502 — was driven in
# headless Chrome against the real blueprint; CI cannot run a browser.

_UPLOAD = pathlib.Path(_ATHENA) / "templates" / "documents" / "upload.html"
# Spelled out: compared to the route's constant, a test would raise
# AttributeError on the old code instead of failing on what it pins.
_INCERTAIN = (
    "Le versement n'a pas pu être confirmé : vérifiez la liste des "
    "documents du dossier ; s'il n'y figure pas, retirez-le de cette "
    "liste (×) et ajoutez-le de nouveau."
)
_CONFIG = re.compile(
    r'<script type="application/json" id="televersement-config">(.*?)</script>',
    re.S)


def _method(source: str, name: str) -> str:
    """One method of an object literal, from its name to its closing brace
    (no brace of these methods sits unbalanced in a string or a comment)."""
    m = re.search(rf"^ *(?:async )?{name}\([^)]*\) \{{", source, re.M)
    assert m, name
    depth = 0
    for i in range(source.index("{", m.start()), len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[m.start():i + 1]
    raise AssertionError(f"{name}: unbalanced")


def _block(source: str, opener: str) -> str:
    """The block *opener* starts, to the brace that closes its last ``{``."""
    assert opener.endswith("{"), opener
    start = source.index(opener)
    depth = 0
    for i in range(start + len(opener) - 1, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError(f"{opener}: unbalanced")


def test_une_reponse_de_finalisation_perdue_est_incertaine_jamais_un_refus():
    """Fails on the old page: its finalization was inline, and a REJECTED
    fetch — the answer lost, the document perhaps filed — fell to the
    outer catch, « erreur réseau — réessayez », a refusal the next submit
    re-sent through a NEW upload: a second copy of the document. So did a
    2xx whose body could not be read, and a gateway's 5xx page.

    finaliser answers ``incertain`` for those three only: a rejected fetch;
    a 2xx whose body could not be READ (cut after its headers — a body that
    is not JSON at all is a SyntaxError, a refusal); a 5xx that is not the
    endpoint's JSON (no ``erreur``). The endpoint's own 5xx keep their
    sentence, and a 4xx — or a 200 that is not the JSON (a refused token,
    an expired session's login page) — filed nothing: a refusal.

    Fails on the round-1 page too, whose verdict forced ``incertain:
    false``: the endpoint's own « incertain » (a replay whose read of the
    reserved id failed — the document perhaps filed) became a refusal the
    next submit re-sent. The verdict now carries it, as the proofs
    dialog's does."""
    source = _UPLOAD.read_text(encoding="utf-8")
    finaliser = _method(source, "finaliser")
    assert finaliser.count("fetch(") == 1
    guarded = _block(finaliser, "try {")
    assert "fin = await fetch('{{ url_for(\"documents.api_finaliser\") }}'" in guarded
    caught = _block(finaliser, "} catch (e) {")
    assert "return { ok: false, incertain: true, erreur: '' };" in caught
    parsed = finaliser.index("const resultat = await fin.json()")
    verdict = finaliser.index("ok: fin.ok && resultat.ok === true,")
    assert "illisible = !(e instanceof SyntaxError);" in finaliser
    lost_body = _block(finaliser, "if (fin.ok && illisible) {")
    foreign = _block(finaliser, "if (fin.status >= 500 && !resultat.erreur) {")
    for branch in (lost_body, foreign):
        assert "return { ok: false, incertain: true, erreur: '' };" in branch
        assert parsed < finaliser.index(branch) < verdict
    assert finaliser.count(
        "return { ok: false, incertain: true, erreur: '' };") == 3
    assert "incertain: resultat.incertain === true," in finaliser[verdict:]
    assert "incertain: false" not in finaliser
    assert "erreur: resultat.erreur || ''," in finaliser[verdict:]
    # The verdict is finaliser's only way out after its three uncertain
    # branches: an early return for a non-ok answer would drop the
    # endpoint's own « incertain » (a replay whose read failed) and make
    # it a refusal, re-sent by the next submit.
    assert finaliser.count("return") == 5
    tail = finaliser[finaliser.index(foreign) + len(foreign):]
    assert tail.count("return") == 1
    assert tail.lstrip().startswith("return {")


def test_la_page_reprend_une_finalisation_incertaine_une_fois_apres_deux_secondes():
    """Fails on the old page (no re-post, no « incertain »). ``incertain``
    gets ONE more finalization, ~2 s later — the SAME one: its fields read
    once, before the first post — and only an ``ok`` re-post replaces the
    first answer. Any other leaves the file « incertain » with the honest
    sentence, never « rejeté ». A failure BEFORE any finalization (the
    session, an interrupted PUT) filed nothing: it stays « rejeté »."""
    source = _UPLOAD.read_text(encoding="utf-8")
    traiter = _method(source, "traiter")
    assert traiter.count("this.finaliser(f, dossierId)") == 2
    assert "while" not in traiter
    branch = _block(traiter, "if (resultat.incertain) {")
    repost = branch.index("resultat = await this.finaliser(f, dossierId);")
    assert branch.index("setTimeout(r, 2000)") < repost
    kept = _block(branch, "if (!resultat.ok) {")
    assert branch.index(kept) > repost
    assert "f.statut = 'incertain'; f.motif = CONFIG.incertain;" in kept
    assert "return;" in kept and "rejeté" not in kept
    # Nothing decides the file between the first finalization and the
    # uncertain branch, and the refusal comes AFTER that branch: a refusal
    # judged first would turn a lost answer back into « rejeté », re-sent.
    first = traiter.index("let resultat = await this.finaliser(f, dossierId);")
    between = traiter[first:traiter.index(branch)]
    assert "f.statut" not in between and "return" not in between
    after = traiter[traiter.index(branch) + len(branch):]
    assert "f.statut = 'rejeté'" in _block(after, "if (!resultat.ok) {")
    # The same finalization: the fields are read ONCE, before the first
    # post, and finaliser reads none of its own.
    assert traiter.index("f.champs = {") < traiter.index("this.finaliser(")
    assert "this.champ(" not in _method(source, "finaliser")
    assert "...f.champs" in _method(source, "finaliser")
    # Only what precedes the finalization can end « erreur réseau ».
    pre = _block(traiter, "try {")
    assert "this.televerser(f, donnees.url)" in pre and "finaliser" not in pre
    caught = _block(traiter, "} catch (e) {")
    assert "f.statut = 'rejeté'" in caught and "return;" in caught
    assert traiter.index(caught) < traiter.index("this.finaliser(")


def test_un_fichier_incertain_ne_repart_jamais_et_garde_la_page():
    """Fails on the old page. A later « Téléverser » (re)sends only a file
    never sent or REFUSED — never an « incertain » one: re-sent, it would
    open a new upload and file a second document. A batch leaving a file
    refused or uncertain keeps the page, each line in the banner (name and
    reason); only a batch entirely filed redirects. With nothing left to
    send, the uncertain line stays rather than « select a file »."""
    source = _UPLOAD.read_text(encoding="utf-8")
    soumettre = _method(source, "soumettre")
    attente = _block(soumettre,
                     "const attente = this.files.filter(function (f) {")
    assert "f.statut === 'en attente' || f.statut === 'rejeté'" in attente
    assert "incertain" not in attente
    lignes = _method(source, "lignesEchec")
    assert "f.statut === 'rejeté' || f.statut === 'incertain'" in lignes
    assert "f.name + ' : ' + (f.motif || 'refusé')" in lignes
    rien = _block(soumettre, "if (!attente.length) {")
    assert "this.lignesEchec()" in rien and "traiter" not in rien
    assert "const echecs = this.lignesEchec();" in soumettre
    assert soumettre.count("this.traiter(") == 1   # the attente loop only
    assert source.count("this.traiter(") == 1      # nothing else calls it
    assert "window.location" in _block(soumettre, "if (!echecs.length) {")
    assert soumettre.rstrip("}").rstrip().endswith("this.erreurs = echecs;")
    # The row reads the sentence alone, in amber — before the generic
    # « statut · taille », which would hide it.
    etiquette = _method(source, "etiquette")
    incertain = etiquette.index(
        "if (f.statut === 'incertain') { return f.motif; }")
    assert incertain < etiquette.index("return f.statut + ' · '")
    assert ("(file.statut === 'incertain' ? 'text-amber-700' : 'text-gray-500')"
            in source)


def test_la_phrase_incertaine_n_a_qu_une_source(web_rendu):
    """Fails on the round-1 page: the sentence was a literal in its script
    — a copy the endpoint could not share — and it told the lawyer to
    « téléverser de nouveau » a file that « Téléverser » never re-sends.
    ONE constant now, ``routes/documents.UPLOAD_UNCERTAIN``: the replay
    branch answers it, and the page receives it in its JSON config (CSP:
    server data never inside the script). It names the page's real way to
    send the file again: the row's remove button (×), then the file added
    anew — a NEW upload."""
    html = web_rendu.get("/documents/upload?dossier_id=d1").get_data(
        as_text=True)
    configs = _CONFIG.findall(html)
    assert len(configs) == 1
    assert json.loads(configs[0]) == {"incertain": _INCERTAIN}
    assert getattr(rd, "UPLOAD_UNCERTAIN", None) == _INCERTAIN
    source = _UPLOAD.read_text(encoding="utf-8")
    assert "pas pu être confirmé" not in source            # no second copy
    assert "document.getElementById('televersement-config').textContent" in (
        _block(source, "function uploadForm() {"))
    assert "retirez-le de cette liste (×) et ajoutez-le de nouveau" in _INCERTAIN
    for mot in ("téléversez", "réessayez", "refus"):
        assert mot not in _INCERTAIN, mot


def test_le_bouton_retirer_offre_une_cible_de_44_px(web_rendu):
    """Fails on the old page: the row's remove button — the way to send
    again a file left « incertain », which its sentence names — was a bare
    16 px glyph, 17 × 26 px at 375 px (Rule 9 asks 44). Now p-3 around a
    20 px glyph (21 px rendered: 12 + 21 + 12), the proofs dialog's icon
    buttons; icon-only, it keeps its aria-label and its title."""
    from utils.icons import ms

    html = web_rendu.get("/documents/upload?dossier_id=d1").get_data(
        as_text=True)
    assert html.count('aria-label="Retirer le fichier"') == 1
    libelle = html.index('aria-label="Retirer le fichier"')
    bouton = html[html.rindex("<button", 0, libelle):
                  html.index("</button>", libelle)]
    assert 'title="Retirer le fichier"' in bouton
    classes = set(re.search(r' class="([^"]*)"', bouton).group(1).split())
    assert {"inline-flex", "items-center", "justify-center", "p-3"} <= classes
    assert str(ms("close", 20)) in bouton


def _escape_css(cls: str) -> str:
    b = "\\"
    for raw, esc in ((b, b * 2), (":", b + ":"), (".", b + "."),
                     ("/", b + "/"), ("[", b + "["), ("]", b + "]")):
        cls = cls.replace(raw, esc)
    return cls


def _not_compiled(classes) -> list[str]:
    css = next(pathlib.Path(_ATHENA).glob("static/vendor/app.*.css")).read_text(
        encoding="utf-8")
    absent = []
    for c in sorted(classes):
        needle = "." + _escape_css(c)
        hits = [m.end() for m in re.finditer(re.escape(needle), css)]
        if not any(i >= len(css) or not (css[i].isalnum() or css[i] in "-_\\")
                   for i in hits):
            absent.append(c)
    return absent


def _classes(markup: str) -> set[str]:
    """Every class the markup can apply: the static ``class="…"`` lists and
    the literals of the ``:class`` expressions."""
    found = {c for block in re.findall(r'(?<![\w:@-])class="([^"]*)"', markup)
             for c in block.split()}
    for expr in re.findall(r':class="([^"]*)"', markup):
        for literal in re.findall(r"[?:]\s*'([^']+)'", expr):
            found |= set(literal.split())
    return found


def test_le_formulaire_n_emploie_que_des_classes_compilees(web_rendu):
    """A class absent from the compiled stylesheet silently does not apply
    (Change Impact 6): the amber of an « incertain » row included."""
    html = web_rendu.get("/documents/upload?dossier_id=d1").get_data(as_text=True)
    debut = html.index('<form class="space-y-6"')
    form = html[debut:html.index("</form>", debut)]
    classes = _classes(form)
    assert {"text-red-600", "text-green-700", "text-amber-700",
            "text-gray-500", "bg-red-50"} <= classes            # non-vacuous
    assert _not_compiled(classes) == []
    assert _not_compiled({"min-h-11", "shrink-0"}) == ["min-h-11", "shrink-0"]
