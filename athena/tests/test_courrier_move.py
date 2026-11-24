"""Tests for Courrier Phase 4: Outlook classification, categories & move_courriels."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from config import Config
    import mcp.handlers as handlers
    import mcp.tools as tools
    from mcp.write_support import ToolArgumentError
    from models import courrier as courrier_model
    from services import courrier as courrier_service
    from utils import graph_courrier

from tests._fake_firestore import install  # noqa: E402


def _fake_modules() -> list:
    return [
        m
        for n, m in sorted(sys.modules.items())
        if (n.startswith("models.") or n in ("dav.sync", "mcp.write_support"))
        and getattr(m, "db", None) is not None
    ]


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, *, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = dict(headers or {})
        self.closed = False

    def json(self):
        if self._payload is None:
            raise ValueError("empty json")
        return self._payload

    def close(self):
        self.closed = True


class _FakeGraphMail:
    """In-memory Outlook mailFolders + messages + masterCategories harness."""

    def __init__(self, *, master_categories_403: bool = False) -> None:
        self.inbox_id = "fld-inbox"
        self.sent_id = "fld-sent"
        self.deleted_id = "fld-deleted"
        self.archive_id = "fld-archive"
        self.dossiers_id = "fld-dossiers"
        self.statut_ids = {
            "Actifs": "fld-actifs",
            "Suspendu": "fld-suspendu",
            "Services-conseils": "fld-sc",
        }
        # {folder_id: {"id", "displayName", "parent_id"}}
        self.folders: dict[str, dict] = {
            self.inbox_id: {"id": self.inbox_id, "displayName": "Inbox", "parent_id": None},
            self.sent_id: {"id": self.sent_id, "displayName": "Sent Items", "parent_id": None},
            self.deleted_id: {"id": self.deleted_id, "displayName": "Deleted Items", "parent_id": None},
            self.archive_id: {"id": self.archive_id, "displayName": "Archive", "parent_id": None},
            self.dossiers_id: {"id": self.dossiers_id, "displayName": "Dossiers", "parent_id": self.archive_id},
            "fld-actifs": {"id": "fld-actifs", "displayName": "Actifs", "parent_id": self.dossiers_id},
            "fld-suspendu": {"id": "fld-suspendu", "displayName": "Suspendu", "parent_id": self.dossiers_id},
            "fld-sc": {"id": "fld-sc", "displayName": "Services-conseils", "parent_id": self.dossiers_id},
        }
        self.messages: dict[str, dict] = {}
        self.master_categories: list[dict[str, str]] = []
        self.master_categories_403 = master_categories_403
        self.write_calls: list[tuple[str, str, dict]] = []
        self.mutate_id_on_move = False
        self._seq = 1

    def add_dossier_folder(self, statut: str, nom: str, *, fid: str | None = None) -> str:
        parent_id = self.statut_ids[statut]
        node_id = fid or f"fld-dos-{self._seq}"
        self._seq += 1
        self.folders[node_id] = {
            "id": node_id,
            "displayName": nom,
            "parent_id": parent_id,
        }
        return node_id

    def add_message(
        self,
        *,
        msg_id: str,
        internet_message_id: str,
        parent_folder_id: str | None = None,
        categories: list[str] | None = None,
        is_read: bool = False,
        subject: str = "Objet confidentiel client",
    ) -> str:
        self.messages[msg_id] = {
            "id": msg_id,
            "internetMessageId": internet_message_id,
            "parentFolderId": parent_folder_id or self.inbox_id,
            "categories": list(categories or []),
            "isRead": is_read,
            "subject": subject,
        }
        return courrier_model.compute_cle(internet_message_id)


@pytest.fixture
def env(monkeypatch):
    graph_courrier._reset_token_cache_for_tests()
    fake = install(monkeypatch, *_fake_modules())
    monkeypatch.setattr(Config, "COURRIER_ACTIF", True)
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_ID", "courrier-cid")
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_SECRET", "courrier-secret")
    monkeypatch.setattr(Config, "GRAPH_TENANT_ID", "tenant-id")
    monkeypatch.setattr(Config, "COURRIER_UPN", "avocat@cabinet.example")
    monkeypatch.setattr(Config, "COURRIER_ONEDRIVE_ROOT", "Dossiers")

    gm = _FakeGraphMail()
    encoded_upn = quote(Config.COURRIER_UPN, safe="")
    prefix = f"https://graph.microsoft.com/v1.0/users/{encoded_upn}"

    def _children_of(parent_id: str) -> list[dict]:
        return [
            {"id": f["id"], "displayName": f["displayName"]}
            for f in gm.folders.values()
            if f["parent_id"] == parent_id
        ]

    def fake_post(url, *, headers=None, params=None, data=None, json=None, timeout=30):
        del headers, params, timeout
        if url.startswith("https://login.microsoftonline.com/"):
            return _FakeResponse(
                200, {"access_token": "tok", "expires_in": 3600}
            )
        assert url.startswith(prefix), url
        rel = url[len(prefix) :]
        if rel == "/outlook/masterCategories":
            gm.write_calls.append(("POST", rel, dict(json or {})))
            if gm.master_categories_403:
                return _FakeResponse(403)
            name = str((json or {}).get("displayName") or "").strip()
            for existing in gm.master_categories:
                if existing["displayName"].strip().casefold() == name.casefold():
                    return _FakeResponse(409)
            created = {"id": f"cat-{len(gm.master_categories)+1}", **(json or {})}
            gm.master_categories.append(created)
            return _FakeResponse(201, created)

        if rel.startswith("/mailFolders/") and rel.endswith("/childFolders"):
            parent_id = rel.split("/")[2]
            gm.write_calls.append(("POST", rel, dict(json or {})))
            nom = str((json or {}).get("displayName") or "").strip()
            for child in gm.folders.values():
                if (
                    child["parent_id"] == parent_id
                    and child["displayName"].strip().casefold() == nom.casefold()
                ):
                    return _FakeResponse(409)
            fid = gm.add_dossier_folder(
                next(k for k, v in gm.statut_ids.items() if v == parent_id),
                nom,
            )
            return _FakeResponse(201, {"id": fid, "displayName": nom})

        if rel.startswith("/messages/") and rel.endswith("/move"):
            msg_id = rel.split("/")[2]
            gm.write_calls.append(("POST", rel, dict(json or {})))
            msg = gm.messages.get(msg_id)
            if msg is None:
                return _FakeResponse(404)
            dest = str((json or {}).get("destinationId") or "")
            msg["parentFolderId"] = dest
            returned_id = f"{msg_id}-mutated" if gm.mutate_id_on_move else msg_id
            return _FakeResponse(201, {**msg, "id": returned_id})

        raise AssertionError(f"Unexpected POST {url}")

    def fake_get(url, *, headers=None, params=None, timeout=30, stream=False):
        del params, timeout, stream
        assert headers and 'IdType="' in headers.get("Prefer", "") or "/outlook/masterCategories" in url
        assert url.startswith(prefix), url
        rel = url[len(prefix) :]
        if rel == "/mailFolders/Inbox":
            return _FakeResponse(
                200, {"id": gm.inbox_id, "displayName": "Boîte de réception"}
            )
        if rel == "/mailFolders/SentItems":
            return _FakeResponse(
                200, {"id": gm.sent_id, "displayName": "Éléments envoyés"}
            )
        if rel == "/mailFolders":
            return _FakeResponse(
                200,
                {
                    "value": [
                        {"id": f["id"], "displayName": f["displayName"]}
                        for f in gm.folders.values()
                        if f["parent_id"] is None
                    ]
                },
            )
        if rel.startswith("/mailFolders/") and rel.endswith("/childFolders"):
            parent_id = rel.split("/")[2]
            return _FakeResponse(200, {"value": _children_of(parent_id)})
        if rel == "/outlook/masterCategories":
            if gm.master_categories_403:
                return _FakeResponse(403)
            return _FakeResponse(200, {"value": list(gm.master_categories)})
        if rel.startswith("/messages/"):
            msg_id = rel.split("/")[2]
            msg = gm.messages.get(msg_id)
            if msg is None:
                return _FakeResponse(404)
            return _FakeResponse(200, dict(msg))
        raise AssertionError(f"Unexpected GET {url}")

    def fake_patch(url, *, headers=None, params=None, data=None, json=None, timeout=30):
        del params, data, timeout
        assert headers and 'IdType="ImmutableId"' in headers.get("Prefer", "")
        assert url.startswith(prefix), url
        rel = url[len(prefix) :]
        if rel.startswith("/messages/"):
            msg_id = rel.split("/")[2]
            body = dict(json or {})
            assert set(body.keys()) == {"categories"}
            gm.write_calls.append(("PATCH", rel, body))
            msg = gm.messages.get(msg_id)
            if msg is None:
                return _FakeResponse(404)
            msg["categories"] = list(body["categories"])
            return _FakeResponse(200, dict(msg))
        raise AssertionError(f"Unexpected PATCH {url}")

    monkeypatch.setattr(graph_courrier._session, "post", fake_post)
    monkeypatch.setattr(graph_courrier._session, "get", fake_get)
    monkeypatch.setattr(graph_courrier._session, "patch", fake_patch)

    yield fake, gm
    graph_courrier._reset_token_cache_for_tests()


def _seed_enregistre(fake, cle: str, msg_id: str, numero: str = "2026-001") -> None:
    fake.seed(
        f"courrier_messages/{cle}",
        {
            "cle": cle,
            "graph_id": msg_id,
            "etat": courrier_model.ETAT_ENREGISTRE,
            "dossier_id": "a0000000-0000-4000-8000-000000000001",
            "dossier_numero": numero,
            "fichiers": [],
            "legacy_ref": "",
            "time_entry_id": "",
            "outlook_folder_id": "",
            "saved_at": datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
            "moved_at": None,
            "expire_at": None,
        },
    )


# ── Low-level Graph helper tests ────────────────────────────────────────


def test_graph_move_and_category_helpers(env):
    _fake, gm = env
    encoded = quote(Config.COURRIER_UPN, safe="")
    cle = gm.add_message(
        msg_id="imm-1",
        internet_message_id="<h1@cabinet.example>",
        is_read=False,
    )
    assert cle

    assert graph_courrier.courrier_lire_courriel("missing", encoded_upn=encoded) is None
    loaded = graph_courrier.courrier_lire_courriel("imm-1", encoded_upn=encoded)
    assert loaded is not None and loaded["isRead"] is False

    assert graph_courrier.courrier_assurer_categories_maitres(encoded_upn=encoded) is True
    names = {c["displayName"] for c in gm.master_categories}
    assert names == {graph_courrier.CATEGORIE_CLASSE, graph_courrier.CATEGORIE_A_CLASSER}

    # Calling again is a no-op (no duplicate 409/POSTs needed when already present)
    prior_writes = len(gm.write_calls)
    assert graph_courrier.courrier_assurer_categories_maitres(encoded_upn=encoded) is True
    assert len(gm.write_calls) == prior_writes

    folder, created = graph_courrier.courrier_creer_dossier_outlook(
        gm.statut_ids["Actifs"], "2026-001 - Client A", encoded_upn=encoded
    )
    assert created is True
    folder_again, created_again = graph_courrier.courrier_creer_dossier_outlook(
        gm.statut_ids["Actifs"], "2026-001 - Client A", encoded_upn=encoded
    )
    assert created_again is False and folder_again["id"] == folder["id"]


def test_graph_master_categories_403_degrades_gracefully(env):
    _fake, gm = env
    gm.master_categories_403 = True
    encoded = quote(Config.COURRIER_UPN, safe="")
    assert graph_courrier.courrier_assurer_categories_maitres(encoded_upn=encoded) is False


def test_graph_lire_courriel_raises_on_500_without_pii(env, monkeypatch):
    encoded = quote(Config.COURRIER_UPN, safe="")
    monkeypatch.setattr(
        graph_courrier._session,
        "get",
        lambda url, **kw: _FakeResponse(500),
    )
    with pytest.raises(graph_courrier.CourrierGraphError) as exc_info:
        graph_courrier.courrier_lire_courriel("imm-1", encoded_upn=encoded)
    assert exc_info.value.status_code == 500
    assert "HTTP 500" in str(exc_info.value)


# ── Switch, registry & integrity refusals ───────────────────────────────


def test_move_courriels_refused_when_switch_off(env, monkeypatch):
    fake, gm = env
    cle = gm.add_message(msg_id="imm-off", internet_message_id="<off@cabinet.example>")
    _seed_enregistre(fake, cle, "imm-off")
    monkeypatch.setattr(Config, "COURRIER_ACTIF", False)

    with pytest.raises(PermissionError):
        courrier_service.deplacer_courriels(
            [{"cle": cle, "id": "imm-off", "action": "laisser"}]
        )
    with pytest.raises(ToolArgumentError, match="COURRIER_ACTIF=false"):
        handlers.move_courriels({
            "messages": [{"cle": cle, "id": "imm-off", "action": "laisser"}],
            "idempotency_key": "k-off",
        })
    assert gm.write_calls == []


@pytest.mark.parametrize("seed_mode", ["absent", "inventorie", "expired"])
def test_classer_refused_without_enregistre_in_registry(env, seed_mode):
    fake, gm = env
    gm.add_dossier_folder("Actifs", "2026-001 - Client Test")
    cle = gm.add_message(msg_id="imm-unreg", internet_message_id="<unreg@cabinet.example>")

    if seed_mode == "inventorie":
        courrier_model.record_inventorie(cle, "imm-unreg")
    elif seed_mode == "expired":
        fake.seed(
            f"courrier_messages/{cle}",
            {
                "cle": cle,
                "graph_id": "imm-unreg",
                "etat": courrier_model.ETAT_INVENTORIE,
                "expire_at": datetime.now(timezone.utc) - timedelta(days=1),
            },
        )

    res = courrier_service.deplacer_courriels(
        [{"cle": cle, "id": "imm-unreg", "action": "classer", "dossier": "2026-001"}]
    )
    assert res["classes"] == 0 and res["echecs"] == 1
    assert "Enregistrement non constaté" in res["messages"][0]["erreur"]
    assert gm.write_calls == []


def test_classer_refused_when_dossier_numero_mismatches_registry(env):
    fake, gm = env
    gm.add_dossier_folder("Actifs", "2026-002 - Autre Dossier")
    cle = gm.add_message(msg_id="imm-dos-mis", internet_message_id="<mis@cabinet.example>")
    _seed_enregistre(fake, cle, "imm-dos-mis", numero="2026-001")

    res = courrier_service.deplacer_courriels(
        [{"cle": cle, "id": "imm-dos-mis", "action": "classer", "dossier": "2026-002"}]
    )
    assert res["echecs"] == 1 and res["classes"] == 0
    assert "déplacement refusé" in res["messages"][0]["erreur"]
    assert gm.write_calls == []


def test_cle_mismatch_refused_without_writes(env):
    fake, gm = env
    gm.add_dossier_folder("Actifs", "2026-001 - Dossier A")
    gm.add_message(msg_id="imm-real", internet_message_id="<real@cabinet.example>")
    wrong_cle = courrier_model.compute_cle("<other@cabinet.example>")
    _seed_enregistre(fake, wrong_cle, "imm-real", numero="2026-001")

    res = courrier_service.deplacer_courriels(
        [{"cle": wrong_cle, "id": "imm-real", "action": "classer", "dossier": "2026-001"}]
    )
    assert res["echecs"] == 1 and res["classes"] == 0
    assert "correspond pas" in res["messages"][0]["erreur"]
    assert gm.write_calls == []


def test_404_message_returns_introuvable_without_incident(env):
    fake, gm = env
    gm.add_dossier_folder("Actifs", "2026-001 - Dossier A")
    cle = courrier_model.compute_cle("<deleted-manually@cabinet.example>")
    _seed_enregistre(fake, cle, "imm-missing")

    res = courrier_service.deplacer_courriels(
        [{"cle": cle, "id": "imm-missing", "action": "classer", "dossier": "2026-001"}]
    )
    assert res["introuvables"] == 1
    assert res["echecs"] == 0 and res["classes"] == 0
    item = res["messages"][0]
    assert item["statut"] == "introuvable" and item["erreur"] is None
    assert gm.write_calls == []
    assert fake.peek(f"courrier_messages/{cle}")["etat"] == courrier_model.ETAT_ENREGISTRE


# ── Classer & créer dossier Outlook & laisser ───────────────────────────


def test_classer_with_existing_outlook_id_preserves_categories_and_unread(env):
    fake, gm = env
    target_fid = gm.add_dossier_folder("Suspendu", "2026-001 - Client Suspendu", fid="fld-2026-001")
    cle = gm.add_message(
        msg_id="imm-stay-unread",
        internet_message_id="<unread@cabinet.example>",
        categories=["Important", graph_courrier.CATEGORIE_A_CLASSER],
        is_read=False,
    )
    _seed_enregistre(fake, cle, "imm-stay-unread", numero="2026-001")

    res = handlers.move_courriels({
        "messages": [
            {
                "cle": cle,
                "id": "imm-stay-unread",
                "action": "classer",
                "dossier": "2026-001",
                "outlook_id": target_fid,
            }
        ],
        "idempotency_key": "move-ok-1",
    })
    assert res["classes"] == 1 and res["echecs"] == 0
    row = res["messages"][0]
    assert row["statut"] == "ok"
    assert row["statut_outlook_cible"] == "Suspendu"
    assert row["outlook_folder_id"] == target_fid
    assert row["dossier_outlook_cree"] is False
    assert row["id_immuable_verifie"] is True

    stored_msg = gm.messages["imm-stay-unread"]
    assert stored_msg["parentFolderId"] == target_fid
    assert stored_msg["isRead"] is False
    assert stored_msg["categories"] == ["Important", graph_courrier.CATEGORIE_CLASSE]

    reg = fake.peek(f"courrier_messages/{cle}")
    assert reg["etat"] == courrier_model.ETAT_CLASSE
    assert reg["outlook_folder_id"] == target_fid
    assert reg["expire_at"] is None
    assert reg["moved_at"] is not None
    assert reg["updated_via"] == "mcp"


def test_classer_creates_new_outlook_folder_once_and_reuses_in_batch(env):
    fake, gm = env
    cle1 = gm.add_message(msg_id="imm-new-1", internet_message_id="<new1@cabinet.example>")
    cle2 = gm.add_message(msg_id="imm-new-2", internet_message_id="<new2@cabinet.example>")
    _seed_enregistre(fake, cle1, "imm-new-1", numero="2026-015")
    _seed_enregistre(fake, cle2, "imm-new-2", numero="2026-015")

    res = courrier_service.deplacer_courriels([
        {
            "cle": cle1,
            "id": "imm-new-1",
            "action": "classer",
            "dossier": "2026-015",
            "outlook_creer": {"statut": "Services-conseils", "nom": "2026-015 - Avis Client"},
        },
        {
            "cle": cle2,
            "id": "imm-new-2",
            "action": "classer",
            "dossier": "2026-015",
            "outlook_creer": {"statut": "Services-conseils", "nom": "2026-015 - Avis Client"},
        },
    ])
    assert res["classes"] == 2 and res["echecs"] == 0
    assert res["messages"][0]["dossier_outlook_cree"] is True
    assert res["messages"][1]["dossier_outlook_cree"] is False
    created_fid = res["messages"][0]["outlook_folder_id"]
    assert res["messages"][1]["outlook_folder_id"] == created_fid
    assert gm.folders[created_fid]["parent_id"] == gm.statut_ids["Services-conseils"]


def test_classer_rejects_folder_outside_archive_or_wrong_dossier(env):
    fake, gm = env
    other_fid = gm.add_dossier_folder("Actifs", "2026-999 - Autre", fid="fld-999")
    cle = gm.add_message(msg_id="imm-bound", internet_message_id="<bound@cabinet.example>")
    _seed_enregistre(fake, cle, "imm-bound", numero="2026-001")

    bad_root = courrier_service.deplacer_courriels([
        {
            "cle": cle,
            "id": "imm-bound",
            "action": "classer",
            "dossier": "2026-001",
            "outlook_id": gm.deleted_id,
        }
    ])
    assert bad_root["echecs"] == 1
    assert "Archive > Dossiers" in bad_root["messages"][0]["erreur"]

    bad_prefix = courrier_service.deplacer_courriels([
        {
            "cle": cle,
            "id": "imm-bound",
            "action": "classer",
            "dossier": "2026-001",
            "outlook_id": other_fid,
        }
    ])
    assert bad_prefix["echecs"] == 1
    assert "2026-001" in bad_prefix["messages"][0]["erreur"]


def test_classer_refuses_if_graph_changes_id_after_move(env):
    fake, gm = env
    gm.mutate_id_on_move = True
    gm.add_dossier_folder("Actifs", "2026-001 - Dossier")
    cle = gm.add_message(msg_id="imm-mut", internet_message_id="<mut@cabinet.example>")
    _seed_enregistre(fake, cle, "imm-mut", numero="2026-001")

    res = courrier_service.deplacer_courriels(
        [{"cle": cle, "id": "imm-mut", "action": "classer", "dossier": "2026-001"}]
    )
    assert res["echecs"] == 1 and res["classes"] == 0
    assert "ImmutableId" in res["messages"][0]["erreur"]
    assert fake.peek(f"courrier_messages/{cle}")["etat"] == courrier_model.ETAT_ENREGISTRE


def test_laisser_tags_and_records_inventorie_without_downgrading_enregistre(env):
    fake, gm = env
    cle_fresh = gm.add_message(
        msg_id="imm-l1",
        internet_message_id="<l1@cabinet.example>",
        categories=[graph_courrier.CATEGORIE_CLASSE, "Suivi"],
        is_read=False,
    )
    cle_saved = gm.add_message(
        msg_id="imm-l2",
        internet_message_id="<l2@cabinet.example>",
        is_read=True,
    )
    _seed_enregistre(fake, cle_saved, "imm-l2")

    res = handlers.move_courriels({
        "messages": [
            {"cle": cle_fresh, "id": "imm-l1", "action": "laisser"},
            {"cle": cle_saved, "id": "imm-l2", "action": "laisser"},
        ],
        "idempotency_key": "move-laisser-1",
    })
    assert res["laisses"] == 2 and res["echecs"] == 0
    assert gm.messages["imm-l1"]["categories"] == ["Suivi", graph_courrier.CATEGORIE_A_CLASSER]
    assert gm.messages["imm-l1"]["isRead"] is False
    assert gm.messages["imm-l2"]["categories"] == [graph_courrier.CATEGORIE_A_CLASSER]

    reg_fresh = fake.peek(f"courrier_messages/{cle_fresh}")
    assert reg_fresh["etat"] == courrier_model.ETAT_INVENTORIE
    assert reg_fresh["expire_at"] is not None
    # Saved record keeps enregistre (never downgraded)
    reg_saved = fake.peek(f"courrier_messages/{cle_saved}")
    assert reg_saved["etat"] == courrier_model.ETAT_ENREGISTRE
    assert reg_saved["expire_at"] is None


def test_laisser_refused_if_already_classe_and_rejects_dossier_args(env):
    fake, gm = env
    cle_cls = gm.add_message(msg_id="imm-cls", internet_message_id="<cls@cabinet.example>")
    _seed_enregistre(fake, cle_cls, "imm-cls")
    courrier_model.record_classe(cle_cls, "imm-cls", outlook_folder_id="fld-2026-001")

    res = courrier_service.deplacer_courriels([
        {"cle": cle_cls, "id": "imm-cls", "action": "laisser"},
        {"cle": cle_cls, "id": "imm-cls", "action": "laisser", "dossier": "2026-001"},
    ])
    assert res["echecs"] == 2
    assert "déjà classé" in res["messages"][0]["erreur"]
    assert "n'accepte ni `dossier`" in res["messages"][1]["erreur"]


def test_master_categories_403_adds_warning_and_proceeds(env):
    fake, gm = env
    gm.master_categories_403 = True
    gm.add_dossier_folder("Actifs", "2026-001 - Dossier")
    cle = gm.add_message(msg_id="imm-403", internet_message_id="<cat403@cabinet.example>")
    _seed_enregistre(fake, cle, "imm-403", numero="2026-001")

    res = courrier_service.deplacer_courriels(
        [{"cle": cle, "id": "imm-403", "action": "classer", "dossier": "2026-001"}]
    )
    assert res["classes"] == 1 and res["echecs"] == 0
    assert len(res["avertissements"]) == 1
    assert "MailboxSettings.ReadWrite" in res["avertissements"][0]
    assert graph_courrier.CATEGORIE_CLASSE in gm.messages["imm-403"]["categories"]


def test_move_courriels_supports_sent_items_and_rejects_essai(env):
    assert "essai" not in tools.TOOLS["move_courriels"]["input_schema"]["properties"]
    errs = tools.validate_args(
        tools.TOOLS["move_courriels"]["input_schema"],
        {
            "messages": [{"cle": "0" * 16, "id": "m1", "action": "laisser"}],
            "essai": True,
            "idempotency_key": "move-no-dry",
        },
    )
    assert errs and any("essai" in e for e in errs)

    fake, gm = env
    target_fid = gm.add_dossier_folder("Actifs", "2026-001 - Client Actif", fid="fld-2026-001")
    cle_move = gm.add_message(
        msg_id="imm-sent-cls",
        internet_message_id="<sent-cls@cabinet.example>",
        parent_folder_id=gm.sent_id,
    )
    cle_hold = gm.add_message(
        msg_id="imm-sent-hold",
        internet_message_id="<sent-hold@cabinet.example>",
        parent_folder_id=gm.sent_id,
    )
    _seed_enregistre(fake, cle_move, "imm-sent-cls", numero="2026-001")

    res = handlers.move_courriels({
        "messages": [
            {"cle": cle_move, "id": "imm-sent-cls", "action": "classer", "dossier": "2026-001"},
            {"cle": cle_hold, "id": "imm-sent-hold", "action": "laisser"},
        ],
        "idempotency_key": "move-sent-1",
    })
    assert res["classes"] == 1 and res["laisses"] == 1 and res["echecs"] == 0
    assert gm.messages["imm-sent-cls"]["parentFolderId"] == target_fid
    assert gm.messages["imm-sent-cls"]["categories"] == [graph_courrier.CATEGORIE_CLASSE]
    assert gm.messages["imm-sent-hold"]["parentFolderId"] == gm.sent_id
    assert gm.messages["imm-sent-hold"]["categories"] == [graph_courrier.CATEGORIE_A_CLASSER]


def test_budget_deferral_and_no_replay_on_partial_and_zero_pii(env):
    fake, gm = env
    gm.add_dossier_folder("Actifs", "2026-001 - Secret Client")
    cle1 = gm.add_message(
        msg_id="imm-b1",
        internet_message_id="<b1@cabinet.example>",
        subject="CONFIDENTIEL OBJET CLIENT",
    )
    cle2 = gm.add_message(
        msg_id="imm-b2",
        internet_message_id="<b2@cabinet.example>",
        subject="AUTRE OBJET CONFIDENTIEL",
    )
    _seed_enregistre(fake, cle1, "imm-b1", numero="2026-001")
    _seed_enregistre(fake, cle2, "imm-b2", numero="2026-001")

    ticks = iter([0.0, 30.0])
    deferred_res = courrier_service.deplacer_courriels(
        [
            {"cle": cle1, "id": "imm-b1", "action": "classer", "dossier": "2026-001"},
            {"cle": cle2, "id": "imm-b2", "action": "classer", "dossier": "2026-001"},
        ],
        clock=lambda: next(ticks),
        budget_secondes=25.0,
    )
    assert deferred_res["classes"] == 1 and deferred_res["differes"] == 1
    assert deferred_res["messages"][0]["statut"] == "ok"
    assert deferred_res["messages"][1]["statut"] == "differe"

    # Full MCP live call + idempotent replay + zero-PII check
    live = handlers.move_courriels({
        "messages": [
            {"cle": cle2, "id": "imm-b2", "action": "classer", "dossier": "2026-001"}
        ],
        "idempotency_key": "move-replay-key",
    })
    assert live["classes"] == 1 and live["idempotent_replay"] is False
    writes_after = len(gm.write_calls)

    replay = handlers.move_courriels({
        "messages": [
            {"cle": cle2, "id": "imm-b2", "action": "classer", "dossier": "2026-001"}
        ],
        "idempotency_key": "move-replay-key",
    })
    assert replay["idempotent_replay"] is True
    assert len(gm.write_calls) == writes_after

    dumped = repr(live) + repr(fake.peek_collection("mcp_idempotency"))
    for forbidden in ("CONFIDENTIEL", "Secret Client", "avocat@cabinet.example"):
        assert forbidden not in dumped


def test_manually_moved_or_soft_deleted_message_returns_introuvable_without_writes(env):
    fake, gm = env
    cle_deleted = gm.add_message(
        msg_id="imm-soft-del",
        internet_message_id="<softdel@cabinet.example>",
        parent_folder_id=gm.deleted_id,
    )
    _seed_enregistre(fake, cle_deleted, "imm-soft-del", numero="2026-001")

    res_classer = courrier_service.deplacer_courriels([
        {
            "cle": cle_deleted,
            "id": "imm-soft-del",
            "action": "classer",
            "dossier": "2026-001",
            "outlook_creer": {"statut": "Actifs", "nom": "2026-001 - Client Test"},
        }
    ])
    assert res_classer["introuvables"] == 1 and res_classer["echecs"] == 0
    assert res_classer["messages"][0]["statut"] == "introuvable"
    assert res_classer["messages"][0]["erreur"] is None
    assert gm.write_calls == []
    assert fake.peek(f"courrier_messages/{cle_deleted}")["etat"] == courrier_model.ETAT_ENREGISTRE

    cle_moved = gm.add_message(
        msg_id="imm-moved-hand",
        internet_message_id="<movedhand@cabinet.example>",
        parent_folder_id=gm.archive_id,
    )
    res_laisser = courrier_service.deplacer_courriels([
        {"cle": cle_moved, "id": "imm-moved-hand", "action": "laisser"}
    ])
    assert res_laisser["introuvables"] == 1 and res_laisser["echecs"] == 0
    assert res_laisser["messages"][0]["statut"] == "introuvable"
    assert gm.write_calls == []

    target_fid = gm.add_dossier_folder("Actifs", "2026-002 - Idempotent", fid="fld-2026-002")
    cle_done = gm.add_message(
        msg_id="imm-done-idem",
        internet_message_id="<doneidem@cabinet.example>",
        parent_folder_id=target_fid,
        categories=[graph_courrier.CATEGORIE_CLASSE],
    )
    _seed_enregistre(fake, cle_done, "imm-done-idem", numero="2026-002")
    courrier_model.record_classe(cle_done, "imm-done-idem", outlook_folder_id=target_fid)

    res_idem = courrier_service.deplacer_courriels([
        {"cle": cle_done, "id": "imm-done-idem", "action": "classer", "dossier": "2026-002"}
    ])
    assert res_idem["classes"] == 1 and res_idem["echecs"] == 0
    assert res_idem["messages"][0]["statut"] == "ok"


def test_tool_descriptor_and_schema_wiring():
    assert "move_courriels" in tools.WRITE_TOOLS
    assert "move_courriels" in tools.EDIT_TOOLS
    assert tools.idempotency_policy("move_courriels") == tools.IDEMPOTENCY_REQUIRED


def test_number_only_outlook_dossier_names_supported(env):
    fake, gm = env

    # MCP schema accepts 8-char number-only outlook_creer.nom and rejects 7-char strings
    assert (
        tools.validate_args(
            tools.TOOLS["move_courriels"]["input_schema"],
            {
                "idempotency_key": "k-outlook-num-ok",
                "messages": [
                    {
                        "cle": "0" * 16,
                        "id": "imm-1",
                        "action": "classer",
                        "dossier": "2026-016",
                        "outlook_creer": {"statut": "Actifs", "nom": "2026-016"},
                    }
                ],
            },
        )
        == []
    )
    errs_short = tools.validate_args(
        tools.TOOLS["move_courriels"]["input_schema"],
        {
            "idempotency_key": "k-outlook-num-bad",
            "messages": [
                {
                    "cle": "0" * 16,
                    "id": "imm-1",
                    "action": "classer",
                    "dossier": "2026-016",
                    "outlook_creer": {"statut": "Actifs", "nom": "2026-01"},
                }
            ],
        },
    )
    assert errs_short

    # _clean_dossier_outlook_name accepts number-only & prefixed, rejects invalid
    assert courrier_service._clean_dossier_outlook_name("2026-016", "2026-016") == "2026-016"
    assert (
        courrier_service._clean_dossier_outlook_name("2026-016 - Client", "2026-016")
        == "2026-016 - Client"
    )
    for bad in ("2026-017", "2026-0160", "2026-016/sub"):
        assert courrier_service._clean_dossier_outlook_name(bad, "2026-016") is None
    assert courrier_service._clean_dossier_outlook_name("2026-016", "") is None

    # Classify into existing number-only Outlook subfolder ("2026-010")
    fid_10 = gm.add_dossier_folder("Actifs", "2026-010", fid="fld-2026-010")
    cle_10 = gm.add_message(msg_id="imm-num-10", internet_message_id="<out10@cabinet.example>")
    _seed_enregistre(fake, cle_10, "imm-num-10", numero="2026-010")

    res_existing = handlers.move_courriels({
        "idempotency_key": "move-num-10",
        "messages": [
            {"cle": cle_10, "id": "imm-num-10", "action": "classer", "dossier": "2026-010"}
        ],
    })
    assert res_existing["classes"] == 1 and res_existing["echecs"] == 0
    assert res_existing["messages"][0]["outlook_folder_id"] == fid_10
    assert res_existing["messages"][0]["dossier_outlook_cree"] is False
    assert gm.messages["imm-num-10"]["parentFolderId"] == fid_10

    # Create and classify into new number-only Outlook subfolder ("2026-016")
    cle_16 = gm.add_message(msg_id="imm-num-16", internet_message_id="<out16@cabinet.example>")
    _seed_enregistre(fake, cle_16, "imm-num-16", numero="2026-016")

    res_create = handlers.move_courriels({
        "idempotency_key": "move-num-16",
        "messages": [
            {
                "cle": cle_16,
                "id": "imm-num-16",
                "action": "classer",
                "dossier": "2026-016",
                "outlook_creer": {"statut": "Actifs", "nom": "2026-016"},
            }
        ],
    })
    assert res_create["classes"] == 1 and res_create["echecs"] == 0
    created_fid = res_create["messages"][0]["outlook_folder_id"]
    assert res_create["messages"][0]["dossier_outlook_cree"] is True
    assert gm.folders[created_fid]["displayName"] == "2026-016"
    assert gm.messages["imm-num-16"]["parentFolderId"] == created_fid
