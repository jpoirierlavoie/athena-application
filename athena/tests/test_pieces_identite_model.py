"""Proofs of identity — the model (``models/piece_identite.py``).

Everything runs on the fake Firestore (``tests/_fake_firestore.py``: the
real client, a fake server) and the fake Cloud Storage
(``tests/_fake_gcs.py``: bytes, generations, create-only sessions, 412s):
what is asserted is what the STORE and the BUCKET hold afterwards.

Families:

1. the constants and their derived labels (one source per list);
2. the pure checks — names refused, never stripped; extensions
   lower-cased; the staging name and the stored path re-checked against
   the ONE builder each, foreign shapes refused both ways;
3. the readers — strict, fail-open for display only, capped and honest
   about truncation, unaddressable ids absent without a read;
4. the session — every check before a byte moves, create-only, the
   whitelist's content type;
5. the filing — sniff, real size, the cap at both ends, replays, adoption
   never erased, rollbacks bound to THIS call's generation, an unknown
   outcome keeping both objects, an adopted or occupying object left
   behind named once for an operator;
6. the rename — compare-and-set, partial, refused never altered;
7. the deletion — by generation, exactly once, incomplete then completed;
   each failure line beside every outcome it can precede; the etag
   guarding the erase, not the record delete;
8. « Consulter » — the signed URL's parameters, a missing object, a refused
   path;
9. the contact document and its CTag are never touched.
"""

import logging
import os
import pathlib
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import models.document as document_model
    import models.partie as partie_model
    import models.piece_identite as pi
    import models.upload_ticket as upload_ticket
    from models import concurrency, provenance

from google.api_core import exceptions as gexc  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBlob, FakeBucket  # noqa: E402
from utils import storage_identity  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
UID = "u1"
OTHER_UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"   # the shape of a Firebase uid
PID = "p1"
PROOF = "3f2b8c1e-9d4a-4e6b-8f7a-1c2d3e4f5a6b"
PROOF2 = "9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d"
UUID1 = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"   # a version-1 uuid
ORIGIN = "https://athena.example"

PDF = b"%PDF-1.7\n" + b"permis de conduire " * 20
JPEG = b"\xff\xd8\xff\xe0" + b"photo " * 40
PNG = b"\x89PNG\r\n\x1a\n" + b"image " * 40
TIFF = b"II*\x00" + b"scan " * 40
HTML = b"<html><body>pas une image</body></html>"
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 40

CONTACT = {
    "id": PID, "name": "Contact de test", "contact_role": "client",
    "created_at": T0, "updated_at": T0, "etag": "pe1",
}
RECORD_KEYS = {
    "id", "partie_id", "display_name", "file_type", "file_size",
    "storage_path", "created_at", "updated_at", "etag", "created_via",
    "updated_via",
}


# ── Fixtures and helpers ─────────────────────────────────────────────────


@pytest.fixture
def world(monkeypatch):
    fake = install(monkeypatch, pi, partie_model)
    bucket = FakeBucket()
    monkeypatch.setattr(pi.storage, "bucket", lambda: bucket)
    fake.seed(f"parties/{PID}", dict(CONTACT))
    fake.seed("dav_sync/parties", {"ctag": "c0", "sync_token": "c0"})
    fake.reset_logs()
    return types.SimpleNamespace(fake=fake, bucket=bucket)


@pytest.fixture
def logged(monkeypatch):
    """Every « id proof: … » line, as (message, fields). Each is logged
    WITHOUT a traceback: a Storage error's text names the object, and the
    lines carry ids and the exception's type only."""
    seen: list = []

    def _record(message, *, exc_info=True, **fields):
        assert exc_info is False, message
        seen.append((message, fields))

    monkeypatch.setattr(pi, "log_unexpected", _record)
    return seen


@pytest.fixture
def deletes(monkeypatch):
    """Every object delete, as (name, the blob's generation AT CALL TIME,
    if_generation_match). The fake does not model versioning; what makes a
    real erase permanent is that ``Blob.delete`` hands
    ``generation=self.generation`` to ``delete_blob`` (google-cloud-storage
    3.15.0, blob.py:851-861), so the generation the blob carries is what is
    pinned here."""
    seen: list = []
    real = FakeBlob.delete

    def _delete(self, client=None, if_generation_match=None, **kwargs):
        seen.append((self.name, self.generation, if_generation_match))
        return real(self, client=client, if_generation_match=if_generation_match,
                    **kwargs)

    monkeypatch.setattr(FakeBlob, "delete", _delete)
    return seen


def _final(proof_id=PROOF, ext=".pdf", uid=UID, partie_id=PID) -> str:
    return f"users/{uid}/parties/{partie_id}/pieces_identite/{proof_id}{ext}"


def _staging(proof_id=PROOF, ext=".pdf", uid=UID) -> str:
    return f"staging/{uid}/identite/{proof_id}/upload{ext}"


def _records(world, partie_id=PID) -> dict:
    return world.fake.peek_collection(f"parties/{partie_id}/pieces_identite")


def _upload(world, name="Permis.pdf", data=PDF, *, partie_id=PID):
    out = pi.open_proof_upload(partie_id, name, len(data), UID, origin=ORIGIN)
    assert out.code == pi.CODE_OK, out
    world.bucket.complete_session(out.doc["url"], data)
    return out.doc["objet"], out.doc["proof_id"]


def _file(world, name="Permis.pdf", data=PDF, *, partie_id=PID):
    objet, proof_id = _upload(world, name, data, partie_id=partie_id)
    return pi.file_staged_proof(objet, partie_id, name, UID), objet, proof_id


def _seed_proof(world, proof_id=PROOF, *, ext=".pdf", data=PDF,
                display="Permis de conduire", path=None, created_at=T0,
                partie_id=PID):
    """A filed proof planted directly: its record and (unless *data* is
    None) its object."""
    path = path if path is not None else _final(proof_id, ext, partie_id=partie_id)
    obj = (world.bucket.put(path, data, content_type=pi.MIME_TYPES.get(ext))
           if data is not None else None)
    record = {
        "id": proof_id, "partie_id": partie_id, "display_name": display,
        "file_type": pi.MIME_TYPES.get(ext, "application/pdf"),
        "file_size": len(data or b""), "storage_path": path,
        "created_at": created_at, "updated_at": created_at,
        "etag": f"e-{proof_id[:8]}", "created_via": "web",
        "updated_via": "web",
    }
    world.fake.seed(f"parties/{partie_id}/pieces_identite/{proof_id}", record)
    return record, obj


def _seed_many(world, n: int, *, partie_id=PID) -> list[str]:
    ids = []
    for i in range(n):
        proof_id = str(uuid.uuid4())
        _seed_proof(world, proof_id, created_at=T0 + timedelta(minutes=i),
                    partie_id=partie_id)
        ids.append(proof_id)
    return ids


def _break_commit(monkeypatch, fake, *, kind: str, apply: bool,
                  reads_fail: bool = False, times: int = 1):
    """Make the next *times* commits that carry a *kind* op on a proof
    record RAISE — after being applied (``apply=True``: the commit landed,
    its answer was lost) or without being applied. ``reads_fail`` makes
    every read fail from that moment on (``state["down"] = False`` lifts
    it): the read-back that would settle the outcome fails too."""
    server = fake._fake_server
    real_commit = server.commit
    real_get = server.batch_get_documents
    state = {"left": times, "down": False}

    def _targets(request) -> bool:
        for write in request.get("writes") or []:
            pb = getattr(write, "_pb", write)
            path = server.rel(server._write_name(pb))
            if server._op_kind(pb) == kind and f"/{pi.SUBCOLLECTION}/" in path:
                return True
        return False

    def _commit(request, metadata=None, **kwargs):
        if state["left"] and _targets(request):
            state["left"] -= 1
            if apply:
                real_commit(request, metadata=metadata, **kwargs)
            if reads_fail:
                state["down"] = True
            raise gexc.ServiceUnavailable("commit answer lost")
        return real_commit(request, metadata=metadata, **kwargs)

    def _get(request, metadata=None, **kwargs):
        if state["down"]:
            raise gexc.ServiceUnavailable("store unavailable")
        return real_get(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "commit", _commit)
    monkeypatch.setattr(server, "batch_get_documents", _get)
    return state


def _boom(*_a, **_kw):
    raise gexc.ServiceUnavailable("store unavailable")


def _assert_ids_only(logged) -> None:
    """« Do not log PII »: ids, codes and exception types — never a name, a
    file name, a path or a URL."""
    for message, fields in logged:
        assert message.startswith("id proof: "), message
        for value in fields.values():
            text = str(value)
            assert "/" not in text and "Permis" not in text, (message, fields)


# ══════════════════════════════════════════════════════════════════════
# 1. The constants — one source, labels derived and pinned
# ══════════════════════════════════════════════════════════════════════


def test_the_labels_derive_from_the_constants():
    assert pi.EXTENSIONS == (".pdf", ".jpg", ".jpeg", ".png")
    assert all(e == e.lower() and e.startswith(".") for e in pi.EXTENSIONS)
    assert pi.FORMATS_LABEL == "PDF, JPG ou PNG"
    assert pi.MAX_SIZE == 20 * 1024 * 1024 and pi.MAX_SIZE % (1024 * 1024) == 0
    assert pi.MAX_SIZE_LABEL == "20 Mo"
    assert pi.MAX_PER_CONTACT == 20
    assert pi.DISPLAY_NAME_MAX == document_model.DISPLAY_NAME_MAX == 300
    assert pi.SIGNED_URL_MINUTES == 15
    assert pi.ACCEPT.split(",") == list(pi.EXTENSIONS) + [
        "application/pdf", "image/jpeg", "image/png"]
    # Every sentence that states a number or a list formats it from its
    # constant — a change of the constant can never leave a stale text.
    assert pi.UPLOAD_HINT == "PDF, JPG ou PNG — 20 Mo au plus par fichier."
    assert pi.TYPE_REFUSED.endswith(f"Formats acceptés : {pi.FORMATS_LABEL}.")
    assert pi.MAX_SIZE_LABEL in pi.SIZE_REFUSED
    assert f"déjà {pi.MAX_PER_CONTACT} preuves" in pi.CAP_REACHED
    assert f"seules les {pi.MAX_PER_CONTACT} premières" in pi.TRUNCATED_NOTICE
    assert pi.CONTACT_NOT_FOUND == partie_model.PARTIE_NOT_FOUND


