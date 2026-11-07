"""Proofs of identity on the contact fiche — the routes, the card, the dialog.

Lot 2 of the compliance work (2026-11). The model (``models/piece_identite``)
is pinned by ``tests/test_pieces_identite_model.py``; this file pins what
the lawyer's browser meets: the five routes of ``parties_bp``, the block
inside the Conformité card (``parties/_pieces_identite.html``) and the
« Gérer » dialog (``parties/_pieces_identite_gestion.html``).

Everything runs through the REAL blueprint, models and templates, on the
fake Firestore (``tests/_fake_firestore.py``: the real client, a fake
server) and the fake Cloud Storage (``tests/_fake_gcs.py``: bytes,
generations, create-only sessions, 412s); what is asserted is what the
STORE and the BUCKET hold, what the browser receives, and what the logs
carry.

1. the upload — the session and the filing end to end, foreign staging
   shapes refused both ways, the unknown outcome the page re-posts once;
2. « Consulter » — the signed redirect, and each failure's own sentence;
3. rename and delete — the redirects (Rule 8), ONE audit row and ONE
   event per deletion, even under a double submission;
4. the fiche — visibility, the failed and truncated reads in the card AND
   the dialog, the CSRF token the script sends, accessibility;
5. pins — the JS twins, compiled classes, vendored icons, no PII logged.
"""

import json
import logging
import os
import pathlib
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync  # its db is patched below
    from models import audit_event as audit_model
    from models import concurrency
    from models import document as document_model
    from models import partie as pm
    from models import piece_identite as pi
    import routes.admin_ledger as admin_routes
    import routes.documents as documents_routes
    import routes.parties as parties_routes

from flask import Blueprint, Flask  # noqa: E402
from google.api_core import exceptions as gexc  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBlob, FakeBucket  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils import storage_identity  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.html_attr import jsattr  # noqa: E402
from utils.icons import MATERIAL_ICONS, ms  # noqa: E402
from utils.markdown_docx import markdown_to_safe_html  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it.
_ = (dav_sync, audit_model)

UTC = timezone.utc
T0 = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
UID = "u1"
PID = "p1"
PROOF = "3f2b8c1e-9d4a-4e6b-8f7a-1c2d3e4f5a6b"
PROOF2 = "9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d"

PDF = b"%PDF-1.7\n" + b"permis de conduire " * 20
JPEG = b"\xff\xd8\xff\xe0" + b"photo " * 40
PNG = b"\x89PNG\r\n\x1a\n" + b"image " * 40

SIGNED_URL = ("https://storage.googleapis.com/fake-bucket/preuve"
              "?X-Goog-Signature=abc")

SESSION = f"/parties/{PID}/pieces-identite/televersement"
FINALIZE = f"/parties/{PID}/pieces-identite/finaliser"

_TEMPLATES = _ATHENA / "templates"
_GESTION = _TEMPLATES / "parties" / "_pieces_identite_gestion.html"
_UPLOAD = _TEMPLATES / "documents" / "upload.html"


def _consult(proof_id=PROOF) -> str:
    return f"/parties/{PID}/pieces-identite/{proof_id}"


def _rename(proof_id=PROOF) -> str:
    return f"/parties/{PID}/pieces-identite/{proof_id}/renommer"


def _delete(proof_id=PROOF) -> str:
    return f"/parties/{PID}/pieces-identite/{proof_id}/supprimer"


# ── Harness ──────────────────────────────────────────────────────────────


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, *_fake_modules())


@pytest.fixture
def bucket(monkeypatch):
    fake = FakeBucket()
    # firebase_admin.storage — the module the model, routes/documents and
    # routes/admin_ledger all reach.
    monkeypatch.setattr(pi.storage, "bucket", lambda: fake)
    return fake


@pytest.fixture
def signed(monkeypatch):
    """Every signing « Consulter » asks for — the URL is a fake capability."""
    calls: list = []

    def _sign(blob, params, expiry_minutes=15):
        calls.append({"name": blob.name, "params": dict(params),
                      "minutes": expiry_minutes})
        return SIGNED_URL

    monkeypatch.setattr(pi, "sign_blob_url", _sign)
    return calls


def _app() -> Flask:
    app = Flask(__name__, template_folder=str(_TEMPLATES),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms,
                                 csp_nonce="n")
    # The REAL jsattr (utils/html_attr: a test that reads x-data registers
    # THIS function), never the `lambda v: v` stub.
    app.jinja_env.filters.update(
        to_mtl=to_mtl, phone=format_phone_display,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=jsattr, markdown=markdown_to_safe_html,
    )
    app.register_blueprint(parties_routes.parties_bp)
    # The fiche links to other blueprints; stubs carry the endpoint names
    # so the REAL templates render without their imports.
    for name, rules in (
        ("doc_templates", (("/gabarits/generer", "generate_modal"),)),
        ("dossiers", (("/dossiers/new", "dossier_new"),
                      ("/dossiers/<dossier_id>", "dossier_detail"))),
        ("reception", (("/reception/inviter", "inviter_form"),)),
        ("auth", (("/login", "login"),)),
    ):
        stub = Blueprint(name, __name__ + "." + name)
        for rule, endpoint in rules:
            stub.add_url_rule(rule, endpoint, lambda **kw: "")
        app.register_blueprint(stub)
    return app


def _client(user_id=UID):
    client = _app().test_client()
    if user_id is not None:
        with client.session_transaction() as s:
            s["user_id"] = user_id
            s["email"] = "test@example.com"
            s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return client


@pytest.fixture
def web(db, bucket):
    return _client()


def _bare_client(blueprint, user_id=UID):
    """Another blueprint's JSON endpoints, alone (no template renders)."""
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(blueprint)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = user_id
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return client


def _seed_contact(db, pid=PID, **over):
    doc = {**pm._default_doc(), "id": pid, "type": "individual",
           "contact_role": "client", "first_name": "Jean",
           "last_name": "Tremblay", "etag": f"e-{pid}",
           "created_at": T0, "updated_at": T0}
    doc.update(over)
    db.seed(f"parties/{pid}", doc)
    return pid


def _final(proof_id=PROOF, ext=".pdf", partie_id=PID) -> str:
    return f"users/{UID}/parties/{partie_id}/pieces_identite/{proof_id}{ext}"


def _seed_proof(db, bucket, proof_id=PROOF, *, ext=".pdf", data=PDF,
                display="Permis de conduire", path=None, created_at=T0,
                partie_id=PID) -> dict:
    """A filed proof planted directly: its record and (unless *data* is
    None) its object."""
    path = path if path is not None else _final(proof_id, ext, partie_id)
    if data is not None:
        bucket.put(path, data, content_type=pi.MIME_TYPES[ext])
    record = {
        "id": proof_id, "partie_id": partie_id, "display_name": display,
        "file_type": pi.MIME_TYPES[ext], "file_size": len(data or b""),
        "storage_path": path, "created_at": created_at,
        "updated_at": created_at, "etag": f"e-{proof_id[:8]}",
        "created_via": "web", "updated_via": "web",
    }
    db.seed(f"parties/{partie_id}/pieces_identite/{proof_id}", record)
    return record


def _seed_many(db, bucket, n: int) -> list[str]:
    ids = []
    for i in range(n):
        proof_id = str(uuid.uuid4())
        _seed_proof(db, bucket, proof_id, display=f"Pièce {i:02d}",
                    created_at=T0 + timedelta(minutes=i))
        ids.append(proof_id)
    return ids


def _boom(*_a, **_kw):
    raise gexc.ServiceUnavailable("store unavailable")


def _landing(resp) -> tuple[str, dict, str]:
    """``(path, query, fragment)`` of a redirect."""
    assert resp.status_code == 302, resp.get_data(as_text=True)[:300]
    target = urlparse(resp.headers["Location"])
    query = {k: v[0] for k, v in parse_qs(target.query).items()}
    return target.path, query, target.fragment


def _back_to_proofs(resp) -> dict:
    """The query of a redirect back to the fiche, dialog reopened."""
    path, query, fragment = _landing(resp)
    assert path == f"/parties/{PID}"
    assert fragment == "preuves-identite"
    assert query.pop("gerer") == "1"
    return query


def _events(caplog) -> list:
    return [(r.json_fields["event"],
             {k: v for k, v in r.json_fields.items() if k != "event"})
            for r in caplog.records if r.name == "pallas.partie"]


