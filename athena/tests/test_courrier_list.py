"""Tests for Courrier Phase 2: courrier_apercu, streaming bytes, services/courrier, list_courriels."""

import io
import os
import sys
import zipfile
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
    from models import courrier as courrier_model
    from services import courrier as courrier_service
    from utils import courrier_apercu, graph_courrier

from tests._fake_firestore import install  # noqa: E402


# ── Synthetic fixture builders ───────────────────────────────────────────────


def _pdf(pages: list[str]) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for page_text in pages:
        if page_text:
            t = c.beginText(72, 720)
            for line in page_text.split("\n"):
                t.textLine(line)
            c.drawText(t)
        c.showPage()
    c.save()
    return buf.getvalue()


def _encrypted_pdf() -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(_pdf(["Contenu confidentiel"]))))
    writer.encrypt("secret123")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _docx(document_xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


class _StreamResponse:
    def __init__(self, status_code: int, *, chunks=None, payload=None, headers=None):
        self.status_code = status_code
        self._chunks = list(chunks or [])
        self._payload = payload
        self.headers = headers or {}
        self.closed = False
        self.iter_called = False

    def iter_content(self, chunk_size=65536):
        self.iter_called = True
        for chunk in self._chunks:
            yield chunk

    def json(self):
        return self._payload

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _courrier_env(monkeypatch):
    graph_courrier._reset_token_cache_for_tests()
    monkeypatch.setattr(Config, "GRAPH_TENANT_ID", "tenant-id")
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_ID", "client-id")
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(Config, "COURRIER_UPN", "juriste@cabinet.example")
    monkeypatch.setattr(Config, "COURRIER_ONEDRIVE_ROOT", "4 - Dossiers")
    yield
    graph_courrier._reset_token_cache_for_tests()


# ── courrier_apercu: skip triage & preview extraction ────────────────────────


def test_motif_ignore_piece_classifies_without_downloading():
    assert (
        courrier_apercu.motif_ignore_piece(
            {
                "@odata.type": "#microsoft.graph.referenceAttachment",
                "isInline": True,
                "name": "doc.pdf",
                "size": 100,
            }
        )
        == "lien_infonuagique"
    )
    assert (
        courrier_apercu.motif_ignore_piece({"isInline": True, "name": "logo.png", "size": 50000})
        == "incorporee"
    )
    assert (
        courrier_apercu.motif_ignore_piece({"isInline": False, "name": "audience.ICS", "size": 500})
        == "calendrier_contact"
    )
    assert (
        courrier_apercu.motif_ignore_piece({"isInline": False, "name": "me.vcf", "size": 500})
        == "calendrier_contact"
    )
    assert (
        courrier_apercu.motif_ignore_piece({"isInline": False, "name": "image1.png", "size": 1200})
        == "image_signature"
    )
    assert (
        courrier_apercu.motif_ignore_piece(
            {"isInline": False, "name": "IMAGE02.JPG", "size": 29 * 1024}
        )
        == "image_signature"
    )
    # Kept when >= 30 KiB, non-signature name, or boolean size.
    assert (
        courrier_apercu.motif_ignore_piece(
            {"isInline": False, "name": "image1.png", "size": 30 * 1024}
        )
        is None
    )
    assert (
        courrier_apercu.motif_ignore_piece({"isInline": False, "name": "contrat.png", "size": 500})
        is None
    )
    assert (
        courrier_apercu.motif_ignore_piece({"isInline": False, "name": "image1.png", "size": True})
        is None
    )


def test_peut_extraire_apercu_extensions_mimes_and_item_attachment():
    assert courrier_apercu.peut_extraire_apercu("acte.PDF", "application/octet-stream") is True
    assert courrier_apercu.peut_extraire_apercu("memo.docx") is True
    assert courrier_apercu.peut_extraire_apercu("notes.txt") is True
    assert courrier_apercu.peut_extraire_apercu("tableau.csv") is True
    assert courrier_apercu.peut_extraire_apercu("sans_extension", "text/csv; charset=utf-8") is True

    assert (
        courrier_apercu.peut_extraire_apercu(
            "message.eml",
            "application/octet-stream",
            "#microsoft.graph.itemAttachment",
        )
        is False
    )
    assert courrier_apercu.peut_extraire_apercu("photo.jpg", "image/jpeg") is False
    assert courrier_apercu.peut_extraire_apercu("feuille.xlsx", "application/octet-stream") is False
    assert courrier_apercu.peut_extraire_apercu("scan.heic", "image/heic") is False


def test_extraire_apercu_pdf_docx_txt_csv_and_failure_reasons():
    # Multi-page PDF: page 1 only, capped at 400 chars.
    page1_long = "PageUn " + ("abcde " * 90)
    pdf_bytes = _pdf([page1_long, "PageDeuxTexteExclu"])
    text, motif = courrier_apercu.extraire_apercu(pdf_bytes, "jugement.pdf")
    assert motif is None
    assert len(text) == 400
    assert text.startswith("PageUn")
    assert "PageDeuxTexteExclu" not in text

    # Encrypted / blank / corrupt PDFs.
    assert courrier_apercu.extraire_apercu(_encrypted_pdf(), "scelle.pdf") == ("", "chiffre")
    assert courrier_apercu.extraire_apercu(_pdf([""]), "scan.pdf") == ("", "aucun_texte")
    assert courrier_apercu.extraire_apercu(b"not a pdf", "brise.pdf") == ("", "illisible")

    # DOCX valid / empty / corrupt.
    docx_ok = _docx(
        "<w:document><w:body><w:p><w:r><w:t>"
        + ("Paragraphe " * 60)
        + "</w:t></w:r></w:p></w:body></w:document>"
    )
    d_text, d_motif = courrier_apercu.extraire_apercu(docx_ok, "projet.docx")
    assert d_motif is None and len(d_text) == 400
    assert courrier_apercu.extraire_apercu(_docx("<w:document><w:body/></w:document>"), "vide.docx") == ("", "aucun_texte")
    assert courrier_apercu.extraire_apercu(b"bad-zip", "brise.docx") == ("", "illisible")

    # UTF-8 TXT spanning >64 KiB boundary with multi-byte char + CP1252 CSV + binary NUL + whitespace.
    long_utf8 = ("é" * 65536 + " suite").encode("utf-8")
    t_text, t_motif = courrier_apercu.extraire_apercu(long_utf8, "long.txt")
    assert t_motif is None and len(t_text) == 400 and t_text.startswith("é")

    cp1252_csv = "Montant;Honoraires payés\r\n150,00;oui".encode("cp1252")
    assert courrier_apercu.extraire_apercu(cp1252_csv, "honoraires.csv") == (
        "Montant;Honoraires payés\n150,00;oui",
        None,
    )
    assert courrier_apercu.extraire_apercu(b"abc\x00def", "piege.txt") == ("", "illisible")
    assert courrier_apercu.extraire_apercu(b"   \r\n  ", "blanc.txt") == ("", "aucun_texte")


# ── graph_courrier: courrier_get_bytes & follow_next=False ───────────────────


def test_courrier_get_bytes_enforces_header_and_stream_caps_and_closes(monkeypatch):
    monkeypatch.setattr(
        graph_courrier,
        "jeton_courrier",
        lambda: graph_courrier.TokenInfo("tok", None, ("Mail.ReadWrite",)),
    )

    resp_header_too_big = _StreamResponse(
        200,
        chunks=[b"abc"],
        headers={"Content-Length": "11"},
    )
    monkeypatch.setattr(graph_courrier._session, "get", lambda *a, **kw: resp_header_too_big)
    with pytest.raises(graph_courrier.CourrierGraphError) as exc_info:
        graph_courrier.courrier_get_bytes("/users/u/messages/m/attachments/a/$value", max_bytes=10)
    assert exc_info.value.reason == "too_large"
    assert resp_header_too_big.closed is True
    assert resp_header_too_big.iter_called is False

    resp_stream_too_big = _StreamResponse(
        200,
        chunks=[b"12345", b"678901", b"unused"],
    )
    monkeypatch.setattr(graph_courrier._session, "get", lambda *a, **kw: resp_stream_too_big)
    with pytest.raises(graph_courrier.CourrierGraphError) as exc_info2:
        graph_courrier.courrier_get_bytes("/users/u/messages/m/attachments/a/$value", max_bytes=10)
    assert exc_info2.value.reason == "too_large"
    assert resp_stream_too_big.closed is True

    resp_ok = _StreamResponse(200, chunks=[b"hello ", b"", b"world"])
    monkeypatch.setattr(graph_courrier._session, "get", lambda *a, **kw: resp_ok)
    assert (
        graph_courrier.courrier_get_bytes("/users/u/messages/m/attachments/a/$value", max_bytes=11)
        == b"hello world"
    )
    assert resp_ok.closed is True


def test_courrier_get_follow_next_false_stops_on_first_page(monkeypatch):
    monkeypatch.setattr(
        graph_courrier,
        "jeton_courrier",
        lambda: graph_courrier.TokenInfo("tok", None, ("Mail.ReadWrite",)),
    )
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _StreamResponse(
            200,
            payload={
                "value": [{"id": "m1"}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/users/u/messages?$skip=1",
            },
        )

    monkeypatch.setattr(graph_courrier._session, "get", fake_get)
    res = graph_courrier.courrier_get("/users/u/messages", follow_next=False)
    assert len(calls) == 1
    assert [m["id"] for m in res["value"]] == ["m1"]


# ── services/courrier & handlers.list_courriels ──────────────────────────────


def test_list_courriels_full_mapping_threads_html_previews_and_read_only(monkeypatch):
    fake = install(monkeypatch, courrier_model)
    seen_calls = []
    byte_calls = []

    long_html = "<p>Bonjour <b>Maître</b></p><!-- secret --><script>x()</script><p>" + ("Mot " * 500) + "</p>"
    csv_bytes = "Ligne;Valeur\n1;Test".encode("utf-8")

    def fake_get(path, *, params=None, immutable_id=True, prefer=(), follow_next=True):
        seen_calls.append((path, params, immutable_id, prefer, follow_next))
        if path.endswith("/mailFolders/Inbox"):
            return {"id": "inbox-id", "totalItemCount": 1}
        if path.endswith("/mailFolders/Inbox/messages"):
            return {
                "value": [
                    {
                        "id": "msg-imm-1",
                        "internetMessageId": "<thread-msg@cabinet.example>",
                        "conversationId": "conv-'quoted'",
                        "receivedDateTime": "2026-11-20T18:45:00Z",  # 13:45 EST Montréal
                        "from": {"emailAddress": {"name": "Greffe", "address": "greffe@ex.com"}},
                        "toRecipients": [{"emailAddress": {"address": "juriste@cabinet.example"}}],
                        "ccRecipients": [],
                        "subject": "Avis d'audience",
                        "isRead": False,
                        "flag": {"flagStatus": "flagged"},
                        "categories": [],
                        "webLink": "https://outlook.office.com/mail/id/msg-imm-1",
                        "uniqueBody": {"contentType": "html", "content": long_html},
                        "hasAttachments": True,
                    }
                ]
            }
        if path.endswith("/attachments"):
            return {
                "value": [
                    {
                        "id": "a1",
                        "name": "rapport.csv",
                        "contentType": "text/csv",
                        "size": len(csv_bytes),
                        "isInline": False,
                    },
                    {
                        "id": "a2",
                        "name": "gros_dossier.pdf",
                        "contentType": "application/pdf",
                        "size": courrier_apercu.APERCU_MAX_BYTES + 1,
                        "isInline": False,
                    },
                    {
                        "id": "a3",
                        "name": "stream_piege.txt",
                        "contentType": "text/plain",
                        "size": 100,
                        "isInline": False,
                    },
                    {
                        "id": "a4",
                        "name": "partage.pdf",
                        "contentType": "application/pdf",
                        "size": 500,
                        "isInline": False,
                        "@odata.type": "#microsoft.graph.referenceAttachment",
                    },
                ]
            }
        if path.endswith("/messages") and params and "$filter" in params:
            assert "conv-''quoted''" in params["$filter"]
            return {
                "value": [
                    {"id": "msg-imm-1", "parentFolderId": "inbox-id"},
                    {"id": "sib-inbox", "parentFolderId": "inbox-id"},
                    {"id": "sib-dossier", "parentFolderId": "fold-2026-001"},
                    {"id": "sib-statut", "parentFolderId": "st-actifs"},
                    {"id": "sib-autre", "parentFolderId": "sent-id"},
                ]
            }
        if path.endswith("/mailFolders"):
            return {"value": [{"id": "arch-id", "displayName": "archive"}]}
        if "/mailFolders/arch-id/childFolders" in path:
            return {"value": [{"id": "doss-id", "displayName": "Dossiers"}]}
        if "/mailFolders/doss-id/childFolders" in path:
            return {
                "value": [
                    {"id": "st-actifs", "displayName": "Actifs"},
                    {"id": "st-susp", "displayName": "Suspendu"},
                    # Intentionally omit Services-conseils to verify warning emission
                ]
            }
        if "/mailFolders/st-actifs/childFolders" in path:
            return {
                "value": [
                    {"id": "fold-2026-001", "displayName": "2026-001 - Dossier Beta"},
                    {"id": "fold-misc", "displayName": "Divers"},
                ]
            }
        if "/childFolders" in path:
            return {"value": []}
        if path.endswith("/children"):
            return {
                "value": [
                    {"id": "od-1", "name": "2026-001 - Un", "folder": {}},
                    {"id": "od-2", "name": "2026-001 - Doublon", "folder": {}},
                    {"id": "od-file", "name": "2026-001.txt", "file": {}},
                ]
            }
        if "/drive/root:/" in path:
            return {"id": "od-root", "folder": {}}
        raise AssertionError(f"unexpected call {path}")

    def fake_get_bytes(path, *, max_bytes, immutable_id=True):
        byte_calls.append(path)
        if "/a3/" in path:
            raise graph_courrier.CourrierGraphError("trop volumineux", reason="too_large")
        return csv_bytes

    monkeypatch.setattr(graph_courrier, "courrier_get", fake_get)
    monkeypatch.setattr(graph_courrier, "courrier_get_bytes", fake_get_bytes)

    res = handlers.list_courriels({"premier": 0, "nombre": 5})

    # Verify Inbox message query flags & single-page mode.
    inbox_msg_calls = [c for c in seen_calls if c[0].endswith("/mailFolders/Inbox/messages")]
    assert len(inbox_msg_calls) == 1
    _, params, imm, prefer, follow_next = inbox_msg_calls[0]
    assert params["$orderby"] == "receivedDateTime asc"
    assert params["$skip"] == 0 and params["$top"] == 5
    assert imm is True
    assert 'outlook.body-content-type="text"' in prefer
    assert follow_next is False

    assert len(res["messages"]) == 1
    msg = res["messages"][0]
    assert msg["recu"] == "2026-11-20 13:45"
    assert msg["texte"].startswith("Bonjour Maître")
    assert "secret" not in msg["texte"] and "x()" not in msg["texte"]
    assert len(msg["texte"]) == 1500
    assert msg["fil"] == {"boite": 1, "2026-001": 1, "statut:Actifs": 1, "autre": 1}

    # Verify attachment 2 (>10 MB) and 4 (referenceAttachment) never triggered courrier_get_bytes.
    assert len(byte_calls) == 2
    assert all("/a1/" in p or "/a3/" in p for p in byte_calls)
    assert msg["pieces"][0]["apercu"] == "Ligne;Valeur\n1;Test"
    assert msg["pieces"][1]["apercu_omis"] == "taille_depassee"
    assert msg["pieces"][2]["apercu_omis"] == "taille_depassee"
    assert msg["pieces"][3]["ignore"] == "lien_infonuagique"
    assert msg["pieces"][3]["apercu_omis"] == "ignoree"

    assert res["onedrive"]["doublons"] == ["2026-001"]
    assert any("Services-conseils" in w for w in res["avertissements"])
    assert fake.commits == []


def test_list_courriels_budget_stops_after_at_least_one_message(monkeypatch):
    install(monkeypatch, courrier_model)

    def fake_get(path, *, params=None, immutable_id=True, prefer=(), follow_next=True):
        if path.endswith("/mailFolders/Inbox"):
            return {"id": "inbox-id", "totalItemCount": 4}
        if path.endswith("/mailFolders/Inbox/messages"):
            return {
                "value": [
                    {
                        "id": f"m{i}",
                        "internetMessageId": f"<m{i}@cabinet.example>",
                        "conversationId": "",
                        "receivedDateTime": "2026-11-20T12:00:00Z",
                        "from": {"emailAddress": {"name": "A", "address": "a@ex.com"}},
                        "subject": f"Msg {i}",
                        "hasAttachments": False,
                    }
                    for i in (1, 2, 3)
                ]
            }
        if path.endswith("/mailFolders"):
            return {"value": []}
        if path.endswith("/children"):
            return {"value": []}
        if "/drive/root:/" in path:
            return {"id": "od-root", "folder": {}}
        raise AssertionError(f"unexpected {path}")

    monkeypatch.setattr(graph_courrier, "courrier_get", fake_get)

    ticks = iter([0.0, 30.0, 30.0])  # Immediately past budget after first message begins
    res = courrier_service.inventorier_courriels(
        premier=0,
        nombre=5,
        budget_secondes=25.0,
        clock=lambda: next(ticks),
    )
    assert res["nombre_retourne"] == 1
    assert res["prochain"] == 1
    assert res["budget_atteint"] is True
    assert res["suite"] is True


def test_list_courriels_fails_closed_on_unconfigured_graph_or_firestore_error(monkeypatch):
    install(monkeypatch, courrier_model)

    # Unconfigured module refused cleanly.
    monkeypatch.setattr(Config, "COURRIER_UPN", "")
    with pytest.raises(handlers.ToolArgumentError, match="pas configuré"):
        handlers.list_courriels({})

    # Restore config and test Graph attachment error (non-too_large) -> HTTP code only.
    monkeypatch.setattr(Config, "COURRIER_UPN", "juriste@cabinet.example")

    def fake_get(path, *, params=None, immutable_id=True, prefer=(), follow_next=True):
        if path.endswith("/mailFolders/Inbox"):
            return {"id": "inbox-id", "totalItemCount": 1}
        if path.endswith("/mailFolders/Inbox/messages"):
            return {
                "value": [
                    {
                        "id": "m1",
                        "internetMessageId": "<m1@cabinet.example>",
                        "receivedDateTime": "2026-11-20T12:00:00Z",
                        "hasAttachments": True,
                    }
                ]
            }
        if path.endswith("/attachments"):
            return {"value": [{"id": "a1", "name": "piece.pdf", "contentType": "application/pdf", "size": 500}]}
        if path.endswith("/mailFolders"):
            return {"value": []}
        if path.endswith("/children"):
            return {"value": []}
        if "/drive/root:/" in path:
            return {"id": "od-root", "folder": {}}
        return {"value": []}

    monkeypatch.setattr(graph_courrier, "courrier_get", fake_get)
    monkeypatch.setattr(
        graph_courrier,
        "courrier_get_bytes",
        mock.Mock(side_effect=graph_courrier.CourrierGraphError("secret detail", status_code=502)),
    )
    with pytest.raises(handlers.ToolArgumentError, match="HTTP 502"):
        handlers.list_courriels({})

    # Restore bytes download, break Firestore strict map read -> fail closed.
    monkeypatch.setattr(graph_courrier, "courrier_get_bytes", lambda *a, **kw: _pdf(["ok"]))
    monkeypatch.setattr(
        courrier_model,
        "get_courrier_messages_map_strict",
        mock.Mock(side_effect=RuntimeError("firestore unreachable")),
    )
    with pytest.raises(handlers.ToolArgumentError, match="échoué"):
        handlers.list_courriels({})