def test_the_formats_label_is_derived_never_restated():
    assert pi._formats_label({".pdf": "application/pdf"}) == "PDF"
    assert pi._formats_label({
        ".pdf": "application/pdf", ".tif": "image/tiff", ".tiff": "image/tiff",
    }) == "PDF ou TIF"
    assert pi._formats_label(pi.MIME_TYPES) == pi.FORMATS_LABEL


@pytest.mark.parametrize("ext, magic", [
    (".pdf", PDF), (".jpg", JPEG), (".jpeg", JPEG), (".png", PNG),
])
def test_each_allowed_type_is_what_the_sniff_recognises(ext, magic):
    assert document_model._sniff_header(magic[:512], ext) == pi.MIME_TYPES[ext]
    assert pi.MIME_TYPES[ext] in document_model.ALLOWED_MIME_TYPES


def test_the_code_vocabularies_are_closed():
    codes = [v for k, v in vars(pi).items() if k.startswith("CODE_")]
    assert len(codes) == len(set(codes))
    for vocabulary in (pi.SESSION_CODES, pi.FILING_CODES, pi.RENAME_CODES,
                       pi.DELETE_CODES):
        assert len(vocabulary) == len(set(vocabulary))
        assert set(vocabulary) <= set(codes)
    assert pi.CODE_SUPPRIME in pi.DELETE_CODES and pi.CODE_DEJA in pi.DELETE_CODES


def test_the_docstring_holds_the_record_shape():
    for key in RECORD_KEYS:
        assert f'"{key}"' in pi.__doc__, key
    assert "original_filename" in pi.__doc__   # said ABSENT, with its reason
    for declared in ("Erase, never delete", "unknown filing outcome KEEPS",
                     "No fail-open single reader", "Capped",
                     "Writers return (doc, errors, code)",
                     "The deletion's etag guards its erase"):
        assert declared in pi.__doc__, declared


def _flat(text: str) -> str:
    """*text* with its line breaks and indents folded to single spaces."""
    return " ".join(text.split())


def test_the_docstring_says_what_the_uncertain_re_post_settles_and_what_not():
    """The page's one re-post settles an « incertain » filing only when the
    store answers it; otherwise the object may stay with no record, and the
    docstring names the operator's step and the lines that carry the ids —
    never « the re-post answers rejoue or verse » (review of lot 2, filing
    F1 and F2)."""
    doc = _flat(pi.__doc__)
    assert "that re-post settles the outcome only when the store answers it" in doc
    assert ("an operator erases it by hand, from the ids of « id proof: "
            "filing uncertain »") in doc
    for line in ("id proof: adopted object left", "id proof: object occupied"):
        assert f"« {line} »" in doc, line
    assert "« id proof: adopted object left »" in _flat(pi.file_staged_proof.__doc__)


def test_the_deletion_declares_that_its_etag_guards_the_erase_only():
    doc = _flat(pi.delete_proof.__doc__)
    assert "guards the ERASE, not the record delete" in doc
    assert "the record delete checks presence only, on purpose" in doc


# ══════════════════════════════════════════════════════════════════════
# 2. The pure checks
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("name, ext", [
    ("SCAN.PDF", ".pdf"), ("PHOTO.JPG", ".jpg"), ("a.JpEg", ".jpeg"),
    ("carte.png", ".png"), ("a.b.pdf", ".pdf"), ("sans", ""), ("", ""),
    (None, ""),
])
def test_the_extension_is_lower_cased(name, ext):
    assert pi.file_extension(name) == ext


@pytest.mark.parametrize("name, stem", [
    ("SCAN.PDF", "SCAN"), ("  Permis de conduire .jpg", "Permis de conduire"),
    ("a.b.pdf", "a.b"), ("sans", "sans"), (None, ""),
])
def test_the_default_display_name_is_the_stem(name, stem):
    assert pi.default_display_name(name) == stem


@pytest.mark.parametrize("value", [
    "Permis de conduire", "Passeport — recto", "Extrait REQ",
    "a < b", "x" * 300,
])
def test_a_valid_display_name_is_accepted_exactly(value):
    assert pi.display_name_errors(value) == []


@pytest.mark.parametrize("value, error", [
    ("", "Nom du document : requis."),
    ("   ", "Nom du document : requis."),
    (None, "Nom du document : requis."),
    ("x" * 301, "Nom du document : 300 caractères au plus."),
    ("Permis\tde conduire", "Nom du document : caractères de contrôle refusés."),
    ("Permis\x85", "Nom du document : caractères de contrôle refusés."),
    ("Permis\x7f", "Nom du document : caractères de contrôle refusés."),
    ("<b>Permis</b>",
     document_model.TEXT_CHEVRONS_ERROR.format(label="Nom du document")),
    ("x<>y", document_model.TEXT_CHEVRONS_ERROR.format(label="Nom du document")),
])
def test_a_display_name_is_refused_never_altered(value, error):
    assert pi.display_name_errors(value) == [error]


@pytest.mark.parametrize("name", [
    "SCAN.PDF", "PHOTO.JPG", "photo.jpeg", "carte.PNG", "Permis de conduire.pdf",
])
def test_file_names_of_every_case_are_accepted(name):
    assert pi.file_name_errors(name) == []


@pytest.mark.parametrize("name, error", [
    ("scan.tiff", pi.TYPE_REFUSED),
    ("photo.heic", pi.TYPE_REFUSED),
    ("lettre.docx", pi.TYPE_REFUSED),
    ("sans-extension", pi.TYPE_REFUSED),
    ("", "Nom du fichier : requis."),
    (None, "Nom du fichier : requis."),
    (".pdf", "Nom du fichier : requis."),
    ("a\n.pdf", "Nom du fichier : caractères de contrôle refusés."),
    ("<b>x</b>.pdf",
     document_model.TEXT_CHEVRONS_ERROR.format(label="Nom du fichier")),
    ("x" * 301 + ".pdf", "Nom du fichier : 300 caractères au plus."),
])
def test_file_names_are_refused_before_any_byte(name, error):
    assert pi.file_name_errors(name) == [error]


def test_the_builders_name_ids_only_and_guard_the_uid():
    assert pi.staging_object(UID, PROOF, ".pdf") == _staging()
    assert pi.storage_path(UID, PID, PROOF, ".jpg") == _final(ext=".jpg")
    for bad_uid in ("unknown", "", None, "a/b", "u 1"):
        with pytest.raises(storage_identity.InvalidStorageUid):
            pi.staging_object(bad_uid, PROOF, ".pdf")
        with pytest.raises(storage_identity.InvalidStorageUid):
            pi.storage_path(bad_uid, PID, PROOF, ".pdf")
    for args in ((PROOF.upper(), ".pdf"), (UUID1, ".pdf"), (PROOF, ".PDF"),
                 (PROOF, ".tiff"), ("x", ".pdf")):
        with pytest.raises(ValueError):
            pi.staging_object(UID, *args)
    for partie_id in ("", "a/b", "..", "__x__"):
        with pytest.raises(ValueError):
            pi.storage_path(UID, partie_id, PROOF, ".pdf")


def test_the_identity_staging_shape_is_parsed_under_the_request_uid():
    assert pi.parse_staging_object(_staging(), UID) == (PROOF, ".pdf")
    assert pi.parse_staging_object(_staging(ext=".jpeg"), UID) == (PROOF, ".jpeg")
    # Never under an invalid request uid, even when the name matches it.
    assert pi.parse_staging_object(_staging(uid="unknown"), "unknown") is None


FOREIGN_SHAPES = [
    f"staging/{UID}/{PROOF}/a.pdf",                     # documents, receipts
    f"staging/{UID}/mcp/{PROOF}/upload.pdf",            # the connector's ticket
    f"staging/{UID}/exports/{PROOF}/lot.zip",           # a zip export
    f"staging/{UID}/exports/{PROOF}/upload.pdf",
    f"staging/autre/identite/{PROOF}/upload.pdf",       # another uid's
    f"staging/{UID}/identite/{PROOF.upper()}/upload.pdf",
    f"staging/{UID}/identite/{UUID1}/upload.pdf",
    f"staging/{UID}/identite/{PROOF}/photo.pdf",
    f"staging/{UID}/identite/{PROOF}/uploadupload.pdf",
    f"staging/{UID}/identite/{PROOF}/upload.tiff",
    f"staging/{UID}/identite/{PROOF}/upload.PDF",       # never upper-case
    f"staging/{UID}/identite/{PROOF}/upload",
    f"staging/{UID}/identite/{PROOF}/upload.pdf/x",     # six segments
    f"/staging/{UID}/identite/{PROOF}/upload.pdf",
    f"users/{UID}/parties/{PID}/pieces_identite/{PROOF}.pdf",
    "",
]


@pytest.mark.parametrize("objet", FOREIGN_SHAPES)
def test_every_foreign_staging_shape_is_refused_and_never_touched(world, objet):
    assert pi.parse_staging_object(objet, UID) is None
    if objet:
        world.bucket.put(objet, PDF)
    before = dict(world.bucket.objects)
    out = pi.file_staged_proof(objet, PID, "a.pdf", UID)
    assert out == pi.Outcome(None, [pi.REQUEST_INVALID], pi.CODE_FORME)
    assert world.bucket.objects == before          # no delete, no copy
    assert _records(world) == {}


def test_a_non_string_staging_name_is_refused():
    assert pi.parse_staging_object(None, UID) is None
    assert pi.parse_staging_object(["staging"], UID) is None


def test_the_connector_finalizer_refuses_the_identity_shape():
    """Both ways: the connector's ticket can never name a proof's staging
    object (its third segment must be ``mcp``)."""
    with pytest.raises(ValueError):
        upload_ticket._staging_name({
            "id": PROOF, "staging_object": _staging(), "ext": ".pdf"})


def _record(path, proof_id=PROOF):
    return {"id": proof_id, "partie_id": PID, "storage_path": path}


def test_the_stored_path_is_checked_by_shape_never_against_the_session():
    assert pi.checked_path(_record(_final()), PID) == _final()
    # A path under ANOTHER valid uid is accepted: a changed Firebase uid
    # must never make every proof undeletable.
    other = _final(uid=OTHER_UID, ext=".png")
    assert pi.checked_path(_record(other), PID) == other