def _unexpected(caplog) -> list:
    return [(r.getMessage(),
             {k: v for k, v in r.json_fields.items() if k != "event"},
             r.exc_info)
            for r in caplog.records if r.name == "pallas.unexpected"]


def _audit_rows(db) -> list:
    return list(db.peek_collection("audit_events").values())


def _card(html: str) -> str:
    """The proofs block inside the Conformité card."""
    start = html.index('<section id="preuves-identite"')
    return html[start:html.index("</section>", start)]


def _dialog(html: str) -> str:
    """The « Gérer » dialog's markup (its script excluded)."""
    start = html.index('id="preuves-identite-dialogue"')
    return html[start:html.index("<script nonce=", start)]


def _upload_zone(html: str) -> str:
    """The dialog's upload zone: its last separated block."""
    dialog = _dialog(html)
    return dialog[dialog.rindex(
        '<div class="mt-4 pt-4 border-t border-gray-100">'):]


def _script(html: str) -> str:
    start = html.index("function preuvesIdentite()")
    return html[start:html.index("</script>", start)]


def _config(html: str) -> dict:
    m = re.search(r'<script type="application/json" '
                  r'id="preuves-identite-config">(.*?)</script>', html, re.S)
    assert m, "no config block"
    return json.loads(m.group(1))


def _fiche(web, query: str = "") -> str:
    resp = web.get(f"/parties/{PID}{query}")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


# ══════════════════════════════════════════════════════════════════════
# 1. The upload — session, bytes, filing
# ══════════════════════════════════════════════════════════════════════


def _open(web, name, size):
    return web.post(SESSION, json={"name": name, "size": size})


def _finalize(web, objet, name):
    return web.post(FINALIZE, json={"objet": objet, "name": name})


@pytest.mark.parametrize("name, data, ext, mime, stem", [
    ("Permis de conduire.pdf", PDF, ".pdf", "application/pdf",
     "Permis de conduire"),
    # Upper-case extensions — a phone's camera, a scanner — are filed
    # under their lower-cased form, never refused after the upload.
    ("PHOTO.JPG", JPEG, ".jpg", "image/jpeg", "PHOTO"),
    ("SCAN.PDF", PDF, ".pdf", "application/pdf", "SCAN"),
    ("recto.png", PNG, ".png", "image/png", "recto"),
])
def test_a_proof_is_uploaded_then_filed_through_the_two_endpoints(
        web, db, bucket, caplog, name, data, ext, mime, stem):
    _seed_contact(db)
    caplog.set_level(logging.INFO)

    opened = _open(web, name, len(data))

    assert opened.status_code == 200, opened.get_data(as_text=True)
    body = opened.get_json()
    assert set(body) == {"url", "objet"}
    m = re.fullmatch(rf"staging/{UID}/identite/([0-9a-f-]{{36}})/upload"
                     rf"{re.escape(ext)}", body["objet"])
    assert m, body["objet"]
    proof_id = m.group(1)
    session = bucket.sessions[body["url"]]
    # Create-only, the declared size, the whitelist's type (never the
    # browser's), the page's origin as the session's CORS policy.
    assert (session["name"], session["content_type"], session["size"],
            session["origin"], session["if_generation_match"]) == (
        body["objet"], mime, len(data), "http://localhost", 0)
    bucket.complete_session(body["url"], data)

    filed = _finalize(web, body["objet"], name)

    assert filed.status_code == 200, filed.get_data(as_text=True)
    assert filed.get_json() == {
        "ok": True, "proof_id": proof_id,
        "suivant": f"/parties/{PID}?gerer=1#preuves-identite"}
    path = _final(proof_id, ext)
    record = db.peek(f"parties/{PID}/pieces_identite/{proof_id}")
    assert (record["storage_path"], record["display_name"],
            record["file_type"], record["file_size"]) == (
        path, stem, mime, len(data))
    assert bucket.objects[path].data == data
    assert bucket.objects[path].content_type == mime
    assert body["objet"] not in bucket.objects          # staging consumed
    assert _events(caplog) == [("id_proof_filed", {
        "partie_id": PID, "proof_id": proof_id, "file_type": mime,
        "size": len(data)})]
    assert stem in _card(_fiche(web))


def test_a_replayed_finalization_answers_ok_and_logs_no_second_filing(
        web, db, bucket, caplog):
    _seed_contact(db)
    caplog.set_level(logging.INFO)
    body = _open(web, "Permis.pdf", len(PDF)).get_json()
    bucket.complete_session(body["url"], PDF)
    first = _finalize(web, body["objet"], "Permis.pdf").get_json()

    again = _finalize(web, body["objet"], "Permis.pdf")

    assert again.status_code == 200
    assert again.get_json()["proof_id"] == first["proof_id"]
    assert len(db.peek_collection(f"parties/{PID}/pieces_identite")) == 1
    assert [e for e, _ in _events(caplog)] == ["id_proof_filed"]


def test_the_session_origin_is_forced_to_https_in_production(
        web, db, bucket, monkeypatch):
    """TLS ends upstream of gunicorn: request.scheme reads « http », and a
    session whose origin says so would refuse the browser's PUTs."""
    _seed_contact(db)
    monkeypatch.setattr(parties_routes.Config, "ENV", "production")
    body = _open(web, "Permis.pdf", len(PDF)).get_json()
    assert bucket.sessions[body["url"]]["origin"] == "https://localhost"


@pytest.mark.parametrize("name, size, error", [
    ("contrat.docx", 100, pi.TYPE_REFUSED),
    ("scan.tiff", 100, pi.TYPE_REFUSED),
    ("Permis.pdf", 0, pi.SIZE_REFUSED),
    ("Permis.pdf", pi.MAX_SIZE + 1, pi.SIZE_REFUSED),
    # A size that is not an integer is refused, never coerced.
    ("Permis.pdf", "100", pi.SIZE_REFUSED),
    ("Permis.pdf", None, pi.SIZE_REFUSED),
    ("<b>Permis</b>.pdf", 100,
     document_model.TEXT_CHEVRONS_ERROR.format(label=pi.FILE_NAME_LABEL)),
    (None, 100, f"{pi.FILE_NAME_LABEL} : requis."),
])
def test_a_refused_file_is_a_422_before_any_session(
        web, db, bucket, name, size, error):
    _seed_contact(db)
    resp = _open(web, name, size)
    assert resp.status_code == 422
    assert resp.get_json() == {"erreur": error}
    assert bucket.sessions == {}


def test_an_unknown_or_full_contact_opens_no_session(web, db, bucket):
    resp = _open(web, "Permis.pdf", 100)
    assert (resp.status_code, resp.get_json()) == (
        422, {"erreur": pi.CONTACT_NOT_FOUND})
    _seed_contact(db)
    _seed_many(db, bucket, pi.MAX_PER_CONTACT)
    resp = _open(web, "Permis.pdf", 100)
    assert (resp.status_code, resp.get_json()) == (
        422, {"erreur": pi.CAP_REACHED})
    assert bucket.sessions == {}


def test_a_failed_contact_read_is_a_503_never_a_refusal(
        web, db, bucket, monkeypatch):
    _seed_contact(db)
    monkeypatch.setattr(db._fake_server, "batch_get_documents", _boom)
    resp = _open(web, "Permis.pdf", 100)
    assert (resp.status_code, resp.get_json()) == (
        503, {"erreur": concurrency.READ_UNAVAILABLE_ERROR})
    assert bucket.sessions == {}


@pytest.mark.parametrize("path, payload", [
    (SESSION, {"name": "Permis.pdf", "size": 100}),
    (FINALIZE, {"objet": f"staging/unknown/identite/{PROOF}/upload.pdf",
                "name": "upload.pdf"}),
])
def test_an_unusable_session_uid_is_a_503_and_touches_nothing(
        db, monkeypatch, path, payload):
    """``@login_required`` checks the uid is truthy only: « unknown »
    passes it. The uid comes from storage_identity, which refuses it."""
    monkeypatch.setattr(pi.storage, "bucket",
                        lambda: pytest.fail("touched the bucket"))
    _seed_contact(db)
    resp = _client(user_id="unknown").post(path, json=payload)
    assert resp.status_code == 503
    assert resp.get_json() == {"erreur": storage_identity.INVALID_UID_MESSAGE}


