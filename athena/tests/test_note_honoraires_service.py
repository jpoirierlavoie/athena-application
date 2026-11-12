"""La note d'honoraires — UNE génération, pour le web et le connecteur
(plan, lot 3a, étape 2).

``services/note_honoraires.py`` est la génération sortie de
``routes/invoices.invoice_note_docx``, qui n'en est plus qu'un adaptateur.
Tout passe ici par les VRAIS modèles, le vrai moteur de remplissage et la
vraie sauvegarde (``services.gabarits.save_generated``) au-dessus du faux
Firestore partagé et du faux Cloud Storage : ce qu'on relit est ce que le
MAGASIN contient.

Quatre familles :

1. **La génération** — la note se classe dans « Mandat › Factures » (dans
   « Projets » jusqu'à l'arborescence par défaut, 2026-09-30), sous l'uid
   obtenu par la garde, liée à sa facture par ``source_invoice_id`` et
   l'empreinte de ce qu'elle imprime.
2. **La déduplication** — une note identique (même gabarit, même version,
   mêmes valeurs imprimées) est RENDUE au lieu d'être refaite ; un
   changement de ce que la note imprime, ou une nouvelle version du
   gabarit, en fait une nouvelle ; ``regenerate`` en force une ; un
   changement que la note n'imprime pas (le statut) ne compte pas.
3. **Les refus** — rien n'est écrit, pas même le dossier « Factures » :
   facture annulée, lignes illisibles sous un sous-total non nul (le
   lecteur STRICT), aucun gabarit désigné, désignation illisible, uid
   introuvable, recherche des notes déjà générées illisible.
4. **Le web** — le bouton garde son comportement (une note par clic, sous
   l'uid de la session), et le refus d'une lecture de lignes ratée est DIT.
   Ce dernier test échouait sur la route d'avant (vérifié en la
   rétablissant) : son lecteur avalait la panne et classait une note aux
   tableaux VIDES sous des totaux complets — un document client d'aspect
   achevé.
"""

import ast
import io
import os
import pathlib
import sys
import zipfile
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import models.doc_template as tpl
    import models.document as document_model
    import models.invoice as invoice_model
    import routes.documents as rd
    import routes.invoices as ri
    import services.note_honoraires as nh
    from models import folder as folder_model

from defusedxml import ElementTree as ET  # noqa: E402
from flask import Flask  # noqa: E402
from markupsafe import escape  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402
from tests._frozen_clock import freeze_deadlines  # noqa: E402
from utils import storage_identity  # noqa: E402
from utils.format_fr import format_cents_fr, format_cents_fr_parens  # noqa: E402

UTC = timezone.utc
UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"
TODAY = date(2026, 9, 28)
_CT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/></Types>'
)
TEMPLATE_TEXT = "Note {{facture.numero}} — total {{facture.total_apres_taxes}}"


def _docx(text: str) -> bytes:
    body = (
        '<?xml version="1.0"?><w:document xmlns:w="http://schemas.'
        'openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r>'
        f'<w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CT)
        zf.writestr("word/document.xml", body)
    return buf.getvalue()


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


def _invoice(**over) -> dict:
    doc = {
        "id": "i1", "invoice_number": "2026-F031", "status": "envoyée",
        "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. Lavoie",
        "client_id": "p1", "client_name": "Jean Tremblay",
        "billing_address": {"name": "Jean Tremblay", "street": "1 rue A",
                            "unit": "", "city": "Montréal",
                            "province": "Québec", "postal_code": "H1A 1A1"},
        "date": datetime(2026, 9, 1, tzinfo=UTC),
        "due_date": datetime(2026, 10, 1, tzinfo=UTC),
        "subtotal_fees": 45000, "subtotal_expenses": 0, "subtotal": 45000,
        "gst_rate": 500, "gst_amount": 2250, "qst_rate": 9975,
        "qst_amount": 4489, "total": 51739, "retainer_applied": 0,
        "amount_due": 51739, "amount_paid": 0,
        "gst_number": "123456789 RT0001", "qst_number": "1234567890 TQ0001",
        "created_at": datetime(2026, 9, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 1, tzinfo=UTC), "etag": "inv-e0",
    }
    doc.update(over)
    return doc


@pytest.fixture
def store(monkeypatch):
    db = install(monkeypatch, *_fake_modules())
    bucket = FakeBucket()
    monkeypatch.setattr(tpl.storage, "bucket", lambda: bucket)
    monkeypatch.setattr(nh, "cabinet_dict", lambda: {})
    db.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001",
                            "title": "Tremblay c. Lavoie", "clients": [],
                            "client_ids": [], "status": "actif"})
    db.seed("parties/p1", {"id": "p1", "type": "individual",
                           "contact_role": "client", "first_name": "Jean",
                           "last_name": "Tremblay"})
    db.seed("invoices/i1", _invoice())
    db.seed("invoices/i1/lineitems/li1", {
        "id": "li1", "type": "fee", "source_id": "e1",
        "date": datetime(2026, 8, 20, tzinfo=UTC),
        "description": "Rédaction", "hours": 1.5, "rate": 30000,
        "amount": 45000, "taxable": True,
    })
    data = _docx(TEMPLATE_TEXT)
    template, errors = tpl.create_template(
        io.BytesIO(data), "note.docx", len(data),
        {"name": "Note d'honoraires", "category": "correspondance",
         "kind": "note_honoraires"}, UID)
    assert errors == [], errors
    designated, errors = tpl.set_active_template(
        template["id"], par="juriste", expected_etag=None)
    assert errors == [], errors
    db.reset_logs()
    return db, bucket, designated


@pytest.fixture
def events(monkeypatch):
    seen: list = []
    monkeypatch.setattr(nh, "log_template_event",
                        lambda event, **kw: seen.append((event, kw)))
    return seen


def _generate(**kw):
    kw.setdefault("resolve_uid", lambda: UID)
    kw.setdefault("today", TODAY)
    return nh.generer_note_honoraires("i1", **kw)


def _documents(db) -> dict:
    return db.peek_collection("documents")


def _nothing_written(db, bucket_names_before=None) -> None:
    assert _documents(db) == {}
    assert db.peek_collection(folder_model.COLLECTION) == {}


def _stored_text(bucket, storage_path: str) -> str:
    data = bucket.blob(storage_path).download_as_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read("word/document.xml").decode("utf-8")


