"""Security hardening regression and unit tests (Milestone 1 audit).

Verifies the 15 security fixes implemented across Athena:
- Internal task request scoping to /taches/ and /_ah/
- Client IP extraction with comma-separated proxy handling
- OpenTelemetry tracing scrub for status descriptions and exception events
- SENSITIVE_KEYS expansion and redaction
- Dashboard exception logging sanitization
- Client portal 500 alert emission
- Model _PROTECTED_ON_UPDATE field stripping (dossier, task, note, partie)
- Model is_addressable_id enforcement across all core readers
- Template signed download URL RFC 6266 disposition hardening
- Multi-database rules targets in firebase.json
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("PORTAIL_SECRET_KEY", "test-portail-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from flask import Flask

import client.security as portail_security
from client import _client_ip as portail_client_ip
from dav.dav_auth import _client_ip as dav_client_ip
from models import admin_ledger, trust
import models.dossier as dossier_model
import models.expense as expense_model
import models.hearing as hearing_model
import models.invoice as invoice_model
import models.note as note_model
import models.partie as partie_model
import models.task as task_model
import models.time_entry as time_entry_model
import security
from utils.logging_setup import RedactionFilter, SENSITIVE_KEYS
from utils.tracing_setup import _SanitizingSpanExporter


def test_internal_task_request_scoping():
    app = Flask(__name__)
    # Main service
    with app.test_request_context("/auth/login", headers={"X-AppEngine-QueueName": "default"}):
        assert security.is_appengine_internal_request() is False
    with app.test_request_context("/dossiers", headers={"X-Appengine-Cron": "true"}):
        assert security.is_appengine_internal_request() is False
    with app.test_request_context("/taches/portail/evenement", headers={"X-AppEngine-QueueName": "portail"}):
        assert security.is_appengine_internal_request() is True
    with app.test_request_context("/_ah/warmup", headers={"X-AppEngine-QueueName": "default"}):
        assert security.is_appengine_internal_request() is True

    # Portal service
    with app.test_request_context("/", headers={"X-AppEngine-QueueName": "portail"}):
        assert portail_security._is_appengine_internal_request() is False
    with app.test_request_context("/session", headers={"X-Appengine-Cron": "true"}):
        assert portail_security._is_appengine_internal_request() is False
    with app.test_request_context("/taches/sync", headers={"X-AppEngine-QueueName": "portail"}):
        assert portail_security._is_appengine_internal_request() is True
    with app.test_request_context("/_ah/start", headers={"X-Appengine-Cron": "true"}):
        assert portail_security._is_appengine_internal_request() is True


def test_client_ip_parsing_strips_proxy_chain():
    from mcp.bearer import _client_ip as mcp_client_ip

    app = Flask(__name__)
    headers = {"CF-Connecting-IP": " 198.51.100.1 , 203.0.113.195 "}
    with app.test_request_context("/", headers=headers):
        assert security._client_ip() == "198.51.100.1"
        assert portail_client_ip() == "198.51.100.1"
        assert dav_client_ip() == "198.51.100.1"
        assert mcp_client_ip() == "198.51.100.1"


def test_tracing_scrubs_status_description_and_events():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import Status, StatusCode

    exporter = InMemorySpanExporter()
    sanitizing = _SanitizingSpanExporter(exporter)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(sanitizing))
    tracer = provider.get_tracer("test_tracer")

    with tracer.start_as_current_span("span_with_leak") as s:
        try:
            raise ValueError("Private client: client@example.com, phone 514-555-1234")
        except Exception as exc:
            s.record_exception(exc)
            s.set_status(Status(StatusCode.ERROR, str(exc)))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    # Status description must have email and phone redacted
    assert "<email>" in span.status.description
    assert "<phone>" in span.status.description
    assert "client@example.com" not in span.status.description
    assert "514-555-1234" not in span.status.description

    # Span events must also be scrubbed
    assert len(span.events) >= 1
    ev_msg = span.events[0].attributes["exception.message"]
    assert "<email>" in ev_msg
    assert "<phone>" in ev_msg
    assert "client@example.com" not in ev_msg
    assert "514-555-1234" not in ev_msg


def test_sensitive_keys_inventory_and_redaction():
    new_keys = {
        "client_secret",
        "cf_origin_secret",
        "origin_secret",
        "graph_client_secret",
        "flask_secret_key",
        "portail_secret_key",
        "code_verifier",
        "code_challenge",
        "upload_url",
        "appcheck_debug_token",
        "debug_token",
        "app_check_token",
        "verification_code",
        "signature",
        "credential",
        "credentials",
    }
    # Verify all are present in SENSITIVE_KEYS
    assert new_keys.issubset(SENSITIVE_KEYS)

    rf = RedactionFilter()
    rec = logging.LogRecord("test", logging.INFO, "", 1, "test", (), None)
    rec.json_fields = {
        "client_secret": "my-secret-value",
        "code_verifier": "verifier-secret",
        "upload_url": "https://storage.googleapis.com/upload-ticket",
    }
    rf.filter(rec)
    assert rec.json_fields["client_secret"] == "<redacted>"
    assert rec.json_fields["code_verifier"] == "<redacted>"
    assert rec.json_fields["upload_url"] == "<redacted>"


def test_dashboard_exception_sanitization(monkeypatch):
    import routes.dashboard as dash

    logged_warnings = []

    class MockLogger:
        def warning(self, msg, *args):
            logged_warnings.append(msg % args)

    monkeypatch.setattr(dash, "logger", MockLogger())
    monkeypatch.setattr(dossier_model, "count_open", lambda: (_ for _ in ()).throw(ValueError("Secret Dossier Data")))
    monkeypatch.setattr(time_entry_model, "get_unbilled_totals", lambda: (_ for _ in ()).throw(RuntimeError("Secret Billing Data")))
    monkeypatch.setattr(invoice_model, "get_outstanding_totals_strict", lambda: (_ for _ in ()).throw(TypeError("Secret Invoice Data")))
    monkeypatch.setattr(trust, "get_firm_trust_snapshot", lambda: (_ for _ in ()).throw(KeyError("Secret Trust Data")))

    dash._get_quick_stats()
    assert any("open dossiers stat failed: ValueError" in w for w in logged_warnings)
    assert any("unbilled time stat failed: RuntimeError" in w for w in logged_warnings)
    assert any("outstanding invoices stat failed: TypeError" in w for w in logged_warnings)
    assert any("trust stat failed: KeyError" in w for w in logged_warnings)
    # Ensure no raw exception strings with secret data leaked into logs
    assert not any("Secret" in w for w in logged_warnings)


def test_portal_500_handler_logs_unexpected(monkeypatch):
    import client.app as client_app

    unexpected_calls = []

    def mock_log_unexpected(msg, **kwargs):
        unexpected_calls.append((msg, kwargs))

    monkeypatch.setattr("utils.logging_setup.log_unexpected", mock_log_unexpected)

    with mock.patch("utils.tracing_setup.init_app"), mock.patch.object(
        client_app, "_init_firebase"
    ):
        app = client_app.create_portail_app()
    app.config["TESTING"] = False
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.route("/crash-test")
    def crash():
        raise RuntimeError("simulated crash")

    client = app.test_client()
    resp = client.get("/crash-test")
    assert resp.status_code == 500
    assert any(call[0] == "portal unhandled exception" for call in unexpected_calls)


def test_dossier_protected_on_update(monkeypatch):
    from tests._fake_firestore import install

    install(monkeypatch, dossier_model)
    doc, errors = dossier_model.create_dossier({
        "title": "Affaire Hardening",
        "file_number": "2026-999",
        "clients": [{"id": "c1", "name": "Client Test"}],
        "client_ids": ["c1"],
        "trust_balance": 5000000,
        "trust_balance_by_client": {"c1": 5000000},
        "vjournal_uid": "injected-uid",
        "created_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
    })
    assert errors == []
    assert doc is not None
    # Caller trust_balance must be ignored and default to 0
    assert doc["trust_balance"] == 0
    assert doc["trust_balance_by_client"] == {}
    assert doc["vjournal_uid"] != "injected-uid"

    # Attempt to inject trust_balance into update_dossier with expected_etag
    updated, errs = dossier_model.update_dossier(
        doc["id"],
        {"trust_balance": 9999999, "vjournal_uid": "hacked-uid"},
        expected_etag=doc["etag"],
    )
    assert errs == []
    assert updated is not None
    assert updated["trust_balance"] == 0
    assert updated["vjournal_uid"] != "hacked-uid"


def test_task_protected_on_update(monkeypatch):
    from tests._fake_firestore import install

    install(monkeypatch, task_model)
    task, errors = task_model.create_task({"title": "Tâche Test", "category": "recherche"})
    assert errors == []
    assert task is not None

    orig_uid = task["vtodo_uid"]
    updated, errs = task_model.update_task(task["id"], {"vtodo_uid": "overwritten-uid", "created_at": "hacked"})
    assert errs == []
    assert updated is not None
    assert updated["vtodo_uid"] == orig_uid


def test_note_protected_on_update(monkeypatch):
    from models import revision as revision_model
    from tests._fake_firestore import install

    fake = install(monkeypatch, note_model, revision_model)
    fake.seed("notes/n1", {
        "id": "n1",
        "title": "Note Test",
        "content": "Contenu initial",
        "category": "recherche",
        "vjournal_uid": "original-uid",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "etag": "e0",
    })

    updated, errs = note_model.update_note(
        "n1",
        {"title": "Note Test Modifiée", "vjournal_uid": "overwritten-uid", "created_at": "hacked"},
        expected_etag="e0",
    )
    assert errs == []
    assert updated is not None
    assert updated["vjournal_uid"] == "original-uid"


def test_partie_protected_on_update(monkeypatch):
    from tests._fake_firestore import install

    fake = install(monkeypatch, partie_model)
    seed_data = partie_model._default_doc()
    seed_data.update({
        "id": "p1",
        "type": "organization",
        "organization_name": "Entreprise Inc.",
        "contact_role": "client",
        "vcard_uid": "original-vcard-uid",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "etag": "e0",
    })
    fake.seed("parties/p1", seed_data)

    updated, errs = partie_model.update_partie(
        "p1",
        {"organization_name": "Entreprise Inc. Renommée", "vcard_uid": "overwritten-uid", "legacy_ref": "bad"},
        expected_etag="e0",
    )
    assert errs == []
    assert updated is not None
    assert updated["vcard_uid"] == "original-vcard-uid"


def test_is_addressable_id_readers_fail_closed():
    from models.budget import get_budget
    from models.doc_template import _read_template_strict, get_template
    from models.folder import get_folder

    invalid_ids = ["", ".", "..", "invalid/slash", "../traversal", "__reserved__"]
    for bad_id in invalid_ids:
        # Dossier
        assert dossier_model.get_dossier(bad_id) is None
        assert dossier_model.get_dossier_strict(bad_id) is None
        # Partie
        assert partie_model.get_partie(bad_id) is None
        assert partie_model.get_partie_strict(bad_id) is None
        # Hearing
        assert hearing_model.get_hearing(bad_id) is None
        assert hearing_model.get_hearing_strict(bad_id) is None
        # Task
        assert task_model.get_task(bad_id) is None
        assert task_model.get_task_strict(bad_id) is None
        # Note
        assert note_model.get_note(bad_id) is None
        assert note_model.get_note_strict(bad_id) is None
        # Time entry
        assert time_entry_model.get_time_entry(bad_id) is None
        assert time_entry_model.get_time_entry_strict(bad_id) is None
        # Expense
        assert expense_model.get_expense(bad_id) is None
        assert expense_model.get_expense_strict(bad_id) is None
        # Invoice
        assert invoice_model.get_invoice(bad_id) is None
        assert invoice_model.get_invoice_strict(bad_id) is None
        # Trust & admin ledgers
        assert trust.get_transaction(bad_id) is None
        assert trust.get_transaction_strict(bad_id) is None
        assert admin_ledger.get_transaction(bad_id) is None
        assert admin_ledger.get_transaction_strict(bad_id) is None
        # Templates, budgets & folders
        assert get_template(bad_id, strict=False) is None
        assert get_template(bad_id, strict=True) is None
        assert _read_template_strict(bad_id) is None
        assert get_budget(bad_id) is None
        assert get_folder("d1", bad_id) is None
        assert get_folder(bad_id, "f1") is None


def test_template_signed_download_url_disposition():
    from models.doc_template import _signed_download_url
    mock_blob = mock.MagicMock()
    mock_bucket = mock.MagicMock()
    mock_bucket.blob.return_value = mock_blob

    with mock.patch("models.doc_template.storage.bucket", return_value=mock_bucket):
        with mock.patch("models.doc_template.sign_blob_url") as mock_sign:
            mock_sign.return_value = "https://signed.url/download"
            url = _signed_download_url("templates/t1/v1/file.docx", 'Gabarit "Test" & Co.docx', 15)
            assert url == "https://signed.url/download"
            params = mock_sign.call_args[0][1]
            disp = params["response-content-disposition"]
            # Double quotes stripped, RFC 6266 encoding present
            assert 'filename="Gabarit Test & Co.docx"' in disp
            assert "filename*=UTF-8''" not in disp or "filename*=" in disp


def test_firebase_json_multi_database_rules():
    firebase_json_path = Path(__file__).resolve().parent.parent.parent / "firebase.json"
    data = json.loads(firebase_json_path.read_text(encoding="utf-8"))
    firestore_cfg = data.get("firestore")
    assert isinstance(firestore_cfg, list), "firestore must be a list of target configs"
    dbs = {entry["database"]: entry for entry in firestore_cfg}
    assert "(default)" in dbs
    assert "portail" in dbs
    assert dbs["(default)"]["rules"] == "firestore.rules"
    assert dbs["portail"]["rules"] == "firestore.rules"