@pytest.mark.parametrize("objet", [
    f"staging/{UID}/{PROOF}/upload.pdf",              # the documents' shape
    f"staging/{UID}/mcp/{PROOF}/upload.pdf",          # the connector's
    f"staging/{UID}/exports/{PROOF}/lot.zip",         # a zip export
    f"staging/autre/identite/{PROOF}/upload.pdf",     # another uid
    f"staging/{UID}/identite/{PROOF}/upload.PDF",     # not this module's case
    f"users/{UID}/parties/{PID}/pieces_identite/{PROOF}.pdf",
    "",
])
def test_a_foreign_staging_name_is_a_400_and_never_touched(
        web, db, bucket, objet):
    _seed_contact(db)
    if objet:
        bucket.put(objet, PDF)
    resp = _finalize(web, objet, "upload.pdf")
    assert resp.status_code == 400
    assert resp.get_json() == {"erreur": pi.REQUEST_INVALID}
    if objet:
        assert objet in bucket.objects          # neither consumed nor copied
    assert db.peek_collection(f"parties/{PID}/pieces_identite") == {}


def test_the_documents_and_receipt_finalizers_refuse_the_identity_shape(
        db, bucket, monkeypatch):
    """The other way round: a proof's staging object can be neither filed
    as a dossier document nor attached as a receipt — refused before the
    bucket is touched (no reload, no copy, no consuming delete)."""
    objet = f"staging/{UID}/identite/{PROOF}/upload.pdf"
    bucket.put(objet, PDF)
    touched: list = []
    real_blob = bucket.blob
    monkeypatch.setattr(bucket, "blob",
                        lambda name: touched.append(name) or real_blob(name))
    monkeypatch.setattr(documents_routes, "get_dossier",
                        lambda d: {"id": d, "file_number": "2026-001"})
    monkeypatch.setattr(admin_routes.al, "get_transaction",
                        lambda t: {"id": t})
    for blueprint, path, payload in (
        (documents_routes.documents_bp, "/documents/api/finaliser",
         {"objet": objet, "name": "upload.pdf", "dossier_id": "d1"}),
        (admin_routes.admin_bp, "/administration/t1/api/recu",
         {"objet": objet, "name": "upload.pdf"}),
    ):
        resp = _bare_client(blueprint).post(path, json=payload)
        assert resp.status_code == 400, path
        assert resp.get_json() == {"erreur": "Requête invalide."}, path
    assert touched == []
    assert objet in bucket.objects


def _lose_next_proof_commit(monkeypatch, db):
    """The next commit that writes under a ``pieces_identite`` record
    RAISES without being applied — its answer is lost."""
    server = db._fake_server
    real_commit = server.commit
    state = {"left": 1}

    def _commit(request, metadata=None, **kwargs):
        writes = request.get("writes") or []
        if state["left"] and any(
                f"/{pi.SUBCOLLECTION}/" in server.rel(
                    server._write_name(getattr(w, "_pb", w)))
                for w in writes):
            state["left"] -= 1
            raise gexc.ServiceUnavailable("commit answer lost")
        return real_commit(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "commit", _commit)


def test_an_unknown_outcome_answers_incertain_and_the_re_post_files_it(
        web, db, bucket, monkeypatch, caplog):
    """The commit raised and the read-back that would settle it failed
    too: the model KEPT both objects. The endpoint says so — a 503 with
    ``incertain`` and Retry-After — and the page's one re-post files the
    proof (the reserved id and the create-only copy make it idempotent)."""
    _seed_contact(db)
    caplog.set_level(logging.INFO)
    body = _open(web, "Permis.pdf", len(PDF)).get_json()
    bucket.complete_session(body["url"], PDF)
    proof_id = body["objet"].split("/")[3]
    _lose_next_proof_commit(monkeypatch, db)
    real_settle = pi.concurrency.settle_failed_create
    monkeypatch.setattr(pi.concurrency, "settle_failed_create",
                        lambda ref: (pi.concurrency.WRITE_UNKNOWN, None))

    uncertain = _finalize(web, body["objet"], "Permis.pdf")

    assert uncertain.status_code == 503
    assert uncertain.get_json() == {"erreur": pi.FILING_UNCERTAIN,
                                    "incertain": True}
    assert uncertain.headers["Retry-After"] == "2"
    assert body["objet"] in bucket.objects and _final(proof_id) in bucket.objects
    assert _events(caplog) == []
    monkeypatch.setattr(pi.concurrency, "settle_failed_create", real_settle)

    again = _finalize(web, body["objet"], "Permis.pdf")

    assert again.status_code == 200
    assert again.get_json()["proof_id"] == proof_id
    assert db.peek(f"parties/{PID}/pieces_identite/{proof_id}") is not None
    assert body["objet"] not in bucket.objects
    assert [e for e, _ in _events(caplog)] == ["id_proof_filed"]


def test_a_re_post_in_the_same_outage_answers_read_unavailable_not_incertain(
        web, db, bucket, monkeypatch, caplog):
    """Why the page keeps the FIRST answer. The record's commit answer is
    lost and, from that moment, every Firestore read fails: the read-back
    cannot settle it — ``incertain``, both objects kept. The page's one
    re-post, ~2 s later, meets the same outage and answers like any
    filing whose first read failed: a 503 WITHOUT ``incertain``, « … Rien
    n'a été enregistré … », though the first commit's outcome is still
    unknown. So the page shows the first answer unless the re-post files
    the proof (the next test), and the sentence it shows for an answer
    lost on the way is that same one, handed by the server
    (``CONFIG.incertain``)."""
    _seed_contact(db)
    caplog.set_level(logging.INFO)
    body = _open(web, "Permis.pdf", len(PDF)).get_json()
    bucket.complete_session(body["url"], PDF)
    server = db._fake_server
    healthy = {"batch_get_documents": server.batch_get_documents,
               "run_query": server.run_query}
    _lose_next_proof_commit(monkeypatch, db)
    lose = server.commit

    def _commit_then_outage(request, metadata=None, **kwargs):
        try:
            return lose(request, metadata=metadata, **kwargs)
        except gexc.ServiceUnavailable:
            for name in healthy:            # from now on, every read fails
                monkeypatch.setattr(server, name, _boom)
            raise

    monkeypatch.setattr(server, "commit", _commit_then_outage)

    first = _finalize(web, body["objet"], "Permis.pdf")
    again = _finalize(web, body["objet"], "Permis.pdf")    # the re-post

    assert (first.status_code, first.get_json()) == (
        503, {"erreur": pi.FILING_UNCERTAIN, "incertain": True})
    assert first.headers["Retry-After"] == "2"
    assert (again.status_code, again.get_json()) == (
        503, {"erreur": concurrency.READ_UNAVAILABLE_ERROR})
    assert "Retry-After" not in again.headers
    assert "Rien n'a été enregistré" in concurrency.READ_UNAVAILABLE_ERROR
    assert _events(caplog) == []
    # The outage over, the fiche hands the page that same sentence.
    for name, method in healthy.items():
        monkeypatch.setattr(server, name, method)
    assert _config(_fiche(web))["incertain"] == pi.FILING_UNCERTAIN


def test_the_page_re_posts_an_uncertain_filing_once_after_two_seconds():
    """Source pin of the script's half of that contract: ``incertain``
    gets ONE more finalization, ~2 s later — never a loop —, and only an
    ``ok`` re-post replaces the first answer. Any other keeps it: the
    file is then « incertain » (amber), labelled with the first answer's
    sentence or the page's own (``CONFIG.incertain``), never « rejeté »
    with the re-post's « Rien n'a été enregistré » (the test above)."""
    source = _GESTION.read_text(encoding="utf-8")
    traiter = _method(source, "traiter")
    assert traiter.count("this.finaliser(f, donnees.objet)") == 2
    branch = _block(traiter, "if (resultat.incertain) {")
    assert "setTimeout(r, 2000)" in branch
    assert "resultat = await this.finaliser(f, donnees.objet);" in branch
    assert "while" not in traiter and "for (" not in traiter
    # The first answer, kept before the re-post…
    repost = branch.index("resultat = await this.finaliser(f, donnees.objet);")
    assert branch.index("const premier = resultat;") < repost
    # …is what a re-post that did not file the proof leaves on screen.
    assert branch.index("if (!resultat.ok) {") > repost
    kept = _block(branch, "if (!resultat.ok) {")
    assert ("f.statut = 'incertain'; "
            "f.motif = premier.erreur || CONFIG.incertain;") in kept
    assert "return;" in kept
    assert "resultat.erreur" not in kept and "rejeté" not in kept
    # Its label is that sentence alone, in amber.
    etiquette = _method(source, "etiquette")
    assert "if (f.statut === 'incertain') { return f.motif; }" in etiquette
    # …before the generic « statut · taille »: after it, the row would read
    # « incertain · 249 o » and the sentence never show.
    incertain = etiquette.index(
        "if (f.statut === 'incertain') { return f.motif; }")
    assert incertain < etiquette.index("return f.statut + ' · '")
    assert ("(f.statut === 'incertain' ? 'text-amber-700' : 'text-gray-500')"
            in source)


