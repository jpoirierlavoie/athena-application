"""RFC 5545 CLASS & Outlook sensitivity classification helpers.

Pure stdlib module (no Flask, no models imports) so utils/graph_miroir.py,
models/hearing.py, models/task.py, models/note.py, and the shared calendar
feed share ONE source of truth.

Fail-closed secrecy policy:
- Dossier-linked items default to ``CONFIDENTIAL`` and NEVER accept or emit
  ``PUBLIC`` (``CONFIDENTIAL`` or ``PRIVATE`` only).
- Non-dossier items (``Général``) accept ``CONFIDENTIAL``, ``PRIVATE``, or
  ``PUBLIC``, with a safe non-public default (``PRIVATE``) when unset.
"""

from typing import Optional

__all__ = [
    "CLASS_CONFIDENTIAL",
    "CLASS_PRIVATE",
    "CLASS_PUBLIC",
    "VALID_CLASSIFICATIONS",
    "DOSSIER_CLASSIFICATIONS",
    "CLASSIFICATION_LABELS_FR",
    "GRAPH_SENSITIVITY_MAP",
    "DOSSIER_PUBLIC_FORBIDDEN_ERROR",
    "INVALID_CLASSIFICATION_ERROR",
    "has_dossier_link",
    "default_classification",
    "normalize_classification_value",
    "effective_classification",
    "validation_error",
    "graph_sensitivity",
]

CLASS_CONFIDENTIAL = "CONFIDENTIAL"
CLASS_PRIVATE = "PRIVATE"
CLASS_PUBLIC = "PUBLIC"

VALID_CLASSIFICATIONS: tuple[str, ...] = (
    CLASS_CONFIDENTIAL,
    CLASS_PRIVATE,
    CLASS_PUBLIC,
)
DOSSIER_CLASSIFICATIONS: tuple[str, ...] = (
    CLASS_CONFIDENTIAL,
    CLASS_PRIVATE,
)

CLASSIFICATION_LABELS_FR: dict[str, str] = {
    CLASS_CONFIDENTIAL: "Confidentiel",
    CLASS_PRIVATE: "Privé",
    CLASS_PUBLIC: "Public",
}

GRAPH_SENSITIVITY_MAP: dict[str, str] = {
    CLASS_CONFIDENTIAL: "confidential",
    CLASS_PRIVATE: "private",
    CLASS_PUBLIC: "normal",
}

DOSSIER_PUBLIC_FORBIDDEN_ERROR = (
    "Un élément lié à un dossier ne peut pas être classé public."
)
INVALID_CLASSIFICATION_ERROR = "Classification invalide."


def has_dossier_link(doc: Optional[dict]) -> bool:
    """True when the record is linked to a client dossier."""
    if not isinstance(doc, dict):
        return False
    return bool(str(doc.get("dossier_id") or "").strip())


def default_classification(*, has_dossier: bool) -> str:
    """Return the safe non-public default classification."""
    return CLASS_CONFIDENTIAL if has_dossier else CLASS_PRIVATE


def normalize_classification_value(raw: object) -> str:
    """Uppercase trimmed classification string (empty if unusable)."""
    if not isinstance(raw, str):
        return ""
    return raw.strip().upper()


def effective_classification(doc: Optional[dict]) -> str:
    """Resolve effective RFC 5545 CLASS fail-closed for *doc*.

    Even if a legacy or malformed stored record linked to a dossier carries
    ``PUBLIC`` or an unknown token, this always refuses ``PUBLIC`` and falls
    back to ``CONFIDENTIAL`` (or ``PRIVATE`` for non-dossier items).
    """
    has_dossier = has_dossier_link(doc)
    raw = normalize_classification_value(
        doc.get("classification") if isinstance(doc, dict) else ""
    )
    if has_dossier:
        return raw if raw in DOSSIER_CLASSIFICATIONS else CLASS_CONFIDENTIAL
    return raw if raw in VALID_CLASSIFICATIONS else CLASS_PRIVATE


def validation_error(raw: object, *, has_dossier: bool) -> Optional[str]:
    """Validate an explicit classification value; return French error or None."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if not isinstance(raw, str):
        return INVALID_CLASSIFICATION_ERROR
    norm = normalize_classification_value(raw)
    if norm not in VALID_CLASSIFICATIONS:
        return INVALID_CLASSIFICATION_ERROR
    if has_dossier and norm == CLASS_PUBLIC:
        return DOSSIER_PUBLIC_FORBIDDEN_ERROR
    return None


def graph_sensitivity(doc: Optional[dict]) -> str:
    """Map effective classification to Microsoft Graph ``sensitivity``."""
    return GRAPH_SENSITIVITY_MAP[effective_classification(doc)]
