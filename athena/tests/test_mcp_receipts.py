"""The connector's pièces justificatives (2026-11-10): begin_upload →
PUT → finalize_upload with purpose « piece_justificative » — attach, or
REPLACE with the replaced file KEPT — and the read get_admin_receipt, whose
photo reaches Claude as an MCP image item, never inside structuredContent.

Real handlers, write protocol, ticket model, accounting service, receipt
door (models/admin_ledger.attach_uploaded_receipt) and dossier-copy service
(services/pieces_justificatives), over the shared fake Firestore
(tests/_fake_firestore.py) and the fake GCS (tests/_fake_gcs.py), which
keep bytes and generations and refuse what the services refuse. Synthetic
data only (the repository is public): a fictional year, invented amounts,
.example addresses, generated images.

Pinned:

1. begin_upload binds the entry and its receipt AS READ; a refusal opens
   nothing; replacing names the receipt replaced (its MD5 — a legacy
   receipt's object's own), the target state, never a toggle;
2. finalize_upload attaches through the model's door, files the dossier
   copy under the ticket's reserved id, keeps a replaced file byte-
   identical, refuses « stale » what the web changed since the opening,
   answers a filed ticket again without writing, and settles an uncertain
   outcome on a reclaim — one receipt, one revision, one copy;
3. get_admin_receipt reads each kind honestly (text, image, or « none »
   with a French reason), writes nothing, never carries a path, and its
   image leaves the payload for the MCP image item — under 1 MiB whole;
4. the tables that say what a purpose and a receipt may be are the
   models' own.
"""

import ast
import base64
import hashlib
import io
import json
import math
import os
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync
    import mcp.bearer as bearer
    import mcp.handlers as handlers
    import mcp.store as store
    import mcp.tools as tools
    import mcp.write_support as write_support
    import models.admin_ledger as al
    import models.document as document_model
    import models.dossier as dossier_model
    import services.comptabilite as comptabilite
    from mcp.output_schemas import OUTPUT_SCHEMAS
    from models import upload_ticket as ut
    from utils import receipt_image, storage_identity

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402
from tests._fake_msg import build_msg  # noqa: E402
from tests._ingest_faults import unreadable_documents  # noqa: E402
from tests.test_email_text import _eml  # noqa: E402
from tests.test_mcp_jsonrpc import _make_app, _rpc, _token_doc  # noqa: E402
from tests.test_mcp_output_schemas import _conforms  # noqa: E402
from tests.test_pdf_text import _encrypted_pdf, _pdf  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it.
_ = (dav_sync, dossier_model, write_support)

ToolArgumentError = tools.ToolArgumentError
UTC = timezone.utc
NOW = datetime(2031, 11, 4, 15, 0, tzinfo=UTC)
UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"
TX = "5b0c6f1e-2a3d-4c4e-9f5a-6b7c8d9e0f1a"
ABSENT = "0e1d2c3b-4a59-4687-8b9a-0c1d2e3f4a5b"
WEB_UPLOAD = "7c6b5a49-3827-4615-a4b3-c2d1e0f9a8b7"
OLD = f"users/{UID}/administration/{TX}/{WEB_UPLOAD}/recu.pdf"
PDF = b"%PDF-1.7\n" + b"facture fournisseur " * 20
PDF2 = b"%PDF-1.7\n" + b"autre facture " * 30
_PAST_THE_HOUR = NOW + ut.OPEN_WINDOW + ut.STALE_CLAIM_AFTER + timedelta(minutes=1)


def _md5(data: bytes) -> str:
    # GCS's content MD5 — an integrity check, not a security hash.
    return base64.b64encode(
        hashlib.md5(data, usedforsecurity=False).digest()).decode()


