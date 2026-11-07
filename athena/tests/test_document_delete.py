"""Deleting a document — the model and the web route (2026-11).

Before this lot, ``models/document.delete_document`` and the route
``documents.document_delete``:

* read the document through the FAIL-OPEN ``get_document``: a Firestore
  blip answered « Document introuvable. » about a document still there;
* deleted the record by a plain ``delete()``: a commit whose answer was
  lost after it landed reported an error and journaled nothing, and two
  concurrent deletions both answered success — two audit rows for one
  deletion;
* lost every refusal: without htmx the route redirected to the browser as
  if the document were gone, and under htmx it answered a 422 fragment
  htmx never swaps (Rule 8);
* journaled the deletion from the route's own fail-open read: a blip there
  wrote an audit row with no dossier and no title.

Each test marked « fails on the old code » pins one of those; « fails on the
round-1 code » marks what review then found in this lot's first version (a
refusal lost when its page could not be shown, the ERROR's traceback), and
« fails without » names the guard a test proves. Everything runs through
the REAL model, blueprint and templates, on the fake Firestore
(``tests/_fake_firestore.py``: the real client, a fake server) and the fake
Cloud Storage (``tests/_fake_gcs.py``); what is asserted is what the STORE
and the BUCKET hold, where the browser lands, what the page shows and what
the logs carry.
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
    from models import audit_event as audit_model
    from models import concurrency
    from models import document as doc
    from models import folder as folder_model
    import routes.documents as documents_routes

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBlob, FakeBucket  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.markdown_docx import markdown_to_safe_html  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

UTC = timezone.utc
DT = datetime(2026, 3, 4, tzinfo=UTC)
DOC = "doc1"
PATH = f"documents/{DOC}"
OBJECT = f"users/u1/dossiers/d1/documents/{DOC}/lettre-tremblay.pdf"
PDF = b"%PDF-1.7\n" + b"lettre " * 40
TITLE = "Lettre à Me Tremblay"
# The browser, filtered to the document's dossier and folder: where a
# deletion lands without a return_to.
BROWSER = "/documents/?dossier_id=d1&folder_id=f1"

# The model's sentences, spelled out: a test comparing to the module's own
# constant would raise AttributeError on the old code instead of failing on
# what it pins.
NOT_FOUND = "Document introuvable."
DELETE_FAILED = "Erreur lors de la suppression. Veuillez réessayer."
FILE_FAILED = "Erreur lors de la suppression du fichier. Veuillez réessayer."


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, doc, folder_model, audit_model)


@pytest.fixture
def bucket(monkeypatch):
    fake = FakeBucket()
    # firebase_admin.storage — the module the model and the route reach.
    monkeypatch.setattr(doc.storage, "bucket", lambda: fake)
    return fake


@pytest.fixture
def web(db, bucket, monkeypatch):
    # The detail page signs an inline URL for a PDF: never here — a real
    # signing reaches Google with the workstation's credentials.
    monkeypatch.setattr(documents_routes, "get_signed_url",
                        lambda *_a, **_kw: None)
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
    app.register_blueprint(documents_routes.documents_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return client


def _seed(db, bucket, *, with_file=True, **over) -> dict:
    """A filed document: its record and (unless *with_file* is False) its
    object."""
    record = {**doc._default_doc(), "id": DOC, "dossier_id": "d1",
              "folder_id": "f1", "display_name": TITLE,
              "filename": "lettre-tremblay.pdf",
              "original_filename": "lettre-tremblay.pdf",
              "file_type": "application/pdf", "file_size": len(PDF),
              "storage_path": OBJECT, "category": "correspondance",
              "category_source": "juriste", "created_at": DT,
              "updated_at": DT, "etag": "e0"}
    record.update(over)
    db.seed(PATH, record)
    if with_file:
        bucket.put(OBJECT, PDF, content_type="application/pdf")
    return record


def _audit_rows(db) -> list:
    return list(db.peek_collection("audit_events").values())


def _unexpected(caplog) -> list:
    return [(r.getMessage(),
             {k: v for k, v in r.json_fields.items() if k != "event"})
            for r in caplog.records if r.name == "pallas.unexpected"]


def _landing(resp) -> tuple[str, dict]:
    assert resp.status_code == 302, resp.get_data(as_text=True)[:300]
    target = urlparse(resp.headers["Location"])
    return target.path, {k: v[0] for k, v in parse_qs(target.query).items()}


def _delete(web, **kwargs):
    return web.post(f"/documents/{DOC}/delete", **kwargs)


def _refusal_shown(web, resp) -> str:
    """The refusal lands back on the document's own page, its sentence in
    the page's banner; returns the sentence."""
    path, query = _landing(resp)
    assert path == f"/documents/{DOC}"
    erreur = query["erreur"]
    page = web.get(resp.headers["Location"])
    assert page.status_code == 200
    banner = f'<div role="alert" class="text-red-600 text-sm mb-4">{escape(erreur)}</div>'
    assert banner in page.get_data(as_text=True)
    return erreur


