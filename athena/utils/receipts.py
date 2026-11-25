"""Shared pure receipt constants and extension helpers (no Flask, no Firestore).

Used by both ``models/admin_ledger`` and ``models/trust`` so accepted formats,
size cap, accept attribute, and French validation messages stay single-sourced.
"""

from utils.format_fr import formats_label

__all__ = [
    "RECEIPT_MIME_TYPES",
    "RECEIPT_EXTENSIONS",
    "RECEIPT_ACCEPT",
    "RECEIPT_FORMATS_FR",
    "MAX_RECEIPT_SIZE",
    "RECEIPT_MAX_SIZE_FR",
    "RECEIPT_HINT_FR",
    "RECEIPT_TYPE_REFUSED_FR",
    "RECEIPT_SIZE_REFUSED_FR",
    "RECEIPT_CONTENT_MISMATCH_FR",
    "RECEIPT_MD5_MAX",
    "receipt_extension",
]

# Supporting receipt MIME whitelist — every uploaded receipt MUST verify both
# extension and 512-byte magic header (models/document._sniff_header).
RECEIPT_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".eml": "message/rfc822",
    ".msg": "application/vnd.ms-outlook",
}
RECEIPT_EXTENSIONS = tuple(RECEIPT_MIME_TYPES)
RECEIPT_ACCEPT = ",".join(
    RECEIPT_EXTENSIONS + tuple(dict.fromkeys(RECEIPT_MIME_TYPES.values()))
)
RECEIPT_FORMATS_FR = formats_label(RECEIPT_MIME_TYPES)
MAX_RECEIPT_SIZE = 10 * 1024 * 1024  # 10 MB — kept by the lawyer, 2026-11-10
RECEIPT_MAX_SIZE_FR = f"{MAX_RECEIPT_SIZE // (1024 * 1024)} Mo"
RECEIPT_HINT_FR = f"{RECEIPT_FORMATS_FR}, {RECEIPT_MAX_SIZE_FR} max."
RECEIPT_TYPE_REFUSED_FR = (
    f"Type de fichier non autorisé. Formats acceptés : {RECEIPT_FORMATS_FR}."
)
RECEIPT_SIZE_REFUSED_FR = (
    "Une pièce justificative doit faire entre 1 octet et "
    f"{RECEIPT_MAX_SIZE_FR}."
)
RECEIPT_CONTENT_MISMATCH_FR = (
    "Le contenu du fichier ne correspond pas à son extension."
)
RECEIPT_MD5_MAX = 64


def receipt_extension(filename: str) -> str:
    """Return the lower-cased extension of *filename*, dot included, or ``""``."""
    name = str(filename or "")
    return "." + name.rsplit(".", 1)[1].lower() if "." in name else ""