def test_a_lost_finalization_answer_is_uncertain_never_a_refusal():
    """A rejected fetch on the finalization means its ANSWER was lost: the
    proof may be filed. finaliser answers ``incertain`` — with no
    sentence, so a re-post that does not settle it shows the page's own,
    ``CONFIG.incertain`` — and the one re-post settles it when the store
    answers (``rejoue`` once the first call consumed the staging object:
    test_a_replayed_finalization_answers_ok_and_logs_no_second_filing).
    Never « erreur réseau — réessayez »: following it would file a second
    copy of the same document.

    So is a 5xx that is not the endpoint's JSON — a gateway's 502, 504 or
    524 page, the HTML 500 of an exception raised after the commit: its
    outcome is unknown too, and the same re-post settles it. The
    endpoint's own 5xx carry their sentence (« indisponible »,
    « incertain ») and keep it. So is a 2xx whose body could not be READ —
    cut after its headers, the answer lost: it may have filed the proof. A
    4xx, or a 200 whose body is not JSON at all — a refused token (an HTML
    400), an expired session (the login page) — filed nothing: it stays
    « refusé — rechargez la page »."""
    source = _GESTION.read_text(encoding="utf-8")
    finaliser = _method(source, "finaliser")
    assert finaliser.count("fetch(") == 1
    guarded = _block(finaliser, "try {")
    assert "fin = await fetch(CONFIG.finaliser, {" in guarded
    caught = _block(finaliser, "} catch (e) {")
    assert "return { ok: false, incertain: true, erreur: '' };" in caught
    # The foreign 5xx, judged once the body is parsed and before the answer
    # is read as the endpoint's: on the status — never !fin.ok, which would
    # count the 400 and the login page — and on the missing sentence.
    parsed = finaliser.index("const resultat = await fin.json()")
    foreign = _block(finaliser,
                     "if (fin.status >= 500 && !resultat.erreur) {")
    assert "return { ok: false, incertain: true, erreur: '' };" in foreign
    assert parsed < finaliser.index(foreign) < finaliser.index(
        "ok: fin.ok && resultat.ok === true,")
    assert "incertain: resultat.incertain === true," in finaliser
    # A 2xx whose body could not be READ (cut after its headers) may have
    # filed the proof: incertain too. A body that is not JSON — the login
    # page, a refused token — is a SyntaxError: it stays a refusal.
    assert "illisible = !(e instanceof SyntaxError);" in finaliser
    lost_body = _block(finaliser, "if (fin.ok && illisible) {")
    assert "return { ok: false, incertain: true, erreur: '' };" in lost_body
    assert parsed < finaliser.index(lost_body) < finaliser.index(
        "ok: fin.ok && resultat.ok === true,")
    # Only these three paths answer « incertain » of their own — never a
    # 4xx, nor a 200 that is not the JSON.
    assert finaliser.count("incertain: true") == 3


def test_every_failure_code_of_the_json_endpoints_has_its_status():
    """One HTTP status per non-success code of SESSION_CODES and
    FILING_CODES — derived, so a new code cannot fall to a 500."""
    success = {pi.CODE_OK, pi.CODE_VERSE, pi.CODE_REJOUE}
    codes = (set(pi.SESSION_CODES) | set(pi.FILING_CODES)) - success
    statuses = parties_routes._ID_PROOF_JSON_STATUS
    assert set(statuses) == codes
    assert statuses[pi.CODE_FORME] == 400
    assert statuses[pi.CODE_REFUS] == 422
    assert statuses[pi.CODE_INDISPONIBLE] == 503


@pytest.mark.parametrize("code", sorted(
    set(pi.FILING_CODES) - {pi.CODE_VERSE, pi.CODE_REJOUE}))
def test_each_failure_code_answers_its_status_and_the_model_s_sentence(
        web, db, monkeypatch, caplog, code):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(pi, "file_staged_proof",
                        lambda *a, **k: pi.Outcome(None, ["Phrase."], code))
    resp = _finalize(web, f"staging/{UID}/identite/{PROOF}/upload.pdf",
                     "upload.pdf")
    assert resp.status_code == parties_routes._ID_PROOF_JSON_STATUS[code]
    assert resp.get_json()["erreur"] == "Phrase."
    assert _events(caplog) == []


# ══════════════════════════════════════════════════════════════════════
# 2. « Consulter »
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("display, ext, disposition", [
    ("Permis de conduire", ".pdf",
     'inline; filename="Permis de conduire.pdf"'),
    ("Photo du passeport", ".jpg",
     'inline; filename="Photo du passeport.jpg"'),
])
def test_consult_redirects_to_a_15_minute_inline_signed_url(
        web, db, bucket, signed, display, ext, disposition):
    _seed_contact(db)
    _seed_proof(db, bucket, ext=ext, display=display,
                data=PDF if ext == ".pdf" else JPEG)

    resp = web.get(_consult())

    assert resp.status_code == 302
    assert resp.headers["Location"] == SIGNED_URL
    assert signed == [{
        "name": _final(ext=ext),
        "params": {"response-content-disposition": disposition,
                   "response-content-type": pi.MIME_TYPES[ext]},
        "minutes": 15,
    }]


def _consult_case(case, db, bucket, monkeypatch):
    if case == "read":
        _seed_proof(db, bucket)
        monkeypatch.setattr(pi, "get_proof_strict", _boom)
    elif case == "path":
        elsewhere = f"users/{UID}/dossiers/d1/{PROOF}.pdf"
        bucket.put(elsewhere, PDF)
        _seed_proof(db, bucket, data=None, path=elsewhere)
    elif case == "missing":
        _seed_proof(db, bucket, data=None)
    elif case == "signing":
        _seed_proof(db, bucket)

        def _refused(*_a, **_kw):
            raise RuntimeError(f"signBlob denied for {_final()}")

        monkeypatch.setattr(pi, "sign_blob_url", _refused)


@pytest.mark.parametrize("case, sentence, message, error_type", [
    ("read", concurrency.READ_UNAVAILABLE_ERROR, "id proof: read failed",
     "ServiceUnavailable"),
    ("absent", pi.PROOF_NOT_FOUND, None, None),
    ("path", pi.PATH_REFUSED, "id proof: stored path refused", None),
    ("missing", pi.OBJECT_MISSING, "id proof: object missing", None),
    ("signing", pi.CONSULT_FAILED, "id proof: signing failed",
     "RuntimeError"),
], ids=["read", "absent", "path", "missing", "signing"])
def test_each_consult_failure_lands_on_the_fiche_with_its_own_sentence(
        web, db, bucket, signed, monkeypatch, caplog,
        case, sentence, message, error_type):
    """Logged, never swallowed (``document.get_signed_url`` swallows its
    failures — this route never does), ids only, no traceback; and the
    lawyer reads WHY on the fiche, the dialog open."""
    _seed_contact(db)
    caplog.set_level(logging.INFO)
    _consult_case(case, db, bucket, monkeypatch)

    resp = web.get(_consult())

    assert _back_to_proofs(resp) == {"erreur": sentence}
    logged = _unexpected(caplog)
    if message is None:
        assert logged == []
    else:
        expected = {"partie_id": PID, "proof_id": PROOF}
        if error_type:
            expected["error_type"] = error_type
        # No traceback: a Storage error's text names the object.
        assert [(m, f, bool(e)) for m, f, e in logged] == [
            (message, expected, False)]
    if case != "signing":
        assert signed == []
    page = web.get(resp.headers["Location"]).get_data(as_text=True)
    assert _config(page)["ouvert"] is True
    assert sentence.replace("'", "&#39;") in _dialog(page)


# ══════════════════════════════════════════════════════════════════════
# 3. Rename and delete — redirects, ONE trail row, ONE event
# ══════════════════════════════════════════════════════════════════════


