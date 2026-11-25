"""Phase 6 (Lot F — 1j DAV & 2b OneDrive Mirroring) comprehensive tests.

Covers all behaviors, boundaries, and failure modes on realistic in-memory
Firestore (``_fake_firestore``), GCS (``FakeBucket``), and HTTP-level
OneDrive Graph simulation (``FakeOneDrive``):

* 1j:
  - RFC 5545 ``CLASS`` storage/emission/round-trip + fail-closed secrecy on
    dossier-linked hearings, tasks, notes + Graph ``sensitivity`` mapping.
  - Sanitized read-only shared ``.ics`` feed (disabled by default, SHA-256
    token generate/rotate/revoke via settings, constant-time compare,
    ``PUBLIC`` non-dossier in clear, ``PRIVATE``/``CONFIDENTIAL``/dossier-linked
    stripped to ``SUMMARY:Occupé``, truncated read -> 503, log/trace redaction).
  - CalDAV/CardDAV compliance: ``SEQUENCE`` increment, server-side
    ``<C:time-range>`` query filtering, ``VTIMEZONE`` ``America/Toronto`` on
    timed events/tasks + ``VALUE=DATE`` on all-day events, multi-``VALARM``
    round-trip across DAV PUT and web edits, vCard 4.0 folded ``LANG``/``GENDER``,
    yearless ``BDAY`` (``--MMDD`` / ``--0229``), and secondary ``EMAIL``/``TEL``.
* 2b:
  - Kill switch ``MIROIR_ONEDRIVE_ACTIF`` (default False), queue/cron origin
    guards, error isolation on post-commit dispatch.
  - Dossier 18-folder tree & documents mirroring, Courrier folder adoption,
    > 4 MiB upload sessions, Courrier/Teams item_id & QuickXorHash deduplication,
    collision disambiguation, renames, moves, and deletions (including
    ``delete_folder(contents="move")``).
  - Firm files: Admin/Trust receipts, completed reconciliation statements
    ONLY (drafts excluded), FR/EN Word templates, and hard exclusion of
    client identity proofs (``users/*/parties/...``).
  - Periodic catch-up cron sweep + orphan cleanup.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import sys
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock
from urllib.parse import unquote, urlsplit

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from flask import Flask  # noqa: E402
from icalendar import Calendar  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import models.admin_ledger as admin_model
    import models.doc_template as tpl_model
    import models.document as doc_model
    import models.dossier as dossier_model
    import models.folder as folder_model
    import models.hearing as hearing_model
    import models.integrations as integ_model
    import models.note as note_model
    import models.partie as partie_model
    import models.task as task_model
    import models.trust as trust_model
    import security  # noqa: F401
    from config import Config
    from dav import dav_bp
    from dav import sync as dav_sync
    from dav.carddav import carddav_bp
    from dav.dossier_collections import dossier_dav_bp
    from routes.calendrier_partage import calendrier_partage_bp
    from routes.hearings import hearings_bp
    from routes.notes import notes_bp
    from routes.settings import settings_bp
    from routes.taches_onedrive import taches_onedrive_bp
    from routes.tasks import tasks_bp
    from services import miroir_onedrive
    from utils import dav_classification, graph_courrier, graph_miroir, ical_tz, logging_setup, tracing_setup
    from utils.quickxorhash import quickxorhash_b64

from tests._fake_firestore import install as install_fake_firestore  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402

UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"


def _minimal_docx_bytes(text: str = "Bonjour {{client.nom}}") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>"
            "</w:document>",
        )
    return buf.getvalue()


@pytest.fixture()
def fake_db(monkeypatch):
    return install_fake_firestore(
        monkeypatch,
        hearing_model,
        task_model,
        note_model,
        partie_model,
        dossier_model,
        folder_model,
        doc_model,
        tpl_model,
        admin_model,
        trust_model,
        integ_model,
        miroir_onedrive,
        dav_sync,
    )


def _seed_dossier(db, *, dossier_id: str = "d1", file_number: str = "2026-001") -> dict:
    doc = {
        "id": dossier_id,
        "file_number": file_number,
        "title": "Dossier Confidentiel Test",
        "status": "actif",
        "client_ids": ["c1"],
        "opposing_party_ids": [],
        "avocat_ids": [],
        "parties": [],
    }
    db.collection("dossiers").document(dossier_id).set(doc)
    return doc


def _make_app(fake_db=None, monkeypatch=None) -> Flask:
    template_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
    )
    app = Flask(__name__, template_folder=template_dir)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["AUTHORIZED_USER_EMAIL"] = "test@example.com"
    app.config["REQUIRE_MFA"] = False

    from tz import to_mtl
    from utils.format_fr import format_cents_fr
    from utils.html_attr import jsattr
    from utils.icons import ms
    from utils.markdown_docx import markdown_to_safe_html
    from utils.validators import format_phone_display

    app.jinja_env.filters["to_mtl"] = to_mtl
    app.jinja_env.filters["phone"] = format_phone_display
    app.jinja_env.filters["cents_fr"] = lambda c: format_cents_fr(c) if c is not None else ""
    app.jinja_env.filters["jsattr"] = jsattr
    app.jinja_env.filters["markdown"] = markdown_to_safe_html
    app.jinja_env.globals["ms"] = ms
    app.jinja_env.globals["csrf_token"] = lambda: "test-csrf"

    # Provide stub routes referenced by shared base.html navigation
    for ep in (
        "dashboard.index",
        "dossiers.index",
        "parties.index",
        "documents.bibliotheque",
        "invoices.index",
        "time_expenses.index",
        "trust.index",
        "admin_ledger.index",
        "comptabilite.index",
        "reception.index",
        "auth.logout",
    ):
        if ep not in app.view_functions:
            app.add_url_rule(f"/__stub/{ep}", endpoint=ep, view_func=lambda: "")

    app.register_blueprint(calendrier_partage_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(hearings_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(notes_bp)
    app.register_blueprint(taches_onedrive_bp)
    app.register_blueprint(dav_bp)
    app.register_blueprint(carddav_bp)
    app.register_blueprint(dossier_dav_bp)
    return app


def _login_session(client) -> None:
    with client.session_transaction() as sess:
        sess["user_id"] = UID
        sess["email"] = "test@example.com"
        sess["sid"] = "s-test"
        sess["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=2)


# ── Fake OneDrive HTTP simulation for graph_courrier ──────────────────────────


class FakeOneDrive:
    """In-memory OneDrive drive supporting GET/POST/PUT/PATCH/DELETE + upload sessions."""

    def __init__(self) -> None:
        self._seq = 0
        self.items: dict[str, dict] = {}
        self.blobs: dict[str, bytes] = {}
        self.sessions: dict[str, dict] = {}
        self.ops: list[tuple[str, str]] = []
        self.root_id = "od-root"
        self.items[self.root_id] = {
            "id": self.root_id,
            "name": "root",
            "folder": {"childCount": 0},
            "parentReference": {},
        }

    def _next_id(self, prefix: str = "od") -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def _child(self, parent_id: str, name: str) -> dict | None:
        for it in self.items.values():
            if (it.get("parentReference") or {}).get("id") == parent_id and it.get("name") == name:
                return it
        return None

    def _by_root_path(self, rel_path: str) -> dict | None:
        cur = self.items[self.root_id]
        segs = [unquote(s) for s in rel_path.strip("/").split("/") if s]
        for s in segs:
            nxt = self._child(cur["id"], s)
            if nxt is None:
                return None
            cur = nxt
        return cur

    def mkdir_path(self, rel_path: str) -> dict:
        cur = self.items[self.root_id]
        for seg in [s for s in rel_path.strip("/").split("/") if s]:
            nxt = self._child(cur["id"], seg)
            if nxt is None:
                iid = self._next_id("dir")
                nxt = {
                    "id": iid,
                    "name": seg,
                    "folder": {"childCount": 0},
                    "parentReference": {"id": cur["id"]},
                }
                self.items[iid] = nxt
            cur = nxt
        return cur

    def put_file_direct(self, rel_path: str, data: bytes) -> dict:
        parent_path, _, name = rel_path.strip("/").rpartition("/")
        parent = self.mkdir_path(parent_path) if parent_path else self.items[self.root_id]
        return self._store_file(parent["id"], name, data, replace=True)

    def _store_file(self, parent_id: str, name: str, data: bytes, *, replace: bool) -> dict:
        existing = self._child(parent_id, name)
        if existing is not None:
            if not replace:
                raise FileExistsError(name)
            iid = existing["id"]
        else:
            iid = self._next_id("file")
        item = {
            "id": iid,
            "name": name,
            "size": len(data),
            "parentReference": {"id": parent_id},
            "file": {
                "mimeType": "application/octet-stream",
                "hashes": {"quickXorHash": quickxorhash_b64(data)},
            },
        }
        self.items[iid] = item
        self.blobs[iid] = data
        return item

    def path_of(self, item_id: str) -> str:
        parts = []
        cur = self.items.get(item_id)
        seen = set()
        while cur and cur["id"] != self.root_id and cur["id"] not in seen:
            seen.add(cur["id"])
            parts.append(cur["name"])
            pid = (cur.get("parentReference") or {}).get("id")
            cur = self.items.get(pid) if pid else None
        return "/".join(reversed(parts))

    def request_with_retry(
        self,
        method: str,
        url: str,
        *,
        headers=None,
        params=None,
        json_body=None,
        raw_bytes=None,
        stream=False,
    ):
        parsed = urlsplit(url)
        path = parsed.path

        class _Resp:
            def __init__(self, status_code: int, body=None, text: str = "") -> None:
                self.status_code = status_code
                self._body = body
                self.text = text or (json.dumps(body) if body is not None else "")
                self.headers = {}

            def json(self):
                return self._body

        # Upload session slice PUT
        if path.startswith("/upload-session/"):
            sid = path.split("/")[-1]
            sess = self.sessions.get(sid)
            if not sess:
                return _Resp(404)
            chunk = raw_bytes or b""
            sess["buf"].extend(chunk)
            cr = (headers or {}).get("Content-Range", "")
            m = re.match(r"bytes (\d+)-(\d+)/(\d+)", cr)
            if m and int(m.group(2)) + 1 == int(m.group(3)):
                data = bytes(sess["buf"])
                self.ops.append(("upload_session_complete", f"{sess['parent_id']}/{sess['name']}"))
                try:
                    item = self._store_file(
                        sess["parent_id"],
                        sess["name"],
                        data,
                        replace=sess["conflict"] == "replace",
                    )
                except FileExistsError:
                    return _Resp(409)
                self.sessions.pop(sid, None)
                return _Resp(201, item)
            return _Resp(202, {"nextExpectedRanges": [f"{len(sess['buf'])}-"]})

        # Strip /v1.0/users/<upn>/drive prefix
        m = re.match(r"^/v1\.0/users/[^/]+/drive(/.*)?$", path)
        if not m:
            return _Resp(404)
        sub = m.group(1) or ""

        if sub == "/root" and method == "GET":
            return _Resp(200, self.items[self.root_id])

        if sub.startswith("/root:/"):
            rel = sub[len("/root:/") :]
            it = self._by_root_path(rel)
            if it is None:
                return _Resp(404)
            return _Resp(200, it)

        # /items/{parent}:/{name}:/content
        m_content = re.match(r"^/items/([^/:]+):/([^/]+):/content$", sub)
        if m_content and method == "PUT":
            parent_id = unquote(m_content.group(1))
            name = unquote(m_content.group(2))
            behavior = (params or {}).get("@microsoft.graph.conflictBehavior", "fail")
            self.ops.append(("put_file", f"{parent_id}/{name}"))
            if parent_id not in self.items:
                return _Resp(404)
            if behavior == "rename" and self._child(parent_id, name) is not None:
                name = f"{name}.renamed"
            try:
                item = self._store_file(
                    parent_id, name, raw_bytes or b"", replace=(behavior == "replace")
                )
            except FileExistsError:
                return _Resp(409)
            return _Resp(201, item)

        # /items/{parent}:/{name}:/createUploadSession
        m_sess = re.match(r"^/items/([^/:]+):/([^/]+):/createUploadSession$", sub)
        if m_sess and method == "POST":
            parent_id = unquote(m_sess.group(1))
            name = unquote(m_sess.group(2))
            item_meta = (json_body or {}).get("item") or {}
            conflict = item_meta.get("@microsoft.graph.conflictBehavior", "fail")
            if conflict == "fail" and self._child(parent_id, name) is not None:
                return _Resp(409)
            if conflict == "rename" and self._child(parent_id, name) is not None:
                name = f"{name}.renamed"
            sid = self._next_id("sess")
            self.sessions[sid] = {
                "parent_id": parent_id,
                "name": name,
                "conflict": conflict,
                "buf": bytearray(),
            }
            self.ops.append(("open_session", f"{parent_id}/{name}"))
            return _Resp(200, {"uploadUrl": f"https://graph.microsoft.com/upload-session/{sid}"})

        # /items/{parent}:/{child}
        m_child_path = re.match(r"^/items/([^/:]+):/([^/]+)$", sub)
        if m_child_path and method == "GET":
            parent_id = unquote(m_child_path.group(1))
            name = unquote(m_child_path.group(2))
            it = self._child(parent_id, name)
            return _Resp(200, it) if it is not None else _Resp(404)

        # /items/{parent}/children
        m_children = re.match(r"^/items/([^/]+)/children$", sub)
        if m_children:
            parent_id = unquote(m_children.group(1))
            if parent_id not in self.items:
                return _Resp(404)
            if method == "GET":
                kids = [
                    it
                    for it in self.items.values()
                    if (it.get("parentReference") or {}).get("id") == parent_id
                ]
                return _Resp(200, {"value": kids})
            if method == "POST":
                name = str((json_body or {}).get("name") or "")
                existing = self._child(parent_id, name)
                if existing is not None:
                    return _Resp(409)
                iid = self._next_id("dir")
                folder = {
                    "id": iid,
                    "name": name,
                    "folder": {"childCount": 0},
                    "parentReference": {"id": parent_id},
                }
                self.items[iid] = folder
                self.ops.append(("mkdir", f"{parent_id}/{name}"))
                return _Resp(201, folder)

        # /items/{item_id}
        m_item = re.match(r"^/items/([^/]+)$", sub)
        if m_item:
            item_id = unquote(m_item.group(1))
            it = self.items.get(item_id)
            if method == "GET":
                return _Resp(200, it) if it is not None else _Resp(404)
            if method == "PATCH":
                if it is None:
                    return _Resp(404)
                new_name = (json_body or {}).get("name", it["name"])
                new_parent = ((json_body or {}).get("parentReference") or {}).get(
                    "id", (it.get("parentReference") or {}).get("id")
                )
                occ = self._child(new_parent, new_name)
                if occ is not None and occ["id"] != item_id:
                    return _Resp(409)
                it["name"] = new_name
                it["parentReference"] = {"id": new_parent}
                self.ops.append(("patch", f"{item_id}->{new_parent}/{new_name}"))
                return _Resp(200, it)
            if method == "DELETE":
                if it is None:
                    return _Resp(404)
                # Delete subtree if folder
                to_drop = [item_id]
                changed = True
                while changed:
                    changed = False
                    for candidate in list(self.items.values()):
                        pid = (candidate.get("parentReference") or {}).get("id")
                        if pid in to_drop and candidate["id"] not in to_drop:
                            to_drop.append(candidate["id"])
                            changed = True
                for drop_id in to_drop:
                    self.items.pop(drop_id, None)
                    self.blobs.pop(drop_id, None)
                self.ops.append(("delete", item_id))
                return _Resp(204)

        return _Resp(404)


@pytest.fixture()
def onedrive_env(fake_db, monkeypatch):
    od = FakeOneDrive()
    bucket = FakeBucket()
    monkeypatch.setattr(Config, "MIROIR_ONEDRIVE_ACTIF", True)
    monkeypatch.setattr(Config, "GRAPH_TENANT_ID", "tenant-1")
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_ID", "cid-1")
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_SECRET", "csecret-1")
    monkeypatch.setattr(Config, "COURRIER_UPN", "cabinet@example.ca")
    monkeypatch.setattr(Config, "COURRIER_ONEDRIVE_ROOT", "Dossiers")

    monkeypatch.setattr(
        graph_courrier,
        "jeton_courrier",
        lambda: SimpleNamespace(access_token="tok", expires_at_utc="2099-01-01T00:00:00Z", roles=()),
    )
    monkeypatch.setattr(graph_courrier, "_request_with_retry", od.request_with_retry)
    monkeypatch.setattr("firebase_admin.storage.bucket", lambda *a, **k: bucket)
    monkeypatch.setattr(miroir_onedrive.storage, "bucket", lambda *a, **k: bucket)
    monkeypatch.setattr(doc_model.storage, "bucket", lambda *a, **k: bucket)
    monkeypatch.setattr(tpl_model.storage, "bucket", lambda *a, **k: bucket)
    monkeypatch.setattr(admin_model.storage, "bucket", lambda *a, **k: bucket)

    dispatched_tasks: list[dict] = []

    class _FakeTasksClient:
        def queue_path(self, proj, loc, queue):
            return f"projects/{proj}/locations/{loc}/queues/{queue}"

        def create_task(self, *, request):
            dispatched_tasks.append(request)
            return SimpleNamespace(name="fake-task")

    monkeypatch.setattr(miroir_onedrive, "_tasks_client", lambda: _FakeTasksClient())
    return fake_db, bucket, od


# ═══════════════════════════════════════════════════════════════════════════
# PART A — 1j: CLASS, sensitivity, Sanitized Feed, & CalDAV / CardDAV Compliance
# ═══════════════════════════════════════════════════════════════════════════


def test_classification_fail_closed_for_dossier_and_general(fake_db):
    _seed_dossier(fake_db, dossier_id="d1")

    # Dossier-linked defaults to CONFIDENTIAL and refuses PUBLIC
    h, errs = hearing_model.create_hearing(
        {
            "dossier_id": "d1",
            "title": "Audience confidentielle",
            "hearing_type": "audience",
            "start_datetime": datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc),
        }
    )
    assert errs == []
    assert h["classification"] == "CONFIDENTIAL"

    h_bad, errs_bad = hearing_model.create_hearing(
        {
            "dossier_id": "d1",
            "title": "Interdit public",
            "hearing_type": "audience",
            "start_datetime": datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc),
            "classification": "PUBLIC",
        }
    )
    assert h_bad is None
    assert errs_bad

    # Non-dossier defaults safe (PRIVATE) and allows PUBLIC / PRIVATE / CONFIDENTIAL
    h_gen, errs_g = hearing_model.create_hearing(
        {
            "dossier_id": None,
            "title": "Rendez-vous personnel",
            "hearing_type": "consultation",
            "start_datetime": datetime(2026, 6, 16, 15, 0, tzinfo=timezone.utc),
        }
    )
    assert errs_g == []
    assert h_gen["classification"] == "PRIVATE"

    h_pub, errs_pub = hearing_model.create_hearing(
        {
            "dossier_id": None,
            "title": "Conférence publique",
            "hearing_type": "autre",
            "start_datetime": datetime(2026, 6, 17, 15, 0, tzinfo=timezone.utc),
            "classification": "PUBLIC",
        }
    )
    assert errs_pub == []
    assert h_pub["classification"] == "PUBLIC"

    # Moving a PUBLIC non-dossier hearing onto a dossier automatically upgrades it to CONFIDENTIAL
    moved, merrs = hearing_model.update_hearing(h_pub["id"], {"dossier_id": "d1"})
    assert merrs == []
    assert moved["classification"] == "CONFIDENTIAL"

    # Tasks and Notes obey the exact same secrecy rules
    t_doss, t_errs = task_model.create_task(
        {"dossier_id": "d1", "title": "Préparer mémoire", "classification": "PUBLIC"}
    )
    assert t_doss is None and t_errs

    t_ok, _ = task_model.create_task({"dossier_id": "d1", "title": "Préparer mémoire"})
    assert t_ok["classification"] == "CONFIDENTIAL"

    n_bad, n_errs = note_model.create_note(
        {"dossier_id": "d1", "title": "Stratégie", "content": "Secret", "classification": "PUBLIC"}
    )
    assert n_bad is None and n_errs

    n_ok, _ = note_model.create_note({"dossier_id": "d1", "title": "Stratégie", "content": "Secret"})
    assert n_ok["classification"] == "CONFIDENTIAL"


def test_graph_miroir_maps_classification_to_sensitivity():
    dossier_h = {
        "id": "h1",
        "dossier_id": "d1",
        "title": "Requête",
        "hearing_type": "audience",
        "start_datetime": datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc),
        "classification": "PUBLIC",  # corrupted/legacy field: fail closed because dossier_id is set
    }
    payload_doss = graph_miroir.construire_charge(dossier_h, False)
    assert payload_doss["sensitivity"] == "confidential"

    public_h = {
        "id": "h2",
        "dossier_id": None,
        "title": "Midi Barreau",
        "hearing_type": "autre",
        "start_datetime": datetime(2026, 7, 2, 16, 0, tzinfo=timezone.utc),
        "classification": "PUBLIC",
    }
    payload_pub = graph_miroir.construire_charge(public_h, False)
    assert payload_pub["sensitivity"] == "normal"

    priv_h = {**public_h, "classification": "PRIVATE"}
    assert graph_miroir.construire_charge(priv_h, False)["sensitivity"] == "private"


def test_sanitized_shared_feed_lifecycle_and_fail_closed(fake_db, monkeypatch):
    _seed_dossier(fake_db, dossier_id="d1", file_number="2026-001")
    app = _make_app(fake_db, monkeypatch)
    client = app.test_client()

    # Disabled by default -> 404
    resp_off = client.get("/calendrier-partage/n-importe-quoi.ics")
    assert resp_off.status_code == 404

    # Generate token via settings route
    _login_session(client)
    gen = client.post("/parametres/integrations/calendrier-partage/generer")
    assert gen.status_code == 200
    m = re.search(r"/calendrier-partage/([A-Za-z0-9_-]+)\.ics", gen.get_data(as_text=True))
    assert m is not None
    token = m.group(1)

    # Integrations doc stores ONLY SHA-256 hash, never plaintext token
    stored_integ = fake_db.collection("settings").document("integrations").get().to_dict()
    assert stored_integ[integ_model.SHARED_CALENDAR_TOKEN_HASH_KEY] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in json.dumps(stored_integ, default=str)

    # Regular integrations update preserves the shared token hash
    client.post(
        "/parametres/integrations/enregistrer",
        data={
            "bookings_subject_keywords": "Consultation",
            "bookings_type_par_mot_cle": "consultation=consultation",
            "bookings_type_defaut": "consultation",
            "graph_sender_name": "Cabinet Test",
            "lundi": "on",
            "mardi": "on",
            "mercredi": "on",
            "jeudi": "on",
            "vendredi": "on",
        },
    )
    stored_after = fake_db.collection("settings").document("integrations").get().to_dict()
    assert stored_after[integ_model.SHARED_CALENDAR_TOKEN_HASH_KEY] == stored_integ[integ_model.SHARED_CALENDAR_TOKEN_HASH_KEY]

    # Seed events within the live lookback/lookahead window of all sensitivity tiers
    now = datetime.now(timezone.utc).replace(hour=14, minute=0, second=0, microsecond=0)
    dt = now + timedelta(days=5)
    hearing_model.create_hearing(
        {
            "dossier_id": None,
            "title": "Congrès ouvert public",
            "hearing_type": "autre",
            "start_datetime": dt,
            "location": "Palais des congrès",
            "notes": "Public info",
            "classification": "PUBLIC",
        }
    )
    hearing_model.create_hearing(
        {
            "dossier_id": None,
            "title": "Médecin perso SECRET_PERSONNEL",
            "hearing_type": "autre",
            "start_datetime": dt + timedelta(days=1),
            "location": "Clinique XYZ",
            "notes": "Détails privés",
            "classification": "PRIVATE",
        }
    )
    # Plant a corrupted dossier event claiming PUBLIC in Firestore directly
    fake_db.collection("hearings").document("h-corrupt").set(
        {
            "id": "h-corrupt",
            "dossier_id": "d1",
            "dossier_file_number": "2026-001",
            "title": "Interrogatoire CLIENT_SECRET_NAME",
            "hearing_type": "interrogatoire",
            "start_datetime": dt + timedelta(days=2),
            "location": "Salle 3.14 SECRET_SALLE",
            "notes": "Stratégie ultra secrète",
            "conference_uri": "https://teams.microsoft.com/l/meetup-join/secret",
            "classification": "PUBLIC",
            "vevent_uid": "uid-h-corrupt",
            "ical_sequence": 0,
            "created_at": dt,
            "updated_at": dt,
        }
    )
    # Pending booking must NOT appear at all
    fake_db.collection("hearings").document("h-pending").set(
        {
            "id": "h-pending",
            "dossier_id": None,
            "title": "Pending Booking Secret Client",
            "hearing_type": "consultation",
            "start_datetime": dt + timedelta(days=3),
            "confirmation": "à_confirmer",
            "classification": "PUBLIC",
            "vevent_uid": "uid-h-pending",
            "created_at": dt,
            "updated_at": dt,
        }
    )

    # Wrong token refused, method guard enforced
    assert client.get("/calendrier-partage/wrong-token.ics").status_code == 404
    assert client.post(f"/calendrier-partage/{token}.ics").status_code == 405

    # Valid feed response
    feed = client.get(f"/calendrier-partage/{token}.ics")
    assert feed.status_code == 200
    assert "text/calendar" in feed.content_type
    assert "no-store" in (feed.headers.get("Cache-Control") or "")
    body = feed.get_data(as_text=True)

    assert "Congrès ouvert public" in body
    assert "Palais des congrès" in body
    assert "Public info" in body
    assert "Occupé" in body
    assert "SECRET_PERSONNEL" not in body
    assert "Détails privés" not in body
    assert "Clinique XYZ" not in body
    assert "CLIENT_SECRET_NAME" not in body
    assert "Stratégie ultra secrète" not in body
    assert "SECRET_SALLE" not in body
    assert "2026-001" not in body
    assert "teams.microsoft.com" not in body
    assert "Pending Booking Secret Client" not in body
    assert "TZID:America/Toronto" in body

    # Log and trace redaction
    import logging as _py_logging

    rec = _py_logging.LogRecord(
        "test", _py_logging.INFO, "", 0, f"GET /calendrier-partage/{token}.ics HTTP/1.1", (), None
    )
    logging_setup.RedactionFilter().filter(rec)
    assert token not in rec.getMessage()
    assert "/calendrier-partage/<redacted>" in rec.getMessage()
    assert token not in tracing_setup._strip_query(f"/calendrier-partage/{token}.ics")

    # Truncated read raises -> 503 Retry-After
    with mock.patch("routes.calendrier_partage.list_hearings_range_strict", side_effect=RuntimeError("truncated")):
        res503 = client.get(f"/calendrier-partage/{token}.ics")
        assert res503.status_code == 503
        assert res503.headers.get("Retry-After") == "60"

    # Revoke immediately invalidates the old token
    client.post("/parametres/integrations/calendrier-partage/revoquer")
    assert client.get(f"/calendrier-partage/{token}.ics").status_code == 404


def test_caldav_sequence_vtimezone_allday_valarm_and_time_range(fake_db, monkeypatch):
    _seed_dossier(fake_db, dossier_id="d1", file_number="2026-001")

    # Timed hearing in EDT (July = UTC-4) -> America/Toronto VTIMEZONE + SEQUENCE increment
    h, _ = hearing_model.create_hearing(
        {
            "dossier_id": "d1",
            "title": "Conférence de gestion",
            "hearing_type": "audience",
            "start_datetime": datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc),  # 09:30 EDT
            "end_datetime": datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc),  # 10:30 EDT
        }
    )
    assert h["ical_sequence"] == 0
    cal_str = hearing_model.hearing_to_vevent(h)
    assert "BEGIN:VTIMEZONE" in cal_str
    assert "TZID:America/Toronto" in cal_str
    assert "DTSTART;TZID=America/Toronto:20260715T093000" in cal_str
    assert "SEQUENCE:0" in cal_str
    assert "CLASS:CONFIDENTIAL" in cal_str

    # Multi-VALARM round-trip through vevent_to_hearing & web edit preservation
    cal_with_alarms = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//Test//FR\r\n"
        + ical_tz.build_america_toronto_vtimezone().to_ical().decode("utf-8")
        + "\r\nBEGIN:VEVENT\r\n"
        f"UID:{h['vevent_uid']}\r\n"
        "DTSTAMP:20260701T120000Z\r\n"
        "DTSTART;TZID=America/Toronto:20260715T100000\r\n"
        "DTEND;TZID=America/Toronto:20260715T110000\r\n"
        "SUMMARY:Conférence modifiée DAV\r\n"
        "CLASS:PRIVATE\r\n"
        "SEQUENCE:1\r\n"
        "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT30M\r\nDESCRIPTION:Rappel 30m\r\nEND:VALARM\r\n"
        "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-P1D\r\nDESCRIPTION:Rappel veille\r\nEND:VALARM\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    parsed = hearing_model.vevent_to_hearing(cal_with_alarms)
    assert parsed["classification"] == "PRIVATE"
    assert len(parsed["dav_valarms"]) == 2

    h2, uerrs = hearing_model.update_hearing(
        h["id"], {k: v for k, v in parsed.items() if k != "vevent_uid"}
    )
    assert uerrs == []
    assert h2["ical_sequence"] == 1
    assert len(h2["dav_valarms"]) == 2
    assert h2["start_datetime"] == datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)

    # Web title edit without changing reminder_minutes preserves client VALARMs & increments SEQUENCE
    h3, uerrs2 = hearing_model.update_hearing(h["id"], {"title": "Titre web"})
    assert uerrs2 == []
    assert h3["ical_sequence"] == 2
    assert len(h3["dav_valarms"]) == 2
    ics3 = hearing_model.hearing_to_vevent(h3)
    assert "SEQUENCE:2" in ics3
    assert ics3.count("BEGIN:VALARM") == 2

    # All-day hearing emits VALUE=DATE without TZID
    h_allday, aerrs = hearing_model.create_hearing(
        {
            "dossier_id": "d1",
            "title": "Délai péremption",
            "type": "autre",
            "start_datetime": datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc),
            "all_day": True,
        }
    )
    assert aerrs == []
    ics_allday = hearing_model.hearing_to_vevent(h_allday)
    assert "DTSTART;VALUE=DATE:20260901" in ics_allday
    assert "DTSTART;TZID=America/Toronto" not in ics_allday

    # Task & Note SEQUENCE + VTIMEZONE timed VTODO
    t1, _ = task_model.create_task(
        {
            "dossier_id": "d1",
            "title": "Tâche timed",
            "due_date": datetime(2026, 12, 10, 19, 0, tzinfo=timezone.utc),  # 14:00 EST
        }
    )
    assert t1["ical_sequence"] == 0
    t2, _ = task_model.update_task(t1["id"], {"title": "Tâche v2"})
    assert t2["ical_sequence"] == 1
    t_ics = task_model.task_to_vtodo(t2)
    assert "SEQUENCE:1" in t_ics
    assert "CLASS:CONFIDENTIAL" in t_ics

    n1, _ = note_model.create_note({"dossier_id": "d1", "title": "N1", "content": "Corps"})
    assert n1["ical_sequence"] == 0
    n2, _ = note_model.update_note(n1["id"], {"title": "N2"})
    assert n2["ical_sequence"] == 1
    n_ics = note_model.note_to_vjournal(n2)
    assert "SEQUENCE:1" in n_ics
    assert "CLASS:CONFIDENTIAL" in n_ics

    # Server-side <C:time-range> REPORT filtering on /dav/dossier-d1/
    import bcrypt as _bcrypt

    app = _make_app(fake_db, monkeypatch)
    monkeypatch.setattr(Config, "DAV_USERNAME", "test@example.com")
    monkeypatch.setattr(
        Config,
        "DAV_PASSWORD_HASH",
        _bcrypt.hashpw(b"secret", _bcrypt.gensalt(4)).decode("ascii"),
    )
    client = app.test_client()
    creds = base64.b64encode(b"test@example.com:secret").decode("ascii")
    query_xml = (
        '<?xml version="1.0" encoding="utf-8" ?>'
        '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        "  <D:prop><D:getetag/></D:prop>"
        '  <C:filter><C:comp-filter name="VCALENDAR">'
        '    <C:comp-filter name="VEVENT">'
        '      <C:time-range start="20260701T000000Z" end="20260801T000000Z"/>'
        "    </C:comp-filter>"
        "  </C:comp-filter></C:filter>"
        "</C:calendar-query>"
    )
    rep = client.open(
        "/dav/dossier-d1/",
        method="REPORT",
        data=query_xml,
        content_type="application/xml",
        headers={"Authorization": f"Basic {creds}", "Depth": "1"},
    )
    assert rep.status_code == 207
    xml_out = rep.get_data(as_text=True)
    assert h["id"] in xml_out
    assert h_allday["id"] not in xml_out  # Sept 1 outside July window!


def test_vcard4_unfolding_yearless_bday_and_secondary_email_tel(fake_db):
    raw_vcard = (
        "BEGIN:VCARD\r\n"
        "VERSION:4.0\r\n"
        "UID:urn:uuid:test-vc-1\r\n"
        "FN:Me Jeanne Tremblay\r\n"
        "N:Tremblay;Jeanne;;Me;\r\n"
        "EMAIL;TYPE=work;PREF=1:j.tremblay@cabinet.ca\r\n"
        "EMAIL;TYPE=work:secretariat@cabinet.ca\r\n"
        "EMAIL;TYPE=home:jeanne@maison.ca\r\n"
        "TEL;TYPE=cell;PREF=1:514-555-0100\r\n"
        "TEL;TYPE=cell:514-555-0199\r\n"
        "TEL;TYPE=work:450-555-0200\r\n"
        "LANG:\r\n"
        " fr\r\n"
        "GENDER:\r\n"
        " F\r\n"
        "BDAY:--0229\r\n"
        "END:VCARD\r\n"
    )
    parsed = partie_model.vcard_to_partie(raw_vcard)
    assert parsed["language"] == "fr"
    assert parsed["gender"] == "F"
    assert parsed["birth_date_mmdd"] == "--0229"
    assert parsed["email_work"] == "j.tremblay@cabinet.ca"
    assert parsed["email"] == "jeanne@maison.ca"
    assert parsed["extra_emails"] == [{"types": ["WORK"], "value": "secretariat@cabinet.ca"}]
    assert parsed["phone_cell"] == "+15145550100"
    assert parsed["phone_work"] == "+14505550200"
    assert parsed["extra_tels"] == [{"types": ["CELL"], "value": "+15145550199"}]

    created, errs = partie_model.create_partie(parsed)
    assert errs == []
    assert created["birth_date_mmdd"] == "--0229"

    emitted = partie_model.partie_to_vcard(created)
    assert "BDAY:--0229" in emitted
    assert "secretariat@cabinet.ca" in emitted
    assert "5145550199" in emitted

    # Invalid yearless month/day refused fail-closed
    bad_bday, berrs = partie_model.create_partie(
        {"first_name": "Test", "last_name": "Bad", "birth_date_mmdd": "--0230"}
    )
    assert bad_bday is None and berrs


# ═══════════════════════════════════════════════════════════════════════════
# PART B — 2b: One-Way OneDrive Mirroring (Athena -> OneDrive)
# ═══════════════════════════════════════════════════════════════════════════


def test_miroir_onedrive_kill_switch_and_origin_guards(fake_db, monkeypatch):
    app = _make_app(fake_db, monkeypatch)
    client = app.test_client()

    # Forbidden without App Engine headers
    assert client.post("/taches/onedrive/event", json={"action": "sync_document"}).status_code == 403
    assert client.get("/taches/onedrive/sync").status_code == 403

    # Kill switch off -> returns disabled without calling Graph or Storage
    monkeypatch.setattr(Config, "MIROIR_ONEDRIVE_ACTIF", False)
    assert miroir_onedrive.enqueue_event("sync_document", document_id="doc-1") is False

    ev = client.post(
        "/taches/onedrive/event",
        json={"action": "sync_document", "document_id": "doc-1"},
        headers={"X-AppEngine-QueueName": Config.MIROIR_ONEDRIVE_QUEUE},
    )
    assert ev.status_code == 200
    assert ev.get_json()["actif"] is False

    cron = client.get("/taches/onedrive/sync", headers={"X-Appengine-Cron": "true"})
    assert cron.status_code == 200
    assert cron.get_json()["actif"] is False

    # Kill switch ON: poison JSON -> 200 refused (no retry storm); transient raise -> 500 retry
    monkeypatch.setattr(Config, "MIROIR_ONEDRIVE_ACTIF", True)
    monkeypatch.setattr(Config, "GRAPH_TENANT_ID", "t")
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_ID", "c")
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_SECRET", "s")
    monkeypatch.setattr(Config, "COURRIER_UPN", "u@x.ca")
    monkeypatch.setattr(Config, "COURRIER_ONEDRIVE_ROOT", "Dossiers")

    bad_json = client.post(
        "/taches/onedrive/event",
        data="not-json",
        content_type="application/json",
        headers={"X-AppEngine-QueueName": Config.MIROIR_ONEDRIVE_QUEUE},
    )
    assert bad_json.status_code == 200
    assert bad_json.get_json()["outcome"] == "refused"

    with mock.patch("services.miroir_onedrive.handle_event", side_effect=RuntimeError("graph 503")):
        retry_res = client.post(
            "/taches/onedrive/event",
            json={"action": "sync_document", "document_id": "doc-1"},
            headers={"X-AppEngine-QueueName": Config.MIROIR_ONEDRIVE_QUEUE},
        )
        assert retry_res.status_code == 500


def test_miroir_dispatch_failure_never_fails_user_write(onedrive_env, monkeypatch):
    fake_db, _bucket, _od = onedrive_env
    _seed_dossier(fake_db, dossier_id="d1", file_number="2026-001")

    payload_bytes = b"%PDF-1.4 test payload\n"
    with mock.patch.object(
        miroir_onedrive, "_tasks_client", side_effect=RuntimeError("Cloud Tasks down")
    ):
        doc, errs = doc_model.upload_document(
            "d1",
            "2026-001",
            io.BytesIO(payload_bytes),
            "contrat.pdf",
            len(payload_bytes),
            {"display_name": "Contrat", "category": "pièce"},
            UID,
        )
    assert errs == []
    assert doc is not None
    assert doc["id"]


def test_miroir_dossier_tree_docs_chunked_dedup_renames_moves_deletes(onedrive_env, monkeypatch):
    fake_db, _bucket, od = onedrive_env
    _seed_dossier(fake_db, dossier_id="d1", file_number="2026-001")
    # Pre-seed an existing Courrier dossier folder with client suffix: must adopt, not duplicate!
    od.mkdir_path("Dossiers/2026-001 — Tremblay c. Bouchard")

    # Ensure Athena 18-folder tree and sync it
    report, terrs = folder_model.ensure_default_tree("d1")
    assert terrs == []
    miroir_onedrive.handle_event({"action": "sync_dossier_tree", "dossier_id": "d1"})

    # Verify nested default folders exist under the adopted Courrier root
    folder_paths = {od.path_of(it["id"]) for it in od.items.values() if "folder" in it}
    assert any(p.startswith("Dossiers/2026-001 — Tremblay c. Bouchard/") for p in folder_paths)
    pieces_folder = report.resolved.get("pieces") or next(iter(report.resolved.values()))

    # Small document upload + mirror -> idempotent re-sync does zero PUTs
    pdf_bytes = b"%PDF-1.4\n%Test small PDF\n"
    d_small, derrs = doc_model.upload_document(
        "d1",
        "2026-001",
        io.BytesIO(pdf_bytes),
        "facture.pdf",
        len(pdf_bytes),
        {"display_name": "Facture initiale", "category": "pièce", "folder_id": pieces_folder["id"]},
        UID,
    )
    assert derrs == []
    od.ops.clear()
    res1 = miroir_onedrive.handle_event({"action": "sync_document", "document_id": d_small["id"]})
    assert res1["outcome"] == "uploaded"
    book_small = fake_db.collection("onedrive_miroir").document(f"doc:{d_small['id']}").get().to_dict()
    assert od.blobs[book_small["onedrive_item_id"]] == pdf_bytes
    assert "/Facture initiale.pdf" in od.path_of(book_small["onedrive_item_id"])

    od.ops.clear()
    res2 = miroir_onedrive.handle_event({"action": "sync_document", "document_id": d_small["id"]})
    assert res2["outcome"] == "unchanged"
    assert od.ops == []

    # Large document (> threshold) uses chunked upload session
    monkeypatch.setattr(graph_courrier, "ONEDRIVE_SIMPLE_MAX_BYTES", 100)
    monkeypatch.setattr(graph_courrier, "ONEDRIVE_UPLOAD_CHUNK_BYTES", 64)
    big_bytes = b"%PDF-1.4\n" + (b"X" * 240)
    d_big, _ = doc_model.upload_document(
        "d1",
        "2026-001",
        io.BytesIO(big_bytes),
        "gros.pdf",
        len(big_bytes),
        {"display_name": "Gros Mémoire", "category": "procédure", "folder_id": pieces_folder["id"]},
        UID,
    )
    od.ops.clear()
    res_big = miroir_onedrive.handle_event({"action": "sync_document", "document_id": d_big["id"]})
    assert res_big["outcome"] == "uploaded"
    assert any(op == "open_session" for op, _ in od.ops)
    assert any(op == "upload_session_complete" for op, _ in od.ops)
    book_big = fake_db.collection("onedrive_miroir").document(f"doc:{d_big['id']}").get().to_dict()
    assert od.blobs[book_big["onedrive_item_id"]] == big_bytes


def test_miroir_courrier_teams_dedup_renames_moves_and_folder_delete_move(onedrive_env):
    fake_db, bucket, od = onedrive_env
    _seed_dossier(fake_db, dossier_id="d1", file_number="2026-001")
    f_src, _ = folder_model.create_folder("d1", "SourceTest")
    f_dst, _ = folder_model.create_folder("d1", "DestTest")

    # 1) Pre-placed Courrier file matched via courrier_messages.onedrive_item_id -> zero upload
    courrier_bytes = b"From: a@b.ca\r\nSubject: Test\r\n\r\nBody"
    doc_c_path = f"users/{UID}/dossiers/d1/documents/doc-courrier/courriel.eml"
    bucket.put(doc_c_path, courrier_bytes, content_type="message/rfc822")
    fake_db.collection("documents").document("doc-courrier").set(
        {
            "id": "doc-courrier",
            "dossier_id": "d1",
            "folder_id": f_src["id"],
            "display_name": "Courriel entrant",
            "filename": "courriel.eml",
            "file_type": "message/rfc822",
            "file_size": len(courrier_bytes),
            "storage_path": doc_c_path,
        }
    )
    od_c = od.put_file_direct("Dossiers/2026-001/SourceTest/courriel.eml", courrier_bytes)
    fake_db.collection("courrier_messages").document("c1").set(
        {
            "dossier_id": "d1",
            "fichiers": [{"document_id": "doc-courrier", "onedrive_item_id": od_c["id"]}],
        }
    )
    od.ops.clear()
    res_c = miroir_onedrive.handle_event({"action": "sync_document", "document_id": "doc-courrier"})
    assert res_c["outcome"] == "deduplicated"
    assert not any(op in ("put_file", "open_session") for op, _ in od.ops)

    # 2) Pre-placed Teams transcript matched via meeting_transcripts -> zero upload
    tx_bytes = _minimal_docx_bytes("Transcription Teams")
    doc_t_path = f"users/{UID}/dossiers/d1/documents/doc-teams/tx.docx"
    bucket.put(
        doc_t_path,
        tx_bytes,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    fake_db.collection("documents").document("doc-teams").set(
        {
            "id": "doc-teams",
            "dossier_id": "d1",
            "folder_id": f_src["id"],
            "display_name": "Transcription réunion",
            "filename": "tx.docx",
            "file_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "file_size": len(tx_bytes),
            "storage_path": doc_t_path,
        }
    )
    od_t = od.put_file_direct("Dossiers/2026-001/SourceTest/tx.docx", tx_bytes)
    fake_db.collection("meeting_transcripts").document("h-teams").set(
        {
            "dossier_id": "d1",
            "versement": {
                "transcript_document_id": "doc-teams",
                "onedrive_transcript_item_id": od_t["id"],
            },
        }
    )
    od.ops.clear()
    res_t = miroir_onedrive.handle_event({"action": "sync_document", "document_id": "doc-teams"})
    assert res_t["outcome"] == "deduplicated"
    assert not any(op in ("put_file", "open_session") for op, _ in od.ops)

    # 3) Rename + move regular document -> PATCH without re-uploading bytes
    doc_normal_bytes = b"%PDF-1.4 doc normal\n"
    doc_normal, _ = doc_model.upload_document(
        "d1",
        "2026-001",
        io.BytesIO(doc_normal_bytes),
        "piece.pdf",
        len(doc_normal_bytes),
        {"display_name": "Pièce P-1", "category": "pièce", "folder_id": f_src["id"]},
        UID,
    )
    miroir_onedrive.handle_event({"action": "sync_document", "document_id": doc_normal["id"]})
    _doc_moved, errs, changed = doc_model.update_metadata(
        doc_normal["id"],
        {"display_name": "Pièce P-1 révisée"},
        folder_id=f_dst["id"],
        expected_etag=doc_normal["etag"],
    )
    assert errs == [] and changed is True
    od.ops.clear()
    res_mv = miroir_onedrive.handle_event({"action": "sync_document", "document_id": doc_normal["id"]})
    assert res_mv["outcome"] == "updated"
    assert not any(op in ("put_file", "open_session") for op, _ in od.ops)
    book_n = fake_db.collection("onedrive_miroir").document(f"doc:{doc_normal['id']}").get().to_dict()
    assert od.path_of(book_n["onedrive_item_id"]) == "Dossiers/2026-001/DestTest/Pièce P-1 révisée.pdf"

    # 4) Folder rename + move
    f_child, _ = folder_model.create_folder("d1", "SousAncien", parent_folder_id=f_src["id"])
    miroir_onedrive.handle_event({"action": "sync_folder", "dossier_id": "d1", "folder_id": f_child["id"]})
    f_child_ren, _, _ = folder_model.rename_folder("d1", f_child["id"], "SousNouveau")
    folder_model.move_folder("d1", f_child["id"], f_dst["id"], expected_etag=f_child_ren["etag"])
    miroir_onedrive.handle_event({"action": "sync_folder", "dossier_id": "d1", "folder_id": f_child["id"]})
    book_f = fake_db.collection("onedrive_miroir").document(f"folder:{f_child['id']}").get().to_dict()
    assert od.path_of(book_f["onedrive_item_id"]) == "Dossiers/2026-001/DestTest/SousNouveau"

    # 5) Delete folder with contents="move": moves surviving docs OUT before deleting OneDrive folder
    surv_bytes = b"%PDF-1.4 survivor\n"
    surviving_doc, _ = doc_model.upload_document(
        "d1",
        "2026-001",
        io.BytesIO(surv_bytes),
        "survivant.pdf",
        len(surv_bytes),
        {"display_name": "Survivant", "category": "pièce", "folder_id": f_child["id"]},
        UID,
    )
    miroir_onedrive.handle_event({"action": "sync_document", "document_id": surviving_doc["id"]})
    dispatched: list[tuple[str, dict]] = []
    with mock.patch.object(
        miroir_onedrive,
        "enqueue_event",
        side_effect=lambda act, **kw: dispatched.append((act, kw)) or True,
    ):
        ok, derr, _info = folder_model.delete_folder(
            "d1", f_child["id"], contents=folder_model.CONTENTS_MOVE
        )
    assert ok is True and derr == ""
    del_ev = [kw for act, kw in dispatched if act == "delete_folder"][0]
    miroir_onedrive.handle_event({"action": "delete_folder", **del_ev})

    # Surviving document moved safely to DestTest (parent), while f_child disappeared!
    book_surv = fake_db.collection("onedrive_miroir").document(f"doc:{surviving_doc['id']}").get().to_dict()
    assert od.path_of(book_surv["onedrive_item_id"]) == "Dossiers/2026-001/DestTest/Survivant.pdf"
    assert book_f["onedrive_item_id"] not in od.items

    # 6) Delete document removes it cleanly from OneDrive even after GCS + Firestore record gone
    del_ok, del_msg = doc_model.delete_document(doc_normal["id"])
    assert del_ok is True and del_msg == ""
    miroir_onedrive.handle_event({"action": "delete_document", "document_id": doc_normal["id"]})
    assert book_n["onedrive_item_id"] not in od.items
    assert not fake_db.collection("onedrive_miroir").document(f"doc:{doc_normal['id']}").get().exists


def test_miroir_firm_receipts_completed_statements_only_templates_and_proof_exclusion(onedrive_env):
    fake_db, bucket, od = onedrive_env

    # 1) Accounts + Receipts (Admin + Trust)
    fake_db.collection("admin_accounts").document("acc-a").set({"id": "acc-a", "name": "Courant BMO"})
    fake_db.collection("trust_accounts").document("acc-t").set({"id": "acc-t", "name": "Fidéicommis BMO"})

    adm_receipt_path = f"users/{UID}/administration/tx-a1/pieces/{uuid.uuid4()}.pdf"
    bucket.put(adm_receipt_path, b"%PDF-1.4 adm receipt\n", content_type="application/pdf")
    fake_db.collection("admin_transactions").document("tx-a1").set(
        {
            "id": "tx-a1",
            "account_id": "acc-a",
            "date": datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc),
            "sequence": 12,
            "receipt_storage_path": adm_receipt_path,
            "receipt_filename": "hydro.pdf",
            "receipt_file_type": "application/pdf",
        }
    )
    trust_receipt_path = f"users/{UID}/fideicommis/tx-t1/{uuid.uuid4()}/cheque.pdf"
    bucket.put(trust_receipt_path, b"%PDF-1.4 trust receipt\n", content_type="application/pdf")
    fake_db.collection("trust_transactions").document("tx-t1").set(
        {
            "id": "tx-t1",
            "account_id": "acc-t",
            "date": datetime(2026, 5, 2, 0, 0, tzinfo=timezone.utc),
            "sequence": 3,
            "receipt_storage_path": trust_receipt_path,
            "receipt_filename": "cheque.pdf",
            "receipt_file_type": "application/pdf",
        }
    )

    assert miroir_onedrive.handle_event({"action": "sync_receipt", "register": "admin", "tx_id": "tx-a1"})["outcome"] == "uploaded"
    assert miroir_onedrive.handle_event({"action": "sync_receipt", "register": "trust", "tx_id": "tx-t1"})["outcome"] == "uploaded"
    b_adm = fake_db.collection("onedrive_miroir").document("receipt:admin:tx-a1").get().to_dict()
    b_tru = fake_db.collection("onedrive_miroir").document("receipt:trust:tx-t1").get().to_dict()
    assert od.path_of(b_adm["onedrive_item_id"]) == "Cabinet/Comptabilité/Administration/Pièces justificatives/Courant BMO/2026-04/0012_hydro.pdf"
    assert od.path_of(b_tru["onedrive_item_id"]) == "Cabinet/Comptabilité/Fidéicommis/Pièces justificatives/Fidéicommis BMO/2026-05/0003_cheque.pdf"

    # 2) Draft vs Completed reconciliations: NEVER mirror drafts!
    st_id_draft = str(uuid.uuid4())
    draft_path = f"users/{UID}/administration/conciliations/rec-draft/releve-{st_id_draft}.pdf"
    bucket.put(draft_path, b"%PDF-1.4 draft\n", content_type="application/pdf")
    fake_db.collection("admin_reconciliations").document("rec-draft").set(
        {
            "id": "rec-draft",
            "account_id": "acc-a",
            "status": "brouillon",
            "period_end": datetime(2026, 4, 30, 0, 0, tzinfo=timezone.utc),
            "statement_id": st_id_draft,
            "statement_storage_path": draft_path,
            "statement_file_type": "application/pdf",
        }
    )
    res_draft = miroir_onedrive.handle_event(
        {"action": "sync_reconciliation", "register": "admin", "reconciliation_id": "rec-draft"}
    )
    assert res_draft["outcome"] == "skipped_not_completed"
    assert not fake_db.collection("onedrive_miroir").document("statement:admin:rec-draft").get().exists

    st_id_done = str(uuid.uuid4())
    done_path = f"users/{UID}/fideicommis/conciliations/rec-done-1234/releve-{st_id_done}.pdf"
    bucket.put(done_path, b"%PDF-1.4 completed\n", content_type="application/pdf")
    fake_db.collection("trust_reconciliations").document("rec-done-1234").set(
        {
            "id": "rec-done-1234",
            "account_id": "acc-t",
            "status": "complétée",
            "period_end": datetime(2026, 4, 30, 0, 0, tzinfo=timezone.utc),
            "statement_id": st_id_done,
            "statement_storage_path": done_path,
            "statement_file_type": "application/pdf",
        }
    )
    res_done = miroir_onedrive.handle_event(
        {"action": "sync_reconciliation", "register": "trust", "reconciliation_id": "rec-done-1234"}
    )
    assert res_done["outcome"] == "uploaded"
    b_stmt = fake_db.collection("onedrive_miroir").document("statement:trust:rec-done-1234").get().to_dict()
    assert od.path_of(b_stmt["onedrive_item_id"]) == "Cabinet/Comptabilité/Fidéicommis/Relevés bancaires/Fidéicommis BMO/2026/releve-2026-04-30-rec-done.pdf"

    # 3) Word templates FR + EN, rename/recategorize, new version, delete
    fr_docx = _minimal_docx_bytes("Version FR")
    en_docx = _minimal_docx_bytes("Version EN")
    tpl, tpl_errs = tpl_model.create_template(
        io.BytesIO(fr_docx),
        "med_fr.docx",
        len(fr_docx),
        {"name": "Mise en demeure", "category": "correspondance"},
        UID,
        en_file_stream=io.BytesIO(en_docx),
        en_filename="med_en.docx",
        en_file_size=len(en_docx),
    )
    assert tpl_errs == []
    miroir_onedrive.handle_event({"action": "sync_template", "template_id": tpl["id"]})
    b_fr = fake_db.collection("onedrive_miroir").document(f"template:{tpl['id']}:fr").get().to_dict()
    b_en = fake_db.collection("onedrive_miroir").document(f"template:{tpl['id']}:en").get().to_dict()
    assert od.path_of(b_fr["onedrive_item_id"]) == "Cabinet/Gabarits/FR/Correspondance/Mise en demeure.docx"
    assert od.path_of(b_en["onedrive_item_id"]) == "Cabinet/Gabarits/EN/Correspondance/Mise en demeure.docx"

    # Rename & recategorize + update FR file version
    fr_v2 = _minimal_docx_bytes("Version FR 2")
    _tpl2, uerrs, uchanged = tpl_model.update_template(
        tpl["id"],
        {"name": "Demande introductive", "category": "procédure"},
        io.BytesIO(fr_v2),
        "dem_v2.docx",
        len(fr_v2),
        expected_etag=tpl["etag"],
    )
    assert uerrs == [] and uchanged is True
    miroir_onedrive.handle_event({"action": "sync_template", "template_id": tpl["id"]})
    b_fr2 = fake_db.collection("onedrive_miroir").document(f"template:{tpl['id']}:fr").get().to_dict()
    b_en2 = fake_db.collection("onedrive_miroir").document(f"template:{tpl['id']}:en").get().to_dict()
    assert od.path_of(b_fr2["onedrive_item_id"]) == "Cabinet/Gabarits/FR/Procédures/Demande introductive.docx"
    assert od.blobs[b_fr2["onedrive_item_id"]] == fr_v2
    assert od.path_of(b_en2["onedrive_item_id"]) == "Cabinet/Gabarits/EN/Procédures/Demande introductive.docx"

    tpl_model.delete_template(tpl["id"])
    miroir_onedrive.handle_event({"action": "delete_template", "template_id": tpl["id"]})
    assert b_fr2["onedrive_item_id"] not in od.items
    assert b_en2["onedrive_item_id"] not in od.items

    # 4) Hard exclusion: identity proofs under users/*/parties/... NEVER mirror!
    _seed_dossier(fake_db, dossier_id="d1", file_number="2026-001")
    proof_path = f"users/{UID}/parties/p1/pieces_identite/{uuid.uuid4()}.pdf"
    bucket.put(proof_path, b"%PDF-1.4 passport\n", content_type="application/pdf")
    fake_db.collection("documents").document("doc-rogue-proof").set(
        {
            "id": "doc-rogue-proof",
            "dossier_id": "d1",
            "display_name": "Passeport",
            "filename": "passeport.pdf",
            "file_type": "application/pdf",
            "storage_path": proof_path,
        }
    )
    od.ops.clear()
    res_excl = miroir_onedrive.handle_event({"action": "sync_document", "document_id": "doc-rogue-proof"})
    assert res_excl["outcome"] == "skipped_excluded"
    assert od.ops == []

    # 5) Periodic catch-up cron sweep syncs everything missing AND cleans orphans, skipping drafts & proofs
    fake_db.collection("documents").document("orphan-doc").delete()
    orphan_od = od.put_file_direct("Dossiers/2026-001/orphan.pdf", b"orphan")
    fake_db.collection("onedrive_miroir").document("doc:orphan-doc").set(
        {"entity_type": "document", "entity_id": "orphan-doc", "onedrive_item_id": orphan_od["id"]}
    )
    sweep = miroir_onedrive.executer_rattrapage_cron()
    assert sweep["actif"] is True
    assert sweep["errors"] == 0
    assert sweep["deleted"] >= 1
    assert orphan_od["id"] not in od.items
    assert not fake_db.collection("onedrive_miroir").document("statement:admin:rec-draft").get().exists
    assert not fake_db.collection("onedrive_miroir").document("doc:doc-rogue-proof").get().exists


def test_settings_shared_calendar_generates_https_url_behind_proxy(fake_db, monkeypatch):
    app = _make_app(fake_db, monkeypatch)
    client = app.test_client()
    with client.session_transaction(base_url="http://athena.poirierlavoie.ca") as sess:
        sess["user_id"] = UID
        sess["email"] = "test@example.com"
        sess["sid"] = "s-test"
        sess["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=2)

    monkeypatch.setattr(Config, "ENV", "production")
    resp = client.post(
        "/parametres/integrations/calendrier-partage/generer",
        base_url="http://athena.poirierlavoie.ca",
    )
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "https://athena.poirierlavoie.ca/calendrier-partage/" in html
    assert "http://athena.poirierlavoie.ca/calendrier-partage/" not in html

    monkeypatch.setattr(Config, "ENV", "development")
    resp_xfp = client.post(
        "/parametres/integrations/calendrier-partage/generer",
        base_url="http://athena.poirierlavoie.ca",
        headers={"X-Forwarded-Proto": "https"},
    )
    assert resp_xfp.status_code == 200
    assert "https://athena.poirierlavoie.ca/calendrier-partage/" in resp_xfp.get_data(as_text=True)


def test_caldav_valarm_deletion_and_utc_last_modified_coercion(fake_db, monkeypatch):
    import bcrypt as _bcrypt

    _seed_dossier(fake_db, dossier_id="d1", file_number="2026-001")
    h, errs = hearing_model.create_hearing(
        {
            "dossier_id": "d1",
            "title": "Audience avec rappel",
            "hearing_type": "audience",
            "start_datetime": datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc),
            "dav_valarms": [
                "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT15M\r\nDESCRIPTION:Rappel\r\nEND:VALARM"
            ],
            "reminder_minutes": 15,
        }
    )
    assert not errs
    t, errs_t = task_model.create_task(
        {
            "dossier_id": "d1",
            "title": "Tâche avec alarme",
            "dav_valarms": [
                "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT30M\r\nDESCRIPTION:Rappel\r\nEND:VALARM"
            ],
        }
    )
    assert not errs_t

    app = _make_app(fake_db, monkeypatch)
    monkeypatch.setattr(Config, "DAV_USERNAME", "test@example.com")
    monkeypatch.setattr(
        Config,
        "DAV_PASSWORD_HASH",
        _bcrypt.hashpw(b"secret", _bcrypt.gensalt(4)).decode("ascii"),
    )
    client = app.test_client()
    creds = base64.b64encode(b"test@example.com:secret").decode("ascii")
    auth_hdr = {"Authorization": f"Basic {creds}"}

    # Phone deletes all VALARMs on VEVENT -> reminder_minutes cleared to 0, no alarm re-emitted
    vevent_no_alarm = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//FR\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{h['vevent_uid']}\r\n"
        "SUMMARY:Audience avec rappel\r\n"
        "DTSTART;TZID=America/Toronto:20260715T093000\r\n"
        "DTEND;TZID=America/Toronto:20260715T103000\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    put_h = client.put(
        f"/dav/dossier-d1/{h['id']}.ics",
        data=vevent_no_alarm.encode("utf-8"),
        content_type="text/calendar",
        headers=auth_hdr,
    )
    assert put_h.status_code == 204
    stored_h = hearing_model.get_hearing(h["id"])
    assert stored_h["reminder_minutes"] == 0
    assert stored_h["dav_valarms"] == []
    assert "BEGIN:VALARM" not in hearing_model.hearing_to_vevent(stored_h)

    # Phone deletes all VALARMs on VTODO -> dav_valarms cleared to []
    vtodo_no_alarm = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//FR\r\n"
        "BEGIN:VTODO\r\n"
        f"UID:{t['vtodo_uid']}\r\n"
        "SUMMARY:Tâche avec alarme\r\n"
        "STATUS:NEEDS-ACTION\r\n"
        "END:VTODO\r\nEND:VCALENDAR\r\n"
    )
    put_t = client.put(
        f"/dav/dossier-d1/{t['id']}.ics",
        data=vtodo_no_alarm.encode("utf-8"),
        content_type="text/calendar",
        headers=auth_hdr,
    )
    assert put_t.status_code == 204
    stored_t = task_model.get_task(t["id"])
    assert stored_t["dav_valarms"] == []
    assert "BEGIN:VALARM" not in task_model.task_to_vtodo(stored_t)

    # Offset / naive updated_at must always coerce to canonical UTC LAST-MODIFIED:...Z
    offset_tz = timezone(timedelta(hours=-4))
    offset_dt = datetime(2026, 6, 1, 10, 0, 0, tzinfo=offset_tz)
    h_ics = hearing_model.hearing_to_vevent({**stored_h, "updated_at": offset_dt})
    t_ics = task_model.task_to_vtodo({**stored_t, "updated_at": offset_dt})
    n_doc = {
        "id": "n-lm",
        "title": "Note",
        "content": "Texte",
        "vjournal_uid": "u-n-lm",
        "created_at": offset_dt,
        "updated_at": offset_dt,
        "classification": "CONFIDENTIAL",
        "dossier_id": "d1",
    }
    n_ics = note_model.note_to_vjournal(n_doc)
    assert "LAST-MODIFIED:20260601T140000Z" in h_ics
    assert "LAST-MODIFIED:20260601T140000Z" in t_ics
    assert "LAST-MODIFIED:20260601T140000Z" in n_ics


def test_carddav_grouped_lang_gender_bday_mmdd_and_slot_deletion(fake_db, monkeypatch):
    import bcrypt as _bcrypt

    p, errs = partie_model.create_partie(
        {
            "type": "individual",
            "last_name": "Tremblay",
            "first_name": "Claire",
            "birth_date": "1990-03-15",
            "email": "home@exemple.ca",
            "email_work": "bureau@exemple.ca",
            "phone_cell": "514-555-0100",
            "phone_work": "514-555-0200",
        }
    )
    assert not errs

    # Grouped item1.LANG and RFC 6350 GENDER:F;elle parse cleanly
    parsed = partie_model.vcard_to_partie(
        "BEGIN:VCARD\r\nVERSION:4.0\r\n"
        f"UID:{p['vcard_uid']}\r\n"
        "FN:Claire Tremblay\r\nN:Tremblay;Claire;;;\r\n"
        "item1.LANG;PREF=1:fr\r\n"
        "GENDER:F;elle\r\n"
        "EMAIL;TYPE=HOME:home@exemple.ca\r\n"
        "TEL;TYPE=CELL:+15145550100\r\n"
        "END:VCARD\r\n"
    )
    assert parsed["language"] == "fr"
    assert parsed["gender"] == "F"
    assert parsed["email_work"] == ""
    assert parsed["phone_work"] == ""

    # Invalid birth_date_mmdd is refused even when birth_date is also supplied
    bad_p, bad_errs = partie_model.update_partie(
        p["id"], {"birth_date": "1990-03-15", "birth_date_mmdd": "--9999"}
    )
    assert bad_p is None and bad_errs

    # Via CardDAV PUT: removing email_work/phone_work and changing BDAY to --0420 persists
    app = _make_app(fake_db, monkeypatch)
    monkeypatch.setattr(Config, "DAV_USERNAME", "test@example.com")
    monkeypatch.setattr(
        Config,
        "DAV_PASSWORD_HASH",
        _bcrypt.hashpw(b"secret", _bcrypt.gensalt(4)).decode("ascii"),
    )
    client = app.test_client()
    creds = base64.b64encode(b"test@example.com:secret").decode("ascii")
    put_vcf = (
        "BEGIN:VCARD\r\nVERSION:4.0\r\n"
        f"UID:{p['vcard_uid']}\r\n"
        "FN:Claire Tremblay\r\nN:Tremblay;Claire;;;\r\n"
        "BDAY:--0420\r\n"
        "item1.LANG:fr\r\n"
        "GENDER:F;elle\r\n"
        "EMAIL;TYPE=HOME:home@exemple.ca\r\n"
        "TEL;TYPE=CELL:+15145550100\r\n"
        "END:VCARD\r\n"
    )
    res = client.put(
        f"/dav/addressbook/{p['id']}.vcf",
        data=put_vcf.encode("utf-8"),
        content_type="text/vcard",
        headers={"Authorization": f"Basic {creds}"},
    )
    assert res.status_code == 204
    stored = partie_model.get_partie(p["id"])
    assert stored["birth_date"] is None
    assert stored["birth_date_mmdd"] == "--0420"
    assert stored["email_work"] == ""
    assert stored["phone_work"] == ""
    assert stored["language"] == "fr"
    assert stored["gender"] == "F"


def test_miroir_onedrive_fail_closed_hash_409_session_rename_and_logging(onedrive_env, monkeypatch):
    fake_db, bucket, od = onedrive_env

    # 1) _item_matches_bytes MUST refuse folders and files lacking quickXorHash
    assert miroir_onedrive._item_matches_bytes(
        {"id": "f1", "size": 100, "folder": {"childCount": 0}},
        expected_size=100,
        expected_qxh="abc=",
    ) is False
    assert miroir_onedrive._item_matches_bytes(
        {"id": "x1", "size": 100, "file": {}},
        expected_size=100,
        expected_qxh="abc=",
    ) is False

    # 2) Upload session (>4 MiB) 409 finalization race recovers cleanly via dedup
    monkeypatch.setattr(graph_courrier, "ONEDRIVE_SIMPLE_MAX_BYTES", 100)
    monkeypatch.setattr(graph_courrier, "ONEDRIVE_UPLOAD_CHUNK_BYTES", 128)
    big_bytes = b"%PDF-1.4\n" + (b"B" * 300)
    _seed_dossier(fake_db, dossier_id="d1", file_number="2026-001")
    d_big_race, derrs_br = doc_model.upload_document(
        "d1",
        "2026-001",
        io.BytesIO(big_bytes),
        "big_race.pdf",
        len(big_bytes),
        {"display_name": "Gros fichier", "category": "autre"},
        UID,
    )
    assert not derrs_br
    od.put_file_direct("Dossiers/2026-001/Gros fichier.pdf", big_bytes)
    orig_upload_chunk = graph_courrier.courrier_televerser_tranche_onedrive
    monkeypatch.setattr(
        graph_courrier,
        "courrier_televerser_tranche_onedrive",
        mock.Mock(
            side_effect=graph_courrier.CourrierGraphError(
                "conflict", status_code=409, reason="conflict"
            )
        ),
    )
    session = miroir_onedrive._MirrorSession()
    orig_get_child = session._get_child_by_path
    child_calls = {"n": 0}

    def _race_get_child(parent_id, name):
        child_calls["n"] += 1
        # Simulate pre-upload check missing the concurrent file, then 409 check finding it
        if child_calls["n"] == 1:
            return None
        return orig_get_child(parent_id, name)

    monkeypatch.setattr(session, "_get_child_by_path", _race_get_child)
    with mock.patch.object(
        graph_courrier,
        "courrier_ouvrir_session_onedrive",
        return_value="https://upload.example.invalid/session/1",
    ):
        outcome = miroir_onedrive._sync_document_internal(session, d_big_race["id"])
    assert outcome == "deduplicated"
    monkeypatch.setattr(graph_courrier, "courrier_televerser_tranche_onedrive", orig_upload_chunk)

    # 3) Disambiguated file stays unchanged on repeated syncs when blocker still exists
    col_bytes = b"%PDF-1.4\nnew-content"
    other_bytes = b"%PDF-1.4\nblocker-content"
    od.put_file_direct("Dossiers/2026-001/Piece.pdf", other_bytes)
    d_col, derrs_col = doc_model.upload_document(
        "d1",
        "2026-001",
        io.BytesIO(col_bytes),
        "piece.pdf",
        len(col_bytes),
        {"display_name": "Piece", "category": "autre"},
        UID,
    )
    assert not derrs_col
    assert miroir_onedrive.handle_event({"action": "sync_document", "document_id": d_col["id"]})["outcome"] == "uploaded"
    od.ops.clear()
    assert miroir_onedrive.handle_event({"action": "sync_document", "document_id": d_col["id"]})["outcome"] == "unchanged"
    assert not [op for op in od.ops if op[0] == "patch"]

    # 4) Dossier file_number rename updates OneDrive folder & delete_dossier removes empty tree
    c_obj, c_errs = partie_model.create_partie(
        {"type": "individual", "last_name": "Client", "first_name": "Jean", "contact_role": "client"}
    )
    assert not c_errs
    d2_obj, errs_d2 = dossier_model.create_dossier(
        {
            "file_number": "2026-009",
            "title": "Renommage Test",
            "clients": [{"id": c_obj["id"], "name": "Jean Client"}],
        }
    )
    assert not errs_d2
    d2_id = d2_obj["id"]
    assert miroir_onedrive.handle_event({"action": "sync_dossier_tree", "dossier_id": d2_id})["action"] == "sync_dossier_tree"
    _updated_d2, u_errs = dossier_model.update_dossier(
        d2_id, {"file_number": "2026-099"}, expected_etag=d2_obj["etag"]
    )
    assert not u_errs
    miroir_onedrive.handle_event({"action": "sync_dossier_tree", "dossier_id": d2_id})
    book = fake_db.collection("onedrive_miroir").document(f"dossier:{d2_id}").get().to_dict()
    assert book["name"] == "2026-099"
    assert od.items[book["onedrive_item_id"]]["name"] == "2026-099"

    ok_del, msg_del, _ = dossier_model.delete_dossier(d2_id)
    assert ok_del, msg_del
    res_del = miroir_onedrive.handle_event({"action": "delete_dossier", "dossier_id": d2_id, "folder_ids": []})
    assert res_del["outcome"] == "deleted"
    assert book["onedrive_item_id"] not in od.items

    # 5) Admin receipt deletion + draft reconciliation orphan cleanup + OBSERVABILITY.md fields
    acc_ref = fake_db.collection("admin_accounts").document("acc-a1")
    acc_ref.set({"id": "acc-a1", "name": "Compte Courant", "account_type": "checking", "ledger_balance": 0})
    r_path = f"users/{UID}/administration/tx-del/{uuid.uuid4()}/recu.pdf"
    bucket.put(r_path, b"%PDF-1.4\nrec", content_type="application/pdf")
    fake_db.collection("admin_transactions").document("tx-del").set(
        {
            "id": "tx-del",
            "account_id": "acc-a1",
            "sequence": 42,
            "direction": "débit",
            "amount": 5000,
            "reconciled": False,
            "reconciliation_id": None,
            "receipt_storage_path": r_path,
            "receipt_filename": "recu.pdf",
            "receipt_file_type": "application/pdf",
        }
    )
    assert miroir_onedrive.handle_event({"action": "sync_receipt", "register": "admin", "tx_id": "tx-del"})["outcome"] == "uploaded"
    assert fake_db.collection("onedrive_miroir").document("receipt:admin:tx-del").get().exists
    deleted_tx, del_errs = admin_model.delete_transaction("tx-del")
    assert deleted_tx and not del_errs
    miroir_onedrive.handle_event({"action": "delete_receipt", "register": "admin", "tx_id": "tx-del"})
    assert not fake_db.collection("onedrive_miroir").document("receipt:admin:tx-del").get().exists

    captured_logs: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        miroir_onedrive,
        "log_onedrive_mirror_event",
        lambda ev, outcome="success", *, reason=None, **kw: captured_logs.append((ev, {"outcome": outcome, "reason": reason, **kw})),
    )
    fake_db.collection("admin_reconciliations").document("rec-draft-only").set(
        {"id": "rec-draft-only", "account_id": "acc-a1", "status": "brouillon", "statement_storage_path": f"users/{UID}/administration/conciliations/rec-draft-only/stmt.pdf"}
    )
    miroir_onedrive.handle_event({"action": "sync_reconciliation", "register": "admin", "reconciliation_id": "rec-draft-only"})
    assert ("miroir_onedrive_statement_sync", {"outcome": "success", "reason": "not_completed", "register": "admin", "reconciliation_id": "rec-draft-only", "action": "skipped_not_completed"}) in captured_logs

    sweep = miroir_onedrive.executer_rattrapage_cron()
    assert all(k in sweep for k in ("dossiers", "docs", "templates", "receipts", "statements", "errors"))
    sweep_logs = [kw for ev, kw in captured_logs if ev == "miroir_onedrive_sweep"]
    assert sweep_logs and all(k in sweep_logs[-1] for k in ("dossiers", "docs", "templates", "receipts", "statements", "errors"))