def _nothing_deleted(db, bucket) -> None:
    assert db.peek(PATH) is not None
    assert OBJECT in bucket.objects
    assert _audit_rows(db) == []


def _fail_reads_of_the_record(monkeypatch, db, when) -> list:
    """The fake server refuses a read of the record when ``when(n, in_txn)``
    holds — *n* counts the reads of the record from 1; returns, read by
    read, whether each was made inside a transaction."""
    server = db._fake_server
    real = server.batch_get_documents
    seen: list = []

    def failing(request, metadata=None, **kwargs):
        if PATH in [server.doc_rel(n) for n in request["documents"]]:
            in_txn = bool(request.get("transaction"))
            seen.append(in_txn)
            if when(len(seen), in_txn):
                raise gexc.ServiceUnavailable("injected read failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", failing)
    return seen


def _deletes_the_record(server, request) -> bool:
    for write in request.get("writes") or []:
        pb = getattr(write, "_pb", write)
        if (pb.WhichOneof("operation") == "delete"
                and server.doc_rel(pb.delete) == PATH):
            return True
    return False


def _record_commit_raises(monkeypatch, db, *, lands: bool,
                          rereads_fail: bool = False) -> dict:
    """The commit that deletes the record RAISES — after applying (its
    answer lost) when *lands*, before otherwise. With *rereads_fail*, every
    plain read of the record after that fails too."""
    server = db._fake_server
    real_commit = server.commit
    real_get = server.batch_get_documents
    state = {"raised": False, "rereads": 0}

    def commit(request, metadata=None, **kwargs):
        if not state["raised"] and _deletes_the_record(server, request):
            state["raised"] = True
            if lands:
                real_commit(request, metadata=metadata, **kwargs)
            raise gexc.DeadlineExceeded("answer lost")
        return real_commit(request, metadata=metadata, **kwargs)

    def batch_get(request, metadata=None, **kwargs):
        names = [server.doc_rel(n) for n in request["documents"]]
        if (rereads_fail and state["raised"]
                and not request.get("transaction") and PATH in names):
            state["rereads"] += 1
            raise gexc.ServiceUnavailable("injected re-read failure")
        return real_get(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "commit", commit)
    monkeypatch.setattr(server, "batch_get_documents", batch_get)
    return state


# ══════════════════════════════════════════════════════════════════════
# 1. The model
# ══════════════════════════════════════════════════════════════════════


def test_the_refusals_are_the_sentences_the_route_routes_on():
    assert doc.DELETE_NOT_FOUND == NOT_FOUND
    assert doc.DELETE_FAILED == DELETE_FAILED
    assert doc.DELETE_FILE_FAILED == FILE_FAILED
    assert documents_routes.DELETE_NOT_FOUND is doc.DELETE_NOT_FOUND


