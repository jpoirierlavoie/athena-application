"""Suppression d'un contact REFUSÉE : dite à l'écran, jamais un nom au journal.

Deux défauts voisins de la règle à rebours du lot 0b (B7), relevés en revue :

* le formulaire web « Supprimer » d'une fiche redirigeait vers la LISTE
  quelle que soit l'issue — un refus (contact lié à un dossier, mandataire
  d'un autre contact) passait pour une suppression réussie, sans un mot, et
  le contact était toujours là ;
* le DELETE CardDAV répondait 500 « Erreur serveur. » à un refus d'affaires,
  et journalisait le TEXTE du refus par ``logger.error`` — « … mandataire de
  Sophie Gagnon » : un nom, que le filtre de caviardage ne touche pas.

Tout passe par le VRAI client Firestore (``tests/_fake_firestore.py``), les
vraies routes et le document STOCKÉ relu.

Les preuves d'identité (lot 2, 2026-11) : un contact qui en détient refuse
la suppression — Firestore ne supprime pas une sous-collection avec son
parent, et ses preuves deviendraient des dossiers et des fichiers
confidentiels qu'aucun écran n'atteint. Le refus est statique et ne nomme
personne (bandeau sur le web, 409 en CardDAV) ; une lecture impossible
refuse (« réessayer », 503 + Retry-After) ; la suppression finale est UNE
transaction qui relit le contact et ses preuves — une preuve versée entre
la vérification et le commit fait refuser —, et un commit dont la réponse
s'est perdue se tranche en relisant : le contact disparu, les deux
appelants posent quand même la pierre tombale, la CTag et la trace.
"""

import logging
import os
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
from google.api_core import exceptions as gexc
from markupsafe import escape

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.carddav as carddav
    import dav.sync as dav_sync  # its db is patched below
    from models import partie as pm
    # delete_partie imports it LOCALLY: imported here, so the fake store's
    # sweep reaches its `db` (the `db` fixture pins it).
    from models import piece_identite as pi
    import routes.parties as parties_routes

from flask import Blueprint, Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.markdown_docx import markdown_to_safe_html  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it. Bound to `_`, the name
# that says « deliberately unused ».
_ = (dav_sync,)

UTC = timezone.utc
AUTH = {"Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTpwdw=="}


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    # A sweep that ran before models.piece_identite was imported would leave
    # the proofs check on the MOCKED client, which answers « no proof » to
    # every query: every refusal below would then pass for the wrong reason.
    assert pi.db is fake
    return fake


def _personne(pid: str, first: str, last: str, **over) -> dict:
    doc = pm._default_doc()
    doc.update({
        "id": pid, "type": "individual", "contact_role": "client",
        "first_name": first, "last_name": last,
        "etag": f"etag-{pid}", "vcard_uid": f"uid-{pid}",
    })
    doc.update(over)
    return doc


@pytest.fixture
def famille(db):
    """Sophie est représentée par sa tutrice Marie : Marie ne se supprime pas."""
    db.seed("parties/marie", _personne("marie", "Marie", "Gagnon"))
    db.seed("parties/sophie", _personne(
        "sophie", "Sophie", "Gagnon",
        mandataires=[{"id": "marie", "kind": "tuteur", "notes": ""}],
    ))
    return db


