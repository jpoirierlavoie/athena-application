"""End-to-end verification of Phase 1 (Lot A) across real models, routes, and templates.

Covers every item in Phase 1 (Lot A) on the shared fake Firestore (``tests/_fake_firestore.py``):
- 1d: Suppliers via MCP (schema validation, round-trip read/create/update, PAYEE_ROLES masking)
- 1h-i & 1l defaults: Journal routing (?account_id=), OOB links, ops-first default, virement/carte defaults
- 1h-iii: Mobile Dossier dropdown (>=44px button rows, blur guards, hidden empty box, sibling forms)
- 1h-iv: Direct-to-GCS staged receipt upload at creation for both Administration and Trust
- 1l: Trust « Déboursé » label, Mode column removed from all 3 PDFs (kept in CSV/web), combined Admin « Tous les comptes » view/CSV/PDF
- 1g: Client detail « Situation financière » card (trust cards, billed/unpaid fees, WIP, co-client isolation, fail-closed, list page zero queries)
- 1b: Reworked 4 dashboard financial pulse tiles (before-tax month/quarter billed with MTL tz boundary, WIP = time + disbursements, strict ops vs trust separation, fail-closed)
"""

import csv
import io
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("PORTAIL_SECRET_KEY", "test-portail-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import (
        admin_ledger as admin_model,
        document as document_model,
        dossier as dossier_model,
        expense as expense_model,
        hearing as hearing_model,
        invoice as invoice_model,
        partie as partie_model,
        protocol as protocol_model,
        task as task_model,
        time_entry as time_entry_model,
        trust as trust_model,
    )
    _PRELOADED_MODELS = (
        admin_model, document_model, dossier_model, expense_model,
        hearing_model, invoice_model, partie_model, protocol_model,
        task_model, time_entry_model, trust_model,
    )
    import mcp.handlers as mcp_handlers
    import mcp.tools as mcp_tools
    import routes._helpers as route_helpers
    import routes.admin_ledger as admin_routes
    import routes.budgets as budgets_routes
    import routes.comptabilite as comptabilite_routes
    import routes.dashboard as dashboard_routes
    import routes.doc_templates as doc_templates_routes
    import routes.documents as documents_routes
    import routes.dossiers as dossiers_routes
    import routes.financial_statements as fs_routes
    import routes.hearings as hearings_routes
    import routes.invoices as invoices_routes
    import routes.journal_radiations as radiations_routes
    import routes.notes as notes_routes
    import routes.parties as parties_routes
    import routes.protocols as protocols_routes
    import routes.settings as settings_routes
    import routes.tasks as tasks_routes
    import routes.time_expenses as time_expenses_routes
    import routes.trust as trust_routes

from flask import Flask  # noqa: E402

from tests import test_compiled_classes as compiled  # noqa: E402
from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils import admin_journal_pdf, deadlines, trust_journal_pdf  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.html_attr import jsattr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.markdown_docx import markdown_to_safe_html  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

UTC = timezone.utc


def _fake_modules() -> list:
    discovered = [
        m for n, m in sorted(sys.modules.items())
        if (n.startswith("models.") or n == "dav.sync")
        and getattr(m, "db", None) is not None
    ]
    return list(dict.fromkeys([*_PRELOADED_MODELS, *discovered]))


@pytest.fixture
def fake(monkeypatch):
    return install(monkeypatch, *_fake_modules())


@pytest.fixture
def client(fake):
    app = Flask(
        __name__,
        template_folder=str(_ATHENA / "templates"),
        static_folder=str(_ATHENA / "static"),
    )
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.jinja_env.globals.update(
        csrf_token=lambda: "tok",
        ms=ms,
        csp_nonce="nonce",
    )
    app.jinja_env.filters.update(
        to_mtl=to_mtl,
        phone=format_phone_display,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=jsattr,
        markdown=markdown_to_safe_html,
    )
    for bp in (
        dashboard_routes.dashboard_bp,
        parties_routes.parties_bp,
        dossiers_routes.dossiers_bp,
        time_expenses_routes.time_expenses_bp,
        invoices_routes.invoices_bp,
        hearings_routes.hearings_bp,
        tasks_routes.tasks_bp,
        protocols_routes.protocols_bp,
        documents_routes.documents_bp,
        notes_routes.notes_bp,
        doc_templates_routes.doc_templates_bp,
        doc_templates_routes.modeles_bp,
        trust_routes.trust_bp,
        budgets_routes.budgets_bp,
        admin_routes.admin_bp,
        comptabilite_routes.comptabilite_bp,
        radiations_routes.journal_radiations_bp,
        fs_routes.etats_bp,
        fs_routes.etats_anciens_bp,
        settings_routes.settings_bp,
    ):
        app.register_blueprint(bp)

    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _seed_admin_accounts(fake):
    # Card is alphabetically BEFORE Operations on purpose to test 1h-i default selection.
    fake.seed("admin_accounts/acc-card", {
        "id": "acc-card",
        "name": "A Carte Corporative",
        "account_type": "carte_crédit",
        "status": "actif",
        "institution": "BNC",
        "account_number_last4": "4444",
        "ledger_balance": -25000,
    })
    fake.seed("admin_accounts/acc-ops", {
        "id": "acc-ops",
        "name": "Z Opérations Cabinet",
        "account_type": "opérations",
        "status": "actif",
        "institution": "Desjardins",
        "account_number_last4": "1111",
        "ledger_balance": 450000,
    })


def _seed_trust_accounts(fake):
    fake.seed("trust_accounts/ta1", {
        "id": "ta1",
        "name": "Compte général 1",
        "account_type": "général",
        "status": "actif",
        "institution": "BNC",
        "account_number_last4": "9001",
        "book_balance": 300000,
        "bank_balance": 300000,
    })
    fake.seed("trust_accounts/ta2", {
        "id": "ta2",
        "name": "Compte général 2",
        "account_type": "général",
        "status": "actif",
        "institution": "RBC",
        "account_number_last4": "9002",
        "book_balance": 100000,
        "bank_balance": 100000,
    })


def _assert_all_classes_compiled(html_text: str):
    import re

    classes = {
        c
        for attr in re.findall(r'(?<![:\w-])class="([^"]+)"', html_text)
        for c in attr.split()
    }
    css = compiled._artifact(_ATHENA / "static" / "vendor").read_text(encoding="utf-8")
    selectors = compiled.compiled_selectors(css)
    missing = sorted(
        c for c in classes
        if c not in {"notranslate"} and not compiled.is_compiled(c, selectors)
    )
    assert missing == [], f"Uncompiled CSS classes found: {missing}"