# ══════════════════════════════════════════════════════════════════════
# 1. La génération
# ══════════════════════════════════════════════════════════════════════


def test_a_note_is_filed_in_factures_linked_to_its_invoice(store, events):
    db, bucket, template = store

    note = _generate()

    assert note.reused is False
    (doc_id, stored), = _documents(db).items()
    assert doc_id == note.document["id"]
    assert stored["source_invoice_id"] == "i1"
    assert stored["generation_fingerprint"] == note.fingerprint
    assert len(note.fingerprint) == 64
    assert stored["storage_path"].startswith(f"users/{UID}/dossiers/d1/")
    assert "unknown" not in stored["storage_path"]
    assert stored["category"] == "correspondance"
    assert stored["tags"] == ["note_honoraires"]
    assert stored["dossier_file_number"] == "2026-001"
    assert stored["genere_depuis"] == "Générée depuis la facture 2026-F031"
    assert stored["display_name"] == (
        "2026-001 - 2026-09-28 - Projet Note d'honoraires 2026-F031")
    # « Mandat › Factures » — created with its parent on first use, at their
    # deterministic ids; nothing else of the tree, and never « Projets ».
    assert stored["folder_id"] == folder_model.system_folder_id("d1", "factures")
    folder = db.peek(f"{folder_model.COLLECTION}/{stored['folder_id']}")
    assert folder["system_role"] == folder_model.SYSTEM_ROLE_FACTURES
    assert folder["parent_folder_id"] == folder_model.system_folder_id("d1", "mandat")
    mandat = db.peek(f"{folder_model.COLLECTION}/{folder['parent_folder_id']}")
    assert mandat["system_role"] == folder_model.SYSTEM_ROLE_MANDAT
    assert mandat["parent_folder_id"] is None
    assert {f["system_role"] for f in
            db.peek_collection(folder_model.COLLECTION).values()} == {
        folder_model.SYSTEM_ROLE_MANDAT, folder_model.SYSTEM_ROLE_FACTURES}
    text = _stored_text(bucket, stored["storage_path"])
    assert "Note 2026-F031" in text and "517,39" in text
    assert [e for e, _ in events] == ["document_generated"]
    assert events[0][1]["invoice_id"] == "i1"
    assert events[0][1]["source"] == "facture"


def test_the_default_uid_is_the_owners_obtained_by_the_guard(store, monkeypatch):
    """A caller with no browser session (the connector, lot 3b) files under
    the authorized user's uid — never a fallback."""
    db, _bucket, _template = store
    monkeypatch.setattr(storage_identity, "owner_uid", lambda: UID)

    note = nh.generer_note_honoraires("i1", today=TODAY)

    assert db.peek(f"documents/{note.document['id']}")["storage_path"].startswith(
        f"users/{UID}/")


def test_the_note_fills_from_the_version_its_record_names(store, monkeypatch):
    """The bytes filled are those of the template record read — never a
    re-read that could print version N+1 under the fingerprint of N."""
    db, bucket, template = store
    seen = []
    real = nh.template_file_bytes

    def _spy(record):
        seen.append((record["id"], record.get("version")))
        return real(record)

    monkeypatch.setattr(nh, "template_file_bytes", _spy)
    _generate()
    assert seen == [(template["id"], template["version"])]


# ══════════════════════════════════════════════════════════════════════
# 2. La déduplication — sur ce que la note IMPRIME
# ══════════════════════════════════════════════════════════════════════


def test_an_identical_note_is_returned_not_filed_twice(store, events):
    db, bucket, _template = store
    first = _generate()
    objects = len(bucket.objects)
    events.clear()

    again = _generate()

    assert again.reused is True
    assert again.document["id"] == first.document["id"]
    assert again.fingerprint == first.fingerprint
    assert len(_documents(db)) == 1
    assert len(bucket.objects) == objects          # no second file
    assert events == []                             # nothing done, nothing said


def test_the_caller_is_named_in_the_provenance_and_the_note_says_what_it_printed(
    store,
):
    """Lot 3b: the connector passes generated_by — the « par Claude
    (connecteur) » its INSTRUCTIONS promise every document it generates
    carries — and reads back the invoice printed and the folder the note
    landed in (none on a reuse: the filed document names its own)."""
    db, _bucket, _template = store
    note = _generate(generated_by="par Claude (connecteur)")
    stored = _documents(db)[note.document["id"]]
    assert stored["genere_depuis"] == (
        "Générée depuis la facture 2026-F031 par Claude (connecteur)")
    assert note.invoice["invoice_number"] == "2026-F031"
    assert note.folder["system_role"] == "factures"
    assert stored["folder_id"] == note.folder["id"]

    again = _generate()
    assert again.reused is True and again.folder is None
    assert again.invoice["id"] == "i1"


def test_a_reused_note_stays_where_it_is_filed(store):
    """A note filed in « Projets » before the default tree (a legacy root
    folder) is still FOUND and returned — the lookup never looks at
    folders —, and stays where it is: nothing refiled, no folder written."""
    db, bucket, _template = store
    first = _generate()
    db.external_write(f"{folder_model.COLLECTION}/legacy-projets", {
        "id": "legacy-projets", "dossier_id": "d1", "name": "Projets",
        "parent_folder_id": None, "order": 0,
        "created_at": datetime(2026, 1, 5, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 5, tzinfo=UTC)})
    stored = db.peek(f"documents/{first.document['id']}")
    db.external_write(f"documents/{first.document['id']}",
                      {**stored, "folder_id": "legacy-projets"})
    folders_before = db.peek_collection(folder_model.COLLECTION)
    objects = len(bucket.objects)
    db.reset_logs()

    again = _generate()

    assert again.reused is True and again.folder is None
    assert again.document["id"] == first.document["id"]
    assert again.document["folder_id"] == "legacy-projets"
    assert db.peek(f"documents/{first.document['id']}")["folder_id"] == (
        "legacy-projets")
    assert db.peek_collection(folder_model.COLLECTION) == folders_before
    assert len(_documents(db)) == 1 and len(bucket.objects) == objects
    assert db.commits == []


