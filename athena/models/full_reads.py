"""Whole-collection reads inside a transaction, capped — lot B (2026-11-07).

The financial statements read every administration and trust entry, every
invoice, every time entry and disbursement, in ONE read-only transaction
(``services/financial_statements``): one consistent snapshot, no tear
between collections read a moment apart. Each model exposes a strict
reader built on :func:`stream_all`; none holds this module's state.

Strict, by CLAUDE.md « A failed read never passes for absence »: a read
error propagates to the caller, and so does a truncation — a total built
on a capped read is never reported (« Nor does a capped read pass for
complete »). The cap is far above the firm's volumes (the administration
ledger grows by a few thousand rows a year, invoices by dozens): it bounds
a runaway read, it never trims a real one.

No ``select()`` projection: a field a projection forgot reads as ABSENT,
which every rule here takes as zero — a silent wrong figure, for a
bandwidth saving these volumes do not need.

The ``id`` of every row is the document's own (``snap.id``), whatever a
stored ``id`` field says.

Private-API reliance (2026-11-11), pinned by ``tests/test_full_reads.py``:
a row's fields are the snapshot's private ``_data``, not ``to_dict()``.
The library deep-copies the decoded fields into ``_data`` when it builds
the snapshot, and ``to_dict()`` deep-copies them a SECOND time — about a
fifth of the work of turning a read into rows (local benchmarks). The
snapshot never leaves :func:`stream_all`'s loop, so nothing else holds
that copy: each row stays an object graph of its own, as before. A
library without a ``dict`` ``_data`` falls back to ``to_dict()`` (slower,
same rows); the tests pin the fast path, so a dependency bump that loses
it fails CI instead.
"""

from __future__ import annotations

from typing import Optional

#: The most rows one whole-collection read may return before it RAISES.
FULL_READ_CAP = 100_000


class TruncatedRead(RuntimeError):
    """A whole-collection read reached its cap: the rows it returned are
    not the collection, and nothing may be computed from them."""


def _fields(snap) -> dict:
    """*snap*'s decoded fields, without ``to_dict()``'s second deep copy
    (the private-API reliance the module docstring describes). A snapshot
    that does not exist has no fields, as ``to_dict()``'s None had."""
    if not snap.exists:
        return {}
    data = getattr(snap, "_data", None)
    if isinstance(data, dict):
        return data
    return snap.to_dict() or {}


def stream_all(query, *, transaction, cap: Optional[int] = None) -> list[dict]:
    """Every document of *query* (a collection reference or a query), read
    inside *transaction*, as ``{**fields, "id": snap.id}`` — or
    :class:`TruncatedRead` when more than *cap* (default ``FULL_READ_CAP``,
    read at call time) exist. The stream is consumed here, inside the
    caller's transaction body (a stream consumed after the body returns has
    no transaction left)."""
    if cap is None:
        cap = FULL_READ_CAP
    rows = []
    for snap in query.limit(cap + 1).stream(transaction=transaction):
        rows.append({**_fields(snap), "id": snap.id})
    if len(rows) > cap:
        raise TruncatedRead(f"whole-collection read truncated at {cap} rows")
    return rows