# ═══════════════════════════════════════════════════════════════════════════
# 1d — Suppliers via MCP (schema validation + handler enforcement)
# ═══════════════════════════════════════════════════════════════════════════


def test_1d_mcp_tools_validate_args_and_round_trip(fake):
    create_args = {
        "type": "organization",
        "organization_name": "Imprimerie Québec inc.",
        "contact_role": "fournisseur",
        "numero_tps": "123456789 RT 0001",
        "numero_tvq": "1234567890 TQ 0001",
        "categorie_depense_defaut": "fournitures",
        "numero_compte_fournisseur": "CLI-99",
    }
    err = mcp_tools.validate_args(
        mcp_tools.TOOLS["create_partie"]["input_schema"], create_args,
    )
    assert err == []
    created = mcp_handlers.create_partie(create_args)
    pid = created["entity"]["id"]
    fetched_after_create = mcp_handlers.get_partie({"partie_id": pid})["partie"]
    assert fetched_after_create["numero_tps"] == "123456789RT0001"
    assert fetched_after_create["numero_tvq"] == "1234567890TQ0001"
    assert fetched_after_create["categorie_depense_defaut"] == "fournitures"
    assert fetched_after_create["numero_compte_fournisseur"] == "CLI-99"

    # Invalid expense category must fail tool argument validation before handler runs.
    bad_err = mcp_tools.validate_args(
        mcp_tools.TOOLS["update_partie"]["input_schema"],
        {
            "partie_id": pid,
            "expected_etag": created["entity"]["etag"],
            "categorie_depense_defaut": "cat_inconnue",
        },
    )
    assert bad_err

    # Update supplier fields, then verify get_partie exposes them.
    upd_args = {
        "partie_id": pid,
        "expected_etag": created["entity"]["etag"],
        "categorie_depense_defaut": "",
        "numero_compte_fournisseur": "CLI-100",
    }
    err = mcp_tools.validate_args(
        mcp_tools.TOOLS["update_partie"]["input_schema"], upd_args,
    )
    assert err == []
    updated = mcp_handlers.update_partie(upd_args)
    assert updated["updated"] is True

    fetched = mcp_handlers.get_partie({"partie_id": pid})["partie"]
    assert fetched["categorie_depense_defaut"] == ""
    assert fetched["numero_compte_fournisseur"] == "CLI-100"
    assert fetched["numero_tps"] == "123456789RT0001"


# ═══════════════════════════════════════════════════════════════════════════
# 1h-i & 1l — Journal account routing, ops-first default, virement/carte
# ═══════════════════════════════════════════════════════════════════════════


def _has_selected_option(html_text: str, value: str) -> bool:
    return bool(re.search(rf'<option value="{re.escape(value)}"[^>]*\bselected\b', html_text))


def test_1h_i_admin_defaults_to_first_active_ops_and_virement(client, fake):
    _seed_admin_accounts(fake)
    resp = client.get("/administration/nouvelle")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Must preselect Operations (acc-ops), NOT alphabetically first Card (acc-card)
    assert _has_selected_option(body, "acc-ops")
    assert not _has_selected_option(body, "acc-card")
    # Default payment mode for Operations is virement
    assert _has_selected_option(body, "virement")


def test_1h_i_admin_account_id_param_preselects_card_with_carte_method(client, fake):
    _seed_admin_accounts(fake)
    resp = client.get("/administration/nouvelle?account_id=acc-card")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert _has_selected_option(body, "acc-card")
    assert _has_selected_option(body, "carte")


def test_1h_i_trust_account_id_param_preselects_and_defaults_to_virement(client, fake):
    _seed_trust_accounts(fake)
    resp_default = client.get("/fideicommis/nouvelle")
    assert resp_default.status_code == 200
    b1 = resp_default.get_data(as_text=True)
    assert _has_selected_option(b1, "ta1")
    assert _has_selected_option(b1, "virement")

    resp_ta2 = client.get("/fideicommis/nouvelle?account_id=ta2")
    assert resp_ta2.status_code == 200
    b2 = resp_ta2.get_data(as_text=True)
    assert _has_selected_option(b2, "ta2")
    assert _has_selected_option(b2, "virement")


def test_1h_i_filtered_list_and_oob_links_propagate_account_id(client, fake):
    _seed_admin_accounts(fake)
    _seed_trust_accounts(fake)

    # Admin journal page + HTMX OOB
    r_adm = client.get("/administration/?account_id=acc-card")
    assert r_adm.status_code == 200
    assert '/administration/nouvelle?account_id=acc-card' in r_adm.get_data(as_text=True)

    r_adm_hx = client.get(
        "/administration/?account_id=acc-card",
        headers={"HX-Request": "true"},
    )
    assert r_adm_hx.status_code == 200
    hx_body = r_adm_hx.get_data(as_text=True)
    assert 'id="admin-new-entry"' in hx_body
    assert 'id="admin-new-entry-fab"' in hx_body
    assert 'hx-swap-oob="true"' in hx_body
    assert '/administration/nouvelle?account_id=acc-card' in hx_body

    # Combined view ?account_id=tous must NOT forward ?account_id=tous to /nouvelle
    r_tous = client.get("/administration/?account_id=tous")
    assert r_tous.status_code == 200
    assert '/administration/nouvelle?account_id=tous' not in r_tous.get_data(as_text=True)

    # Trust journal page + HTMX OOB
    r_trust_hx = client.get(
        "/fideicommis/?account_id=ta2",
        headers={"HX-Request": "true"},
    )
    assert r_trust_hx.status_code == 200
    thx = r_trust_hx.get_data(as_text=True)
    assert 'id="trust-new-entry"' in thx
    assert 'id="trust-new-entry-fab"' in thx
    assert 'hx-swap-oob="true"' in thx
    assert '/fideicommis/nouvelle?account_id=ta2' in thx

    # Account detail cards and hub also pass ?account_id=
    assert '/administration/nouvelle?account_id=acc-card' in client.get(
        "/administration/comptes/acc-card"
    ).get_data(as_text=True)
    assert '/fideicommis/nouvelle?account_id=ta2' in client.get(
        "/fideicommis/comptes/ta2"
    ).get_data(as_text=True)
    hub_body = client.get("/comptabilite/").get_data(as_text=True)
    assert '/administration/nouvelle?account_id=acc-ops' in hub_body
    assert '/fideicommis/nouvelle?account_id=ta1' in hub_body


# ═══════════════════════════════════════════════════════════════════════════
# 1h-iii — Mobile Dossier autocomplete dropdown
# ═══════════════════════════════════════════════════════════════════════════