def test_a_note_refuses_when_factures_cannot_be_obtained(
    store, events, monkeypatch,
):
    """The folders cannot be read: the note is REFUSED
    (``factures_unavailable``, the folder model's own reason) — never filed
    at the dossier root, nothing written."""
    db, _bucket, _template = store
    real = db._fake_server.run_query
    refused: list = []

    def _run_query(request, metadata=None, **kw):
        sq = request["structured_query"]._pb
        if any(f.collection_id == folder_model.COLLECTION for f in sq.from_):
            refused.append(request["parent"])
            raise RuntimeError("firestore indisponible")
        return real(request, metadata=metadata, **kw)

    monkeypatch.setattr(db._fake_server, "run_query", _run_query)

    refusal = _refused("factures_unavailable")

    assert refused                      # the folder read is what failed
    assert refusal.message == folder_model.READ_ERROR
    _nothing_written(db)
    assert [e for e, _ in events] == ["generation_failed"]
    assert events[0][1]["reason"] == "factures_unavailable"


def test_regenerate_files_a_new_note(store):
    db, _bucket, _template = store
    first = _generate()

    again = _generate(regenerate=True)

    assert again.reused is False
    assert again.document["id"] != first.document["id"]
    assert len(_documents(db)) == 2


def test_a_change_the_note_prints_makes_a_new_note(store):
    db, _bucket, _template = store
    first = _generate()
    db.external_write("invoices/i1", _invoice(total=60000, amount_due=60000))

    again = _generate()

    assert again.reused is False
    assert again.fingerprint != first.fingerprint
    assert len(_documents(db)) == 2


def test_a_change_the_note_does_not_print_is_not_a_new_note(store):
    """The invoice's status (and its updated_at) moves; the note prints
    neither — the same note is the answer."""
    db, _bucket, _template = store
    first = _generate()
    db.external_write("invoices/i1", _invoice(
        status="en_retard", updated_at=datetime(2026, 9, 20, tzinfo=UTC),
        etag="inv-e1"))

    again = _generate()

    assert again.reused is True and again.document["id"] == first.document["id"]


def test_a_new_template_version_makes_a_new_note(store):
    """A replaced letterhead is a new version — the old note no longer
    matches what the note d'honoraires looks like today."""
    db, _bucket, template = store
    first = _generate()
    data = _docx(TEMPLATE_TEXT + " ")
    _updated, errors, changed = tpl.update_template(
        template["id"], {}, io.BytesIO(data), "note.docx", len(data),
        expected_version=template["version"])
    assert errors == [] and changed

    again = _generate()

    assert again.reused is False
    assert again.fingerprint != first.fingerprint


def test_a_note_of_another_dossier_is_never_the_answer(store):
    db, _bucket, _template = store
    first = _generate()
    moved = {**db.peek(f"documents/{first.document['id']}"), "dossier_id": "d9"}
    db.external_write(f"documents/{first.document['id']}", moved)

    again = _generate()

    assert again.reused is False
    assert len(_documents(db)) == 2


def test_the_fingerprint_covers_template_version_values_rows_and_conditions():
    ctx = SimpleNamespace(rows={"ligne_honoraire": [{"h.date": "1er"}]},
                          conditions={"si_debours": False})
    base = nh.fill_fingerprint({"id": "t1", "version": 2}, {"a": "1"}, ctx)
    assert base == nh.fill_fingerprint({"id": "t1", "version": 2}, {"a": "1"}, ctx)
    for other in (
        nh.fill_fingerprint({"id": "t2", "version": 2}, {"a": "1"}, ctx),
        nh.fill_fingerprint({"id": "t1", "version": 3}, {"a": "1"}, ctx),
        nh.fill_fingerprint({"id": "t1", "version": 2}, {"a": "2"}, ctx),
        nh.fill_fingerprint({"id": "t1", "version": 2}, {"a": "1"},
                            SimpleNamespace(rows={"ligne_honoraire": []},
                                            conditions={"si_debours": False})),
        nh.fill_fingerprint({"id": "t1", "version": 2}, {"a": "1"},
                            SimpleNamespace(rows=ctx.rows,
                                            conditions={"si_debours": True})),
    ):
        assert other != base


# ══════════════════════════════════════════════════════════════════════
# 3. Les refus — rien n'est écrit, pas même « Factures »
# ══════════════════════════════════════════════════════════════════════


def _refused(reason: str, **kw) -> nh.NoteRefusee:
    with pytest.raises(nh.NoteRefusee) as caught:
        _generate(**kw)
    assert caught.value.reason == reason
    return caught.value


def test_a_voided_invoice_is_refused(store, events):
    db, _bucket, _template = store
    db.external_write("invoices/i1", _invoice(status="annulée"))
    refusal = _refused("invoice_voided")
    assert refusal.message == nh.INVOICE_VOIDED
    _nothing_written(db)
    assert events == [("generation_failed", {"reason": "invoice_voided"})]


def test_an_unknown_invoice_is_refused(store):
    db, _bucket, _template = store
    with pytest.raises(nh.NoteRefusee) as caught:
        nh.generer_note_honoraires("absente", resolve_uid=lambda: UID)
    assert caught.value.reason == "invoice_not_found"
    _nothing_written(db)


def test_line_items_missing_under_a_non_zero_subtotal_are_refused(store, events):
    """Absent, not unreadable: the void's own rule (``line_items_missing``)
    — the note would print totals over empty tables."""
    db, _bucket, _template = store
    db.external_delete("invoices/i1/lineitems/li1")
    refusal = _refused("line_items_unreadable")
    assert refusal.message == nh.LINE_ITEMS_UNREADABLE
    _nothing_written(db)


def test_a_zero_invoice_without_lines_is_generated(store):
    db, _bucket, _template = store
    db.external_delete("invoices/i1/lineitems/li1")
    db.external_write("invoices/i1", _invoice(
        subtotal_fees=0, subtotal=0, gst_amount=0, qst_amount=0, total=0,
        amount_due=0))
    note = _generate()
    assert note.reused is False and len(_documents(db)) == 1


def _fail_line_item_reads(db, monkeypatch) -> list:
    """Every query of a ``lineitems`` subcollection fails at the SERVER;
    every other read still works. Returns the list of refused queries —
    a test asserts it is not empty, so the simulation cannot pass by
    failing something else."""
    real = db._fake_server.run_query
    refused: list = []

    def _run_query(request, metadata=None, **kw):
        sq = request["structured_query"]._pb
        if any(f.collection_id == "lineitems" for f in sq.from_):
            refused.append(request["parent"])
            raise RuntimeError("firestore indisponible")
        return real(request, metadata=metadata, **kw)

    monkeypatch.setattr(db._fake_server, "run_query", _run_query)
    return refused


