"""End-to-end verification of Phase 2 (Lot B) across real models, routes, and templates.

Covers every item in Phase 2 (Lot B) on the shared fake Firestore (``tests/_fake_firestore.py``):
- 1a: Iconography pass (>=44 px icon-only ms() buttons, French aria-label/title,
      arrow_back navigation, domain action text labels kept, time_expenses '+' menu)
- 1e: Forum-aware form field hiding (Tribunal / Juge / courthouse chips hidden & disabled
      when forum == extrajudiciaire; cleared server-side), tribunal prefill from
      dossier.tribunal, dynamic detail heading (« Lieu et tribunal » vs « Lieu »),
      connected files via direct-to-GCS resumable upload & link/unlink (dossier events only,
      default procès_verbal_audience & Procès-verbaux folder on judicial hearings),
      connected Notes & Tasks (all events, pre-linked creation inheriting dossier_id,
      link/unlink with partial transactional writes & DAV CTag bumps, two-way links,
      strict read error handling, zero spurious note revisions).
"""

import os
import pathlib
import re
import sys
import uuid
from datetime import datetime, timezone
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
        folder as folder_model,
        hearing as hearing_model,
        invoice as invoice_model,
        note as note_model,
        partie as partie_model,
        protocol as protocol_model,
        task as task_model,
        time_entry as time_entry_model,
        transcription_reunion as tr_model,
        trust as trust_model,
    )
    import dav.sync as dav_sync
    _PRELOADED_MODELS = (
        admin_model, document_model, dossier_model, expense_model,
        folder_model, hearing_model, invoice_model, note_model,
        partie_model, protocol_model, task_model, time_entry_model,
        tr_model, trust_model, dav_sync,
    )
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
    import routes.reception as reception_routes
    import routes.tasks as tasks_routes
    import routes.time_expenses as time_expenses_routes
    import routes.trust as trust_routes

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
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
        reception_routes.reception_bp,
    ):
        app.register_blueprint(bp)

    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _seed_dossier(fake, dossier_id="d-1", file_number="2026-001", title="Dupont c. Tremblay", tribunal="Cour supérieure"):
    fake.seed(f"dossiers/{dossier_id}", {
        "id": dossier_id,
        "file_number": file_number,
        "title": title,
        "status": "actif",
        "tribunal": tribunal,
        "created_at": datetime(2026, 3, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 3, 1, tzinfo=UTC),
    })


def _seed_hearing(
    fake,
    hearing_id="h-1",
    *,
    dossier_id="d-1",
    dossier_file_number="2026-001",
    dossier_title="Dupont c. Tremblay",
    hearing_type="audience",
    title="Audience sur requête",
    location="Palais de justice de Montréal",
    court="Cour supérieure",
    judge="Hon. Gagnon",
    etag="etag-h1",
):
    fake.seed(f"hearings/{hearing_id}", {
        "id": hearing_id,
        "dossier_id": dossier_id,
        "dossier_file_number": dossier_file_number,
        "dossier_title": dossier_title,
        "hearing_type": hearing_type,
        "title": title,
        "start_datetime": datetime(2026, 11, 20, 14, 0, tzinfo=UTC),
        "end_datetime": datetime(2026, 11, 20, 15, 0, tzinfo=UTC),
        "all_day": False,
        "location": location,
        "court": court,
        "judge": judge,
        "modalite": "présentiel",
        "status": "confirmée",
        "reminder_minutes": 1440,
        "etag": etag,
        "created_at": datetime(2026, 3, 2, tzinfo=UTC),
        "updated_at": datetime(2026, 3, 2, tzinfo=UTC),
    })


# ── 1a: Iconography pass & >=44 px tap targets ────────────────────────────────


def test_1a_no_literal_larr_back_arrows_or_small_form_back_links():
    templates_dir = _ATHENA / "templates"
    for html_file in templates_dir.rglob("*.html"):
        text = html_file.read_text(encoding="utf-8")
        assert "&larr;" not in text, f"&larr; left in {html_file}"
        rel = html_file.relative_to(templates_dir).as_posix()
        # Only pagination / month navigation keep chevron_left; back arrows migrated to arrow_back.
        if rel not in (
            "components/pagination.html",
            "comptabilite/radiations.html",
            "finances/etats.html",
            "hearings/_month_grid.html",
        ):
            assert "ms('chevron_left'" not in text, f"chevron_left left in {rel}"


