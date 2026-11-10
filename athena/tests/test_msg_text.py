"""The text of a pièce justificative filed as an Outlook .msg —
utils/msg_text through olefile (2026-11-10). The fixtures are SYNTHETIC
compound files (tests/_fake_msg.py): the repository is public, so no real
Outlook export ever enters it."""

import os
import sys
from datetime import datetime, timezone

import olefile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests._fake_msg import build_cfb, build_msg  # noqa: E402
from tests.test_pdf_text import _pdf  # noqa: E402
from utils import msg_text  # noqa: E402
from utils.pdf_text import DocumentTextError  # noqa: E402


def test_the_synthetic_writer_is_a_compound_file_olefile_reads():
    data = build_cfb({"court": b"abc", "long": b"L" * 5000,
                      "dossier/dedans": b"xyz"})
    assert olefile.isOleFile(data=data)
    ole = olefile.OleFileIO(data)
    try:
        assert ole.openstream("court").read() == b"abc"           # mini stream
        assert ole.openstream("long").read() == b"L" * 5000       # sectors
        assert ole.openstream("dossier/dedans").read() == b"xyz"  # a storage
    finally:
        ole.close()


def test_headers_body_date_and_a_pdf_attachment_s_text():
    pdf = _pdf(["FACTURE 2031-77 Total 88,20 $"])
    data = build_msg(
        subject="Votre facture", sender="Fournisseur",
        sender_email="factures@fournisseur.example",
        to="Cabinet", cc="Comptable", body="Bonjour,\r\n\r\nCi-joint.\r\n",
        sent=datetime(2031, 11, 4, 14, 15, tzinfo=timezone.utc),
        attachments=[("facture.pdf", "application/pdf", pdf),
                     ("photo.jpg", "image/jpeg", b"\xff\xd8\xff" + b"\x00" * 50)])
    out = msg_text.extract_msg(data)
    assert out.headers == {
        "from": "Fournisseur <factures@fournisseur.example>",
        "to": "Cabinet", "cc": "Comptable",
        "date": "2031-11-04T14:15:00Z", "subject": "Votre facture",
    }
    assert out.body == "Bonjour,\n\nCi-joint."
    assert [(a.filename, a.content_type, a.size_bytes, a.text_included)
            for a in out.attachments] == [
        ("facture.pdf", "application/pdf", len(pdf), True),
        ("photo.jpg", "image/jpeg", 53, False),
    ]
    ((name, text),) = out.attachment_texts
    assert name == "facture.pdf" and "88,20" in text
    assert not out.truncated and out.warnings == []


def test_an_html_body_is_read_when_there_is_no_plain_one():
    out = msg_text.extract_msg(build_msg(subject="s", html="<p>Total&nbsp;: 9,99</p>"))
    assert out.body == "Total : 9,99"


def test_an_rtf_only_body_is_said_never_guessed():
    out = msg_text.extract_msg(build_msg(subject="s", rtf_only=True))
    assert out.body == "" and out.warnings == ["body_rtf_only"]


def test_the_body_is_cut_to_the_budget_and_said_cut():
    out = msg_text.extract_msg(build_msg(subject="s", body="Z" * 3000), char_cap=100)
    assert out.body == "Z" * 100 and out.truncated


def test_the_attachments_listed_are_capped(monkeypatch):
    monkeypatch.setattr(msg_text, "MAX_ATTACHMENTS", 3)
    data = build_msg(subject="s", body="b", attachments=[
        (f"{k}.txt", "text/plain", b"x") for k in range(5)])
    out = msg_text.extract_msg(data)
    assert len(out.attachments) == 3 and "attachments_capped" in out.warnings


def test_an_oversized_header_stream_is_never_read(monkeypatch):
    monkeypatch.setattr(msg_text, "HEADER_STREAM_MAX", 10)
    out = msg_text.extract_msg(build_msg(subject="un objet beaucoup trop long", body="b"))
    assert "subject" not in out.headers


@pytest.mark.parametrize("data", [
    b"", b"not ole at all" * 200,
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 2000,      # a header, no file
])
def test_what_is_not_a_compound_file_is_refused(data):
    with pytest.raises(DocumentTextError) as exc:
        msg_text.extract_msg(data)
    assert exc.value.reason == "invalid_msg"


def test_a_truncated_compound_file_is_refused_never_raised_raw():
    data = build_msg(subject="s", body="b" * 6000)
    with pytest.raises(DocumentTextError) as exc:
        msg_text.extract_msg(data[:1600])
    assert exc.value.reason == "invalid_msg"