def test_a_deletion_erases_the_file_then_the_record_in_one_transaction(
        db, bucket):
    """Fails on the old code (a plain delete(), no ``deleted_out``). The
    record is read THROUGH the transaction that deletes it, and the caller
    receives the record that transaction deleted."""
    _seed(db, bucket, category="note")      # a legacy key, migrated on read
    db.reset_logs()
    out: dict = {}

    assert doc.delete_document(DOC, deleted_out=out) == (True, "")

    assert db.peek(PATH) is None and OBJECT not in bucket.objects
    written = [c for c in db.commits if c.ops]
    assert [(c.ops, c.transaction is not None) for c in written] == [
        ((("delete", PATH),), True)]
    assert any(r.transactional and PATH in r.paths for r in db.reads)
    assert (out["id"], out["dossier_id"], out["display_name"],
            out["category"]) == (DOC, "d1", TITLE, "autre")


def test_a_failed_read_refuses_with_nothing_deleted_never_introuvable(
        db, bucket, monkeypatch, caplog):
    """Fails on the old code: its fail-open read answered the blip with
    « Document introuvable. » — about a document that was still there."""
    _seed(db, bucket)
    _fail_reads_of_the_record(monkeypatch, db, lambda n, in_txn: True)
    caplog.set_level(logging.INFO)

    assert doc.delete_document(DOC) == (
        False, concurrency.READ_UNAVAILABLE_ERROR)

    _nothing_deleted(db, bucket)
    assert _unexpected(caplog) == [("document delete: read failed", {
        "document_id": DOC, "error_type": "ServiceUnavailable"})]
    assert not any(r.exc_info for r in caplog.records)


def test_deleted_out_is_filled_on_success_only(db, bucket, monkeypatch):
    """Fails on the old code (no ``deleted_out``). Nor is the record
    handed out before its deletion lands: a refused file or a failed
    commit leaves the caller nothing to journal."""
    _seed(db, bucket)
    out: dict = {}
    assert doc.delete_document("absent", deleted_out=out) == (
        False, NOT_FOUND)
    with monkeypatch.context() as m:
        _fail_reads_of_the_record(m, db, lambda n, in_txn: True)
        assert doc.delete_document(DOC, deleted_out=out)[0] is False
    assert out == {}

    def _storage_refuses(self, *a, **kw):
        raise gexc.ServiceUnavailable("503")

    with monkeypatch.context() as m:
        m.setattr(FakeBlob, "delete", _storage_refuses)
        assert doc.delete_document(DOC, deleted_out=out) == (
            False, FILE_FAILED)
    assert out == {}
    with monkeypatch.context() as m:
        _record_commit_raises(m, db, lands=False)
        assert doc.delete_document(DOC, deleted_out=out) == (
            False, DELETE_FAILED)
    assert out == {}
    assert doc.delete_document(DOC, deleted_out=out) == (True, "")
    assert out["id"] == DOC


def test_a_document_deleted_meanwhile_is_introuvable_never_a_second_success(
        db, bucket, caplog):
    """Fails on the old code. Another call deletes the record between this
    one's read and its commit: the transaction read it, so its commit
    aborts and the re-run finds it gone — « introuvable ». The old plain
    delete() of a record already gone answered success, and its route
    journaled a second deletion. A plain race, not a failure: no ERROR."""
    _seed(db, bucket)
    fired: list = []

    def hook(info):
        if not fired and ("delete", PATH) in info.ops:
            fired.append(info.index)
            db.external_delete(PATH)

    caplog.set_level(logging.INFO)
    remove = db.add_commit_hook(hook)
    try:
        assert doc.delete_document(DOC) == (False, NOT_FOUND)
    finally:
        remove()
    assert fired, "the race never ran"
    assert db.peek(PATH) is None
    assert _unexpected(caplog) == []