def test_1h_iii_standard_dossier_row_and_dropdown_guards(client, fake):
    _seed_admin_accounts(fake)
    _seed_trust_accounts(fake)

    html_row = route_helpers.standard_dossier_row(
        {
            "id": "d1",
            "file_number": "2026-001",
            "title": "Tremblay <Test> & Co",
            "client_name": "Jean Tremblay",
        },
        extra_attrs='data-dossier-client-id="c1"',
    )
    assert html_row.startswith('<li><button type="button"')
    assert "min-h-[44px]" in html_row
    assert 'data-dossier-id="d1"' in html_row
    assert 'data-dossier-client-id="c1"' in html_row
    assert "&lt;Test&gt;" in html_row
    _assert_all_classes_compiled(html_row)

    for url in (
        "/administration/nouvelle",
        "/fideicommis/nouvelle",
        "/fideicommis/virement",
    ):
        page = client.get(url).get_data(as_text=True)
        assert "@pointerdown.prevent" in page
        assert "@mousedown.prevent" in page
        assert "button[data-dossier-id]" in page
        assert "dossierEmpty" in page or "fromDEmpty" in page

    # Sibling forms updated to match <button data-dossier-id>
    for tpl_rel in (
        "templates/hearings/form.html",
        "templates/notes/form.html",
        "templates/tasks/form.html",
        "templates/time_expenses/expense_form.html",
        "templates/time_expenses/time_form.html",
    ):
        src = (_ATHENA / tpl_rel).read_text(encoding="utf-8")
        assert "button[data-dossier-id]" in src, tpl_rel
        assert "div[data-dossier-id]" not in src, tpl_rel


# ═══════════════════════════════════════════════════════════════════════════
# 1h-iv — Proof upload at creation (Administration & Trust)
# ═══════════════════════════════════════════════════════════════════════════


def _mock_storage(monkeypatch, module, *, pdf_bytes=b"%PDF-1.7 test", md5="XUFAKrxLKna5cZ2REBfFkg=="):
    staging_blob = mock.MagicMock()
    staging_blob.size = len(pdf_bytes)
    staging_blob.md5_hash = md5
    staging_blob.download_as_bytes.return_value = pdf_bytes
    staging_blob.create_resumable_upload_session.return_value = "https://up.example/session"

    dest_blob = mock.MagicMock()
    dest_blob.rewrite.return_value = (None, len(pdf_bytes), len(pdf_bytes))

    bucket = mock.Mock()
    bucket.blob.side_effect = lambda p: staging_blob if p.startswith("staging/") else dest_blob
    monkeypatch.setattr(module.storage, "bucket", lambda: bucket)
    return staging_blob, dest_blob, bucket


def test_1h_iv_admin_create_depense_attaches_receipt_and_copies_to_mandat(
    client, fake, monkeypatch,
):
    _seed_admin_accounts(fake)
    fake.seed("dossiers/dos1", {
        "id": "dos1",
        "file_number": "2026-010",
        "title": "Dossier A",
        "status": "actif",
        "client_ids": ["c1"],
    })
    copied = {}

    def _fake_verser(tx_id, uid, *, document_id=None):
        copied["tx_id"] = tx_id
        copied["uid"] = uid
        copied["document_id"] = document_id
        return SimpleNamespace(document={"id": document_id}, code="versee")

    monkeypatch.setattr(
        admin_routes.pieces_justificatives,
        "verser_recu_au_dossier",
        _fake_verser,
    )
    staging_blob, dest_blob, _ = _mock_storage(monkeypatch, admin_routes)
    staged_objet = "staging/u1/3f2b8c1e-9d4a-4e6b-8f7a-1c2d3e4f5a6b/recu.pdf"

    resp = client.post("/administration/", data={
        "account_id": "acc-ops",
        "kind": "dépense",
        "date": "2026-11-10",
        "amount": "114.98",
        "counterparty": "Bell Canada",
        "description": "Téléphone",
        "category": "fournitures",
        "method": "virement",
        "dossier_id": "dos1",
        "tax_included": "1",
        "staged_receipt_objet": staged_objet,
        "staged_receipt_name": "recu.pdf",
    })
    assert resp.status_code == 302
    loc = resp.headers["Location"]
    assert "avertissement=" not in loc
    tx_id = loc.rstrip("/").split("/")[-1]

    saved = admin_model.get_transaction(tx_id)
    assert saved["receipt_filename"] == "recu.pdf"
    assert saved["receipt_file_type"] == "application/pdf"
    assert saved["receipt_storage_path"] == (
        f"users/u1/administration/{tx_id}/3f2b8c1e-9d4a-4e6b-8f7a-1c2d3e4f5a6b/recu.pdf"
    )
    assert dest_blob.content_disposition == "attachment"
    staging_blob.delete.assert_called_once()
    # Automatic copy into Mandat > Déboursés ran for dossier dépense!
    assert copied["tx_id"] == tx_id
    assert copied["uid"] == "u1"
    assert copied["document_id"]


def test_1h_iv_admin_create_non_depense_does_not_copy_and_400_preserves_staging(
    client, fake, monkeypatch,
):
    _seed_admin_accounts(fake)
    monkeypatch.setattr(
        admin_routes.pieces_justificatives,
        "verser_recu_au_dossier",
        lambda *a, **k: pytest.fail("Should not copy non-dépense receipt to dossier"),
    )
    staging_blob, _, _ = _mock_storage(monkeypatch, admin_routes)
    staged_objet = "staging/u1/3f2b8c1e-9d4a-4e6b-8f7a-1c2d3e4f5a6b/bordereau.pdf"

    # 1) Validation 400 preserves hidden staging inputs
    bad_resp = client.post("/administration/", data={
        "account_id": "acc-ops",
        "kind": "recette_autre",
        "date": "2026-11-10",
        "amount": "",  # missing amount -> 400
        "counterparty": "Tiers",
        "description": "Test",
        "method": "virement",
        "staged_receipt_objet": staged_objet,
        "staged_receipt_name": "bordereau.pdf",
    })
    assert bad_resp.status_code == 400
    bad_html = bad_resp.get_data(as_text=True)
    assert staged_objet in bad_html
    assert "bordereau.pdf" in bad_html

    # 2) Valid recette_autre attaches receipt without dossier copy
    ok_resp = client.post("/administration/", data={
        "account_id": "acc-ops",
        "kind": "recette_autre",
        "date": "2026-11-10",
        "amount": "50.00",
        "counterparty": "Tiers",
        "description": "Ristourne",
        "method": "virement",
        "staged_receipt_objet": staged_objet,
        "staged_receipt_name": "bordereau.pdf",
    })
    assert ok_resp.status_code == 302
    assert "avertissement=" not in ok_resp.headers["Location"]
    staging_blob.delete.assert_called_once()