@pytest.mark.parametrize("path", [
    f"users/unknown/parties/{PID}/pieces_identite/{PROOF}.pdf",
    f"users/a b/parties/{PID}/pieces_identite/{PROOF}.pdf",
    f"users//parties/{PID}/pieces_identite/{PROOF}.pdf",
    f"users/{UID}/parties/p2/pieces_identite/{PROOF}.pdf",     # another contact
    f"users/{UID}/parties/{PID}/pieces_identite/{PROOF2}.pdf",  # another proof
    f"users/{UID}/parties/{PID}/pieces_identite/{PROOF}.tiff",
    f"users/{UID}/parties/{PID}/pieces_identite/{PROOF}.PDF",
    f"users/{UID}/parties/{PID}/pieces_identite/{PROOF}",
    f"users/{UID}/parties/{PID}/pieces_identite/{PROOF}.pdf/x",
    f"users/{UID}/parties/{PID}/autre/{PROOF}.pdf",
    f"users/{UID}/dossiers/d1/{PROOF}.pdf",                    # a document's
    f"staging/{UID}/identite/{PROOF}/upload.pdf",
    "",
    None,
])
def test_a_stored_path_of_another_shape_is_refused(path):
    with pytest.raises(pi.StoredPathRefused):
        pi.checked_path(_record(path), PID)


def test_a_record_that_names_no_proof_is_refused():
    for proof in (None, "x", {}, _record(_final("x"), proof_id="x"),
                  {"id": PROOF, "storage_path": 3}):
        with pytest.raises(pi.StoredPathRefused):
            pi.checked_path(proof, PID)
    with pytest.raises(pi.StoredPathRefused):
        pi.checked_path(_record(_final()), "a/b")


# ══════════════════════════════════════════════════════════════════════
# 3. The readers
# ══════════════════════════════════════════════════════════════════════


def test_the_list_is_oldest_first_and_capped(world):
    ids = _seed_many(world, 3)
    listing = pi.list_proofs_strict(PID)
    assert [p["id"] for p in listing.proofs] == ids
    assert listing.truncated is False


def test_a_list_over_the_cap_says_it_is_truncated(world):
    ids = _seed_many(world, pi.MAX_PER_CONTACT + 2)
    listing = pi.list_proofs_strict(PID)
    assert [p["id"] for p in listing.proofs] == ids[:pi.MAX_PER_CONTACT]
    assert listing.truncated is True


def test_a_failed_list_read_raises_and_its_display_twin_answers_none(
        world, monkeypatch, caplog):
    _seed_many(world, 2)
    monkeypatch.setattr(world.fake._fake_server, "run_query", _boom)
    with pytest.raises(gexc.ServiceUnavailable):
        pi.list_proofs_strict(PID)
    caplog.set_level(logging.WARNING, logger=pi.__name__)
    assert pi.list_proofs(PID) is None              # never an empty list
    assert "id proof list read failed: ServiceUnavailable" in caplog.text


def test_has_proofs_reads_strictly_and_inside_a_transaction(world, monkeypatch):
    assert pi.has_proofs_strict(PID) is False
    _seed_proof(world)
    assert pi.has_proofs_strict(PID) is True
    world.fake.reset_logs()
    transaction = world.fake.transaction()
    seen = []

    @pi.firestore.transactional
    def _probe(txn):
        seen.append(pi.has_proofs_strict(PID, transaction=txn))

    _probe(transaction)
    assert seen == [True]
    assert world.fake.reads and all(r.transactional for r in world.fake.reads)
    monkeypatch.setattr(world.fake._fake_server, "run_query", _boom)
    with pytest.raises(gexc.ServiceUnavailable):
        pi.has_proofs_strict(PID)


def test_get_proof_strict_reads_one_and_raises_on_failure(world, monkeypatch):
    record, _ = _seed_proof(world)
    assert pi.get_proof_strict(PID, PROOF)["display_name"] == record["display_name"]
    assert pi.get_proof_strict(PID, PROOF2) is None
    monkeypatch.setattr(world.fake._fake_server, "batch_get_documents", _boom)
    with pytest.raises(gexc.ServiceUnavailable):
        pi.get_proof_strict(PID, PROOF)


@pytest.mark.parametrize("partie_id, proof_id", [
    ("", PROOF), ("a/b", PROOF), ("..", PROOF), (None, PROOF),
    (PID, ""), (PID, "x"), (PID, PROOF.upper()), (PID, UUID1),
    (PID, f"{PROOF}/x"),
])
def test_an_unaddressable_id_is_absent_without_a_read(world, partie_id, proof_id):
    _seed_proof(world)
    world.fake.reset_logs()
    assert pi.get_proof_strict(partie_id, proof_id) is None
    if partie_id != PID:
        assert pi.list_proofs_strict(partie_id) == pi.Listing([], False)
        assert pi.has_proofs_strict(partie_id) is False
    assert pi.rename_proof(partie_id, proof_id, "Nouveau",
                           expected_etag="e").code == pi.CODE_INTROUVABLE
    assert pi.delete_proof(partie_id, proof_id,
                           expected_etag="e").code == pi.CODE_INTROUVABLE
    assert world.fake.reads == [] and world.fake.commits == []


# ══════════════════════════════════════════════════════════════════════
# 4. The session — every check before a byte moves
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("name, ext, mime", [
    ("Permis.pdf", ".pdf", "application/pdf"),
    ("SCAN.PDF", ".pdf", "application/pdf"),
    ("PHOTO.JPG", ".jpg", "image/jpeg"),
    ("carte.Png", ".png", "image/png"),
])
def test_the_session_opens_the_identity_staging_shape(world, name, ext, mime):
    out = pi.open_proof_upload(PID, name, 1234, UID, origin=ORIGIN)
    assert out.code == pi.CODE_OK and out.errors == []
    proof_id = out.doc["proof_id"]
    assert document_model.is_canonical_uuid4(proof_id)
    assert out.doc["objet"] == _staging(proof_id, ext)    # no client file name
    [session] = world.bucket.sessions.values()
    assert out.doc["url"].startswith("https://")
    assert session == {
        "name": out.doc["objet"], "content_type": mime, "size": 1234,
        "origin": ORIGIN, "if_generation_match": 0, "md5_hash": None,
    }
    assert world.fake.commits == []            # nothing written to the store


@pytest.mark.parametrize("name, size, error", [
    ("scan.tiff", 10, pi.TYPE_REFUSED),
    ("photo.heic", 10, pi.TYPE_REFUSED),
    ("<b>x</b>.pdf", 10,
     document_model.TEXT_CHEVRONS_ERROR.format(label="Nom du fichier")),
    ("a.pdf", 0, pi.SIZE_REFUSED),
    ("a.pdf", -5, pi.SIZE_REFUSED),
    ("a.pdf", 20 * 1024 * 1024 + 1, pi.SIZE_REFUSED),
    ("a.pdf", True, pi.SIZE_REFUSED),
    ("a.pdf", "10", pi.SIZE_REFUSED),
    ("a.pdf", 10.0, pi.SIZE_REFUSED),
])
def test_a_refused_file_opens_no_session(world, name, size, error):
    out = pi.open_proof_upload(PID, name, size, UID, origin=ORIGIN)
    assert out == pi.Outcome(None, [error], pi.CODE_REFUS)
    assert world.bucket.sessions == {}


def test_the_size_bounds_are_inclusive(world):
    for size in (1, pi.MAX_SIZE):
        assert pi.open_proof_upload(PID, "a.pdf", size, UID,
                                    origin=ORIGIN).code == pi.CODE_OK


@pytest.mark.parametrize("partie_id", ["p-absent", "", "a/b"])
def test_an_unknown_contact_opens_no_session(world, partie_id):
    out = pi.open_proof_upload(partie_id, "a.pdf", 10, UID, origin=ORIGIN)
    assert out == pi.Outcome(None, [pi.CONTACT_NOT_FOUND], pi.CODE_REFUS)
    assert world.bucket.sessions == {}


def test_a_full_contact_opens_no_session(world):
    _seed_many(world, pi.MAX_PER_CONTACT)
    out = pi.open_proof_upload(PID, "a.pdf", 10, UID, origin=ORIGIN)
    assert out == pi.Outcome(None, [pi.CAP_REACHED], pi.CODE_REFUS)
    assert world.bucket.sessions == {}


@pytest.mark.parametrize("rpc, message", [
    ("batch_get_documents", "id proof: contact unreadable"),
    ("run_query", "id proof: count unreadable"),
])
def test_a_failed_read_at_session_open_is_unavailable_never_a_refusal(
        world, monkeypatch, logged, rpc, message):
    monkeypatch.setattr(world.fake._fake_server, rpc, _boom)
    out = pi.open_proof_upload(PID, "a.pdf", 10, UID, origin=ORIGIN)
    assert out == pi.Outcome(None, [concurrency.READ_UNAVAILABLE_ERROR],
                             pi.CODE_INDISPONIBLE)
    assert world.bucket.sessions == {}
    assert [m for m, _ in logged] == [message]
    _assert_ids_only(logged)


@pytest.mark.parametrize("bad_uid", ["unknown", "", None, "a/b"])
def test_an_unusable_uid_is_refused_before_any_read(world, monkeypatch, bad_uid):
    monkeypatch.setattr(pi.storage, "bucket", lambda: pytest.fail("bucket"))
    for out in (pi.open_proof_upload(PID, "a.pdf", 10, bad_uid, origin=ORIGIN),
                pi.file_staged_proof(_staging(), PID, "a.pdf", bad_uid)):
        assert out == pi.Outcome(None, [storage_identity.INVALID_UID_MESSAGE],
                                 pi.CODE_INDISPONIBLE)
    assert world.fake.reads == []


def test_a_session_that_cannot_open_is_unavailable_and_logged_without_its_url(
        world, monkeypatch, logged):
    def _refuse(self, **_kw):
        raise gexc.ServiceUnavailable("https://storage.example/upload?secret")

    monkeypatch.setattr(FakeBlob, "create_resumable_upload_session", _refuse)
    out = pi.open_proof_upload(PID, "a.pdf", 10, UID, origin=ORIGIN)
    assert out == pi.Outcome(None, [pi.SESSION_FAILED], pi.CODE_INDISPONIBLE)
    assert logged == [("id proof: session open failed", {
        "partie_id": PID, "proof_id": mock.ANY,
        "error_type": "ServiceUnavailable"})]
    _assert_ids_only(logged)


def test_a_session_url_that_is_not_https_is_never_handed_out(
        world, monkeypatch, logged):
    monkeypatch.setattr(FakeBlob, "create_resumable_upload_session",
                        lambda self, **_kw: "http://storage.example/upload")
    out = pi.open_proof_upload(PID, "a.pdf", 10, UID, origin=ORIGIN)
    assert out == pi.Outcome(None, [pi.SESSION_FAILED], pi.CODE_INDISPONIBLE)
    assert [m for m, _ in logged] == ["id proof: session open failed"]


