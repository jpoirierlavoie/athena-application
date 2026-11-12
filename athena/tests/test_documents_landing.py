"""The Documents landing, one page at a time (2026-11-12).

The landing — /documents/ with no dossier chosen, the Bibliothèque tab —
streamed every document of the firm and sorted it in Python to show
fifteen (p50 4.2 s on 2026-11-11). It now reads the page it shows, newest
first, through ``models.document.list_documents_page`` (STRICT) and its
COUNT, both built by ``_landing_query``; the dossier picker reads four
fields per dossier (``models.dossier.list_dossiers_for_picker``).

Real routes, models and templates over the shared fake Firestore (the real
client, an in-memory server). Every name, number and date is synthetic, in
the fictional year 2031.
"""

import json
import os
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import models.doc_template as tpl
    import models.document as document_model
    import models.dossier as dossier_model
    import routes.doc_templates as rdt
    import routes.documents as rdocs

from flask import Flask  # noqa: E402

from pagination import PAGE_SIZE  # noqa: E402
from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc
UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"
_TEMPLATES = _ATHENA / "templates"
BASE = datetime(2031, 3, 1, 9, 0, tzinfo=UTC)

ERROR = "La liste des documents n'a pas pu être lue"
EMPTY = "Aucun document"


@pytest.fixture()
def bench(monkeypatch):
    modules = [m for n, m in sorted(sys.modules.items())
               if n.startswith("models.") and getattr(m, "db", None) is not None]
    fake = install(monkeypatch, *modules)
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
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return SimpleNamespace(client=client, fake=fake)


def _doc(i: int, *, created=..., category="pièce", **extra) -> dict:
    doc = {
        "id": f"doc{i:03d}", "dossier_id": "dos1", "dossier_file_number": "2031-001",
        "display_name": f"Pièce fictive {i:03d}", "filename": f"piece-{i:03d}.pdf",
        "file_type": "application/pdf", "file_size": 1000 + i, "category": category,
    }
    if created is ...:
        created = BASE + timedelta(hours=i)
    if created is not None:
        doc["created_at"] = created
    doc.update(extra)
    return doc


def _seed(fake, docs):
    for d in docs:
        fake.seed(f"documents/{d['id']}", d)


def _shown(html: str) -> list:
    return re.findall(r'href="/documents/(doc\d{3})"', html)


def _documents_read(fake, since: int) -> list:
    """Documents RETURNED by queries (not counted by aggregations)."""
    return [p for r in fake.reads[since:] if r.rpc == "run_query"
            for p in r.paths if p.startswith("documents/")]


def _next_vals(html: str):
    """The live « Page suivante » control's hx-vals, or None."""
    for m in re.finditer(r"<button type=\"button\"(?:(?!</button>).)*?</button>", html, re.S):
        tag = m.group(0)
        if 'aria-label="Page suivante"' in tag and "disabled" not in tag.split(">")[0]:
            vals = re.search(r"hx-vals='([^']+)'", tag)
            return json.loads(vals.group(1)) if vals else None
    return None