def _photo(width: int = 2400, height: int = 1800) -> bytes:
    """A phone-sized JPEG: a paper-white page ruled with dark lines."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), (250, 248, 240))
    draw = ImageDraw.Draw(image)
    for y in range(150, height - 150, 90):
        draw.rectangle([150, y, width - 150, y + 24], fill=(40, 40, 40))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=90)
    return out.getvalue()


def _entry(**over) -> dict:
    doc = {
        "id": TX, "account_id": "ops1", "sequence": 7,
        "date": datetime(2031, 9, 12, tzinfo=UTC),
        "direction": "déboursé", "kind": "dépense", "amount": 11498,
        "net_amount": 10000, "gst_amount": 500, "qst_amount": 998,
        "category": "fournitures", "counterparty": "Fournisseur A",
        "description": "", "reference": "", "supplier_invoice_ref": "F-1",
        "method": "virement", "dossier_id": "d1",
        "dossier_file_number": "2031-001", "dossier_title": "Alpha c. Bêta",
        "invoice_id": None, "invoice_number": "", "trust_transaction_id": None,
        "receipt_storage_path": None, "receipt_filename": "",
        "receipt_file_type": "", "receipt_file_size": 0, "receipt_md5": "",
        "status": "en_circulation", "cleared_date": None,
        "reconciliation_id": None, "reverses_id": None, "reversed_by_id": None,
        "related_transaction_id": None, "revisions": [],
        "created_at": datetime(2031, 9, 12, tzinfo=UTC),
        "updated_at": datetime(2031, 9, 12, tzinfo=UTC), "etag": "e0",
        "created_via": "web", "updated_via": "web",
    }
    doc.update(over)
    return doc


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n in ("dav.sync", "mcp.write_support"))
            and getattr(m, "db", None) is not None]


@pytest.fixture
def world(monkeypatch):
    db = install(monkeypatch, *_fake_modules())
    bucket = FakeBucket()
    monkeypatch.setattr(document_model.storage, "bucket", lambda: bucket)
    monkeypatch.setattr(storage_identity, "owner_uid", lambda: UID)
    clock = {"now": NOW}
    monkeypatch.setattr(ut, "_now", lambda: clock["now"])
    db.seed("dossiers/d1", {
        "id": "d1", "file_number": "2031-001", "title": "Alpha c. Bêta",
        "clients": [], "client_ids": [], "opposing_parties": [],
        "opposing_party_ids": [], "status": "actif"})
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry())
    db.reset_logs()
    return {"db": db, "bucket": bucket, "clock": clock}


@pytest.fixture
def events(monkeypatch):
    import utils.logging_setup as logging_setup

    seen: list = []
    monkeypatch.setattr(logging_setup, "log_mcp_event",
                        lambda event, outcome, **kw: seen.append((event, outcome, kw)))
    return seen


def _with_receipt(world, data: bytes = PDF, *, path: str = OLD,
                  file_type: str = "application/pdf",
                  filename: str = "recu.pdf", md5: bool = True, **over) -> None:
    """The entry carries *data* as its receipt — attached on the web."""
    world["bucket"].put(path, data, content_type=file_type,
                        content_disposition="attachment")
    world["db"].seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry(**{
        "receipt_storage_path": path, "receipt_filename": filename,
        "receipt_file_type": file_type, "receipt_file_size": len(data),
        "receipt_md5": _md5(data) if md5 else "", **over}))
    world["db"].reset_logs()


def _begin(data: bytes = PDF, **over) -> dict:
    args = {"purpose": "piece_justificative", "tx_id": TX,
            "filename": "Facture F-1.pdf", "size_bytes": len(data),
            "md5_base64": _md5(data)}
    args.update(over)
    return handlers.begin_upload({k: v for k, v in args.items() if v is not None})


def _ticket(world, ticket_id: str) -> dict:
    return world["db"].peek(f"{ut.COLLECTION}/{ticket_id}")


def _put(world, opened: dict, data: bytes) -> None:
    world["bucket"].complete_session(opened["upload_url"], data)


def _stored(world) -> dict:
    return world["db"].peek(f"{al.TRANSACTIONS_COLLECTION}/{TX}")


def _dest(ticket_id: str, ext: str = ".pdf") -> str:
    return f"users/{UID}/administration/{TX}/pieces/{ticket_id}{ext}"


def _attach(world, data: bytes = PDF, **over) -> tuple[dict, dict]:
    opened = _begin(data, **over)
    _put(world, opened, data)
    return opened, handlers.finalize_upload({"ticket_id": opened["ticket_id"]})


def _no_path_in(payload) -> None:
    text = json.dumps(tools._jsonable(payload), ensure_ascii=False)
    assert "users/" not in text and "/administration/" not in text


# ══════════════════════════════════════════════════════════════════════
# 1. begin_upload
# ══════════════════════════════════════════════════════════════════════


def test_begin_binds_the_entry_and_its_receipt_as_read(world, events):
    opened = _begin()
    ticket = _ticket(world, opened["ticket_id"])
    assert ticket["purpose"] == ut.PURPOSE_PIECE_JUSTIFICATIVE
    assert ticket["transaction_id"] == TX
    assert ticket["receipt_params"] == {"expected_path": "", "expected_md5": ""}
    assert ticket["reserved_document_id"]        # the dossier copy's id
    assert "dossier_id" not in ticket or not ticket["dossier_id"]
    session = world["bucket"].sessions[opened["upload_url"]]
    assert session["content_type"] == "application/pdf"
    assert session["if_generation_match"] == 0
    assert session["md5_hash"] == _md5(PDF) and session["size"] == len(PDF)
    assert opened["headers"]["Content-Type"] == "application/pdf"
    assert opened["entity"] == {"id": opened["ticket_id"], "dossier_id": ""}
    assert any("2031-001" in w and "Mandat › Déboursés" in w
               for w in opened["warnings"])
    assert not any("remplacée" in w for w in opened["warnings"])
    _conforms("begin_upload", opened)
    ((name, outcome, fields),) = [e for e in events if e[0] == "mcp_upload_opened"]
    assert outcome == "success"
    assert fields["transaction_id"] == TX and fields["replaces"] is False
    assert "Facture" not in json.dumps(fields, default=str)


@pytest.mark.parametrize("filename, mime", [
    ("Facture.eml", "message/rfc822"),
    ("Facture.MSG", "application/vnd.ms-outlook"),
    ("Recu.jpg", "image/jpeg"),
    ("Recu.png", "image/png"),
    ("Recu.tiff", "image/tiff"),
])
def test_each_receipt_type_opens_with_its_own_content_type(world, filename, mime):
    opened = _begin(filename=filename)
    assert world["bucket"].sessions[opened["upload_url"]]["content_type"] == mime
    assert opened["headers"]["Content-Type"] == mime


@pytest.mark.parametrize("over, fragment", [
    ({"tx_id": None}, "`tx_id` est requis"),
    ({"tx_id": "a/b"}, "`tx_id` est requis"),
    ({"tx_id": ABSENT}, "Aucune écriture d'administration"),
    ({"filename": "Facture.docx"}, al.RECEIPT_TYPE_REFUSED_FR),
    ({"filename": "Facture"}, al.RECEIPT_TYPE_REFUSED_FR),
    ({"size_bytes": al.MAX_RECEIPT_SIZE + 1},
     "10 Mo pour une pièce justificative"),
    ({"dossier_id": "d1"}, "ne s'applique pas"),
    ({"category": "facture"}, "ne s'applique pas"),
    ({"template_mode": "create"}, "ne s'applique pas"),
    ({"replaces_receipt_md5": _md5(PDF)}, "aucune pièce justificative"),
])
def test_a_receipt_opening_refuses_and_writes_nothing(world, over, fragment):
    before = _stored(world)
    with pytest.raises(ToolArgumentError, match=re.escape(fragment)) as exc:
        _begin(**over)
    assert "Rien n'a été ouvert" in str(exc.value)
    assert world["db"].peek_collection(ut.COLLECTION) == {}
    assert world["bucket"].sessions == {}
    assert _stored(world) == before


def test_another_purpose_never_takes_a_receipt_argument(world):
    with pytest.raises(ToolArgumentError, match="ne s'applique pas"):
        handlers.begin_upload({
            "purpose": "document", "dossier_id": "d1", "tx_id": TX,
            "filename": "a.pdf", "size_bytes": len(PDF),
            "md5_base64": _md5(PDF)})
    with pytest.raises(ToolArgumentError, match="piece_justificative"):
        _begin(purpose="recu")
    assert world["db"].peek_collection(ut.COLLECTION) == {}


def test_a_receipt_in_place_is_replaced_only_by_naming_it(world, events):
    """The target state, never a toggle: an entry carrying a receipt opens
    a replacement only for the caller who names that receipt's MD5 — the
    one get_admin_ledger shows; any other is « stale »."""
    _with_receipt(world)
    with pytest.raises(ToolArgumentError, match="replaces_receipt_md5") as exc:
        _begin(PDF2)
    assert "recu.pdf" in str(exc.value) and "l'ancienne reste conservée" in str(exc.value)
    with pytest.raises(ToolArgumentError) as stale:
        _begin(PDF2, replaces_receipt_md5=_md5(PDF2))
    assert stale.value.reason == "stale_etag"
    assert world["db"].peek_collection(ut.COLLECTION) == {}
    opened = _begin(PDF2, replaces_receipt_md5=_md5(PDF))
    assert _ticket(world, opened["ticket_id"])["receipt_params"] == {
        "expected_path": OLD, "expected_md5": _md5(PDF)}
    assert any("remplacée ; son fichier reste conservé" in w
               for w in opened["warnings"])
    (fields,) = [kw for e, _, kw in events if e == "mcp_upload_opened"]
    assert fields["replaces"] is True


def test_a_legacy_receipt_is_named_by_its_object_s_own_md5(world):
    """A receipt attached before receipt_md5 existed: the caller names the
    MD5 get_admin_ledger cannot show — its object's, read now — and the
    ticket binds the entry as stored (no digest)."""
    _with_receipt(world, md5=False)
    opened = _begin(PDF2, replaces_receipt_md5=_md5(PDF))
    assert _ticket(world, opened["ticket_id"])["receipt_params"] == {
        "expected_path": OLD, "expected_md5": ""}
    with pytest.raises(ToolArgumentError) as stale:
        _begin(PDF2, replaces_receipt_md5=_md5(PDF2))
    assert stale.value.reason == "stale_etag"


def test_an_unreadable_entry_or_legacy_object_opens_nothing(world, monkeypatch):
    _with_receipt(world, md5=False)
    world["bucket"].remove(OLD)
    with pytest.raises(ToolArgumentError, match="Rien n'a été ouvert") as exc:
        _begin(PDF2, replaces_receipt_md5=_md5(PDF))
    assert exc.value.reason == "upload_retry"
    unreadable_documents(world["db"], monkeypatch,
                         f"{al.TRANSACTIONS_COLLECTION}/{TX}")
    with pytest.raises(ToolArgumentError, match="Rien n'a été ouvert") as exc:
        _begin()
    assert exc.value.reason == "upload_retry"
    assert world["db"].peek_collection(ut.COLLECTION) == {}
    assert world["bucket"].sessions == {}


# ══════════════════════════════════════════════════════════════════════
# 2. finalize_upload
# ══════════════════════════════════════════════════════════════════════


def test_a_receipt_is_attached_and_its_copy_filed_in_the_dossier(world, events):
    opened = _begin()
    ticket_id = opened["ticket_id"]
    ticket = _ticket(world, ticket_id)
    reserved, staging = ticket["reserved_document_id"], ticket["staging_object"]
    _put(world, opened, PDF)
    done = handlers.finalize_upload({"ticket_id": ticket_id})

    entry = _stored(world)
    assert entry["receipt_storage_path"] == _dest(ticket_id)
    assert entry["receipt_filename"] == "Facture F-1.pdf"
    assert entry["receipt_file_type"] == "application/pdf"
    assert entry["receipt_file_size"] == len(PDF)
    assert entry["receipt_md5"] == _md5(PDF)
    assert entry["updated_via"] == "mcp" and entry["etag"] != "e0"
    (revision,) = entry["revisions"]
    assert revision["via"] == "mcp"
    assert revision["motif"] == al.RECEIPT_ATTACHED_MOTIF
    assert world["bucket"].objects[_dest(ticket_id)].data == PDF
    assert staging not in world["bucket"].objects
    stored = _ticket(world, ticket_id)
    assert stored["status"] == ut.STATUS_DONE
    assert stored["result"] == {"transaction_id": TX}

    assert done["finalized"] is True and done["already_finalized"] is False
    assert done["purpose"] == "piece_justificative" and done["mode"] == "attach"
    assert done["entity_type"] == "admin_transaction"
    assert done["entity"]["id"] == TX and done["entity"]["dossier_id"] == "d1"
    assert done["file"] == {"size_bytes": len(PDF), "file_type": "application/pdf"}
    assert done["receipt"] == {
        "filename": "Facture F-1.pdf", "file_type": "application/pdf",
        "size_bytes": len(PDF), "md5_base64": _md5(PDF)}
    assert done["dossier_copy"] == {"status": "versee", "document_id": reserved,
                                    "dossier_id": "d1"}
    copy = world["db"].peek(f"documents/{reserved}")
    assert copy["dossier_id"] == "d1"
    assert world["bucket"].objects[copy["storage_path"]].data == PDF
    _conforms("finalize_upload", done)
    _no_path_in(done)

    (fields,) = [kw for e, _, kw in events if e == "mcp_upload_finalized"]
    assert fields["transaction_id"] == TX and fields["replaced"] is False
    assert fields["dossier_copy"] == "versee"
    assert fields["already_finalized"] is False
    assert "Facture" not in json.dumps(fields, default=str)


def test_an_entry_of_no_dossier_gets_no_copy(world):
    world["db"].seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry(
        dossier_id="", dossier_file_number="", dossier_title=""))
    opened = _begin()
    assert not any("Déboursés" in w for w in opened["warnings"])
    _put(world, opened, PDF)
    done = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert done["dossier_copy"] is None
    assert world["db"].peek_collection("documents") == {}
    _conforms("finalize_upload", done)


@pytest.mark.parametrize("filename, data, mime", [
    ("Facture.eml", None, "message/rfc822"),
    ("Facture.msg", None, "application/vnd.ms-outlook"),
    ("Recu.jpg", None, "image/jpeg"),
])
def test_an_email_or_a_photo_is_attached_with_its_own_type(world, filename,
                                                          data, mime):
    data = {
        "message/rfc822": _eml(text="Votre facture.\n"),
        "application/vnd.ms-outlook": build_msg(subject="Facture F-9",
                                                body="Montant : 57,49 $"),
        "image/jpeg": _photo(640, 480),
    }[mime]
    opened, done = _attach(world, data, filename=filename)
    ext = "." + filename.rsplit(".", 1)[1].lower()
    entry = _stored(world)
    assert entry["receipt_storage_path"] == _dest(opened["ticket_id"], ext)
    assert entry["receipt_file_type"] == mime
    assert done["file"]["file_type"] == mime
    obj = world["bucket"].objects[_dest(opened["ticket_id"], ext)]
    assert obj.data == data and obj.content_type == mime


def test_bytes_that_are_not_their_type_are_refused_and_discarded(world):
    """The shared sniff (models/document._sniff_header) judges the bytes,
    not the name: a PDF named .eml is refused, its staging consumed, the
    entry untouched."""
    opened = _begin(PDF, filename="Facture.eml")
    staging = _ticket(world, opened["ticket_id"])["staging_object"]
    _put(world, opened, PDF)
    with pytest.raises(ToolArgumentError):
        handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert _ticket(world, opened["ticket_id"])["status"] == ut.STATUS_REFUSED
    assert staging not in world["bucket"].objects
    assert _stored(world)["receipt_storage_path"] is None
    assert _stored(world)["revisions"] == []


def test_a_replacement_keeps_the_previous_file_byte_identical(world, events):
    _with_receipt(world)
    opened, done = _attach(world, PDF2, replaces_receipt_md5=_md5(PDF))
    dest = _dest(opened["ticket_id"])
    assert done["mode"] == "replace"
    assert any("son fichier reste conservé" in w for w in done["warnings"])
    assert world["bucket"].objects[OLD].data == PDF           # KEPT
    assert world["bucket"].objects[dest].data == PDF2
    entry = _stored(world)
    assert entry["receipt_storage_path"] == dest
    assert entry["receipt_md5"] == _md5(PDF2)
    (revision,) = entry["revisions"]
    assert revision["motif"] == al.RECEIPT_REPLACED_MOTIF
    assert revision["changes"]["receipt_storage_path"] == [OLD, dest]
    assert done["receipt"]["md5_base64"] == _md5(PDF2)
    _conforms("finalize_upload", done)
    (fields,) = [kw for e, _, kw in events if e == "mcp_upload_finalized"]
    assert fields["replaced"] is True


def test_a_receipt_changed_on_the_web_since_the_opening_refuses_stale(world):
    """The lawyer attached a receipt in the application between the opening
    and the finalization: the connector's ticket — bound to « no receipt »
    — refuses « stale », erases its bytes, and the lawyer's receipt stands."""
    opened = _begin()
    staging = _ticket(world, opened["ticket_id"])["staging_object"]
    _put(world, opened, PDF)
    web = f"users/{UID}/administration/{TX}/{WEB_UPLOAD}/web.pdf"
    world["bucket"].put(web, PDF2, content_type="application/pdf")
    _, errors = al.attach_receipt(TX, web, "web.pdf", "application/pdf",
                                  len(PDF2), md5=_md5(PDF2))
    assert errors == []
    with pytest.raises(ToolArgumentError, match="relisez get_admin_ledger") as exc:
        handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert exc.value.reason == "stale_etag"
    assert _stored(world)["receipt_storage_path"] == web
    ticket = _ticket(world, opened["ticket_id"])
    assert ticket["status"] == ut.STATUS_REFUSED
    assert ticket["refusal_reason"] == "piece_modifiee"
    assert staging not in world["bucket"].objects
    assert _dest(opened["ticket_id"]) not in world["bucket"].objects
    assert world["db"].peek_collection("documents") == {}


