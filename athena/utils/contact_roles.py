"""A contact's role — the ONE vocabulary (2026-11-11).

``contact_role`` describes the contact itself (« Fournisseur »,
« Huissier(ère) »), never its place in a dossier: the dossier's party arrays
carry that. Pure — standard library only — so the connector's registry
(``mcp/tools.py``, which may not import ``models``) derives its enums from
it; ``models/partie.py``, the forms, DavX5's vCard CATEGORIES and the
generated documents' address rule read the same lists, and tests pin every
copy to them (``tests/test_contact_roles.py``).

Added on 2026-11-11, at the lawyer's request: « Fournisseur » (a supplier
the firm pays) and « Sténographe » (the one professional — beside lawyers,
notaries, bailiffs and experts — that had no role). The accounting forms
are to suggest these two groups as payees; that lands with its own lot.
Today the groups decide the work address of generated documents and who
stays off the phone.

DavX5's address book carries every contact EXCEPT the roles of
``PHONE_EXCLUDED_ROLES`` (the lawyer's decision, 2026-11-11: suppliers stay
in Athena). :func:`in_address_book` is the one membership test;
``services/carnet.py`` keeps the phone in step when a contact's role
changes.

vCard: the role travels as its French LABEL in CATEGORIES — the label is
the wire format, so a shipped label never changes; :func:`role_for_vcard_label`
maps one back, with the pre-rename aliases of ``VCARD_LEGACY_LABELS``. The
module refuses, at import, two roles sharing a label and a label holding a
comma (CATEGORIES is a comma-separated list).
"""

from __future__ import annotations

import unicodedata

# key → French label, in the order the forms offer them.
ROLE_LABELS: dict[str, str] = {
    "client": "Client",
    "partie_adverse": "Partie",
    "avocat_adverse": "Avocat(e)",
    "témoin": "Témoin",
    "expert": "Expert(e)",
    "huissier": "Huissier(ère)",
    "notaire": "Notaire",
    "sténographe": "Sténographe",
    "fournisseur": "Fournisseur",
    "autre": "Autre",
}
ROLES: tuple[str, ...] = tuple(ROLE_LABELS)

# The professionals (to be suggested as payees beside the suppliers).
# « avocat_adverse » is ANY lawyer (its label is « Avocat(e) »).
PROFESSIONAL_ROLES: tuple[str, ...] = (
    "avocat_adverse", "notaire", "huissier", "expert", "sténographe",
)
SUPPLIER_ROLES: tuple[str, ...] = ("fournisseur",)

# The payees: the roles whose contact holds a supplier record — TPS and TVQ
# numbers, a usual expense category, the firm's account number there
# (models/partie.SUPPLIER_RECORD_FIELDS). A bailiff or an expert bills the
# firm with TPS/TVQ like any supplier.
PAYEE_ROLES: tuple[str, ...] = SUPPLIER_ROLES + PROFESSIONAL_ROLES

# The roles whose WORK address a generated document prefers when the
# contact has one (utils/template_fields.selected_address; the rule the
# gabarits guide, athena/GABARITS_PLACEHOLDERS.md, describes).
WORK_PREFERRED_ROLES: frozenset[str] = frozenset(PROFESSIONAL_ROLES + SUPPLIER_ROLES)

# The roles DavX5's address book never carries.
PHONE_EXCLUDED_ROLES: frozenset[str] = frozenset(SUPPLIER_ROLES)

# Labels the vCards carried before a rename → their role, so a card synced
# before the rename comes back as its role instead of « autre ».
VCARD_LEGACY_LABELS: dict[str, str] = {
    "Partie adverse": "partie_adverse",
    "Avocat(e) adverse": "avocat_adverse",
}

# What a CATEGORIES label this vocabulary does not know becomes.
UNKNOWN_VCARD_ROLE = "autre"


def _folded(label: str) -> str:
    """A label as compared: NFC (a phone may send decomposed accents),
    outer spaces dropped."""
    return unicodedata.normalize("NFC", label or "").strip()


def _vcard_reverse_map() -> dict[str, str]:
    """label → role, built once; raises on a collision or a comma."""
    mapping: dict[str, str] = {}
    pairs = list(ROLE_LABELS.items())
    pairs += [(role, label) for label, role in VCARD_LEGACY_LABELS.items()]
    for role, label in pairs:
        if role not in ROLE_LABELS:
            raise ValueError(f"vCard label {label!r} names no role")
        if "," in label:
            raise ValueError(f"vCard label {label!r} holds a comma")
        key = _folded(label)
        if mapping.get(key, role) != role:
            raise ValueError(f"vCard label {label!r} names two roles")
        mapping[key] = role
    return mapping


_ROLE_BY_VCARD_LABEL: dict[str, str] = _vcard_reverse_map()


def role_label(role: str) -> str:
    """The label of *role*, ``""`` when unset or unknown."""
    return ROLE_LABELS.get(role or "", "")


def role_for_vcard_label(label: str) -> str:
    """The role a vCard CATEGORIES *label* names; ``UNKNOWN_VCARD_ROLE``
    for a label this vocabulary does not know."""
    return _ROLE_BY_VCARD_LABEL.get(_folded(label), UNKNOWN_VCARD_ROLE)


def in_address_book(partie: dict | None) -> bool:
    """Whether DavX5's address book carries *partie* (every role but
    ``PHONE_EXCLUDED_ROLES``)."""
    return (partie or {}).get("contact_role") not in PHONE_EXCLUDED_ROLES