# ══════════════════════════════════════════════════════════════════════
# Le formulaire web
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def web(db):
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms,
                                 csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl, phone=format_phone_display,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=lambda v: v, markdown=markdown_to_safe_html,
    )
    app.register_blueprint(parties_routes.parties_bp)
    # The contact page links to three other blueprints; stubs carry the
    # endpoint names so the REAL template renders without their imports.
    for name, rules in (
        ("doc_templates", (("/gabarits/generer", "generate_modal"),)),
        ("dossiers", (("/dossiers/new", "dossier_new"),
                      ("/dossiers/<dossier_id>", "dossier_detail"))),
        ("reception", (("/reception/inviter", "inviter_form"),)),
    ):
        stub = Blueprint(name, __name__ + "." + name)
        for rule, endpoint in rules:
            stub.add_url_rule(rule, endpoint, lambda **kw: "")
        app.register_blueprint(stub)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["user_email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def test_a_refused_web_delete_says_so_on_the_contact_page(famille, web):
    """THE web defect: the refusal redirected to the LIST like a success,
    and the contact silently stayed. It must land back on the contact's
    own page, carrying the French reason, the contact intact."""
    resp = web.post("/parties/marie/delete")
    assert resp.status_code == 302
    target = urlparse(resp.headers["Location"])
    assert target.path.endswith("/parties/marie")
    erreur = parse_qs(target.query).get("erreur", [""])[0]
    assert "mandataire de Sophie Gagnon" in erreur
    assert famille.peek("parties/marie") is not None

    page = web.get(resp.headers["Location"])
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Impossible de supprimer" in body
    assert "mandataire de Sophie Gagnon" in body


def test_a_successful_web_delete_still_goes_to_the_list(db, web):
    db.seed("parties/seul", _personne("seul", "Paul", "Roy"))
    resp = web.post("/parties/seul/delete")
    assert resp.status_code == 302
    assert urlparse(resp.headers["Location"]).path.endswith("/parties/")
    assert db.peek("parties/seul") is None


def test_the_contact_page_renders_no_error_banner_by_default(famille, web):
    body = web.get("/parties/marie").get_data(as_text=True)
    assert "Impossible de supprimer" not in body


# ══════════════════════════════════════════════════════════════════════
# CardDAV
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def dav(db, monkeypatch):
    monkeypatch.setattr("dav.dav_auth._check_credentials", lambda u, p: True)
    monkeypatch.setattr("dav.dav_auth._check_success_cache", lambda u, p: True)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(carddav.carddav_bp)
    return app.test_client()


def test_a_carddav_delete_of_a_mandataire_is_a_french_409_with_no_name_logged(
    famille, dav, caplog
):
    """THE DAV defect: 500 « Erreur serveur. » for a business refusal, and
    the refusal TEXT — a contact's name — logged at ERROR."""
    with caplog.at_level(logging.DEBUG):
        resp = dav.delete("/dav/addressbook/marie.vcf", headers=AUTH)
    assert resp.status_code == 409
    body = resp.get_data(as_text=True)
    assert "mandataire de Sophie Gagnon" in body
    assert famille.peek("parties/marie") is not None
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "Sophie" not in logged and "Gagnon" not in logged
    assert "Marie" not in logged


def test_a_carddav_delete_whose_check_cannot_run_is_a_503(
    famille, dav, monkeypatch
):
    def _outage(pid):
        raise RuntimeError("lecture impossible")

    monkeypatch.setattr(pm, "list_mandataire_referrers", _outage)
    resp = dav.delete("/dav/addressbook/marie.vcf", headers=AUTH)
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After")
    assert famille.peek("parties/marie") is not None


def test_a_carddav_delete_that_fails_to_write_is_still_a_500(
    db, dav, monkeypatch
):
    db.seed("parties/seul", _personne("seul", "Paul", "Roy"))
    monkeypatch.setattr(
        pm, "delete_partie",
        lambda pid: (False, pm.PARTIE_DELETE_FAILED),
    )
    monkeypatch.setattr(carddav, "delete_partie", pm.delete_partie)
    resp = dav.delete("/dav/addressbook/seul.vcf", headers=AUTH)
    assert resp.status_code == 500


def test_a_carddav_delete_of_a_free_contact_still_succeeds(db, dav):
    db.seed("parties/seul", _personne("seul", "Paul", "Roy"))
    resp = dav.delete("/dav/addressbook/seul.vcf", headers=AUTH)
    assert resp.status_code == 204
    assert db.peek("parties/seul") is None
    assert db.peek("dav_sync/parties/tombstones/seul") is not None


# ══════════════════════════════════════════════════════════════════════
# Les preuves d'identité (lot 2) : refus, lecture impossible, course,
# commit à la réponse perdue
# ══════════════════════════════════════════════════════════════════════

PROOF = "3f2b8c1e-9d4a-4e6b-8f7a-1c2d3e4f5a6b"
PROOF_PATH = f"parties/seul/pieces_identite/{PROOF}"
T0 = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)


def _preuve(pid: str, proof_id: str = PROOF) -> dict:
    """A proof record as models/piece_identite stores it."""
    return {
        "id": proof_id, "partie_id": pid,
        "display_name": "Permis de conduire",
        "file_type": "application/pdf", "file_size": 2048,
        "storage_path": (
            f"users/u1/parties/{pid}/pieces_identite/{proof_id}.pdf"),
        "created_at": T0, "updated_at": T0, "etag": "pe1",
        "created_via": "web", "updated_via": "web",
    }


@pytest.fixture
def libre(db):
    """Paul Roy, a client linked to nothing and holding no proof."""
    db.seed("parties/seul", _personne("seul", "Paul", "Roy"))
    db.seed("dav_sync/parties", {"ctag": "c0", "sync_token": "c0"})
    return db


@pytest.fixture
def detenteur(libre):
    """The same contact — holding one proof of identity."""
    libre.seed(PROOF_PATH, _preuve("seul"))
    return libre


