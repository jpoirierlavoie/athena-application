"""The text of a pièce justificative filed as an Outlook .msg — olefile,
bounded (2026-11-10).

The .msg twin of ``utils/email_text`` (whose :class:`EmailText` it returns):
the five headers, the body, the attachments' names, types and sizes, and the
TEXT of PDF attachments through ``utils/pdf_text``. olefile is the one
dependency it needs (pure Python, BSD; requirements.in says why), and it is
used narrowly:

* only NAMED property streams are read ([MS-OXMSG]: ``__substg1.0_PPPPTTTT``,
  a property id and a type — ``001F`` UTF-16LE, ``001E`` 8-bit, ``0102``
  binary), each size-checked with ``get_size`` BEFORE it is read, against a
  cap of its own;
* the date comes from the root's ``__properties_version1.0`` stream (its
  fixed-size entries), read up to :data:`PROPERTIES_MAX` bytes;
* at most :data:`MAX_ATTACHMENTS` attachment storages are listed; an
  attachment's DATA is read only for a PDF, whose text joins the shared
  character budget;
* any error olefile raises — a malformed or hostile compound file —
  refuses the read as ``invalid_msg``.

An RTF-only body (no plain or HTML body) is not decoded: the result says
``body_rtf_only``. Pure: no Flask, no models.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from typing import Optional

import olefile

from utils.email_text import (
    FILENAME_MAX,
    HEADER_MAX,
    PDF_TYPE,
    Attachment,
    EmailText,
    html_to_text,
    tidy_text,
)
from utils.pdf_text import DocumentTextError, extract_pdf_pages

MAX_ATTACHMENTS = 50
PROPERTIES_MAX = 64 * 1024
HEADER_STREAM_MAX = 16 * 1024
HTML_STREAM_MAX = 4 * 1024 * 1024
ATTACHMENT_DATA_MAX = 10 * 1024 * 1024

# Property ids ([MS-OXPROPS]).
_SUBJECT = 0x0037
_SENDER_NAME = 0x0C1A
_SENDER_EMAIL = 0x0C1F
_SENDER_SMTP = 0x5D01
_DISPLAY_TO = 0x0E04
_DISPLAY_CC = 0x0E03
_BODY = 0x1000
_BODY_HTML = 0x1013
_RTF_COMPRESSED = 0x1009
_SUBMIT_TIME = 0x0039
_DELIVERY_TIME = 0x0E06
_ATTACH_LONG_FILENAME = 0x3707
_ATTACH_FILENAME = 0x3704
_ATTACH_MIME = 0x370E
_ATTACH_DATA = 0x3701

_UNICODE, _STRING8, _BINARY, _SYSTIME = 0x001F, 0x001E, 0x0102, 0x0040
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def _stream(prop: int, kind: int, storage: str = "") -> str:
    name = f"__substg1.0_{prop:04X}{kind:04X}"
    return f"{storage}/{name}" if storage else name


def _read(ole, name: str, cap: int, *, prefix: bool = False) -> Optional[bytes]:
    """The stream *name*: whole when it is at most *cap* bytes, else
    ``None`` — or, with *prefix*, its first *cap* bytes whatever its size.
    Never more than *cap* bytes are read."""
    if not ole.exists(name):
        return None
    if ole.get_size(name) > cap and not prefix:
        return None
    with ole.openstream(name) as handle:
        return handle.read(cap)


def _string(ole, prop: int, cap: Optional[int] = None, storage: str = "",
            *, prefix: bool = False) -> str:
    """A string property, Unicode first, 8-bit (cp1252) as a fallback —
    capped at *cap* bytes (default :data:`HEADER_STREAM_MAX`, read when
    called)."""
    cap = HEADER_STREAM_MAX if cap is None else cap
    raw = _read(ole, _stream(prop, _UNICODE, storage), cap - cap % 2,
                prefix=prefix)
    if raw is not None:
        return raw.decode("utf-16-le", errors="replace").rstrip("\x00")
    raw = _read(ole, _stream(prop, _STRING8, storage), cap, prefix=prefix)
    if raw is not None:
        return raw.decode("cp1252", errors="replace").rstrip("\x00")
    return ""


def _short(text: str, limit: int = HEADER_MAX) -> str:
    return " ".join(text.split())[:limit]


def _sent_at(ole) -> str:
    """The submit (else delivery) time, ISO 8601 UTC, from the root's
    fixed-size property entries — or ``""``."""
    raw = _read(ole, "__properties_version1.0", PROPERTIES_MAX)
    if not raw:
        return ""
    found: dict[int, int] = {}
    for offset in range(32, len(raw) - 15, 16):   # a 32-byte header first
        tag = int.from_bytes(raw[offset:offset + 4], "little")
        if tag & 0xFFFF == _SYSTIME and tag >> 16 in (_SUBMIT_TIME, _DELIVERY_TIME):
            found[tag >> 16] = int.from_bytes(raw[offset + 8:offset + 16], "little")
    value = found.get(_SUBMIT_TIME) or found.get(_DELIVERY_TIME)
    if not value:
        return ""
    try:
        moment = _FILETIME_EPOCH + timedelta(microseconds=value // 10)
    except OverflowError:
        return ""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_msg(data: bytes, *, char_cap: int = 40_000) -> EmailText:
    """The text of the Outlook message *data*; see the module docstring."""
    if not isinstance(data, (bytes, bytearray)) or not olefile.isOleFile(data=bytes(data)):
        raise DocumentTextError("invalid_msg")
    try:
        ole = olefile.OleFileIO(io.BytesIO(bytes(data)))
    except Exception as exc:
        raise DocumentTextError("invalid_msg") from exc
    try:
        return _extract(ole, max(0, char_cap))
    except DocumentTextError:
        raise
    except Exception as exc:
        raise DocumentTextError("invalid_msg") from exc
    finally:
        ole.close()


def _extract(ole, budget: int) -> EmailText:
    result = EmailText()
    sender = _short(_string(ole, _SENDER_NAME))
    address = _short(_string(ole, _SENDER_SMTP) or _string(ole, _SENDER_EMAIL))
    if sender or address:
        result.headers["from"] = (f"{sender} <{address}>" if sender and address
                                  else sender or address)
    for key, prop in (("to", _DISPLAY_TO), ("cc", _DISPLAY_CC)):
        value = _short(_string(ole, prop))
        if value:
            result.headers[key] = value
    sent = _sent_at(ole)
    if sent:
        result.headers["date"] = sent
    subject = _short(_string(ole, _SUBJECT))
    if subject:
        result.headers["subject"] = subject

    # The body: plain text first, read no further than the budget allows
    # (UTF-16: two bytes a character); else the HTML body; else RTF only.
    body = _string(ole, _BODY, cap=max(2, budget * 2 + 2), prefix=True)
    if not body:
        html = _read(ole, _stream(_BODY_HTML, _BINARY), HTML_STREAM_MAX)
        if html is not None:
            body = html_to_text(html.decode("utf-8", errors="replace"))
        else:
            body = html_to_text(_string(ole, _BODY_HTML, cap=HTML_STREAM_MAX))
        if not body and ole.exists(_stream(_RTF_COMPRESSED, _BINARY)):
            result.warnings.append("body_rtf_only")
    else:
        body = tidy_text(body)
    if len(body) > budget or (ole.exists(_stream(_BODY, _UNICODE))
                              and ole.get_size(_stream(_BODY, _UNICODE)) > budget * 2 + 2):
        result.truncated = True
    result.body = body[:budget]
    budget -= len(result.body)

    storages = sorted(
        "/".join(path) for path in ole.listdir(streams=False, storages=True)
        if len(path) == 1 and path[0].startswith("__attach_version1.0_#")
    )
    if len(storages) > MAX_ATTACHMENTS:
        result.warnings.append("attachments_capped")
    for storage in storages[:MAX_ATTACHMENTS]:
        name = _short(_string(ole, _ATTACH_LONG_FILENAME, storage=storage)
                      or _string(ole, _ATTACH_FILENAME, storage=storage),
                      FILENAME_MAX)
        ctype = _short(_string(ole, _ATTACH_MIME, storage=storage), 100)
        data_name = _stream(_ATTACH_DATA, _BINARY, storage)
        size = ole.get_size(data_name) if ole.exists(data_name) else 0
        included = False
        if ctype == PDF_TYPE or name.lower().endswith(".pdf"):
            if budget > 0:
                payload = _read(ole, data_name, ATTACHMENT_DATA_MAX)
                if payload is not None:
                    pdf = extract_pdf_pages(payload, char_cap=budget)
                    if pdf.readable:
                        text = "\n\n".join(p.text for p in pdf.pages if p.text)
                        if pdf.truncated or pdf.next_page is not None:
                            result.truncated = True
                        result.attachment_texts.append((name, text))
                        budget -= len(text)
                        included = True
                    else:
                        result.warnings.append(
                            f"attachment_unreadable:{pdf.reason}")
            else:
                result.truncated = True
        result.attachments.append(
            Attachment(name, ctype or "application/octet-stream", size, included))
    return result