def test_a_failed_line_item_read_is_refused_never_filled_empty(
    store, events, monkeypatch,
):
    db, _bucket, _template = store
    refused = _fail_line_item_reads(db, monkeypatch)
    refusal = _refused("invoice_unreadable")
    assert refusal.message == nh.INVOICE_UNREADABLE
    assert refused                      # the line-item read is what failed
    _nothing_written(db)


def _fail_first_dossier_read(db, monkeypatch) -> list:
    """The FIRST read of ``dossiers/d1`` fails at the SERVER — the
    generation's own read of the invoice's dossier; every later read (the
    « Projets » system folder's) works again. Returns the refused reads."""
    real = db._fake_server.batch_get_documents
    refused: list = []

    def _batch_get(request, metadata=None, **kw):
        names = [str(n) for n in request["documents"]]
        if not refused and any(n.endswith("/dossiers/d1") for n in names):
            refused.append(names)
            raise RuntimeError("firestore indisponible")
        return real(request, metadata=metadata, **kw)

    monkeypatch.setattr(db._fake_server, "batch_get_documents", _batch_get)
    return refused


def test_a_failed_dossier_read_is_refused_never_filled_with_gaps(
    store, events, monkeypatch,
):
    """Régression (revue du lot 3a, étape 2) — le service lisait le dossier
    par ``get_dossier``, qui avale la panne en ``None`` : la note se
    remplissait alors de « [CHAMP MANQUANT : …] » à chaque champ du dossier
    (numéro de cour, district, intitulé…), se classait quand même — la
    lecture suivante, celle du dossier « Projets », réussissant — et
    l'appel rapportait un succès. Une lecture ratée REFUSE désormais."""
    db, _bucket, _template = store
    refused = _fail_first_dossier_read(db, monkeypatch)

    refusal = _refused("dossier_unreadable")

    assert refused                      # the dossier read is what failed
    assert refusal.message == nh.DOSSIER_UNREADABLE
    _nothing_written(db)
    assert [e for e, _ in events] == ["generation_failed"]
    assert events[0][1]["invoice_id"] == "i1"


def test_the_web_says_a_failed_dossier_read_and_files_nothing(
    store, web, monkeypatch,
):
    db, _bucket, _template = store
    refused = _fail_first_dossier_read(db, monkeypatch)

    resp = web.post("/factures/i1/note-docx", headers={"HX-Request": "true"})

    assert refused
    assert resp.status_code == 200
    assert str(escape(nh.DOSSIER_UNREADABLE)) in resp.get_data(as_text=True)
    _nothing_written(db)


def test_no_designated_template_names_the_fix(store, events):
    db, _bucket, template = store
    cleared, errors = tpl.clear_active_template(
        template["id"], expected_etag=None)
    assert errors == [], errors
    refusal = _refused("no_note_template")
    assert refusal.message == nh.NO_ACTIVE_NOTE_HONORAIRES
    _nothing_written(db)
    assert events == [("generation_failed", {"reason": "no_note_template"})]


def test_an_unreadable_designation_says_retry(store, monkeypatch):
    db, _bucket, _template = store

    def unreadable(kind):
        raise tpl.TemplateReadError(kind)

    monkeypatch.setattr(nh, "get_active_template", unreadable)
    refusal = _refused("template_read_failed")
    assert refusal.message == nh.NOTE_TEMPLATE_UNREADABLE
    _nothing_written(db)


def test_an_unobtainable_owner_uid_writes_nothing(store, events, monkeypatch):
    db, _bucket, _template = store

    def _no_owner():
        raise storage_identity.StorageIdentityUnavailable()

    monkeypatch.setattr(storage_identity, "owner_uid", _no_owner)
    with pytest.raises(nh.NoteRefusee) as caught:
        nh.generer_note_honoraires("i1", today=TODAY)
    assert caught.value.reason == "save_failed"
    assert caught.value.message == storage_identity.UNAVAILABLE_MESSAGE
    _nothing_written(db)
    assert events[-1][0] == "generation_failed"


def test_a_placeholder_uid_is_refused_by_the_guard(store):
    db, _bucket, _template = store
    refusal = _refused("save_failed", resolve_uid=lambda: "unknown")
    assert refusal.message == storage_identity.INVALID_UID_MESSAGE
    _nothing_written(db)


def test_an_unreadable_lookup_refuses_rather_than_duplicating(store, monkeypatch):
    db, _bucket, _template = store
    _generate()

    def _boom(invoice_id):
        raise RuntimeError("firestore indisponible")

    monkeypatch.setattr(nh, "find_generated_for_invoice", _boom)
    refusal = _refused("generated_lookup_failed")
    assert refusal.message == nh.GENERATED_LOOKUP_FAILED
    assert len(_documents(db)) == 1


@pytest.mark.usefixtures("store")          # the fake store, installed
def test_regenerate_skips_the_lookup(monkeypatch):
    monkeypatch.setattr(nh, "find_generated_for_invoice",
                        lambda iid: pytest.fail("lookup read on regenerate"))
    note = _generate(regenerate=True)
    assert note.reused is False


# ══════════════════════════════════════════════════════════════════════
# 4. Le modèle de document : le lien, et la recherche stricte
# ══════════════════════════════════════════════════════════════════════


def test_the_invoice_link_travels_only_by_its_keyword(store):
    """A form (or a caller's metadata) cannot pose it: an unknown metadata
    key is ignored by the whitelist; the keyword refuses a bad shape."""
    db, _bucket, _template = store
    data = _docx("x")
    doc, errors = document_model.upload_document(
        "d1", "2026-001", io.BytesIO(data), "lettre.docx", len(data),
        {"display_name": "Lettre", "source_invoice_id": "i1",
         "generation_fingerprint": "0" * 64}, UID)
    assert errors == [], errors
    stored = db.peek(f"documents/{doc['id']}")
    assert "source_invoice_id" not in stored
    assert "generation_fingerprint" not in stored

    for bad in ({"invoice_id": "i1"}, {"invoice_id": "i1", "fingerprint": "zz"},
                {"invoice_id": "a/b", "fingerprint": "0" * 64},
                {"invoice_id": "i1", "fingerprint": "A" * 64, "x": 1}):
        doc, errors = document_model.upload_document(
            "d1", "2026-001", io.BytesIO(data), "lettre.docx", len(data),
            {"display_name": "Lettre"}, UID, generated_from_invoice=bad)
        assert doc is None
        assert errors == [document_model._GENERATED_FROM_INVOICE_ERROR]


