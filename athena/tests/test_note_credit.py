"""The credit note — lot 3d of « Factures : retard, radiation, intérêts »
(the lawyer's decision 8 of 2026-11-12: a write-off is a price reduction the
client gets a numbered credit note for).

``services/note_credit.py`` fills the « actif » gabarit of kind
« note_credit » for ONE standing radiation and files the .docx where and how
the note d'honoraires files its own (« Mandat › Factures », the invoice link
and the fingerprint). Through the REAL models, fill engine and save over the
shared fake Firestore and fake Cloud Storage, « today » frozen at the one
seam on a day of a fictitious year; every amount is invented.

1. The fill — every placeholder, split runs included, in the document, the
   header and the footer; every other zip entry byte for byte.
2. The filing and the regeneration — the folder, the link, the tag; an
   identical credit note found again, a regeneration or a new version filed
   anew; two radiations never share one; a credit note is no note
   d'honoraires.
3. The refusals — nothing written, the reason logged.
4. The web — the sheet's button, its fragment and its refusals.
5. The kind — designated through the gabarit routes, shown on its page,
   never by the one-shot script.
"""

import io
import os
import pathlib
import re
import sys
import zipfile
from datetime import date, datetime, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import mcp.handlers as handlers
    import models.doc_template as tpl
    import models.invoice as invoice_model
    import routes.admin_ledger as admin_ledger_routes
    import routes.doc_templates as rdt
    import routes.documents as rd
    import routes.dossiers as dossiers_routes
    import routes.invoices as ri
    import routes.parties as parties_routes
    import scripts.designer_gabarits_actifs as script
    import services.note_credit as nc
    from models import folder as folder_model

from flask import Flask  # noqa: E402
from markupsafe import escape  # noqa: E402

from tests import test_compiled_classes as compiled  # noqa: E402
from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402
from tests._frozen_clock import freeze_deadlines  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils import storage_identity  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc
UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"
# Monday 10 March 2031, 11:00 in Montréal: the invoice below, due on 3
# February, is late — the write-off door writes off its whole remainder.
NOW = datetime(2031, 3, 10, 15, 0, tzinfo=UTC)
TODAY = date(2031, 3, 12)
# 750,00 $ of fees, TPS 5 %, TVQ 9,975 % — invented.
SUBTOTAL, GST, QST, TOTAL = 75000, 3750, 7481, 86231
REASON = "Créance jugée irrécouvrable."

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_CT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/></Types>'
)
# Parts the fill must copy byte for byte (Change Impact item 3).
_OTHER_PARTS = {
    "word/styles.xml": (f'<?xml version="1.0"?><w:styles {_W}>'
                        '<w:style w:styleId="Titre"/></w:styles>').encode("utf-8"),
    "docProps/core.xml": b'<?xml version="1.0"?><cp:coreProperties/>',
    "word/media/image1.png": bytes(range(256)) * 3,
}


def _p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


# The number split by Word across two runs of different languages — the
# engine heals it (utils/docx_fill._bridges_placeholder).
_SPLIT_NUMBER = (
    '<w:p><w:r><w:t xml:space="preserve">NOTE DE CRÉDIT </w:t></w:r>'
    '<w:r><w:rPr><w:lang w:val="fr-CA"/></w:rPr><w:t>{{nc.</w:t></w:r>'
    '<w:r><w:rPr><w:lang w:val="en-US"/></w:rPr><w:t>numero}}</w:t></w:r></w:p>'
)
BODY = (
    _SPLIT_NUMBER
    + _p("Date : {{nc.date}}")
    + _p("Facture {{facture.numero}} du {{facture.date}}")
    + _p("Destinataire : {{destinataire.nom_complet}} — dossier "
         "{{dossier.reference_interne}}")
    + _p("Avant taxes {{nc.montant_avant_taxes}} ; TPS {{nc.tps}} ; "
         "TVQ {{nc.tvq}} ; total {{nc.total}}")
    + _p("Crédit accordé : {{nc.total}}")
    + _p("Solde de la facture : {{facture.solde_restant}}")
    + _p("Motif : {{nc.motif}}")
    + _p("TPS n° {{facture.tps_numero}} — TVQ n° {{facture.tvq_numero}}")
)
HEADER = "{{cabinet.nom}} — {{nc.numero}}"
FOOTER = "Note de crédit {{nc.numero}}, facture {{facture.numero}}"


