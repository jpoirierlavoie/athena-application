"""DavX5's address book after a contact write — the one door (2026-11-11).

DavX5's address book (``dav/carddav.py``, sync collection « parties »)
carries every contact EXCEPT the roles of
``utils/contact_roles.PHONE_EXCLUDED_ROLES``: suppliers stay in Athena, the
lawyer's decision of 2026-11-11. A contact's role changes on the web form,
through the connector or by any later writer; whatever the path, the phone
must follow it:

* a contact that is not (or no longer) in the address book gets a
  TOMBSTONE — the next sync tells DavX5 to delete its copy (the listing
  alone never does: a resource that merely stops being listed stays on the
  phone for good);
* a contact that is (or is again) in the address book loses any stale
  tombstone, so it is listed and fetched like a new card;
* the collection's CTag is bumped in the SAME commit as those tombstones —
  both reach the phone or neither does (``dav.sync.update_membership``,
  which ``tests/test_mcp_framework_guards.py`` counts as a CTag bump).

Every contact-write call site runs :func:`apres_ecriture_contact` with the
contact(s) AS WRITTEN, and a contact delete runs
:func:`apres_suppression_contact` — never a bare ``bump_ctag("parties")`` or
tombstone call (``tests/test_carnet.py`` sweeps the code base for one).
Change Impact item 2.

A failure propagates, as ``bump_ctag`` did at these call sites; the
connector wraps it (``mcp/handlers._bump_parties_ctag``), because its write
is already committed and must not be reported as failed.

The tombstone is the FIRST signal, not the only one: every full
sync-collection also reports each contact the address book leaves out as
deleted, from the same read (``dav/carddav._handle_sync_collection``). So a
supplier whose tombstone never landed (this door's commit failed after the
contact's own) or was pruned after ``dav.sync.TOMBSTONE_TTL_DAYS`` still
leaves the phone at its next sync. A DELETED contact keeps the tombstone
as its only signal, and the 30-day limit that comes with it.
"""

from __future__ import annotations

from dav.sync import update_membership
from utils.contact_roles import in_address_book

# The address book's sync collection (dav_sync/parties).
COLLECTION = "parties"


def apres_ecriture_contact(*parties: dict | None) -> str:
    """Keep the address book in step with *parties*, just created or
    updated, and bump its CTag — one commit. Returns the new token.

    Called with no contact (a request that wrote contacts it does not hold,
    such as Réception's opposing parties), it bumps alone: the listing then
    carries them, as every one of them is in the address book.
    """
    present: list[str] = []
    absent: list[str] = []
    for partie in parties:
        partie_id = (partie or {}).get("id")
        if not partie_id:
            continue
        (present if in_address_book(partie) else absent).append(partie_id)
    return update_membership(COLLECTION, present=present, absent=absent)


def apres_suppression_contact(partie_id: str) -> str:
    """A deleted contact's tombstone and the CTag bump — one commit.
    Returns the new token."""
    return update_membership(COLLECTION, present=[], absent=[partie_id])