ETAG = f"e-{PROOF[:8]}"


def test_a_rename_lands_on_the_dialog_with_its_message_and_one_event(
        web, db, bucket, caplog):
    _seed_contact(db)
    _seed_proof(db, bucket)
    caplog.set_level(logging.INFO)

    resp = web.post(_rename(), data={"expected_etag": ETAG,
                                     "display_name": "  Passeport canadien  "})

    assert _back_to_proofs(resp) == {
        "message": parties_routes.ID_PROOF_RENAMED}
    stored = db.peek(f"parties/{PID}/pieces_identite/{PROOF}")
    assert stored["display_name"] == "Passeport canadien"      # trimmed
    assert stored["etag"] != ETAG
    assert _events(caplog) == [("id_proof_renamed",
                                {"partie_id": PID, "proof_id": PROOF})]
    page = web.get(resp.headers["Location"]).get_data(as_text=True)
    assert parties_routes.ID_PROOF_RENAMED in _dialog(page)

    # The same name again: nothing written, no banner, no event.
    same = web.post(_rename(), data={"expected_etag": stored["etag"],
                                     "display_name": "Passeport canadien"})
    assert _back_to_proofs(same) == {}
    assert db.peek(f"parties/{PID}/pieces_identite/{PROOF}") == stored
    assert len(_events(caplog)) == 1


@pytest.mark.parametrize("form, error", [
    ({"expected_etag": "e-autre", "display_name": "Passeport"},
     pi.PROOF_CHANGED),
    ({"display_name": "Passeport"}, pi.VERSION_REQUIRED),
    ({"expected_etag": ETAG, "display_name": "<b>Passeport</b>"},
     document_model.TEXT_CHEVRONS_ERROR.format(label=pi.DISPLAY_NAME_LABEL)),
    ({"expected_etag": ETAG, "display_name": "   "},
     f"{pi.DISPLAY_NAME_LABEL} : requis."),
])
def test_a_refused_rename_says_why_and_writes_nothing(
        web, db, bucket, caplog, form, error):
    _seed_contact(db)
    before = _seed_proof(db, bucket)
    caplog.set_level(logging.INFO)
    resp = web.post(_rename(), data=form)
    assert _back_to_proofs(resp) == {"erreur": error}
    assert db.peek(f"parties/{PID}/pieces_identite/{PROOF}") == before
    assert _events(caplog) == []


def test_renaming_a_missing_proof_says_so(web, db, bucket):
    _seed_contact(db)
    resp = web.post(_rename(PROOF2), data={"expected_etag": ETAG,
                                           "display_name": "Passeport"})
    assert _back_to_proofs(resp) == {"erreur": pi.PROOF_NOT_FOUND}


def test_a_delete_erases_then_journals_one_neutral_row_and_one_event(
        web, db, bucket, caplog):
    _seed_contact(db)
    _seed_proof(db, bucket, display="Passeport de Jean Tremblay")
    caplog.set_level(logging.INFO)

    resp = web.post(_delete(), data={"expected_etag": ETAG})

    assert _back_to_proofs(resp) == {"message": parties_routes.ID_PROOF_DELETED}
    assert db.peek(f"parties/{PID}/pieces_identite/{PROOF}") is None
    assert _final() not in bucket.objects
    (row,) = _audit_rows(db)
    # D6: a NEUTRAL title — the display name, typed by hand and possibly
    # carrying an ID number, never outlives the erased file.
    assert (row["entity_type"], row["entity_id"], row["dossier_id"],
            row["snapshot_min"]) == (
        "piece_identite", PID, "",
        {"title": "Preuve d'identité", "status": "application/pdf"})
    assert "Tremblay" not in json.dumps(row, default=str)
    assert _events(caplog) == [("id_proof_deleted",
                                {"partie_id": PID, "proof_id": PROOF})]

    # A second submission finds nothing: no second row, no second event.
    again = web.post(_delete(), data={"expected_etag": ETAG})
    assert _back_to_proofs(again) == {"erreur": pi.DELETE_NOT_FOUND}
    assert len(_audit_rows(db)) == 1 and len(_events(caplog)) == 1


def test_a_double_submission_racing_through_the_route_journals_once(
        web, db, bucket, caplog, monkeypatch):
    """Two « Supprimer définitivement » in flight together (a double tap, a
    second tab): the second runs to completion while the first is erasing.
    Both pass the etag check; ONE removes the record (``supprime``: the row
    and the event), the other finds it gone (``deja``: nothing journaled —
    D6 says ONE row)."""
    _seed_contact(db)
    _seed_proof(db, bucket)
    caplog.set_level(logging.INFO)
    real_delete = FakeBlob.delete
    nested: list = []

    def _delete_racing(self, client=None, if_generation_match=None, **kw):
        if self.name == _final() and not nested:
            nested.append(None)
            nested[0] = web.post(_delete(), data={"expected_etag": ETAG})
        return real_delete(self, client=client,
                           if_generation_match=if_generation_match, **kw)

    monkeypatch.setattr(FakeBlob, "delete", _delete_racing)

    first = web.post(_delete(), data={"expected_etag": ETAG})

    second = nested[0]
    assert _back_to_proofs(second) == {
        "message": parties_routes.ID_PROOF_DELETED}
    assert _back_to_proofs(first) == {"message": pi.ALREADY_DELETED}
    assert len(_audit_rows(db)) == 1
    assert [e for e, _ in _events(caplog)] == ["id_proof_deleted"]
    assert db.peek(f"parties/{PID}/pieces_identite/{PROOF}") is None
    assert _final() not in bucket.objects


@pytest.mark.parametrize("code", sorted(
    set(pi.DELETE_CODES) - {pi.CODE_SUPPRIME, pi.CODE_DEJA}))
def test_a_delete_that_did_not_remove_the_record_journals_nothing(
        web, db, monkeypatch, caplog, code):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(pi, "delete_proof",
                        lambda *a, **k: pi.Outcome(None, ["Phrase."], code))
    resp = web.post(_delete(), data={"expected_etag": ETAG})
    assert _back_to_proofs(resp) == {"erreur": "Phrase."}
    assert _audit_rows(db) == [] and _events(caplog) == []