def test_1a_rendered_views_use_44px_icon_actions_and_keep_domain_labels(client, fake, monkeypatch):
    monkeypatch.setattr(documents_routes, "get_signed_url", lambda *a, **kw: None)
    monkeypatch.setattr("models.portail_invitation._pdb", lambda: fake)
    _seed_dossier(fake)
    _seed_hearing(fake)
    fake.seed("parties/p1", {
        "id": "p1", "name": "Jean Dupont", "party_type": "personne_physique",
        "contact_role": "client", "created_at": datetime(2026, 3, 1, tzinfo=UTC),
    })
    fake.seed("notes/n1", {
        "id": "n1", "dossier_id": "d-1", "dossier_file_number": "2026-001",
        "title": "Note test", "content": "Contenu", "category": "autre",
        "pinned": False, "etag": "e1", "created_at": datetime(2026, 3, 1, tzinfo=UTC),
    })
    fake.seed("tasks/t1", {
        "id": "t1", "dossier_id": "d-1", "dossier_file_number": "2026-001",
        "title": "Tâche test", "status": "à_faire", "priority": "normale",
        "category": "autre", "etag": "e1", "created_at": datetime(2026, 3, 1, tzinfo=UTC),
    })
    fake.seed("documents/doc1", {
        "id": "doc1", "dossier_id": "d-1", "dossier_file_number": "2026-001",
        "filename": "acte.pdf", "display_name": "Acte", "file_type": "application/pdf",
        "file_size": 1024, "category": "procédure", "etag": "e1",
        "created_at": datetime(2026, 3, 1, tzinfo=UTC),
    })
    fake.seed("invoices/inv1", {
        "id": "inv1", "dossier_id": "d-1", "dossier_file_number": "2026-001",
        "invoice_number": "2026-001-01", "status": "brouillon",
        "subtotal": 10000, "gst_amount": 500, "qst_amount": 998, "total": 11498,
        "amount_paid": 0, "balance": 11498,
        "total_cents": 11498, "paid_cents": 0, "balance_cents": 11498,
        "etag": "e1", "created_at": datetime(2026, 3, 1, tzinfo=UTC),
    })
    fake.seed("admin_accounts/aa1", {
        "id": "aa1", "name": "Opérations", "account_type": "opérations", "status": "actif",
    })
    fake.seed("trust_accounts/ta1", {
        "id": "ta1", "name": "Général", "account_type": "général", "status": "actif",
        "book_balance": 10000, "bank_balance": 10000,
    })

    for path in (
        "/dossiers/d-1",
        "/parties/p1",
        "/notes/n1",
        "/taches/t1",
        "/documents/doc1",
        "/audiences/h-1",
        "/factures/inv1",
        "/administration/comptes/",
        "/administration/comptes/aa1",
        "/fideicommis/comptes/",
        "/fideicommis/comptes/ta1",
        "/comptabilite/",
        "/gabarits/",
        "/reception/",
        "/finances/",
    ):
        resp = client.get(path)
        assert resp.status_code == 200, path
        html = resp.get_data(as_text=True)
        if path in (
            "/dossiers/d-1", "/parties/p1", "/notes/n1", "/taches/t1",
            "/documents/doc1", "/audiences/h-1", "/factures/inv1",
            "/administration/comptes/", "/administration/comptes/aa1",
            "/fideicommis/comptes/", "/fideicommis/comptes/ta1",
        ):
            assert "arrow_back" in html, f"missing arrow_back on {path}"
        assert "min-h-[44px]" in html and "min-w-[44px]" in html, f"missing >=44px target on {path}"

    # Desktop list headers expose >=44px '+' / upload / export controls.
    for list_path in (
        "/dossiers/", "/parties/", "/notes/", "/taches/",
        "/documents/", "/audiences/", "/factures/",
    ):
        resp = client.get(list_path)
        assert resp.status_code == 200, list_path
        lhtml = resp.get_data(as_text=True)
        assert "min-h-[44px]" in lhtml and "min-w-[44px]" in lhtml, list_path

    # High-stakes invoice status actions keep clear French text labels.
    inv_html = client.get("/factures/inv1").get_data(as_text=True)
    assert "Marquer comme envoyée" in inv_html

    # Time & expenses list uses the '+' Alpine dropdown with Entrée de temps / Dépense.
    te_html = client.get("/temps/").get_data(as_text=True)
    assert "addOpen" in te_html
    assert "Entrée de temps" in te_html
    assert "Dépense" in te_html


# ── 1e: Forum hiding, tribunal prefill & dynamic detail heading ────────────────


def test_1e_hearing_new_prefills_court_from_dossier_tribunal(client, fake):
    _seed_dossier(fake, "d-trib", tribunal="Cour du Québec")
    resp = client.get("/audiences/new?dossier_id=d-trib")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert re.search(r'<option value="Cour du Québec"\s+selected>', html)


def test_1e_forum_hides_court_judge_and_chips_and_strips_on_submit(client, fake):
    _seed_dossier(fake)

    # New form starts on judiciaire -> court & judge NOT style="display: none"
    form_html = client.get("/audiences/new?dossier_id=d-1").get_data(as_text=True)
    assert "x-show=\"forum === 'judiciaire'\"" in form_html
    assert ":disabled=\"forum !== 'judiciaire'\"" in form_html

    # Extrajudicial event edit form has SSR display: none on courthouse chips and Court+Judge row.
    _seed_hearing(fake, "h-extra", hearing_type="rencontre", court="", judge="", location="Cabinet")
    edit_html = client.get("/audiences/h-extra/edit").get_data(as_text=True)
    assert 'x-show="forum === \'judiciaire\'"\n           style="display: none"' in edit_html
    assert 'x-show="forum === \'judiciaire\'"\n         style="display: none"' in edit_html

    # Submitting a crafted POST with extrajudicial type + court/judge ignores & clears both.
    post = client.post(
        "/audiences/",
        data={
            "csrf_token": "tok",
            "dossier_id": "d-1",
            "title": "Rencontre stratégique",
            "hearing_type": "rencontre",
            "start_date": "2026-12-01",
            "start_time": "10:00",
            "end_time": "11:00",
            "location": "Cabinet",
            "court": "Cour supérieure",
            "judge": "Hon. Stale",
            "status": "confirmée",
            "modalite": "présentiel",
            "reminder_minutes": "1440",
        },
    )
    assert post.status_code == 302
    created = hearing_model.list_hearings(dossier_id="d-1")
    extra_rows = [h for h in created if h.get("title") == "Rencontre stratégique"]
    assert len(extra_rows) == 1
    assert extra_rows[0]["court"] == ""
    assert extra_rows[0]["judge"] == ""


def test_1e_detail_section_heading_depends_on_forum(client, fake):
    _seed_dossier(fake)
    _seed_hearing(fake, "h-jud", hearing_type="audience", court="Cour supérieure", judge="Hon. X")
    _seed_hearing(fake, "h-ext", hearing_type="consultation", court="Cour supérieure", judge="Hon. Y", location="Bureau")

    jud_html = client.get("/audiences/h-jud").get_data(as_text=True)
    assert "Lieu et tribunal" in jud_html
    assert "Hon. X" in jud_html

    ext_html = client.get("/audiences/h-ext").get_data(as_text=True)
    assert "Lieu et tribunal" not in ext_html
    assert ">Lieu<" in ext_html
    assert "Hon. Y" not in ext_html