def _nothing_deleted(db) -> None:
    """The contact stands: no tombstone, no CTag move, no trail row."""
    assert db.peek("parties/seul") is not None
    assert db.peek("dav_sync/parties")["ctag"] == "c0"
    assert db.peek("dav_sync/parties/tombstones/seul") is None
    assert db.peek_collection("audit_events") == {}


def _deleted_and_journaled(db) -> None:
    """Gone, and announced: a tombstone, a new CTag, ONE trail row."""
    assert db.peek("parties/seul") is None
    assert db.peek("dav_sync/parties/tombstones/seul") is not None
    assert db.peek("dav_sync/parties")["ctag"] != "c0"
    rows = list(db.peek_collection("audit_events").values())
    assert [(r["entity_type"], r["entity_id"]) for r in rows] == [
        ("partie", "seul")]


def _web_refusal(web) -> str:
    """POST the fiche's « Supprimer »: the refusal's ``erreur``, after
    checking it lands back on the contact's own page and is rendered."""
    resp = web.post("/parties/seul/delete")
    assert resp.status_code == 302
    target = urlparse(resp.headers["Location"])
    assert target.path.endswith("/parties/seul")
    erreur = parse_qs(target.query).get("erreur", [""])[0]
    page = web.get(resp.headers["Location"])
    assert page.status_code == 200
    assert str(escape(erreur)) in page.get_data(as_text=True)
    return erreur


def _fail_queries_on(monkeypatch, db, collection: str, *, in_transaction):
    """The fake server refuses the queries on *collection* made inside
    (True) or outside (False) a transaction; returns the refusals seen."""
    server = db._fake_server
    real = server.run_query
    refused: list = []

    def failing(request, metadata=None, **kwargs):
        sq = request["structured_query"]._pb
        if (any(f.collection_id == collection for f in sq.from_)
                and bool(request.get("transaction")) == in_transaction):
            refused.append(collection)
            raise gexc.ServiceUnavailable("injected query failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", failing)
    return refused


def _fail_transactional_get_of(monkeypatch, db, path: str):
    """The fake server refuses a read of *path* made INSIDE a transaction
    (the plain reads before it still work); returns the refusals seen."""
    server = db._fake_server
    real = server.batch_get_documents
    refused: list = []

    def failing(request, metadata=None, **kwargs):
        names = [server.doc_rel(n) for n in request["documents"]]
        if request.get("transaction") and path in names:
            refused.append(path)
            raise gexc.ServiceUnavailable("injected read failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", failing)
    return refused


def _deletes_the_contact(server, request) -> bool:
    for write in request.get("writes") or []:
        pb = getattr(write, "_pb", write)
        if (pb.WhichOneof("operation") == "delete"
                and server.doc_rel(pb.delete) == "parties/seul"):
            return True
    return False


def _contact_commit_raises(monkeypatch, db, *, lands: bool,
                           rereads_fail: bool = False) -> dict:
    """The commit that deletes the contact RAISES — after applying (its
    answer lost) when *lands*, before otherwise. With *rereads_fail*,
    every plain read of the contact after that fails too."""
    server = db._fake_server
    real_commit = server.commit
    real_get = server.batch_get_documents
    state = {"raised": False, "rereads": 0}

    def commit(request, metadata=None, **kwargs):
        if not state["raised"] and _deletes_the_contact(server, request):
            state["raised"] = True
            if lands:
                real_commit(request, metadata=metadata, **kwargs)
            raise gexc.DeadlineExceeded("answer lost")
        return real_commit(request, metadata=metadata, **kwargs)

    def batch_get(request, metadata=None, **kwargs):
        names = [server.doc_rel(n) for n in request["documents"]]
        if (rereads_fail and state["raised"]
                and not request.get("transaction")
                and "parties/seul" in names):
            state["rereads"] += 1
            raise gexc.ServiceUnavailable("injected re-read failure")
        return real_get(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "commit", commit)
    monkeypatch.setattr(server, "batch_get_documents", batch_get)
    return state


# ── The refusal ─────────────────────────────────────────────────────────


def test_the_refusal_is_static_and_names_no_one():
    """CardDAV maps it through its generic « references » branch, whose
    body carries the text: it must name no one, and be none of the three
    constants that branch excludes."""
    assert pm.PARTIE_HOLDS_ID_PROOFS == (
        "Impossible de supprimer : ce contact détient des preuves "
        "d'identité. Supprimez-les d'abord dans la section Conformité de sa "
        "fiche.")
    assert pm.PARTIE_HOLDS_ID_PROOFS not in (
        pm.PARTIE_NOT_FOUND, pm.PARTIE_DELETE_CHECK_UNAVAILABLE,
        pm.PARTIE_DELETE_FAILED)