def test_a_filed_ticket_answers_again_and_writes_nothing(world):
    opened, first = _attach(world)
    world["db"].reset_logs()
    again = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    # The claim's read transaction commits — empty: no write.
    assert [c.ops for c in world["db"].commits if c.ops] == []
    assert again["already_finalized"] is True
    assert again["entity"]["id"] == TX and again["receipt"] == first["receipt"]
    assert again["warnings"] == []
    assert len(_stored(world)["revisions"]) == 1
    assert len(world["db"].peek_collection("documents")) == 1
    _conforms("finalize_upload", again)
    # A later replacement: the first ticket's answer says so, and its file
    # is still there.
    second, _ = _attach(world, PDF2, replaces_receipt_md5=_md5(PDF))
    replay = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert replay["receipt"]["md5_base64"] == _md5(PDF2)
    assert any("remplacée depuis" in w for w in replay["warnings"])
    assert world["bucket"].objects[_dest(opened["ticket_id"])].data == PDF
    _conforms("finalize_upload", replay)


def _lose_the_entry_s_answer(world, monkeypatch, *, land: bool) -> dict:
    """The first commit writing the entry LANDS — or not, ``land=False`` —
    then raises, as a connection reset after (or before) the server's
    commit would; from then on every read of the entry fails, until the
    test sets ``state["dead"] = False``: the store answers again."""
    server = world["db"]._fake_server
    real_commit, real_get = server.commit, server.batch_get_documents
    target = f"{al.TRANSACTIONS_COLLECTION}/{TX}"
    state = {"armed": True, "dead": False}

    def _commit(request, metadata=None, **kwargs):
        writes = request.get("writes") or []
        if state["armed"] and any(
                server.rel(server._write_name(getattr(w, "_pb", w))) == target
                for w in writes):
            state["armed"], state["dead"] = False, True
            if land:
                real_commit(request, metadata=metadata, **kwargs)
            raise RuntimeError("connection reset")
        return real_commit(request, metadata=metadata, **kwargs)

    def _get(request, metadata=None, **kwargs):
        if state["dead"] and any(server.rel(str(name)) == target
                                 for name in request.get("documents") or []):
            raise RuntimeError("firestore unreadable")
        return real_get(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "commit", _commit)
    monkeypatch.setattr(server, "batch_get_documents", _get)
    return state