def test_deja_is_said_without_alarm_and_journals_nothing(
        web, db, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(pi, "delete_proof", lambda *a, **k: pi.Outcome(
        None, [pi.ALREADY_DELETED], pi.CODE_DEJA))
    resp = web.post(_delete(), data={"expected_etag": ETAG})
    assert _back_to_proofs(resp) == {"message": pi.ALREADY_DELETED}
    assert _audit_rows(db) == [] and _events(caplog) == []


@pytest.mark.parametrize("code, banner", [
    (pi.CODE_MODIFIE, {"message": "Nom du document modifié."}),
    (pi.CODE_INCHANGE, {}),
] + [(code, {"erreur": "Phrase."}) for code in sorted(
    set(pi.RENAME_CODES) - {pi.CODE_MODIFIE, pi.CODE_INCHANGE})])
def test_every_rename_code_lands_on_its_banner(
        web, db, monkeypatch, caplog, code, banner):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(pi, "rename_proof", lambda *a, **k: pi.Outcome(
        {"id": PROOF}, ["Phrase."] if banner.get("erreur") else [], code))
    resp = web.post(_rename(), data={"expected_etag": ETAG,
                                     "display_name": "Passeport"})
    assert _back_to_proofs(resp) == banner
    assert len(_events(caplog)) == (1 if code == pi.CODE_MODIFIE else 0)


@pytest.mark.parametrize("url", [_rename(), _delete()])
def test_under_htmx_the_redirect_also_says_hx_redirect(web, db, bucket, url):
    """Rule 8: htmx swaps only a 2xx — never a 422; the answer is the
    redirect, and HX-Redirect for htmx (the kyc_confirm pattern).

    Since the refusals lot (2026-11-06), a 200 carrying HX-Redirect and NO
    Location (``routes/_helpers.redirect_after_post``): the browser's XHR
    follows a 302 by itself, so htmx never saw the header this test pinned
    on one. HX-Redirect names exactly where the plain form lands."""
    _seed_contact(db)
    _seed_proof(db, bucket)
    form = {"expected_etag": "e-autre", "display_name": "Passeport"}
    plain = web.post(url, data=form)
    assert plain.status_code == 302 and "HX-Redirect" not in plain.headers
    resp = web.post(url, data=form, headers={"HX-Request": "true"})
    assert resp.status_code == 200 and "Location" not in resp.headers
    assert resp.headers["HX-Redirect"] == plain.headers["Location"]


@pytest.mark.parametrize("url", [_rename(), _delete()])
def test_rename_and_delete_are_post_only(web, db, url):
    assert web.get(url).status_code == 405


@pytest.mark.parametrize("method, url", [
    ("post", SESSION), ("post", FINALIZE), ("get", _consult()),
    ("post", _rename()), ("post", _delete()),
])
def test_every_proof_route_requires_a_session(db, monkeypatch, method, url):
    for name in ("open_proof_upload", "file_staged_proof", "get_proof_strict",
                 "rename_proof", "delete_proof"):
        monkeypatch.setattr(pi, name, lambda *a, **k: pytest.fail("reached"))
    resp = getattr(_client(user_id=None), method)(url)
    assert resp.status_code == 302
    assert urlparse(resp.headers["Location"]).path == "/login"


# ══════════════════════════════════════════════════════════════════════
# 4. The fiche — the card and the « Gérer » dialog
# ══════════════════════════════════════════════════════════════════════


def test_the_card_lists_each_proof_with_its_icon_and_a_consult_link(
        web, db, bucket, signed):
    _seed_contact(db)
    _seed_proof(db, bucket, display="Permis de conduire")
    _seed_proof(db, bucket, PROOF2, ext=".jpg", data=JPEG,
                display="Photo du passeport",
                created_at=T0 + timedelta(hours=1))

    html = _fiche(web)

    card = _card(html)
    assert card.index("Permis de conduire") < card.index("Photo du passeport")
    # _file_icon reads `kind`: without it every row would show « draft ».
    assert ">picture_as_pdf</span>" in card and ">image</span>" in card
    assert ">draft</span>" not in card
    for proof_id, name in ((PROOF, "Permis de conduire"),
                           (PROOF2, "Photo du passeport")):
        link = re.search(rf'<a href="{_consult(proof_id)}"[^>]*>', card)
        assert link, proof_id
        assert 'target="_blank"' in link.group(0)
        assert 'rel="noopener"' in link.group(0)
        assert f'aria-label="Consulter « {name} »"' in link.group(0)
    assert "Aucune preuve" not in card
    # No signed URL rides in the page: it is minted on the click only.
    assert "googleapis" not in html and "X-Goog" not in html
    assert signed == []


def test_the_dialog_rows_carry_their_own_etag_and_the_two_forms(
        web, db, bucket):
    _seed_contact(db)
    _seed_proof(db, bucket)
    dialog = _dialog(_fiche(web, "?gerer=1"))
    for action in (_rename(), _delete()):
        form = re.search(rf'action="{action}"[^>]*>(.*?)</form>', dialog, re.S)
        assert form, action
        assert 'name="csrf_token" value="tok"' in form.group(1)
        assert f'name="expected_etag" value="{ETAG}"' in form.group(1)
    assert "Supprimer « Permis de conduire » ? Le fichier sera effacé " \
           "définitivement." in dialog
    assert "Supprimer définitivement" in dialog
    assert f'maxlength="{pi.DISPLAY_NAME_MAX}"' in dialog
    # The size and the filing DAY in Montréal (Rule 5: an instant through
    # to_mtl) — 15 h UTC on 2 November is 10 h in Montréal.
    size = parties_routes.format_file_size(len(PDF))
    assert f"{size} · versée le 2 novembre 2026" in dialog


def test_an_empty_list_says_aucune_in_the_card_and_the_dialog(web, db, bucket):
    _seed_contact(db)
    html = _fiche(web, "?gerer=1")
    assert "Aucune preuve d'identité versée." in _card(html)
    assert "Aucune preuve d'identité versée." in _dialog(html)
    assert "Liste indisponible" not in html


def test_a_failed_read_says_indisponible_never_aucune_in_both_places(
        web, db, bucket, monkeypatch):
    _seed_contact(db)
    _seed_proof(db, bucket)
    monkeypatch.setattr(pi, "list_proofs_strict", _boom)
    html = _fiche(web, "?gerer=1")
    for block in (_card(html), _dialog(html)):
        assert "Liste indisponible pour le moment — rechargez la page." in block
        assert "Aucune preuve" not in block
        assert "Permis de conduire" not in block
    # « Gérer » stays: the uploads read the store themselves, strictly.
    assert 'id="preuves-identite-gerer"' in _card(html)
    assert 'x-ref="csrf"' in _dialog(html)


def test_a_failed_read_on_a_non_client_claims_nothing(
        web, db, bucket, monkeypatch):
    _seed_contact(db, contact_role="autre")
    monkeypatch.setattr(pi, "list_proofs_strict", _boom)
    html = _fiche(web)
    assert ">Conformité</h2>" not in html
    assert "preuves-identite" not in html


def test_a_list_over_the_cap_says_it_is_truncated_in_both_places(
        web, db, bucket):
    _seed_contact(db)
    _seed_many(db, bucket, pi.MAX_PER_CONTACT + 1)
    html = _fiche(web, "?gerer=1")
    card, dialog = _card(html), _dialog(html)
    assert card.count("pieces-identite/") == pi.MAX_PER_CONTACT
    assert dialog.count("/renommer") == pi.MAX_PER_CONTACT
    for block in (card, dialog):
        assert pi.TRUNCATED_NOTICE in block
    assert "Pièce 20" not in card


def test_a_non_client_holding_a_proof_still_sees_it(web, db, bucket):
    """A role change never strands confidential files out of reach: the
    card shows, its checks' grid does not."""
    _seed_contact(db, contact_role="autre")
    _seed_proof(db, bucket)
    html = _fiche(web)
    assert ">Conformité</h2>" in html
    assert "Permis de conduire" in _card(html)
    assert "Conflit d" not in html            # the grid of the two checks
    assert 'x-data="preuvesIdentite()"' in html
    # No separator above the block when it is the card's only content.
    assert '<section id="preuves-identite">' in html


def test_a_non_client_without_proofs_sees_neither_block_nor_dialog(
        web, db, bucket):
    _seed_contact(db, contact_role="autre")
    html = _fiche(web)
    assert ">Conformité</h2>" not in html
    assert "preuves-identite" not in html and "preuvesIdentite" not in html


def test_a_client_sees_the_checks_and_the_proofs(web, db, bucket):
    _seed_contact(db)
    html = _fiche(web)
    assert "Conflit d" in html
    assert ('<section id="preuves-identite" class="mt-4 pt-4 border-t '
            'border-gray-100">') in html
    assert "Ex. : permis de conduire, passeport." in _dialog(html)


def test_a_dossier_client_of_another_role_sees_both(web, db, bucket):
    _seed_contact(db, contact_role="autre")
    db.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001",
                            "title": "T", "status": "actif",
                            "clients": [{"id": PID, "name": "Jean Tremblay"}],
                            "client_ids": [PID]})
    html = _fiche(web)
    assert "Conflit d" in html and 'id="preuves-identite"' in html


def test_an_organization_client_is_offered_its_own_example(web, db, bucket):
    """An REQ extract is an organization's proof (art. 20)."""
    _seed_contact(db, type="organization",
                  organization_name="Constructions Beaubien inc.",
                  first_name="", last_name="")
    dialog = _dialog(_fiche(web))
    assert ("Ex. : l'état des renseignements du Registraire des "
            "entreprises.") in dialog
    assert "permis de conduire" not in dialog


@pytest.mark.parametrize("gerer, ouvert", [
    ("", False), ("?gerer=1", True), ("?gerer=oui", False),
    ("?gerer=true", False), ("?gerer=0", False),
])
def test_only_gerer_1_opens_the_dialog(web, db, bucket, gerer, ouvert):
    _seed_contact(db)
    config = _config(_fiche(web, gerer))
    assert config == {
        "ouvert": ouvert,
        "televersement": SESSION,
        "finaliser": FINALIZE,
        "suivant": f"/parties/{PID}?gerer=1#preuves-identite",
        "fiche": f"/parties/{PID}#preuves-identite",
        # One source: the model's sentence for an unknown filing outcome.
        "incertain": pi.FILING_UNCERTAIN,
    }


def test_the_banners_repeat_inside_the_open_dialog_only(web, db, bucket):
    _seed_contact(db)
    opened = _dialog(_fiche(web, "?gerer=1&message=Fait.&erreur=Refus."))
    assert "Fait." in opened and "Refus." in opened
    closed = _dialog(_fiche(web, "?message=Fait.&erreur=Refus."))
    assert "Fait." not in closed and "Refus." not in closed