# ══════════════════════════════════════════════════════════════════════
# 5. The filing
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("name, data, ext, mime, stem", [
    ("Permis de conduire.pdf", PDF, ".pdf", "application/pdf", "Permis de conduire"),
    ("SCAN.PDF", PDF, ".pdf", "application/pdf", "SCAN"),
    ("PHOTO.JPG", JPEG, ".jpg", "image/jpeg", "PHOTO"),
    ("photo.jpeg", JPEG, ".jpeg", "image/jpeg", "photo"),
    ("carte.png", PNG, ".png", "image/png", "carte"),
])
def test_a_staged_upload_is_filed_object_first_then_record(
        world, name, data, ext, mime, stem):
    objet, proof_id = _upload(world, name, data)
    object_at_commit = []
    world.fake.add_commit_hook(lambda info: object_at_commit.append(
        _final(proof_id, ext) in world.bucket.objects))
    with provenance.writing_via("web"):
        out = pi.file_staged_proof(objet, PID, name, UID)
        noted = provenance.committed_writes()
    assert out.code == pi.CODE_VERSE and out.errors == []
    assert object_at_commit == [True]         # the object BEFORE the record
    stored = _records(world)[proof_id]
    assert set(stored) == RECORD_KEYS          # no original_filename
    assert stored == out.doc
    assert stored["display_name"] == stem
    assert stored["file_type"] == mime and stored["file_size"] == len(data)
    assert stored["storage_path"] == _final(proof_id, ext)
    assert stored["created_via"] == stored["updated_via"] == "web"
    assert stored["etag"] and stored["created_at"] == stored["updated_at"]
    assert noted == ((f"parties/{PID}/pieces_identite", proof_id),)
    final = world.bucket.objects[_final(proof_id, ext)]
    assert final.data == data and final.content_type == mime
    assert objet not in world.bucket.objects   # the staging object is consumed


def test_the_staging_object_is_consumed_by_generation(world, deletes):
    objet, proof_id = _upload(world)
    generation = world.bucket.objects[objet].generation
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert out.code == pi.CODE_VERSE
    assert deletes == [(objet, generation, generation)]


@pytest.mark.parametrize("name, data", [
    ("faux.pdf", PNG),       # a PNG named .pdf
    ("page.jpg", HTML),      # an HTML page named .jpg
    ("iphone.jpg", HEIC),    # a HEIC named .jpg
    ("scan.png", TIFF),      # a TIFF named .png
    ("photo.png", JPEG),     # a JPEG named .png
])
def test_content_that_lies_about_its_extension_files_nothing(world, name, data):
    objet, proof_id = _upload(world, name, data)
    out = pi.file_staged_proof(objet, PID, name, UID)
    assert out == pi.Outcome(None, [pi.CONTENT_MISMATCH], pi.CODE_REFUS)
    assert _records(world) == {}
    assert world.bucket.objects == {}          # no copy, staging consumed


@pytest.mark.parametrize("size", [0, 17])
def test_the_real_staged_size_is_judged_at_filing(world, monkeypatch, size):
    monkeypatch.setattr(pi, "MAX_SIZE", 16)
    world.bucket.put(_staging(), (b"%PDF-1.7\n" + b"x" * 20)[:size])
    out = pi.file_staged_proof(_staging(), PID, "a.pdf", UID)
    assert out == pi.Outcome(None, [pi.SIZE_REFUSED], pi.CODE_REFUS)
    assert world.bucket.objects == {} and _records(world) == {}


def test_a_name_refused_after_the_upload_still_consumes_the_staging(world):
    objet, _ = _upload(world, "a.pdf")
    out = pi.file_staged_proof(objet, PID, "<b>a</b>.pdf", UID)
    assert out.code == pi.CODE_REFUS
    assert world.bucket.objects == {} and _records(world) == {}


def test_a_name_whose_extension_is_not_the_staged_one_is_a_forged_request(world):
    objet, _ = _upload(world, "a.pdf")
    out = pi.file_staged_proof(objet, PID, "a.jpg", UID)
    assert out == pi.Outcome(None, [pi.REQUEST_INVALID], pi.CODE_FORME)
    assert world.bucket.objects == {}         # consumed: the shape was ours


def test_a_replayed_filing_lands_on_one_record(world):
    out, objet, proof_id = _file(world)
    assert out.code == pi.CODE_VERSE
    objects = dict(world.bucket.objects)
    again = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert again.code == pi.CODE_REJOUE and again.errors == []
    assert again.doc == _records(world)[proof_id]
    assert list(_records(world)) == [proof_id]
    assert world.bucket.objects == objects


def test_a_replay_with_no_record_is_refused_honestly(world):
    out = pi.file_staged_proof(_staging(), PID, "a.pdf", UID)
    assert out == pi.Outcome(None, [pi.STAGED_MISSING], pi.CODE_REFUS)


def test_a_reserved_id_held_by_another_record_files_nothing(world):
    _seed_proof(world, PROOF, data=None, path=_final(PROOF, ".png"), ext=".png")
    world.bucket.put(_staging(), PDF)
    out = pi.file_staged_proof(_staging(), PID, "a.pdf", UID)
    assert out == pi.Outcome(None, [pi.CONFLICT], pi.CODE_REFUS)
    assert world.bucket.objects == {}          # nothing copied, staging consumed


def test_a_contact_deleted_before_filing_files_nothing(world):
    objet, proof_id = _upload(world)
    world.fake.external_delete(f"parties/{PID}")
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert out == pi.Outcome(None, [pi.CONTACT_NOT_FOUND], pi.CODE_REFUS)
    assert world.bucket.objects == {} and _records(world) == {}


def test_a_contact_deleted_during_filing_aborts_and_erases_this_copy(
        world, deletes):
    objet, proof_id = _upload(world)
    fired = []

    def _hook(info):
        if not fired and any(kind == "create" for kind, _ in info.ops):
            fired.append(True)
            world.fake.external_delete(f"parties/{PID}")

    world.fake.add_commit_hook(_hook)
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert fired
    assert out == pi.Outcome(None, [pi.CONTACT_NOT_FOUND], pi.CODE_REFUS)
    assert _records(world) == {} and world.bucket.objects == {}
    assert (_final(proof_id), mock.ANY, mock.ANY) in deletes
    erased = next(d for d in deletes if d[0] == _final(proof_id))
    assert erased[1] is not None and erased[1] == erased[2]   # by generation


def test_the_cap_is_re_checked_at_filing(world):
    _seed_many(world, pi.MAX_PER_CONTACT - 1)
    objet, proof_id = _upload(world)
    _seed_proof(world, PROOF2, created_at=T0 + timedelta(days=1))
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert out == pi.Outcome(None, [pi.CAP_REACHED], pi.CODE_REFUS)
    assert proof_id not in _records(world)
    assert _final(proof_id) not in world.bucket.objects
    assert objet not in world.bucket.objects


def test_the_cap_is_re_checked_inside_the_create_transaction(world):
    """A proof filed by another tab between this call's reads and its
    commit aborts the transaction; the re-run counts it and refuses."""
    _seed_many(world, pi.MAX_PER_CONTACT - 1)
    objet, proof_id = _upload(world)
    fired = []

    def _hook(info):
        if not fired and any(kind == "create" for kind, _ in info.ops):
            fired.append(True)
            world.fake.external_write(
                f"parties/{PID}/pieces_identite/{PROOF2}",
                {"id": PROOF2, "partie_id": PID, "created_at": T0})

    world.fake.add_commit_hook(_hook)
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert fired
    assert out == pi.Outcome(None, [pi.CAP_REACHED], pi.CODE_REFUS)
    assert proof_id not in _records(world)
    assert _final(proof_id) not in world.bucket.objects


def test_an_occupied_destination_is_never_overwritten_nor_erased(
        world, logged):
    objet, proof_id = _upload(world)
    planted = world.bucket.put(_final(proof_id), b"%PDF-1.7\nautre fichier")
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert out == pi.Outcome(None, [pi.OCCUPIED], pi.CODE_REFUS)
    assert world.bucket.objects[_final(proof_id)] is planted
    assert _records(world) == {}
    # Kept, never erased — and named for an operator: only this upload's
    # finalizations write its reserved path, so the occupant is a copy it
    # left behind.
    assert logged == [("id proof: object occupied",
                       {"partie_id": PID, "proof_id": proof_id})]


def _fail_final_patch(monkeypatch, on_failure=None):
    real = FakeBlob.patch

    def _patch(self, client=None, if_generation_match=None, **kwargs):
        if self.name.startswith("users/"):
            if on_failure is not None:
                on_failure()
            raise gexc.ServiceUnavailable("patch failed")
        return real(self, client=client, if_generation_match=if_generation_match,
                    **kwargs)

    monkeypatch.setattr(FakeBlob, "patch", _patch)


def test_an_adopted_object_and_a_patch_failure_never_erase_the_other_calls_bytes(
        world, monkeypatch, logged):
    """A parallel finalization of the same upload wrote the object (same
    bytes) and has not committed yet: this call ADOPTS it, its patch fails —
    and it must erase nothing, or the other call would commit a record
    whose bytes are gone."""
    objet, proof_id = _upload(world)
    theirs = world.bucket.put(_final(proof_id), PDF,
                              content_type="application/pdf")
    _fail_final_patch(monkeypatch)
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert out == pi.Outcome(None, [pi.FILING_FAILED], pi.CODE_INDISPONIBLE)
    assert world.bucket.objects[_final(proof_id)] is theirs
    assert world.bucket.objects[_final(proof_id)].generation == theirs.generation
    assert _records(world) == {}
    assert [m for m, _ in logged] == ["id proof: object write failed",
                                      "id proof: adopted object left"]
    assert logged[-1][1] == {"partie_id": PID, "proof_id": proof_id}
    _assert_ids_only(logged)


def _their_record(proof_id, *, ext=".pdf", data=PDF) -> dict:
    """The record a parallel finalization of the same upload commits."""
    return {
        "id": proof_id, "partie_id": PID, "display_name": "Permis",
        "file_type": pi.MIME_TYPES[ext], "file_size": len(data),
        "storage_path": _final(proof_id, ext), "created_at": T0,
        "updated_at": T0, "etag": "e-theirs", "created_via": "web",
        "updated_via": "web",
    }