@pytest.mark.parametrize("landed", [True, False])
def test_an_uncertain_attach_keeps_the_claim_and_a_reclaim_settles_it(
    world, monkeypatch, landed,
):
    """The attach's commit LANDED — or not —, its answer was lost and the
    entry cannot be re-read. Never « rien n'a été joint »: the ticket stays
    held, its bytes kept, and the reclaim past the hour finds the receipt
    (or attaches it, adopting the object the first attempt wrote) — ONE
    receipt, ONE revision, ONE dossier copy."""
    opened = _begin()
    ticket_id = opened["ticket_id"]
    staging = _ticket(world, ticket_id)["staging_object"]
    world["bucket"].put(staging, PDF, content_type="application/pdf",
                        time_created=NOW + timedelta(minutes=1))
    state = _lose_the_entry_s_answer(world, monkeypatch, land=landed)
    with pytest.raises(ToolArgumentError) as exc:
        handlers.finalize_upload({"ticket_id": ticket_id,
                                  "idempotency_key": "piece-cle-1"})
    assert exc.value.reason == "write_outcome_uncertain"
    assert exc.value.keep_claim is True
    assert TX in str(exc.value) and "MÊME ticket_id" in str(exc.value)
    assert "rien n'a été" not in str(exc.value).lower()
    assert _ticket(world, ticket_id)["status"] == ut.STATUS_CLAIMED
    assert staging in world["bucket"].objects
    assert bool(_stored(world)["receipt_storage_path"]) is landed

    state["dead"] = False                                 # the store answers
    world["clock"]["now"] = _PAST_THE_HOUR
    done = handlers.finalize_upload({"ticket_id": ticket_id,
                                     "idempotency_key": "piece-cle-2"})
    assert done["already_finalized"] is landed
    entry = _stored(world)
    assert entry["receipt_storage_path"] == _dest(ticket_id)
    assert len(entry["revisions"]) == 1
    assert len(world["db"].peek_collection("documents")) == 1
    assert _ticket(world, ticket_id)["status"] == ut.STATUS_DONE
    assert staging not in world["bucket"].objects
    _conforms("finalize_upload", done)