def _aborted_then_unreadable(monkeypatch, db) -> tuple[dict, object]:
    """Another call's deletion lands while this call's FIRST attempt
    commits — that commit aborts, and the decorator runs the body again —,
    and every later read of the record through a transaction fails.
    Returns the state and the commit hook's remover."""
    server = db._fake_server
    real = server.batch_get_documents
    state = {"aborted": False}

    def hook(info):
        if not state["aborted"] and ("delete", PATH) in info.ops:
            state["aborted"] = True
            db.external_delete(PATH)

    def failing(request, metadata=None, **kwargs):
        names = [server.doc_rel(n) for n in request["documents"]]
        if state["aborted"] and request.get("transaction") and PATH in names:
            raise gexc.ServiceUnavailable("injected read failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", failing)
    return state, db.add_commit_hook(hook)


def test_an_aborted_attempt_never_counts_as_staged(db, bucket, monkeypatch):
    """Fails without the body's ``staged.clear()``. The first attempt
    stages the delete, but its commit aborts (another call deleted the
    record first); the next attempt's read fails before it stages
    anything. Nothing of THIS call landed: « introuvable » — never the
    success the first attempt's leftover stage would claim, which the
    route would journal a second time."""
    _seed(db, bucket)
    state, remove = _aborted_then_unreadable(monkeypatch, db)
    try:
        assert doc.delete_document(DOC) == (False, NOT_FOUND)
    finally:
        remove()
    assert state["aborted"], "the abort never happened"
    assert db.peek(PATH) is None


@pytest.mark.parametrize("gone", [True, False])
def test_a_read_failing_inside_the_transaction_is_settled_on_the_record(
        db, bucket, monkeypatch, gone):
    """Fails on the old code (no transaction: it deleted and answered
    success). The re-read THROUGH the transaction fails, so nothing was
    staged and nothing of this call can have landed: the record gone
    meanwhile is « introuvable » — another call deleted it, never a success
    of this one —; still there, it is the save error, and the next
    « Supprimer » completes it (the file, deleted first, then answers
    NotFound)."""
    _seed(db, bucket)
    server = db._fake_server
    real = server.batch_get_documents

    def failing(request, metadata=None, **kwargs):
        names = [server.doc_rel(n) for n in request["documents"]]
        if request.get("transaction") and PATH in names:
            if gone:
                db.external_delete(PATH)
            raise gexc.ServiceUnavailable("injected read failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", failing)

    assert doc.delete_document(DOC) == (
        False, NOT_FOUND if gone else DELETE_FAILED)
    assert (db.peek(PATH) is None) is gone
    assert OBJECT not in bucket.objects
    if not gone:
        monkeypatch.setattr(server, "batch_get_documents", real)
        assert doc.delete_document(DOC) == (True, "")
        assert db.peek(PATH) is None


@pytest.mark.parametrize("lands", [True, False])
def test_an_unknowable_outcome_is_a_failure_and_warns_with_the_id(
        db, bucket, monkeypatch, caplog, lands):
    """Fails on the old code (no warning; and lands=True journals no row
    either way). The commit raised and the re-read that would tell raised
    too: never announced as a deletion, and the sentence claims nothing
    about what was written. Every line carries ids and classes only — the
    ERROR included, whose traceback the round-1 code still logged."""
    _seed(db, bucket)
    state = _record_commit_raises(monkeypatch, db, lands=lands,
                                  rereads_fail=True)
    caplog.set_level(logging.INFO)

    assert doc.delete_document(DOC) == (False, DELETE_FAILED)

    assert state["raised"] and state["rereads"] == 1
    assert (db.peek(PATH) is None) is lands
    warnings = [r.getMessage() for r in caplog.records
                if r.name == doc.logger.name and r.levelno == logging.WARNING]
    assert warnings == [
        f"delete_document: outcome unreadable for {DOC}: ServiceUnavailable"]
    assert _unexpected(caplog) == [("document delete failed", {
        "document_id": DOC, "error_type": "DeadlineExceeded"})]
    assert not any(r.exc_info for r in caplog.records)


# ══════════════════════════════════════════════════════════════════════
# 2. The web route
# ══════════════════════════════════════════════════════════════════════


