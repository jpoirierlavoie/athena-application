"""The connector's receipt door — models/admin_ledger.attach_uploaded_receipt
and its readers (2026-11-10): attach, or REPLACE with the replaced object
KEPT; the precondition begin_upload bound, re-checked in the transaction;
create-only objects, a 412 adopted only for the same bytes; a lost commit
answer settled by a strict re-read — and never a delete.

Real model, fake Firestore (tests/_fake_firestore.py) and fake GCS
(tests/_fake_gcs.py), which keep bytes and generations and refuse what the
services refuse."""

import ast
import base64
import hashlib
import inspect
import os
import pathlib
import sys
import textwrap
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
    import models.admin_ledger as al
    import models.document as document_model
    from models import provenance

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402

UTC = timezone.utc
UID = "u1"
TX = "t1"
OBJ = "3f2b8c1e-9d4a-4e6b-8f7a-1c2d3e4f5a6b"
OBJ2 = "9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d"
PDF = b"%PDF-1.7\n" + b"facture fournisseur " * 20
PDF2 = b"%PDF-1.7\n" + b"autre facture " * 30
OLD = f"users/{UID}/administration/{TX}/recu.pdf"


def _md5(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()


def _dest(object_id=OBJ, ext=".pdf") -> str:
    return f"users/{UID}/administration/{TX}/pieces/{object_id}{ext}"


def _entry(**over) -> dict:
    doc = {
        "id": TX, "account_id": "ops1", "sequence": 7,
        "date": datetime(2031, 9, 12, tzinfo=UTC), "kind": "dépense",
        "direction": "déboursé", "amount": 11498, "dossier_id": "",
        "receipt_storage_path": None, "receipt_filename": "",
        "receipt_file_type": "", "receipt_file_size": 0, "receipt_md5": "",
        "revisions": [], "etag": "e0", "updated_via": "web",
    }
    doc.update(over)
    return doc


@pytest.fixture
def store(monkeypatch):
    modules = [m for n, m in sorted(sys.modules.items())
               if n.startswith("models.") and getattr(m, "db", None) is not None]
    db = install(monkeypatch, *modules)
    bucket = FakeBucket()
    monkeypatch.setattr(document_model.storage, "bucket", lambda: bucket)
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry())
    return db, bucket


@pytest.fixture
def notes(monkeypatch):
    seen = []
    monkeypatch.setattr(provenance, "note_commit",
                        lambda collection, doc_id: seen.append((collection, doc_id)))
    return seen


def _staged(bucket, data=PDF, name="staging/u1/mcp/tk/upload.pdf"):
    bucket.put(name, data, content_type="application/pdf")
    blob = bucket.blob(name)
    blob.reload()
    return blob


def _attach(source, *, object_id=OBJ, filename="Facture Bell.pdf",
            expected_path="", expected_md5="", via="mcp"):
    with provenance.writing_via(via):
        return al.attach_uploaded_receipt(
            TX, source, object_id=object_id, filename=filename, uid=UID,
            expected_path=expected_path, expected_md5=expected_md5)


def _stored(db) -> dict:
    return db.peek(f"{al.TRANSACTIONS_COLLECTION}/{TX}")


# ── Attach and replace ────────────────────────────────────────────────────


def test_a_receipt_is_attached_create_only_with_its_revision_and_stamps(store, notes):
    db, bucket = store
    out = _attach(_staged(bucket))
    assert out.code == al.RECEIPT_ATTACHED and out.errors == [] and not out.replaced
    entry = _stored(db)
    assert entry["receipt_storage_path"] == _dest()
    assert entry["receipt_filename"] == "Facture Bell.pdf"
    assert entry["receipt_file_type"] == "application/pdf"
    assert entry["receipt_file_size"] == len(PDF)
    assert entry["receipt_md5"] == _md5(PDF)
    assert entry["updated_via"] == "mcp" and entry["etag"] != "e0"
    (rev,) = entry["revisions"]
    assert rev["via"] == "mcp" and rev["motif"] == al.RECEIPT_ATTACHED_MOTIF
    assert rev["changes"]["receipt_storage_path"] == [None, _dest()]
    obj = bucket.blob(_dest())
    obj.reload()
    assert obj.download_as_bytes() == PDF
    assert obj.content_type == "application/pdf"
    assert obj.content_disposition == "attachment"
    assert notes == [(al.TRANSACTIONS_COLLECTION, TX)]