def test_1h_iv_admin_forged_staging_path_warns_without_losing_entry(client, fake):
    _seed_admin_accounts(fake)
    resp = client.post("/administration/", data={
        "account_id": "acc-ops",
        "kind": "recette_autre",
        "date": "2026-11-10",
        "amount": "75.00",
        "counterparty": "Tiers",
        "description": "Test warning",
        "method": "virement",
        "staged_receipt_objet": "staging/other-user/3f2b8c1e-9d4a-4e6b-8f7a-1c2d3e4f5a6b/hack.pdf",
        "staged_receipt_name": "hack.pdf",
    })
    assert resp.status_code == 302
    assert "avertissement=recu_joint" in resp.headers["Location"]


def test_1h_iv_trust_receipt_endpoints_and_creation_flow(client, fake, monkeypatch):
    _seed_trust_accounts(fake)
    fake.seed("dossiers/dos1", {
        "id": "dos1",
        "file_number": "2026-020",
        "title": "Dossier Trust",
        "status": "actif",
        "client_ids": ["c1"],
        "clients": [{"id": "c1", "name": "Jean Client"}],
        "trust_balance_by_client": {},
    })
    fake.seed("parties/c1", {"id": "c1", "last_name": "Client", "first_name": "Jean"})

    # 1) Staging upload session validation
    bad_ext = client.post(
        "/fideicommis/api/televersement",
        data=json.dumps({"name": "malware.exe", "size": 100}),
        content_type="application/json",
    )
    assert bad_ext.status_code == 422
    assert trust_model.RECEIPT_TYPE_REFUSED_FR in bad_ext.get_json()["erreur"]

    bad_size = client.post(
        "/fideicommis/api/televersement",
        data=json.dumps({"name": "ok.pdf", "size": 20 * 1024 * 1024}),
        content_type="application/json",
    )
    assert bad_size.status_code == 422

    staging_blob, dest_blob, _bucket = _mock_storage(monkeypatch, trust_routes)
    ok_stage = client.post(
        "/fideicommis/api/televersement",
        data=json.dumps({"name": "preuve.pdf", "size": 1024}),
        content_type="application/json",
    )
    assert ok_stage.status_code == 200
    assert ok_stage.get_json()["objet"].startswith("staging/u1/")

    # 2) Validation 400 on entry_create preserves staged_receipt_objet & name
    staged_objet = "staging/u1/3f2b8c1e-9d4a-4e6b-8f7a-1c2d3e4f5a6b/preuve.pdf"
    r400 = client.post("/fideicommis/", data={
        "account_id": "ta1",
        "direction": "recette",
        "date": "2026-11-10",
        "amount": "",  # invalid
        "dossier_id": "dos1",
        "client_id": "c1",
        "counterparty": "Jean Client",
        "purpose": "dépôt_client",
        "method": "virement",
        "staged_receipt_objet": staged_objet,
        "staged_receipt_name": "preuve.pdf",
    })
    assert r400.status_code == 400
    assert staged_objet in r400.get_data(as_text=True)

    # 3) Create trust entry WITH staged receipt
    r_ok = client.post("/fideicommis/", data={
        "account_id": "ta1",
        "direction": "recette",
        "date": "2026-11-10",
        "amount": "1000.00",
        "dossier_id": "dos1",
        "client_id": "c1",
        "counterparty": "Jean Client",
        "purpose": "dépôt_client",
        "method": "virement",
        "staged_receipt_objet": staged_objet,
        "staged_receipt_name": "preuve.pdf",
    })
    assert r_ok.status_code == 302
    loc = r_ok.headers["Location"]
    assert "avertissement=" not in loc
    tx_id = loc.rstrip("/").split("/")[-1]

    saved = trust_model.get_transaction(tx_id)
    assert saved["receipt_filename"] == "preuve.pdf"
    assert saved["receipt_storage_path"] == (
        f"users/u1/fideicommis/{tx_id}/3f2b8c1e-9d4a-4e6b-8f7a-1c2d3e4f5a6b/preuve.pdf"
    )
    assert dest_blob.content_disposition == "attachment"
    staging_blob.delete.assert_called_once()

    # Detail page renders receipt card (note template classes verified by test_compiled_classes.py)
    detail_html = client.get(f"/fideicommis/{tx_id}").get_data(as_text=True)
    assert "Pièce justificative" in detail_html
    assert "preuve.pdf" in detail_html

    # 4) Signed download redirects cleanly
    monkeypatch.setattr(
        trust_routes, "sign_blob_url", lambda blob, params: "https://gcs.example/signed-pdf",
    )
    dl_resp = client.get(f"/fideicommis/{tx_id}/recu")
    assert dl_resp.status_code == 302
    assert dl_resp.headers["Location"] == "https://gcs.example/signed-pdf"

    # 5) Existing tx api_recu rejects sniff mismatch and cleans up staging
    staging_blob.download_as_bytes.return_value = b"PK\x03\x04bad-zip"
    staging_blob.delete.reset_mock()
    bad_sniff = client.post(
        f"/fideicommis/{tx_id}/api/recu",
        data=json.dumps({"objet": staged_objet, "name": "preuve.pdf"}),
        content_type="application/json",
    )
    assert bad_sniff.status_code == 422
    staging_blob.delete.assert_called_once()

    # 5b) api_recu fails closed (503, never 404) when strict read raises
    with monkeypatch.context() as mp:
        mp.setattr(
            trust_model, "get_transaction_strict",
            lambda _tid: (_ for _ in ()).throw(RuntimeError("firestore down")),
        )
        fail_503 = client.post(
            f"/fideicommis/{tx_id}/api/recu",
            data=json.dumps({"objet": staged_objet, "name": "preuve.pdf"}),
            content_type="application/json",
        )
        assert fail_503.status_code == 503

    # 6) Foreign staging UID during creation writes entry + ?avertissement=recu_joint
    r_warn = client.post("/fideicommis/", data={
        "account_id": "ta1",
        "direction": "recette",
        "date": "2026-11-11",
        "amount": "200.00",
        "dossier_id": "dos1",
        "client_id": "c1",
        "counterparty": "Jean Client",
        "purpose": "dépôt_client",
        "method": "virement",
        "staged_receipt_objet": "staging/autre-uid/3f2b8c1e-9d4a-4e6b-8f7a-1c2d3e4f5a6b/a.pdf",
        "staged_receipt_name": "a.pdf",
    })
    assert r_warn.status_code == 302
    assert "avertissement=recu_joint" in r_warn.headers["Location"]


# ═══════════════════════════════════════════════════════════════════════════
# 1l — Trust « Déboursé », Mode removed from PDFs, combined « Tous les comptes »
# ═══════════════════════════════════════════════════════════════════════════