def test_a_deletion_lands_on_the_browser_and_journals_one_row(web, db, bucket):
    """The unchanged success (a guard, green on the old code too)."""
    _seed(db, bucket)

    resp = _delete(web)

    assert resp.status_code == 302 and resp.headers["Location"] == BROWSER
    assert "HX-Redirect" not in resp.headers
    assert [(r["entity_type"], r["entity_id"], r["dossier_id"],
             r["snapshot_min"]) for r in _audit_rows(db)] == [
        ("document", DOC, "d1", {"title": TITLE, "status": "correspondance"})]
    assert db.peek(PATH) is None and OBJECT not in bucket.objects
    # Gone: « introuvable » takes the success's way out (return_to when
    # internal), and journals nothing more.
    again = _delete(web, data={"return_to": "/dossiers/d1?tab=documents"})
    assert again.headers["Location"] == "/dossiers/d1?tab=documents"
    assert len(_audit_rows(db)) == 1


def test_the_audit_row_comes_from_the_record_the_model_deleted(
        web, db, bucket, monkeypatch):
    """Fails on the old code: the route journaled the deletion from its own
    fail-open read, so a blip there wrote a row with no dossier and no
    title — and sent the lawyer to the flat browser."""
    _seed(db, bucket)
    seen = _fail_reads_of_the_record(monkeypatch, db,
                                     lambda n, in_txn: n == 1)

    resp = _delete(web)

    assert seen[0] is False, "the first read was the route's own"
    (row,) = _audit_rows(db)
    assert (row["entity_id"], row["dossier_id"], row["snapshot_min"]) == (
        DOC, "d1", {"title": TITLE, "status": "correspondance"})
    assert resp.headers["Location"] == BROWSER


def test_the_audit_row_names_the_record_as_its_deletion_read_it(
        web, db, bucket, monkeypatch):
    """Fails on the old code (the route's own earlier read), and if
    ``deleted_out`` took the model's strict pre-read. The document is
    renamed while its file is being erased: the row names the record as
    the transaction that deleted it read it."""
    record = _seed(db, bucket)
    real_delete = FakeBlob.delete

    def _renamed_meanwhile(self, *a, **kw):
        if self.name == OBJECT:
            db.external_write(PATH, {**record, "display_name": "Renommé"})
        return real_delete(self, *a, **kw)

    monkeypatch.setattr(FakeBlob, "delete", _renamed_meanwhile)

    resp = _delete(web)

    assert resp.headers["Location"] == BROWSER
    (row,) = _audit_rows(db)
    assert (row["entity_id"], row["snapshot_min"]["title"]) == (
        DOC, "Renommé")
    assert db.peek(PATH) is None


def test_two_deletions_racing_journal_once(web, db, bucket, monkeypatch):
    """Fails on the old code (two rows). Two « Supprimer » in flight (a
    double tap, a second tab): the second runs to completion while the
    first is erasing the file. ONE removes the record and journals it; the
    other finds it gone — the browser all the same, and no second row."""
    _seed(db, bucket)
    real_delete = FakeBlob.delete
    nested: list = []

    def _delete_racing(self, client=None, if_generation_match=None, **kw):
        if self.name == OBJECT and not nested:
            nested.append(None)
            nested[0] = _delete(web)
        return real_delete(self, client=client,
                           if_generation_match=if_generation_match, **kw)

    monkeypatch.setattr(FakeBlob, "delete", _delete_racing)

    first = _delete(web)

    second = nested[0]
    assert second.headers["Location"] == first.headers["Location"] == BROWSER
    assert len(_audit_rows(db)) == 1
    assert db.peek(PATH) is None and OBJECT not in bucket.objects


def test_an_aborted_attempt_then_a_failed_read_journals_nothing(
        web, db, bucket, monkeypatch):
    """The route side of ``test_an_aborted_attempt_never_counts_as_staged``
    (fails without the body's ``staged.clear()``): another call deleted the
    document — and journals it —; this one lands on the browser like any
    « introuvable », with no row of its own."""
    _seed(db, bucket)
    state, remove = _aborted_then_unreadable(monkeypatch, db)
    try:
        resp = _delete(web)
    finally:
        remove()
    assert state["aborted"], "the abort never happened"
    assert resp.status_code == 302 and resp.headers["Location"] == BROWSER
    assert _audit_rows(db) == []


