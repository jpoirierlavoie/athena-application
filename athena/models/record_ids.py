"""Whether a caller's id can name ONE top-level record.

Its own module, importing nothing: any model can ask without an import
cycle. It lived in ``models/document.py`` until 2026-11-07, when the
contacts and protocols models began asking too, and the import of
``models.document`` closed a cycle through its lazy import of
``models.dossier`` (CodeQL py/cyclic-import n° 902).
``models.document`` re-exports it: ``document.is_addressable_id`` stays
the name the house rules and the callers use.
"""

# Firestore's own ceiling on a document id (« Must be no longer than 1,500
# bytes »).
_FIRESTORE_ID_MAX_BYTES = 1500


def is_addressable_id(value: object) -> bool:
    """True when *value* can name ONE top-level record: a non-empty string
    with no « / », and an id Firestore itself accepts.

    The Firestore client joins then re-splits a document id on « / », so
    ``document("{id}/analyses/{analyse_id}")`` addresses a record DEEPER in
    the tree — an entry of a document's write-once analysis journal read as
    if it were the document, and a partial update staged ONTO it (lot 2A,
    T7: the connector hands ids through verbatim). No id this application
    mints carries a slash (UUIDv4, or the UUIDv5 of a system folder), so a
    slashed id is an ABSENCE, never a path. The web routes never reach this
    (Flask's default converter stops at « / »).

    Review of T7: the ids the SERVER refuses — « . », « .. », anything
    matching ``__.*__``, more than 1 500 bytes (Firestore's documented id
    rules) — are absences too. The client builds such a reference without
    complaint and the RPC fails: the strict reader then reported « lecture
    impossible — réessayez » for an id no retry will ever read, and ONE such
    id in a bulk move sank the whole batch as a store failure. Tested by
    hand, no regex: the id is the caller's string."""
    if not isinstance(value, str) or not value or "/" in value:
        return False
    if value in (".", ".."):
        return False
    if len(value) >= 4 and value.startswith("__") and value.endswith("__"):
        return False
    try:
        encoded = value.encode("utf-8")   # a lone surrogate is not UTF-8
    except UnicodeEncodeError:
        return False
    return len(encoded) <= _FIRESTORE_ID_MAX_BYTES
