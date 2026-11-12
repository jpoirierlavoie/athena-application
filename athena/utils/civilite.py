"""Honorific titles of a natural person — the ONE vocabulary (2026-10-30).

Pure — no Firestore, no Flask, no model import — so the contact model
(``models/partie.py``), the gabarit fields (``utils/template_fields.py``),
the conflict-check matcher (``utils/rapprochement.py``), the connector's
enum (``mcp/tools.py``) and the contact routes all read one definition, and
a test pins every derived copy to it.

The stored value is the abbreviation the Office québécois de la langue
française gives (« Dr », « Dre », « Pr », « Pre » take no period; « M. »
does), plus « L'hon. », the courts' form for a judge. vCard 4.0 carries it in
the honorific-prefixes component of ``N`` (RFC 6350 §6.2.2), which DAVx⁵ maps
to Android's « Préfixe » field.

What a phone sends is NORMALIZED, not refused: a spelling this module knows
(« Dr. », « Docteure », « Maître », « Mr », « L'honorable »…) becomes its key;
an unknown one is kept as received (refusing the PUT would stop DavX5 from
syncing, dropping it would lose the lawyer's data). ``models/partie.py``
declares that exception; the web form and the connector accept keys only.

DavX5 joins a multi-valued N component with spaces and splits it again on
the way back (vcard4android ``ContactReader``/``ContactWriter``), so a list
is joined the same way here: « Ad. E. » leaves as one value, returns as
``["Ad.", "E."]`` and is joined back to « Ad. E. ».
"""

from __future__ import annotations

import re
import unicodedata

# key → full French label, in the order the form offers them.
PREFIX_LABELS: dict[str, str] = {
    "M.": "Monsieur",
    "Mme": "Madame",
    "Me": "Maître",
    "Dr": "Docteur",
    "Dre": "Docteure",
    "Pr": "Professeur",
    "Pre": "Professeure",
    "L'hon.": "L'honorable",
}
PREFIXES: tuple[str, ...] = tuple(PREFIX_LABELS)

# Spellings a phone or a typist may use, beyond each key and its label.
# « Doctor » and « Prof. » carry no gender: they map to the masculine form,
# which the lawyer corrects on the fiche if need be. « Mlle » is left as
# received — it is no longer used, and mapping it to « Mme » would be a
# choice that is not ours to make.
_EXTRA_SPELLINGS: dict[str, str] = {
    "Mr": "M.",
    "Mister": "M.",
    "Mrs": "Mme",
    "Ms": "Mme",
    "Doctor": "Dr",
    "Prof": "Pr",
    "Professor": "Pr",
    "Hon": "L'hon.",
    "Honorable": "L'hon.",
    "Honourable": "L'hon.",
    "The Honorable": "L'hon.",
    "The Honourable": "L'hon.",
    "The Hon": "L'hon.",
}

_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'", "‘": "'", "`": "'"})
_SPACES_RE = re.compile(r"\s+")


def fold(text: str) -> str:
    """The comparison form of a title: no accents, no periods, case-folded,
    one kind of apostrophe, single spaces."""
    bare = "".join(
        c for c in unicodedata.normalize("NFD", text or "")
        if unicodedata.category(c) != "Mn"
    )
    bare = bare.translate(_APOSTROPHES).replace(".", "").casefold()
    return _SPACES_RE.sub(" ", bare).strip()


def _build_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    spellings = [(key, key) for key in PREFIXES]
    spellings += [(label, key) for key, label in PREFIX_LABELS.items()]
    spellings += list(_EXTRA_SPELLINGS.items())
    for spelling, key in spellings:
        folded = fold(spelling)
        if aliases.get(folded, key) != key:
            raise ValueError(f"title spelling {spelling!r} maps to two keys")
        aliases[folded] = key
    return aliases


_ALIASES = _build_aliases()


def as_text(value: object) -> str:
    """A name component as one string: a list (a multi-valued vCard
    component) is joined with spaces, as DavX5 does; anything else that is
    not a string is empty."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return " ".join(
            part.strip() for part in value
            if isinstance(part, str) and part.strip()
        )
    return ""


def canonical_prefix(value: object) -> str:
    """The stored form of *value*: its key when the spelling is known, the
    text as received otherwise (never refused, never dropped)."""
    text = as_text(value)
    if not text:
        return ""
    return _ALIASES.get(fold(text), text)


def is_known(prefix: object) -> bool:
    """Whether *prefix* is a key of the vocabulary (or empty)."""
    return prefix == "" or prefix in PREFIX_LABELS


def split_prefix(name: str) -> tuple[str, str]:
    """``(title, rest)`` when *name* starts with a title of the vocabulary
    followed by a name, else ``("", name)`` — a lookup on the first word,
    never a ``startswith`` on the key."""
    stripped = (name or "").strip()
    head, _, rest = stripped.partition(" ")
    rest = rest.strip()
    if head in PREFIX_LABELS and rest:
        return head, rest
    return "", stripped


def strip_prefix(name: str) -> str:
    """*name* without its leading title (dossier snapshots carry it)."""
    return split_prefix(name)[1]


def _part(value: object) -> str:
    # A stored string is used verbatim (the legacy output of display_name,
    # which the dossier snapshots were taken from); only a list is joined.
    return value if isinstance(value, str) else as_text(value)


def full_name(partie: dict, *, honorific: bool) -> str:
    """A contact's name: the legal name of an organization; « [title] first
    last » for a person. Never the suffix or the middle names — the name
    fields of documents and the dossier snapshots are built from this."""
    if partie.get("type") == "organization":
        return _part(partie.get("organization_name", ""))
    parts = [
        _part(partie.get("prefix", "")) if honorific else "",
        _part(partie.get("first_name", "")),
        _part(partie.get("last_name", "")),
    ]
    return " ".join(p for p in parts if p).strip()


def payee_name(partie: dict) -> str:
    """The name a counterparty pick writes (the lawyer's decision of
    2026-11-11): the trade name (« Nom d'emprunt ») if any, else the legal
    name of an organization or the person's first and last names — never a
    title such as « Me »."""
    return _part(partie.get("trade_name", "")).strip() or full_name(
        partie, honorific=False)
