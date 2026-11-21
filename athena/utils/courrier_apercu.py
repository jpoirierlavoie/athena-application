"""Bounded attachment triage and page-1 preview extraction for Courrier.

Pure utility (stdlib + ``utils.pdf_text`` + ``utils.email_text``): no Flask, no
``models`` import, no network. Evaluates Graph attachment metadata to skip inline,
cloud-reference, calendar/contact, and signature-image files without downloading
bytes, and extracts normalized 400-character previews from PDF, DOCX, TXT, and
CSV attachments within bounded memory.
"""

from __future__ import annotations

import codecs
import os
import re
from typing import Any, Optional

from utils.email_text import tidy_text
from utils.pdf_text import DocumentTextError, extract_docx_text, extract_pdf_pages

APERCU_MAX_CHARS = 400
APERCU_MAX_BYTES = 10 * 1024 * 1024
SIGNATURE_IMAGE_MAX_BYTES = 30 * 1024
_TEXT_SAMPLE_BYTES = 65536

_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

MOTIFS_IGNORE: tuple[str, ...] = (
    "incorporee",
    "lien_infonuagique",
    "calendrier_contact",
    "image_signature",
)
MOTIFS_APERCU_OMIS: tuple[str, ...] = (
    "ignoree",
    "non_demande",
    "format_non_texte",
    "taille_depassee",
    "chiffre",
    "aucun_texte",
    "illisible",
)

# Linear CWE-1333 regex: explicit ASCII digits, no dot wildcard, no nested quantifiers.
_SIGNATURE_IMAGE_RE = re.compile(
    r"\Aimage[0-9]+\.(?:png|jpg|jpeg|gif)\Z", re.IGNORECASE
)

__all__ = [
    "APERCU_MAX_BYTES",
    "APERCU_MAX_CHARS",
    "MOTIFS_APERCU_OMIS",
    "MOTIFS_IGNORE",
    "SIGNATURE_IMAGE_MAX_BYTES",
    "extraire_apercu",
    "motif_ignore_piece",
    "peut_extraire_apercu",
]


def _normalized_ext(filename: str) -> str:
    return os.path.splitext((filename or "").strip())[1].lower()


def _normalized_mime(content_type: str) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _kind_for_apercu(filename: str, content_type: str = "") -> Optional[str]:
    ext = _normalized_ext(filename)
    if ext:
        if ext == ".pdf":
            return "pdf"
        if ext == ".docx":
            return "docx"
        if ext in (".txt", ".csv"):
            return "text"
        return None
    mime = _normalized_mime(content_type)
    if mime == "application/pdf":
        return "pdf"
    if mime == _DOCX_MIME:
        return "docx"
    if mime in ("text/plain", "text/csv"):
        return "text"
    return None


def motif_ignore_piece(att: dict[str, Any]) -> Optional[str]:
    """Return the skip reason for an attachment from Graph metadata alone."""
    odata_type = str(att.get("@odata.type") or "").strip().lower()
    if odata_type == "#microsoft.graph.referenceattachment":
        return "lien_infonuagique"
    if bool(att.get("isInline")):
        return "incorporee"
    name = str(att.get("name") or "").strip()
    if _normalized_ext(name) in (".ics", ".vcf"):
        return "calendrier_contact"
    size = att.get("size")
    if (
        _SIGNATURE_IMAGE_RE.match(name)
        and isinstance(size, int)
        and not isinstance(size, bool)
        and 0 <= size < SIGNATURE_IMAGE_MAX_BYTES
    ):
        return "image_signature"
    return None


def peut_extraire_apercu(
    filename: str,
    content_type: str = "",
    odata_type: str = "",
) -> bool:
    """Return True when the attachment type supports a text preview."""
    odata_norm = (odata_type or "").strip().lower()
    if odata_norm in (
        "#microsoft.graph.itemattachment",
        "#microsoft.graph.referenceattachment",
    ):
        return False
    return _kind_for_apercu(filename, content_type) is not None


def _extract_pdf_apercu(data: bytes, max_chars: int) -> tuple[str, Optional[str]]:
    res = extract_pdf_pages(
        data,
        first_page=1,
        last_page=1,
        char_cap=max(max_chars * 4, 1600),
    )
    if not res.readable:
        return ("", "chiffre" if res.reason == "encrypted" else "illisible")
    raw_page = res.pages[0].text if (res.pages and res.pages[0].has_text) else ""
    cleaned = tidy_text(raw_page)
    if not cleaned:
        if any(w.startswith("page_extraction_failed:") for w in res.warnings):
            return ("", "illisible")
        return ("", "aucun_texte")
    return (cleaned[:max_chars], None)


def _extract_docx_apercu(data: bytes, max_chars: int) -> tuple[str, Optional[str]]:
    try:
        paragraphs = extract_docx_text(data)
    except DocumentTextError:
        return ("", "illisible")
    cleaned = tidy_text("\n".join(paragraphs))
    if not cleaned:
        return ("", "aucun_texte")
    return (cleaned[:max_chars], None)


def _extract_text_apercu(data: bytes, max_chars: int) -> tuple[str, Optional[str]]:
    if not data:
        return ("", "aucun_texte")
    if b"\x00" in data:
        return ("", "illisible")
    sample = data[:_TEXT_SAMPLE_BYTES]
    truncated_bytes = len(data) > _TEXT_SAMPLE_BYTES
    try:
        if truncated_bytes:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
            decoded = decoder.decode(sample, final=False).lstrip("\ufeff")
        else:
            decoded = sample.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        decoded = sample.decode("cp1252", errors="replace")
    cleaned = tidy_text(decoded)
    if not cleaned:
        return ("", "aucun_texte")
    return (cleaned[:max_chars], None)


def extraire_apercu(
    data: bytes,
    filename: str,
    content_type: str = "",
    *,
    max_chars: int = APERCU_MAX_CHARS,
) -> tuple[str, Optional[str]]:
    """Extract up to ``max_chars`` normalized characters of attachment text."""
    if max_chars <= 0:
        return ("", "aucun_texte")
    kind = _kind_for_apercu(filename, content_type)
    if kind == "pdf":
        return _extract_pdf_apercu(data, max_chars)
    if kind == "docx":
        return _extract_docx_apercu(data, max_chars)
    if kind == "text":
        return _extract_text_apercu(data, max_chars)
    return ("", "format_non_texte")