def _docx(body: str = BODY, *, header: str = HEADER, footer: str = FOOTER) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CT)
        zf.writestr("word/document.xml",
                    f'<?xml version="1.0"?><w:document {_W}><w:body>{body}'
                    "</w:body></w:document>")
        zf.writestr("word/header1.xml",
                    f'<?xml version="1.0"?><w:hdr {_W}>{_p(header)}</w:hdr>')
        zf.writestr("word/footer1.xml",
                    f'<?xml version="1.0"?><w:ftr {_W}>{_p(footer)}</w:ftr>')
        for name, data in _OTHER_PARTS.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


def _invoice(**over) -> dict:
    """An issued invoice of a fictitious year, overdue on NOW."""
    doc = {
        "id": "i1", "invoice_number": "2031-F042", "status": "en_retard",
        "dossier_id": "d1", "dossier_file_number": "2031-009",
        "dossier_title": "Exemple c. Témoin",
        "client_id": "p1", "client_name": "Cliente Exemple",
        "billing_address": {"name": "Cliente Exemple", "street": "1 rue Fictive",
                            "unit": "", "city": "Laval", "province": "Québec",
                            "postal_code": "H0H 0H0"},
        "date": datetime(2031, 1, 3, tzinfo=UTC),
        "due_date": datetime(2031, 2, 3, tzinfo=UTC),
        "subtotal_fees": SUBTOTAL, "subtotal_expenses": 0, "subtotal": SUBTOTAL,
        "gst_rate": 500, "gst_amount": GST, "qst_rate": 9975, "qst_amount": QST,
        "total": TOTAL, "retainer_applied": 0, "amount_due": TOTAL,
        "amount_paid": 0, "gst_number": "123456789 RT0001",
        "qst_number": "1234567890 TQ0001",
        "created_at": datetime(2031, 1, 3, tzinfo=UTC),
        "updated_at": datetime(2031, 1, 3, tzinfo=UTC), "etag": "inv-e0",
    }
    doc.update(over)
    return doc


def _template(data: bytes, name: str = "Note de crédit") -> dict:
    created, errors = tpl.create_template(
        io.BytesIO(data), "note_credit.docx", len(data),
        {"name": name, "category": "correspondance", "kind": "note_credit"}, UID)
    assert errors == [], errors
    return created


def _designate(template: dict) -> dict:
    designated, errors = tpl.set_active_template(
        template["id"], par="juriste", expected_etag=None)
    assert errors == [], errors
    return designated


def _write_off(db) -> dict:
    report, errors = invoice_model.write_off_remainder(
        "i1", expected_etag=db.peek("invoices/i1")["etag"], reason=REASON)
    assert errors == [], errors
    return report["write_off"]


@pytest.fixture
def store(monkeypatch):
    freeze_deadlines(monkeypatch, NOW)
    db = install(monkeypatch, *_fake_modules())
    bucket = FakeBucket()
    monkeypatch.setattr(tpl.storage, "bucket", lambda: bucket)
    monkeypatch.setattr(nc, "cabinet_dict", lambda: {"nom": "Me Témoin Exemple"})
    db.seed("dossiers/d1", {"id": "d1", "file_number": "2031-009",
                            "title": "Exemple c. Témoin", "clients": [],
                            "client_ids": [], "status": "actif"})
    db.seed("parties/p1", {"id": "p1", "type": "individual",
                           "contact_role": "client", "first_name": "Cliente",
                           "last_name": "Exemple"})
    db.seed("invoices/i1", _invoice())
    template = _designate(_template(_docx()))
    radiation = _write_off(db)
    db.reset_logs()
    return db, bucket, template, radiation


@pytest.fixture
def events(monkeypatch):
    seen: list = []
    monkeypatch.setattr(nc, "log_template_event",
                        lambda event, **kw: seen.append((event, kw)))
    return seen