def test_a_replacement_keeps_the_previous_object_byte_identical(store, notes):
    db, bucket = store
    bucket.put(OLD, PDF, content_type="application/pdf")
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry(
        receipt_storage_path=OLD, receipt_filename="recu.pdf",
        receipt_md5=_md5(PDF), receipt_file_size=len(PDF)))
    out = _attach(_staged(bucket, PDF2), expected_path=OLD, expected_md5=_md5(PDF))
    assert out.code == al.RECEIPT_ATTACHED and out.replaced
    entry = _stored(db)
    assert entry["receipt_storage_path"] == _dest()
    assert entry["receipt_md5"] == _md5(PDF2)
    (rev,) = entry["revisions"]
    assert rev["motif"] == al.RECEIPT_REPLACED_MOTIF
    assert rev["changes"]["receipt_storage_path"] == [OLD, _dest()]
    kept = bucket.blob(OLD)
    kept.reload()
    assert kept.download_as_bytes() == PDF          # never overwritten nor deleted


def test_a_legacy_receipt_without_md5_is_replaced_on_its_path_alone(store, notes):
    db, bucket = store
    bucket.put(OLD, PDF, content_type="application/pdf")
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry(receipt_storage_path=OLD))
    out = _attach(_staged(bucket, PDF2), expected_path=OLD, expected_md5="")
    assert out.code == al.RECEIPT_ATTACHED and out.replaced
    assert al.receipt_object_md5(_entry(receipt_storage_path=OLD)) == _md5(PDF)


# ── The precondition ──────────────────────────────────────────────────────


def test_a_receipt_changed_since_begin_refuses_before_any_byte_moves(store, notes):
    db, bucket = store
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry(
        receipt_storage_path=OLD, receipt_md5=_md5(PDF)))
    out = _attach(_staged(bucket), expected_path="", expected_md5="")
    assert out.code == al.RECEIPT_CHANGED and out.errors == [al.MSG_RECEIPT_CHANGED]
    assert _dest() not in bucket.objects
    assert notes == []


def test_a_receipt_changed_inside_the_transaction_writes_nothing(store, notes):
    """The pre-read passes; the web replaces the receipt before the
    transaction reads it: refused there, the entry untouched, the copied
    object KEPT (never deleted) and named in the log."""
    db, bucket = store
    real = al.get_transaction_strict

    def _stale_then_web(tx_id):
        found = real(tx_id)
        db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry(
            receipt_storage_path=OLD, receipt_md5=_md5(PDF)))
        return found

    with mock.patch.object(al, "get_transaction_strict", _stale_then_web):
        out = _attach(_staged(bucket))
    assert out.code == al.RECEIPT_CHANGED
    assert _stored(db)["receipt_storage_path"] == OLD
    assert _dest() in bucket.objects
    assert notes == []


def test_the_entry_gone_is_said(store, notes):
    db, bucket = store
    al.db.collection(al.TRANSACTIONS_COLLECTION).document(TX).delete()
    out = _attach(_staged(bucket))
    assert out.code == al.RECEIPT_ENTRY_GONE and notes == []


# ── The bytes ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("filename, data, message", [
    ("recu.pdf", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, al.RECEIPT_CONTENT_MISMATCH_FR),
    ("recu.docx", b"PK\x03\x04" + b"\x00" * 64, al.RECEIPT_TYPE_REFUSED_FR),
    ("recu.eml", PDF, al.RECEIPT_CONTENT_MISMATCH_FR),
])
def test_bytes_that_are_not_a_receipt_of_their_type_are_refused(
        store, notes, filename, data, message):
    db, bucket = store
    out = _attach(_staged(bucket, data), filename=filename)
    assert out.code == al.RECEIPT_REFUSED and out.errors == [message]
    assert _stored(db)["receipt_storage_path"] is None
    assert not [n for n in bucket.objects if "/pieces/" in n]


def test_an_email_receipt_is_attached_as_message_rfc822(store, notes):
    db, bucket = store
    eml = b"From: Fournisseur <f@fournisseur.example>\r\nSubject: F-1\r\n\r\nx"
    out = _attach(_staged(bucket, eml, "staging/u1/mcp/tk/upload.eml"),
                  filename="F-1.eml")
    assert out.code == al.RECEIPT_ATTACHED
    assert _stored(db)["receipt_file_type"] == "message/rfc822"
    assert _stored(db)["receipt_storage_path"] == _dest(ext=".eml")


# ── Retries: create-only objects, the 412, the lost answer ───────────────


def test_a_412_on_the_same_bytes_is_adopted(store, notes):
    db, bucket = store
    bucket.put(_dest(), PDF, content_type="application/pdf")
    out = _attach(_staged(bucket))
    assert out.code == al.RECEIPT_ATTACHED
    assert _stored(db)["receipt_storage_path"] == _dest()


def test_a_412_on_other_bytes_is_refused_and_both_kept(store, notes):
    db, bucket = store
    bucket.put(_dest(), PDF2, content_type="application/pdf")
    out = _attach(_staged(bucket))
    assert out.code == al.RECEIPT_REFUSED and out.errors == [al.MSG_RECEIPT_OCCUPIED]
    occupant = bucket.blob(_dest())
    occupant.reload()
    assert occupant.download_as_bytes() == PDF2
    assert _stored(db)["receipt_storage_path"] is None