def test_a_contact_holding_proofs_refuses_web_deletion_with_a_banner(
    detenteur, web
):
    assert _web_refusal(web) == pm.PARTIE_HOLDS_ID_PROOFS
    _nothing_deleted(detenteur)
    assert detenteur.peek(PROOF_PATH) is not None


def test_a_carddav_delete_of_a_contact_holding_proofs_is_a_french_409(
    detenteur, dav, caplog
):
    with caplog.at_level(logging.DEBUG):
        resp = dav.delete("/dav/addressbook/seul.vcf", headers=AUTH)
    assert resp.status_code == 409
    assert resp.get_data(as_text=True) == pm.PARTIE_HOLDS_ID_PROOFS
    assert resp.headers["Content-Type"].startswith("text/plain")
    _nothing_deleted(detenteur)
    assert detenteur.peek(PROOF_PATH) is not None
    logged = " ".join(r.getMessage() for r in caplog.records)
    for word in ("Paul", "Roy", "Permis", "preuves d'identité"):
        assert word not in logged, word


def test_the_proofs_refusal_comes_after_the_reference_conflicts(famille):
    """The plan's order: a dossier link, then a represented contact, then
    the proofs — the first conflict the lawyer must resolve is named."""
    famille.seed("parties/marie/pieces_identite/" + PROOF, _preuve("marie"))
    ok, message = pm.delete_partie("marie")
    assert ok is False
    assert "mandataire de Sophie Gagnon" in message
    famille.seed("parties/sophie/pieces_identite/" + PROOF, _preuve("sophie"))
    assert pm.delete_partie("sophie") == (False, pm.PARTIE_HOLDS_ID_PROOFS)


def test_a_contact_without_proofs_still_deletes_and_is_journaled(libre, dav):
    """The guard bites only on a proof: a free contact goes, announced."""
    resp = dav.delete("/dav/addressbook/seul.vcf", headers=AUTH)
    assert resp.status_code == 204
    _deleted_and_journaled(libre)


# ── A proofs read that cannot be made ───────────────────────────────────


@pytest.mark.parametrize("caller", ["web", "dav"])
def test_the_proofs_check_fails_closed(libre, web, dav, monkeypatch, caller):
    """The contact holds NO proof: a check that failed OPEN would delete
    it. Refused — « réessayez » on the web, 503 + Retry-After on DAV."""
    refused = _fail_queries_on(monkeypatch, libre, pi.SUBCOLLECTION,
                               in_transaction=False)
    if caller == "web":
        assert _web_refusal(web) == pm.PARTIE_DELETE_CHECK_UNAVAILABLE
        assert "Veuillez réessayer" in pm.PARTIE_DELETE_CHECK_UNAVAILABLE
    else:
        resp = dav.delete("/dav/addressbook/seul.vcf", headers=AUTH)
        assert resp.status_code == 503
        assert resp.headers.get("Retry-After")
    assert refused, "the injected failure never ran"
    _nothing_deleted(libre)


@pytest.mark.parametrize("caller", ["model", "web", "dav"])
@pytest.mark.parametrize("read", ["contact", "proofs"])
def test_a_read_failing_inside_the_transaction_is_check_unavailable(
    libre, web, dav, monkeypatch, read, caller
):
    """delete_dossier's branch: the re-read of the contact, or of its
    proofs, THROUGH the transaction fails — the deletion could not be
    checked (503 + Retry-After on DAV, never the 500 of a failed write)."""
    if read == "contact":
        refused = _fail_transactional_get_of(monkeypatch, libre,
                                             "parties/seul")
    else:
        refused = _fail_queries_on(monkeypatch, libre, pi.SUBCOLLECTION,
                                   in_transaction=True)
    logged: list = []
    monkeypatch.setattr(
        pm, "log_unexpected",
        lambda message, **fields: logged.append((message, fields)))
    if caller == "model":
        assert pm.delete_partie("seul") == (
            False, pm.PARTIE_DELETE_CHECK_UNAVAILABLE)
    elif caller == "web":
        assert _web_refusal(web) == pm.PARTIE_DELETE_CHECK_UNAVAILABLE
    else:
        resp = dav.delete("/dav/addressbook/seul.vcf", headers=AUTH)
        assert resp.status_code == 503
        assert resp.headers.get("Retry-After")
    assert refused, "the injected failure never ran"
    assert ("partie delete: read failed",
            {"partie_id": "seul", "check": "transaction"}) in logged
    _nothing_deleted(libre)