def _generate(radiation_id: str, **kw):
    kw.setdefault("resolve_uid", lambda: UID)
    kw.setdefault("today", TODAY)
    return nc.generer_note_credit("i1", radiation_id, **kw)


def _documents(db) -> dict:
    return db.peek_collection("documents")


def _stored_zip(db, bucket, document_id: str) -> dict:
    data = bucket.blob(db.peek(f"documents/{document_id}")["storage_path"]
                       ).download_as_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def _text(xml: bytes) -> str:
    return re.sub(r"<[^>]+>", "", xml.decode("utf-8"))


# ══════════════════════════════════════════════════════════════════════
# 1. The fill
# ══════════════════════════════════════════════════════════════════════


def test_every_placeholder_fills_and_every_other_part_is_byte_identical(
        store, events):
    db, bucket, template, radiation = store
    assert radiation["credit_note_number"] == "2031-NC001"
    assert (radiation["amount"], radiation["gst_amount"],
            radiation["qst_amount"]) == (TOTAL, GST, QST)

    note = _generate(radiation["id"])

    parts = _stored_zip(db, bucket, note.document["id"])
    with zipfile.ZipFile(io.BytesIO(_docx())) as zf:
        source = {name: zf.read(name) for name in zf.namelist()}
    # No new part, none lost — and every part but the three filled ones is
    # the gabarit's, byte for byte.
    assert list(parts) == list(source)
    filled = ("word/document.xml", "word/header1.xml", "word/footer1.xml")
    for name in source:
        if name not in filled:
            assert parts[name] == source[name], name
    for name in filled:
        assert b"{{" not in parts[name] and b"}}" not in parts[name], name
    body = _text(parts["word/document.xml"])
    assert "NOTE DE CRÉDIT 2031-NC001" in body          # the split run healed
    assert "Date : 10 mars 2031" in body
    assert "Facture 2031-F042 du 3 janvier 2031" in body
    assert "Destinataire : Cliente Exemple — dossier 2031-009" in body
    assert (f"Avant taxes {format_cents_fr(SUBTOTAL)} ; TPS {format_cents_fr(GST)} "
            f"; TVQ {format_cents_fr(QST)} ; total {format_cents_fr(TOTAL)}") in body
    assert body.count(format_cents_fr(TOTAL)) == 2        # every occurrence
    assert f"Solde de la facture : {format_cents_fr(0)}" in body
    assert f"Motif : {REASON}" in body
    assert "TPS n° 123456789 RT0001 — TVQ n° 1234567890 TQ0001" in body
    assert _text(parts["word/header1.xml"]) == "Me Témoin Exemple — 2031-NC001"
    assert _text(parts["word/footer1.xml"]) == (
        "Note de crédit 2031-NC001, facture 2031-F042")
    assert events == [("document_generated", {
        "template_id": template["id"], "dossier_id": "d1",
        "saved_document_id": note.document["id"], "source": "note_credit",
        "invoice_id": "i1", "write_off_id": radiation["id"]})]


def test_a_radiation_that_claims_no_tax_prints_its_whole_amount_before_taxes(
        store):
    db, bucket, _template, radiation = store
    _report, errors = invoice_model.reverse_write_off(
        "i1", radiation["id"], expected_etag=db.peek("invoices/i1")["etag"])
    assert errors == [], errors
    report, errors = invoice_model.write_off_remainder(
        "i1", expected_etag=db.peek("invoices/i1")["etag"], claim_tax=False)
    assert errors == [], errors
    second = report["write_off"]
    assert second["credit_note_number"] == "2031-NC002"

    note = _generate(second["id"])

    body = _text(_stored_zip(db, bucket, note.document["id"])["word/document.xml"])
    assert (f"Avant taxes {format_cents_fr(TOTAL)} ; TPS {format_cents_fr(0)} ; "
            f"TVQ {format_cents_fr(0)} ; total {format_cents_fr(TOTAL)}") in body
    assert "Motif : " in body and REASON not in body     # no reason: nothing


# ══════════════════════════════════════════════════════════════════════
# 2. The filing and the regeneration
# ══════════════════════════════════════════════════════════════════════