def test_the_upload_zone_carries_the_csrf_value_the_script_sends(
        web, db, bucket):
    """The test apps run without CSRFProtect: an EMPTY token would pass
    every test here and refuse every upload in production. The input
    carries the value, and the script reads that very input."""
    _seed_contact(db)
    html = _fiche(web)
    zone = _upload_zone(html)
    assert '<input type="hidden" name="csrf_token" value="tok" x-ref="csrf">' in zone
    assert "'X-CSRFToken': this.$refs.csrf.value" in _script(html)


def test_the_file_input_accepts_the_whitelist_and_has_no_capture(
        web, db, bucket):
    _seed_contact(db)
    html = _fiche(web)
    (file_input,) = re.findall(r'<input type="file"[^>]*>', _dialog(html))
    assert f'accept="{pi.ACCEPT}"' in file_input
    assert " multiple" in file_input
    assert "capture" not in file_input
    # The hint derives from the model's constants (one source per list).
    assert pi.UPLOAD_HINT in _dialog(html)
    assert pi.UPLOAD_HINT == (f"{pi.FORMATS_LABEL} — {pi.MAX_SIZE_LABEL} "
                              "au plus par fichier.")


def test_the_dialog_is_an_accessible_modal(web, db, bucket):
    _seed_contact(db)
    _seed_proof(db, bucket)
    html = _fiche(web)
    dialog, script = _dialog(html), _script(html)
    opening = dialog[:dialog.index(">")]
    for attribute in ('role="dialog"', 'aria-modal="true"',
                      'aria-labelledby="preuves-identite-titre"'):
        assert attribute in opening, attribute
    assert 'id="preuves-identite-titre"' in dialog
    # The queue's outcomes are announced — a status, never each
    # percentage: the label leaves the live region while the bytes go up,
    # and « en cours » is said once instead.
    assert '<ul aria-live="polite"' in dialog
    status = re.search(r'<p class="text-xs"\s[^>]*x-text="etiquette\(f\)"',
                       dialog)
    assert status, "status label"
    assert ':aria-hidden="f.statut === \'en cours\'"' in status.group(0)
    assert ('<span class="sr-only" x-show="f.statut === \'en cours\'">'
            'en cours</span>') in dialog
    # The focus moves in on open (« Fermer ») and back to « Gérer ».
    gerer = re.search(r'<button type="button"\s+id="preuves-identite-gerer"'
                      r'[^>]*>', _card(html))
    assert gerer and 'aria-haspopup="dialog"' in gerer.group(0)
    assert 'x-ref="fermer"' in dialog
    assert "this.$refs.fermer.focus()" in script
    assert "document.getElementById('preuves-identite-gerer')" in script
    # …and stays inside the panel: Tab wraps from its last control to the
    # first, Shift+Tab from the first to the last, and a focus that left
    # the panel comes back in (the listener is the window's). Hand-written:
    # x-trap would be a new vendored asset.
    assert '@keydown.tab.window="garderFocus($event)"' in opening
    assert re.search(r'<div x-ref="panneau"\s+@click\.outside="fermer\(\)"',
                     dialog)
    garder = _method(script, "garderFocus")
    # It wraps between the PANEL's first and last controls: never the
    # page's, never a control in between.
    assert "const panneau = this.$refs.panneau;" in garder
    assert "Array.from(panneau.querySelectorAll(" in garder
    assert "const premier = cibles[0];" in garder
    assert "const dernier = cibles[cibles.length - 1];" in garder
    assert garder.index("if (!this.open) { return; }") < garder.index(
        "querySelectorAll")
    assert ("'button, [href], input, select, textarea, "
            "[tabindex]:not([tabindex=\"-1\"])'") in garder
    assert ("!el.disabled && el.type !== 'hidden' "
            "&& el.getClientRects().length > 0") in garder
    assert "const dedans = panneau.contains(actif);" in garder
    backwards = _block(garder, "if (event.shiftKey) {")
    assert ("if (!dedans || actif === premier) "
            "{ event.preventDefault(); dernier.focus(); }") in backwards
    forwards = _block(garder, "} else if (!dedans || actif === dernier) {")
    assert "event.preventDefault(); premier.focus();" in forwards
    # Icon-only buttons carry a label and a title (Rule 10).
    for label in ("Fermer", "Renommer « Permis de conduire »",
                  "Supprimer « Permis de conduire »"):
        button = re.search(rf'<button[^>]*aria-label="{label}"[^>]*>', dialog)
        assert button and 'title="' in button.group(0), label
    # The step buttons measure 44 px (py-3), never the 40 px py-2.5.
    for text in ("Annuler", "Enregistrer", "Supprimer définitivement"):
        buttons = re.findall(rf'<button[^>]*>\s*{text}\s*</button>', dialog)
        assert buttons, text
        for button in buttons:
            assert "px-4 py-3" in button, text


def test_the_file_input_s_keyboard_focus_shows_on_its_label(web, db, bucket):
    """The file input is sr-only and the focus trap lands on it from both
    directions: its focus shows on the label, whose dashed border turns
    indigo while the input holds it — a bound class, both colours already
    compiled (a focus-within ring would need a stylesheet rebuild, Change
    Impact 6). The gray left the static list: kept there beside the bound
    indigo, the stylesheet's order would decide which one shows."""
    _seed_contact(db)
    html = _fiche(web)
    zone = _upload_zone(html)
    (label,) = re.findall(r"<label\s[^>]*>", zone)
    static = re.search(r'\sclass="([^"]*)"', label).group(1).split()
    assert "border-gray-300" not in static
    assert {"border-2", "border-dashed", "rounded-xl",
            "cursor-pointer"} <= set(static)
    # The hover darkens only an unfocused label: under the pointer, a
    # static hover:border-gray-400 would hide the focus (compiled later).
    assert "hover:border-gray-400" not in static
    assert (":class=\"focusAjout ? 'border-indigo-600' : "
            "'border-gray-300 hover:border-gray-400'\"" in label)
    (file_input,) = re.findall(r'<input type="file"[^>]*>', zone)
    assert '@focus="focusAjout = true"' in file_input
    assert '@blur="focusAjout = false"' in file_input
    assert "focusAjout: false," in _script(html)
    assert _not_compiled({"border-indigo-600", "border-gray-300"}) == []


def test_closing_and_a_filed_batch_read_the_fiche_again_through_recharger():
    """``location.assign()`` to the page's own path and query, a fragment
    added, is a same-document navigation: no request — the list stays
    stale and, in fermer(), the dialog stays open. The fiche opens at a
    plain /parties/<id> (the contact list, a dossier, an invoice) and a
    filed batch leaves it at ?gerer=1#preuves-identite: both navigations
    go through recharger(), which reloads in that case."""
    source = _GESTION.read_text(encoding="utf-8")
    recharger = _method(source, "recharger")
    assert "const cible = new URL(url, window.location.href);" in recharger
    same = recharger[:recharger.index("} else {")]
    assert re.search(r"if \(cible\.pathname === window\.location\.pathname"
                     r"\s+&& cible\.search === window\.location\.search\) \{",
                     same)
    assert same.index("history.replaceState(null, '', cible.href);") < (
        same.index("window.location.reload();"))
    other = _block(recharger, "} else {")
    assert "window.location.assign(cible.href);" in other
    # Never assign() then reload(): the reload would cancel the navigation.
    assert "assign" not in same and "reload" not in other
    for name, target in (("fermer", "CONFIG.fiche"),
                         ("ajouter", "CONFIG.suivant")):
        method = _method(source, name)
        assert f"this.recharger({target});" in method, name
        assert "window.location.assign(" not in method, name
        assert "location.reload(" not in method, name
    # Closing reads the fiche again once a file is filed — or its filing
    # left unconfirmed: the list then says whether the proof is there.
    fermer = _method(source, "fermer")
    assert "f.statut === 'versé' || f.statut === 'incertain'" in fermer
    # A batch is judged on ITS files: an earlier batch's refusal no
    # longer holds a later, fully filed one back.
    ajouter = _method(source, "ajouter")
    assert "const lot = this.fichiers.slice(debut);" in ajouter
    assert "for (const f of lot) { await this.traiter(f); }" in ajouter
    filed = _block(ajouter, "if (lot.every(function (f) "
                            "{ return f.statut === 'versé'; })) {")
    assert "this.recharger(CONFIG.suivant);" in filed
    assert "this.fichiers.some(" not in ajouter
    # The hint promises what fermer() does: it shows only when a filed
    # file sits beside a refused or unconfirmed one (no reload yet).
    hint = re.search(r'<p x-show="([^"]*)"[^>]*>Les fichiers versés '
                     r'paraîtront dans la liste à la fermeture\.</p>', source)
    assert hint, "hint"
    assert hint.group(1) == (
        "fichiers.some(f => f.statut === 'versé') && fichiers.some(f => "
        "f.statut === 'rejeté' || f.statut === 'incertain')")