# ── 1e: Connected files (dossier events only) ─────────────────────────────────


def test_1e_judicial_hearing_upload_defaults_and_standalone_omits_files_section(client, fake):
    _seed_dossier(fake)
    pv_folder_id = folder_model.node_folder_id("d-1", "proces_verbaux")
    proc_folder_id = folder_model.node_folder_id("d-1", "procedures")
    fake.seed(f"folders/{proc_folder_id}", {"id": proc_folder_id, "dossier_id": "d-1", "name": "Procédures", "parent_id": None})
    fake.seed(f"folders/{pv_folder_id}", {"id": pv_folder_id, "dossier_id": "d-1", "name": "Procès-verbaux", "parent_id": proc_folder_id})

    _seed_hearing(fake, "h-jud", hearing_type="audience", dossier_id="d-1")
    _seed_hearing(fake, "h-ext-d", hearing_type="rencontre", dossier_id="d-1", court="", judge="")
    _seed_hearing(fake, "h-solo", hearing_type="rencontre", dossier_id="", dossier_file_number="", dossier_title="", court="", judge="")

    jud_html = client.get("/audiences/h-jud").get_data(as_text=True)
    assert "Documents liés" in jud_html
    assert "hearing-upload-config" in jud_html
    assert re.search(r'<option value="procès_verbal_audience"\s+selected>', jud_html)
    assert re.search(rf'<option value="{re.escape(pv_folder_id)}"\s+selected>', jud_html)

    ext_d_html = client.get("/audiences/h-ext-d").get_data(as_text=True)
    assert re.search(r'<option value="autre"\s+selected>', ext_d_html)
    assert re.search(rf'<option value="{re.escape(pv_folder_id)}"\s+selected>', ext_d_html) is None

    solo_html = client.get("/audiences/h-solo").get_data(as_text=True)
    assert "Documents liés" not in solo_html
    assert "hearing-upload-config" not in solo_html
    assert "Notes liées" in solo_html
    assert "Tâches liées" in solo_html


def test_1e_direct_upload_from_hearing_stamps_hearing_id_and_enforces_strict_checks(client, fake, monkeypatch):
    _seed_dossier(fake, "d-1")
    _seed_dossier(fake, "d-2", file_number="2026-002", title="Autre dossier")
    _seed_hearing(fake, "h-1", dossier_id="d-1")

    doc_uuid = str(uuid.uuid4())
    staging_path = f"staging/u1/{doc_uuid}/pv.pdf"
    fake_source_blob = mock.MagicMock()
    fake_source_blob.size = 200
    fake_source_blob.md5_hash = "abc"
    fake_source_blob.download_as_bytes.return_value = b"%PDF-1.4\nhello"
    fake_source_blob.create_resumable_upload_session.return_value = "https://gcs.example/resumable"

    fake_dest_blob = mock.MagicMock()
    fake_dest_blob.size = 200
    fake_dest_blob.md5_hash = "abc"
    fake_dest_blob.generation = 1
    fake_dest_blob.rewrite.return_value = (None, 200, 200)

    def _blob(path):
        if path.startswith("staging/"):
            return fake_source_blob
        return fake_dest_blob

    fake_bucket = mock.MagicMock()
    fake_bucket.blob.side_effect = _blob
    monkeypatch.setattr(documents_routes.storage, "bucket", lambda: fake_bucket)
    monkeypatch.setattr(document_model.storage, "bucket", lambda: fake_bucket)
    monkeypatch.setattr(documents_routes, "get_signed_url", lambda *a, **kw: None)

    # 1. Cross-dossier hearing rejected on session open (422)
    r_bad = client.post(
        "/documents/api/televersement",
        json={"name": "pv.pdf", "size": 200, "dossier_id": "d-2", "hearing_id": "h-1", "category": "procès_verbal_audience"},
    )
    assert r_bad.status_code == 422

    # 2. Strict read error on hearing answers 503 on session open and finalise
    with mock.patch("models.hearing.get_hearing_strict", side_effect=RuntimeError("boom")):
        r_503 = client.post(
            "/documents/api/televersement",
            json={"name": "pv.pdf", "size": 200, "dossier_id": "d-1", "hearing_id": "h-1", "category": "procès_verbal_audience"},
        )
        assert r_503.status_code == 503

        r_fin_503 = client.post(
            "/documents/api/finaliser",
            json={"objet": staging_path, "name": "pv.pdf", "dossier_id": "d-1", "hearing_id": "h-1", "category": "procès_verbal_audience"},
        )
        assert r_fin_503.status_code == 503
        assert not fake_source_blob.delete.called

    # 3. Valid open + finalise ingests document stamped with hearing_id
    r_open = client.post(
        "/documents/api/televersement",
        json={"name": "pv.pdf", "size": 200, "dossier_id": "d-1", "hearing_id": "h-1", "category": "procès_verbal_audience"},
    )
    assert r_open.status_code == 200
    open_json = r_open.get_json()
    assert open_json["url"] == "https://gcs.example/resumable"
    staging_path = open_json["objet"]

    r_fin = client.post(
        "/documents/api/finaliser",
        json={
            "objet": staging_path,
            "name": "pv.pdf",
            "dossier_id": "d-1",
            "hearing_id": "h-1",
            "category": "procès_verbal_audience",
            "display_name": "PV audience 20 nov",
        },
    )
    assert r_fin.status_code == 200
    doc_id = r_fin.get_json()["document_id"]
    stored = document_model.get_document_strict(doc_id)
    assert stored["hearing_id"] == "h-1"
    assert stored["category"] == "procès_verbal_audience"
    assert stored["display_name"] == "PV audience 20 nov"

    # Two-way navigation links between hearing detail & document detail
    h_html = client.get("/audiences/h-1").get_data(as_text=True)
    assert "PV audience 20 nov" in h_html
    assert f"/documents/{doc_id}" in h_html

    d_html = client.get(f"/documents/{doc_id}").get_data(as_text=True)
    assert "Événement lié" in d_html
    assert "/audiences/h-1" in d_html
    assert "Audience sur requête" in d_html


