"""Tests for Courrier Phase 5: atomic record_courriels_temps & zero-PII observability."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
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
    from utils import graph_courrier, storage_identity
    from utils.logging_setup import ContextFilter, RedactionFilter
    from utils.quickxorhash import quickxorhash_b64

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402


def _fake_modules() -> list:
    return [
        m
        for n, m in sorted(sys.modules.items())
        if (n == "models" or n.startswith("models.") or n in ("dav.sync", "mcp.write_support"))
        and getattr(m, "db", None) is not None
    ]


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self.addFilter(ContextFilter())
        self.addFilter(RedactionFilter())

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def env(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    monkeypatch.setattr(Config, "COURRIER_ACTIF", True)
    did1 = "a0000000-0000-4000-8000-000000000001"
    did2 = "a0000000-0000-4000-8000-000000000002"
    fake.seed(
        f"dossiers/{did1}",
        {
            "id": did1,
            "file_number": "2026-001",
            "title": "Tremblay c. Gagnon",
            "status": "actif",
            "hourly_rate": 30000,
        },
    )
    fake.seed(
        f"dossiers/{did2}",
        {
            "id": did2,
            "file_number": "2026-002",
            "title": "Lavoie c. Bouchard",
            "status": "actif",
            "hourly_rate": 25000,
        },
    )
    return fake, did1, did2


def _seed_saved_email(
    fake,
    cle: str,
    dossier_id: str,
    dossier_numero: str,
    *,
    etat: str = courrier_model.ETAT_ENREGISTRE,
    legacy_ref: str = "",
    time_entry_id: str = "",
    expire_at=None,
) -> None:
    fake.seed(
        f"courrier_messages/{cle}",
        {
            "cle": cle,
            "graph_id": f"imm-{cle}",
            "etat": etat,
            "dossier_id": dossier_id,
            "dossier_numero": dossier_numero,
            "fichiers": [],
            "legacy_ref": legacy_ref,
            "time_entry_id": time_entry_id,
            "outlook_folder_id": "",
            "saved_at": datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
            "moved_at": None,
            "expire_at": expire_at,
        },
    )


# ── legacy_ref sorting & validation ─────────────────────────────────────


def test_build_courrier_legacy_ref_picks_alphabetically_first_cle():
    cle_b = "ffffffffffffffff"
    cle_a = "000000000000000a"
    cle_c = "1234567890abcdef"
    ref = courrier_model.build_courrier_legacy_ref(
        "2026-001", "2026-11-20", [cle_b, cle_c, cle_a]
    )
    assert ref == f"recep:2026-001:2026-11-20:{cle_a}"

    with pytest.raises(ValueError):
        courrier_model.build_courrier_legacy_ref("BAD", "2026-11-20", [cle_a])
    with pytest.raises(ValueError):
        courrier_model.build_courrier_legacy_ref("2026-001", "2026-99-99", [cle_a])
    with pytest.raises(ValueError):
        courrier_model.build_courrier_legacy_ref("2026-001", "2026-11-20", [])


# ── Switch guard ────────────────────────────────────────────────────────


def test_record_courriels_temps_refused_when_switch_off(env, monkeypatch):
    fake, did1, _ = env
    cle = "aaaaaaaaaaaaaaaa"
    _seed_saved_email(fake, cle, did1, "2026-001")
    ref = courrier_model.build_courrier_legacy_ref("2026-001", "2026-11-20", [cle])
    monkeypatch.setattr(Config, "COURRIER_ACTIF", False)

    payload_args = {
        "entrees": [
            {
                "legacy_ref": ref,
                "cles": [cle],
                "entree": {
                    "dossier_id": did1,
                    "date": "2026-11-20",
                    "description": "Lecture du courriel.",
                    "hours": 0.25,
                },
            }
        ],
        "idempotency_key": "temps-off",
    }
    for essai in (False, True):
        with pytest.raises(PermissionError):
            courrier_service.inscrire_temps_courriels(
                payload_args["entrees"], essai=essai
            )
        with pytest.raises(ToolArgumentError, match="COURRIER_ACTIF=false"):
            handlers.record_courriels_temps({**payload_args, "essai": essai})
    assert fake.peek_collection("timeentries") == {}


# ── Atomic creation & linking ───────────────────────────────────────────


def test_atomic_multi_email_creation_links_registry_and_find_imported(env):
    fake, did1, did2 = env
    cle1 = "bbbbbbbbbbbbbbbb"
    cle2 = "aaaaaaaaaaaaaaaa"
    cle3 = "cccccccccccccccc"
    _seed_saved_email(fake, cle1, did1, "2026-001")
    _seed_saved_email(fake, cle2, did1, "2026-001", etat=courrier_model.ETAT_CLASSE)
    _seed_saved_email(fake, cle3, did2, "2026-002")

    ref1 = courrier_model.build_courrier_legacy_ref("2026-001", "2026-11-20", [cle1, cle2])
    ref2 = courrier_model.build_courrier_legacy_ref("2026-002", "2026-11-20", [cle3])

    res = handlers.record_courriels_temps({
        "entrees": [
            {
                "legacy_ref": ref1,
                "cles": [cle1, cle2],
                "entree": {
                    "dossier_id": did1,
                    "date": "2026-11-20",
                    "description": "Analyse des courriels de la partie adverse.",
                    "hours": 0.5,
                    "phase": "CTS",
                },
            },
            {
                "legacy_ref": ref2,
                "cles": [cle3],
                "entree": {
                    "dossier_id": did2,
                    "date": "2026-11-20",
                    "description": "Courriel non facturable au dossier.",
                    "hours": 0.25,
                    "billable": False,
                },
            },
        ],
        "idempotency_key": "temps-batch-1",
    })
    assert res["essai"] is False
    assert res["creees"] == 2 and res["courriels_lies"] == 3
    assert res["idempotent_replay"] is False

    e1, e2 = res["entrees"]
    assert e1["rate_cents"] == 30000 and e1["amount_cents"] == 15000
    assert e2["rate_cents"] == 25000 and e2["amount_cents"] == 0 and e2["billable"] is False

    stored_t1 = fake.peek(f"timeentries/{e1['time_entry_id']}")
    assert stored_t1["created_via"] == "mcp"
    assert stored_t1["legacy_ref"] == ref1
    assert stored_t1["amount"] == 15000
    assert stored_t1["sous_phase"] == "CTS-00"

    for cle in (cle1, cle2):
        reg = fake.peek(f"courrier_messages/{cle}")
        assert reg["legacy_ref"] == ref1
        assert reg["time_entry_id"] == e1["time_entry_id"]
        assert reg["updated_via"] == "mcp"
        assert reg["expire_at"] is None

    reg3 = fake.peek(f"courrier_messages/{cle3}")
    assert reg3["legacy_ref"] == ref2
    assert reg3["time_entry_id"] == e2["time_entry_id"]

    # find_imported locates it durably past the 24 h MCP replay window
    found = handlers.find_imported({"legacy_ref": ref1})
    assert found["count"] == 1 and found["matches"][0]["id"] == e1["time_entry_id"]

    # Replay returns stored payload verbatim without writing duplicate rows
    replay = handlers.record_courriels_temps({
        "entrees": [
            {
                "legacy_ref": ref1,
                "cles": [cle1, cle2],
                "entree": {
                    "dossier_id": did1,
                    "date": "2026-11-20",
                    "description": "Analyse des courriels de la partie adverse.",
                    "hours": 0.5,
                    "phase": "CTS",
                },
            },
            {
                "legacy_ref": ref2,
                "cles": [cle3],
                "entree": {
                    "dossier_id": did2,
                    "date": "2026-11-20",
                    "description": "Courriel non facturable au dossier.",
                    "hours": 0.25,
                    "billable": False,
                },
            },
        ],
        "idempotency_key": "temps-batch-1",
    })
    assert replay["idempotent_replay"] is True
    assert len(fake.peek_collection("timeentries")) == 2


# ── All-or-nothing refusals ─────────────────────────────────────────────


def test_refuses_unsaved_wrong_dossier_and_already_billed_email_atomically(env):
    fake, did1, did2 = env
    cle_ok = "1111111111111111"
    cle_inv = "2222222222222222"
    cle_other = "3333333333333333"
    cle_billed = "4444444444444444"
    _seed_saved_email(fake, cle_ok, did1, "2026-001")
    _seed_saved_email(
        fake,
        cle_inv,
        did1,
        "2026-001",
        etat=courrier_model.ETAT_INVENTORIE,
        expire_at=datetime.now(timezone.utc) + timedelta(days=10),
    )
    _seed_saved_email(fake, cle_other, did2, "2026-002")
    _seed_saved_email(
        fake,
        cle_billed,
        did1,
        "2026-001",
        legacy_ref=f"recep:2026-001:2026-11-19:{cle_billed}",
        time_entry_id="existing-te-id",
    )

    # Even if row 0 is valid, failure on row 1 aborts ALL writes
    ref_valid = courrier_model.build_courrier_legacy_ref("2026-001", "2026-11-20", [cle_ok])
    for bad_cle, msg_fragment in (
        ("9999999999999999", "non enregistré"),
        (cle_inv, "non enregistré"),
        (cle_other, "autre dossier"),
        (cle_billed, "déjà compté"),
    ):
        ref_bad = courrier_model.build_courrier_legacy_ref(
            "2026-001", "2026-11-20", [bad_cle]
        )
        with pytest.raises(ToolArgumentError, match=msg_fragment):
            handlers.record_courriels_temps({
                "entrees": [
                    {
                        "legacy_ref": ref_valid,
                        "cles": [cle_ok],
                        "entree": {
                            "dossier_id": did1,
                            "date": "2026-11-20",
                            "description": "Courriel valide dans un lot refusé.",
                            "hours": 0.25,
                        },
                    },
                    {
                        "legacy_ref": ref_bad,
                        "cles": [bad_cle],
                        "entree": {
                            "dossier_id": did1,
                            "date": "2026-11-20",
                            "description": "Courriel en erreur.",
                            "hours": 0.25,
                        },
                    },
                ],
                "idempotency_key": f"bad-{bad_cle}",
            })
        assert fake.peek_collection("timeentries") == {}
        assert fake.peek(f"courrier_messages/{cle_ok}")["legacy_ref"] == ""


def test_refuses_legacy_ref_mismatch_collision_duplicate_and_bad_hours(env):
    fake, did1, _ = env
    cle1 = "aaaaaaaaaaaaaaaa"
    cle2 = "bbbbbbbbbbbbbbbb"
    _seed_saved_email(fake, cle1, did1, "2026-001")
    _seed_saved_email(fake, cle2, did1, "2026-001")

    # Wrong first cle in legacy_ref
    wrong_ref = f"recep:2026-001:2026-11-20:{cle2}"
    with pytest.raises(ToolArgumentError, match="attendu"):
        handlers.record_courriels_temps({
            "entrees": [
                {
                    "legacy_ref": wrong_ref,
                    "cles": [cle2, cle1],
                    "entree": {
                        "dossier_id": did1,
                        "date": "2026-11-20",
                        "description": "Lecture.",
                        "hours": 0.25,
                    },
                }
            ],
            "idempotency_key": "k-wrong-ref",
        })

    # Hours with > 2 decimals refused
    good_ref = courrier_model.build_courrier_legacy_ref("2026-001", "2026-11-20", [cle1])
    with pytest.raises(ToolArgumentError, match="deux décimales"):
        handlers.record_courriels_temps({
            "entrees": [
                {
                    "legacy_ref": good_ref,
                    "cles": [cle1],
                    "entree": {
                        "dossier_id": did1,
                        "date": "2026-11-20",
                        "description": "Lecture.",
                        "hours": 0.333,
                    },
                }
            ],
            "idempotency_key": "k-hours",
        })

    # Same cle appearing in two entries of the same batch refused
    with pytest.raises(ToolArgumentError, match="deux entrées"):
        handlers.record_courriels_temps({
            "entrees": [
                {
                    "legacy_ref": good_ref,
                    "cles": [cle1],
                    "entree": {
                        "dossier_id": did1,
                        "date": "2026-11-20",
                        "description": "Entrée 1.",
                        "hours": 0.25,
                    },
                },
                {
                    "legacy_ref": good_ref,
                    "cles": [cle1],
                    "entree": {
                        "dossier_id": did1,
                        "date": "2026-11-20",
                        "description": "Entrée 2.",
                        "hours": 0.25,
                    },
                },
            ],
            "idempotency_key": "k-dup-cle",
        })

    # Collision with already imported timeentry sharing same legacy_ref
    fake.seed(
        "timeentries/existing-1",
        {"id": "existing-1", "dossier_id": did1, "legacy_ref": good_ref},
    )
    with pytest.raises(ToolArgumentError, match="existe déjà"):
        handlers.record_courriels_temps({
            "entrees": [
                {
                    "legacy_ref": good_ref,
                    "cles": [cle1],
                    "entree": {
                        "dossier_id": did1,
                        "date": "2026-11-20",
                        "description": "Doublon.",
                        "hours": 0.25,
                    },
                }
            ],
            "idempotency_key": "k-collision",
        })
    assert len(fake.peek_collection("timeentries")) == 1

    # Read failure raises read_unavailable and keeps registry untouched
    with mock.patch("models.courrier.find_by_legacy_ref", side_effect=RuntimeError("boom")):
        with pytest.raises(ToolArgumentError, match="échoué") as exc_info:
            handlers.record_courriels_temps({
                "entrees": [
                    {
                        "legacy_ref": good_ref,
                        "cles": [cle1],
                        "entree": {
                            "dossier_id": did1,
                            "date": "2026-11-20",
                            "description": "Lecture.",
                            "hours": 0.25,
                        },
                    }
                ],
                "idempotency_key": "k-read-err",
            })
        assert exc_info.value.reason == "read_unavailable"


def test_essai_true_validates_all_without_writes(env):
    fake, did1, _ = env
    cle = "5555555555555555"
    _seed_saved_email(fake, cle, did1, "2026-001")
    ref = courrier_model.build_courrier_legacy_ref("2026-001", "2026-11-20", [cle])

    res = handlers.record_courriels_temps({
        "entrees": [
            {
                "legacy_ref": ref,
                "cles": [cle],
                "entree": {
                    "dossier_id": did1,
                    "date": "2026-11-20",
                    "description": "Simulation sans écriture.",
                    "hours": 0.25,
                },
            }
        ],
        "essai": True,
        "idempotency_key": "k-dry",
    })
    assert res["essai"] is True
    assert res["creees"] == 1 and res["courriels_lies"] == 1
    assert res["entrees"][0]["time_entry_id"] is None
    assert res["entrees"][0]["etag"] is None
    assert fake.peek_collection("timeentries") == {}
    assert fake.peek(f"courrier_messages/{cle}")["legacy_ref"] == ""
    assert fake.peek_collection("mcp_idempotency") == {}


# ── Zero-PII observability audit across all 5 Courrier tools (§ 11 #11) ─


def test_zero_pii_in_logs_across_all_five_courrier_tools(env, monkeypatch):
    _fake, did1, _ = env
    bucket = FakeBucket()
    monkeypatch.setattr(courrier_model.storage, "bucket", lambda: bucket)
    monkeypatch.setattr("models.document.storage.bucket", lambda: bucket)
    monkeypatch.setattr(storage_identity, "owner_uid", lambda: "u1")
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_ID", "cid")
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_SECRET", "super-secret-value")
    monkeypatch.setattr(Config, "GRAPH_TENANT_ID", "tid")
    monkeypatch.setattr(Config, "COURRIER_UPN", "avocat@cabinet.example")
    monkeypatch.setattr(Config, "COURRIER_ONEDRIVE_ROOT", "4 - Dossiers")

    canary_name = "Jean-Baptiste Canari"
    canary_email = "adversaire.canari@cabinet-secret.example"
    canary_subject = "Mise en demeure CONFIDENTIELLE Canari"
    canary_att = "contrat-confidentiel-canari.txt"
    canary_desc = "Prise de connaissance courriel CANARI confidentiel"

    imid = "<canary-msg@cabinet-secret.example>"
    cle = courrier_model.compute_cle(imid)
    eml_bytes = b"From: x\r\nSubject: CANARI\r\n\r\nBody"
    att_bytes = b"Contenu piece jointe"

    from contextlib import contextmanager

    @contextmanager
    def fake_stream(path, *, max_bytes, immutable_id=True):
        del max_bytes, immutable_id
        if path.endswith("/messages/imm-c/%24value") or path.endswith("/messages/imm-c/$value"):
            yield [eml_bytes]
        else:
            yield [att_bytes]

    monkeypatch.setattr(
        graph_courrier,
        "verifier_connexion",
        lambda: {
            "interrupteur_actif": True,
            "configure": True,
            "compte": "avocat@cabinet.example",
            "racine_onedrive": "4 - Dossiers",
            "region": "northamerica-northeast1",
            "mode_auth": "application",
            "jeton_valide": True,
            "expiration_utc": datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc),
            "reconsentement_requis": False,
            "portees": ["Mail.ReadWrite", "Files.ReadWrite.All"],
            "envoi_absent": True,
            "boite_accessible": True,
            "onedrive_accessible": True,
            "erreur": None,
        },
    )

    def fake_get(path, *, params=None, immutable_id=True, prefer=(), follow_next=True):
        del params, immutable_id, prefer, follow_next
        if path.endswith("/mailFolders/Inbox"):
            return {"id": "inbox-1", "totalItemCount": 1}
        if path.endswith("/mailFolders/Inbox/messages"):
            return {
                "value": [
                    {
                        "id": "imm-c",
                        "internetMessageId": imid,
                        "conversationId": "conv-c",
                        "receivedDateTime": "2026-11-20T14:00:00Z",
                        "from": {"emailAddress": {"name": canary_name, "address": canary_email}},
                        "toRecipients": [{"emailAddress": {"address": "avocat@cabinet.example"}}],
                        "ccRecipients": [],
                        "subject": canary_subject,
                        "isRead": False,
                        "flag": {"flagStatus": "notFlagged"},
                        "categories": [],
                        "webLink": "https://outlook.office.com/mail/imm-c",
                        "uniqueBody": {"contentType": "text", "content": canary_subject},
                        "hasAttachments": True,
                    }
                ]
            }
        if path.endswith("/mailFolders"):
            return {"value": [{"id": "fld-archive", "displayName": "Archive"}]}
        if path.endswith("/mailFolders/fld-archive/childFolders"):
            return {"value": [{"id": "fld-dossiers", "displayName": "Dossiers"}]}
        if path.endswith("/mailFolders/fld-dossiers/childFolders"):
            return {"value": [{"id": "fld-actifs", "displayName": "Actifs"}]}
        if path.endswith("/mailFolders/fld-actifs/childFolders"):
            return {"value": [{"id": "fld-2026-001", "displayName": "2026-001 - Tremblay c. Gagnon"}]}
        if path.endswith("/childFolders"):
            return {"value": []}
        if path.endswith("/messages") and "drive" not in path:
            return {"value": []}
        if path.endswith("/drive/root:/4%20-%20Dossiers"):
            return {"id": "od-root", "folder": {"childCount": 1}}
        if path.endswith("/drive/root:/4%20-%20Dossiers:/children"):
            return {
                "value": [
                    {
                        "id": "od-2026-001",
                        "name": "2026-001 - Tremblay c. Gagnon",
                        "folder": {"childCount": 0},
                    }
                ]
            }
        if path.endswith("/messages/imm-c"):
            return {
                "id": "imm-c",
                "internetMessageId": imid,
                "receivedDateTime": "2026-11-20T14:00:00Z",
                "from": {"emailAddress": {"name": canary_name, "address": canary_email}},
                "hasAttachments": True,
            }
        if path.endswith("/messages/imm-c/attachments"):
            return {
                "value": [
                    {
                        "id": "att-c",
                        "name": canary_att,
                        "contentType": "text/plain",
                        "size": len(att_bytes),
                        "isInline": False,
                    }
                ]
            }
        raise AssertionError(f"Unexpected courrier_get {path}")

    od_subfolders: dict[str, str] = {}

    def fake_read_od(path, *, params=None):
        del params
        if path.endswith(":/Correspondance"):
            return {"id": od_subfolders.get("Correspondance", "od-corr"), "folder": {}} if "Correspondance" in od_subfolders else None
        return None

    def fake_mkdir_od(parent_id, nom, *, encoded_upn):
        del encoded_upn
        fid = f"od-{parent_id}-{nom}"
        od_subfolders[nom] = fid
        return {"id": fid, "name": nom, "folder": {}}, True

    def fake_put_od(parent_id, nom, data, *, encoded_upn):
        del parent_id, nom, encoded_upn
        return {"id": f"od-item-{len(data)}", "size": len(data), "file": {"hashes": {"quickXorHash": quickxorhash_b64(data)}}}

    monkeypatch.setattr(graph_courrier, "courrier_get", fake_get)
    monkeypatch.setattr(graph_courrier, "courrier_get_bytes", lambda path, *, max_bytes, immutable_id=True: att_bytes)
    monkeypatch.setattr(graph_courrier, "courrier_ouvrir_flux", fake_stream)
    monkeypatch.setattr(graph_courrier, "courrier_lire_element_onedrive", fake_read_od)
    monkeypatch.setattr(graph_courrier, "courrier_creer_dossier_onedrive", fake_mkdir_od)
    monkeypatch.setattr(graph_courrier, "courrier_deposer_petit_fichier_onedrive", fake_put_od)
    monkeypatch.setattr(
        graph_courrier,
        "courrier_lire_courriel",
        lambda msg_id, *, encoded_upn, select="": {
            "id": msg_id,
            "internetMessageId": imid,
            "parentFolderId": "inbox-1",
            "categories": [],
            "isRead": False,
        },
    )
    monkeypatch.setattr(graph_courrier, "courrier_assurer_categories_maitres", lambda *, encoded_upn: True)
    monkeypatch.setattr(graph_courrier, "courrier_modifier_categories_courriel", lambda msg_id, cats, *, encoded_upn: {"id": msg_id})
    monkeypatch.setattr(graph_courrier, "courrier_deplacer_courriel", lambda msg_id, dest, *, encoded_upn: {"id": msg_id, "parentFolderId": dest})

    capture = _LogCapture()
    root_logger = logging.getLogger()
    root_logger.addHandler(capture)
    prior_level = root_logger.level
    root_logger.setLevel(logging.INFO)
    try:
        handlers.get_courrier_connexion({})
        handlers.list_courriels({"premier": 0, "nombre": 5})
        handlers.save_courriels({
            "messages": [
                {
                    "cle": cle,
                    "id": "imm-c",
                    "dossier": "2026-001",
                    "dossier_onedrive": "2026-001 - Tremblay c. Gagnon",
                    "creer_dossier_onedrive": False,
                    "eml": {"sous_dossier": "Correspondance\\Courriels"},
                    "pieces": [{"rang": 1, "id": "att-c", "sous_dossier": "Correspondance", "nom": canary_att}],
                }
            ],
            "idempotency_key": "pii-save-1",
        })
        ref = courrier_model.build_courrier_legacy_ref("2026-001", "2026-11-20", [cle])
        handlers.record_courriels_temps({
            "entrees": [
                {
                    "legacy_ref": ref,
                    "cles": [cle],
                    "entree": {
                        "dossier_id": did1,
                        "date": "2026-11-20",
                        "description": canary_desc,
                        "hours": 0.25,
                    },
                }
            ],
            "idempotency_key": "pii-temps-1",
        })
        handlers.move_courriels({
            "messages": [{"cle": cle, "id": "imm-c", "action": "classer", "dossier": "2026-001"}],
            "idempotency_key": "pii-move-1",
        })
    finally:
        root_logger.removeHandler(capture)
        root_logger.setLevel(prior_level)

    courrier_records = [r for r in capture.records if r.name == "pallas.courrier"]
    assert {r.json_fields["event"] for r in courrier_records} == {
        "courrier_connexion",
        "courrier_inventaire",
        "courrier_enregistrement",
        "courrier_temps",
        "courrier_classement",
    }

    all_logged = " ".join(
        f"{r.getMessage()} {getattr(r, 'json_fields', {})!r} {getattr(r, 'exc_text', '') or ''}"
        for r in capture.records
    )
    for forbidden in (
        canary_name,
        canary_email,
        canary_subject,
        canary_att,
        canary_desc,
        "Tremblay",
        "Gagnon",
        "avocat@cabinet.example",
        "super-secret-value",
    ):
        assert forbidden not in all_logged


def test_tool_descriptor_and_registry_wiring():
    assert "record_courriels_temps" in tools.WRITE_TOOLS
    assert "record_courriels_temps" not in tools.EDIT_TOOLS
    assert tools.idempotency_policy("record_courriels_temps") == tools.IDEMPOTENCY_REQUIRED