def test_a_commit_whose_answer_was_lost_is_a_deletion_journaled_once(
        web, db, bucket, monkeypatch, caplog):
    """Fails on the old code: the record was gone, the route answered as
    for any refusal and journaled nothing. The record's absence proves the
    commit landed: a deletion, its one row, the ERROR beside it."""
    _seed(db, bucket)
    state = _record_commit_raises(monkeypatch, db, lands=True)
    caplog.set_level(logging.INFO)

    resp = _delete(web)

    assert state["raised"], "the lost answer never happened"
    assert resp.headers["Location"] == BROWSER
    assert db.peek(PATH) is None
    assert [(r["entity_id"], r["snapshot_min"]["title"])
            for r in _audit_rows(db)] == [(DOC, TITLE)]
    assert _unexpected(caplog) == [("document delete failed", {
        "document_id": DOC, "error_type": "DeadlineExceeded"})]
    assert not any(r.exc_info for r in caplog.records)


def test_a_commit_that_raised_without_landing_is_said_on_the_document_page(
        web, db, bucket, monkeypatch):
    """Fails on the old code: the refusal redirected to the browser as if
    the document were gone. The record is still there: the page says so,
    and the next « Supprimer » completes the deletion."""
    _seed(db, bucket)
    state = _record_commit_raises(monkeypatch, db, lands=False)

    assert _refusal_shown(web, _delete(web)) == DELETE_FAILED

    assert state["raised"]
    assert db.peek(PATH) is not None and _audit_rows(db) == []
    assert OBJECT not in bucket.objects            # the file went first
    again = _delete(web)
    assert again.headers["Location"] == BROWSER
    assert db.peek(PATH) is None and len(_audit_rows(db)) == 1


def test_a_failed_read_is_said_on_the_document_page_never_introuvable(
        web, db, bucket, monkeypatch, caplog):
    """Fails on the old code: « Document introuvable. », and a redirect to
    the browser — the document still there, and no word of it."""
    _seed(db, bucket)
    caplog.set_level(logging.INFO)
    with monkeypatch.context() as m:
        _fail_reads_of_the_record(m, db, lambda n, in_txn: True)
        resp = _delete(web)

    assert _refusal_shown(web, resp) == concurrency.READ_UNAVAILABLE_ERROR
    _nothing_deleted(db, bucket)
    assert [m for m, _f in _unexpected(caplog)] == [
        "document delete: read failed"]


def test_a_refusal_whose_page_cannot_be_shown_still_reaches_the_browser(
        web, db, bucket, monkeypatch):
    """Fails on the round-1 code. The reads of the record keep failing — the
    deletion's strict read, then, on the redirect, the document page's own
    fail-open one (an outage outlasts one request: the client retries a
    failed read for minutes before it raises) —, so the page, finding no
    document, sent the lawyer to the browser WITHOUT the refusal: a list
    with no word that the deletion was refused, the document intact. The
    refusal now goes on to the browser, which prints it."""
    _seed(db, bucket)
    _fail_reads_of_the_record(monkeypatch, db, lambda n, in_txn: True)
    refusal = {"erreur": concurrency.READ_UNAVAILABLE_ERROR}

    resp = _delete(web)

    assert _landing(resp) == (f"/documents/{DOC}", refusal)
    page = web.get(resp.headers["Location"])
    assert _landing(page) == ("/documents/", refusal)
    listing = web.get(page.headers["Location"], headers={"HX-Request": "true"})
    assert listing.status_code == 200
    banner = (f'<div class="text-red-600 text-sm mb-4">'
              f'{escape(concurrency.READ_UNAVAILABLE_ERROR)}</div>')
    assert banner in listing.get_data(as_text=True)
    _nothing_deleted(db, bucket)