def test_find_generated_for_invoice_propagates_a_read_failure(store, monkeypatch):
    db, _bucket, _template = store

    def _boom(*_a, **_kw):
        raise RuntimeError("firestore indisponible")

    monkeypatch.setattr(db._fake_server, "run_query", _boom)
    with pytest.raises(RuntimeError):
        document_model.find_generated_for_invoice("i1")
    assert document_model.find_generated_for_invoice("a/b") == []


# ══════════════════════════════════════════════════════════════════════
# 5. Le web — même comportement, et le refus DIT
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def web(store):
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", csp_nonce="n")
    app.register_blueprint(ri.invoices_bp)
    app.register_blueprint(rd.documents_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = UID
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return client


def test_the_web_says_where_the_note_landed(store, web):
    """The fragment names the folder the SERVICE filed the note in —
    « Mandat › Factures » — where it used to say « Projets » whatever
    happened."""
    resp = web.post("/factures/i1/note-docx", headers={"HX-Request": "true"})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "enregistrée dans le dossier, sous « Mandat › Factures »." in body
    assert "Projets »" not in body


def test_the_note_location_follows_the_filed_folder(store):
    """The route derives the path from the folder, never from a name of its
    own: a note (re)found in a legacy root « Projets » says « Projets »; the
    parent's name leads when it can be read; no folder at all is the root."""
    db, _bucket, _template = store
    note = _generate()
    doc = note.document
    assert ri._note_location(doc, note.folder) == {
        "folder_path": "Mandat › Factures", "at_root": False}
    # A reused note reports no folder: its own folder_id is read.
    assert ri._note_location(doc, None)["folder_path"] == "Mandat › Factures"
    db.external_write(f"{folder_model.COLLECTION}/legacy-projets", {
        "id": "legacy-projets", "dossier_id": "d1", "name": "Projets",
        "parent_folder_id": None, "order": 0})
    legacy = {**doc, "folder_id": "legacy-projets"}
    assert ri._note_location(legacy, None) == {
        "folder_path": "Projets", "at_root": False}
    # The parent unreadable (gone): the folder's name alone.
    orphan = {**note.folder, "parent_folder_id": "absent"}
    assert ri._note_location(doc, orphan)["folder_path"] == "Factures"
    assert ri._note_location({**doc, "folder_id": None}, None) == {
        "folder_path": "", "at_root": True}


def test_the_web_button_still_files_one_note_per_click(store, web):
    db, _bucket, _template = store
    for _ in range(2):
        resp = web.post("/factures/i1/note-docx", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "Note d&#39;honoraires" in resp.get_data(as_text=True)
    docs = list(_documents(db).values())
    assert len(docs) == 2
    assert all(d["storage_path"].startswith(f"users/{UID}/") for d in docs)
    assert {d["source_invoice_id"] for d in docs} == {"i1"}


def test_the_web_says_a_failed_line_item_read_and_files_nothing(
    store, web, monkeypatch,
):
    """Régression — la route d'avant lisait les lignes par le lecteur qui
    avale la panne, puis classait une note aux tableaux vides sous des
    totaux complets. Elle refuse, et le dit (200 : htmx n'échange qu'un
    2xx)."""
    db, _bucket, _template = store
    refused = _fail_line_item_reads(db, monkeypatch)

    resp = web.post("/factures/i1/note-docx", headers={"HX-Request": "true"})

    assert refused                      # the line-item read is what failed

    assert resp.status_code == 200
    assert str(escape(nh.INVOICE_UNREADABLE)) in resp.get_data(as_text=True)
    _nothing_written(db)


# ══════════════════════════════════════════════════════════════════════
# 6. Balayages : une seule génération, sans session
# ══════════════════════════════════════════════════════════════════════


def _called_names(path: pathlib.Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            names.add(fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", ""))
    return names


def test_the_route_no_longer_fills_nor_saves_a_note():
    called = _called_names(_ATHENA / "routes" / "invoices.py")
    for writer in ("fill_docx", "upload_document", "save_generated",
                   "ensure_system_folder", "build_invoice_context",
                   "get_active_template", "get_note_honoraires_template"):
        assert writer not in called, writer
    assert "generer_note_honoraires" in called


def test_the_service_never_reads_the_browser_session():
    tree = ast.parse((_ATHENA / "services" / "note_honoraires.py")
                     .read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id != "session"
        if isinstance(node, ast.Attribute):
            assert node.attr != "session"
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [getattr(node, "module", "") or ""] + [a.name for a in node.names]
            assert not any(m.split(".")[0] == "flask" for m in modules)


# ══════════════════════════════════════════════════════════════════════
# 7. Le lot « incertain » (2026-11-06) — chacun échoue sur 25f0592
# ══════════════════════════════════════════════════════════════════════


def test_a_note_whose_save_is_unknown_says_to_look_in_factures(
    store, events, monkeypatch,
):
    """La réponse de l'enregistrement s'est perdue et sa relecture échoue :
    la note est PEUT-ÊTRE versée. L'ancien service répondait ``save_failed``
    « Erreur lors du téléversement. Veuillez réessayer. » — et le bouton,
    qui verse une note à chaque clic, en versait une seconde. La phrase
    envoie d'abord le juriste à « Mandat › Factures »."""
    from tests._ingest_faults import lose_answer_then_unreadable

    db, _bucket, _template = store
    lose_answer_then_unreadable(db, monkeypatch)

    with pytest.raises(nh.NoteRefusee) as exc:
        _generate(regenerate=True)

    assert exc.value.reason == "save_uncertain"
    assert "« Mandat › Factures »" in exc.value.message
    assert "n'a pas pu être confirmé" in exc.value.message
    assert "réessayez" not in exc.value.message.lower()
    assert [(e, kw.get("reason")) for e, kw in events] == [
        ("generation_failed", "save_uncertain")]
    assert len(_documents(db)) == 1      # it landed: « Factures » shows it


def test_the_web_button_says_where_to_look_before_another_click(
    store, web, monkeypatch,
):
    """Le bouton garde sa règle — une note par clic, choix du lot : la
    phrase, montrée telle quelle (200 : htmx n'échange qu'un 2xx), dit de
    vérifier « Mandat › Factures » avant de cliquer de nouveau, jamais
    « réessayez »."""
    from html import unescape

    from tests._ingest_faults import lose_answer_then_unreadable

    db, _bucket, _template = store
    lose_answer_then_unreadable(db, monkeypatch)

    resp = web.post("/factures/i1/note-docx", headers={"HX-Request": "true"})

    assert resp.status_code == 200
    body = unescape(resp.get_data(as_text=True))
    assert "n'a pas pu être confirmé" in body
    assert "Vérifiez « Mandat › Factures »" in body
    assert "réessayez" not in body.lower()


# ══════════════════════════════════════════════════════════════════════
# 7. Les paiements imputés (2026-11-08)
# ══════════════════════════════════════════════════════════════════════
# Un gabarit qui IMPRIME les paiements (un champ p.* ou
# facture.solde_restant) les lit — strictement — et refuse quand ils ne
# mènent pas au solde ; tout autre gabarit ne lit rien et garde le contexte,
# donc l'empreinte, qu'il a toujours eus.


def _paragraph(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _cell(text: str) -> str:
    return f"<w:tc>{_paragraph(text)}</w:tc>"


def _docx_body(body_xml: str) -> bytes:
    body = (
        '<?xml version="1.0"?><w:document xmlns:w="http://schemas.'
        'openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        f'{body_xml}</w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CT)
        zf.writestr("word/document.xml", body)
    return buf.getvalue()


# Le bloc des totaux d'une note, réduit à trois rangées : la rangée des
# paiements tient la place de celle des « Avances en fidéicommis ».
_TOTALS = (
    "<w:tbl><w:tblPr/>"
    "<w:tr>" + _cell("TOTAL APRÈS TAXES") + _cell("{{facture.total_apres_taxes}}") + "</w:tr>"
    "<w:tr>" + _cell("{{#ligne_paiement}}{{p.date}} — {{p.source}}")
    + _cell("{{p.montant}}") + "</w:tr>"
    "<w:tr>" + _cell("SOLDE") + _cell("{{facture.solde_restant}}") + "</w:tr>"
    "</w:tbl>"
)


def _print_payments(store) -> dict:
    """Le gabarit désigné reçoit ce bloc — une NOUVELLE version, comme
    l'avocat la téléverserait ; la désignation suit le gabarit."""
    _db, _bucket, template = store
    data = _docx_body(_paragraph("Note {{facture.numero}}") + _TOTALS)
    updated, errors, changed = tpl.update_template(
        template["id"], {}, io.BytesIO(data), "note.docx", len(data),
        expected_version=template["version"])
    assert errors == [] and changed
    assert "p.montant" in updated["placeholders"]
    return updated


def _receipt(rid: str, amount: int, day: int, **extra) -> dict:
    row = {"id": rid, "kind": "encaissement_facture", "direction": "recette",
           "status": "compensée", "amount": amount, "method": "virement",
           "date": datetime(2026, 9, day, tzinfo=UTC), "sequence": day,
           "invoice_id": "i1", "invoice_number": "2026-F031"}
    row.update(extra)
    return row


def _seed_payments(db) -> None:
    """Un paiement d'honoraires du fidéicommis et un virement debout, un
    chèque contre-passé (sans provision) — et la facture qui porte leur
    cumul, comme l'écrivent les registres (517,39 − 450,00 = 67,39 $)."""
    db.external_write("admin_transactions/a1",
                      _receipt("a1", 30000, 10, trust_transaction_id="t1"))
    db.external_write("admin_transactions/a2",
                      _receipt("a2", 6739, 12, method="chèque", reversed_by_id="a9"))
    db.external_write("admin_transactions/a3", _receipt("a3", 15000, 15))
    db.external_write("invoices/i1", _invoice(
        amount_paid=45000, paid_date=datetime(2026, 9, 15, tzinfo=UTC)))


def _fail_admin_reads(db, monkeypatch) -> list:
    """Toute requête du registre d'administration échoue au SERVEUR ; le
    reste se lit. Rend les requêtes refusées (un test vérifie qu'il y en a)."""
    real = db._fake_server.run_query
    refused: list = []

    def _run_query(request, metadata=None, **kw):
        sq = request["structured_query"]._pb
        if any(f.collection_id == "admin_transactions" for f in sq.from_):
            refused.append(request["parent"])
            raise RuntimeError("firestore indisponible")
        return real(request, metadata=metadata, **kw)

    monkeypatch.setattr(db._fake_server, "run_query", _run_query)
    return refused


def test_a_note_that_prints_the_payments_lists_the_standing_ones(store, events):
    db, bucket, _template = store
    _print_payments(store)
    _seed_payments(db)

    note = _generate()

    text = _stored_text(bucket, db.peek(f"documents/{note.document['id']}")["storage_path"])
    assert "10 septembre 2026 — Fidéicommis" in text
    assert "15 septembre 2026 — Virement" in text
    assert "(300,00)" in text and "(150,00)" in text
    assert "Chèque" not in text and "(67,39)" not in text   # contre-passé : jamais imprimé
    assert "SOLDE" in text and "67,39" in text               # le solde restant
    assert "{{" not in text
    assert text.count("<w:tr>") == 4                         # total + 2 paiements + solde
    assert note.counts["rows_paiement"] == 2
    assert events[-1][0] == "document_generated"
    assert events[-1][1]["rows_paiement"] == 2


def test_a_note_that_prints_no_payment_never_reads_them(store, monkeypatch):
    """Le gabarit de toujours (aucun champ de paiement) : aucune lecture du
    registre, et l'empreinte se calcule sur les MÊMES clés qu'avant — une
    note déjà classée reste la réponse du connecteur."""
    db, _bucket, _template = store
    _seed_payments(db)
    monkeypatch.setattr(nh, "list_invoice_receipts_strict",
                        lambda iid: pytest.fail("payments read for a template that prints none"))
    seen = []
    real = nh.fill_fingerprint

    def _spy(template, values, ctx):
        seen.append((set(ctx.rows), set(ctx.conditions)))
        return real(template, values, ctx)

    monkeypatch.setattr(nh, "fill_fingerprint", _spy)

    note = _generate()

    assert note.reused is False and "rows_paiement" not in note.counts
    assert seen == [({"ligne_honoraire", "ligne_debours_tx", "ligne_debours_ntx"},
                     {"si_honoraires", "si_debours_tx", "si_debours_ntx"})]


def test_a_failed_payments_read_is_refused_never_printed_as_unpaid(
    store, events, monkeypatch,
):
    db, _bucket, _template = store
    template = _print_payments(store)
    _seed_payments(db)
    refused = _fail_admin_reads(db, monkeypatch)

    refusal = _refused("payments_unreadable")

    assert refused                      # the register read is what failed
    assert refusal.message == nh.PAYMENTS_UNREADABLE
    _nothing_written(db)
    assert events == [("generation_failed", {
        "reason": "payments_unreadable", "template_id": template["id"],
        "invoice_id": "i1"})]


def test_payments_that_do_not_lead_to_the_balance_are_refused(store, events):
    """Le registre et la facture ont dérivé (450,00 $ debout, 300,00 $
    inscrits sur la facture) : les lignes imprimées ne mèneraient pas au
    solde imprimé."""
    db, _bucket, _template = store
    _print_payments(store)
    _seed_payments(db)
    db.external_write("invoices/i1", _invoice(amount_paid=30000))

    refusal = _refused("payments_mismatch")

    assert refusal.message == nh.PAYMENT_DEFECT_MESSAGES["payments_mismatch"]
    _nothing_written(db)
    assert events[-1][1]["reason"] == "payments_mismatch"


def test_a_hand_set_payee_with_a_balance_refuses_only_a_note_that_prints_payments(
    store,
):
    """« payée » posée à la main, rien d'inscrit : le gabarit de toujours
    l'imprime comme avant (une donnée ancienne ne verrouille rien) ; celui
    qui imprime les paiements refuserait d'afficher un solde dû sous ce
    statut, et dit quoi faire."""
    db, _bucket, _template = store
    db.external_write("invoices/i1", _invoice(status="payée"))
    assert _generate().reused is False

    _print_payments(store)
    refusal = _refused("paid_by_hand")

    assert refusal.message == nh.PAYMENT_DEFECT_MESSAGES["paid_by_hand"]
    assert len(_documents(db)) == 1     # only the note of the former template


def test_a_new_payment_makes_a_new_note_and_none_reuses_it(store):
    db, _bucket, _template = store
    _print_payments(store)
    _seed_payments(db)
    first = _generate()
    assert _generate().reused is True

    db.external_write("admin_transactions/a4",
                      _receipt("a4", 6739, 22, method="comptant"))
    db.external_write("invoices/i1", _invoice(
        status="payée", amount_paid=51739,
        paid_date=datetime(2026, 9, 22, tzinfo=UTC)))

    again = _generate()

    assert again.reused is False
    assert again.fingerprint != first.fingerprint
    assert again.counts["rows_paiement"] == 3


# ── Lot 3a: the write-off, a forgiveness SHOWN TO THE CLIENT (D2) ────────
# Through the model's own doors on the fake store, the clock frozen on a
# Monday of a fictitious year: the invoice, due on 3 February 2031, is late
# on 10 March; 150,00 $ came in on 20 February, the rest is written off.

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
WRITE_OFF_DAY = date(2031, 3, 10)


def _table(text: str) -> list[list[str]]:
    """The document's table, row by row, each cell's text."""
    return [["".join(t.text or "" for t in cell.iter(f"{_W}t"))
             for cell in row.iter(f"{_W}tc")]
            for row in ET.fromstring(text).iter(f"{_W}tr")]


def _overdue_with_a_payment(db, monkeypatch) -> None:
    freeze_deadlines(monkeypatch, datetime(2031, 3, 10, 15, tzinfo=UTC))
    db.external_write("admin_transactions/a5", _receipt(
        "a5", 15000, 20, date=datetime(2031, 2, 20, tzinfo=UTC),
        invoice_number="2031-F031"))
    db.external_write("invoices/i1", _invoice(
        invoice_number="2031-F031", status="en_retard",
        date=datetime(2031, 1, 3, tzinfo=UTC),
        due_date=datetime(2031, 2, 3, tzinfo=UTC),
        amount_paid=15000, paid_date=datetime(2031, 2, 20, tzinfo=UTC)))


def _write_off(db) -> dict:
    report, errors = invoice_model.write_off_remainder(
        "i1", expected_etag=db.peek("invoices/i1")["etag"])
    assert errors == [], errors
    return report["write_off"]


def _printed(db, bucket, note) -> str:
    data = bucket.blob(
        db.peek(f"documents/{note.document['id']}")["storage_path"]
    ).download_as_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # String substitution only (Change Impact item 3): no new part, and
        # every other entry byte for byte.
        assert zf.namelist() == ["[Content_Types].xml", "word/document.xml"]
        assert zf.read("[Content_Types].xml").decode("utf-8") == _CT
        return zf.read("word/document.xml").decode("utf-8")


def test_a_written_off_invoice_prints_its_radiation_and_a_zero_balance(
    store, monkeypatch,
):
    """The radiation is a row of the template's own payment row, after the
    payment it followed: TOTAL APRÈS TAXES less the rows gives the balance,
    which reaches zero — the note never refuses a written-off invoice
    (payments_print_defect accepts it). Since lot 3b the row names the
    credit note the door numbered (« Note de crédit 2031-NC001 »)."""
    db, bucket, _template = store
    _print_payments(store)
    _overdue_with_a_payment(db, monkeypatch)
    radiation = _write_off(db)
    assert radiation["amount"] == 51739 - 15000
    assert radiation["credit_note_number"] == "2031-NC001"

    note = _generate(today=WRITE_OFF_DAY)

    assert note.invoice["status"] == "payée"
    assert note.counts["rows_paiement"] == 2
    assert _table(_printed(db, bucket, note)) == [
        ["TOTAL APRÈS TAXES", format_cents_fr(51739)],
        ["20 février 2031 — Virement", format_cents_fr_parens(15000)],
        ["10 mars 2031 — Note de crédit 2031-NC001", format_cents_fr_parens(36739)],
        ["SOLDE", format_cents_fr(0)],
    ]


def test_a_cancelled_radiation_prints_neither_row(store, monkeypatch):
    """Cancelled, the radiation and its cancellation both leave the note:
    the invoice reopens « en_retard » and its balance is owing again."""
    db, bucket, _template = store
    _print_payments(store)
    _overdue_with_a_payment(db, monkeypatch)
    radiation = _write_off(db)
    _report, errors = invoice_model.reverse_write_off(
        "i1", radiation["id"], expected_etag=db.peek("invoices/i1")["etag"])
    assert errors == [], errors

    note = _generate(today=WRITE_OFF_DAY)

    text = _printed(db, bucket, note)
    assert note.invoice["status"] == "en_retard"
    assert note.counts["rows_paiement"] == 1
    assert "Radiation" not in text and "Note de crédit" not in text
    assert _table(text) == [
        ["TOTAL APRÈS TAXES", format_cents_fr(51739)],
        ["20 février 2031 — Virement", format_cents_fr_parens(15000)],
        ["SOLDE", format_cents_fr(36739)],
    ]


# ══════════════════════════════════════════════════════════════════════
# 8. Le solde en fidéicommis (2026-11-08)
# ══════════════════════════════════════════════════════════════════════
# {{facture.solde_fideicommis}} : le solde AU REGISTRE du client dans le
# dossier de la facture, au moment de la génération — un retrait encore en
# circulation compris, si bien qu'un paiement d'honoraires tout juste
# inscrit est déjà déduit.


def _print_trust_balance(store, extra: str = "") -> dict:
    """Le gabarit désigné imprime le solde (et, au besoin, *extra*)."""
    _db, _bucket, template = store
    data = _docx_body(
        _paragraph("Note {{facture.numero}}") + extra
        + _paragraph("Solde en fidéicommis : {{facture.solde_fideicommis}}"))
    updated, errors, changed = tpl.update_template(
        template["id"], {}, io.BytesIO(data), "note.docx", len(data),
        expected_version=template["version"])
    assert errors == [] and changed
    return updated


def _dossier(**over) -> dict:
    doc = {"id": "d1", "file_number": "2026-001", "title": "Tremblay c. Lavoie",
           "clients": [], "client_ids": ["p1"], "status": "actif",
           "trust_balance_by_client": {}, "trust_cleared_by_client": {}}
    doc.update(over)
    return doc


def _note_text(db, bucket, note) -> str:
    return _stored_text(bucket, db.peek(f"documents/{note.document['id']}")["storage_path"])


def test_the_trust_balance_is_the_book_balance_in_circulation_included(store):
    """Un retrait que la banque n'a pas encore compensé est déjà déduit :
    c'est le solde AU REGISTRE qui s'imprime (100,00 $), jamais le solde
    compensé (400,00 $), ni les fonds d'un autre client du dossier."""
    db, bucket, _template = store
    _print_trust_balance(store)
    db.external_write("dossiers/d1", _dossier(
        client_ids=["p1", "p9"],
        trust_balance_by_client={"p1": 10000, "p9": 99900},
        trust_cleared_by_client={"p1": 40000, "p9": 99900}))

    text = _note_text(db, bucket, _generate())

    assert "Solde en fidéicommis : 100,00" in text
    assert "400,00" not in text and "999,00" not in text
    assert "{{" not in text


def test_the_note_shows_the_accounting_work_done_before_it(store):
    """Le cheminement de l'avocat : la facture, puis l'imputation des
    avances (un paiement d'honoraires — le retrait du fidéicommis, la
    recette d'administration et le paiement de la facture, tels que sa
    transaction unique les écrit), puis la note. Elle montre la rangée
    « Fidéicommis », le solde restant de la facture et ce qui reste au
    registre ; le connecteur ne rend plus l'ancienne note."""
    import re

    db, bucket, _template = store
    _print_trust_balance(store, extra=_TOTALS)
    db.external_write("dossiers/d1", _dossier(
        trust_balance_by_client={"p1": 80000}, trust_cleared_by_client={"p1": 80000}))
    before = _generate()
    text = _note_text(db, bucket, before)
    assert "Solde en fidéicommis : 800,00" in text
    assert "— Fidéicommis" not in text                 # nothing imputed yet

    # The fee payment's single transaction, as it lands in the store.
    db.external_write("admin_transactions/a1",
                      _receipt("a1", 51739, 20, trust_transaction_id="t1",
                               status="en_circulation"))
    db.external_write("invoices/i1", _invoice(
        status="payée", amount_paid=51739,
        paid_date=datetime(2026, 9, 20, tzinfo=UTC)))
    db.external_write("dossiers/d1", _dossier(
        trust_balance_by_client={"p1": 28261}, trust_cleared_by_client={"p1": 80000}))

    after = _generate()

    assert after.reused is False and after.fingerprint != before.fingerprint
    text = _note_text(db, bucket, after)
    assert "20 septembre 2026 — Fidéicommis" in text and "(517,39)" in text
    assert re.search(r"SOLDE</w:t>.*?<w:t[^>]*>0,00", text)
    assert "Solde en fidéicommis : 282,61" in text


def test_an_imported_invoice_without_client_reports_its_dossiers_only_client(store):
    """Une facture importée sans client, dans un dossier qui n'en compte
    qu'un : la règle d'un paiement d'honoraires (D21) — ce client-là."""
    db, bucket, _template = store
    _print_trust_balance(store)
    db.external_write("invoices/i1", _invoice(client_id=""))
    db.external_write("dossiers/d1", _dossier(trust_balance_by_client={"p1": 7500}))

    text = _note_text(db, bucket, _generate())

    assert "Solde en fidéicommis : 75,00" in text


def test_no_client_to_name_refuses_only_a_note_that_prints_the_balance(store, events):
    """Sans client nommé, dans un dossier qui en compte deux, rien ne dit de
    qui imprimer le solde : la note qui l'imprime refuse ; le gabarit de
    toujours s'imprime comme avant (une donnée ancienne ne verrouille rien)."""
    db, _bucket, _template = store
    db.external_write("invoices/i1", _invoice(client_id=""))
    db.external_write("dossiers/d1", _dossier(
        client_ids=["p1", "p2"], trust_balance_by_client={"p1": 100, "p2": 200}))
    assert _generate().reused is False

    template = _print_trust_balance(store)
    refusal = _refused("trust_balance_unavailable")

    assert refusal.message == nh.TRUST_BALANCE_UNAVAILABLE
    assert len(_documents(db)) == 1     # only the note of the former template
    assert events[-1] == ("generation_failed", {
        "reason": "trust_balance_unavailable", "template_id": template["id"],
        "invoice_id": "i1"})