# ── The race ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("caller", ["web", "dav"])
def test_a_proof_filed_during_the_delete_makes_it_refuse(
    libre, web, dav, caller
):
    """A proof filed between the checks and the commit: the transaction
    read the contact's proofs, so its commit aborts, and the re-run
    refuses — never a contact deleted with a proof left behind it."""
    fired: list = []

    def hook(info):
        if not fired and ("delete", "parties/seul") in info.ops:
            fired.append(info.index)
            libre.external_write(PROOF_PATH, _preuve("seul"))

    remove = libre.add_commit_hook(hook)
    try:
        if caller == "web":
            assert _web_refusal(web) == pm.PARTIE_HOLDS_ID_PROOFS
        else:
            resp = dav.delete("/dav/addressbook/seul.vcf", headers=AUTH)
            assert resp.status_code == 409
            assert resp.get_data(as_text=True) == pm.PARTIE_HOLDS_ID_PROOFS
    finally:
        remove()
    assert fired, "the race never ran"
    _nothing_deleted(libre)
    assert libre.peek(PROOF_PATH) is not None


def test_a_contact_deleted_meanwhile_is_not_found_and_journaled_once(
    libre, dav, monkeypatch
):
    """Another writer deletes the contact between the checks and the
    transaction: its re-read answers « introuvable » (DAV 404) — no second
    tombstone, no second trail row for one deletion."""
    real = pi.has_proofs_strict

    def racing(partie_id, *, transaction=None):
        if transaction is None:
            libre.external_delete("parties/seul")
        return real(partie_id, transaction=transaction)

    monkeypatch.setattr(pi, "has_proofs_strict", racing)
    resp = dav.delete("/dav/addressbook/seul.vcf", headers=AUTH)
    assert resp.status_code == 404
    assert libre.peek("dav_sync/parties/tombstones/seul") is None
    assert libre.peek_collection("audit_events") == {}


# ── A commit that raised ────────────────────────────────────────────────


@pytest.mark.parametrize("caller", ["web", "dav"])
def test_a_commit_whose_answer_was_lost_still_tombstones_and_journals(
    libre, web, dav, monkeypatch, caller
):
    """The delete LANDED and its answer was lost: the contact's absence
    proves it. Reported as failed, neither caller would tombstone or bump
    the CTag — DavX5 would keep the card, and a later edit on the phone
    would PUT the contact back — nor journal the deletion."""
    state = _contact_commit_raises(monkeypatch, libre, lands=True)
    if caller == "web":
        resp = web.post("/parties/seul/delete")
        assert resp.status_code == 302
        assert urlparse(resp.headers["Location"]).path.endswith("/parties/")
    else:
        resp = dav.delete("/dav/addressbook/seul.vcf", headers=AUTH)
        assert resp.status_code == 204
    assert state["raised"], "the lost answer never happened"
    _deleted_and_journaled(libre)


@pytest.mark.parametrize("caller", ["web", "dav"])
def test_a_commit_that_raised_without_landing_deletes_nothing(
    libre, web, dav, monkeypatch, caller
):
    state = _contact_commit_raises(monkeypatch, libre, lands=False)
    if caller == "web":
        assert _web_refusal(web) == pm.PARTIE_DELETE_FAILED
    else:
        resp = dav.delete("/dav/addressbook/seul.vcf", headers=AUTH)
        assert resp.status_code == 500
    assert state["raised"]
    _nothing_deleted(libre)


@pytest.mark.parametrize("lands", [True, False])
def test_an_unknowable_outcome_is_a_failure_never_a_success(
    libre, monkeypatch, caplog, lands
):
    """The commit raised and the re-read that would tell raised too: never
    announced as a deletion (no tombstone for a contact that may stand),
    and the message claims nothing about what was written."""
    state = _contact_commit_raises(monkeypatch, libre, lands=lands,
                                   rereads_fail=True)
    with caplog.at_level(logging.WARNING, logger=pm.logger.name):
        assert pm.delete_partie("seul") == (False, pm.PARTIE_DELETE_FAILED)
    assert state["raised"] and state["rereads"] == 1
    assert "rien n'a été supprimé" not in pm.PARTIE_DELETE_FAILED
    assert (libre.peek("parties/seul") is None) is lands
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno == logging.WARNING]
    assert any(m.startswith("delete_partie: outcome unreadable for seul")
               for m in warnings), warnings
    assert not any("Paul" in m or "Roy" in m for m in warnings)
