"""Tests for Courrier Phase 2: courrier_messages Firestore registry model."""

import hashlib
import json
import os
import pathlib
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
    from models import courrier as courrier_model
    from models import provenance

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 11, 20, 14, 0, tzinfo=UTC)
ATHENA_DIR = pathlib.Path(__file__).resolve().parents[1]
VALID_SHA = "a" * 64


def test_compute_cle_and_validity():
    raw_id = "  <synthetic-001@cabinet.example> \r\n"
    expected = hashlib.sha256(b"<synthetic-001@cabinet.example>").hexdigest()[:16]
    cle = courrier_model.compute_cle(raw_id)
    assert cle == expected
    assert courrier_model.is_valid_cle(cle) is True

    for invalid in ("", "   ", None, "a" * 15, "a" * 17, "A" * 16, "g" * 16, "0" * 15 + "\n"):
        assert courrier_model.is_valid_cle(invalid) is False

    with pytest.raises(ValueError, match="vide"):
        courrier_model.compute_cle("   ")


def test_ttl_override_declared_and_no_composite_index_exists():
    root = ATHENA_DIR.parent
    spec = json.loads((root / "firestore.indexes.json").read_text(encoding="utf-8"))
    overrides = [
        o for o in spec.get("fieldOverrides", [])
        if o.get("collectionGroup") == courrier_model.COLLECTION
    ]
    assert overrides == [
        {
            "collectionGroup": courrier_model.COLLECTION,
            "fieldPath": "expire_at",
            "ttl": True,
            "indexes": [],
        }
    ]
    assert not [
        i for i in spec.get("indexes", [])
        if i.get("collectionGroup") == courrier_model.COLLECTION
    ]


def test_record_inventorie_sets_ttl_provenance_and_refuses_downgrade(monkeypatch):
    fake = install(monkeypatch, courrier_model)
    cle = courrier_model.compute_cle("<msg-ttl@cabinet.example>")

    with provenance.writing_via("mcp", tool="move_courriels"):
        doc, errors = courrier_model.record_inventorie(cle, "graph-id-1", now=NOW)
        commits = provenance.committed_writes()

    assert errors == []
    assert doc is not None
    assert commits == ((courrier_model.COLLECTION, cle),)
    assert doc["etat"] == "inventorie"
    assert doc["expire_at"] == NOW + timedelta(days=30)
    assert doc["created_via"] == "mcp"
    assert doc["updated_via"] == "mcp"
    assert doc["mcp_updated_at"] == NOW
    assert doc["etag"]

    stored = fake.peek(f"{courrier_model.COLLECTION}/{cle}")
    assert stored["etag"] == doc["etag"]

    # In-code TTL enforcement regardless of Firestore TTL lag.
    before_expiry = NOW + timedelta(days=29, hours=23)
    at_expiry = NOW + timedelta(days=30)
    assert courrier_model.get_courrier_message_strict(cle, now=before_expiry) is not None
    assert courrier_model.get_courrier_message_strict(cle, now=at_expiry) is None
    assert courrier_model.get_courrier_message(cle, now=at_expiry) is None

    # Enregistre clears expire_at permanently and cannot be downgraded back to inventorie.
    saved, save_errors = courrier_model.record_enregistre(
        cle,
        "graph-id-1",
        dossier_id="dossier-1",
        dossier_numero="2026-001",
        fichiers=[
            {
                "slot": "eml",
                "sha256": VALID_SHA,
                "taille": 2048,
                "onedrive_item_id": "od-item-1",
                "document_id": "doc-1",
                "statut_onedrive": "ok",
                "statut_athena": "ok",
            }
        ],
        now=NOW + timedelta(minutes=5),
    )
    assert save_errors == []
    assert saved is not None
    assert saved["etat"] == "enregistre"
    assert saved["expire_at"] is None
    # Permanent records never expire, even 10 years later.
    far_future = NOW + timedelta(days=3650)
    assert courrier_model.is_expired(saved, now=far_future) is False
    assert courrier_model.get_courrier_message_strict(cle, now=far_future) is not None

    downgraded, down_errors = courrier_model.record_inventorie(
        cle, "graph-id-1", now=far_future
    )
    assert downgraded is None
    assert down_errors