def test_the_credit_note_is_filed_beside_the_note_dhonoraires(store):
    db, _bucket, template, radiation = store

    note = _generate(radiation["id"])

    assert note.reused is False and note.write_off["id"] == radiation["id"]
    (doc_id, stored), = _documents(db).items()
    assert doc_id == note.document["id"]
    assert stored["source_invoice_id"] == "i1"
    assert stored["generation_fingerprint"] == note.fingerprint
    assert stored["tags"] == ["note_credit"]
    assert stored["category"] == "correspondance"
    assert stored["storage_path"].startswith(f"users/{UID}/dossiers/d1/")
    assert stored["display_name"] == (
        "2031-009 - 2031-03-12 - Projet Note de crédit 2031-NC001")
    assert stored["genere_depuis"] == (
        "Générée depuis la radiation de la facture 2031-F042 (note de crédit "
        "2031-NC001)")
    # « Mandat › Factures », by its role — the note d'honoraires' folder.
    assert stored["folder_id"] == folder_model.system_folder_id("d1", "factures")
    assert note.folder["system_role"] == folder_model.SYSTEM_ROLE_FACTURES
    assert note.template["id"] == template["id"]


def test_an_identical_credit_note_is_found_again_never_filed_twice(store, events):
    db, bucket, _template, radiation = store
    first = _generate(radiation["id"])
    objects = len(bucket.objects)
    events.clear()

    again = _generate(radiation["id"])

    assert again.reused is True and again.folder is None
    assert again.document["id"] == first.document["id"]
    assert len(_documents(db)) == 1 and len(bucket.objects) == objects
    assert events == []


def test_regenerate_files_a_new_credit_note(store):
    db, _bucket, _template, radiation = store
    first = _generate(radiation["id"])

    again = _generate(radiation["id"], regenerate=True)

    assert again.reused is False
    assert again.document["id"] != first.document["id"]
    assert len(_documents(db)) == 2


def test_a_new_template_version_files_a_new_credit_note(store):
    db, _bucket, template, radiation = store
    first = _generate(radiation["id"])
    data = _docx(BODY + _p("Merci."))
    _updated, errors, changed = tpl.update_template(
        template["id"], {}, io.BytesIO(data), "note_credit.docx", len(data),
        expected_version=template["version"])
    assert errors == [] and changed

    again = _generate(radiation["id"])

    assert again.reused is False and again.fingerprint != first.fingerprint
    assert len(_documents(db)) == 2


def test_another_dossiers_document_is_never_the_answer(store):
    db, _bucket, _template, radiation = store
    first = _generate(radiation["id"])
    moved = {**db.peek(f"documents/{first.document['id']}"), "dossier_id": "d9"}
    db.external_write(f"documents/{first.document['id']}", moved)

    again = _generate(radiation["id"])

    assert again.reused is False
    assert len(_documents(db)) == 2


def test_two_radiations_never_share_a_credit_note(store):
    """A gabarit that prints no number: two credit notes of one invoice
    print the same values — the fingerprint's subject (the radiation) still
    tells them apart."""
    db, _bucket, template, radiation = store
    data = _docx(_p("Facture {{facture.numero}}"), header="", footer="")
    _updated, errors, _changed = tpl.update_template(
        template["id"], {}, io.BytesIO(data), "note_credit.docx", len(data),
        expected_version=template["version"])
    assert errors == []
    first = _generate(radiation["id"])
    _report, errors = invoice_model.reverse_write_off(
        "i1", radiation["id"], expected_etag=db.peek("invoices/i1")["etag"])
    assert errors == []
    second = _write_off(db)

    again = _generate(second["id"])

    assert again.reused is False
    assert again.fingerprint != first.fingerprint
    assert len(_documents(db)) == 2


