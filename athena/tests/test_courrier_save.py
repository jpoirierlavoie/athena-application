"""Tests for Courrier Phase 3: QuickXorHash, GCS staging, OneDrive streaming & save_courriels."""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from urllib.parse import quote
from unittest import mock

import pytest
from google.cloud.storage._media.requests import ResumableUpload

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from config import Config
    import mcp.handlers as handlers
    import mcp.tools as tools
    from models import courrier as courrier_model
    from models import document as document_model
    from models import folder as folder_model
    from services import courrier as courrier_service
    from utils import graph_courrier, storage_identity
    from utils.quickxorhash import QuickXorHash, quickxorhash_b64

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402


def _fake_modules() -> list:
    return [
        m
        for n, m in sorted(sys.modules.items())
        if (n.startswith("models.") or n in ("dav.sync", "mcp.write_support"))
        and getattr(m, "db", None) is not None
    ]


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, *, headers=None, chunks=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = dict(headers or {})
        self._chunks = list(chunks or [])
        self.closed = False

    def json(self):
        if self._payload is None:
            raise ValueError("empty json")
        return self._payload

    def iter_content(self, chunk_size: int = 65536):
        del chunk_size
        yield from self._chunks

    def close(self):
        self.closed = True


class _FakeGraphDrive:
    """In-memory Microsoft Graph + OneDrive + SharePoint upload session harness."""

    def __init__(self, *, root_name: str = "Dossiers") -> None:
        self.root_id = "od-root"
        self.root_name = root_name
        # {folder_id: {"id", "name", "parent_id", "children_names": dict[name, id]}}
        self.folders: dict[str, dict] = {
            self.root_id: {"id": self.root_id, "name": root_name, "parent_id": None, "children": {}}
        }
        # {item_id: {"id", "name", "parent_id", "bytes": bytes, "quickXorHash": str}}
        self.files: dict[str, dict] = {}
        self.messages: dict[str, dict] = {}
        self.message_mimes: dict[str, bytes] = {}
        self.attachments: dict[str, dict[str, tuple[dict, bytes]]] = {}
        self.upload_sessions: dict[str, dict] = {}
        self.chunk_auth_headers: list[str] = []
        self.corrupt_next_upload_qxh = False
        self._next_id = 1

    def _new_id(self, prefix: str) -> str:
        val = f"{prefix}-{self._next_id}"
        self._next_id += 1
        return val

    def add_folder(self, parent_id: str, name: str, *, fid: str | None = None) -> str:
        existing = self.folders[parent_id]["children"].get(name.casefold())
        if existing:
            return existing
        node_id = fid or self._new_id("fld")
        self.folders[node_id] = {
            "id": node_id,
            "name": name,
            "parent_id": parent_id,
            "children": {},
        }
        self.folders[parent_id]["children"][name.casefold()] = node_id
        return node_id

    def add_file(self, parent_id: str, name: str, data: bytes) -> str:
        item_id = self._new_id("odf")
        qxh = quickxorhash_b64(data)
        self.files[item_id] = {
            "id": item_id,
            "name": name,
            "parent_id": parent_id,
            "bytes": data,
            "quickXorHash": qxh,
        }
        self.folders[parent_id]["children"][name.casefold()] = item_id
        return item_id

    def add_email(
        self,
        msg_id: str,
        *,
        imid: str,
        received: str = "2026-11-20T14:30:00Z",
        sender_name: str = "Me Jean Tremblay",
        sender_addr: str = "jean@ex.com",
        mime_bytes: bytes = b"From: jean@ex.com\r\nSubject: Test\r\n\r\nCorps\r\n",
        pieces: list[tuple[dict, bytes]] | None = None,
    ) -> str:
        cle = courrier_model.compute_cle(imid)
        self.messages[msg_id] = {
            "id": msg_id,
            "internetMessageId": imid,
            "receivedDateTime": received,
            "from": {"emailAddress": {"name": sender_name, "address": sender_addr}},
            "hasAttachments": bool(pieces),
        }
        self.message_mimes[msg_id] = mime_bytes
        att_map: dict[str, tuple[dict, bytes]] = {}
        for meta, raw in pieces or []:
            att_map[meta["id"]] = (dict(meta), raw)
        self.attachments[msg_id] = att_map
        return cle

    def _item_payload(self, item_id: str) -> dict:
        if item_id in self.folders:
            f = self.folders[item_id]
            return {
                "id": f["id"],
                "name": f["name"],
                "folder": {"childCount": len(f["children"])},
            }
        it = self.files[item_id]
        qxh = "AAAA==" if self.corrupt_next_upload_qxh else it["quickXorHash"]
        if self.corrupt_next_upload_qxh:
            self.corrupt_next_upload_qxh = False
        return {
            "id": it["id"],
            "name": it["name"],
            "size": len(it["bytes"]),
            "file": {"hashes": {"quickXorHash": qxh}},
        }

    def handle_request(self, method: str, url: str, *, headers=None, params=None, data=None, json=None, **_kw):
        headers = headers or {}
        if url.startswith("https://login.microsoftonline.com/"):
            return _FakeResponse(
                200,
                {
                    "access_token": "opaque-token",
                    "expires_in": 3600,
                },
            )

        if url.startswith("https://cabinet-my.sharepoint.com/upload/"):
            if "Authorization" in headers:
                self.chunk_auth_headers.append(headers["Authorization"])
            sess = self.upload_sessions.get(url)
            if sess is None:
                return _FakeResponse(404)
            content_range = headers.get("Content-Range", "")
            prefix, total_s = content_range.replace("bytes ", "").split("/")
            start_s, end_s = prefix.split("-")
            start, end, total = int(start_s), int(end_s), int(total_s)
            assert len(data) == (end - start + 1)
            assert start == len(sess["buf"])
            sess["buf"].extend(data)
            if end + 1 < total:
                return _FakeResponse(202)
            # Final chunk: check conflictBehavior=fail
            parent = self.folders[sess["parent_id"]]
            if sess["name"].casefold() in parent["children"]:
                return _FakeResponse(409)
            item_id = self.add_file(sess["parent_id"], sess["name"], bytes(sess["buf"]))
            return _FakeResponse(201, self._item_payload(item_id))

        # Graph endpoints
        encoded_root = quote(self.root_name, safe="/")
        base_prefix = "https://graph.microsoft.com/v1.0"
        assert url.startswith(base_prefix), url
        path = url[len(base_prefix) :]

        if path == f"/users/avocat%40cabinet.example/drive/root:/{encoded_root}":
            return _FakeResponse(200, self._item_payload(self.root_id))

        if path == f"/users/avocat%40cabinet.example/drive/root:/{encoded_root}:/children":
            children = [
                self._item_payload(cid)
                for cid in self.folders[self.root_id]["children"].values()
            ]
            return _FakeResponse(200, {"value": children})

        if path.startswith("/users/avocat%40cabinet.example/messages/") and method == "GET":
            rest = path[len("/users/avocat%40cabinet.example/messages/") :]
            if rest.endswith("/$value"):
                prefix_part = rest[: -len("/$value")]
                if "/attachments/" in prefix_part:
                    msg_id, att_id = prefix_part.split("/attachments/", 1)
                    if msg_id not in self.attachments or att_id not in self.attachments[msg_id]:
                        return _FakeResponse(404)
                    _, raw = self.attachments[msg_id][att_id]
                    mid = max(1, len(raw) // 2)
                    return _FakeResponse(
                        200,
                        headers={"Content-Length": str(len(raw))},
                        chunks=[raw[:mid], raw[mid:]] if len(raw) > 1 else [raw],
                    )
                msg_id = prefix_part
                if msg_id not in self.message_mimes:
                    return _FakeResponse(404)
                raw = self.message_mimes[msg_id]
                return _FakeResponse(
                    200,
                    headers={"Content-Length": str(len(raw))},
                    chunks=[raw[:16], raw[16:]],
                )
            if rest.endswith("/attachments"):
                msg_id = rest[: -len("/attachments")]
                if msg_id not in self.attachments:
                    return _FakeResponse(404)
                vals = [dict(meta) for meta, _ in self.attachments[msg_id].values()]
                return _FakeResponse(200, {"value": vals})
            msg_id = rest
            if msg_id not in self.messages:
                return _FakeResponse(404)
            return _FakeResponse(200, dict(self.messages[msg_id]))

        items_prefix = "/users/avocat%40cabinet.example/drive/items/"
        if path.startswith(items_prefix):
            tail = path[len(items_prefix) :]
            if method == "POST" and tail.endswith("/children"):
                parent_id = tail[: -len("/children")]
                name = str((json or {}).get("name") or "")
                if parent_id not in self.folders:
                    return _FakeResponse(404)
                if name.casefold() in self.folders[parent_id]["children"]:
                    return _FakeResponse(409)
                new_id = self.add_folder(parent_id, name)
                return _FakeResponse(201, self._item_payload(new_id))

            if ":/" in tail:
                parent_id, after = tail.split(":/", 1)
                if parent_id not in self.folders:
                    return _FakeResponse(404)
                parent = self.folders[parent_id]
                if after.endswith(":/content") and method == "PUT":
                    from urllib.parse import unquote

                    name = unquote(after[: -len(":/content")])
                    if name.casefold() in parent["children"]:
                        return _FakeResponse(409)
                    new_id = self.add_file(parent_id, name, bytes(data or b""))
                    return _FakeResponse(201, self._item_payload(new_id))
                if after.endswith(":/createUploadSession") and method == "POST":
                    from urllib.parse import unquote

                    name = unquote(after[: -len(":/createUploadSession")])
                    if name.casefold() in parent["children"]:
                        return _FakeResponse(409)
                    surl = f"https://cabinet-my.sharepoint.com/upload/{len(self.upload_sessions) + 1}"
                    self.upload_sessions[surl] = {
                        "parent_id": parent_id,
                        "name": name,
                        "buf": bytearray(),
                    }
                    return _FakeResponse(200, {"uploadUrl": surl})
                if method == "GET":
                    from urllib.parse import unquote

                    subpath = unquote(after)
                    cur_id = parent_id
                    for seg in subpath.split("/"):
                        if cur_id not in self.folders:
                            return _FakeResponse(404)
                        nxt = self.folders[cur_id]["children"].get(seg.casefold())
                        if nxt is None:
                            return _FakeResponse(404)
                        cur_id = nxt
                    return _FakeResponse(200, self._item_payload(cur_id))

        raise AssertionError(f"Unhandled fake Graph call: {method} {url}")


@pytest.fixture
def env(monkeypatch):
    fake_db = install(monkeypatch, *_fake_modules())
    bucket = FakeBucket()
    drive = _FakeGraphDrive()

    monkeypatch.setattr(courrier_model.storage, "bucket", lambda: bucket)
    monkeypatch.setattr(document_model.storage, "bucket", lambda: bucket)
    monkeypatch.setattr(storage_identity, "owner_uid", lambda: "u1")
    monkeypatch.setattr(Config, "COURRIER_ACTIF", True)
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_ID", "cid")
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_SECRET", "sec")
    monkeypatch.setattr(Config, "GRAPH_TENANT_ID", "tid")
    monkeypatch.setattr(Config, "COURRIER_UPN", "avocat@cabinet.example")
    monkeypatch.setattr(Config, "COURRIER_ONEDRIVE_ROOT", "Dossiers")
    graph_courrier._reset_token_cache_for_tests()

    class _SessionProxy:
        def get(self, url, **kw):
            return drive.handle_request("GET", url, **kw)

        def post(self, url, **kw):
            return drive.handle_request("POST", url, **kw)

        def put(self, url, **kw):
            return drive.handle_request("PUT", url, **kw)

    monkeypatch.setattr(graph_courrier, "_session", _SessionProxy())

    did = "a0000000-0000-4000-8000-000000000001"
    fake_db.seed(
        f"dossiers/{did}",
        {
            "id": did,
            "file_number": "2026-001",
            "title": "Tremblay c. Lavoie",
            "status": "actif",
            "clients": [],
            "opposing_parties": [],
            "other_parties": [],
        },
    )
    od_did = drive.add_folder(drive.root_id, "2026-001 - Tremblay c. Lavoie", fid="od-d1")
    return {
        "db": fake_db,
        "bucket": bucket,
        "drive": drive,
        "dossier_id": did,
        "od_dossier_id": od_did,
    }


def test_quickxorhash_vectors_and_arbitrary_chunking():
    assert quickxorhash_b64(b"") == "AAAAAAAAAAAAAAAAAAAAAAAAAAA="
    v1 = base64.b64decode(
        "jyibxJSzO6ZiZ0O1qe3tG/bvIAYssvukh9suIT5wEy1JBINVgPiqdsTW0cOpP0aU"
        "fP7mgqLfADkzI/m/GgCuVhr8oFLrOCoTx1/psBOWwhltCbhUx51Icm9aH8tY4Z3c"
        "cU+6BKpYQkLCy0B/A9Zc"
    )
    v2 = base64.b64decode(
        "ikwCorI7PKWz17EI50jZCGbV9JU2E8bXVfxNMg5zdmqSZ2NlsQPp0kqYIPjzwTg1"
        "MBtfWPg53k0h0P2naJNEVgrqpoHTfV2b3pJ4m0zYPTJmUX4Bg/lOxcnCxAYKU29Y"
        "5F0U8Quz7ZXFBEweftXxJ7RS4r6N7BzJrPsLhY7hgck="
    )
    assert quickxorhash_b64(v1) == "hZfLIilSITC6N3e3tQ/iSgEzkto="
    assert quickxorhash_b64(v2) == "imAoFvCWlDn4yVw3/oq1PDbbm6U="

    payload = (v1 + v2) * 37
    expected = quickxorhash_b64(payload)
    for step in (1, 7, 37, 160, 4096):
        h = QuickXorHash()
        for off in range(0, len(payload), step):
            h.update(payload[off : off + step])
        assert h.b64digest() == expected


def test_tools_sous_dossier_enum_matches_service_mapping():
    pieces_prop = tools.TOOLS["save_courriels"]["input_schema"]["properties"]["messages"]["items"]["properties"]["pieces"]
    enum_vals = pieces_prop["items"]["properties"]["sous_dossier"]["enum"]
    assert set(enum_vals) == set(courrier_service.VALID_SOUS_DOSSIERS)


def test_naming_helpers_enforce_caps_and_strip_honorifics():
    from datetime import datetime, timezone

    assert (
        courrier_service.nettoyer_expediteur_sans_civilite("Courriel de Me Jean-Luc Tremblay")
        == "Jean-Luc Tremblay"
    )
    assert courrier_service.nettoyer_expediteur_sans_civilite("M. Marc Lavoie") == "Marc Lavoie"
    assert courrier_service.nettoyer_expediteur_sans_civilite("Mme. Claire Roy") == "Claire Roy"
    assert (
        courrier_service.nettoyer_expediteur_sans_civilite("", "greffe@justice.example")
        == "greffe"
    )

    # 2026-11-20T14:30:00Z == 09h30 EST (America/Montreal)
    recu_dt = datetime(2026, 11, 20, 14, 30, tzinfo=timezone.utc)
    eml_name = courrier_service.construire_nom_eml(
        "2026-001",
        recu_dt,
        "Me " + ("A" * 120),
    )
    assert eml_name.startswith("2026-001 - 2026-11-20 09h30 - ")
    assert eml_name.endswith(".eml")
    assert len(eml_name) <= courrier_service.EML_MAX_CHARS

    # Server-side attachment naming: auto-prefix, no double prefix, and preserve exhibit code.
    auto_name, err_auto = courrier_service.nettoyer_nom_piece(
        "", "Confirmation <TAL>.PDF", dossier_num="2026-034", recu_dt=recu_dt
    )
    assert err_auto is None and auto_name == "2026-034 - 2026-11-20 - Confirmation TAL.pdf"

    already_prefixed, err_dup = courrier_service.nettoyer_nom_piece(
        "", "2026-034 - D-70 Decision finale.pdf", dossier_num="2026-034", recu_dt=recu_dt
    )
    assert err_dup is None and already_prefixed == "2026-034 - D-70 Decision finale.pdf"

    custom_short, err_custom = courrier_service.nettoyer_nom_piece(
        "Facture medecin", "scan001.PDF", dossier_num="2026-034", recu_dt=recu_dt
    )
    assert err_custom is None and custom_short == "2026-034 - 2026-11-20 - Facture medecin.pdf"

    long_cote, err_cote = courrier_service.nettoyer_nom_piece(
        "",
        "Très longue description détaillée de la décision rendue par le Tribunal administratif du logement "
        + ("a " * 30)
        + "D-70.PDF",
        dossier_num="2026-034",
        recu_dt=recu_dt,
    )
    assert err_cote is None and long_cote is not None
    assert long_cote.startswith("2026-034 - 2026-11-20 - ")
    assert "D-70" in long_cote
    assert long_cote.endswith(".pdf")
    assert len(long_cote) <= courrier_service.PIECE_NOM_MAX_CHARS

    ok_name, err = courrier_service.nettoyer_nom_piece(
        "Rapport <final> " + ("x" * 150) + ".pdf", "raw.PDF"
    )
    assert err is None and ok_name is not None
    assert ok_name.endswith(".pdf") and len(ok_name) <= courrier_service.PIECE_NOM_MAX_CHARS
    assert "<" not in ok_name

    assert courrier_service.nettoyer_nom_piece("piege.exe", "facture.pdf")[0] is None
    assert courrier_service.nettoyer_nom_piece("CON.pdf", "facture.pdf")[0] is None


def test_save_courriels_eml_pdf_txt_csv_and_zero_pii_registry(env):
    drive = env["drive"]
    db = env["db"]
    bucket = env["bucket"]
    did = env["dossier_id"]

    eml = b"From: jean@ex.com\r\nSubject: Pieces\r\n\r\nVeuillez trouver ci-joint.\r\n"
    pdf = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n"
    txt = "Notes d'audience Québec.\r\n".encode("utf-8")
    csv_cp = "col1;col2\nÉté;15,00\n".encode("cp1252")

    cle = drive.add_email(
        "msg-1",
        imid="<msg-full-1@cabinet.example>",
        sender_name="Me Jean Tremblay",
        mime_bytes=eml,
        pieces=[
            ({"id": "att-1", "name": "Requete.PDF", "contentType": "application/pdf", "size": len(pdf), "isInline": False}, pdf),
            ({"id": "att-2", "name": "notes.txt", "contentType": "text/plain", "size": len(txt), "isInline": False}, txt),
            ({"id": "att-3", "name": "tableau.csv", "contentType": "text/csv", "size": len(csv_cp), "isInline": False}, csv_cp),
        ],
    )

    res = handlers.save_courriels({
        "idempotency_key": "k-full-001",
        "messages": [
            {
                "cle": cle,
                "id": "msg-1",
                "dossier": "2026-001",
                "dossier_onedrive": "2026-001 - Tremblay c. Lavoie",
                "eml": {"sous_dossier": "Correspondance\\Courriels", "nom": "ignore.eml"},
                "pieces": [
                    {"rang": 1, "id": "att-1", "sous_dossier": "Pièces"},
                    {"rang": 2, "id": "att-2", "sous_dossier": "Documents du client", "nom": "notes.txt"},
                    {"rang": 3, "id": "att-3", "sous_dossier": "", "nom": "tableau.csv"},
                ],
            }
        ],
    })

    assert res["traites"] == 1 and res["echecs"] == 0 and res["differes"] == 0
    msg_out = res["messages"][0]
    assert msg_out["statut"] == "ok"
    assert [f["slot"] for f in msg_out["fichiers"]] == ["eml", "piece:1", "piece:2", "piece:3"]
    by_slot = {f["slot"]: f for f in msg_out["fichiers"]}
    assert by_slot["eml"]["nom"] == "2026-001 - 2026-11-20 09h30 - Jean Tremblay.eml"
    assert (
        by_slot["eml"]["chemin"]
        == "2026-001 - Tremblay c. Lavoie\\Correspondance\\Courriels\\2026-001 - 2026-11-20 09h30 - Jean Tremblay.eml"
    )
    assert by_slot["piece:1"]["nom"] == "2026-001 - 2026-11-20 - Requete.pdf"
    assert (
        by_slot["piece:1"]["chemin"]
        == "2026-001 - Tremblay c. Lavoie\\Pièces\\2026-001 - 2026-11-20 - Requete.pdf"
    )
    assert by_slot["piece:3"]["nom"] == "2026-001 - 2026-11-20 - tableau.csv"
    assert (
        by_slot["piece:3"]["chemin"]
        == "2026-001 - Tremblay c. Lavoie\\2026-001 - 2026-11-20 - tableau.csv"
    )

    # Staging objects cleaned up
    assert not any(p.startswith("staging/") for p in bucket.objects)

    # Exact bytes landed in OneDrive
    od_bytes = {f["name"]: f["bytes"] for f in drive.files.values()}
    assert od_bytes["2026-001 - 2026-11-20 09h30 - Jean Tremblay.eml"] == eml
    assert od_bytes["2026-001 - 2026-11-20 - Requete.pdf"] == pdf
    assert od_bytes["2026-001 - 2026-11-20 - notes.txt"] == txt
    assert od_bytes["2026-001 - 2026-11-20 - tableau.csv"] == csv_cp

    # Exact bytes & presumed MCP categories & folders landed in Athena
    docs = db.peek_collection("documents")
    assert len(docs) == 4
    by_orig = {d["original_filename"]: d for d in docs.values()}

    eml_doc = by_orig["2026-001 - 2026-11-20 09h30 - Jean Tremblay.eml"]
    assert eml_doc["category"] == "correspondance"
    assert eml_doc["category_source"] == "mcp"
    assert eml_doc["category_set_by_lawyer"] is False
    assert eml_doc["folder_id"] == folder_model.node_folder_id(did, "courriels")
    assert bucket.objects[eml_doc["storage_path"]].data == eml
    assert bucket.objects[eml_doc["storage_path"]].content_disposition == "attachment"

    pdf_doc = by_orig["2026-001 - 2026-11-20 - Requete.pdf"]
    assert pdf_doc["category"] == "pièce"
    assert pdf_doc["folder_id"] == folder_model.node_folder_id(did, "pieces")
    assert bucket.objects[pdf_doc["storage_path"]].data == pdf
    assert bucket.objects[pdf_doc["storage_path"]].content_disposition != "attachment"

    txt_doc = by_orig["2026-001 - 2026-11-20 - notes.txt"]
    assert txt_doc["category"] == "autre"
    assert txt_doc["folder_id"] == folder_model.node_folder_id(did, "documents_client")
    assert bucket.objects[txt_doc["storage_path"]].data == txt
    assert bucket.objects[txt_doc["storage_path"]].content_disposition == "attachment"

    csv_doc = by_orig["2026-001 - 2026-11-20 - tableau.csv"]
    assert csv_doc["category"] == "autre"
    assert csv_doc["folder_id"] is None
    assert bucket.objects[csv_doc["storage_path"]].data == csv_cp
    assert bucket.objects[csv_doc["storage_path"]].content_disposition == "attachment"

    # Registry permanent row + zero PII in registry and idempotency (no nom/chemin persisted!)
    reg = db.peek(f"courrier_messages/{cle}")
    assert reg["etat"] == "enregistre" and reg["expire_at"] is None
    assert len(reg["fichiers"]) == 4
    reg_blob = str(reg)
    for forbidden in ("Jean Tremblay", "Requete", "notes.txt", "tableau.csv", "jean@ex.com"):
        assert forbidden not in reg_blob
    idem = list(db.peek_collection("mcp_idempotency").values())[0]
    idem_blob = str(idem)
    for forbidden in ("Jean Tremblay", "Requete", "notes.txt", "tableau.csv", "jean@ex.com"):
        assert forbidden not in idem_blob


def test_large_streaming_upload_session_and_no_bearer_to_sharepoint(env):
    drive = env["drive"]
    bucket = env["bucket"]
    eml = b"From: a@ex.com\r\nSubject: Gros\r\n\r\nVoir piece.\r\n"
    big_pdf = b"%PDF-1.7\n" + (b"0123456789ABCDEF" * 280_000)  # ~4.48 MiB > 4 MiB

    cle = drive.add_email(
        "msg-big",
        imid="<msg-big@cabinet.example>",
        mime_bytes=eml,
        pieces=[
            ({"id": "att-big", "name": "volumineux.pdf", "contentType": "application/pdf", "size": len(big_pdf), "isInline": False}, big_pdf),
        ],
    )

    res = handlers.save_courriels({
        "idempotency_key": "k-big-001",
        "messages": [
            {
                "cle": cle,
                "id": "msg-big",
                "dossier": "2026-001",
                "dossier_onedrive": "2026-001 - Tremblay c. Lavoie",
                "pieces": [{"rang": 1, "id": "att-big", "sous_dossier": "Procédures"}],
            }
        ],
    })

    assert res["traites"] == 1
    assert drive.chunk_auth_headers == []  # Pre-authenticated uploadUrl received no Graph Bearer
    od_big = next(f for f in drive.files.values() if f["name"] == "2026-001 - 2026-11-20 - volumineux.pdf")
    assert od_big["bytes"] == big_pdf
    assert od_big["quickXorHash"] == quickxorhash_b64(big_pdf)
    assert not any(p.startswith("staging/") for p in bucket.objects)


def test_idempotency_replay_deja_present_and_conflict_suffix(env):
    drive = env["drive"]
    db = env["db"]

    eml = b"From: jean@ex.com\r\nSubject: Conflit\r\n\r\nTexte.\r\n"
    pdf_same = b"%PDF-1.7\nSAME\n"
    pdf_diff = b"%PDF-1.7\nDIFFERENT-NEW\n"
    existing_diff = b"%PDF-1.7\nEXISTING-OTHER\n"

    od_pieces = drive.add_folder(env["od_dossier_id"], "Pièces")
    drive.add_file(od_pieces, "2026-001 - 2026-11-20 - contrat.pdf", existing_diff)

    cle = drive.add_email(
        "msg-conf",
        imid="<msg-conf@cabinet.example>",
        mime_bytes=eml,
        pieces=[
            ({"id": "att-same", "name": "identique.pdf", "contentType": "application/pdf", "size": len(pdf_same), "isInline": False}, pdf_same),
            ({"id": "att-diff", "name": "contrat.pdf", "contentType": "application/pdf", "size": len(pdf_diff), "isInline": False}, pdf_diff),
        ],
    )
    drive.add_file(od_pieces, "2026-001 - 2026-11-20 - identique.pdf", pdf_same)

    plan = [
        {
            "cle": cle,
            "id": "msg-conf",
            "dossier": "2026-001",
            "dossier_onedrive": "2026-001 - Tremblay c. Lavoie",
            "pieces": [
                {"rang": 1, "id": "att-same", "sous_dossier": "Pièces"},
                {"rang": 2, "id": "att-diff", "sous_dossier": "Pièces"},
            ],
        }
    ]

    first = handlers.save_courriels({"idempotency_key": "k-conf-1", "messages": plan})
    assert first["traites"] == 1
    by_slot = {f["slot"]: f for f in first["messages"][0]["fichiers"]}
    assert by_slot["piece:1"]["statut_onedrive"] == "deja_present"
    assert by_slot["piece:1"]["statut_athena"] == "cree"
    assert by_slot["piece:2"]["statut_onedrive"] == "renomme"
    assert by_slot["piece:2"]["renomme_index"] == 2
    assert by_slot["piece:2"]["nom"] == "2026-001 - 2026-11-20 - contrat (2).pdf"
    assert (
        by_slot["piece:2"]["chemin"]
        == "2026-001 - Tremblay c. Lavoie\\Pièces\\2026-001 - 2026-11-20 - contrat (2).pdf"
    )

    # Existing OneDrive file intact; new file written as "... contrat (2).pdf"
    od_names = {f["name"]: f["bytes"] for f in drive.files.values()}
    assert od_names["2026-001 - 2026-11-20 - contrat.pdf"] == existing_diff
    assert od_names["2026-001 - 2026-11-20 - contrat (2).pdf"] == pdf_diff

    # Exact key replay
    replay = handlers.save_courriels({"idempotency_key": "k-conf-1", "messages": plan})
    assert replay["idempotent_replay"] is True
    assert len(db.peek_collection("documents")) == 3

    # Different key with identical content: Athena & OneDrive detect deja_present without duplicating
    second = handlers.save_courriels({"idempotency_key": "k-conf-2", "messages": plan})
    assert second["idempotent_replay"] is False
    by_slot_2 = {f["slot"]: f for f in second["messages"][0]["fichiers"]}
    assert by_slot_2["eml"]["statut_onedrive"] == "deja_present"
    assert by_slot_2["eml"]["statut_athena"] == "deja_present"
    assert by_slot_2["piece:1"]["statut_onedrive"] == "deja_present"
    assert by_slot_2["piece:1"]["statut_athena"] == "deja_present"
    # Note: piece:2 finds "... contrat (2).pdf" after 409 on "... contrat.pdf" -> deja_present at index 2!
    assert by_slot_2["piece:2"]["statut_onedrive"] == "deja_present"
    assert by_slot_2["piece:2"]["renomme_index"] == 2
    assert by_slot_2["piece:2"]["statut_athena"] == "deja_present"
    assert len(db.peek_collection("documents")) == 3


def test_exotic_attachment_non_pris_en_charge_and_legacy_emails_folder(env):
    drive = env["drive"]
    db = env["db"]

    od_corr = drive.add_folder(env["od_dossier_id"], "Correspondance")
    od_emails = drive.add_folder(od_corr, "Emails")  # Legacy folder only -> reused

    eml = b"From: client@ex.com\r\nSubject: Photo HEIC\r\n\r\nVoir photo.\r\n"
    heic = b"\x00\x00\x00\x18ftypheic" + b"\x01" * 32
    cle = drive.add_email(
        "msg-heic",
        imid="<msg-heic@cabinet.example>",
        mime_bytes=eml,
        pieces=[
            ({"id": "att-heic", "name": "sinistre.HEIC", "contentType": "image/heic", "size": len(heic), "isInline": False}, heic),
        ],
    )

    res = handlers.save_courriels({
        "idempotency_key": "k-heic-1",
        "messages": [
            {
                "cle": cle,
                "id": "msg-heic",
                "dossier": "2026-001",
                "dossier_onedrive": "2026-001 - Tremblay c. Lavoie",
                "pieces": [{"rang": 1, "id": "att-heic", "sous_dossier": "Pièces", "nom": "sinistre.HEIC"}],
            }
        ],
    })

    assert res["traites"] == 1
    by_slot = {f["slot"]: f for f in res["messages"][0]["fichiers"]}
    assert by_slot["piece:1"]["statut_onedrive"] == "cree"
    assert by_slot["piece:1"]["statut_athena"] == "non_pris_en_charge"
    assert by_slot["piece:1"]["document_id"] is None

    # .eml landed in legacy Correspondance/Emails (not splitting into Courriels)
    eml_od = next(f for f in drive.files.values() if f["name"].endswith(".eml"))
    assert eml_od["parent_id"] == od_emails
    assert db.peek(f"courrier_messages/{cle}")["etat"] == "enregistre"
    assert len(db.peek_collection("documents")) == 1  # only the .eml


def test_essai_rejected_at_mcp_schema_boundary():
    assert "essai" not in tools.TOOLS["save_courriels"]["input_schema"]["properties"]
    errs = tools.validate_args(
        tools.TOOLS["save_courriels"]["input_schema"],
        {
            "idempotency_key": "k-no-dry",
            "essai": True,
            "messages": [
                {
                    "cle": "0" * 16,
                    "id": "m1",
                    "dossier": "2026-001",
                    "dossier_onedrive": "2026-001 - Dossier",
                }
            ],
        },
    )
    assert errs and any("essai" in e for e in errs)


def test_security_refusals_and_budget_deferral(env, monkeypatch):
    drive = env["drive"]
    db = env["db"]

    eml = b"From: a@ex.com\r\nSubject: Sec\r\n\r\nTexte.\r\n"
    real_cle = drive.add_email("msg-sec", imid="<msg-sec@cabinet.example>", mime_bytes=eml)

    # 1. Kill switch OFF refuses cleanly
    monkeypatch.setattr(Config, "COURRIER_ACTIF", False)
    with pytest.raises(tools.ToolArgumentError, match="désactivé"):
        handlers.save_courriels({
            "idempotency_key": "k-kill-1",
            "messages": [
                {
                    "cle": real_cle,
                    "id": "msg-sec",
                    "dossier": "2026-001",
                    "dossier_onedrive": "2026-001 - Tremblay c. Lavoie",
                }
            ],
        })
    monkeypatch.setattr(Config, "COURRIER_ACTIF", True)

    # 2. cle mismatch -> message refused cleanly, nothing written
    bad_cle = "0" * 16
    res_bad_cle = handlers.save_courriels({
        "idempotency_key": "k-bad-cle",
        "messages": [
            {
                "cle": bad_cle,
                "id": "msg-sec",
                "dossier": "2026-001",
                "dossier_onedrive": "2026-001 - Tremblay c. Lavoie",
            }
        ],
    })
    assert res_bad_cle["echecs"] == 1 and res_bad_cle["traites"] == 0
    assert "ne correspond pas" in (res_bad_cle["messages"][0]["erreur"] or "")
    assert db.peek_collection("courrier_messages") == {}
    assert db.peek_collection("mcp_idempotency") == {}

    # 3. Missing OneDrive folder with creer_dossier_onedrive=False -> refused
    db.seed("dossiers/a0000000-0000-4000-8000-000000000002", {
        "id": "a0000000-0000-4000-8000-000000000002",
        "file_number": "2026-002",
        "title": "Gagnon c. Roy",
        "status": "actif",
        "clients": [],
        "opposing_parties": [],
        "other_parties": [],
    })
    res_no_create = handlers.save_courriels({
        "idempotency_key": "k-no-create",
        "messages": [
            {
                "cle": real_cle,
                "id": "msg-sec",
                "dossier": "2026-002",
                "dossier_onedrive": "2026-002 - Gagnon c. Roy",
                "creer_dossier_onedrive": False,
            }
        ],
    })
    assert res_no_create["echecs"] == 1
    assert "creer_dossier_onedrive" in (res_no_create["messages"][0]["erreur"] or "")

    # 4. Duplicate OneDrive dossier prefix -> refused
    drive.add_folder(drive.root_id, "2026-001 - Doublon")
    res_dup = handlers.save_courriels({
        "idempotency_key": "k-dup-od",
        "messages": [
            {
                "cle": real_cle,
                "id": "msg-sec",
                "dossier": "2026-001",
                "dossier_onedrive": "2026-001 - Tremblay c. Lavoie",
            }
        ],
    })
    assert res_dup["echecs"] == 1
    assert "Plusieurs dossiers OneDrive" in (res_dup["messages"][0]["erreur"] or "")

    # 5. OneDrive QuickXorHash mismatch fails closed before record_enregistre
    drive.folders[drive.root_id]["children"].pop("2026-001 - doublon", None)
    drive.corrupt_next_upload_qxh = True
    res_qxh = handlers.save_courriels({
        "idempotency_key": "k-qxh-fail",
        "messages": [
            {
                "cle": real_cle,
                "id": "msg-sec",
                "dossier": "2026-001",
                "dossier_onedrive": "2026-001 - Tremblay c. Lavoie",
            }
        ],
    })
    assert res_qxh["echecs"] == 1
    assert "QuickXorHash" in (res_qxh["messages"][0]["erreur"] or "")
    assert db.peek(f"courrier_messages/{real_cle}") is None

    # 6. Budget deferral on service-level batch leaves msg 2 as differe
    cle_2 = drive.add_email("msg-sec-2", imid="<msg-sec-2@cabinet.example>", mime_bytes=eml)
    ticks = iter([0.0, 30.0])
    out_budget = courrier_service.enregistrer_courriels(
        [
            {
                "cle": real_cle,
                "id": "msg-sec",
                "dossier": "2026-001",
                "dossier_onedrive": "2026-001 - Tremblay c. Lavoie",
            },
            {
                "cle": cle_2,
                "id": "msg-sec-2",
                "dossier": "2026-001",
                "dossier_onedrive": "2026-001 - Tremblay c. Lavoie",
            },
        ],
        user_id="u1",
        clock=lambda: next(ticks, 30.0),
        budget_secondes=25.0,
    )
    assert out_budget["traites"] == 1 and out_budget["differes"] == 1
    assert out_budget["messages"][0]["statut"] == "ok"
    assert out_budget["messages"][1]["statut"] == "differe"
    assert db.peek(f"courrier_messages/{real_cle}")["etat"] == "enregistre"
    assert db.peek(f"courrier_messages/{cle_2}") is None


@pytest.mark.parametrize("total", [1_234, 256 * 1024, 600 * 1024])
def test_streaming_digest_reader_satisfies_real_gcs_resumable_upload(total):
    chunk_size = 256 * 1024
    data = bytes(((i * 31) % 251) for i in range(total))
    fed_chunks = [data[i : i + 7_000] for i in range(0, len(data), 7_000)]
    reader = courrier_model._StreamingDigestReader(iter(fed_chunks))

    upload = ResumableUpload(
        "https://storage.googleapis.com/upload/storage/v1/b/test-bucket/o?uploadType=resumable",
        chunk_size,
        checksum=None,
    )
    uploaded = bytearray()
    transport = mock.MagicMock()

    def _side_effect(method, url, data=None, headers=None, **_kwargs):
        if url.endswith("uploadType=resumable"):
            return mock.MagicMock(
                status_code=200,
                headers={"location": "https://storage.googleapis.com/u/sess-1"},
            )
        cr = (headers or {}).get("content-range", "")
        if data:
            uploaded.extend(data)
        if not cr.endswith("/*"):
            return mock.MagicMock(status_code=200, headers={}, content=b"{}", json=lambda: {})
        start = int(cr.split(" ", 1)[1].split("-", 1)[0])
        end = start + len(data) - 1
        return mock.MagicMock(status_code=308, headers={"range": f"bytes=0-{end}"})

    transport.request.side_effect = _side_effect
    upload.initiate(
        transport,
        reader,
        {},
        "application/octet-stream",
        total_bytes=None,
        stream_final=False,
    )
    while not upload.finished:
        upload.transmit_next_chunk(transport)

    assert bytes(uploaded) == data
    assert reader.tell() == reader.size == len(data)
    assert reader.sha256.hexdigest() == hashlib.sha256(data).hexdigest()
    assert reader.quickxorhash.b64digest() == quickxorhash_b64(data)