def test_no_inline_handler_and_the_script_carries_the_nonce(web, db, bucket):
    _seed_contact(db)
    _seed_proof(db, bucket)
    html = _fiche(web)
    for block in (_card(html), _dialog(html)):
        assert not re.search(r"\son[a-z]+=", block)
    assert '<script nonce="n">\n// « Gérer »' in html
    assert html.index('id="preuves-identite-config"') < html.index(
        "function preuvesIdentite()")


def test_the_contact_delete_dialog_warns_when_the_contact_holds_proofs(
        web, db, bucket):
    sentence = ("Ce contact détient des preuves d'identité : supprimez-les "
                "d'abord dans la section Conformité, sinon la suppression "
                "sera refusée.")
    _seed_contact(db)
    assert sentence not in _fiche(web)
    _seed_proof(db, bucket)
    assert sentence in _fiche(web)


# ══════════════════════════════════════════════════════════════════════
# 5. Pins — the JS twins, compiled classes, vendored icons, the logs
# ══════════════════════════════════════════════════════════════════════


_TWINS = ("putTranche", "interrogerDecalage", "televerser")


def _method(source: str, name: str) -> str:
    """One method of an object literal, from its name to its closing brace
    (the twins hold no brace in a string or a comment)."""
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
    """The block *opener* starts — ``if (…) {``, ``} catch (e) {``… —
    from the opener to the brace that closes ITS last ``{``, counted as
    :func:`_method` counts."""
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


def _twin_drift(original: str, twin: str) -> list[str]:
    return [name for name in _TWINS
            if _method(original, name) != _method(twin, name)]


def test_the_chunked_put_functions_are_verbatim_twins_of_the_upload_page():
    """The resumable PUT (Content-Range, the 308 resume, three retries with
    backoff) is duplicated rather than shared — a shared static file would
    be a new vendored asset and its fan-out. Duplicated, it must not
    drift: a fix on one page is a fix on both."""
    upload = _UPLOAD.read_text(encoding="utf-8")
    gestion = _GESTION.read_text(encoding="utf-8")
    assert _twin_drift(upload, gestion) == []
    for name in _TWINS:
        assert len(_method(upload, name).splitlines()) >= 12, name
    # Each names the other, so an edit of one finds the other.
    assert "templates/parties/_pieces_identite_gestion.html" in upload
    assert "templates/documents/upload.html" in gestion
    # The pin catches what it claims: one character of drift is seen.
    drifted = gestion.replace("Math.min(99,", "Math.min(98,")
    assert drifted != gestion
    assert _twin_drift(upload, drifted) == ["putTranche"]


def _escape_css(cls: str) -> str:
    b = "\\"
    for raw, esc in ((b, b * 2), (":", b + ":"), (".", b + "."),
                     ("/", b + "/"), ("[", b + "["), ("]", b + "]")):
        cls = cls.replace(raw, esc)
    return cls


def _not_compiled(classes) -> list[str]:
    css = next(_ATHENA.glob("static/vendor/app.*.css")).read_text(
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


def test_the_block_and_the_dialog_use_only_compiled_classes(web, db, bucket):
    """A class absent from the compiled stylesheet silently does not apply
    (Change Impact 6): every class of the card and the dialog — with rows,
    banners and the queue's bound classes — is in app.*.css."""
    _seed_contact(db)
    _seed_proof(db, bucket)
    _seed_proof(db, bucket, PROOF2, ext=".jpg", data=JPEG, display="Photo")
    html = _fiche(web, "?gerer=1&message=Fait.&erreur=Refus.")
    markup = _card(html) + _dialog(html)
    classes = _classes(markup)
    assert {"text-red-600", "text-green-700", "text-amber-700",
            "max-h-[80vh]", "bg-black/40", "w-8", "sr-only",
            "border-indigo-600"} <= classes                  # non-vacuous
    assert _not_compiled(classes) == []
    # The check catches what it claims.
    assert _not_compiled({"min-h-11", "shrink-0"}) == ["min-h-11", "shrink-0"]


def test_every_glyph_of_the_block_and_the_dialog_is_vendored(web, db, bucket):
    _seed_contact(db)
    _seed_proof(db, bucket)
    _seed_proof(db, bucket, PROOF2, ext=".png", data=PNG, display="Photo")
    html = _fiche(web, "?gerer=1")
    glyphs = set(re.findall(r'<span class="ms [^"]*" aria-hidden="true" '
                            r'translate="no">([a-z0-9_]+)</span>',
                            _card(html) + _dialog(html)))
    assert glyphs == {"edit", "delete", "close", "upload", "picture_as_pdf",
                      "image"}
    assert glyphs <= MATERIAL_ICONS
    for template in ("_pieces_identite.html", "_pieces_identite_gestion.html"):
        source = (_TEMPLATES / "parties" / template).read_text(encoding="utf-8")
        used = set(re.findall(r"""\bms\(\s*['"]([a-z0-9_]+)['"]""", source))
        assert used and used <= MATERIAL_ICONS, template


_NEVER_LOGGED = ("Tremblay", "Passeport", "Permis", "users/", "staging/",
                 "storage.example", "googleapis", "X-Goog")


def _log_leaks(records) -> list:
    leaks = []
    for record in records:
        texts = [record.getMessage()]
        texts += [str(v) for v in
                  (getattr(record, "json_fields", None) or {}).values()]
        if record.exc_info:
            texts.append(repr(record.exc_info[1]))
        for text in texts:
            leaks += [(record.name, record.getMessage(), word)
                      for word in _NEVER_LOGGED if word in text]
    return leaks


def test_no_name_file_name_path_or_url_reaches_a_log(
        web, db, bucket, signed, monkeypatch, caplog):
    """« Do not log PII »: a whole life — filed, replayed, consulted, a
    consult failing, renamed, a list read failing, deleted — logs ids,
    codes and types only."""
    caplog.set_level(logging.DEBUG)
    _seed_contact(db)
    name = "Passeport Jean Tremblay.pdf"
    body = _open(web, name, len(PDF)).get_json()
    bucket.complete_session(body["url"], PDF)
    proof_id = _finalize(web, body["objet"], name).get_json()["proof_id"]
    _finalize(web, body["objet"], name)                       # replay
    assert web.get(_consult(proof_id)).headers["Location"] == SIGNED_URL

    def _refused(*_a, **_kw):
        raise RuntimeError(f"signBlob denied for {body['objet']} ({name})")

    with monkeypatch.context() as m:
        m.setattr(pi, "sign_blob_url", _refused)
        web.get(_consult(proof_id))
    etag = db.peek(f"parties/{PID}/pieces_identite/{proof_id}")["etag"]
    web.post(_rename(proof_id), data={"expected_etag": etag,
                                      "display_name": "Permis Jean Tremblay"})
    with monkeypatch.context() as m:
        m.setattr(db._fake_server, "run_query", _boom)
        _fiche(web)
    _seed_proof(db, bucket, PROOF2, data=None, display="Permis Tremblay")
    web.get(_consult(PROOF2))                                 # object missing
    etag = db.peek(f"parties/{PID}/pieces_identite/{proof_id}")["etag"]
    web.post(_delete(proof_id), data={"expected_etag": etag})

    messages = {r.getMessage() for r in caplog.records}
    events = [e for e, _ in _events(caplog)]
    assert events == ["id_proof_filed", "id_proof_renamed", "id_proof_deleted"]
    assert {"id proof: signing failed", "id proof: object missing",
            "id proof list read failed: ServiceUnavailable"} <= messages
    assert _log_leaks(caplog.records) == []
    # The scan catches what it claims.
    planted = logging.makeLogRecord({"msg": "id proof: %s", "args": (name,)})
    assert _log_leaks([planted])
