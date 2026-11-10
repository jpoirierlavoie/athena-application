"""The text of an emailed pièce justificative (.eml) — utils/email_text
(2026-11-10). Pure: no Firestore, no Flask. Synthetic messages only (the
repository is public): .example addresses, invented amounts."""

import os
import sys
import time
from email.message import EmailMessage

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.test_pdf_text import _pdf  # noqa: E402
from utils import email_text  # noqa: E402
from utils.pdf_text import DocumentTextError  # noqa: E402


def _eml(*, text=None, html=None, attachments=(), subject="Facture F-1",
         sender="Fournisseur <factures@fournisseur.example>") -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "cabinet@cabinet.example"
    msg["Date"] = "Tue, 04 Nov 2031 09:15:00 -0500"
    msg["Subject"] = subject
    if text is not None:
        msg.set_content(text)
    if html is not None:
        if text is None:
            msg.set_content(html, subtype="html")
        else:
            msg.add_alternative(html, subtype="html")
    for filename, maintype, subtype, data in attachments:
        msg.add_attachment(data, maintype=maintype, subtype=subtype,
                           filename=filename)
    return bytes(msg)


def test_headers_body_and_a_pdf_attachment_s_text():
    pdf = _pdf(["FACTURE F-1 Total 123,45 $"])
    out = email_text.extract_eml(_eml(
        text="Bonjour,\nVoici votre facture.\n",
        attachments=[("F-1.pdf", "application", "pdf", pdf),
                     ("logo.png", "image", "png", b"\x89PNG....")]))
    assert out.headers["from"] == "Fournisseur <factures@fournisseur.example>"
    assert out.headers["subject"] == "Facture F-1"
    assert out.headers["date"].startswith("Tue, 04 Nov 2031")
    assert out.body == "Bonjour,\nVoici votre facture."
    names = [(a.filename, a.content_type, a.text_included) for a in out.attachments]
    assert names == [("F-1.pdf", "application/pdf", True),
                     ("logo.png", "image/png", False)]
    assert out.attachments[0].size_bytes == len(pdf)
    ((filename, text),) = out.attachment_texts
    assert filename == "F-1.pdf" and "123,45" in text
    assert not out.truncated


def test_an_html_only_body_becomes_text():
    out = email_text.extract_eml(_eml(html=(
        "<html><head><title>x</title><style>p{color:red}</style></head><body>"
        "<p>Montant&nbsp;: <b>45,00&nbsp;$</b></p><div>TPS 2,25</div>"
        "<script>alert(1)</script><!-- note --><br>Merci</body></html>")))
    assert out.body == "Montant : 45,00 $\nTPS 2,25\nMerci"


def test_an_encoded_subject_is_decoded():
    out = email_text.extract_eml(_eml(text="x", subject="Reçu n° 12 — Café"))
    assert out.headers["subject"] == "Reçu n° 12 — Café"


def test_the_body_and_the_attachments_share_one_budget():
    pdf = _pdf(["A" * 3000])
    out = email_text.extract_eml(
        _eml(text="B" * 500, attachments=[("f.pdf", "application", "pdf", pdf)]),
        char_cap=1000)
    assert len(out.body) == 500
    assert sum(len(t) for _n, t in out.attachment_texts) <= 500
    assert out.truncated
    tight = email_text.extract_eml(_eml(text="C" * 5000), char_cap=100)
    assert tight.body == "C" * 100 and tight.truncated


def test_an_unreadable_pdf_attachment_is_named_never_fatal():
    out = email_text.extract_eml(_eml(
        text="x", attachments=[("cassé.pdf", "application", "pdf", b"%PDF-1.7 nope")]))
    assert out.attachments[0].text_included is False
    assert out.warnings == ["attachment_unreadable:invalid_pdf"]


def test_the_parts_walked_are_capped():
    msg = EmailMessage()
    msg["Subject"] = "beaucoup"
    msg.set_content("corps")
    for k in range(email_text.MAX_PARTS + 20):
        msg.add_attachment(b"x", maintype="application", subtype="octet-stream",
                           filename=f"{k}.bin")
    out = email_text.extract_eml(bytes(msg))
    assert "parts_capped" in out.warnings
    assert len(out.attachments) < email_text.MAX_PARTS


def test_a_header_flood_is_cut_before_it_is_decoded():
    flood = "=?utf-8?q?a?= " * 200_000
    start = time.perf_counter()
    out = email_text.extract_eml(_eml(text="x", subject="s").replace(
        b"Subject: s", b"Subject: " + flood.encode()))
    assert time.perf_counter() - start < 3.0
    assert len(out.headers["subject"]) <= email_text.HEADER_MAX


def test_html_to_text_is_linear_on_hostile_markup():
    for hostile in ("<" * 300_000, "<a" * 300_000, "<!--" * 200_000,
                    "<script>" + "x" * 300_000, "&amp;" * 200_000):
        start = time.perf_counter()
        email_text.html_to_text(hostile)
        assert time.perf_counter() - start < 2.0, hostile[:12]


def test_html_to_text_keeps_indexes_when_lowering_would_grow_a_string():
    # « İ » lower-cases to two code points with str.lower(): the ASCII-only
    # translation keeps every index of the lowered copy valid.
    assert email_text.html_to_text("İİ<B>gras</B><SCRIPT>x</SCRIPT>fin") == "İİgrasfin"


def test_not_an_email_is_refused():
    # BytesParser accepts nearly anything as a header-less message; a binary
    # blob comes back empty rather than raising — the sniff upstream already
    # refused it as an .eml. What it never does is pretend to have content.
    out = email_text.extract_eml(b"\x00\x01\x02")
    assert out.headers == {} and out.attachments == []
    with pytest.raises(DocumentTextError):
        email_text.extract_eml(None)