def test_an_attach_that_already_landed_writes_no_second_revision(store, notes):
    db, bucket = store
    source = _staged(bucket)
    first = _attach(source)
    again = _attach(source)
    assert first.code == al.RECEIPT_ATTACHED and again.code == al.RECEIPT_ALREADY
    assert len(_stored(db)["revisions"]) == 1
    assert notes == [(al.TRANSACTIONS_COLLECTION, TX)]


def test_a_lost_answer_that_landed_is_settled_landed(store, notes, monkeypatch):
    db, bucket = store
    server = db._fake_server
    real_commit = server.commit
    state = {"armed": True}

    def _commit(request, metadata=None, **kwargs):
        if state["armed"]:
            state["armed"] = False
            real_commit(request, metadata=metadata, **kwargs)
            raise RuntimeError("connection reset")
        return real_commit(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "commit", _commit)
    out = _attach(_staged(bucket))
    assert out.code == al.RECEIPT_ATTACHED
    assert _stored(db)["receipt_storage_path"] == _dest()
    assert notes == [(al.TRANSACTIONS_COLLECTION, TX)]


def test_a_failed_commit_is_retryable_and_keeps_the_object(store, notes):
    db, bucket = store

    def _refuse(info):
        raise RuntimeError("unavailable")

    remove = db.add_commit_hook(_refuse)
    out = _attach(_staged(bucket))
    remove()
    assert out.code == al.RECEIPT_UNAVAILABLE and notes == []
    assert _stored(db)["receipt_storage_path"] is None
    assert _dest() in bucket.objects                   # never deleted
    retried = _attach(_staged(bucket))                 # adopted on the retry
    assert retried.code == al.RECEIPT_ATTACHED


def test_an_unknown_outcome_is_uncertain_never_nothing_written(store, notes, monkeypatch):
    db, bucket = store

    def _refuse(info):
        raise RuntimeError("connection reset")

    db.add_commit_hook(_refuse)
    monkeypatch.setattr(al, "get_transaction_strict",
                        mock.Mock(side_effect=[_entry(), RuntimeError("unreadable")]))
    out = _attach(_staged(bucket))
    assert out.code == al.RECEIPT_UNCERTAIN
    assert out.errors == [al.concurrency.WRITE_OUTCOME_UNCERTAIN_ERROR]


def test_the_door_never_deletes():
    """Neither the door nor the helpers it calls name a delete: a replaced
    receipt, an object left unreferenced — all KEPT."""
    for fn in (al.attach_uploaded_receipt, al.receipt_object_name,
               al.receipt_content_type, al.get_receipt_bytes,
               al.receipt_object_md5, al._receipt_state):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != "delete", fn.__name__
            if isinstance(node, ast.Name):
                assert "delete" not in node.id, fn.__name__


# ── The readers ───────────────────────────────────────────────────────────


def test_get_receipt_bytes_checks_size_first_and_the_md5_after(store, monkeypatch):
    _db, bucket = store
    bucket.put(OLD, PDF)
    entry = _entry(id=TX, receipt_storage_path=OLD, receipt_md5=_md5(PDF),
                   receipt_file_size=len(PDF))
    assert al.get_receipt_bytes(entry) == (PDF, "")
    assert al.get_receipt_bytes(_entry()) == (None, "no_receipt")
    huge = dict(entry, receipt_file_size=al.MAX_RECEIPT_SIZE + 1)
    downloads = []
    monkeypatch.setattr(type(bucket.blob(OLD)), "download_as_bytes",
                        lambda self, **kw: downloads.append(1) or PDF)
    assert al.get_receipt_bytes(huge) == (None, "too_large") and downloads == []
    monkeypatch.undo()
    monkeypatch.setattr(document_model.storage, "bucket", lambda: bucket)
    assert al.get_receipt_bytes(dict(entry, receipt_md5=_md5(PDF2))) == (
        None, "receipt_changed")
    gone = dict(entry, receipt_storage_path="users/u1/administration/t1/absent.pdf")
    assert al.get_receipt_bytes(gone) == (None, "download_failed")


def test_the_object_name_holds_ids_only():
    assert al.receipt_object_name(UID, TX, OBJ, ".msg") == (
        f"users/{UID}/administration/{TX}/pieces/{OBJ}.msg")
    for bad in ((UID, "a/b", OBJ, ".pdf"), (UID, TX, "nope", ".pdf"),
                (UID, TX, OBJ, ".exe")):
        with pytest.raises(ValueError):
            al.receipt_object_name(*bad)