def test_1e_link_and_unlink_document_partial_transactional_and_guarded(client, fake):
    _seed_dossier(fake, "d-1")
    _seed_dossier(fake, "d-2", file_number="2026-002", title="Autre")
    _seed_hearing(fake, "h-1", dossier_id="d-1")
    fake.seed("documents/doc-d1", {
        "id": "doc-d1", "dossier_id": "d-1", "dossier_file_number": "2026-001",
        "filename": "memo.pdf", "display_name": "Mémo", "file_type": "application/pdf",
        "file_size": 500, "category": "autre", "notes_internes": "Confidentiel",
        "etag": "etag-doc1", "created_at": datetime(2026, 3, 1, tzinfo=UTC),
    })
    fake.seed("documents/doc-d2", {
        "id": "doc-d2", "dossier_id": "d-2", "dossier_file_number": "2026-002",
        "filename": "autre.pdf", "display_name": "Autre", "file_type": "application/pdf",
        "file_size": 500, "category": "autre", "etag": "etag-doc2",
        "created_at": datetime(2026, 3, 1, tzinfo=UTC),
    })

    # Link existing doc in same dossier -> succeeds without touching notes_internes
    r_link = client.post("/audiences/h-1/documents/lier", data={"csrf_token": "tok", "document_id": "doc-d1"})
    assert r_link.status_code == 302
    assert "succes=" in r_link.headers["Location"]
    updated = document_model.get_document_strict("doc-d1")
    assert updated["hearing_id"] == "h-1"
    assert updated["notes_internes"] == "Confidentiel"

    # Cross-dossier document rejected
    r_cross = client.post("/audiences/h-1/documents/lier", data={"csrf_token": "tok", "document_id": "doc-d2"})
    assert r_cross.status_code == 302
    assert "erreur=" in r_cross.headers["Location"]
    assert document_model.get_document_strict("doc-d2").get("hearing_id") is None

    # Stale expected_etag unlink refused
    r_stale = client.post(
        "/audiences/h-1/documents/doc-d1/delier",
        data={"csrf_token": "tok", "expected_etag": "stale-etag"},
    )
    assert r_stale.status_code == 302
    assert "erreur=" in r_stale.headers["Location"]
    assert document_model.get_document_strict("doc-d1")["hearing_id"] == "h-1"

    # Fresh expected_etag unlink clears hearing_id while keeping document intact
    r_unlink = client.post(
        "/audiences/h-1/documents/doc-d1/delier",
        data={"csrf_token": "tok", "expected_etag": updated["etag"]},
    )
    assert r_unlink.status_code == 302
    assert "succes=" in r_unlink.headers["Location"]
    after = document_model.get_document_strict("doc-d1")
    assert after is not None
    assert after["hearing_id"] is None


# ── 1e: Connected Notes & Tasks (all events, DAV CTags, two-way links) ────────


def test_1e_create_note_and_task_from_dossier_and_standalone_hearings(client, fake):
    _seed_dossier(fake, "d-1")
    _seed_dossier(fake, "d-2", file_number="2026-002", title="Autre")
    _seed_hearing(fake, "h-d1", dossier_id="d-1", title="Audience D1")
    _seed_hearing(fake, "h-solo", dossier_id="", dossier_file_number="", dossier_title="", hearing_type="consultation", title="Rencontre libre", court="", judge="")

    # 1. Note created from dossier hearing inherits dossier_id + bumps dossier CTag
    col_d1 = dav_sync.collection_for("d-1")
    ctag_before = dav_sync.get_ctag(col_d1)
    form_note = client.get("/notes/new?hearing_id=h-d1").get_data(as_text=True)
    assert 'name="hearing_id" value="h-d1"' in form_note
    assert "Audience D1" in form_note

    r_note = client.post(
        "/notes/",
        data={
            "csrf_token": "tok",
            "dossier_id": "d-1",
            "hearing_id": "h-d1",
            "title": "Notes d'audience",
            "content": "Résumé de l'audience.",
            "category": "vacation",
        },
    )
    assert r_note.status_code == 302
    assert dav_sync.get_ctag(col_d1) != ctag_before
    linked_notes = note_model.list_notes_for_hearing_strict("h-d1")
    assert len(linked_notes) == 1
    nid = linked_notes[0]["id"]
    assert "/audiences/h-d1" in client.get(f"/notes/{nid}").get_data(as_text=True)
    assert f"/notes/{nid}" in client.get("/audiences/h-d1").get_data(as_text=True)

    # 2. Task created from standalone hearing stays in Général, sets hearing_id, bumps 'general' CTag
    gen_ctag_before = dav_sync.get_ctag("general")
    form_task = client.get("/taches/new?hearing_id=h-solo").get_data(as_text=True)
    assert 'name="hearing_id" value="h-solo"' in form_task
    assert "Rencontre libre" in form_task

    r_task = client.post(
        "/taches/",
        data={
            "csrf_token": "tok",
            "dossier_id": "",
            "hearing_id": "h-solo",
            "title": "Préparer mémo",
            "priority": "haute",
            "status": "à_faire",
            "category": "suivi",
        },
    )
    assert r_task.status_code == 302
    assert dav_sync.get_ctag("general") != gen_ctag_before
    linked_tasks = task_model.list_tasks_for_hearing_strict("h-solo")
    assert len(linked_tasks) == 1
    tid = linked_tasks[0]["id"]
    assert linked_tasks[0]["dossier_id"] is None
    assert "/audiences/h-solo" in client.get(f"/taches/{tid}").get_data(as_text=True)
    assert f"/taches/{tid}" in client.get("/audiences/h-solo").get_data(as_text=True)

    # 3. Direct update on note without hearing_id form key preserves stored hearing_id
    r_edit = client.post(
        f"/notes/{nid}",
        data={
            "csrf_token": "tok",
            "etag": linked_notes[0]["etag"],
            "dossier_id": "d-1",
            "title": "Notes d'audience v2",
            "content": "Résumé mis à jour.",
            "category": "vacation",
        },
    )
    assert r_edit.status_code == 302
    assert note_model.get_note_strict(nid)["hearing_id"] == "h-d1"

    # 4. Cross-dossier POST attempt rejected on note/task create
    r_bad_note = client.post(
        "/notes/",
        data={
            "csrf_token": "tok",
            "dossier_id": "d-2",
            "hearing_id": "h-d1",
            "title": "Croisée",
            "content": "Refusée",
            "category": "autre",
        },
    )
    assert r_bad_note.status_code == 200
    assert "même dossier" in r_bad_note.get_data(as_text=True)


