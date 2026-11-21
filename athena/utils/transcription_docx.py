"""Pure stdlib OOXML (.docx) builders for Teams transcripts & meeting notes.

Zero Flask, zero Firestore, zero ``python-docx`` — produces clean, valid
WordprocessingML packages compatible with Microsoft Word, ``utils.pdf_text.extract_docx_text``,
and ``utils.docx_fill._structural_errors``.
"""

from __future__ import annotations

import io
import zipfile

from utils.docx_fill import _escape_xml
from utils.markdown_docx import DEFAULT_USABLE_WIDTH, markdown_to_ooxml
from utils.webvtt import TranscriptCue

_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    "</Types>"
)

_PACKAGE_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)

_DOCUMENT_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
    'Target="styles.xml"/>'
    "</Relationships>"
)

_STYLES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:docDefaults>"
    "<w:rPrDefault><w:rPr>"
    '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
    '<w:sz w:val="22"/><w:szCs w:val="22"/>'
    '<w:lang w:val="fr-CA"/>'
    "</w:rPr></w:rPrDefault>"
    '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
    "</w:docDefaults>"
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
    '<w:name w:val="Normal"/>'
    "</w:style>"
    "</w:styles>"
)

_SECT_PR_XML = (
    "<w:sectPr>"
    '<w:pgSz w:w="12240" w:h="15840"/>'
    '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
    'w:header="720" w:footer="720" w:gutter="0"/>'
    "</w:sectPr>"
)


def _esc(value: str) -> str:
    return _escape_xml(str(value or "").replace("\r\n", "\n").replace("\r", "\n"))


def _run(
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    color: str = "",
    sz: int | None = None,
) -> str:
    rpr_parts: list[str] = []
    if bold:
        rpr_parts.append("<w:b/><w:bCs/>")
    if italic:
        rpr_parts.append("<w:i/><w:iCs/>")
    if color:
        rpr_parts.append(f'<w:color w:val="{color}"/>')
    if sz is not None:
        rpr_parts.append(f'<w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/>')
    rpr = f"<w:rPr>{''.join(rpr_parts)}</w:rPr>" if rpr_parts else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{_esc(text)}</w:t></w:r>'


def _meta_line(label: str, value: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        return ""
    return (
        '<w:p><w:pPr><w:spacing w:before="0" w:after="60"/></w:pPr>'
        + _run(f"{label} : ", bold=True, color="4B5563", sz=20)
        + _run(cleaned, color="111827", sz=20)
        + "</w:p>"
    )


def _divider_paragraph() -> str:
    return (
        "<w:p><w:pPr>"
        '<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="6" w:color="D1D5DB"/></w:pBdr>'
        '<w:spacing w:before="80" w:after="200"/>'
        "</w:pPr></w:p>"
    )


def _header_blocks(
    *,
    kicker: str,
    title: str,
    dossier_label: str,
    date_label: str,
    extra_meta: tuple[tuple[str, str], ...] = (),
) -> str:
    parts: list[str] = [
        '<w:p><w:pPr><w:spacing w:before="0" w:after="60"/><w:keepNext/></w:pPr>'
        + _run(kicker.upper(), bold=True, color="4F46E5", sz=18)
        + "</w:p>",
        '<w:p><w:pPr><w:spacing w:before="0" w:after="120"/><w:keepNext/></w:pPr>'
        + _run(title or "Réunion Teams", bold=True, color="111827", sz=32)
        + "</w:p>",
    ]
    for label, val in (
        ("Dossier", dossier_label),
        ("Date", date_label),
        *extra_meta,
    ):
        line = _meta_line(label, val)
        if line:
            parts.append(line)
    parts.append(_divider_paragraph())
    return "".join(parts)


def _wrap_ooxml_document(body_xml: str) -> bytes:
    """Package WordprocessingML body fragments into a valid ``.docx`` archive."""
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body_xml}{_SECT_PR_XML}</w:body>"
        "</w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML.encode("utf-8"))
        zf.writestr("_rels/.rels", _PACKAGE_RELS_XML.encode("utf-8"))
        zf.writestr("word/_rels/document.xml.rels", _DOCUMENT_RELS_XML.encode("utf-8"))
        zf.writestr("word/styles.xml", _STYLES_XML.encode("utf-8"))
        zf.writestr("word/document.xml", document_xml.encode("utf-8"))
    return buf.getvalue()


def build_transcript_docx(
    *,
    title: str,
    dossier_label: str,
    date_label: str,
    speakers: list[str],
    cues: list[TranscriptCue],
) -> bytes:
    """Build a timestamped, speaker-attributed ``.docx`` verbatim transcript."""
    participants = ", ".join(s for s in speakers if str(s or "").strip())
    extra = (("Participants", participants),) if participants else ()
    blocks: list[str] = [
        _header_blocks(
            kicker="Transcription de réunion Microsoft Teams",
            title=title,
            dossier_label=dossier_label,
            date_label=date_label,
            extra_meta=extra,
        )
    ]
    if not cues:
        blocks.append(
            "<w:p>"
            + _run("Aucun segment de parole détecté dans la transcription.", italic=True, color="6B7280")
            + "</w:p>"
        )
    else:
        for cue in cues:
            runs: list[str] = [_run(f"[{cue.start_hms}] ", color="6B7280", sz=19)]
            if cue.speaker:
                runs.append(_run(f"{cue.speaker} : ", bold=True, color="111827", sz=22))
            runs.append(_run(cue.text, color="1F2937", sz=22))
            blocks.append(
                '<w:p><w:pPr><w:spacing w:before="0" w:after="120" w:line="276" w:lineRule="auto"/></w:pPr>'
                + "".join(runs)
                + "</w:p>"
            )
    return _wrap_ooxml_document("".join(blocks))


def build_standalone_note_docx(
    *,
    title: str,
    dossier_label: str,
    date_label: str,
    category_label: str,
    markdown_text: str,
) -> bytes:
    """Build a standalone ``.docx`` note when no active ``note`` gabarit exists."""
    extra = (("Catégorie", category_label),) if category_label else ()
    header = _header_blocks(
        kicker="Notes de réunion",
        title=title,
        dossier_label=dossier_label,
        date_label=date_label,
        extra_meta=extra,
    )
    body_ooxml = markdown_to_ooxml(
        markdown_text or "",
        base_rpr='<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/><w:sz w:val="22"/><w:szCs w:val="22"/>',
        usable_width=DEFAULT_USABLE_WIDTH,
    )
    return _wrap_ooxml_document(header + body_ooxml)


__all__ = [
    "build_standalone_note_docx",
    "build_transcript_docx",
]