def _break_reads(monkeypatch, fake, collection: str, exc: Exception):
    """Make every query and aggregation on *collection* fail, as an outage or
    a missing index fails them in production."""
    server = fake._fake_server
    run_query, run_aggregation = server.run_query, server.run_aggregation_query

    def broken_query(request, metadata=None, **kwargs):
        if request["structured_query"]._pb.from_[0].collection_id == collection:
            raise exc
        return run_query(request, metadata=metadata, **kwargs)

    def broken_aggregation(request, metadata=None, **kwargs):
        sq = request["structured_aggregation_query"]._pb.structured_query
        if sq.from_[0].collection_id == collection:
            raise exc
        return run_aggregation(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", broken_query)
    monkeypatch.setattr(server, "run_aggregation_query", broken_aggregation)


# ══════════════════════════════════════════════════════════════════════
# 1. One page read, newest first
# ══════════════════════════════════════════════════════════════════════


def test_the_landing_reads_the_page_it_shows_not_the_collection(bench):
    _seed(bench.fake, [_doc(i) for i in range(40)])
    since = len(bench.fake.reads)
    html = bench.client.get("/documents/").get_data(as_text=True)
    assert _shown(html) == [f"doc{i:03d}" for i in range(39, 24, -1)]
    # limit + 1 documents (the extra one says a next page exists) — the old
    # landing streamed all forty.
    assert len(_documents_read(bench.fake, since)) == PAGE_SIZE + 1


def test_a_walk_lists_every_document_once_newest_first_ties_by_name(bench):
    """Documents sharing a creation instant are ordered by their name,
    descending, so the cursor resumes exactly where the page stopped."""
    docs = [_doc(i, created=BASE + timedelta(hours=i // 4)) for i in range(37)]
    _seed(bench.fake, docs)
    expected = [d["id"] for d in sorted(
        docs, key=lambda d: (d["created_at"], d["id"]), reverse=True)]
    html = bench.client.get("/documents/").get_data(as_text=True)
    seen, pages = _shown(html), 1
    vals = _next_vals(html)
    while vals is not None:
        assert "cursor" in vals and vals["cursor"], vals
        html = bench.client.get("/documents/", query_string=vals,
                                headers={"HX-Request": "true"}).get_data(as_text=True)
        seen += _shown(html)
        pages += 1
        vals = _next_vals(html)
    assert seen == expected
    assert pages == 3


def test_the_last_page_control_leaps_by_offset_in_the_same_order(bench):
    _seed(bench.fake, [_doc(i) for i in range(40)])
    html = bench.client.get("/documents/", query_string={"page": 3, "cursor": "", "trail": ""},
                            headers={"HX-Request": "true"}).get_data(as_text=True)
    assert _shown(html) == [f"doc{i:03d}" for i in range(9, -1, -1)]
    assert "Page 3 / 3" in html


def test_the_category_filter_pages_in_firestore(bench):
    docs = [_doc(i, category="jugement" if i % 3 == 0 else "pièce") for i in range(60)]
    _seed(bench.fake, docs)
    since = len(bench.fake.reads)
    html = bench.client.get("/documents/", query_string={"category": "jugement"}).get_data(as_text=True)
    jugements = [d["id"] for d in reversed(docs) if d["category"] == "jugement"]
    assert _shown(html) == jugements[:PAGE_SIZE]
    assert len(_documents_read(bench.fake, since)) == PAGE_SIZE + 1
    assert "Page 1 / 2" in html


# ══════════════════════════════════════════════════════════════════════
# 2. A failed read is never « aucun document »
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("exc", [
    gexc.FailedPrecondition("The query requires an index."),   # index building
    RuntimeError("firestore indisponible"),
], ids=["index", "panne"])
@pytest.mark.parametrize("htmx", [False, True], ids=["page", "htmx"])
def test_a_failed_page_read_says_so_at_200_never_the_empty_state(bench, monkeypatch, exc, htmx):
    _seed(bench.fake, [_doc(i) for i in range(5)])
    _break_reads(monkeypatch, bench.fake, "documents", exc)
    headers = {"HX-Request": "true"} if htmx else {}
    resp = bench.client.get("/documents/", query_string={"category": "pièce"}, headers=headers)
    assert resp.status_code == 200          # htmx swaps only 2xx
    html = resp.get_data(as_text=True)
    assert ERROR in html
    assert 'role="alert"' in html
    assert EMPTY not in html
    assert 'href="/documents/?category=pi%C3%A8ce"' in html     # « Réessayer »


def test_an_empty_firm_still_reads_aucun_document(bench):
    html = bench.client.get("/documents/").get_data(as_text=True)
    assert EMPTY in html and ERROR not in html


# ══════════════════════════════════════════════════════════════════════
# 3. Nothing dropped in silence
# ══════════════════════════════════════════════════════════════════════


def test_documents_without_created_at_are_announced_and_the_name_sort_shows_them(bench):
    """An order excludes a document that lacks the ordered field. The model
    stamps created_at on every creation, so only a write made outside it can
    lack one — and the landing then says how many it cannot list."""
    docs = [_doc(i) for i in range(20)]
    docs += [_doc(90, created=None, display_name="A sans date"),
             _doc(91, created=None, display_name="A sans date bis")]
    _seed(bench.fake, docs)
    html = bench.client.get("/documents/").get_data(as_text=True)
    assert ("2 documents sans date de création n'apparaissent pas dans ce "
            "classement — le tri « Nom » les montre.") in html
    assert "doc090" not in _shown(html)
    by_name = bench.client.get("/documents/", query_string={"sort": "name"}).get_data(as_text=True)
    assert _shown(by_name)[:2] == ["doc090", "doc091"]


def test_a_complete_set_shows_no_notice(bench):
    _seed(bench.fake, [_doc(i) for i in range(3)])
    html = bench.client.get("/documents/").get_data(as_text=True)
    assert "sans date de création" not in html


def test_one_undated_document_is_announced_in_the_singular(bench):
    _seed(bench.fake, [_doc(1), _doc(2, created=None)])
    html = bench.client.get("/documents/").get_data(as_text=True)
    assert ("1 document sans date de création n'apparaît pas dans ce "
            "classement — le tri « Nom » le montre.") in html


# ══════════════════════════════════════════════════════════════════════
# 4. The whole read stays for the search box and the other sorts
# ══════════════════════════════════════════════════════════════════════


def test_search_and_the_name_and_size_sorts_keep_the_whole_read(bench):
    _seed(bench.fake, [_doc(i) for i in range(20)])
    found = bench.client.get("/documents/", query_string={"q": "fictive 007"}).get_data(as_text=True)
    assert _shown(found) == ["doc007"]
    by_size = bench.client.get("/documents/", query_string={"sort": "size"}).get_data(as_text=True)
    assert _shown(by_size)[:3] == ["doc019", "doc018", "doc017"]


def test_the_search_box_replaces_its_request_in_flight(bench):
    html = bench.client.get("/documents/").get_data(as_text=True)
    box = re.search(r'<input type="search"[^>]*>', html).group(0)
    assert 'hx-sync="this:replace"' in box


# ══════════════════════════════════════════════════════════════════════
# 5. The dossier picker
# ══════════════════════════════════════════════════════════════════════


def _dossier(i: int, opened: datetime, created: datetime) -> dict:
    return {"id": f"dos{i}", "file_number": f"2031-00{i}", "title": f"Dossier fictif {i}",
            "status": "actif", "opened_date": opened, "created_at": created,
            "clients": [{"id": "p1", "name": "Client fictif"}], "notes_internes": "x" * 500}


def test_the_picker_lists_every_dossier_newest_opened_first_from_a_projection(bench, monkeypatch):
    day = datetime(2031, 2, 1, tzinfo=UTC)
    for d in (_dossier(1, day, day + timedelta(hours=1)),
              _dossier(2, day + timedelta(days=3), day + timedelta(days=3)),
              _dossier(3, day, day + timedelta(hours=5))):
        bench.fake.seed(f"dossiers/{d['id']}", d)
    masks = []
    server = bench.fake._fake_server
    run_query = server.run_query

    def spy(request, metadata=None, **kwargs):
        sq = request["structured_query"]._pb
        if sq.from_[0].collection_id == "dossiers":
            masks.append(sorted(f.field_path for f in sq.select.fields)
                         if sq.HasField("select") else None)
        return run_query(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", spy)
    html = bench.client.get("/documents/").get_data(as_text=True)
    options = re.findall(r'<option value="(dos\d)"[^>]*>([^<]*)</option>', html)
    assert options == [("dos2", "2031-002 — Dossier fictif 2"),
                       ("dos3", "2031-003 — Dossier fictif 3"),
                       ("dos1", "2031-001 — Dossier fictif 1")]
    # Four small fields, never the whole dossier.
    assert masks == [sorted(dossier_model.PICKER_FIELDS)]


def test_a_failed_picker_read_says_unavailable_and_keeps_the_choice(bench, monkeypatch):
    _seed(bench.fake, [_doc(i) for i in range(3)])
    _break_reads(monkeypatch, bench.fake, "dossiers", RuntimeError("panne"))
    resp = bench.client.get("/documents/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert '<option value="" disabled>Liste des dossiers indisponible</option>' in html
    assert _shown(html) == ["doc002", "doc001", "doc000"]


# ══════════════════════════════════════════════════════════════════════
# 6. Indexes (Change Impact item 7)
# ══════════════════════════════════════════════════════════════════════


def _inventory() -> dict:
    root = _ATHENA.parent
    return json.loads((root / "firestore.indexes.json").read_text(encoding="utf-8"))


def _shape(query) -> tuple:
    """(equality fields, [(order field, direction)]) of a real query."""
    sq = query._to_protobuf()._pb
    eq = []
    if sq.HasField("where"):
        f = sq.where
        if f.HasField("field_filter"):
            eq.append(f.field_filter.field.field_path)
        else:
            eq += [c.field_filter.field.field_path for c in f.composite_filter.filters]
    orders = [(o.field.field_path, "DESCENDING" if o.direction == 2 else "ASCENDING")
              for o in sq.order_by]
    return eq, orders


def test_the_category_query_has_its_composite_index_and_the_plain_one_needs_none(bench):
    eq, orders = _shape(document_model._landing_query("pièce"))
    assert eq == ["category"]
    assert orders == [("created_at", "DESCENDING"), ("__name__", "DESCENDING")]
    composites = [
        [(f["fieldPath"], f.get("order")) for f in idx["fields"]]
        for idx in _inventory()["indexes"] if idx["collectionGroup"] == "documents"
    ]
    # Equality first, then the order; the implicit __name__ trails in the
    # direction of the last field, as the query asks.
    assert [("category", "ASCENDING"), ("created_at", "DESCENDING")] in composites

    eq, orders = _shape(document_model._landing_query(None))
    # No filter and one field ordered, __name__ in the same direction: the
    # automatic single-field index serves it…
    assert eq == [] and orders == [("created_at", "DESCENDING"), ("__name__", "DESCENDING")]
    # …provided no override exempts documents.created_at from indexing.
    assert not [o for o in _inventory().get("fieldOverrides", [])
                if o.get("collectionGroup") == "documents"
                and o.get("fieldPath") == "created_at"]


def test_an_unknown_category_is_ignored_never_queried(bench):
    eq, _ = _shape(document_model._landing_query("inconnue"))
    assert eq == []