def test_a_credit_note_is_no_note_dhonoraires(store):
    """It shares the invoice link (source_invoice_id): the connector's
    « a note d'honoraires is filed » sets it apart by its own tag — and
    counts every other document of the link, as it always did."""
    db, _bucket, _template, radiation = store
    assert handlers._note_on_file("i1") is False
    note = _generate(radiation["id"])
    assert handlers._note_on_file("i1") is False
    untagged = {**db.peek(f"documents/{note.document['id']}"), "id": "autre",
                "tags": []}
    db.external_write("documents/autre", untagged)
    assert handlers._note_on_file("i1") is True


# ══════════════════════════════════════════════════════════════════════
# 3. The refusals — nothing written, not even « Factures »
# ══════════════════════════════════════════════════════════════════════


def _cancel(db, radiation):
    _report, errors = invoice_model.reverse_write_off(
        "i1", radiation["id"], expected_etag=db.peek("invoices/i1")["etag"])
    assert errors == [], errors


def _boom(*_a, **_kw):
    raise RuntimeError("firestore indisponible")


def _undesignate(db, template):
    _doc, errors = tpl.clear_active_template(template["id"], expected_etag=None)
    assert errors == [], errors


@pytest.mark.parametrize("case, reason", [
    ("cancelled", "write_off_cancelled"),
    ("contre_passation", "write_off_not_found"),
    ("unknown_event", "write_off_not_found"),
    ("no_template", "no_credit_note_template"),
    ("template_unreadable", "template_read_failed"),
    ("voided", "invoice_voided"),
    ("unknown_invoice", "invoice_not_found"),
    ("invoice_unreadable", "invoice_unreadable"),
    ("events_unreadable", "write_offs_unreadable"),
    ("amount_unreadable", "montant_illisible"),
    ("dossier_unreadable", "dossier_unreadable"),
    ("lookup_failed", "generated_lookup_failed"),
    ("file_missing", "template_file_unavailable"),
    ("no_uid", "save_failed"),
])
def test_a_refusal_writes_nothing_and_says_why(store, events, monkeypatch,
                                               case, reason):
    db, _bucket, template, radiation = store
    event_id, invoice_id, kw = radiation["id"], "i1", {}
    stored = db.peek("invoices/i1")
    if case == "cancelled":
        _cancel(db, radiation)
    elif case == "contre_passation":
        _cancel(db, radiation)
        event_id = db.peek("invoices/i1")["write_offs"][1]["id"]
    elif case == "unknown_event":
        event_id = "absente"
    elif case == "no_template":
        _undesignate(db, template)
    elif case == "template_unreadable":
        def _unreadable(kind):
            raise tpl.TemplateReadError(kind)
        monkeypatch.setattr(nc, "get_active_template", _unreadable)
    elif case == "voided":
        db.external_write("invoices/i1", {**stored, "status": "annulée"})
    elif case == "unknown_invoice":
        invoice_id = "absente"
    elif case == "invoice_unreadable":
        monkeypatch.setattr(nc, "get_invoice_strict", _boom)
    elif case == "events_unreadable":
        db.external_write("invoices/i1", {**stored, "write_offs": [{"id": "x"}]})
    elif case == "amount_unreadable":
        db.external_write("invoices/i1", {**stored, "amount_paid": "huit cents"})
    elif case == "dossier_unreadable":
        monkeypatch.setattr(nc, "get_dossier_strict", _boom)
    elif case == "lookup_failed":
        monkeypatch.setattr(nc, "find_generated_for_invoice", _boom)
    elif case == "file_missing":
        monkeypatch.setattr(nc, "template_file_bytes", lambda record: None)
    elif case == "no_uid":
        def _no_uid():
            raise storage_identity.StorageIdentityUnavailable()
        kw["resolve_uid"] = _no_uid
    db.reset_logs()
    events.clear()

    with pytest.raises(nc.NoteRefusee) as caught:
        kw.setdefault("resolve_uid", lambda: UID)
        nc.generer_note_credit(invoice_id, event_id, today=TODAY, **kw)

    assert caught.value.reason == reason
    assert "huit cents" not in caught.value.message
    assert _documents(db) == {}
    assert db.peek_collection(folder_model.COLLECTION) == {}
    assert [e for e, _ in events] == ["generation_failed"]
    assert events[0][1]["reason"] == reason
    assert events[0][1]["source"] == "note_credit"