def test_1e_link_unlink_notes_and_tasks_bump_ctag_no_spurious_revision_and_fail_closed(client, fake):
    _seed_dossier(fake, "d-1")
    _seed_dossier(fake, "d-2", file_number="2026-002", title="Autre")
    _seed_hearing(fake, "h-1", dossier_id="d-1")
    _seed_hearing(fake, "h-solo", dossier_id="", dossier_file_number="", dossier_title="", hearing_type="consultation", court="", judge="")

    fake.seed("notes/n1", {
        "id": "n1", "dossier_id": "d-1", "dossier_file_number": "2026-001",
        "title": "Note existante", "content": "Corps", "category": "autre",
        "is_analyse": False, "etag": "en1", "created_at": datetime(2026, 3, 1, tzinfo=UTC),
    })
    fake.seed("notes/n-ana", {
        "id": "n-ana", "dossier_id": "d-1", "title": "Théorie de la cause",
        "content": "Analyse", "is_analyse": True, "etag": "ena",
        "created_at": datetime(2026, 3, 1, tzinfo=UTC),
    })
    fake.seed("tasks/t1", {
        "id": "t1", "dossier_id": "d-1", "dossier_file_number": "2026-001",
        "title": "Tâche existante", "status": "à_faire", "priority": "normale",
        "category": "autre", "etag": "et1", "created_at": datetime(2026, 3, 1, tzinfo=UTC),
    })
    fake.seed("tasks/t2", {
        "id": "t2", "dossier_id": "d-2", "dossier_file_number": "2026-002",
        "title": "Tâche autre dossier", "status": "à_faire", "priority": "normale",
        "category": "autre", "etag": "et2", "created_at": datetime(2026, 3, 1, tzinfo=UTC),
    })

    # Linking a note bumps d-1 CTag and writes NO revision snapshot
    col_d1 = dav_sync.collection_for("d-1")
    col_d2 = dav_sync.collection_for("d-2")
    c0 = dav_sync.get_ctag(col_d1)
    r_ln = client.post("/audiences/h-1/notes/lier", data={"csrf_token": "tok", "note_id": "n1"})
    assert r_ln.status_code == 302
    assert "succes=" in r_ln.headers["Location"]
    assert dav_sync.get_ctag(col_d1) != c0
    assert list(fake.collection("notes/n1/revisions").stream()) == []

    # Analyse note cannot be linked
    r_ana = client.post("/audiences/h-1/notes/lier", data={"csrf_token": "tok", "note_id": "n-ana"})
    assert r_ana.status_code == 302
    assert "erreur=" in r_ana.headers["Location"]
    assert note_model.get_note_strict("n-ana").get("hearing_id") is None

    # Stale expected_etag unlink refused cleanly
    r_un_stale = client.post(
        "/audiences/h-1/notes/n1/delier",
        data={"csrf_token": "tok", "expected_etag": "stale-etag"},
    )
    assert r_un_stale.status_code == 302
    assert "erreur=" in r_un_stale.headers["Location"]
    assert note_model.get_note_strict("n1")["hearing_id"] == "h-1"

    # Unlinking note bumps d-1 CTag
    c1 = dav_sync.get_ctag(col_d1)
    updated_n1 = note_model.get_note_strict("n1")
    r_un = client.post(
        "/audiences/h-1/notes/n1/delier",
        data={"csrf_token": "tok", "expected_etag": updated_n1["etag"]},
    )
    assert r_un.status_code == 302
    assert "succes=" in r_un.headers["Location"]
    assert dav_sync.get_ctag(col_d1) != c1
    assert note_model.get_note_strict("n1")["hearing_id"] is None

    # Cross-dossier task refused on dossier hearing, allowed on standalone hearing
    r_bad_t = client.post("/audiences/h-1/taches/lier", data={"csrf_token": "tok", "task_id": "t2"})
    assert r_bad_t.status_code == 302
    assert "erreur=" in r_bad_t.headers["Location"]

    c_d2 = dav_sync.get_ctag(col_d2)
    r_solo_t = client.post("/audiences/h-solo/taches/lier", data={"csrf_token": "tok", "task_id": "t2"})
    assert r_solo_t.status_code == 302
    assert "succes=" in r_solo_t.headers["Location"]
    assert dav_sync.get_ctag(col_d2) != c_d2
    assert task_model.get_task_strict("t2")["hearing_id"] == "h-solo"

    # Stale expected_etag unlink refused on task
    r_ut_stale = client.post(
        "/audiences/h-solo/taches/t2/delier",
        data={"csrf_token": "tok", "expected_etag": "stale-etag"},
    )
    assert r_ut_stale.status_code == 302
    assert "erreur=" in r_ut_stale.headers["Location"]
    assert task_model.get_task_strict("t2")["hearing_id"] == "h-solo"

    # Unlink task bumps CTag
    c_d2_2 = dav_sync.get_ctag(col_d2)
    updated_t2 = task_model.get_task_strict("t2")
    r_ut = client.post(
        "/audiences/h-solo/taches/t2/delier",
        data={"csrf_token": "tok", "expected_etag": updated_t2["etag"]},
    )
    assert r_ut.status_code == 302
    assert "succes=" in r_ut.headers["Location"]
    assert dav_sync.get_ctag(col_d2) != c_d2_2
    assert task_model.get_task_strict("t2")["hearing_id"] is None

    # Fail-closed display when strict readers fail
    with (
        mock.patch.object(document_model, "list_documents_for_hearing_strict", side_effect=RuntimeError("boom")),
        mock.patch.object(note_model, "list_notes_for_hearing_strict", side_effect=RuntimeError("boom")),
        mock.patch.object(task_model, "list_tasks_for_hearing_strict", side_effect=RuntimeError("boom")),
    ):
        err_html = client.get("/audiences/h-1").get_data(as_text=True)
        assert "Impossible de charger les documents liés" in err_html
        assert "Impossible de charger les notes liées" in err_html
        assert "Impossible de charger les tâches liées" in err_html