@pytest.mark.parametrize("return_to, kept", [
    ("", None),
    ("/dossiers/d1?tab=documents", "/dossiers/d1?tab=documents"),
    ("https://evil.example/x", None),
])
def test_a_refused_deletion_is_said_on_the_document_page(
        web, db, bucket, monkeypatch, caplog, return_to, kept):
    """Fails on the old code: the refusal redirected to the browser (or the
    return_to) as if the document were gone. It lands back on the
    document, its « Retour » kept when it was internal; the Storage error,
    whose text names the object (the client's file name), never reaches a
    log."""
    _seed(db, bucket)
    real_delete = FakeBlob.delete

    def _refused(self, *a, **kw):
        if self.name == OBJECT:
            raise gexc.ServiceUnavailable(f"503 DELETE {OBJECT}")
        return real_delete(self, *a, **kw)

    monkeypatch.setattr(FakeBlob, "delete", _refused)
    caplog.set_level(logging.DEBUG)

    resp = _delete(web, data={"return_to": return_to})

    assert _refusal_shown(web, resp) == FILE_FAILED
    _path, query = _landing(resp)
    assert query.get("return_to") == kept
    if kept:
        page = web.get(resp.headers["Location"]).get_data(as_text=True)
        assert f'href="{escape(kept)}"' in page
    _nothing_deleted(db, bucket)
    assert _unexpected(caplog) == [("document file delete failed", {
        "document_id": DOC, "error_type": "ServiceUnavailable"})]
    for record in caplog.records:
        texts = [record.getMessage(), repr(record.exc_info or ""),
                 repr(getattr(record, "json_fields", ""))]
        assert not any("tremblay" in t.lower() for t in texts), record


@pytest.mark.parametrize("refusal", ["file", "read", "commit"])
def test_under_htmx_a_refusal_is_an_hx_redirect_to_the_document_page(
        web, db, bucket, monkeypatch, refusal):
    """Fails on the old code: a 422 fragment, which htmx never swaps
    (Rule 8). Every refusal is a redirect, HX-Redirect for htmx.

    Since the refusals lot (2026-11-06) htmx gets a 200 carrying
    HX-Redirect and NO Location (``routes/_helpers.redirect_after_post``):
    the 302 this lot first answered carried a header the browser's XHR
    never let htmx see — it follows a 302 by itself. No page posts here
    through htmx, so no behaviour of production changed."""
    _seed(db, bucket)
    if refusal == "file":
        def _refused(self, *a, **kw):
            raise gexc.ServiceUnavailable("503")

        monkeypatch.setattr(FakeBlob, "delete", _refused)
        expected = FILE_FAILED
    elif refusal == "read":
        _fail_reads_of_the_record(monkeypatch, db, lambda n, in_txn: True)
        expected = concurrency.READ_UNAVAILABLE_ERROR
    else:
        _record_commit_raises(monkeypatch, db, lands=False)
        expected = DELETE_FAILED

    resp = _delete(web, headers={"HX-Request": "true"})

    assert resp.status_code == 200 and "Location" not in resp.headers
    target = urlparse(resp.headers["HX-Redirect"])
    assert (target.path,
            {k: v[0] for k, v in parse_qs(target.query).items()}) == (
        f"/documents/{DOC}", {"erreur": expected})
    assert db.peek(PATH) is not None and _audit_rows(db) == []


def test_an_absent_document_lands_on_the_browser_htmx_included(web, db, bucket):
    """Fails on the old code under htmx (a 422 fragment). Gone either way:
    the browser, never the page of a document that does not exist — a 302
    for the plain form, a 200 carrying HX-Redirect for htmx (the refusals
    lot: no Location, which the XHR would follow by itself)."""
    plain = web.post("/documents/absent/delete")
    assert plain.status_code == 302
    assert plain.headers["Location"] == "/documents/"
    assert "HX-Redirect" not in plain.headers
    htmx = web.post("/documents/absent/delete",
                    headers={"HX-Request": "true"})
    assert htmx.status_code == 200 and "Location" not in htmx.headers
    assert htmx.headers["HX-Redirect"] == "/documents/"
    assert db.commits == [] and _audit_rows(db) == []