def test_the_refusals_say_what_to_do_in_french(store):
    db, _bucket, template, radiation = store
    _undesignate(db, template)
    with pytest.raises(nc.NoteRefusee) as caught:
        _generate(radiation["id"])
    assert caught.value.message == (
        "Aucun gabarit « Note de crédit » n'est désigné comme actif : "
        "désignez-en un dans Gabarits.")
    _designate(template)
    _cancel(db, radiation)
    with pytest.raises(nc.NoteRefusee) as caught:
        _generate(radiation["id"])
    assert caught.value.message.startswith(
        "Cette radiation a été annulée : sa note de crédit 2031-NC001 est "
        "annulée")


def test_the_service_never_reads_the_browser_session():
    import ast
    tree = ast.parse((_ATHENA / "services" / "note_credit.py")
                     .read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
              for a in n.names}
    assert "session" not in names and "request_uid" not in names


# ══════════════════════════════════════════════════════════════════════
# 4. The web — the sheet's button
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def web(store):
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=lambda v: v,
    )
    for bp in (ri.invoices_bp, rd.documents_bp, admin_ledger_routes.admin_bp,
               dossiers_routes.dossiers_bp, parties_routes.parties_bp,
               rdt.doc_templates_bp, rdt.modeles_bp):
        app.register_blueprint(bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = UID
        s["email"] = "juriste@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return client


def _url(radiation: dict) -> str:
    return f"/factures/i1/radiation/{radiation['id']}/note-de-credit"


def _sheet(web) -> str:
    resp = web.get("/factures/i1")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_a_standing_radiation_offers_the_button_a_cancelled_one_does_not(
        store, web):
    db, _bucket, _template, radiation = store
    html = _sheet(web)
    action = f'action="{_url(radiation)}"'
    assert action in html and f'hx-post="{_url(radiation)}"' in html
    form = html[html.index(action):html.index("</form>", html.index(action))]
    assert 'name="csrf_token" value="tok"' in form
    assert "Générer la note de crédit" in form
    assert 'hx-target="#note-credit-1"' in form and 'id="note-credit-1"' in html
    _cancel(db, radiation)
    html = _sheet(web)
    assert "/note-de-credit" not in html and "Générer la note de crédit" not in html


def test_an_unreadable_amount_offers_no_button(store, web):
    db, _bucket, _template, _radiation = store
    db.external_write("invoices/i1", {**db.peek("invoices/i1"),
                                      "amount_paid": "huit cents"})
    assert "/note-de-credit" not in _sheet(web)


def test_the_button_files_then_finds_again_and_links_to_the_document(store, web):
    db, _bucket, _template, radiation = store
    resp = web.post(_url(radiation), headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    (doc_id, _stored), = _documents(db).items()
    assert "Note de crédit 2031-NC001 générée" in body
    assert f'href="/documents/{doc_id}"' in body
    assert "sous « Mandat › Factures »" in body

    resp = web.post(_url(radiation), headers={"HX-Request": "true"})
    body = resp.get_data(as_text=True)
    assert "Note de crédit 2031-NC001 déjà versée au dossier" in body
    assert f'href="/documents/{doc_id}"' in body
    assert len(_documents(db)) == 1


def test_a_refusal_is_said_at_200_to_htmx_and_by_redirect_without_it(store, web):
    db, _bucket, template, radiation = store
    _undesignate(db, template)
    resp = web.post(_url(radiation), headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert str(escape(nc.NO_ACTIVE_NOTE_CREDIT)) in resp.get_data(as_text=True)

    resp = web.post(_url(radiation))
    assert resp.status_code == 302
    loc = urlparse(resp.location)
    assert loc.path == "/factures/i1"
    assert parse_qs(loc.query)["erreur"] == [nc.NO_ACTIVE_NOTE_CREDIT]
    assert _documents(db) == {}


def test_without_javascript_the_document_opens(store, web):
    db, _bucket, _template, radiation = store
    resp = web.post(_url(radiation))
    (doc_id, _stored), = _documents(db).items()
    assert resp.status_code == 302
    assert urlparse(resp.location).path == f"/documents/{doc_id}"


def test_the_button_uses_compiled_classes_and_a_44_px_target(store, web):
    """Change Impact item 6 and Rule 9: py-3 around a text-sm label."""
    page = _sheet(web)
    main = page[page.index("<main"):page.index("</main>")]
    tag = re.search(r'<button type="submit"\s+class="([^"]*)">\s*Générer la note '
                    r'de crédit', main)
    assert tag
    classes = set(tag.group(1).split())
    assert {"py-3", "text-sm"} <= classes
    css = compiled._artifact(_ATHENA / "static" / "vendor").read_text(encoding="utf-8")
    selectors = compiled.compiled_selectors(css)
    assert sorted(c for c in classes if not compiled.is_compiled(c, selectors)) == []


# ══════════════════════════════════════════════════════════════════════
# 5. The kind — designated through the gabarit routes
# ══════════════════════════════════════════════════════════════════════


def test_the_kind_is_designated_through_the_gabarit_routes(store, web):
    db, _bucket, designated, _radiation = store
    # A second credit note template, designated from its page: it takes the
    # designation from the first — one « actif » per kind.
    other = _template(_docx(), name="Note de crédit (bis)")
    page = web.get(f"/gabarits/{other['id']}").get_data(as_text=True)
    assert "Note de crédit (radiation)" in page
    assert "les « Note de crédit » utilisent « Note de crédit »" in " ".join(
        page.split())
    resp = web.post(f"/gabarits/{other['id']}/activer",
                    data={"expected_etag": other["etag"]})
    assert resp.status_code == 302
    assert parse_qs(urlparse(resp.location).query)["message"] == [
        "Ce gabarit est désormais le gabarit actif des « Note de crédit »."]
    assert db.peek(f"doc_templates/{other['id']}")["active_for"] == "note_credit"
    assert "active_for" not in db.peek(f"doc_templates/{designated['id']}")
    assert tpl.get_active_template("note_credit")["id"] == other["id"]
    # The list says which one is « Actif ».
    rows = web.get("/gabarits/", headers={"HX-Request": "true"}).get_data(
        as_text=True)
    assert rows.count("Note de crédit (radiation)") == 2 and rows.count(">Actif<") == 1


def test_the_page_names_the_flow_fields_apart(store, web):
    _db, _bucket, template, _radiation = store
    page = " ".join(web.get(f"/gabarits/{template['id']}").get_data(
        as_text=True).split())
    flow = page.split("Remplis depuis la radiation et sa facture", 1)[1].split(
        "</div>", 1)[0]
    for name in ("nc.numero", "nc.total", "facture.numero", "facture.solde_restant"):
        assert f">{name}<" in flow, name
    assert "À compléter dans Word" not in page          # nothing left to Word


def test_the_form_offers_the_kind_and_the_refusal_names_it():
    assert "Note de crédit (radiation)" in tpl.KIND_LABELS.values()
    assert tpl.NOT_SPECIAL_ERROR == (
        "Seuls les gabarits « Note d'honoraires », « Note de crédit » et "
        "« Note (impression) » se désignent comme gabarit actif.")
    assert set(rdt.FLOW_SOURCES_FR) == set(tpl.SPECIAL_KINDS)


def test_the_new_kind_form_preselects_it(web):
    page = web.get("/gabarits/new?kind=note_credit").get_data(as_text=True)
    assert re.search(r'value="note_credit"\s+checked', page)
    assert "Générer la note de crédit" in " ".join(page.split())


def test_the_one_shot_script_never_designates_a_credit_note_by_recency():
    """The designation script (lot 2A) reproduced the PRE-T3 recency rule
    for the kinds production then selected by recency: a kind added since
    never had that rule — only the lawyer designates it."""
    credit = {"id": "c1", "kind": "note_credit", "name": "Note de crédit",
              "updated_at": NOW}
    lines = script.plan([credit])
    assert [line["kind"] for line in lines] == list(script.LEGACY_KINDS)
    assert "note_credit" not in script.LEGACY_KINDS
    assert set(script.LEGACY_KINDS) < set(tpl.SPECIAL_KINDS)
