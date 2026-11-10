"""The text of an emailed pièce justificative (.eml) — stdlib ``email`` and
pypdf, bounded (2026-11-10).

A supplier's invoice often arrives by email, and the lawyer files the email
itself as the receipt (.eml joined the receipt types on 2026-11-10).
``get_admin_receipt`` returns its text: five headers, the body, the list of
attachments, and the TEXT of its PDF attachments — the invoice is usually
there — through ``utils/pdf_text``. Never an attachment's bytes, never an
image.

Bounds (a receipt is at most ``MAX_RECEIPT_SIZE``, 10 MB):

* at most :data:`MAX_PARTS` MIME parts are walked (a deeper or wider message
  is reported ``parts_capped``; a nesting deep enough to exhaust recursion is
  refused as ``invalid_email``);
* each header is cut to :data:`HEADER_MAX` characters BEFORE its encoded
  words are decoded;
* the body and the attachments' text share ONE character budget
  (``char_cap``) — whatever does not fit is ``truncated``;
* HTML becomes text in one LINEAR pass over the markup (:func:`html_to_text`)
  — never ``html.parser``, whose worst case CVE-2025-6069 made quadratic,
  and no regular expression over the markup.

Pure: no Flask, no models. Container failures raise
``utils.pdf_text.DocumentTextError`` with a machine-stable ``reason``; the
handler words it in French.
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass, field
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser

from utils.pdf_text import DocumentTextError, extract_pdf_pages

MAX_PARTS = 100
HEADER_MAX = 1000
FILENAME_MAX = 200
HEADER_FIELDS = ("from", "to", "cc", "date", "subject")
PDF_TYPE = "application/pdf"

# Tags whose end starts a new line of text.
_BLOCK_TAGS = frozenset({
    "address", "article", "blockquote", "br", "dd", "div", "dl", "dt",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li",
    "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
})
# Tags whose CONTENT is never text.
_SKIPPED_TAGS = ("script", "style", "head", "title")
# ASCII-only lower-casing: the same length as the input, so indexes found in
# the lowered copy hold in the original (str.lower() may grow a string).
_ASCII_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str
    size_bytes: int
    text_included: bool        # its text is among ``attachment_texts``


@dataclass
class EmailText:
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    # (filename, text) of each PDF attachment read, in order.
    attachment_texts: list[tuple[str, str]] = field(default_factory=list)
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)


def html_to_text(markup: str) -> str:
    """The visible text of an HTML body, in one forward pass: tags dropped,
    a new line after each block, script/style/head content skipped,
    comments skipped, entities unescaped, blank runs collapsed. An
    unterminated tag or comment ends the text there."""
    low = markup.translate(_ASCII_LOWER)
    out: list[str] = []
    # True while the current line holds no text yet: a block boundary then
    # adds no second line break (« </p><div> » is ONE break, not a blank).
    line_empty = True

    def text(chunk: str) -> None:
        nonlocal line_empty
        out.append(chunk)
        if chunk.strip():
            line_empty = False

    i, n = 0, len(markup)
    while i < n:
        lt = markup.find("<", i)
        if lt < 0:
            text(_html.unescape(markup[i:]))
            break
        text(_html.unescape(markup[i:lt]))
        if low.startswith("<!--", lt):
            end = markup.find("-->", lt + 4)
            if end < 0:
                break
            i = end + 3
            continue
        gt = markup.find(">", lt + 1)
        if gt < 0:
            break
        inner = low[lt + 1:gt].strip()
        closing = inner.startswith("/")
        name = inner.lstrip("/").split(None, 1)[0].rstrip("/") if inner else ""
        i = gt + 1
        if not closing and name in _SKIPPED_TAGS:
            end = low.find("</" + name, i)
            if end < 0:
                break
            close = markup.find(">", end)
            i = n if close < 0 else close + 1
            continue
        if name in _BLOCK_TAGS and not line_empty:
            out.append("\n")
            line_empty = True
    return tidy_text("".join(out))


def tidy_text(text: str) -> str:
    """Spaces collapsed within each line; at most one blank line in a row."""
    lines, blank = [], False
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = " ".join(raw.split())
        if line:
            lines.append(line)
            blank = False
        elif not blank and lines:
            lines.append("")
            blank = True
    return "\n".join(lines).strip()


def _header(value) -> str:
    """A header's text: cut BEFORE its encoded words are decoded."""
    raw = str(value)[:HEADER_MAX]
    try:
        text = str(make_header(decode_header(raw)))
    except Exception:
        text = raw
    return " ".join(text.split())[:HEADER_MAX]


def _decoded(part) -> bytes:
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else b""


def _as_text(part) -> str:
    charset = part.get_content_charset() or "utf-8"
    try:
        return _decoded(part).decode(charset, errors="replace")
    except LookupError:                     # an unknown charset name
        return _decoded(part).decode("utf-8", errors="replace")


def _is_attachment(part) -> bool:
    disposition = (part.get("Content-Disposition") or "").split(";", 1)[0]
    return (disposition.strip().lower() == "attachment"
            or bool(part.get_filename()))


def extract_eml(data: bytes, *, char_cap: int = 40_000) -> EmailText:
    """The text of the RFC 822 message *data*; see the module docstring."""
    try:
        message = BytesParser(policy=policy.compat32).parsebytes(data)
    except Exception as exc:
        raise DocumentTextError("invalid_email") from exc
    result = EmailText()
    for name in HEADER_FIELDS:
        if message.get(name) is not None:
            result.headers[name] = _header(message.get(name))

    plain, markup, files = None, None, []
    try:
        for count, part in enumerate(message.walk()):
            if count >= MAX_PARTS:
                result.warnings.append("parts_capped")
                break
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            if _is_attachment(part):
                files.append(part)
            elif ctype == "text/plain" and plain is None:
                plain = part
            elif ctype == "text/html" and markup is None:
                markup = part
    except RecursionError as exc:
        raise DocumentTextError("invalid_email") from exc

    budget = max(0, char_cap)
    if plain is not None:
        body = tidy_text(_as_text(plain))
    elif markup is not None:
        body = html_to_text(_as_text(markup))
    else:
        body = ""
    if len(body) > budget:
        body, result.truncated = body[:budget], True
    result.body = body
    budget -= len(body)

    for part in files:
        name = _header(part.get_filename() or "")[:FILENAME_MAX]
        payload = _decoded(part)
        ctype = part.get_content_type()
        included = False
        if ctype == PDF_TYPE or name.lower().endswith(".pdf"):
            if budget > 0:
                pdf = extract_pdf_pages(payload, char_cap=budget)
                if pdf.readable:
                    text = "\n\n".join(p.text for p in pdf.pages if p.text)
                    if pdf.truncated or pdf.next_page is not None:
                        result.truncated = True
                    result.attachment_texts.append((name, text))
                    budget -= len(text)
                    included = True
                else:
                    result.warnings.append(f"attachment_unreadable:{pdf.reason}")
            else:
                result.truncated = True
        result.attachments.append(
            Attachment(name, ctype, len(payload), included))
    return result