def test_1l_trust_debourse_label_and_pdf_mode_removal(client, fake):
    _seed_trust_accounts(fake)
    fake.seed("dossiers/dos1", {
        "id": "dos1",
        "file_number": "2026-030",
        "title": "Dossier Déboursé",
        "status": "actif",
        "client_ids": ["c1"],
        "clients": [{"id": "c1", "name": "Mme Client"}],
    })
    fake.seed("trust_transactions/tx1", {
        "id": "tx1",
        "account_id": "ta1",
        "dossier_id": "dos1",
        "client_id": "c1",
        "client_name": "Mme Client",
        "dossier_file_number": "2026-030",
        "date": datetime(2026, 11, 5, tzinfo=UTC),
        "direction": "déboursé",
        "amount": 15000,
        "counterparty": "Huissier",
        "purpose": "débours_dossier",
        "method": "virement",
        "reference": "V-1",
        "status": "compensée",
        "sequence": 1,
        "balance_after_account": 285000,
        "balance_after_client": 0,
    })

    # Web table + Carte-client show « Déboursé », never « Crédit » column header
    journal_html = client.get("/fideicommis/?account_id=ta1").get_data(as_text=True)
    assert ">Déboursé<" in journal_html
    assert ">Crédit<" not in journal_html

    carte_html = client.get("/fideicommis/carte/dos1/c1").get_data(as_text=True)
    assert ">Déboursé<" in carte_html
    assert ">Crédit<" not in carte_html

    # CSV retains Mode du retrait and uses Déboursé
    csv_text = client.get("/fideicommis/export/csv?account_id=ta1").get_data(as_text=True)
    header = next(csv.reader(io.StringIO(csv_text.lstrip("\ufeff"))))
    assert "Déboursé" in header
    assert "Crédit" not in header
    assert "Mode du retrait" in header

    # Carte-client PDF omits mode while Carte CSV retains mode
    assert [k for k, _, _ in trust_routes._PDF_COLUMNS] == [
        k for k, _ in trust_model.BARREAU_COLUMNS if k != "mode"
    ]
    assert "mode" in [k for k, _ in trust_routes._CSV_COLUMNS]
    pdf_resp = client.get("/fideicommis/carte/dos1/c1/export/pdf")
    assert pdf_resp.status_code == 200
    assert pdf_resp.data.startswith(b"%PDF")

    # Journal PDF builders omit Mode
    assert "mode" not in [c.key for c in trust_journal_pdf.COLUMNS]
    assert "Mode" not in [c.label for c in trust_journal_pdf.COLUMNS]
    assert "mode" not in [c.key for c in admin_journal_pdf.COLUMNS]
    assert "Mode" not in [c.label for c in admin_journal_pdf.COLUMNS]


def test_1l_admin_combined_tous_les_comptes_view_csv_pdf_and_fail_closed(
    client, fake, monkeypatch,
):
    _seed_admin_accounts(fake)
    fake.seed("admin_transactions/at1", {
        "id": "at1",
        "account_id": "acc-ops",
        "date": datetime(2026, 11, 2, tzinfo=UTC),
        "kind": "dépense",
        "counterparty": "Hydro-Québec",
        "category": "loyer",
        "method": "virement",
        "amount": 11498,
        "amount_net": 10000,
        "tps": 500,
        "tvq": 998,
        "status": "compensée",
        "sequence": 1,
    })
    fake.seed("admin_transactions/at2", {
        "id": "at2",
        "account_id": "acc-card",
        "date": datetime(2026, 11, 8, tzinfo=UTC),
        "kind": "dépense",
        "counterparty": "Adobe",
        "category": "logiciels",
        "method": "carte",
        "amount": 5749,
        "amount_net": 5000,
        "tps": 250,
        "tvq": 499,
        "status": "compensée",
        "sequence": 1,
    })

    # Web view
    r = client.get("/administration/?account_id=tous")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert '<option value="tous" selected>Tous les comptes</option>' in body
    assert "Hydro-Québec" in body
    assert "Adobe" in body
    assert "Z Opérations Cabinet" in body
    assert "A Carte Corporative" in body
    assert ">Compte<" in body
    # Never display a mixed running Solde column in table header, and keep per-account headers separate
    assert '<th class="px-3 py-2 text-right font-medium text-gray-500 whitespace-nowrap">Solde</th>' not in body
    assert "Solde dû" in body

    # CSV export includes Compte and leaves Solde blank
    csv_resp = client.get("/administration/export/csv?account_id=tous")
    assert csv_resp.status_code == 200
    rows = list(csv.reader(io.StringIO(csv_resp.get_data(as_text=True).lstrip("\ufeff"))))
    assert "Compte" in rows[0]
    solde_idx = rows[0].index("Solde")
    assert len(rows) >= 3
    for data_row in rows[1:]:
        assert data_row[solde_idx] == ""

    # PDF export renders combined notice & per-account category tags with blank soldes
    captured_pdf_kwargs = {}
    real_pdf = admin_journal_pdf.build_admin_journal_pdf

    def _spy_pdf(*args, **kwargs):
        captured_pdf_kwargs["args"] = args
        captured_pdf_kwargs["kwargs"] = kwargs
        return real_pdf(*args, **kwargs)

    monkeypatch.setattr(admin_journal_pdf, "build_admin_journal_pdf", _spy_pdf)
    pdf_resp = client.get("/administration/export/pdf?account_id=tous")
    assert pdf_resp.status_code == 200
    assert pdf_resp.data.startswith(b"%PDF")
    assert all(r["solde"] is None for r in captured_pdf_kwargs["args"][0])
    assert any("Vue combinée" in n for n in captured_pdf_kwargs["kwargs"]["notices"])
    assert any("[" in r["categorie"] for r in captured_pdf_kwargs["args"][0])

    # Fail-closed when one account read fails under account_id=tous
    def _boom_register(*_a, **_k):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(admin_model, "list_register", _boom_register)
    err_web = client.get("/administration/?account_id=tous").get_data(as_text=True)
    assert "n&#39;ont pas pu être lues" in err_web or "n'ont pas pu être lues" in err_web
    assert client.get("/administration/export/csv?account_id=tous").status_code == 503
    err_pdf = client.get("/administration/export/pdf?account_id=tous")
    assert err_pdf.status_code == 200
    assert any("AVERTISSEMENT" in n for n in captured_pdf_kwargs["kwargs"]["notices"])


# ═══════════════════════════════════════════════════════════════════════════
# 1g — Client detail « Situation financière » card
# ═══════════════════════════════════════════════════════════════════════════