def test_a_failed_commit_keeps_the_bytes_for_a_retry(world):
    """The register refused the commit outright (nothing landed): the claim
    is released, the bytes kept, and the next finalize attaches — once."""
    opened = _begin()
    _put(world, opened, PDF)

    def _refuse_the_entry(info):
        if any(path == f"{al.TRANSACTIONS_COLLECTION}/{TX}"
               for _, path in info.ops):
            raise RuntimeError("unavailable")

    remove = world["db"].add_commit_hook(_refuse_the_entry)
    with pytest.raises(ToolArgumentError, match="rien n'a été versé") as exc:
        handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    remove()
    assert exc.value.reason == "upload_retry"
    assert _ticket(world, opened["ticket_id"])["status"] == ut.STATUS_OPEN
    assert _stored(world)["receipt_storage_path"] is None
    done = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    assert done["already_finalized"] is False
    assert len(_stored(world)["revisions"]) == 1


# ══════════════════════════════════════════════════════════════════════
# 3. get_admin_receipt
# ══════════════════════════════════════════════════════════════════════


def _read(tx_id: str = TX, **args) -> dict:
    return handlers.get_admin_receipt({"tx_id": tx_id, **args})


def _split(payload: dict):
    """What structuredContent carries — conformed, path-free — and the
    image the endpoint takes out of it."""
    clean, image = tools.split_image("get_admin_receipt", payload)
    _conforms("get_admin_receipt", clean)
    _no_path_in(clean)
    return clean, image