def test_an_adopted_object_whose_owner_committed_answers_with_its_record(
        world, monkeypatch):
    objet, proof_id = _upload(world)
    theirs = world.bucket.put(_final(proof_id), PDF,
                              content_type="application/pdf")
    _fail_final_patch(monkeypatch, on_failure=lambda: world.fake.external_write(
        f"parties/{PID}/pieces_identite/{proof_id}", _their_record(proof_id)))
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert out.code == pi.CODE_REJOUE and out.doc["etag"] == "e-theirs"
    assert world.bucket.objects[_final(proof_id)] is theirs


def test_a_parallel_filing_of_the_same_upload_is_its_answer_not_a_full_contact(
        world, logged):
    """The other call wrote the object and committed the record after this
    call's first look: the create transaction reads the reserved record
    BEFORE counting, so the contact's 20th proof — this very upload — is
    the answer (``rejoue``), never a « déjà 20 preuves » refusal."""
    _seed_many(world, pi.MAX_PER_CONTACT - 1)
    objet, proof_id = _upload(world)
    planted = []

    def _parallel(dest, _source):
        if dest == _final(proof_id) and not planted:
            planted.append(world.bucket.put(dest, PDF,
                                            content_type="application/pdf"))
            world.fake.external_write(
                f"parties/{PID}/pieces_identite/{proof_id}",
                _their_record(proof_id))

    world.bucket.rewrite_hooks.append(_parallel)
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert out.code == pi.CODE_REJOUE and out.doc["etag"] == "e-theirs"
    assert world.bucket.objects[_final(proof_id)] is planted[0]   # adopted
    assert objet not in world.bucket.objects
    assert logged == []          # a record points at the adopted object


def test_an_already_exists_at_commit_answers_with_the_committed_record(world):
    objet, proof_id = _upload(world)
    fired = []

    def _hook(info):
        if not fired and any(kind == "create" for kind, _ in info.ops):
            fired.append(True)
            world.fake.external_write(
                f"parties/{PID}/pieces_identite/{proof_id}",
                _their_record(proof_id))
            raise gexc.AlreadyExists("Document already exists")

    world.fake.add_commit_hook(_hook)
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert fired
    # Its record, committed by the other call: a replay of this upload —
    # never « verse », which would report a second filing.
    assert out.code == pi.CODE_REJOUE and out.doc["etag"] == "e-theirs"
    assert world.bucket.objects[_final(proof_id)].data == PDF


def test_another_record_under_the_reserved_id_refuses_and_erases_this_copy(
        world, deletes):
    objet, proof_id = _upload(world)
    other = _their_record(proof_id, ext=".png")

    def _sneak(dest, _source):
        if dest == _final(proof_id):
            world.fake.external_write(
                f"parties/{PID}/pieces_identite/{proof_id}", other)

    world.bucket.rewrite_hooks.append(_sneak)
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert out == pi.Outcome(None, [pi.CONFLICT], pi.CODE_REFUS)
    assert _final(proof_id) not in world.bucket.objects   # this call's copy
    erased = [d for d in deletes if d[0] == _final(proof_id)]
    assert len(erased) == 1
    assert erased[0][2] is not None and erased[0][1] == erased[0][2]
    assert _records(world)[proof_id]["storage_path"] == _final(proof_id, ".png")


def test_a_rollback_never_erases_another_generation_of_its_object(
        world, monkeypatch):
    """Bound to the generation THIS call created: a live object of another
    generation is refused by the service (412) and survives."""
    objet, proof_id = _upload(world)
    replaced = []

    def _replace():
        replaced.append(world.bucket.put(_final(proof_id), PDF,
                                         content_type="application/pdf"))

    _fail_final_patch(monkeypatch, on_failure=_replace)
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert out == pi.Outcome(None, [pi.FILING_FAILED], pi.CODE_INDISPONIBLE)
    assert world.bucket.objects[_final(proof_id)] is replaced[0]


def test_a_failure_before_the_create_is_staged_is_certain_nothing_was_written(
        world, monkeypatch, logged):
    """The transaction's own read fails, then every read: nothing was
    staged, so this call's outcome is CERTAIN — « réessayez », the staging
    object consumed — while its copy is kept, since whether a record points
    at it could not be read."""
    objet, proof_id = _upload(world)
    server = world.fake._fake_server
    real_get = server.batch_get_documents
    down = []

    def _get(request, metadata=None, **kwargs):
        if request.get("transaction") or down:
            down.append(True)
            raise gexc.ServiceUnavailable("store down")
        return real_get(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", _get)
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert out == pi.Outcome(None, [pi.FILING_FAILED], pi.CODE_INDISPONIBLE)
    assert _final(proof_id) in world.bucket.objects
    assert objet not in world.bucket.objects
    assert [m for m, _ in logged] == ["id proof: record write failed",
                                      "id proof: rollback check unreadable"]
    _assert_ids_only(logged)


def test_a_replay_answers_only_with_this_uploads_own_record(world):
    _seed_proof(world, PROOF, ext=".png")       # the same id, another object
    out = pi.file_staged_proof(_staging(PROOF, ".pdf"), PID, "a.pdf", UID)
    assert out == pi.Outcome(None, [pi.STAGED_MISSING], pi.CODE_REFUS)


def test_a_patch_failure_on_this_calls_own_copy_erases_it_by_generation(
        world, monkeypatch, deletes):
    objet, proof_id = _upload(world)
    _fail_final_patch(monkeypatch)
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert out == pi.Outcome(None, [pi.FILING_FAILED], pi.CODE_INDISPONIBLE)
    assert _final(proof_id) not in world.bucket.objects
    erased = [d for d in deletes if d[0] == _final(proof_id)]
    assert len(erased) == 1 and erased[0][1] is not None
    assert erased[0][1] == erased[0][2]


def test_an_adopted_object_is_never_erased_when_the_record_commit_fails(
        world, monkeypatch, logged):
    objet, proof_id = _upload(world)
    theirs = world.bucket.put(_final(proof_id), PDF,
                              content_type="application/pdf")
    _break_commit(monkeypatch, world.fake, kind="create", apply=False)
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert out == pi.Outcome(None, [pi.FILING_FAILED], pi.CODE_INDISPONIBLE)
    assert world.bucket.objects[_final(proof_id)] is theirs
    assert [m for m, _ in logged] == ["id proof: record write failed",
                                      "id proof: adopted object left"]


def test_a_failed_record_commit_erases_this_calls_copy_by_generation(
        world, monkeypatch, deletes, logged):
    objet, proof_id = _upload(world)
    _break_commit(monkeypatch, world.fake, kind="create", apply=False)
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert out == pi.Outcome(None, [pi.FILING_FAILED], pi.CODE_INDISPONIBLE)
    assert _records(world) == {} and world.bucket.objects == {}
    erased = [d for d in deletes if d[0] == _final(proof_id)]
    assert len(erased) == 1
    assert erased[0][2] is not None and erased[0][1] == erased[0][2]
    assert [m for m, _ in logged] == ["id proof: record write failed"]


def test_a_lost_commit_answer_whose_record_landed_is_filed(world, monkeypatch):
    objet, proof_id = _upload(world)
    _break_commit(monkeypatch, world.fake, kind="create", apply=True)
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert out.code == pi.CODE_VERSE and out.errors == []
    assert out.doc["storage_path"] == _final(proof_id)
    assert list(_records(world)) == [proof_id]
    assert world.bucket.objects[_final(proof_id)].data == PDF
    assert objet not in world.bucket.objects


def test_an_unknown_outcome_keeps_both_objects_and_the_repost_files_it(
        world, monkeypatch, logged):
    objet, proof_id = _upload(world)
    state = _break_commit(monkeypatch, world.fake, kind="create", apply=False,
                          reads_fail=True)
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert out == pi.Outcome(None, [pi.FILING_UNCERTAIN], pi.CODE_INCERTAIN)
    assert objet in world.bucket.objects               # the staging object
    final = world.bucket.objects[_final(proof_id)]     # and the final one
    assert _records(world) == {}
    assert ("id proof: filing uncertain",
            {"partie_id": PID, "proof_id": proof_id}) in logged
    _assert_ids_only(logged)

    state["down"] = False                  # the page re-posts « finaliser »
    before = len(logged)
    again = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert again.code == pi.CODE_VERSE
    assert list(_records(world)) == [proof_id]
    assert world.bucket.objects[_final(proof_id)] is final   # adopted, kept
    assert objet not in world.bucket.objects
    assert logged[before:] == []           # filed: nothing left to name


def test_an_unknown_outcome_whose_commit_landed_answers_rejoue_on_repost(
        world, monkeypatch):
    objet, proof_id = _upload(world)
    state = _break_commit(monkeypatch, world.fake, kind="create", apply=True,
                          reads_fail=True)
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert out.code == pi.CODE_INCERTAIN
    assert objet in world.bucket.objects and _final(proof_id) in world.bucket.objects
    state["down"] = False
    again = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert again.code == pi.CODE_REJOUE
    assert list(_records(world)) == [proof_id]
    assert world.bucket.objects[_final(proof_id)].data == PDF
    assert objet not in world.bucket.objects


ADOPTED_LEFT = "id proof: adopted object left"


def _lost_rewrite_answer(monkeypatch, *, then=None) -> list:
    """Play the library's conditional retry of a rewrite whose answer was
    lost (the fake models no retry, ``tests/_fake_gcs.py``).

    google-cloud-storage 3.15.0's ``Blob.rewrite`` defaults to
    ``DEFAULT_RETRY_IF_GENERATION_SPECIFIED``, and ``ifGenerationMatch=0``
    counts as specified: the first attempt CREATES the destination, its
    answer never reaches the caller (the blob learns nothing), and the
    retry of the same request answers 412 — the call sees
    ``PreconditionFailed`` on the object it wrote itself. *then* runs
    between the two attempts."""
    real = FakeBlob.rewrite
    played: list = []

    def _rewrite(self, source, token=None, client=None,
                 if_generation_match=None, **kwargs):
        if self.name.startswith("users/") and not played:
            played.append(True)
            real(FakeBlob(self.bucket, self.name), source, token=token,
                 client=client, if_generation_match=if_generation_match,
                 **kwargs)                      # lands; its answer is lost
            if then is not None:
                then()
        return real(self, source, token=token, client=client,
                    if_generation_match=if_generation_match, **kwargs)

    monkeypatch.setattr(FakeBlob, "rewrite", _rewrite)
    return played


def _fail_transactional_reads(monkeypatch, world) -> None:
    """Every read made INSIDE a transaction raises; the others answer."""
    server = world.fake._fake_server
    real_get = server.batch_get_documents

    def _get(request, metadata=None, **kwargs):
        if request.get("transaction"):
            raise gexc.ServiceUnavailable("store down")
        return real_get(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", _get)


@pytest.mark.parametrize("case, code, errors, lines", [
    pytest.param("contact", pi.CODE_REFUS, [pi.CONTACT_NOT_FOUND],
                 [ADOPTED_LEFT], id="contact-deleted"),
    pytest.param("cap", pi.CODE_REFUS, [pi.CAP_REACHED], [ADOPTED_LEFT],
                 id="cap-reached"),
    pytest.param("conflict", pi.CODE_REFUS, [pi.CONFLICT], [ADOPTED_LEFT],
                 id="another-record"),
    pytest.param("absent", pi.CODE_INDISPONIBLE, [pi.FILING_FAILED],
                 ["id proof: record write failed", ADOPTED_LEFT],
                 id="commit-settled-absent"),
    pytest.param("read", pi.CODE_INDISPONIBLE, [pi.FILING_FAILED],
                 ["id proof: record write failed", ADOPTED_LEFT],
                 id="transaction-read-failed"),
    # Unknown outcome: « filing uncertain » names the same ids already.
    pytest.param("uncertain", pi.CODE_INCERTAIN, [pi.FILING_UNCERTAIN],
                 ["id proof: record write failed",
                  "id proof: filing uncertain"], id="uncertain"),
    pytest.param("filed", pi.CODE_VERSE, [], [], id="filed"),
])
def test_an_adopted_object_is_kept_and_named_once_unless_filed(
        world, monkeypatch, logged, case, code, errors, lines):
    """The library's retry of a rewrite whose answer was lost hands this
    call a 412 on its OWN copy: identical bytes, ADOPTED — and an adopted
    object is never erased (the plan's rule: it may be another call's).
    When the call then files nothing, the object stays with no record:
    « id proof: adopted object left » names it ONCE, whichever exit, so an
    operator can erase it (review of lot 2, filing F2)."""
    if case == "cap":
        _seed_many(world, pi.MAX_PER_CONTACT - 1)
    objet, proof_id = _upload(world)
    reserved = f"parties/{PID}/pieces_identite/{proof_id}"
    then = {
        "contact": lambda: world.fake.external_delete(f"parties/{PID}"),
        "cap": lambda: _seed_proof(world, PROOF2,
                                   created_at=T0 + timedelta(days=1)),
        "conflict": lambda: world.fake.external_write(
            reserved, _their_record(proof_id, ext=".png")),
    }.get(case)
    if case == "absent":
        _break_commit(monkeypatch, world.fake, kind="create", apply=False)
    elif case == "uncertain":
        _break_commit(monkeypatch, world.fake, kind="create", apply=False,
                      reads_fail=True)
    elif case == "read":
        _fail_transactional_reads(monkeypatch, world)
    played = _lost_rewrite_answer(monkeypatch, then=then)

    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)

    assert played and (out.code, out.errors) == (code, errors)
    assert world.bucket.objects[_final(proof_id)].data == PDF   # never erased
    assert [m for m, _ in logged] == lines
    assert [f for m, f in logged if m == ADOPTED_LEFT] == [
        {"partie_id": PID, "proof_id": proof_id}] * lines.count(ADOPTED_LEFT)
    _assert_ids_only(logged)
    stored = world.fake.peek(reserved)
    if case == "filed":
        assert stored["storage_path"] == _final(proof_id)
    elif case == "conflict":                  # the other record, untouched
        assert stored["storage_path"] == _final(proof_id, ".png")
    else:
        assert stored is None
    # The staging object is consumed, except by the uncertain answer.
    assert (objet in world.bucket.objects) is (case == "uncertain")


def test_a_reserved_path_that_cannot_be_reloaded_is_occupied_and_named(
        world, monkeypatch, logged):
    """The same lost answer, and the reload that would compare the bytes
    fails: nothing proves the object identical, so it is refused
    (``OCCUPIED``) and kept — neither adopted nor erased — and « id proof:
    object occupied » names it for an operator (review of lot 2, filing
    F2)."""
    objet, proof_id = _upload(world)
    played = _lost_rewrite_answer(monkeypatch)
    real_reload = FakeBlob.reload

    def _reload(self, *a, **kw):
        if self.name.startswith("users/"):
            raise gexc.ServiceUnavailable("gcs down")
        return real_reload(self, *a, **kw)

    monkeypatch.setattr(FakeBlob, "reload", _reload)
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert played
    assert out == pi.Outcome(None, [pi.OCCUPIED], pi.CODE_REFUS)
    assert world.bucket.objects[_final(proof_id)].data == PDF     # kept
    assert _records(world) == {} and objet not in world.bucket.objects
    assert logged == [("id proof: object occupied",
                       {"partie_id": PID, "proof_id": proof_id})]


def test_an_uncertain_filing_s_copy_adopted_by_a_refused_re_post_is_named(
        world, monkeypatch, logged):
    """The « incertain » predecessor kept its copy for the re-post to adopt;
    the re-post adopts it, then the cap — filled meanwhile by another tab —
    refuses the filing. The copy is never erased (the plan's rule) and no
    call owns it any more: « id proof: adopted object left » names it
    (review of lot 2, filing F2)."""
    objet, proof_id = _upload(world)
    state = _break_commit(monkeypatch, world.fake, kind="create", apply=False,
                          reads_fail=True)
    first = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert first.code == pi.CODE_INCERTAIN
    final = world.bucket.objects[_final(proof_id)]
    state["down"] = False
    _seed_many(world, pi.MAX_PER_CONTACT)
    before = len(logged)

    again = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)

    assert again == pi.Outcome(None, [pi.CAP_REACHED], pi.CODE_REFUS)
    assert world.bucket.objects[_final(proof_id)] is final   # adopted, kept
    assert proof_id not in _records(world)
    assert objet not in world.bucket.objects                 # consumed
    assert logged[before:] == [(ADOPTED_LEFT,
                                {"partie_id": PID, "proof_id": proof_id})]