def test_1g_client_financial_situation_totals_isolation_and_fail_closed(
    client, fake, monkeypatch,
):
    fake.seed("parties/c1", {
        "id": "c1", "type": "individual", "contact_role": "client",
        "first_name": "Alice", "last_name": "Client",
    })
    fake.seed("parties/c2", {
        "id": "c2", "type": "individual", "contact_role": "client",
        "first_name": "Bob", "last_name": "Coclient",
    })
    fake.seed("parties/t1", {
        "id": "t1", "type": "individual", "contact_role": "témoin",
        "first_name": "Témoin", "last_name": "Neutre",
    })
    fake.seed("parties/t2", {
        "id": "t2", "type": "individual", "contact_role": "fournisseur",
        "first_name": "Intervenant", "last_name": "Mixte",
    })

    # d1: sole client c1 (500,00 $ in trust); d2: co-clients c1 + c2 (120,00 $ for c1, 999,99 $ for c2!)
    fake.seed("dossiers/d1", {
        "id": "d1", "file_number": "2026-001", "title": "Dos Un",
        "status": "actif", "client_ids": ["c1"],
        "trust_balance_by_client": {"c1": 50000},
    })
    fake.seed("dossiers/d2", {
        "id": "d2", "file_number": "2026-002", "title": "Dos Deux",
        "status": "actif", "client_ids": ["c1", "c2", "t2"],
        "trust_balance_by_client": {"c1": 12000, "c2": 99999, "t2": 0},
    })
    # Other client's dossier must not bleed into c1
    fake.seed("dossiers/d3", {
        "id": "d3", "file_number": "2026-003", "title": "Dos Autre",
        "status": "actif", "client_ids": ["c2"],
        "trust_balance_by_client": {"c2": 88888},
    })

    # Invoices: revenue + unpaid + legacy blank-client dossier invoice + draft excluded
    fake.seed("invoices/inv1", {
        "id": "inv1", "invoice_number": "2026-F001", "status": "envoyée",
        "dossier_id": "d1", "client_id": "c1",
        "subtotal_fees": 100000, "subtotal_expenses": 5000,
        "amount_due": 120724, "amount_paid": 20724, "total": 120724, "retainer_applied": 0,
    })
    fake.seed("invoices/inv2", {
        "id": "inv2", "invoice_number": "2026-F002", "status": "payée",
        "dossier_id": "d2", "client_id": "",  # legacy blank client_id on client dossier d2
        "subtotal_fees": 50000, "subtotal_expenses": 0,
        "amount_due": 57488, "amount_paid": 57488, "total": 57488, "retainer_applied": 0,
    })
    fake.seed("invoices/inv3", {
        "id": "inv3", "invoice_number": "2026-F003", "status": "brouillon",
        "dossier_id": "d1", "client_id": "c1",
        "subtotal_fees": 77700, "subtotal_expenses": 0,
        "amount_due": 77700, "amount_paid": 0, "total": 77700, "retainer_applied": 0,
    })
    fake.seed("invoices/inv4", {
        "id": "inv4", "invoice_number": "2026-F004", "status": "en_retard",
        "dossier_id": "d2", "client_id": "c2",  # co-client's own invoice: excluded for c1
        "subtotal_fees": 40000, "subtotal_expenses": 0,
        "amount_due": 45990, "amount_paid": 0, "total": 45990, "retainer_applied": 0,
    })

    # Unbilled WIP: billable unbilled (2.0h = 500,00 $) + unbilled expense (35,00 $)
    fake.seed("timeentries/te1", {
        "id": "te1", "dossier_id": "d1", "billable": True, "invoiced": False,
        "hours": 2.0, "amount": 50000,
    })
    fake.seed("timeentries/te2", {
        "id": "te2", "dossier_id": "d1", "billable": False, "invoiced": False,
        "hours": 1.0, "amount": 25000,
    })
    fake.seed("timeentries/te3", {
        "id": "te3", "dossier_id": "d2", "billable": True, "invoiced": True,
        "hours": 3.0, "amount": 75000,
    })
    fake.seed("expenses/ex1", {
        "id": "ex1", "dossier_id": "d2", "invoiced": False,
        "amount": 3500, "date": datetime(2026, 11, 5, tzinfo=UTC),
    })
    fake.seed("expenses/ex2", {
        "id": "ex2", "dossier_id": "d1", "invoiced": True,
        "amount": 9000, "date": datetime(2026, 11, 4, tzinfo=UTC),
    })

    body = client.get("/parties/c1").get_data(as_text=True)
    assert "Situation financière" in body
    # Trust: 500,00 $ + 120,00 $ = 620,00 $ (never 999,99 $)
    assert "620,00\xa0$" in body
    assert "999,99" not in body
    assert "/fideicommis/carte/d1/c1" in body
    assert "/fideicommis/carte/d2/c1" in body
    # Billed fees: 1000 + 500 = 1500,00 $; unpaid: 1000,00 $
    assert "1\xa0500,00\xa0$" in body
    assert "1\xa0000,00\xa0$" in body
    # WIP: 500 + 35 = 535,00 $, 2.0 h, 35,00 $ déboursés
    assert "535,00\xa0$" in body
    assert "2.0 h" in body
    assert "35,00\xa0$" in body

    # Pure witness hides financial situation; supplier who is a dossier client shows it
    assert "Situation financière" not in client.get("/parties/t1").get_data(as_text=True)
    assert "Situation financière" in client.get("/parties/t2").get_data(as_text=True)

    # List page stays fast: zero financial queries
    monkeypatch.setattr(
        invoice_model, "list_client_invoices_strict",
        lambda *a, **k: pytest.fail("parties list must not query client invoices"),
    )
    monkeypatch.setattr(
        time_entry_model, "get_unbilled_time_entries_strict",
        lambda *a, **k: pytest.fail("parties list must not query WIP time"),
    )
    monkeypatch.setattr(
        expense_model, "get_unbilled_expenses_strict",
        lambda *a, **k: pytest.fail("parties list must not query WIP expenses"),
    )
    assert client.get("/parties/").status_code == 200

    # Fail-closed outages show « indisponible »
    monkeypatch.setattr(
        parties_routes, "list_dossiers_for_partie_strict",
        lambda _pid: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    err_body = client.get("/parties/c1").get_data(as_text=True)
    assert "Situation financière" in err_body
    assert "indisponible" in err_body


# ═══════════════════════════════════════════════════════════════════════════
# 1b — Dashboard 4 financial pulse tiles
# ═══════════════════════════════════════════════════════════════════════════


def test_1b_billed_totals_montreal_boundary_and_dashboard_tiles(
    client, fake, monkeypatch,
):
    from datetime import date

    # Today frozen at 2026-05-15 (Q2 / May)
    monkeypatch.setattr(deadlines, "today_mtl", lambda: date(2026, 5, 15))
    _seed_admin_accounts(fake)  # ops=450000, card=-25000 (card must NEVER be included)
    _seed_trust_accounts(fake)  # total=300000 + 100000 = 400000

    # Invoice at Q1 end (2026-03-31 midnight UTC): excluded from BOTH Month May and Quarter Q2
    fake.seed("invoices/inv-mar", {
        "id": "inv-mar", "invoice_number": "2026-F009", "status": "payée",
        "date": datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
        "subtotal_fees": 50000, "subtotal_expenses": 0,
        "total": 57488, "amount_due": 57488, "amount_paid": 57488, "retainer_applied": 0,
    })
    # Invoice in April (Q2, NOT May): before-tax = 90000 + 5000 = 95000
    fake.seed("invoices/inv-apr", {
        "id": "inv-apr", "invoice_number": "2026-F010", "status": "payée",
        "date": datetime(2026, 4, 5, 0, 0, tzinfo=UTC),
        "subtotal_fees": 90000, "subtotal_expenses": 5000,
        "total": 109226, "amount_due": 109226, "amount_paid": 109226, "retainer_applied": 0,
    })
    # Invoice in May (May & Q2): before-tax = 200000 + 10000 = 210000, unpaid = 150000
    fake.seed("invoices/inv-may", {
        "id": "inv-may", "invoice_number": "2026-F011", "status": "envoyée",
        "date": datetime(2026, 5, 10, 0, 0, tzinfo=UTC),
        "subtotal_fees": 200000, "subtotal_expenses": 10000,
        "total": 241448, "amount_due": 241448, "amount_paid": 91448, "retainer_applied": 0,
    })
    # Draft in May must be excluded
    fake.seed("invoices/inv-draft", {
        "id": "inv-draft", "invoice_number": "2026-F012", "status": "brouillon",
        "date": datetime(2026, 5, 11, 0, 0, tzinfo=UTC),
        "subtotal_fees": 999999, "subtotal_expenses": 0,
        "total": 999999, "amount_due": 999999, "amount_paid": 0, "retainer_applied": 0,
    })

    # Billable unbilled time (3.5h, 700,00 $) + unbilled expense (45,00 $) -> WIP 745,00 $
    fake.seed("timeentries/t-wip", {
        "id": "t-wip", "dossier_id": "d1", "billable": True, "invoiced": False,
        "hours": 3.5, "amount": 70000,
    })
    fake.seed("expenses/e-wip", {
        "id": "e-wip", "dossier_id": "d1", "invoiced": False,
        "amount": 4500, "date": datetime(2026, 5, 10, tzinfo=UTC),
    })

    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    # Tile 1: Month == 2 100,00 $ (May only), Quarter == 3 050,00 $ (April + May; March excluded)
    assert "Facturé ce mois" in body
    assert "2\xa0100,00\xa0$" in body
    assert "Ce trimestre : 3\xa0050,00\xa0$" in body

    # Tile 2: Total WIP = 700 + 45 = 745,00 $
    assert "Travail non facturé" in body
    assert "745,00\xa0$" in body
    assert "3.5 h" in body
    assert "45,00\xa0$" in body

    # Tile 3: Unpaid invoices = 1500,00 $
    assert "Factures impayées" in body
    assert "1\xa0500,00\xa0$" in body

    # Tile 4: Operations = 4 500,00 $ (no credit card -250 $!), Trust = 4 000,00 $
    assert "Opérations &amp; Fidéicommis" in body
    assert "4\xa0500,00\xa0$" in body
    assert "4\xa0000,00\xa0$" in body
    assert "4\xa0250,00" not in body

    # Fail-closed check: expense failure turns ONLY Tile 2 unavailable; admin failure ONLY Solde opérations
    monkeypatch.setattr(
        expense_model, "get_unbilled_totals_strict",
        lambda: (_ for _ in ()).throw(RuntimeError("expense boom")),
    )
    monkeypatch.setattr(
        admin_model, "get_firm_admin_snapshot",
        lambda: (_ for _ in ()).throw(RuntimeError("admin boom")),
    )
    degraded = client.get("/").get_data(as_text=True)
    # Tiles 1 & 3 + Trust remain populated while WIP & Ops say indisponible
    assert "2\xa0100,00\xa0$" in degraded
    assert "4\xa0000,00\xa0$" in degraded
    assert degraded.count("indisponible") == 2


# ═══════════════════════════════════════════════════════════════════════════
# Review regression guards (bugs fixed after Step 2 audit)
# ═══════════════════════════════════════════════════════════════════════════


def test_review_combined_export_links_propagate_account_id_tous(client, fake):
    import html as html_lib
    from urllib.parse import parse_qs, urlsplit

    _seed_admin_accounts(fake)
    fake.seed("admin_transactions/tx-ops", {
        "id": "tx-ops", "account_id": "acc-ops", "sequence": 1,
        "date": datetime(2026, 5, 10, tzinfo=UTC),
        "kind": "recette_autre", "direction": "recette", "amount": 25000,
        "method": "virement", "counterparty": "Client Ops",
        "status": "compensée", "category": None,
        "net_amount": None, "gst_amount": None, "qst_amount": None,
    })
    fake.seed("admin_transactions/tx-card", {
        "id": "tx-card", "account_id": "acc-card", "sequence": 1,
        "date": datetime(2026, 5, 11, tzinfo=UTC),
        "kind": "depense", "direction": "déboursé", "amount": 8900,
        "method": "carte", "counterparty": "Librairie Card",
        "status": "en_circulation", "category": "fournitures",
        "net_amount": None, "gst_amount": None, "qst_amount": None,
    })

    for headers in ({}, {"HX-Request": "true"}):
        resp = client.get("/administration/?account_id=tous", headers=headers)
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        m = re.search(r'<div id="admin-export"[^>]*>(.*?)</div>', body, re.DOTALL)
        assert m, "Expected #admin-export block"
        hrefs = [html_lib.unescape(h) for h in re.findall(r'href="([^"]+)"', m.group(1))]
        assert len(hrefs) == 2
        for href in hrefs:
            qs = parse_qs(urlsplit(href).query)
            assert qs.get("account_id") == ["tous"], href

        csv_resp = client.get(hrefs[0])
        assert csv_resp.status_code == 200
        csv_text = csv_resp.get_data(as_text=True)
        assert "Client Ops" in csv_text
        assert "Librairie Card" in csv_text

        pdf_resp = client.get(hrefs[1])
        assert pdf_resp.status_code == 200
        assert pdf_resp.data[:4] == b"%PDF"


def test_review_admin_combined_read_error_suppresses_empty_white_box(
    client, fake, monkeypatch,
):
    _seed_admin_accounts(fake)
    monkeypatch.setattr(
        admin_model, "list_register",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("firestore down")),
    )
    resp = client.get("/administration/?account_id=tous")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "n'ont pas pu être lues" in body
    assert "bg-white rounded-xl border border-gray-200 p-8" not in body