def _read_conformed(tx_id: str = TX, **args):
    """The REAL handler's answer as structuredContent carries it, validated
    against its output schema and path-free, and the image the endpoint
    takes out of it — every branch the tests below reach is a conformance
    run (tests/test_mcp_framework_guards finds the handler and _conforms
    named together here)."""
    payload = handlers.get_admin_receipt({"tx_id": tx_id, **args})
    clean, image = tools.split_image("get_admin_receipt", payload)
    _conforms("get_admin_receipt", clean)
    _no_path_in(clean)
    return clean, image


def test_an_unknown_or_blank_entry_is_not_found(world):
    for tx_id in (ABSENT, "", "a/b"):
        out, image = _read_conformed(tx_id)
        assert out == {"found": False, "tx_id": tx_id} and image is None


def test_an_entry_without_a_receipt_says_so(world):
    out, image = _read_conformed()
    assert out["found"] is True and out["has_receipt"] is False
    assert out["receipt"] is None and "content_kind" not in out
    assert out["entry"]["id"] == TX and image is None


def test_a_pdf_receipt_is_read_as_its_text_layer(world):
    _with_receipt(world, _pdf(["FACTURE F-1", "Total 114,98 $"]))
    out, image = _read_conformed()
    assert out["content_kind"] == "text" and out["email"] is None
    assert image is None
    text = json.dumps(out["pdf"], ensure_ascii=False)
    assert "FACTURE F-1" in text
    assert out["receipt"]["file_type"] == "application/pdf"
    assert out["dossier_copy_document_id"] is None
    later, _ = _read_conformed(page_range="2")
    later_text = json.dumps(later["pdf"], ensure_ascii=False)
    assert "114,98" in later_text and "FACTURE F-1" not in later_text


def test_an_emailed_receipt_is_read_with_its_pdf_attachment_s_text(world):
    eml = _eml(text="Bonjour,\nVotre facture est jointe.\n",
               attachments=[("F-1.pdf", "application", "pdf",
                             _pdf(["Total 114,98 $"]))])
    _with_receipt(world, eml, file_type="message/rfc822", filename="facture.eml")
    out, image = _read_conformed()
    assert out["content_kind"] == "text" and out["pdf"] is None and image is None
    mail = out["email"]
    assert mail["headers"]["subject"] == "Facture F-1"
    assert mail["body"].startswith("Bonjour,")
    ((attached),) = mail["attachment_texts"]
    assert attached["filename"] == "F-1.pdf" and "114,98" in attached["text"]


def test_an_outlook_receipt_is_read_as_text(world):
    msg = build_msg(subject="Facture F-9", sender="Fournisseur B",
                    sender_email="factures@fournisseur.example",
                    body="Montant : 57,49 $")
    _with_receipt(world, msg, file_type="application/vnd.ms-outlook",
                  filename="facture.msg")
    out, _ = _read_conformed()
    assert out["content_kind"] == "text"
    assert out["email"]["headers"]["subject"] == "Facture F-9"
    assert "57,49" in out["email"]["body"]


def test_a_photo_reaches_claude_as_the_image_itself_reduced(world):
    _with_receipt(world, _photo(), file_type="image/jpeg", filename="recu.jpg")
    payload = _read()
    assert isinstance(payload[tools.IMAGE_RESULT_KEY], tools.ImageAttachment)
    out, image = _split(payload)
    assert tools.IMAGE_RESULT_KEY not in out
    assert out["content_kind"] == "image"
    meta = out["image"]
    assert (meta["source_width"], meta["source_height"]) == (2400, 1800)
    assert max(meta["width"], meta["height"]) == receipt_image.LONG_SIDE
    assert image.mime_type == meta["mime_type"] == "image/jpeg"
    assert image.data[:2] == b"\xff\xd8"
    assert meta["size_bytes"] == len(image.data) <= tools.RECEIPT_IMAGE_MAX_BYTES


@pytest.mark.parametrize("setup, reason", [
    (lambda w: _with_receipt(w, b"II*\x00" + bytes(200), file_type="image/tiff",
                             filename="scan.tif"), "unsupported_type"),
    (lambda w: _with_receipt(w, PDF, receipt_file_size=al.MAX_RECEIPT_SIZE + 1),
     "too_large"),
    (lambda w: (_with_receipt(w), w["bucket"].remove(OLD)), "download_failed"),
    (lambda w: _with_receipt(w, PDF2, receipt_md5=_md5(PDF)), "receipt_changed"),
    (lambda w: _with_receipt(w, _encrypted_pdf()), "encrypted"),
    (lambda w: _with_receipt(w, bytes(600), file_type="application/vnd.ms-outlook",
                             filename="x.msg"), "invalid_msg"),
])
def test_a_receipt_it_cannot_transmit_is_said_honestly(world, setup, reason):
    setup(world)
    out, image = _read_conformed()
    assert out["content_kind"] == "none" and out["reason"] == reason
    assert out["message"] == handlers._RECEIPT_UNREADABLE_FR[reason]
    assert out["has_receipt"] is True and image is None


