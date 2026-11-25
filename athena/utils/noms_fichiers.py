"""Pure stdlib filename, MIME extension, and dossier-prefix helpers.

Shared across ``models/document``, ``services/courrier``, and
``services/miroir_onedrive`` without introducing cross-layer import cycles.
"""

from __future__ import annotations

import mimetypes
import os
import re

__all__ = [
    "EXTENSION_MIME_TYPES",
    "DOWNLOAD_EXTENSIONS",
    "DOSSIER_PREFIX_RE",
    "zip_component",
    "zip_entry_basename",
    "extraire_prefixe_dossier",
]

# Expected MIME type for each allowed extension (used to detect a
# mismatch between the sniffed content and the client-supplied name).
EXTENSION_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".zip": "application/zip",
    ".eml": "message/rfc822",
    ".msg": "application/vnd.ms-outlook",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
    ".csv": "text/csv",
}

# Download-filename extensions, deterministic across platforms —
# mimetypes reads OS registries and may return ".jpe" for JPEG,
# ".mht"/None for rfc822/ms-outlook depending on the host.
DOWNLOAD_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "application/zip": ".zip",
    "message/rfc822": ".eml",
    "application/vnd.ms-outlook": ".msg",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "text/plain": ".txt",
    "text/csv": ".csv",
}

# Caractères que l'extraction Windows refuse — un zip ou un miroir OneDrive
# avec ces caractères échoue à l'ouverture sous Windows.
_WINDOWS_HOSTILES = '<>:"/\\|?*'
_DOS_RESERVES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# Linear CWE-1333 regex: explicit ASCII character classes, zero nested quantifiers.
DOSSIER_PREFIX_RE = re.compile(r"\A([0-9]{4}-[0-9]{3})(?:\Z|[\s\-–—_])")


def zip_component(name: str) -> str:
    """Assainit UN segment de chemin (nom de dossier ou de fichier) pour Windows.

    Balayages linéaires, aucun regex (doctrine CWE-1333).
    """
    propre = "".join(
        ch for ch in str(name or "") if ord(ch) >= 32 and ord(ch) != 127
    )
    propre = "".join(
        "-" if ch in _WINDOWS_HOSTILES else ch for ch in propre
    )
    sortie: list[str] = []
    for ch in propre:
        if ch.isspace():
            if sortie and sortie[-1] == " ":
                continue
            sortie.append(" ")
        elif ch == "-" and sortie and sortie[-1] == "-":
            continue
        else:
            sortie.append(ch)
    propre = "".join(sortie).strip().rstrip(". ")
    if propre and propre.split(".", 1)[0].upper() in _DOS_RESERVES:
        propre = "_" + propre
    if not propre:
        return "sans-titre"
    if len(propre) > 150:
        base, ext = os.path.splitext(propre)
        garde = ext if len(ext) <= 10 else ""
        propre = base[: 150 - len(garde)] + garde
    return propre


def zip_entry_basename(doc: dict) -> str:
    """Nom de fichier d'une entrée d'archive ou miroir, avec garantie d'extension."""
    file_type = doc.get("file_type", "")
    nom = zip_component(
        doc.get("display_name") or doc.get("original_filename")
        or doc.get("filename") or "document"
    )
    connues = [e for e, m in EXTENSION_MIME_TYPES.items() if m == file_type]
    if not any(nom.lower().endswith(e) for e in connues):
        nom += (
            DOWNLOAD_EXTENSIONS.get(file_type)
            or (connues[0] if connues else "")
            or (mimetypes.guess_extension(file_type) or "")
        )
    return nom


def extraire_prefixe_dossier(nom: str) -> str:
    """Extract a leading ``AAAA-NNN`` dossier number from a folder display name."""
    match = DOSSIER_PREFIX_RE.match(str(nom or "").strip())
    return match.group(1) if match else ""