@pytest.mark.parametrize("failing, message, error", [
    ("reload", "id proof: staging reload failed", pi.STAGED_READ_FAILED),
    ("download_as_bytes", "id proof: staged read failed", pi.STAGED_READ_FAILED),
])
def test_a_staging_read_failure_is_unavailable(
        world, monkeypatch, logged, failing, message, error):
    objet, _ = _upload(world)
    real = getattr(FakeBlob, failing)

    def _fail(self, *a, **kw):
        if self.name.startswith("staging/"):
            raise gexc.ServiceUnavailable("gcs down")
        return real(self, *a, **kw)

    monkeypatch.setattr(FakeBlob, failing, _fail)
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert out == pi.Outcome(None, [error], pi.CODE_INDISPONIBLE)
    assert message in [m for m, _ in logged]
    assert _records(world) == {}
    assert not any(n.startswith("users/") for n in world.bucket.objects)


def test_an_unreadable_reserved_record_copies_nothing(world, monkeypatch, logged):
    objet, proof_id = _upload(world)
    real_get = world.fake._fake_server.batch_get_documents

    def _get(request, metadata=None, **kwargs):
        if any("pieces_identite" in str(n) for n in request["documents"]):
            raise gexc.ServiceUnavailable("store down")
        return real_get(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(world.fake._fake_server, "batch_get_documents", _get)
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert out == pi.Outcome(None, [concurrency.READ_UNAVAILABLE_ERROR],
                             pi.CODE_INDISPONIBLE)
    assert [m for m, _ in logged] == ["id proof: reserved record unreadable"]
    assert _final(proof_id) not in world.bucket.objects


def test_a_contact_unreadable_at_filing_is_unavailable(world, monkeypatch, logged):
    objet, _ = _upload(world)
    monkeypatch.setattr(world.fake._fake_server, "batch_get_documents", _boom)
    out = pi.file_staged_proof(objet, PID, "Permis.pdf", UID)
    assert out == pi.Outcome(None, [concurrency.READ_UNAVAILABLE_ERROR],
                             pi.CODE_INDISPONIBLE)
    assert [m for m, _ in logged] == ["id proof: contact unreadable"]
    assert world.bucket.objects == {}


# ══════════════════════════════════════════════════════════════════════
# 6. The rename
# ══════════════════════════════════════════════════════════════════════


def test_a_rename_is_a_partial_compare_and_set(world):
    record, _ = _seed_proof(world)
    out = pi.rename_proof(PID, PROOF, "Passeport — recto",
                          expected_etag=record["etag"])
    assert out.code == pi.CODE_MODIFIE and out.errors == []
    stored = _records(world)[PROOF]
    assert out.doc == stored                       # the WRITTEN row
    changed = {k for k in RECORD_KEYS if stored[k] != record[k]}
    assert changed == {"display_name", "updated_at", "etag", "updated_via"}
    assert stored["display_name"] == "Passeport — recto"
    assert stored["updated_via"] == "script" and stored["etag"] != record["etag"]
    [commit] = world.fake.commits
    assert commit.ops == (("update", f"parties/{PID}/pieces_identite/{PROOF}"),)


def test_an_unchanged_name_writes_nothing(world):
    record, _ = _seed_proof(world)
    out = pi.rename_proof(PID, PROOF, record["display_name"],
                          expected_etag=record["etag"])
    assert out.code == pi.CODE_INCHANGE and out.errors == []
    assert world.fake.commits == []
    assert _records(world)[PROOF]["etag"] == record["etag"]


def test_a_rename_needs_the_current_version(world):
    record, _ = _seed_proof(world)
    assert pi.rename_proof(PID, PROOF, "X", expected_etag=None) == pi.Outcome(
        None, [pi.VERSION_REQUIRED], pi.CODE_VERSION)
    assert pi.rename_proof(PID, PROOF, "X", expected_etag="ancien") == pi.Outcome(
        None, [pi.PROOF_CHANGED], pi.CODE_VERSION)
    assert world.fake.commits == []
    assert _records(world)[PROOF] == record


def test_a_rename_racing_another_write_is_refused_and_writes_nothing(world):
    record, _ = _seed_proof(world)
    fired = []

    def _hook(info):
        if not fired:
            fired.append(True)
            world.fake.external_write(
                f"parties/{PID}/pieces_identite/{PROOF}",
                {**record, "display_name": "Autre onglet", "etag": "e-autre"})

    world.fake.add_commit_hook(_hook)
    out = pi.rename_proof(PID, PROOF, "Passeport", expected_etag=record["etag"])
    assert out == pi.Outcome(None, [pi.PROOF_CHANGED], pi.CODE_VERSION)
    assert _records(world)[PROOF]["display_name"] == "Autre onglet"


@pytest.mark.parametrize("name, error", [
    ("<b>Passeport</b>",
     document_model.TEXT_CHEVRONS_ERROR.format(label="Nom du document")),
    ("Passeport\trecto", "Nom du document : caractères de contrôle refusés."),
    ("Passeport\x9b", "Nom du document : caractères de contrôle refusés."),
    ("x" * 301, "Nom du document : 300 caractères au plus."),
    ("", "Nom du document : requis."),
])
def test_a_refused_name_is_never_altered_and_writes_nothing(world, name, error):
    record, _ = _seed_proof(world)
    out = pi.rename_proof(PID, PROOF, name, expected_etag=record["etag"])
    assert out == pi.Outcome(None, [error], pi.CODE_REFUS)
    assert world.fake.commits == [] and _records(world)[PROOF] == record


def test_renaming_a_missing_proof_is_introuvable(world):
    assert pi.rename_proof(PID, PROOF, "X", expected_etag="e") == pi.Outcome(
        None, [pi.PROOF_NOT_FOUND], pi.CODE_INTROUVABLE)


def test_a_rename_read_failure_is_unavailable(world, monkeypatch, logged):
    _seed_proof(world)
    monkeypatch.setattr(world.fake._fake_server, "batch_get_documents", _boom)
    out = pi.rename_proof(PID, PROOF, "X", expected_etag="e")
    assert out == pi.Outcome(None, [concurrency.READ_UNAVAILABLE_ERROR],
                             pi.CODE_INDISPONIBLE)
    assert [m for m, _ in logged] == ["id proof: read failed"]


@pytest.mark.parametrize("apply, reads_fail, code, errors", [
    (True, False, pi.CODE_MODIFIE, []),                              # landed
    (False, False, pi.CODE_INDISPONIBLE, [pi.RENAME_FAILED]),        # did not
    (False, True, pi.CODE_INCERTAIN, [pi.RENAME_UNCERTAIN]),         # unknown
])
def test_a_rename_whose_commit_raised_is_settled_by_its_etag(
        world, monkeypatch, logged, apply, reads_fail, code, errors):
    record, _ = _seed_proof(world)
    _break_commit(monkeypatch, world.fake, kind="update", apply=apply,
                  reads_fail=reads_fail)
    out = pi.rename_proof(PID, PROOF, "Passeport", expected_etag=record["etag"])
    assert (out.code, out.errors) == (code, errors)
    stored = world.fake.peek(f"parties/{PID}/pieces_identite/{PROOF}")
    assert (stored["display_name"] == "Passeport") is apply
    if apply:
        assert out.doc == stored
    assert [m for m, _ in logged] == ["id proof: write failed"]


# ══════════════════════════════════════════════════════════════════════
# 7. The deletion (D6)
# ══════════════════════════════════════════════════════════════════════


def test_a_deletion_erases_the_live_object_by_generation_then_the_record(
        world, deletes):
    record, obj = _seed_proof(world)
    order = []
    world.fake.add_commit_hook(lambda info: order.append(
        _final() in world.bucket.objects))
    out = pi.delete_proof(PID, PROOF, expected_etag=record["etag"])
    assert out.code == pi.CODE_SUPPRIME and out.errors == []
    assert out.doc["id"] == PROOF and out.doc["file_type"] == "application/pdf"
    assert order == [False]               # the object went FIRST
    assert _records(world) == {} and world.bucket.objects == {}
    # The blob carried the generation its own reload loaded: Blob.delete
    # hands it to delete_blob, which then erases that revision for good.
    assert deletes == [(_final(), obj.generation, obj.generation)]


def test_a_deletion_needs_the_current_version_and_erases_nothing_otherwise(
        world, deletes):
    record, _ = _seed_proof(world)
    assert pi.delete_proof(PID, PROOF, expected_etag=None) == pi.Outcome(
        None, [pi.VERSION_REQUIRED], pi.CODE_VERSION)
    assert pi.delete_proof(PID, PROOF, expected_etag="ancien") == pi.Outcome(
        None, [pi.PROOF_CHANGED], pi.CODE_VERSION)
    assert deletes == [] and _final() in world.bucket.objects
    assert PROOF in _records(world)


def test_a_double_submission_deletes_once_supprime_then_deja(world, monkeypatch):
    """Two « Supprimer définitivement » for one proof: the second runs while
    the first is about to erase the object. Only the call that removed the
    record answers ``supprime`` — the one that journals."""
    record, _ = _seed_proof(world)
    real = FakeBlob.delete
    fired, inner = [], []

    def _delete(self, client=None, if_generation_match=None, **kwargs):
        if self.name == _final() and not fired:
            fired.append(True)
            inner.append(pi.delete_proof(PID, PROOF,
                                         expected_etag=record["etag"]))
        return real(self, client=client,
                    if_generation_match=if_generation_match, **kwargs)

    monkeypatch.setattr(FakeBlob, "delete", _delete)
    outer = pi.delete_proof(PID, PROOF, expected_etag=record["etag"])
    assert inner[0].code == pi.CODE_SUPPRIME and inner[0].doc["id"] == PROOF
    assert outer == pi.Outcome(None, [pi.ALREADY_DELETED], pi.CODE_DEJA)
    assert _records(world) == {} and world.bucket.objects == {}


def test_a_second_deletion_after_the_first_finds_nothing(world):
    record, _ = _seed_proof(world)
    assert pi.delete_proof(PID, PROOF, expected_etag=record["etag"]).code == (
        pi.CODE_SUPPRIME)
    assert pi.delete_proof(PID, PROOF, expected_etag=record["etag"]) == (
        pi.Outcome(None, [pi.DELETE_NOT_FOUND], pi.CODE_INTROUVABLE))


def test_a_record_whose_file_is_gone_can_still_be_deleted(world):
    record, _ = _seed_proof(world, data=None)
    out = pi.delete_proof(PID, PROOF, expected_etag=record["etag"])
    assert out.code == pi.CODE_SUPPRIME and _records(world) == {}


def test_an_incomplete_deletion_is_completed_by_the_next_call(
        world, monkeypatch, logged):
    record, _ = _seed_proof(world)
    _break_commit(monkeypatch, world.fake, kind="delete", apply=False)
    out = pi.delete_proof(PID, PROOF, expected_etag=record["etag"])
    assert out == pi.Outcome(None, [pi.DELETE_INCOMPLETE], pi.CODE_INCOMPLET)
    assert world.bucket.objects == {}                  # the file is erased
    assert PROOF in _records(world)                    # its entry is left
    assert [m for m, _ in logged] == ["id proof: record delete failed after erase"]
    retry = pi.delete_proof(PID, PROOF, expected_etag=record["etag"])
    assert retry.code == pi.CODE_SUPPRIME and _records(world) == {}


@pytest.mark.parametrize("apply, reads_fail, code", [
    (True, False, pi.CODE_SUPPRIME),       # landed, answer lost: settled
    (False, False, pi.CODE_INCOMPLET),     # did not land: the entry stays
    (True, True, pi.CODE_INCERTAIN),       # unknown
    (False, True, pi.CODE_INCERTAIN),
])
def test_a_record_delete_whose_commit_raised_is_settled_by_re_reading(
        world, monkeypatch, logged, apply, reads_fail, code):
    """« id proof: record delete failed after erase » is logged BEFORE the
    settling re-read, so the ERROR rides beside EVERY outcome — the
    ``supprime`` the route journals included. A pin of the pairing the
    registry states (review of lot 2, lifecycle F1)."""
    record, _ = _seed_proof(world)
    _break_commit(monkeypatch, world.fake, kind="delete", apply=apply,
                  reads_fail=reads_fail)
    out = pi.delete_proof(PID, PROOF, expected_etag=record["etag"])
    assert out.code == code
    assert logged == [("id proof: record delete failed after erase", {
        "partie_id": PID, "proof_id": PROOF,
        "error_type": "ServiceUnavailable"})]
    assert world.bucket.objects == {}             # the file went first
    if code == pi.CODE_SUPPRIME:
        assert out.doc["id"] == PROOF and _records(world) == {}
    elif code == pi.CODE_INCOMPLET:
        assert out.errors == [pi.DELETE_INCOMPLETE]
        assert PROOF in _records(world)
    else:
        assert out.errors == [pi.DELETE_UNCERTAIN]


def test_a_record_delete_that_staged_nothing_is_deja_once_another_removed_it(
        world, monkeypatch, logged):
    """The transaction's read raised — nothing staged — while another
    call's delete landed: the re-read finds the record gone, this call
    answers ``deja`` (the other one journals), beside the same ERROR."""
    record, _ = _seed_proof(world)
    server = world.fake._fake_server
    real_get = server.batch_get_documents
    fired = []

    def _get(request, metadata=None, **kwargs):
        if request.get("transaction") and not fired:
            fired.append(True)
            world.fake.external_delete(f"parties/{PID}/pieces_identite/{PROOF}")
            raise gexc.ServiceUnavailable("read answer lost")
        return real_get(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", _get)
    out = pi.delete_proof(PID, PROOF, expected_etag=record["etag"])
    assert fired
    assert out == pi.Outcome(None, [pi.ALREADY_DELETED], pi.CODE_DEJA)
    assert [m for m, _ in logged] == [
        "id proof: record delete failed after erase"]


def test_an_erase_whose_answer_was_lost_still_deletes_beside_its_error(
        world, monkeypatch, logged):
    """The DELETE landed and its answer was lost past the client's retries
    (google-cloud-storage's DEFAULT_RETRY re-sends a DeadlineExceeded; a
    RetryError surfaces once its budget is spent): « id proof: erase
    failed » is logged, the follow-up reload finds the object gone, and the
    deletion goes on to ``supprime`` — so that ERROR can precede the
    journaled outcome (review of lot 2, lifecycle F1)."""
    record, _ = _seed_proof(world)
    real = FakeBlob.delete

    def _landed_then_lost(self, client=None, if_generation_match=None,
                          **kwargs):
        real(self, client=client, if_generation_match=if_generation_match,
             **kwargs)
        raise gexc.RetryError("Timeout of 120.0s exceeded",
                              gexc.ServiceUnavailable("answer lost"))

    monkeypatch.setattr(FakeBlob, "delete", _landed_then_lost)
    out = pi.delete_proof(PID, PROOF, expected_etag=record["etag"])
    assert out.code == pi.CODE_SUPPRIME and out.doc["id"] == PROOF
    assert logged == [("id proof: erase failed", {
        "partie_id": PID, "proof_id": PROOF, "error_type": "RetryError"})]
    assert world.bucket.objects == {} and _records(world) == {}


def test_a_rename_landing_after_the_etag_check_is_deleted_with_the_proof(
        world, monkeypatch):
    """The declared exception to Rule 7: the deletion's etag guards its
    ERASE — compared on the strict read just before it, since a GCS erase
    cannot join a Firestore transaction — not its record delete, which
    checks presence only ON PURPOSE: refusing once the file is erased
    would leave a listed proof with no file. A rename landing in that
    window goes with the proof — same id, same bytes (review of lot 2,
    lifecycle F2). An etag check added to the record delete fails here."""
    record, _ = _seed_proof(world)
    real = FakeBlob.delete
    renamed = []

    def _delete(self, client=None, if_generation_match=None, **kwargs):
        if self.name == _final() and not renamed:
            renamed.append(pi.rename_proof(PID, PROOF, "Passeport",
                                           expected_etag=record["etag"]))
        return real(self, client=client,
                    if_generation_match=if_generation_match, **kwargs)

    monkeypatch.setattr(FakeBlob, "delete", _delete)
    out = pi.delete_proof(PID, PROOF, expected_etag=record["etag"])
    assert renamed[0].code == pi.CODE_MODIFIE
    assert out.code == pi.CODE_SUPPRIME and out.errors == []
    # The row deleted is the renamed one: a version the form never asserted.
    assert out.doc["etag"] != record["etag"]
    assert out.doc["etag"] == renamed[0].doc["etag"]
    assert out.doc["display_name"] == "Passeport"
    assert _records(world) == {} and world.bucket.objects == {}


def test_an_erase_that_fails_deletes_nothing_and_says_so(
        world, monkeypatch, logged):
    record, obj = _seed_proof(world)

    def _refuse(self, **_kw):
        raise gexc.ServiceUnavailable("gcs down")

    monkeypatch.setattr(FakeBlob, "delete", _refuse)
    out = pi.delete_proof(PID, PROOF, expected_etag=record["etag"])
    assert out == pi.Outcome(None, [pi.ERASE_FAILED], pi.CODE_FICHIER)
    assert world.bucket.objects[_final()] is obj
    assert _records(world)[PROOF] == record
    assert [m for m, _ in logged] == ["id proof: erase failed"]
    _assert_ids_only(logged)


def test_an_object_that_cannot_be_reloaded_is_never_erased(world, monkeypatch):
    record, obj = _seed_proof(world)
    monkeypatch.setattr(FakeBlob, "reload", _boom)
    out = pi.delete_proof(PID, PROOF, expected_etag=record["etag"])
    assert out == pi.Outcome(None, [pi.ERASE_FAILED], pi.CODE_FICHIER)
    assert world.bucket.objects[_final()] is obj and PROOF in _records(world)


def test_an_erase_whose_outcome_cannot_be_told_is_uncertain(world, monkeypatch):
    record, _ = _seed_proof(world)
    real_reload = FakeBlob.reload
    calls = []

    def _reload(self, *a, **kw):
        calls.append(self.name)
        if len(calls) > 1:
            raise gexc.ServiceUnavailable("gcs down")
        return real_reload(self, *a, **kw)

    def _delete(self, **_kw):
        raise gexc.DeadlineExceeded("answer lost")

    monkeypatch.setattr(FakeBlob, "reload", _reload)
    monkeypatch.setattr(FakeBlob, "delete", _delete)
    out = pi.delete_proof(PID, PROOF, expected_etag=record["etag"])
    assert out == pi.Outcome(None, [pi.DELETE_UNCERTAIN], pi.CODE_INCERTAIN)
    assert PROOF in _records(world)                 # the record stays


def test_a_stored_path_of_another_shape_is_never_erased(world, logged, deletes):
    elsewhere = f"users/{UID}/dossiers/d1/{PROOF}.pdf"   # a document's object
    world.bucket.put(elsewhere, PDF)
    record, _ = _seed_proof(world, data=None, path=elsewhere)
    out = pi.delete_proof(PID, PROOF, expected_etag=record["etag"])
    assert out == pi.Outcome(None, [pi.PATH_REFUSED], pi.CODE_CHEMIN)
    assert elsewhere in world.bucket.objects and deletes == []
    assert _records(world)[PROOF] == record
    assert logged == [("id proof: stored path refused",
                       {"partie_id": PID, "proof_id": PROOF})]


def test_a_proof_filed_under_another_uid_is_still_deleted(world):
    """The uid segment is validated, never compared with the session's."""
    path = _final(uid=OTHER_UID)
    record, _ = _seed_proof(world, path=path)
    out = pi.delete_proof(PID, PROOF, expected_etag=record["etag"])
    assert out.code == pi.CODE_SUPPRIME
    assert path not in world.bucket.objects and _records(world) == {}


def test_a_deletion_read_failure_is_unavailable(world, monkeypatch, logged):
    _seed_proof(world)
    monkeypatch.setattr(world.fake._fake_server, "batch_get_documents", _boom)
    out = pi.delete_proof(PID, PROOF, expected_etag="e")
    assert out == pi.Outcome(None, [concurrency.READ_UNAVAILABLE_ERROR],
                             pi.CODE_INDISPONIBLE)
    assert _final() in world.bucket.objects


# ══════════════════════════════════════════════════════════════════════
# 8. « Consulter »
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def signed(monkeypatch):
    captured: dict = {}

    def _sign(blob, params, expiry_minutes=15):
        captured.update(name=blob.name, generation=blob.generation,
                        params=dict(params), minutes=expiry_minutes)
        return "https://signed.example/preuve"

    monkeypatch.setattr(pi, "sign_blob_url", _sign)
    return captured


@pytest.mark.parametrize("display, ext, disposition", [
    ("Permis de conduire", ".jpg",
     'inline; filename="Permis de conduire.jpg"'),
    ("scan.PDF", ".pdf", 'inline; filename="scan.PDF"'),        # not doubled
    ("photo.jpeg", ".jpg", 'inline; filename="photo.jpeg"'),    # same type
    ("rapport.pdf", ".png", 'inline; filename="rapport.pdf.png"'),
    ("Passeport émis", ".pdf",
     "inline; filename=\"Passeport mis.pdf\"; "
     "filename*=UTF-8''Passeport%20%C3%A9mis.pdf"),
])
def test_consult_signs_a_15_minute_inline_url(world, signed, display, ext,
                                              disposition):
    record, obj = _seed_proof(world, ext=ext, display=display)
    url = pi.consult_url(PID, pi.get_proof_strict(PID, PROOF))
    assert url == "https://signed.example/preuve"
    assert signed == {
        "name": _final(ext=ext), "generation": obj.generation,
        "params": {
            "response-content-disposition": disposition,
            "response-content-type": pi.MIME_TYPES[ext],
        },
        "minutes": 15,
    }


def test_a_stored_name_the_rules_refuse_never_reaches_the_header(world, signed):
    _seed_proof(world, display="Permis\r\nSet-Cookie: x")
    pi.consult_url(PID, pi.get_proof_strict(PID, PROOF))
    disposition = signed["params"]["response-content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert "Set-Cookie" not in disposition
    assert disposition == document_model.content_disposition(
        pi.NEUTRAL_NAME + ".pdf", inline=True)


def test_the_neutral_name_names_no_one():
    """The deletion trail's title and the consult fallback: a fixed label,
    never a name typed by hand (D6)."""
    assert pi.NEUTRAL_NAME == "Preuve d'identité"
    assert pi.display_name_errors(pi.NEUTRAL_NAME) == []


def test_consulting_a_proof_whose_file_is_gone_raises_object_missing(
        world, signed):
    _seed_proof(world, data=None)
    with pytest.raises(pi.ObjectMissing):
        pi.consult_url(PID, pi.get_proof_strict(PID, PROOF))
    assert signed == {}


def test_consulting_a_refused_path_signs_nothing(world, signed):
    elsewhere = f"users/{UID}/dossiers/d1/{PROOF}.pdf"
    world.bucket.put(elsewhere, PDF)
    _seed_proof(world, data=None, path=elsewhere)
    with pytest.raises(pi.StoredPathRefused):
        pi.consult_url(PID, pi.get_proof_strict(PID, PROOF))
    assert signed == {}


def test_a_signing_failure_propagates_to_the_caller(world, monkeypatch):
    _seed_proof(world)

    def _sign(*_a, **_kw):
        raise RuntimeError("signBlob denied")

    monkeypatch.setattr(pi, "sign_blob_url", _sign)
    with pytest.raises(RuntimeError):
        pi.consult_url(PID, pi.get_proof_strict(PID, PROOF))


def test_the_inline_disposition_shares_the_attachment_body():
    """``content_disposition`` is the one body: the attachment form is
    byte-identical to what ``build_attachment_disposition`` always gave."""
    for name in ("piece.pdf", "Pièces jointes.zip", 'a"b.pdf', "", None):
        attachment = document_model.build_attachment_disposition(name)
        assert attachment == document_model.content_disposition(name)
        inline = document_model.content_disposition(name, inline=True)
        assert attachment.startswith("attachment; ")
        assert inline == "inline; " + attachment[len("attachment; "):]
    assert document_model.build_attachment_disposition("piece.pdf") == (
        'attachment; filename="piece.pdf"')


# ══════════════════════════════════════════════════════════════════════
# 9. The contact document and its CTag are never touched
# ══════════════════════════════════════════════════════════════════════


def test_proof_writes_never_touch_the_contact_nor_its_ctag(world):
    before = (world.fake.peek(f"parties/{PID}"),
              world.fake.stored_update_time(f"parties/{PID}"),
              world.fake.peek("dav_sync/parties"),
              world.fake.stored_update_time("dav_sync/parties"))
    out, _objet, proof_id = _file(world)
    renamed = pi.rename_proof(PID, proof_id, "Passeport",
                              expected_etag=out.doc["etag"])
    assert renamed.code == pi.CODE_MODIFIE
    deleted = pi.delete_proof(PID, proof_id, expected_etag=renamed.doc["etag"])
    assert deleted.code == pi.CODE_SUPPRIME
    after = (world.fake.peek(f"parties/{PID}"),
             world.fake.stored_update_time(f"parties/{PID}"),
             world.fake.peek("dav_sync/parties"),
             world.fake.stored_update_time("dav_sync/parties"))
    assert after == before
    touched = {path for commit in world.fake.commits for _, path in commit.ops}
    assert touched == {f"parties/{PID}/pieces_identite/{proof_id}"}