def test_a_photo_too_large_even_reduced_is_said_honestly(world, monkeypatch):
    _with_receipt(world, _photo(640, 480), file_type="image/jpeg",
                  filename="recu.jpg")

    def refuse(data, mime):
        raise receipt_image.ImageRefused("image_too_large")

    monkeypatch.setattr(receipt_image, "prepare_for_claude", refuse)
    out, image = _read_conformed()
    assert out["content_kind"] == "none" and out["reason"] == "image_too_large"
    assert image is None


def test_the_read_writes_nothing_and_names_the_dossier_copy(world):
    _, done = _attach(world)
    world["db"].reset_logs()
    out, _ = _read_conformed()
    assert world["db"].commits == []
    assert out["dossier_copy_document_id"] == done["dossier_copy"]["document_id"]
    assert out["receipt"] == done["receipt"]


def test_a_legacy_receipt_is_named_by_the_read_then_replaced(world):
    """A receipt attached before its digest was kept: the ledger's row shows
    md5_base64 null; get_admin_receipt gives the MD5 of the bytes it read —
    the object's own —, and that is what a replacement names."""
    _with_receipt(world, _pdf(["FACTURE F-2"]), md5=False)
    assert handlers._receipt_meta(_stored(world))["md5_base64"] is None
    out, _ = _read_conformed()
    legacy = out["receipt"]["md5_base64"]
    assert legacy == _md5(world["bucket"].objects[OLD].data)
    assert _stored(world)["receipt_md5"] == ""          # the read wrote nothing
    opened, done = _attach(world, PDF2, replaces_receipt_md5=legacy)
    assert done["mode"] == "replace"
    assert world["bucket"].objects[OLD].data.startswith(b"%PDF")   # kept
    # A TIFF is never read: its digest stays unknown here.
    world["db"].seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry())
    _with_receipt(world, b"II*\x00" + bytes(200), file_type="image/tiff",
                  filename="scan.tif", md5=False)
    tiff, _ = _read_conformed()
    assert tiff["reason"] == "unsupported_type"
    assert tiff["receipt"]["md5_base64"] is None


def test_an_unreadable_entry_is_refused_never_absent(world, monkeypatch):
    unreadable_documents(world["db"], monkeypatch,
                         f"{al.TRANSACTIONS_COLLECTION}/{TX}")
    with pytest.raises(ToolArgumentError) as exc:
        _read()
    assert exc.value.reason == "accounting_refused"


def test_the_photo_travels_as_an_image_item_never_in_structured_content(
    world, monkeypatch,
):
    """Through the real endpoint: content[0] the JSON text, content[1] the
    image — base64, its type — and structuredContent the same object as the
    text, without the image or its bytes. The whole answer stays under
    1 MiB (claude.ai's reported cap on a tool result)."""
    _with_receipt(world, _photo(), file_type="image/jpeg", filename="recu.jpg")
    monkeypatch.setattr(store, "get_token", lambda h: _token_doc("athena:read"))
    monkeypatch.setattr(store, "stamp_token_last_used", lambda h: None)
    bearer.reset_brake_state()
    try:
        client = _make_app().test_client()
        resp = _rpc(client, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_admin_receipt", "arguments": {"tx_id": TX}},
        }, headers={"MCP-Protocol-Version": "2025-06-18"})
    finally:
        bearer.reset_brake_state()
    assert resp.status_code == 200
    result = resp.get_json()["result"]
    assert result["isError"] is False
    text, image = result["content"]
    assert text["type"] == "text"
    assert image["type"] == "image" and image["mimeType"] == "image/jpeg"
    data = base64.b64decode(image["data"], validate=True)
    assert data[:2] == b"\xff\xd8" and len(data) <= tools.RECEIPT_IMAGE_MAX_BYTES
    structured = result["structuredContent"]
    assert structured["content_kind"] == "image"
    assert tools.IMAGE_RESULT_KEY not in structured
    assert json.loads(text["text"]) == structured
    assert image["data"][:200] not in text["text"]
    assert len(resp.data) < 1024 * 1024


def test_the_largest_image_still_fits_the_one_mebibyte_answer():
    """The arithmetic the cap rests on: the largest reduced image, base64,
    plus a generous allowance for the JSON around it, stays under 1 MiB."""
    encoded = math.ceil(tools.RECEIPT_IMAGE_MAX_BYTES / 3) * 4
    assert encoded + 64 * 1024 < 1024 * 1024


# ══════════════════════════════════════════════════════════════════════
# 4. The image side channel, and the tables
# ══════════════════════════════════════════════════════════════════════