def test_review_closed_account_journal_omits_account_id_on_new_entry_links(
    client, fake,
):
    fake.seed("admin_accounts/acc-closed", {
        "id": "acc-closed", "name": "Ancien compte",
        "account_type": "opérations", "status": "fermé",
        "institution": "BNC", "account_number_last4": "0001", "ledger_balance": 0,
    })
    fake.seed("trust_accounts/ta-closed", {
        "id": "ta-closed", "name": "Ancien fidéicommis",
        "account_type": "général", "status": "fermé",
        "institution": "BNC", "account_number_last4": "0002",
        "book_balance": 0, "bank_balance": 0,
    })
    for url, link_prefix in (
        ("/administration/?account_id=acc-closed", "/administration/nouvelle"),
        ("/fideicommis/?account_id=ta-closed", "/fideicommis/nouvelle"),
    ):
        for headers in ({}, {"HX-Request": "true"}):
            body = client.get(url, headers=headers).get_data(as_text=True)
            assert f'href="{link_prefix}"' in body
            assert f'href="{link_prefix}?account_id=' not in body


def test_review_trust_400_missing_method_aligns_fee_payee_with_virement(client, fake):
    _seed_trust_accounts(fake)
    fake.seed("settings/cabinet", {
        "organisation": "Cabinet Lavoie Inc.",
        "nom": "Me Jason Lavoie",
    })
    resp = client.post("/fideicommis/", data={
        "account_id": "ta1",
        "purpose": "virement_honoraires",
        "direction": "déboursé",
        "amount": "",  # triggers 400 validation error
        "date": "2026-05-10",
    })
    assert resp.status_code == 400
    body = resp.get_data(as_text=True)
    assert _has_selected_option(body, "virement")
    assert _has_selected_option(body, "Cabinet Lavoie Inc.")
    assert not _has_selected_option(body, "Me Jason Lavoie")