def test_deleted_hearing_heals_on_note_and_task_edit_but_read_error_fails_closed(
    client, monkeypatch,
):
    fake = install(monkeypatch, *_fake_modules())
    _seed_dossier(fake)
    _seed_hearing(fake, hearing_id="h-gone")
    _seed_hearing(fake, hearing_id="h-live")

    note, errs = note_model.create_note({
        "title": "Note audience",
        "content": "Contenu initial",
        "dossier_id": "d-1",
        "hearing_id": "h-gone",
    })
    assert not errs and note is not None

    task, terrs = task_model.create_task({
        "title": "Tâche audience",
        "status": "à_faire",
        "dossier_id": "d-1",
        "hearing_id": "h-gone",
    })
    assert not terrs and task is not None

    # Simulate deleted hearing (record removed from Firestore).
    fake.collection("hearings").document("h-gone").delete()

    # Editing via Web UI keeps/heals the deleted hearing_id -> None rather than deadlocking edits.
    from flask import url_for
    with client.application.test_request_context():
        note_post = url_for("notes.note_update", note_id=note["id"])
        task_post = url_for("tasks.task_update", task_id=task["id"])

    resp_n = client.post(note_post, data={
        "title": "Note audience modifiée",
        "content": "Contenu modifié",
        "dossier_id": "d-1",
        "hearing_id": "h-gone",
        "expected_etag": note["etag"],
    })
    assert resp_n.status_code == 302
    assert note_model.get_note_strict(note["id"])["hearing_id"] is None

    resp_t = client.post(task_post, data={
        "title": "Tâche audience modifiée",
        "status": "en_cours",
        "dossier_id": "d-1",
        "hearing_id": "h-gone",
        "expected_etag": task["etag"],
    })
    assert resp_t.status_code == 302
    assert task_model.get_task_strict(task["id"])["hearing_id"] is None

    # Direct model door also heals an unchanged dangling hearing_id when deleted,
    # while refusing an explicitly supplied unknown target id.
    note2, _ = note_model.create_note({
        "title": "Note 2",
        "content": "Texte",
        "dossier_id": "d-1",
        "hearing_id": "h-live",
    })
    assert note2 is not None
    updated_refuse, uerrs = note_model.update_note(
        note2["id"], {"hearing_id": "h-nonexistent"}, expected_etag=note2["etag"]
    )
    assert updated_refuse is None and "introuvable" in " ".join(uerrs).lower()

    # Store read error must fail closed — never heal or allow unverified writes.
    with mock.patch.object(hearing_model, "get_hearing_strict", side_effect=RuntimeError("outage")):
        fail_n = client.post(note_post, data={
            "title": "Note outage",
            "content": "Texte",
            "dossier_id": "d-1",
            "hearing_id": "h-live",
            "expected_etag": note_model.get_note_strict(note["id"])["etag"],
        })
        assert fail_n.status_code == 200
        assert "panne passagère" in fail_n.get_data(as_text=True).lower()
        assert note_model.get_note_strict(note["id"])["title"] == "Note audience modifiée"

        fail_t = client.post(task_post, data={
            "title": "Tâche outage",
            "status": "à_faire",
            "dossier_id": "d-1",
            "hearing_id": "h-live",
            "expected_etag": task_model.get_task_strict(task["id"])["etag"],
        })
        assert fail_t.status_code == 200
        assert "panne passagère" in fail_t.get_data(as_text=True).lower()
        assert task_model.get_task_strict(task["id"])["title"] == "Tâche audience modifiée"