def test_only_the_receipt_read_may_carry_an_image():
    """A read, allowlisted by name; a write never — whatever it returns."""
    assert tools.IMAGE_RESULT_TOOLS == {"get_admin_receipt"}
    assert not tools.IMAGE_RESULT_TOOLS & tools.WRITE_TOOLS
    assert tools.IMAGE_RESULT_TOOLS <= set(tools.TOOLS)
    image = tools.ImageAttachment("image/jpeg", b"\xff\xd8\xff\xe0data")
    for name in ("get_document_text", "finalize_upload", "create_note"):
        with pytest.raises(RuntimeError):
            tools.split_image(name, {"a": 1, tools.IMAGE_RESULT_KEY: image})
    payload = {"a": 1}
    assert tools.split_image("get_document_text", payload) == (payload, None)
    clean, got = tools.split_image(
        "get_admin_receipt", {"a": 1, tools.IMAGE_RESULT_KEY: image})
    assert clean == {"a": 1} and got is image
    with pytest.raises(RuntimeError):
        tools.split_image("get_admin_receipt",
                          {tools.IMAGE_RESULT_KEY: b"raw bytes"})


def test_an_image_attachment_is_bounded_and_typed():
    for mime, data in (("image/gif", b"GIF89a"), ("image/jpeg", b""),
                       ("image/jpeg", b"x" * (tools.RECEIPT_IMAGE_MAX_BYTES + 1)),
                       ("text/plain", b"abc")):
        with pytest.raises(ValueError):
            tools.ImageAttachment(mime, data)
    assert "data" not in repr(tools.ImageAttachment("image/png", b"\x89PNG"))


def test_the_image_item_follows_the_text_and_nowhere_else():
    image = tools.ImageAttachment("image/png", b"\x89PNGdata")
    result = tools.tool_result({"k": "v"}, "2025-06-18", image=image)
    assert [c["type"] for c in result["content"]] == ["text", "image"]
    assert result["content"][1] == {
        "type": "image", "data": base64.b64encode(b"\x89PNGdata").decode(),
        "mimeType": "image/png"}
    assert result["structuredContent"] == {"k": "v"}
    assert [c["type"] for c in tools.tool_result(
        {"k": "v"}, "2025-06-18")["content"]] == ["text"]


def test_only_the_receipt_handler_builds_an_image():
    """DERIVED over the handlers' top-level functions: the one that names
    the image key or builds an ImageAttachment is get_admin_receipt's."""
    tree = ast.parse(pathlib.Path(handlers.__file__).read_text(encoding="utf-8"))
    builders = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if names & {"ImageAttachment", "IMAGE_RESULT_KEY"}:
                builders.add(node.name)
    assert builders == {"get_admin_receipt"}


def test_the_purpose_tables_are_the_model_s():
    purposes = list(ut.VALID_PURPOSES)
    assert tools._UPLOAD_PURPOSES == purposes
    assert set(handlers._UPLOAD_ALLOWED_ARGS) == set(purposes)
    assert set(ut._PURPOSE_RULES) == set(purposes)
    schema = tools.TOOLS["begin_upload"]["input_schema"]["properties"]
    assert schema["purpose"]["enum"] == purposes
    out = OUTPUT_SCHEMAS["begin_upload"]
    assert purposes == json.loads(json.dumps(_enum_of(out, "purpose")))
    rule = ut._PURPOSE_RULES[ut.PURPOSE_PIECE_JUSTIFICATIVE]
    assert rule.extensions == frozenset(al.RECEIPT_EXTENSIONS)
    assert rule.max_bytes == al.MAX_RECEIPT_SIZE
    assert rule.content_types == al.RECEIPT_MIME_TYPES
    assert rule.reserves_document is True


def _enum_of(schema, key):
    """The enum of *key* wherever the schema declares it (first found)."""
    stack = [schema]
    while stack:
        node = stack.pop(0)
        if isinstance(node, dict):
            prop = (node.get("properties") or {}).get(key)
            if isinstance(prop, dict) and "enum" in prop:
                return prop["enum"]
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(node)
    raise AssertionError(f"no enum for {key}")


def test_the_receipt_limits_are_the_model_s():
    assert tools.UPLOAD_RECEIPT_MAX_BYTES == al.MAX_RECEIPT_SIZE
    assert comptabilite.MAX_RECEIPT_SIZE == al.MAX_RECEIPT_SIZE
    assert comptabilite.RECEIPT_MIME_TYPES == al.RECEIPT_MIME_TYPES
    assert comptabilite.RECEIPT_EXTENSIONS == al.RECEIPT_EXTENSIONS
    assert tools.RECEIPT_IMAGE_MAX_BYTES == receipt_image.OUTPUT_MAX_BYTES
    size = tools.TOOLS["begin_upload"]["input_schema"]["properties"]["size_bytes"]
    assert size["maximum"] >= al.MAX_RECEIPT_SIZE


def test_every_stored_receipt_type_has_its_reading():
    """Text, image, or — TIFF alone — « none »: a type added to the model
    fails here until the read says what it does with it."""
    stored = set(al.RECEIPT_MIME_TYPES.values())
    readable = handlers._RECEIPT_TEXT_TYPES | handlers._RECEIPT_IMAGE_TYPES
    assert readable <= stored
    assert stored - readable == {"image/tiff"}
    assert handlers._RECEIPT_IMAGE_TYPES <= tools.IMAGE_MIME_TYPES


def test_every_none_reason_has_its_french_sentence():
    branches = OUTPUT_SCHEMAS["get_admin_receipt"]["anyOf"]
    (none,) = [b for b in branches
               if (b["properties"].get("content_kind") or {}).get("enum") == ["none"]]
    assert set(none["properties"]["reason"]["enum"]) == set(
        handlers._RECEIPT_UNREADABLE_FR)