def test_review_trust_attach_receipt_and_recu_fail_closed(client, fake, monkeypatch):
    _seed_trust_accounts(fake)
    fake.seed("trust_transactions/tx-1", {
        "id": "tx-1", "account_id": "ta1", "sequence": 1,
        "receipt_storage_path": "users/u1/fideicommis/tx-1/abc/recu.pdf",
        "receipt_filename": "recu.pdf", "receipt_file_type": "application/pdf",
        "receipt_file_size": 100,
    })
    monkeypatch.setattr(
        trust_model, "get_transaction_strict",
        lambda _tx_id: (_ for _ in ()).throw(RuntimeError("firestore outage")),
    )
    updated, errs = trust_model.attach_receipt(
        "tx-1", "users/u1/fideicommis/tx-1/x/recu.pdf", "recu.pdf", "application/pdf", 100,
    )
    assert updated is None
    assert errs == ["Erreur lors de la lecture de l'écriture. Veuillez réessayer."]

    view_resp = client.get("/fideicommis/tx-1/recu")
    assert view_resp.status_code == 503


def test_review_staged_receipt_name_falls_back_to_object_basename(
    client, fake, monkeypatch,
):
    import uuid
    import routes.admin_ledger as admin_routes_mod

    _seed_admin_accounts(fake)
    monkeypatch.setattr(admin_routes_mod.storage_identity, "request_uid", lambda: "u1")
    monkeypatch.setattr(
        admin_routes_mod, "_copy_and_attach_staged_receipt",
        lambda tx_id, objet, nom, dest_path, user_id: (
            fake.seed(f"admin_transactions/{tx_id}", {
                **(fake.peek_collection("admin_transactions")[tx_id]),
                "receipt_filename": nom,
                "receipt_storage_path": dest_path,
            }) or fake.peek_collection("admin_transactions")[tx_id],
            None,
            200,
        ),
    )
    uid4 = str(uuid.uuid4())
    resp = client.post("/administration/", data={
        "account_id": "acc-ops",
        "kind": "recette_autre",
        "direction": "recette",
        "amount": "50,00",
        "method": "virement",
        "date": "2026-05-10",
        "counterparty": "Test",
        "staged_receipt_objet": f"staging/u1/{uid4}/piece.pdf",
        "staged_receipt_name": "",
    })
    assert resp.status_code == 302
    txs = list(fake.peek_collection("admin_transactions").values())
    assert len(txs) == 1
    assert txs[0]["receipt_filename"] == "piece.pdf"


def test_review_client_financial_situation_malformed_trust_map_fails_closed(
    client, fake,
):
    fake.seed("parties/p-bad-trust", {
        "id": "p-bad-trust", "type": "individual",
        "first_name": "Claire", "last_name": "Corrupt",
        "contact_role": "client",
    })
    fake.seed("dossiers/d-bad", {
        "id": "d-bad", "file_number": "2026-888", "title": "Dossier Corrupt",
        "client_ids": ["p-bad-trust"],
        "clients": [{"id": "p-bad-trust", "name": "Claire Corrupt"}],
        "trust_balance_by_client": {"p-bad-trust": None},
    })
    resp = client.get("/parties/p-bad-trust")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Situation financière" in body
    assert "indisponible" in body


def test_review_dashboard_none_balances_and_mcp_none_supplier_fields(
    client, fake, monkeypatch,
):
    from datetime import date

    monkeypatch.setattr(deadlines, "today_mtl", lambda: date(2026, 5, 15))
    fake.seed("admin_accounts/ops-none", {
        "id": "ops-none", "name": "Opérations", "account_type": "opérations",
        "status": "actif", "institution": "BNC", "account_number_last4": "1111",
        "ledger_balance": None,
    })
    monkeypatch.setattr(
        trust_model, "get_firm_trust_snapshot",
        lambda: {"total_held_cents": None, "reconciliation_overdue": False},
    )
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.get_data(as_text=True).count("indisponible") == 2

    fake.seed("parties/sup-none", {
        "id": "sup-none", "type": "organization",
        "organization": "Fournisseur Null", "contact_role": "fournisseur",
        "numero_tps": None, "numero_tvq": None,
        "categorie_depense_defaut": None, "numero_compte_fournisseur": None,
    })
    res = mcp_handlers.get_partie({"partie_id": "sup-none"})["partie"]
    for field in partie_model.SUPPLIER_RECORD_FIELDS:
        assert res[field] == ""
        assert isinstance(res[field], str)
