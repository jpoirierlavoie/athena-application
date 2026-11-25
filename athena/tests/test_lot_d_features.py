"""End-to-end verification of Phase 4 (Lot D) across real models, routes, and templates.

Covers every feature and guardrail in Phase 4 (Lot D) on ``_fake_firestore.py``:
- 1i: Bilingual FR/EN gabarits & modèles (optional ``en_file`` upload, independent
      FR/EN versions, identical placeholder names with ``lang='en'`` formatting for
      dates, currency, courts, litigation roles, salutations, client.language=='en'
      highlighting/defaults, billing note_honoraires / note_credit / avis_interets
      FR+EN generation & fingerprint isolation, strict refusal without EN file).
- 1f: Unified chronological « À traiter » Réception feed + filter pills, batch
      file & feed-item selection (bulk Verser to chosen dossier folder, bulk
      Refuser, bulk Archiver/Écarter/Révoquer/Refuser ouverture with honest
      per-item reporting), dynamic dossier folder picker, printable ReportLab PDF
      Accusé de réception (Helvetica-free, fail-closed), and cohesive firm-branded
      inline-CSS layout across all 11 outbound email templates.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import sys
import zipfile
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest import mock

import pytest
from flask import Flask, render_template
from pypdf import PdfReader

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
        doc_template as template_model,
        document as document_model,
        dossier as dossier_model,
        expense as expense_model,
        folder as folder_model,
        hearing as hearing_model,
        invoice as invoice_model,
        note as note_model,
        partie as partie_model,
        portail_invitation as pi_model,
        protocol as protocol_model,
        settings as settings_model,
        task as task_model,
        time_entry as time_entry_model,
        transcription_reunion as tr_model,
        trust as trust_model,
    )
    import dav.sync as dav_sync
    _PRELOADED_MODELS = (
        admin_model, template_model, document_model,
        dossier_model, expense_model, folder_model, hearing_model,
        invoice_model, note_model, partie_model,
        protocol_model, settings_model, task_model, time_entry_model,
        tr_model, trust_model, dav_sync,
    )
    import routes.doc_templates as doc_templates_routes
    import routes.invoices as invoices_routes
    import routes.reception as reception_routes
    import routes.parties as parties_routes
    import routes.dossiers as dossiers_routes
    import routes.documents as documents_routes
    import services.avis_interets as avis_interets_service
    import services.gabarit_champs as gabarit_champs_service
    import services.gabarits as gabarits_service
    import services.note_credit as note_credit_service
    import services.note_honoraires as note_honoraires_service

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils import deadlines as deadlines_util  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.html_attr import jsattr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.markdown_docx import markdown_to_safe_html  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

UTC = timezone.utc


class _FakeBlob:
    def __init__(self, store: dict, name: str):
        self._store = store
        self.name = name
        self.content_type = "application/octet-stream"
        self.content_disposition = None
        self.size = 0
        self.generation = 1 if name in store else None

    def exists(self) -> bool:
        return self.name in self._store

    def upload_from_string(self, data, content_type="application/octet-stream", **kw):
        if kw.get("if_generation_match") == 0 and self.name in self._store:
            from google.cloud.exceptions import Conflict
            raise Conflict("already exists")
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        self._store[self.name] = raw
        self.content_type = content_type
        self.size = len(raw)
        self.generation = (self.generation or 0) + 1

    def upload_from_file(self, fp, content_type="application/octet-stream", **kw):
        if kw.get("if_generation_match") == 0 and self.name in self._store:
            from google.cloud.exceptions import Conflict
            raise Conflict("already exists")
        fp.seek(0)
        raw = fp.read()
        self._store[self.name] = raw
        self.content_type = content_type
        self.size = len(raw)
        self.generation = (self.generation or 0) + 1

    def patch(self, **kw):
        pass

    def rewrite(self, source_blob, token=None, **kw):
        if kw.get("if_generation_match") == 0 and self.name in self._store:
            from google.cloud.exceptions import PreconditionFailed
            raise PreconditionFailed("exists")
        data = source_blob.download_as_bytes()
        self._store[self.name] = data
        self.size = len(data)
        self.generation = (self.generation or 0) + 1
        return None, self.size, self.size

    def download_as_bytes(self, start=None, end=None, **kw) -> bytes:
        if self.name not in self._store:
            raise FileNotFoundError(self.name)
        data = self._store[self.name]
        if start is not None or end is not None:
            s = start or 0
            e = (end + 1) if end is not None else None
            return data[s:e]
        return data

    def open(self, mode="rb", **kw):
        return io.BytesIO(self.download_as_bytes())

    def reload(self):
        if self.name not in self._store:
            raise FileNotFoundError(self.name)
        self.size = len(self._store[self.name])
        if self.generation is None:
            self.generation = 1

    def delete(self):
        self._store.pop(self.name, None)

    def generate_signed_url(self, **kw) -> str:
        disp = (kw.get("response_disposition") or "").replace('"', "'")
        return f"https://signed.example/{self.name}?disp={disp}"


class _FakeBucket:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def blob(self, name: str) -> _FakeBlob:
        b = _FakeBlob(self.objects, name)
        if name in self.objects:
            b.size = len(self.objects[name])
            b.generation = 1
        return b

    def copy_blob(self, src_blob, dest_bucket, dest_name: str, **kw):
        if kw.get("if_generation_match") == 0 and dest_name in dest_bucket.objects:
            from google.cloud.exceptions import PreconditionFailed
            raise PreconditionFailed("exists")
        dest_bucket.objects[dest_name] = src_blob.download_as_bytes()
        dest = dest_bucket.blob(dest_name)
        dest.generation = 1
        return dest

    def list_blobs(self, prefix: str = ""):
        out = []
        for k in sorted(self.objects):
            if k.startswith(prefix):
                out.append(self.blob(k))
        return out


def _make_docx(text: str) -> bytes:
    """Create a valid minimal .docx archive containing *text*."""
    buf = io.BytesIO()
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    from xml.sax.saxutils import escape
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p></w:body>"
        "</w:document>"
    )
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", doc_xml)
    return buf.getvalue()


def _read_docx_text(raw: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
        return zf.read("word/document.xml").decode("utf-8")


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
def fake_portail_db(monkeypatch):
    from tests._fake_firestore import FakeFirestore
    portail_db = FakeFirestore()
    monkeypatch.setattr(pi_model, "_col", lambda: portail_db.collection("invitations"))
    return portail_db


@pytest.fixture
def buckets(monkeypatch):
    main_bucket = _FakeBucket()
    portail_bucket = _FakeBucket()
    for mod in (template_model, document_model):
        monkeypatch.setattr(mod, "storage", SimpleNamespace(bucket=lambda *a, **k: main_bucket))
    monkeypatch.setattr(reception_routes, "_bucket", lambda: portail_bucket)
    return SimpleNamespace(main=main_bucket, portail=portail_bucket)


@pytest.fixture
def app_ctx(fake, fake_portail_db, buckets):
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
        doc_templates_routes.doc_templates_bp,
        doc_templates_routes.modeles_bp,
        invoices_routes.invoices_bp,
        reception_routes.reception_bp,
        parties_routes.parties_bp,
        dossiers_routes.dossiers_bp,
        documents_routes.documents_bp,
    ):
        app.register_blueprint(bp)

    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return SimpleNamespace(app=app, client=c, db=fake, portail_db=fake_portail_db, buckets=buckets)


def _seed_cabinet(fake):
    fake.seed("settings/cabinet", {
        "nom": "Me Jason Poirier Lavoie",
        "organisation": "Poirier Lavoie Avocats",
        "address_street": "100 rue Notre-Dame",
        "address_city": "Montréal",
        "address_province": "QC",
        "address_postal_code": "H2Y 1C1",
        "telephone": "+15145550100",
        "courriel": "cabinet@exemple.ca",
        "gst_number": "123456789RT0001",
        "qst_number": "1234567890TQ0001",
    })


def _seed_en_client_and_dossier(fake, dossier_id="d-1", *, client_lang="en"):
    fake.seed("parties/c-1", {
        "id": "c-1",
        "type": "individual",
        "contact_role": "client",
        "prefix": "Mme",
        "first_name": "Jane",
        "last_name": "Doe",
        "email": "jane@example.com",
        "language": client_lang,
        "address_street": "50 Main St",
        "address_city": "Montreal",
        "address_province": "QC",
        "address_postal_code": "H3A 1A1",
        "etag": "ec1",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    })
    fake.seed("parties/adv-1", {
        "id": "adv-1",
        "type": "organization",
        "contact_role": "partie_adverse",
        "organization_name": "Acme Corp.",
        "etag": "ea1",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    })
    fake.seed(f"dossiers/{dossier_id}", {
        "id": dossier_id,
        "file_number": "2026-001",
        "title": "Doe c. Acme Corp.",
        "tribunal": "Cour supérieure",
        "court_file_number": "500-17-123456-261",
        "role": "demandeur",
        "status": "actif",
        "clients": [{"id": "c-1", "role": "demandeur"}],
        "opposing_parties": [{"id": "adv-1", "role": "defendeur"}],
        "client_ids": ["c-1"],
        "opposing_party_ids": ["adv-1"],
        "etag": "ed1",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    })


# ═════════════════════════════════════════════════════════════════════════
# 1. Feature 1i — Bilingual FR/EN Gabarits & Billing Documents
# ═════════════════════════════════════════════════════════════════════════


def test_1i_bilingual_template_create_update_restore_download_and_ui(app_ctx, monkeypatch):
    monkeypatch.setattr(
        template_model,
        "sign_blob_url",
        lambda blob, params, **kw: f"https://signed.example/{blob.name}?disp={params.get('response-content-disposition', '')}",
    )
    c = app_ctx.client
    fr_docx = _make_docx(
        "Bonjour {{client.nom_complet_avec_civilite}}, {{dossier.tribunal}} le {{date.aujourdhui}} — {{dossier.role_label}}."
    )
    en_docx = _make_docx(
        "Dear {{client.nom_complet_avec_civilite}}, {{dossier.tribunal}} on {{date.aujourdhui}} — {{dossier.role_label}} ({{objet}})."
    )

    resp = c.post(
        "/gabarits/",
        data={
            "name": "Mise en demeure bilingue",
            "category": "correspondance",
            "kind": "gabarit",
            "file": (io.BytesIO(fr_docx), "med_fr.docx"),
            "en_file": (io.BytesIO(en_docx), "med_en.docx"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    loc = resp.headers["Location"]
    tid = loc.rstrip("/").split("/")[-1]

    # Create a second FR-only template to contrast list UI buttons.
    fr_only = _make_docx("Lettre {{client.nom}}.")
    c.post(
        "/gabarits/",
        data={
            "name": "Lettre FR seule",
            "category": "correspondance",
            "kind": "gabarit",
            "file": (io.BytesIO(fr_only), "fr_seule.docx"),
        },
        content_type="multipart/form-data",
    )

    # List page shows FR/EN buttons on bilingual template and single Générer on FR-only.
    list_html = c.get("/gabarits/").get_data(as_text=True)
    assert "FR/EN" in list_html
    assert f'template_id={tid}' in list_html or f'/gabarits/generer?' in list_html
    assert "Générer (FR)" in list_html
    assert "Générer (EN)" in list_html

    # Detail page renders FR and EN placeholders, versions, and downloads.
    detail_html = c.get(f"/gabarits/{tid}").get_data(as_text=True)
    assert "med_fr.docx" in detail_html and "med_en.docx" in detail_html
    assert "Champs FR (" in detail_html
    assert "Champs EN (" in detail_html
    assert "objet" in detail_html

    # Download FR and EN separately.
    dl_fr = c.get(f"/gabarits/{tid}/download")
    dl_en = c.get(f"/gabarits/{tid}/download?lang=en")
    assert dl_fr.status_code == 302 and "med_fr.docx" in dl_fr.headers["Location"]
    assert dl_en.status_code == 302 and "/en/" in dl_en.headers["Location"]
    dl_es = c.get(f"/gabarits/{tid}/download?lang=es")
    assert dl_es.status_code == 302 and "erreur=" in dl_es.headers["Location"]

    # Replace ONLY the EN stream -> EN bumps to v2, FR remains v1.
    stored = app_ctx.db.peek(f"doc_templates/{tid}")
    en_v2 = _make_docx("Updated EN {{client.nom_complet}} on {{date.aujourdhui}}.")
    upd = c.post(
        f"/gabarits/{tid}",
        data={
            "name": "Mise en demeure bilingue",
            "category": "correspondance",
            "kind": "gabarit",
            "expected_etag": stored["etag"],
            "en_file": (io.BytesIO(en_v2), "med_en_v2.docx"),
        },
        content_type="multipart/form-data",
    )
    assert upd.status_code == 302
    stored_after = app_ctx.db.peek(f"doc_templates/{tid}")
    assert stored_after["version"] == 1
    assert stored_after["en_version"] == 2
    assert stored_after["en_placeholders"] == ["client.nom_complet", "date.aujourdhui"]

    # Detail page shows Versions — FR and Versions — EN and restores EN v1 -> v3.
    det2 = c.get(f"/gabarits/{tid}").get_data(as_text=True)
    assert "Versions — FR" in det2 and "Versions — EN" in det2
    rest = c.post(
        f"/gabarits/{tid}/versions/1/retablir",
        data={"expected_etag": stored_after["etag"], "lang": "en"},
    )
    assert rest.status_code == 302
    restored = app_ctx.db.peek(f"doc_templates/{tid}")
    assert restored["version"] == 1
    assert restored["en_version"] == 3
    assert "objet" in restored["en_placeholders"]

    # Bilingual modèle exposes side-by-side FR/EN downloads on /modeles/ & detail.
    mod_resp = c.post(
        "/gabarits/",
        data={
            "name": "Contrat bilingue",
            "kind": "modele",
            "file": (io.BytesIO(_make_docx("Contrat FR")), "contrat_fr.docx"),
            "en_file": (io.BytesIO(_make_docx("Contract EN")), "contrat_en.docx"),
        },
        content_type="multipart/form-data",
    )
    assert mod_resp.status_code == 302
    mid = mod_resp.headers["Location"].rstrip("/").split("/")[-1]
    mdet = c.get(f"/gabarits/{mid}").get_data(as_text=True)
    assert "Télécharger le modèle (FR)" in mdet
    assert "Télécharger (EN)" in mdet


def test_1i_generation_en_formats_english_values_and_defaults_client_language(app_ctx, monkeypatch):
    _seed_cabinet(app_ctx.db)
    _seed_en_client_and_dossier(app_ctx.db, client_lang="en")
    monkeypatch.setattr(deadlines_util, "today_mtl", lambda: date(2026, 11, 24))
    for mod in (doc_templates_routes, gabarits_service, gabarit_champs_service):
        if hasattr(mod, "today_mtl"):
            monkeypatch.setattr(mod, "today_mtl", lambda: date(2026, 11, 24))

    fr_docx = _make_docx("Le {{date.aujourdhui}}, {{client.nom_complet_avec_civilite}} ({{dossier.role_label}}) devant {{dossier.tribunal}}.")
    en_docx = _make_docx("On {{date.aujourdhui}}, {{client.nom_complet_avec_civilite}} ({{dossier.role_label}}) before {{dossier.tribunal}} — {{privilège}}.")
    tpl, errors = template_model.create_template(
        io.BytesIO(fr_docx), "lettre_fr.docx", len(fr_docx),
        {"name": "Lettre bilingue", "category": "correspondance", "kind": "gabarit"},
        "u1",
        en_file_stream=io.BytesIO(en_docx), en_filename="lettre_en.docx", en_file_size=len(en_docx),
    )
    assert errors == [] and tpl is not None
    tid = tpl["id"]

    c = app_ctx.client
    # Client language == "en" + template has_en -> modal defaults to lang="en" and renders English values.
    modal_html = c.get(f"/gabarits/generer?template_id={tid}&dossier_id=d-1").get_data(as_text=True)
    assert 'name="lang" value="en"' in modal_html
    assert "November 24, 2026" in modal_html
    assert "Ms. Jane Doe" in modal_html
    assert "Superior Court" in modal_html
    assert "Plaintiff" in modal_html
    assert "FR — Français" in modal_html and "EN — English" in modal_html

    # Switching explicitly to FR renders French auto values.
    fr_fields_html = c.get(
        f"/gabarits/generer/champs?template_id={tid}&dossier_id=d-1&lang=fr"
    ).get_data(as_text=True)
    assert 'name="lang" value="fr"' in fr_fields_html
    assert "24 novembre 2026" in fr_fields_html
    assert "Cour supérieure" in fr_fields_html
    assert "Demandeur" in fr_fields_html

    # Generate in EN (from dossier -> saved into Projets).
    gen_resp = c.post(
        "/gabarits/generer",
        data={
            "template_id": tid,
            "dossier_id": "d-1",
            "client_id": "c-1",
            "adverse_id": "adv-1",
            "lang": "en",
            "champ__date.aujourdhui": "November 24, 2026",
            "champ__client.nom_complet_avec_civilite": "Ms. Jane Doe",
            "champ__dossier.role_label": "Plaintiff",
            "champ__dossier.tribunal": "Superior Court",
            "champ__privilège": "WITHOUT PREJUDICE",
        },
        headers={"HX-Request": "true"},
    )
    assert gen_resp.status_code == 200
    assert "Document généré" in gen_resp.get_data(as_text=True)

    docs = document_model.list_documents("d-1")
    assert len(docs) == 1
    assert "(EN)" in docs[0]["display_name"]
    raw_docx = app_ctx.buckets.main.objects[docs[0]["storage_path"]]
    xml = _read_docx_text(raw_docx)
    assert "November 24, 2026" in xml
    assert "Ms. Jane Doe" in xml
    assert "Plaintiff" in xml
    assert "Superior Court" in xml
    assert "WITHOUT PREJUDICE" in xml

    # Refuses cleanly when requesting EN on a FR-only template.
    fr_only, _ = template_model.create_template(
        io.BytesIO(fr_docx), "fr_only.docx", len(fr_docx),
        {"name": "FR Uniquement", "category": "correspondance", "kind": "gabarit"},
        "u1",
    )
    refused = c.post(
        "/gabarits/generer",
        data={"template_id": fr_only["id"], "dossier_id": "d-1", "lang": "en"},
        headers={"HX-Request": "true"},
    )
    assert refused.status_code == 200
    assert "pas de version anglaise" in refused.get_data(as_text=True)


def test_1i_billing_fr_en_buttons_default_highlight_and_fingerprint_isolation(app_ctx, monkeypatch):
    _seed_cabinet(app_ctx.db)
    _seed_en_client_and_dossier(app_ctx.db, client_lang="en")
    monkeypatch.setattr(deadlines_util, "today_mtl", lambda: date(2026, 11, 24))
    monkeypatch.setattr(deadlines_util, "today_utc", lambda: date(2026, 11, 24))
    for mod in (
        invoices_routes, note_honoraires_service, note_credit_service,
        avis_interets_service, invoice_model, gabarits_service, gabarit_champs_service,
    ):
        if hasattr(mod, "today_mtl"):
            monkeypatch.setattr(mod, "today_mtl", lambda: date(2026, 11, 24))
        if hasattr(mod, "today_utc"):
            monkeypatch.setattr(mod, "today_utc", lambda: date(2026, 11, 24))

    # Active bilingual templates for note_honoraires, note_credit, avis_interets.
    nh_fr = _make_docx("Facture {{facture.numero}} du {{facture.date}} Total {{facture.total_apres_taxes}}.")
    nh_en = _make_docx("Invoice {{facture.numero}} dated {{facture.date}} Total {{facture.total_apres_taxes}}.")
    t_nh, errs = template_model.create_template(
        io.BytesIO(nh_fr), "nh_fr.docx", len(nh_fr),
        {"name": "Facture bilingue", "category": "autre", "kind": "note_honoraires"},
        "u1",
        en_file_stream=io.BytesIO(nh_en), en_filename="nh_en.docx", en_file_size=len(nh_en),
    )
    assert errs == [] and t_nh is not None
    template_model.set_active_template(t_nh["id"], par="test@example.com", expected_etag=t_nh["etag"])

    nc_fr = _make_docx("Note crédit {{nc.numero}} le {{nc.date}} Total {{nc.total}}.")
    nc_en = _make_docx("Credit Note {{nc.numero}} on {{nc.date}} Total {{nc.total}}.")
    t_nc, errs = template_model.create_template(
        io.BytesIO(nc_fr), "nc_fr.docx", len(nc_fr),
        {"name": "Note de crédit bilingue", "category": "autre", "kind": "note_credit"},
        "u1",
        en_file_stream=io.BytesIO(nc_en), en_filename="nc_en.docx", en_file_size=len(nc_en),
    )
    assert errs == [] and t_nc is not None
    template_model.set_active_template(t_nc["id"], par="test@example.com", expected_etag=t_nc["etag"])

    ai_fr = _make_docx("Avis d'intérêts {{ai.numero}} du {{ai.date}} Montant {{ai.montant}} Taux {{ai.taux}}.")
    ai_en = _make_docx("Interest Notice {{ai.numero}} dated {{ai.date}} Amount {{ai.montant}} Rate {{ai.taux}}.")
    t_ai, errs = template_model.create_template(
        io.BytesIO(ai_fr), "ai_fr.docx", len(ai_fr),
        {"name": "Avis d'intérêts bilingue", "category": "autre", "kind": "avis_interets"},
        "u1",
        en_file_stream=io.BytesIO(ai_en), en_filename="ai_en.docx", en_file_size=len(ai_en),
    )
    assert errs == [] and t_ai is not None
    template_model.set_active_template(t_ai["id"], par="test@example.com", expected_etag=t_ai["etag"])

    inv_id = "inv-1"
    app_ctx.db.seed(f"invoices/{inv_id}", {
        "id": inv_id,
        "invoice_number": "2026-F001",
        "dossier_id": "d-1",
        "client_id": "c-1",
        "status": "envoyée",
        "date": datetime(2026, 11, 24, tzinfo=UTC),
        "issue_date": datetime(2026, 11, 24, tzinfo=UTC),
        "due_date": datetime(2026, 10, 1, tzinfo=UTC),
        "subtotal_fees": 150000,
        "subtotal_expenses": 0,
        "subtotal": 150000,
        "gst_rate": 500,
        "gst_amount": 7500,
        "qst_rate": 9975,
        "qst_amount": 14963,
        "total": 172463,
        "retainer_applied": 0,
        "amount_due": 172463,
        "amount_paid": 0,
        "gst_number": "123456789RT0001",
        "qst_number": "1234567890TQ0001",
        "write_offs": [
            {
                "id": "wo-1",
                "kind": "radiation",
                "credit_note_number": "2026-NC001",
                "date": datetime(2026, 11, 24, tzinfo=UTC),
                "amount": 50000,
                "gst_amount": 2174,
                "qst_amount": 4338,
                "tax_claimed": True,
                "reverses_id": "",
            }
        ],
        "interest_charges": [
            {
                "id": "ai-1",
                "kind": "avis_interets",
                "interest_notice_number": "2026-AI001",
                "date": datetime(2026, 11, 24, tzinfo=UTC),
                "period_start": datetime(2026, 10, 2, tzinfo=UTC),
                "period_end": datetime(2026, 11, 24, tzinfo=UTC),
                "days": 53,
                "principal": 100000,
                "rate_bps": 500,
                "amount": 726,
                "reverses_id": "",
            }
        ],
        "etag": "einv1",
        "created_at": datetime(2026, 11, 24, tzinfo=UTC),
        "updated_at": datetime(2026, 11, 24, tzinfo=UTC),
    })
    app_ctx.db.seed(f"invoices/{inv_id}/lineitems/li-1", {
        "id": "li-1",
        "type": "fee",
        "date": datetime(2026, 11, 20, tzinfo=UTC),
        "description": "Rédaction de procédures",
        "hours": 3.0,
        "rate": 50000,
        "amount": 150000,
    })

    c = app_ctx.client
    html = c.get(f"/factures/{inv_id}").get_data(as_text=True)
    # All three show FR + EN side-by-side and highlight EN because client.language == "en".
    assert "Note d'honoraires (Word — FR)" in html
    assert "Note d'honoraires (Word — EN)" in html
    assert "Générer la note de crédit (FR)" in html
    assert "Générer la note de crédit (EN)" in html
    assert "Générer l'avis d'intérêts (FR)" in html
    assert "Générer l'avis d'intérêts (EN)" in html
    assert 'hx-vals=\'{"lang": "en"}\'' in html

    # Generate Note d'honoraires in FR and EN -> separate documents & distinct formatting.
    r_fr = c.post(f"/factures/{inv_id}/note-docx", data={"lang": "fr"}, headers={"HX-Request": "true"})
    r_en = c.post(f"/factures/{inv_id}/note-docx", data={"lang": "en"}, headers={"HX-Request": "true"})
    assert r_fr.status_code == 200 and r_en.status_code == 200
    assert "Ouvrir le document" in r_fr.get_data(as_text=True)
    assert "Ouvrir le document" in r_en.get_data(as_text=True)

    # Generate Credit Note in FR and EN -> separate documents.
    c.post(f"/factures/{inv_id}/radiation/wo-1/note-de-credit", data={"lang": "fr"}, headers={"HX-Request": "true"})
    c.post(f"/factures/{inv_id}/radiation/wo-1/note-de-credit", data={"lang": "en"}, headers={"HX-Request": "true"})

    # Generate Interest Notice in EN.
    c.post(f"/factures/{inv_id}/interets/ai-1/avis", data={"lang": "en"}, headers={"HX-Request": "true"})

    docs = document_model.list_documents("d-1")
    # Expected 5 distinct documents: NH FR, NH EN, NC FR, NC EN, AI EN (never colliding!).
    assert len(docs) == 5
    xmls = [_read_docx_text(app_ctx.buckets.main.objects[d["storage_path"]]) for d in docs]
    assert any("Invoice 2026-F001 dated November 24, 2026 Total $1,724.63." in x for x in xmls)
    assert any("Facture 2026-F001 du 24 novembre 2026 Total 1\u00a0724,63\u00a0$." in x for x in xmls)
    assert any("Credit Note 2026-NC001 on November 24, 2026 Total $500.00." in x for x in xmls)
    assert any("Interest Notice 2026-AI001 dated November 24, 2026 Amount $7.26 Rate 5%." in x for x in xmls)


# ═════════════════════════════════════════════════════════════════════════
# 2. Feature 1f — Unified Réception, Batch, Emails & Accusé PDF
# ═════════════════════════════════════════════════════════════════════════


def _seed_reception_lot(
    app_ctx,
    inv_id="inv-doc1",
    batch="b1",
    *,
    submitted_at="2026-11-24T15:00:00+00:00",
    dossier_id="d-1",
    files=None,
    statut="soumise",
):
    pdf_a = b"%PDF-1.4 piece-a"
    pdf_b = b"%PDF-1.4 piece-b"
    if files is None:
        files = [
            {
                "objet": f"submissions/{inv_id}/{batch}/files/001_contrat.pdf",
                "name": "contrat.pdf",
                "size_declared": len(pdf_a),
                "size_gcs": len(pdf_a),
                "content_type": "application/pdf",
                "sha512": hashlib.sha512(pdf_a).hexdigest(),
                "etat": "reçu",
                "divergence": None,
                "payload": pdf_a,
            },
            {
                "objet": f"submissions/{inv_id}/{batch}/files/002_annexe.pdf",
                "name": "annexe.pdf",
                "size_declared": len(pdf_b),
                "size_gcs": len(pdf_b),
                "content_type": "application/pdf",
                "sha512": hashlib.sha512(pdf_b).hexdigest(),
                "etat": "reçu",
                "divergence": None,
                "payload": pdf_b,
            },
        ]
    manifest_files = []
    for f in files:
        entry = {k: v for k, v in f.items() if k != "payload"}
        manifest_files.append(entry)
        if "payload" in f:
            app_ctx.buckets.portail.objects[f["objet"]] = f["payload"]
    app_ctx.buckets.portail.objects[f"submissions/{inv_id}/{batch}/envelope.json"] = json.dumps({
        "batch": batch, "invitation_id": inv_id, "submitted_at": submitted_at,
    }).encode("utf-8")
    app_ctx.buckets.portail.objects[f"submissions/{inv_id}/{batch}/manifeste.json"] = json.dumps({
        "batch": batch,
        "invitation_id": inv_id,
        "submitted_at": submitted_at,
        "files": manifest_files,
        "etat_lot": "traité" if statut == "traitée" else "soumis",
        "http": {"ip": "198.51.100.12", "user_agent": "Mozilla/5.0"},
    }).encode("utf-8")
    app_ctx.portail_db.seed(f"invitations/{inv_id}", {
        "id": inv_id,
        "type": "documents",
        "email": "jane@example.com",
        "client_name": "Jane Doe",
        "partie_id": "c-1",
        "dossier_id": dossier_id,
        "display_label": f"Dossier 2026-001 ({inv_id})",
        "statut": statut,
        "soumissions": [{"batch": batch, "submitted_at": submitted_at}],
        "accuses": {batch: True},
        "expires_at": datetime(2026, 12, 15, tzinfo=UTC),
        "created_at": datetime(2026, 11, 20, tzinfo=UTC),
        "updated_at": datetime(2026, 11, 24, 15, 0, tzinfo=UTC),
    })


def test_1f_unified_chronological_feed_filter_pills_and_outage_fail_closed(app_ctx, monkeypatch):
    _seed_en_client_and_dossier(app_ctx.db)
    _seed_reception_lot(app_ctx, "inv-doc1", "b1", submitted_at="2026-11-24T15:00:00+00:00")

    # Divergence (16:00 UTC), RDV (17:00 UTC), Teams transcription (18:00 UTC), Opening (14:00 UTC).
    app_ctx.db.seed("hearings/h-div", {
        "id": "h-div",
        "source": "bookings",
        "confirmation": "",
        "title": "Consultation Divergente",
        "start_datetime": datetime(2026, 11, 26, 14, 0, tzinfo=UTC),
        "bookings_divergence": {"motif": "modifié_côté_client", "detail": "Nouvelle heure"},
        "etag": "ehdiv",
        "created_at": datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 11, 24, 16, 0, tzinfo=UTC),
    })
    app_ctx.db.seed("hearings/h-rdv", {
        "id": "h-rdv",
        "source": "bookings",
        "confirmation": "à_confirmer",
        "status": "à_confirmer",
        "title": "Rencontre Chronologique",
        "client_nom": "Bob Test",
        "client_email": "bob@example.com",
        "start_datetime": datetime(2026, 11, 28, 15, 0, tzinfo=UTC),
        "end_datetime": datetime(2026, 11, 28, 16, 0, tzinfo=UTC),
        "etag": "ehrdv",
        "created_at": datetime(2026, 11, 24, 17, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 11, 24, 17, 0, tzinfo=UTC),
    })
    app_ctx.db.seed("hearings/h-tx", {
        "id": "h-tx",
        "source": "teams",
        "title": "Appel Teams Récents",
        "start_datetime": datetime(2026, 11, 24, 17, 30, tzinfo=UTC),
        "etag": "ehtx",
    })
    app_ctx.db.seed("meeting_transcripts/h-tx", {
        "hearing_id": "h-tx",
        "status": "a_revoir",
        "hearing_start_at": datetime(2026, 11, 24, 17, 30, tzinfo=UTC),
        "has_insights": True,
        "has_transcript": True,
        "transcript_cue_count": 3,
        "copilot_markdown": "## Notes",
        "etag": "etx1",
        "created_at": datetime(2026, 11, 24, 18, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 11, 24, 18, 0, tzinfo=UTC),
    })
    app_ctx.portail_db.seed("invitations/inv-open", {
        "id": "inv-open",
        "type": "intake",
        "email": "new@example.com",
        "client_name": "Nouveau Client",
        "display_label": "Ouverture Chronologique",
        "statut": "soumise",
        "soumissions": [{"batch": "ob1", "submitted_at": "2026-11-24T14:00:00+00:00"}],
        "expires_at": datetime(2026, 12, 15, tzinfo=UTC),
        "created_at": datetime(2026, 11, 24, 13, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 11, 24, 14, 0, tzinfo=UTC),
    })
    app_ctx.buckets.portail.objects["submissions/inv-open/ob1/envelope.json"] = json.dumps({
        "type": "intake",
        "submitted_at": "2026-11-24T14:00:00+00:00",
        "donnees": {"nature": "physique", "prenom": "Alice", "nom": "Lavoie"},
    }).encode("utf-8")
    app_ctx.portail_db.seed("invitations/inv-arch", {
        "id": "inv-arch",
        "type": "documents",
        "email": "done@example.com",
        "display_label": "Archive Close",
        "statut": "traitée",
        "soumissions": [{"batch": "ba", "submitted_at": "2026-11-20T10:00:00+00:00"}],
        "expires_at": datetime(2026, 12, 15, tzinfo=UTC),
        "created_at": datetime(2026, 11, 19, tzinfo=UTC),
        "updated_at": datetime(2026, 11, 20, tzinfo=UTC),
    })

    c = app_ctx.client
    resp = c.get("/reception/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # All 5 filter pills render cleanly.
    for pill in ("Tout", "Documents", "RDV &amp; Transcriptions", "Ouvertures", "Archives / Invitations"):
        assert pill in html

    # All 5 pending feed kinds appear inside ONE chronological descending stream.
    pos_tx = html.index("Appel Teams Récents")
    pos_rdv = html.index("Rencontre Chronologique")
    pos_div = html.index("Consultation Divergente")
    pos_lot = html.index("Dossier 2026-001 (inv-doc1)")
    pos_open = html.index("Ouverture Chronologique")
    assert pos_tx < pos_rdv < pos_div < pos_lot < pos_open

    # Filter pills scope content cleanly.
    doc_html = c.get("/reception/?onglet=documents").get_data(as_text=True)
    assert "Dossier 2026-001 (inv-doc1)" in doc_html
    assert "Appel Teams Récents" not in doc_html

    rdv_html = c.get("/reception/?onglet=rdv").get_data(as_text=True)
    assert "Rencontre Chronologique" in rdv_html
    assert "Dossier 2026-001 (inv-doc1)" not in rdv_html

    ouv_html = c.get("/reception/?onglet=ouvertures").get_data(as_text=True)
    assert "Ouverture Chronologique" in ouv_html
    assert "Rencontre Chronologique" not in ouv_html

    arch_html = c.get("/reception/?onglet=archives").get_data(as_text=True)
    assert "Archive Close" in arch_html
    assert "/reception/invitations/inv-arch/accuse.pdf" in arch_html

    # Fail closed on RDV read failure inside unified feed (amber warning, never empty illusion).
    monkeypatch.setattr(
        reception_routes.rendez_vous,
        "lister_en_attente",
        lambda: (_ for _ in ()).throw(reception_routes.rendez_vous.LectureImpossible("boom")),
    )
    err_html = c.get("/reception/").get_data(as_text=True)
    assert "Lecture des rendez-vous impossible" in err_html


def test_1f_dynamic_folder_endpoint_and_bulk_verser_refuser_actions_lot(app_ctx, monkeypatch):
    _seed_en_client_and_dossier(app_ctx.db, "d-1")
    _seed_en_client_and_dossier(app_ctx.db, "d-2")
    parent, _ = folder_model.create_folder("d-1", "Mandat")
    sub, _ = folder_model.create_folder("d-1", "Correspondance client", parent_folder_id=parent["id"])
    foreign_folder, _ = folder_model.create_folder("d-2", "Autre dossier")

    c = app_ctx.client
    # JSON folder picker lists full hierarchy.
    f_resp = c.get("/reception/dossiers/d-1/dossiers", headers={"Accept": "application/json"})
    assert f_resp.status_code == 200
    payload = f_resp.get_json()
    assert payload["unreadable"] is False
    labels = {o["id"]: o["label"] for o in payload["options"]}
    assert labels[sub["id"]] == "Mandat › Correspondance client"

    # Unreadable folder list fails closed (503).
    with monkeypatch.context() as mp:
        mp.setattr(
            reception_routes,
            "list_dossier_folders",
            lambda _did: (_ for _ in ()).throw(RuntimeError("db down")),
        )
        assert c.get("/reception/dossiers/d-1/dossiers", headers={"Accept": "application/json"}).status_code == 503

    # Single Verser with foreign folder_id refuses fail-closed; valid custom folder_id files there.
    _seed_reception_lot(app_ctx, "inv-single", "b1", dossier_id="d-1")
    bad_single = c.post(
        "/reception/lots/inv-single/b1/fichiers/0/verser",
        data={"dossier_id": "d-1", "folder_id": foreign_folder["id"], "category": "correspondance"},
    )
    assert bad_single.status_code == 302 and "erreur=" in bad_single.headers["Location"]
    ok_single = c.post(
        "/reception/lots/inv-single/b1/fichiers/0/verser",
        data={"dossier_id": "d-1", "folder_id": sub["id"], "category": "correspondance"},
    )
    assert ok_single.status_code == 302 and "message=" in ok_single.headers["Location"]
    assert document_model.list_documents("d-1")[0]["folder_id"] == sub["id"]

    # Batch Verser selected files directly into chosen subfolder.
    _seed_reception_lot(app_ctx, "inv-b1", "b1", dossier_id="d-1")
    resp_bulk = c.post(
        "/reception/lots/verser-lot",
        data={
            "onglet": "tout",
            "dossier_id": "d-1",
            "folder_id": sub["id"],
            "category": "correspondance",
            "file_keys": ["inv-b1:b1:0", "inv-b1:b1:1"],
        },
    )
    assert resp_bulk.status_code == 302
    assert "message=" in resp_bulk.headers["Location"]
    assert "2+fichier" in resp_bulk.headers["Location"]
    docs = document_model.list_documents("d-1")
    assert len(docs) == 3
    assert all(d["folder_id"] == sub["id"] for d in docs)
    assert all(d["category"] == "correspondance" for d in docs)

    # Archiver via actions-lot after all files decided -> closes invitation & archives lot.
    arch_resp = c.post(
        "/reception/actions-lot",
        data={
            "onglet": "tout",
            "bulk_action": "archiver_lot",
            "feed_keys": ["lot:inv-b1:b1"],
        },
    )
    assert arch_resp.status_code == 302
    assert "message=" in arch_resp.headers["Location"]
    assert app_ctx.portail_db.peek("invitations/inv-b1")["statut"] == "traitée"
    assert "archive/inv-b1/b1/manifeste.json" in app_ctx.buckets.portail.objects

    # Partial failure honesty: valid file + unversable .exe file -> never pure success.
    pdf_raw = b"%PDF-1.4 ok"
    _seed_reception_lot(
        app_ctx,
        "inv-mix",
        "b1",
        files=[
            {
                "objet": "submissions/inv-mix/b1/files/001_ok.pdf",
                "name": "ok.pdf",
                "size_declared": len(pdf_raw),
                "size_gcs": len(pdf_raw),
                "content_type": "application/pdf",
                "sha512": hashlib.sha512(pdf_raw).hexdigest(),
                "etat": "reçu",
                "divergence": None,
                "payload": pdf_raw,
            },
            {
                "objet": "submissions/inv-mix/b1/files/002_bad.exe",
                "name": "bad.exe",
                "size_declared": 10,
                "size_gcs": 10,
                "content_type": "application/octet-stream",
                "sha512": hashlib.sha512(b"0123456789").hexdigest(),
                "etat": "reçu",
                "divergence": None,
                "payload": b"0123456789",
            },
        ],
    )
    mix_resp = c.post(
        "/reception/lots/verser-lot",
        data={
            "dossier_id": "d-1",
            "file_keys": ["inv-mix:b1:0", "inv-mix:b1:1"],
        },
    )
    assert mix_resp.status_code == 302
    assert "erreur=" in mix_resp.headers["Location"]

    # Refuse remaining file in bulk, then fail-closed check on archiving before refusing vs after.
    ref_resp = c.post(
        "/reception/lots/refuser-lot",
        data={"file_keys": ["inv-mix:b1:1"]},
    )
    assert ref_resp.status_code == 302
    assert "message=" in ref_resp.headers["Location"]
    man = json.loads(app_ctx.buckets.portail.objects["submissions/inv-mix/b1/manifeste.json"])
    assert man["files"][1]["etat"] == "refusé"

    # Bulk Écarter transcription, Refuser ouverture, Révoquer invitation.
    app_ctx.db.seed("meeting_transcripts/h-e1", {
        "hearing_id": "h-e1", "status": "a_revoir", "etag": "e1",
        "copilot_markdown": "note", "staged_vtt_path": "",
    })
    ec_resp = c.post(
        "/reception/actions-lot",
        data={"bulk_action": "ecarter", "feed_keys": ["transcription:h-e1:e1"]},
    )
    assert "message=" in ec_resp.headers["Location"]
    assert app_ctx.db.peek("meeting_transcripts/h-e1")["status"] == "ecartee"

    app_ctx.portail_db.seed("invitations/inv-rev", {
        "id": "inv-rev", "type": "documents", "email": "rev@example.com",
        "statut": "envoyée", "expires_at": datetime(2026, 12, 15, tzinfo=UTC),
        "created_at": datetime(2026, 11, 20, tzinfo=UTC),
    })
    rv_resp = c.post(
        "/reception/actions-lot",
        data={"bulk_action": "revoquer", "feed_keys": ["inv:inv-rev"]},
    )
    assert "message=" in rv_resp.headers["Location"]
    assert app_ctx.portail_db.peek("invitations/inv-rev")["statut"] == "révoquée"

    # verser_lot fail-closed guards: empty selection, invalid key, unreadable dossier,
    # cross-dossier folder, and uncertain ingest outcome leaving file in quarantine (« reçu »).
    _seed_reception_lot(app_ctx, "inv-unc", "b1", dossier_id="d-1")
    empty_bulk = c.post("/reception/lots/verser-lot", data={"dossier_id": "d-1"})
    assert empty_bulk.status_code == 302 and "erreur=" in empty_bulk.headers["Location"]
    bad_key_bulk = c.post(
        "/reception/lots/verser-lot",
        data={"dossier_id": "d-1", "file_keys": ["../bad:b:0", "onlytwo:parts"]},
    )
    assert bad_key_bulk.status_code == 302 and "erreur=" in bad_key_bulk.headers["Location"]
    foreign_bulk = c.post(
        "/reception/lots/verser-lot",
        data={"dossier_id": "d-1", "folder_id": foreign_folder["id"], "file_keys": ["inv-unc:b1:0"]},
    )
    assert foreign_bulk.status_code == 302 and "erreur=" in foreign_bulk.headers["Location"]
    with monkeypatch.context() as mp:
        mp.setattr(
            reception_routes,
            "get_dossier_strict",
            lambda _did: (_ for _ in ()).throw(RuntimeError("firestore down")),
        )
        unreadable_d = c.post(
            "/reception/lots/verser-lot",
            data={"dossier_id": "d-1", "file_keys": ["inv-unc:b1:0"]},
        )
        assert unreadable_d.status_code == 302 and "erreur=" in unreadable_d.headers["Location"]
    with monkeypatch.context() as mp:
        mp.setattr(reception_routes.concurrency, "is_outcome_uncertain", bool)
        mp.setattr(
            reception_routes,
            "ingest_blob_as_document",
            lambda *a, **kw: (None, ["Enregistrement incertain."]),
        )
        uncertain = c.post(
            "/reception/lots/verser-lot",
            data={"dossier_id": "d-1", "file_keys": ["inv-unc:b1:0"]},
        )
        assert uncertain.status_code == 302 and "erreur=" in uncertain.headers["Location"]
    man_after = json.loads(app_ctx.buckets.portail.objects["submissions/inv-unc/b1/manifeste.json"])
    assert man_after["files"][0]["etat"] == "reçu"


def test_1f_accuse_reception_pdf_content_helvetica_free_and_fail_closed(app_ctx, monkeypatch):
    _seed_cabinet(app_ctx.db)
    _seed_en_client_and_dossier(app_ctx.db)
    pdf_a = b"%PDF-1.4 payload-a"
    sha_a = hashlib.sha512(pdf_a).hexdigest()
    _seed_reception_lot(
        app_ctx,
        "inv-pdf",
        "b-pdf",
        statut="traitée",
        files=[
            {
                "objet": "submissions/inv-pdf/b-pdf/files/001_preuve.pdf",
                "name": "preuve_officielle.pdf",
                "size_declared": len(pdf_a),
                "size_gcs": len(pdf_a),
                "content_type": "application/pdf",
                "sha512": sha_a,
                "etat": "versé",
                "verse_dossier": "2026-001",
                "verse_nom": "preuve_officielle.pdf",
            },
            {
                "objet": "submissions/inv-pdf/b-pdf/files/002_note.txt",
                "name": "rejet.txt",
                "size_declared": 42,
                "size_gcs": 42,
                "content_type": "text/plain",
                "sha512": "ab" * 64,
                "etat": "refusé",
            },
        ],
    )
    # Move manifest to archive/ as real processed lots do.
    app_ctx.buckets.portail.objects["archive/inv-pdf/b-pdf/manifeste.json"] = (
        app_ctx.buckets.portail.objects.pop("submissions/inv-pdf/b-pdf/manifeste.json")
    )

    c = app_ctx.client
    resp = c.get("/reception/invitations/inv-pdf/accuse.pdf")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    raw = resp.data
    assert raw.startswith(b"%PDF-")
    assert b"Helvetica" not in raw

    extracted = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(raw)).pages)
    assert "BORDEREAU D'ACCUSÉ DE RÉCEPTION" in extracted
    assert "Poirier Lavoie Avocats" in extracted
    assert "Jane Doe" in extracted
    assert "preuve_officielle.pdf" in extracted
    assert f"({len(pdf_a)} octets)" in extracted
    assert sha_a.upper() in extracted.replace("\n", "")
    assert "Versé (2026-001)" in extracted
    assert "Refusé" in extracted

    # Opening PDF also works cleanly without Helvetica.
    app_ctx.portail_db.seed("invitations/inv-intpdf", {
        "id": "inv-intpdf",
        "type": "intake",
        "email": "open@example.com",
        "client_name": "Alice Ouverture",
        "display_label": "Ouverture de dossier client",
        "statut": "traitée",
        "soumissions": [{"batch": "ib1", "submitted_at": "2026-11-24T12:00:00+00:00"}],
        "created_at": datetime(2026, 11, 20, tzinfo=UTC),
    })
    app_ctx.buckets.portail.objects["archive/inv-intpdf/ib1/envelope.json"] = json.dumps({
        "type": "intake",
        "submitted_at": "2026-11-24T12:00:00+00:00",
        "donnees": {"nature": "physique", "prenom": "Alice", "nom": "Ouverture"},
        "consentement": {"version_texte": "1", "horodatage": "2026-11-24T12:00:00+00:00"},
    }).encode("utf-8")
    r_int = c.get("/reception/invitations/inv-intpdf/accuse.pdf")
    assert r_int.status_code == 200 and b"Helvetica" not in r_int.data

    # Fail closed on missing -> 404; on Storage archive read failure -> 503; on DB read failure -> 503.
    assert c.get("/reception/invitations/absent/accuse.pdf").status_code == 404
    with monkeypatch.context() as mp:
        mp.setattr(
            reception_routes,
            "_lire_manifeste_archive",
            lambda _inv, _b: (_ for _ in ()).throw(RuntimeError("gcs down")),
        )
        assert c.get("/reception/invitations/inv-pdf/accuse.pdf").status_code == 503
    monkeypatch.setattr(
        pi_model,
        "lire_invitation_strict",
        lambda _id: (_ for _ in ()).throw(RuntimeError("firestore down")),
    )
    assert c.get("/reception/invitations/inv-pdf/accuse.pdf").status_code == 503


def test_1f_standardized_html_emails_preserve_wrapper_and_no_pii_intake(app_ctx):
    common = {
        "display_label": "Dossier 2026-001",
        "display_label_phrase": "2026-001",
        "lien": "https://portail.example/go?t=abc",
        "max_file_mb": 200,
        "date_expiration": "10 décembre 2026",
        "url_secours": "https://portail.example/entree?i=inv1",
        "quand_rdv": "25 novembre 2026 à 14 h 00",
        "quand_fr": "25 novembre 2026 à 14 h 00",
        "quand": "24 novembre 2026 à 15 h 30",
        "duree_minutes": 30,
        "preavis_h": 24,
        "firm_name": "Poirier Lavoie Avocats",
        "firm_phone": "(514) 555-0100",
        "firm_email": "cabinet@exemple.ca",
        "cabinet": {
            "nom": "Me Jason Poirier Lavoie",
            "organisation": "Poirier Lavoie Avocats",
            "adresse_lignes": ["100 rue Notre-Dame"],
            "telephone": "(514) 555-0100",
            "telecopieur": "",
        },
        "client": {
            "nom": "Jane Doe",
            "organisation": "",
            "adresse_lignes": [],
            "telephone": "(514) 555-0199",
            "courriel": "jane@example.com",
        },
        "file_number": "2026-001",
        "dossier": {"court_file_number": "500-17-123456-261", "title": "Doe c. Acme"},
        "fichiers": [{"nom": "piece.pdf", "taille": "10 ko", "sha512": "AB" * 64}],
    }
    templates = (
        ("reception/_invitation_documents.html", "transmettre mes documents"),
        ("reception/_invitation_ouverture.html", "remplir le formulaire d'ouverture"),
        ("reception/_invitation_rdv.html", "choisir un créneau"),
        ("reception/_invitation_rdv_verification.html", "vérifier mon courriel et retenir le créneau"),
        ("reception/_invitation_rdv_gestion.html", "gérer mon rendez-vous"),
        ("reception/_accuse_rdv_public.html", "visioconférence Microsoft Teams"),
        ("reception/_confirmation_rdv_portail.html", "confirmé"),
        ("reception/_annulation_rdv_portail.html", "annulé"),
        ("reception/_refus_rdv_portail.html", "ne sommes malheureusement pas en mesure"),
        ("reception/_accuse_bordereau.html", "ACCUSÉ DE RÉCEPTION"),
        ("reception/_confirmation_intake.html", "ni la formation d'un mandat"),
    )
    with app_ctx.app.app_context():
        for tpl_name, expected in templates:
            rendered = render_template(tpl_name, **common)
            assert "border-top: 4px solid #4F46E5" in rendered, tpl_name
            assert "Poirier Lavoie Avocats" in rendered, tpl_name
            assert expected in rendered, tpl_name
            if tpl_name == "reception/_confirmation_intake.html":
                assert "Jane Doe" not in rendered
                assert "Acme" not in rendered



def test_1i_note_docx_assemble_uses_en_placeholders():
    from datetime import date
    from utils.note_docx import assemble_note_print_values, build_note_context

    note = {
        "id": "note-1",
        "title": "Case Evaluation",
        "category": "stratégie",
        "content": "Confidential analysis body.",
        "created_at": datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc),
    }
    dossier = {"file_number": "2026-042", "title": "Doe v. Roe"}
    firm = {"nom": "Poirier Lavoie"}
    template = {
        "placeholders": ["dossier.reference_interne"],
        "en_placeholders": ["date.aujourdhui", "note.titre", "note.categorie"],
        "en_storage_path": "templates/t-note/en/note.docx",
    }

    ctx_en = build_note_context(
        note, dossier=dossier, firm=firm, today=date(2026, 11, 24), lang="en"
    )
    vals_en = assemble_note_print_values(template, ctx_en, lang="en")
    assert vals_en["date.aujourdhui"] == "November 24, 2026"
    assert vals_en["note.titre"] == "Case Evaluation"
    assert vals_en["note.categorie"] == "Strategy"
    assert "dossier.reference_interne" not in vals_en


def test_1i_generate_modal_template_switch_drops_sticky_en_without_refusal(app_ctx, monkeypatch):
    from models import doc_template

    _seed_cabinet(app_ctx.db)
    _seed_en_client_and_dossier(app_ctx.db, "d-1", client_lang="en")

    en_bytes = _make_docx("EN {{client.nom}} {{custom_en}}")
    _tpl_bi, errs1 = doc_template.create_template(
        io.BytesIO(_make_docx("FR {{client.nom}}")),
        "bilingual_fr.docx",
        len(_make_docx("FR {{client.nom}}")),
        {"name": "Bilingual Template", "category": "correspondance", "kind": "gabarit"},
        "user-1",
        en_file_stream=io.BytesIO(en_bytes),
        en_filename="bilingual_en.docx",
        en_file_size=len(en_bytes),
    )
    assert not errs1

    fr_only_bytes = _make_docx("FR seul {{client.nom}} {{champ_fr}}")
    tpl_fr, errs3 = doc_template.create_template(
        io.BytesIO(fr_only_bytes),
        "fr_only.docx",
        len(fr_only_bytes),
        {"name": "FR Only Template", "category": "correspondance", "kind": "gabarit"},
        "user-1",
    )
    assert not errs3

    c = app_ctx.client
    # Switching from EN template to FR-only template via modal <select> drops cleanly to FR.
    resp_switch = c.get(
        f"/gabarits/generer?template_id={tpl_fr['id']}&dossier_id=d-1&lang=en&switch_template=1"
    )
    assert resp_switch.status_code == 200
    body_switch = resp_switch.get_data(as_text=True)
    assert "Ce gabarit n'a pas de version anglaise." not in body_switch
    assert "champ_fr" in body_switch
    assert 'name="lang" value="fr"' in body_switch

    # Direct request or fixed=1 with lang=en against an FR-only template remains fail-closed.
    resp_direct = c.get(
        f"/gabarits/generer?template_id={tpl_fr['id']}&dossier_id=d-1&lang=en"
    )
    assert resp_direct.status_code == 200
    assert "Ce gabarit n&#39;a pas de version anglaise." in resp_direct.get_data(as_text=True) or (
        "Ce gabarit n'a pas de version anglaise." in resp_direct.get_data(as_text=True)
    )

    resp_fixed = c.get(
        f"/gabarits/generer?template_id={tpl_fr['id']}&dossier_id=d-1&lang=en&switch_template=1&fixed=1"
    )
    assert resp_fixed.status_code == 200
    body_fixed = resp_fixed.get_data(as_text=True)
    assert (
        "Ce gabarit n&#39;a pas de version anglaise." in body_fixed
        or "Ce gabarit n'a pas de version anglaise." in body_fixed
    )


def test_1f_unified_feed_archives_badge_includes_intake_and_rdv_invitations(app_ctx, monkeypatch):
    import re

    _seed_en_client_and_dossier(app_ctx.db, "d-1")

    app_ctx.portail_db.seed("invitations/inv-doc-active", {
        "id": "inv-doc-active",
        "type": "documents",
        "statut": "envoyée",
        "email": "doc@example.com",
        "display_label": "Doc Active",
        "created_at": datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc),
        "dossier_id": "d-1",
    })
    app_ctx.portail_db.seed("invitations/inv-intake-active", {
        "id": "inv-intake-active",
        "type": "intake",
        "statut": "ouverte",
        "email": "intake@example.com",
        "display_label": "Intake Active",
        "created_at": datetime(2026, 11, 24, 11, 0, tzinfo=timezone.utc),
    })
    app_ctx.portail_db.seed("invitations/inv-rdv-active", {
        "id": "inv-rdv-active",
        "type": "rdv",
        "statut": "envoyée",
        "email": "rdv@example.com",
        "display_label": "RDV Active",
        "created_at": datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc),
    })

    html = app_ctx.client.get("/reception/").get_data(as_text=True)
    m = re.search(
        r"Archives / Invitations</span>\s*<span[^>]*>(\d+)</span>",
        html,
    )
    assert m is not None, "Expected non-zero count badge on Archives / Invitations pill"
    assert int(m.group(1)) == 3


def test_1f_actions_lot_refuser_ouverture_strict_read_and_already_closed(app_ctx, monkeypatch):
    from urllib.parse import unquote_plus
    from models import portail_invitation as pi_mod

    _seed_en_client_and_dossier(app_ctx.db, "d-1")

    # Seed one already-closed intake opening and two open ("ouverte") intake openings.
    for inv_id, batch, statut in (
        ("inv-closed", "b1", "traitée"),
        ("inv-open", "b2", "ouverte"),
        ("inv-outage", "b3", "ouverte"),
    ):
        app_ctx.portail_db.seed(f"invitations/{inv_id}", {
            "id": inv_id,
            "type": "intake",
            "statut": statut,
            "email": f"{inv_id}@example.com",
            "display_label": inv_id,
            "created_at": datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc),
            "soumissions": [{"batch": batch}],
        })
        app_ctx.buckets.portail.objects[f"submissions/{inv_id}/{batch}/envelope.json"] = json.dumps({
            "batch": batch,
            "type": "intake",
            "submitted_at": "2026-11-24T11:00:00+00:00",
            "donnees": {"nature": "physique", "prenom": "Jean", "nom": inv_id},
        }).encode("utf-8")

    # 1. Refusing an already-closed opening refuses cleanly without overwriting "traitée".
    r1 = app_ctx.client.post(
        "/reception/actions-lot",
        data={"bulk_action": "refuser_ouverture", "feed_keys": ["ouverture:inv-closed:b1"], "onglet": "tout"},
        follow_redirects=False,
    )
    assert r1.status_code == 302
    r1_loc = unquote_plus(r1.headers["Location"])
    assert "erreur=" in r1_loc
    assert "déjà close" in r1_loc
    assert app_ctx.portail_db.collection("invitations").document("inv-closed").get().to_dict()["statut"] == "traitée"

    # 2. Strict DB outage raises on lire_invitation_strict -> surfaces read error while open item succeeds.
    orig_strict = pi_mod.lire_invitation_strict

    def _boom_strict(inv_id):
        if inv_id == "inv-outage":
            raise RuntimeError("firestore down")
        return orig_strict(inv_id)

    monkeypatch.setattr(pi_mod, "lire_invitation_strict", _boom_strict)
    r2 = app_ctx.client.post(
        "/reception/actions-lot",
        data={
            "bulk_action": "refuser_ouverture",
            "feed_keys": ["ouverture:inv-outage:b3", "ouverture:inv-open:b2"],
            "onglet": "tout",
        },
        follow_redirects=False,
    )
    assert r2.status_code == 302
    loc = unquote_plus(r2.headers["Location"])
    assert "erreur de lecture" in loc
    assert "1 ouverture(s) refusée(s)" in loc
    assert app_ctx.portail_db.collection("invitations").document("inv-open").get().to_dict()["statut"] == "refusée"
    assert app_ctx.portail_db.collection("invitations").document("inv-outage").get().to_dict()["statut"] == "ouverte"


def test_1f_accuse_pdf_long_firm_title_xml_chars_and_multipage_pagination():
    from pypdf import PdfReader
    from utils.accuse_reception_pdf import build_accuse_reception_pdf

    long_firm = (
        "Cabinet Me Tremblay & Associés <Litige> S.E.N.C.R.L. — "
        + "Avocats Montréal & Québec " * 3
    )
    inv = {
        "id": "inv-long-1",
        "type": "documents",
        "statut": "traitée",
        "cree_le": "2026-11-24T12:00:00+00:00",
    }
    lots = [{
        "batch": "b-30",
        "submitted_at": "2026-11-24T13:00:00+00:00",
        "fichiers": [
            {
                "name": f"Piece_{i}_&_<Test>_" + "a" * 35 + ".pdf",
                "size_gcs": 2048 + i,
                "sha512": f"{i:02x}" * 64,
                "etat": "versé" if i % 2 == 0 else "refusé",
                "verse_dossier": "2026-001",
            }
            for i in range(30)
        ],
    }]

    pdf_bytes = build_accuse_reception_pdf(
        inv,
        lots,
        cabinet={"nom": long_firm, "courriel": "info&legal@cabinet.ca"},
        client={"first_name": "Jean & Marie", "last_name": "<Clients>", "email": "jm@example.com"},
        dossier={"title": "Affaire A & B <2026>", "file_number": "2026-001"},
        generated_at=datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc),
    )
    assert pdf_bytes.startswith(b"%PDF-")
    assert b"/Helvetica" not in pdf_bytes
    assert b"NotoSerif" in pdf_bytes

    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) >= 2
    first_text = reader.pages[0].extract_text()
    assert "… — Accusé de réception" in first_text
    assert "Page 1" in first_text