def test_record_enregistre_rejects_pii_and_preserves_classe_and_legacy_ref(monkeypatch):
    fake = install(monkeypatch, courrier_model)
    cle = courrier_model.compute_cle("<msg-save@cabinet.example>")

    # Extra PII key in fichiers must be refused before any DB write.
    for pii_field in ("nom", "filename", "chemin", "path", "objet", "expediteur", "url"):
        doc, errors = courrier_model.record_enregistre(
            cle,
            "gid-1",
            dossier_id="dossier-1",
            dossier_numero="2026-001",
            fichiers=[
                {
                    "slot": "eml",
                    "sha256": VALID_SHA,
                    "taille": 100,
                    "onedrive_item_id": "od-1",
                    "document_id": "doc-1",
                    "statut_onedrive": "ok",
                    "statut_athena": "ok",
                    pii_field: "Secret Client",
                }
            ],
            now=NOW,
        )
        assert doc is None and errors
    assert fake.commits == []

    # Seed pre-existing classe + legacy_ref row and replay record_enregistre: preserves both.
    fake.seed(
        f"{courrier_model.COLLECTION}/{cle}",
        {
            "cle": cle,
            "graph_id": "gid-1",
            "etat": "classe",
            "dossier_id": "dossier-1",
            "dossier_numero": "2026-001",
            "fichiers": [],
            "legacy_ref": f"recep:2026-001:2026-11-20:{cle}",
            "time_entry_id": "te-1",
            "outlook_folder_id": "fold-1",
            "saved_at": NOW,
            "moved_at": NOW,
            "expire_at": None,
            "created_at": NOW,
            "created_via": "script",
        },
    )
    updated, errs = courrier_model.record_enregistre(
        cle,
        "gid-1",
        dossier_id="dossier-1",
        dossier_numero="2026-001",
        fichiers=[],
        now=NOW + timedelta(hours=1),
    )
    assert errs == []
    assert updated["etat"] == "classe"
    assert updated["legacy_ref"] == f"recep:2026-001:2026-11-20:{cle}"
    assert updated["time_entry_id"] == "te-1"
    assert updated["outlook_folder_id"] == "fold-1"
    assert updated["created_via"] == "script"


def test_record_classe_refuses_unsaved_or_expired_and_transitions_saved(monkeypatch):
    install(monkeypatch, courrier_model)
    cle = courrier_model.compute_cle("<msg-move@cabinet.example>")

    # Missing record refused.
    moved, errors = courrier_model.record_classe(
        cle, "gid-1", outlook_folder_id="fold-2026-001", now=NOW
    )
    assert moved is None
    assert "Enregistrement non constaté" in errors[0]

    # inventorie-only record refused.
    _, _ = courrier_model.record_inventorie(cle, "gid-1", now=NOW)
    moved2, errors2 = courrier_model.record_classe(
        cle, "gid-1", outlook_folder_id="fold-2026-001", now=NOW
    )
    assert moved2 is None
    assert "Enregistrement non constaté" in errors2[0]

    # Save first, then classer succeeds permanently.
    saved, errs = courrier_model.record_enregistre(
        cle,
        "gid-1",
        dossier_id="dossier-1",
        dossier_numero="2026-001",
        fichiers=[],
        now=NOW,
    )
    assert errs == []

    with provenance.writing_via("mcp", tool="move_courriels"):
        classed, c_errors = courrier_model.record_classe(
            cle, "gid-1", outlook_folder_id="fold-2026-001", now=NOW + timedelta(minutes=2)
        )
        commits = provenance.committed_writes()

    assert c_errors == []
    assert commits == ((courrier_model.COLLECTION, cle),)
    assert classed["etat"] == "classe"
    assert classed["outlook_folder_id"] == "fold-2026-001"
    assert classed["etag"] != saved["etag"]
    assert classed["expire_at"] is None


def test_get_courrier_messages_map_strict_handles_reversed_order_and_propagates_errors(monkeypatch):
    fake = install(monkeypatch, courrier_model)
    cle1 = courrier_model.compute_cle("<m1@cabinet.example>")
    cle2 = courrier_model.compute_cle("<m2@cabinet.example>")
    cle_exp = courrier_model.compute_cle("<m-exp@cabinet.example>")

    fake.seed(
        f"{courrier_model.COLLECTION}/{cle1}",
        {"cle": cle1, "graph_id": "g1", "etat": "enregistre", "expire_at": None},
    )
    fake.seed(
        f"{courrier_model.COLLECTION}/{cle2}",
        {
            "cle": cle2,
            "graph_id": "g2",
            "etat": "inventorie",
            "expire_at": NOW + timedelta(days=5),
        },
    )
    fake.seed(
        f"{courrier_model.COLLECTION}/{cle_exp}",
        {
            "cle": cle_exp,
            "graph_id": "g-exp",
            "etat": "inventorie",
            "expire_at": NOW - timedelta(seconds=1),
        },
    )

    # FakeFirestore.get_all reverses snapshots; map must still key accurately.
    found = courrier_model.get_courrier_messages_map_strict(
        [cle1, "invalid", cle2, cle_exp, cle1],
        now=NOW,
    )
    assert set(found) == {cle1, cle2}
    assert found[cle1]["graph_id"] == "g1"
    assert found[cle2]["graph_id"] == "g2"

    # Strict reader propagates store errors while fail-open returns None.
    def boom(*_args, **_kwargs):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(courrier_model.db, "get_all", boom)
    with pytest.raises(RuntimeError, match="firestore unavailable"):
        courrier_model.get_courrier_messages_map_strict([cle1], now=NOW)

    monkeypatch.setattr(courrier_model.db, "collection", boom)
    with pytest.raises(RuntimeError, match="firestore unavailable"):
        courrier_model.get_courrier_message_strict(cle1, now=NOW)
    assert courrier_model.get_courrier_message(cle1, now=NOW) is None
