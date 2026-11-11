"""models/full_reads.stream_all — its rows, and its fast path (2026-11-11).

The bench is the shared fake Firestore (``tests/_fake_firestore.py``): the
REAL client and its real ``DocumentSnapshot`` over an in-memory server, so
the fast path pinned here is the one production runs. A dependency bump
that drops the snapshot's private ``_data`` fails here, in CI, instead of
slowing every statement in production. Every value is synthetic.
"""

import os
import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import pytest
from google.cloud import firestore
from google.cloud.firestore_v1.base_document import DocumentSnapshot

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import full_reads

from tests._fake_firestore import FakeFirestore  # noqa: E402

UTC = timezone.utc
COLLECTION = "admin_transactions"

DOCS = {
    "a1": {"kind": "dépense", "amount": 11498, "date": datetime(2031, 5, 2, tzinfo=UTC),
           "split": [{"category": "loyer", "net_amount": 10000}],
           "revisions": [{"at": datetime(2031, 5, 3, tzinfo=UTC),
                          "fields": {"amount": 11000, "memo": None}}],
           "invoice_id": None},
    "a2": {"id": "stale-id", "kind": "apport", "amount": 2500, "cleared": True},
    "a3": {},
}


@pytest.fixture
def fake():
    f = FakeFirestore()
    f.seed_collection(COLLECTION, DOCS)
    return f


def _read_twice(fake, between=None):
    """Two whole-collection reads in ONE read-only transaction, *between*
    run on the first read's rows before the second read."""
    out = {}

    @firestore.transactional
    def body(txn):
        out["first"] = full_reads.stream_all(fake.collection(COLLECTION), transaction=txn)
        if between is not None:
            between(out["first"])
        out["second"] = full_reads.stream_all(fake.collection(COLLECTION), transaction=txn)

    body(fake.transaction(read_only=True))
    return out["first"], out["second"]


def _by_id(rows):
    return {r["id"]: r for r in rows}


def _as_before(fake):
    """The rows as the reader built them before 2026-11-11, from to_dict()."""
    return {s.id: {**(s.to_dict() or {}), "id": s.id}
            for s in fake.collection(COLLECTION).stream()}


def test_the_rows_are_the_fields_to_dict_returns_and_the_documents_own_id(fake):
    rows, _ = _read_twice(fake)
    assert _by_id(rows) == _as_before(fake)
    assert _by_id(rows)["a2"]["id"] == "a2"                 # never the stored « id »
    assert _by_id(rows)["a3"] == {"id": "a3"}
    assert _by_id(rows)["a1"]["revisions"][0]["fields"] == {"amount": 11000, "memo": None}


def test_the_fast_path_never_deep_copies_through_to_dict(fake, monkeypatch):
    expected = _as_before(fake)

    def refused(self):
        raise AssertionError("stream_all called to_dict(): the fast path is lost")

    monkeypatch.setattr(DocumentSnapshot, "to_dict", refused)
    rows, _ = _read_twice(fake)
    assert _by_id(rows) == expected


def test_a_row_is_its_own_object_graph(fake):
    """What the fast path must never allow: a change to one read's row
    showing in another read, through an object the two share."""
    def damage(rows):
        row = _by_id(rows)["a1"]
        row["amount"] = 0
        row["split"][0]["net_amount"] = 0
        row["revisions"][0]["fields"]["amount"] = 0
        row["revisions"].append({"fields": {}})

    first, second = _read_twice(fake, between=damage)
    assert _by_id(second) == _as_before(fake)
    assert _by_id(first)["a1"]["split"] is not _by_id(second)["a1"]["split"]


class _Query:
    """A query whose snapshots are stand-ins (a library without ``_data``)."""

    def __init__(self, snaps):
        self.snaps = snaps

    def limit(self, n):
        return self

    def stream(self, transaction=None):
        return iter(self.snaps)


def _snap(id_, data, *, exists=True, **attrs):
    return SimpleNamespace(id=id_, exists=exists,
                           to_dict=lambda: dict(data) if exists else None, **attrs)


@pytest.mark.parametrize("attrs", [{}, {"_data": None}, {"_data": ("not", "a", "dict")}])
def test_without_a_dict_data_the_reader_falls_back_to_to_dict(attrs):
    snaps = [_snap("x1", {"amount": 100}, **attrs),
             _snap("x2", {}, exists=False, **attrs)]
    rows = full_reads.stream_all(_Query(snaps), transaction=None)
    assert rows == [{"amount": 100, "id": "x1"}, {"id": "x2"}]


def test_a_snapshot_that_does_not_exist_has_no_fields_even_with_data():
    """A snapshot keeps the data it was given whatever ``exists`` says
    (``DocumentSnapshot.__init__``): ``exists`` decides, as ``to_dict()``'s
    None did."""
    snaps = [_snap("x3", {"amount": 1}, exists=False, _data={"amount": 1})]
    assert full_reads.stream_all(_Query(snaps), transaction=None) == [{"id": "x3"}]