def test_note_and_task_form_link_off_and_detachment(client, monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    _seed_dossier(fake)
    _seed_hearing(fake, hearing_id="h-1")

    note, _ = note_model.create_note({
        "title": "Note liée",
        "content": "Corps",
        "dossier_id": "d-1",
        "hearing_id": "h-1",
    })
    task, _ = task_model.create_task({
        "title": "Tâche liée",
        "status": "à_faire",
        "dossier_id": "d-1",
        "hearing_id": "h-1",
    })
    assert note is not None and task is not None

    from flask import url_for
    with client.application.test_request_context():
        note_edit_url = url_for("notes.note_edit", note_id=note["id"])
        note_post_url = url_for("notes.note_update", note_id=note["id"])
        task_edit_url = url_for("tasks.task_edit", task_id=task["id"])
        task_post_url = url_for("tasks.task_update", task_id=task["id"])

    n_html = client.get(note_edit_url).get_data(as_text=True)
    assert 'aria-label="Dissocier de l\'événement"' in n_html
    assert "link_off" in n_html
    assert 'min-h-[44px] min-w-[44px]' in n_html
    assert ':value="hearingId"' in n_html

    t_html = client.get(task_edit_url).get_data(as_text=True)
    assert 'aria-label="Dissocier de l\'événement"' in t_html
    assert "link_off" in t_html
    assert ':value="hearingId"' in t_html

    assert client.post(note_post_url, data={
        "title": "Note liée",
        "content": "Corps",
        "dossier_id": "d-1",
        "hearing_id": "",
        "expected_etag": note["etag"],
    }).status_code == 302
    assert note_model.get_note_strict(note["id"])["hearing_id"] is None

    assert client.post(task_post_url, data={
        "title": "Tâche liée",
        "status": "à_faire",
        "dossier_id": "d-1",
        "hearing_id": "",
        "expected_etag": task["etag"],
    }).status_code == 302
    assert task_model.get_task_strict(task["id"])["hearing_id"] is None


def test_note_and_task_models_enforce_hearing_one_door_per_rule(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    _seed_dossier(fake, dossier_id="d-1")
    _seed_dossier(fake, dossier_id="d-2", file_number="2026-002", title="Autre")
    _seed_hearing(fake, hearing_id="h-1", dossier_id="d-1")

    n_bad, errs_n = note_model.create_note({
        "title": "N", "content": "C", "dossier_id": "d-1", "hearing_id": "missing",
    })
    assert n_bad is None and errs_n

    n_cross, errs_cross = note_model.create_note({
        "title": "N", "content": "C", "dossier_id": "d-2", "hearing_id": "h-1",
    })
    assert n_cross is None and errs_cross

    n_ana, errs_ana = note_model.create_note({
        "title": "Théorie", "content": "C", "dossier_id": "d-1",
        "is_analyse": True, "hearing_id": "h-1",
    })
    assert n_ana is not None and not errs_ana
    assert n_ana["hearing_id"] is None
    ana_res, ana_errs, _ = note_model.set_note_hearing(n_ana["id"], "h-1")
    assert ana_res is None and ana_errs

    t_bad, terrs = task_model.create_task({
        "title": "T", "status": "à_faire", "dossier_id": "d-1", "hearing_id": "missing",
    })
    assert t_bad is None and terrs

    t_cross, terrs_cross = task_model.create_task({
        "title": "T", "status": "à_faire", "dossier_id": "d-2", "hearing_id": "h-1",
    })
    assert t_cross is None and terrs_cross

    # Moving a linked note/task to another dossier while keeping hearing_id must fail.
    n_ok, _ = note_model.create_note({
        "title": "N ok", "content": "C", "dossier_id": "d-1", "hearing_id": "h-1",
    })
    t_ok, _ = task_model.create_task({
        "title": "T ok", "status": "à_faire", "dossier_id": "d-1", "hearing_id": "h-1",
    })
    assert n_ok is not None and t_ok is not None
    assert note_model.update_note(
        n_ok["id"], {"dossier_id": "d-2"}, expected_etag=n_ok["etag"]
    )[0] is None
    assert task_model.update_task(
        t_ok["id"], {"dossier_id": "d-2"}, expected_etag=t_ok["etag"]
    )[0] is None


def test_hearing_dossier_move_detaches_cross_dossier_links_and_blocks_leaks(
    client, monkeypatch,
):
    fake = install(monkeypatch, *_fake_modules())
    _seed_dossier(fake, dossier_id="d-1")
    _seed_dossier(fake, dossier_id="d-2", file_number="2026-002", title="Dossier 2")
    _seed_hearing(fake, hearing_id="h-move", dossier_id="d-1")

    fake.collection("documents").document("doc-d1").set({
        "id": "doc-d1",
        "dossier_id": "d-1",
        "hearing_id": "h-move",
        "title": "PV D1",
        "file_name": "pv.pdf",
        "file_type": "pdf",
        "file_size": 100,
        "category": "procès_verbal_audience",
        "storage_path": "users/test/dossiers/d-1/pv.pdf",
        "etag": "etag-doc1",
        "created_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
    })
    note, _ = note_model.create_note({
        "title": "Note D1", "content": "C", "dossier_id": "d-1", "hearing_id": "h-move",
    })
    task, _ = task_model.create_task({
        "title": "Tâche D1", "status": "à_faire", "dossier_id": "d-1", "hearing_id": "h-move",
    })
    assert note is not None and task is not None

    ctag_before = dav_sync.get_ctag(dav_sync.collection_for("d-1"))

    resp = client.post("/audiences/h-move", data={
        "title": "Audience déplacée",
        "hearing_type": "audience",
        "start_date": "2026-06-15",
        "start_time": "09:30",
        "end_time": "10:30",
        "dossier_id": "d-2",
        "expected_etag": "etag-h1",
    })
    assert resp.status_code == 302

    assert document_model.get_document_strict("doc-d1")["hearing_id"] is None
    assert note_model.get_note_strict(note["id"])["hearing_id"] is None
    assert task_model.get_task_strict(task["id"])["hearing_id"] is None
    assert dav_sync.get_ctag(dav_sync.collection_for("d-1")) != ctag_before

    # Even if a stale cross-dossier record manually pointed to h-move, detail views must not leak it.
    fake.collection("documents").document("doc-stale").set({
        "id": "doc-stale",
        "dossier_id": "d-1",
        "hearing_id": "h-move",
        "title": "Stale Doc",
        "file_name": "stale.pdf",
        "file_type": "pdf",
        "file_size": 50,
        "category": "autre",
        "storage_path": "users/test/dossiers/d-1/stale.pdf",
        "etag": "etag-stale",
        "created_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
    })
    h_html = client.get("/audiences/h-move").get_data(as_text=True)
    assert "Stale Doc" not in h_html
    doc_html = client.get("/documents/doc-stale").get_data(as_text=True)
    assert "Audience déplacée" not in doc_html


def test_all_day_linked_hearing_date_not_shifted_by_timezone(client, monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    _seed_dossier(fake)
    _seed_hearing(fake, hearing_id="h-allday")
    fake.collection("hearings").document("h-allday").update({
        "all_day": True,
        "start_datetime": datetime(2026, 6, 15, 0, 0, tzinfo=timezone.utc),
        "end_datetime": datetime(2026, 6, 15, 0, 0, tzinfo=timezone.utc),
    })

    fake.collection("documents").document("doc-ad").set({
        "id": "doc-ad",
        "dossier_id": "d-1",
        "hearing_id": "h-allday",
        "title": "Doc journée",
        "file_name": "doc.pdf",
        "file_type": "pdf",
        "file_size": 10,
        "category": "autre",
        "storage_path": "users/test/dossiers/d-1/doc.pdf",
        "etag": "etag-ad",
        "created_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
    })
    note, _ = note_model.create_note({
        "title": "Note journée", "content": "C", "dossier_id": "d-1", "hearing_id": "h-allday",
    })
    task, _ = task_model.create_task({
        "title": "Tâche journée", "status": "à_faire", "dossier_id": "d-1", "hearing_id": "h-allday",
    })
    assert note is not None and task is not None

    from flask import url_for
    with client.application.test_request_context():
        urls = [
            url_for("documents.document_detail", document_id="doc-ad"),
            url_for("notes.note_detail", note_id=note["id"]),
            url_for("tasks.task_detail", task_id=task["id"]),
            url_for("notes.note_edit", note_id=note["id"]),
            url_for("tasks.task_edit", task_id=task["id"]),
        ]
    for url in urls:
        html = client.get(url).get_data(as_text=True)
        assert "2026-06-15" in html
        assert "2026-06-14" not in html


def test_standalone_hearing_detail_uses_bounded_queries_and_no_min_w_12rem(
    client, monkeypatch,
):
    fake = install(monkeypatch, *_fake_modules())
    _seed_hearing(
        fake,
        hearing_id="h-solo",
        dossier_id="",
        dossier_file_number="",
        dossier_title="",
        hearing_type="reunion",
        title="Rencontre hors dossier",
        location="Bureau",
        court="",
        judge="",
    )
    note, _ = note_model.create_note({
        "title": "Note libre", "content": "Texte", "dossier_id": None,
    })
    task, _ = task_model.create_task({
        "title": "Tâche libre", "status": "à_faire", "dossier_id": None,
    })
    assert note is not None and task is not None

    def _forbid(*_a, **_kw):
        raise AssertionError("unbounded list_notes/list_tasks must not be called")

    with (
        mock.patch.object(note_model, "list_notes", side_effect=_forbid),
        mock.patch.object(task_model, "list_tasks", side_effect=_forbid),
    ):
        html = client.get("/audiences/h-solo").get_data(as_text=True)

    assert "Lieu" in html
    assert "Lieu et tribunal" not in html
    assert "Note libre" in html
    assert "Tâche libre" in html
    assert "min-w-[12rem]" not in html


def test_hearing_upload_incertain_guard_and_accept_present(client, monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    _seed_dossier(fake)
    _seed_hearing(fake, hearing_id="h-up")
    html = client.get("/audiences/h-up").get_data(as_text=True)
    assert "this.incertain = true;" in html
    assert ':disabled="submitting || !fichier || incertain"' in html
    assert 'accept=".pdf,' in html
    assert "min-w-[12rem]" not in html


def test_phase2_1a_remediated_icon_targets():
    tpl = _ATHENA / "templates"
    browser = (tpl / "documents" / "_browser.html").read_text(encoding="utf-8")
    assert 'title="Créer le dossier"' in browser
    assert 'min-h-[44px] min-w-[44px] p-2.5 text-green-600' in browser
    assert 'min-h-[44px] min-w-[44px] p-2.5 text-gray-400' in browser

    gabarit = (tpl / "gabarits" / "detail.html").read_text(encoding="utf-8")
    assert "Télécharger la version" in gabarit
    assert "ms('download', 20)" in gabarit
    assert "inline-flex items-center min-h-[44px] px-3 py-1.5" in gabarit

    for acc in ("trust/account_detail.html", "administration/account_detail.html"):
        src = (tpl / acc).read_text(encoding="utf-8")
        assert 'aria-label="Nouvelle conciliation"' in src
        assert "ms('add', 20)" in src
        assert 'min-h-[44px] min-w-[44px]' in src

    for frag in ("gabarits/_generate_fields.html", "invoices/_note_generated.html"):
        src = (tpl / frag).read_text(encoding="utf-8")
        assert 'aria-label="Télécharger"' in src
        assert "ms('download', 20)" in src
        assert 'min-h-[44px] min-w-[44px]' in src

    budget = (tpl / "dossiers" / "_tab_budget.html").read_text(encoding="utf-8")
    assert budget.count("min-h-[44px]") >= 2

    parties = (tpl / "parties" / "detail.html").read_text(encoding="utf-8")
    assert 'min-h-[44px] px-3 py-2 text-sm text-gray-700' in parties
    inv_detail = (tpl / "invoices" / "detail.html").read_text(encoding="utf-8")
    edit_idx = inv_detail.index("invoice_draft_edit")
    edit_block = inv_detail[edit_idx:inv_detail.index("</a>", edit_idx)]
    assert "p-3 text-gray-700" in edit_block
    assert "min-w-[44px]" not in edit_block
    assert "min-w-[44px]" not in (tpl / "invoices" / "draft_form.html").read_text(encoding="utf-8")
    assert "p-3 text-gray-500" in (tpl / "invoices" / "draft_form.html").read_text(encoding="utf-8")

