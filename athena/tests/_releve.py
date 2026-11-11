"""Tests: a reconciliation completes only with its bank statement.

Since 2026-11-10 (``models/releve_conciliation``, art. 32) both registers'
``complete_reconciliation`` refuse a reconciliation that carries no
statement. A test that completes one — to build its world, or to test the
completion itself — attaches a SYNTHETIC statement first, through the
register model's own store (``model.db``: the per-file fakes patch it
there), with :func:`completer`. The upload path itself is tested in
``tests/test_releve_conciliation.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

from models import releve_conciliation as rc
from tests._fake_gcs import FakeBucket

#: The bytes of a synthetic statement: a PDF header the sniff accepts.
PDF = b"%PDF-1.7 " + b"releve de compte fictif " * 20

#: A statement id a test uses: a canonical UUIDv4, never minted.
STATEMENT_ID = "0b6c7f3e-2d4a-4e8b-9c1d-5a7e3f9b2c4d"


def _register(model) -> str:
    for register, collection in rc.REGISTERS.items():
        if model.RECONCILIATIONS_COLLECTION == collection:
            return register
    raise AssertionError(f"not a register model: {model!r}")


def joindre(model, rec_id: str, *, uid: str = "u1", bucket=None) -> dict:
    """Seed a synthetic statement on *rec_id* in *model*'s store — and its
    object in *bucket* when one is given; returns the fields written."""
    register = _register(model)
    fields = {
        "statement_id": STATEMENT_ID,
        "statement_storage_path": rc.storage_path(
            uid, register, rec_id, STATEMENT_ID, ".pdf"),
        "statement_file_type": "application/pdf",
        "statement_file_size": 1234,
        "statement_md5": "",
        "statement_attached_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    (model.db.collection(model.RECONCILIATIONS_COLLECTION)
     .document(rec_id).update(fields))
    if bucket is not None:
        bucket.put(fields["statement_storage_path"], PDF,
                   content_type="application/pdf")
    return fields


def completer(model, rec_id: str, cleared_tx_ids: list):
    """``model.complete_reconciliation``, its statement attached first —
    the record AND its object, which the completion checks before it
    freezes them (a bucket of its own, patched for the call)."""
    bucket = FakeBucket()
    joindre(model, rec_id, bucket=bucket)
    with mock.patch.object(rc.storage, "bucket", lambda: bucket):
        return model.complete_reconciliation(rec_id, cleared_tx_ids)
