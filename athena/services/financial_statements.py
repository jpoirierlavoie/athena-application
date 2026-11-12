"""The financial statements' read — lot B (2026-11-07).

ONE read-only Firestore transaction per request — a page, a PDF, or a
connector series of periods (``build_series``) — reads every collection the
statements rest on (``models/financial_statements.COLLECTIONS``): one
consistent snapshot, so a register write landing between two reads can
never tear the balance sheet. Each collection is read by its model's strict
whole-collection reader (``models/full_reads``): a failed or truncated
read makes the sections that need it « indisponible », never zero
(CLAUDE.md « A failed read never passes for absence »). A transaction that
cannot begin, or whose (empty) commit fails, loses every collection.

The « Journal des radiations et intérêts » (lot 3c, ``build_write_off_journal``)
reads the invoices alone through the same reader — its own read-only
transaction, the same strict reader, cap and log lines —, so it fails
exactly as the statements' invoice sections do.

The transaction comes from a model module's client (``admin_ledger.db``,
read at call time): this module holds no client of its own.

This module orchestrates and owns no rule: every figure and label is
``models/financial_statements``'.
"""

from __future__ import annotations

from datetime import date, datetime

from google.api_core import exceptions as api_exceptions
from google.cloud import firestore

from models import admin_ledger as al
from models import expense, full_reads, invoice, time_entry, trust
from models import financial_statements as fs
from utils.logging_setup import log_unexpected

#: Each collection's strict reader, in ``fs.COLLECTIONS`` order (pinned).
READERS = {
    "admin_accounts": al.list_accounts_strict,
    "admin_transactions": al.list_all_transactions_strict,
    "admin_reconciliations": al.list_reconciliations_strict,
    "invoices": invoice.list_invoices_strict,
    "trust_accounts": trust.list_accounts_strict,
    "trust_transactions": trust.list_all_transactions_strict,
    "time_entries": time_entry.list_all_strict,
    "expenses": expense.list_all_strict,
}


def _root_error(exc: BaseException) -> BaseException:
    """The first Google API error in *exc*'s context chain, else *exc*: when
    BeginTransaction fails, the transactional decorator's rollback raises a
    ValueError over it (no transaction id to roll back), hiding the cause."""
    seen = exc
    while seen is not None:
        if isinstance(seen, api_exceptions.GoogleAPIError):
            return seen
        seen = seen.__context__
    return exc


def read_snapshot(names: tuple = fs.COLLECTIONS) -> fs.Snapshot:
    """The collections *names* of the statements — every one, by default —
    read in ONE read-only transaction; a collection whose read fails is
    ``None`` with its reason in ``failures``. A collection left out of
    *names* is ``None`` with no reason: nothing may ask for it."""
    snapshot = fs.Snapshot()
    transaction = al.db.transaction(read_only=True)

    @firestore.transactional
    def _read(txn) -> None:
        for name in names:
            try:
                setattr(snapshot, name, READERS[name](txn))
            except full_reads.TruncatedRead:
                snapshot.failures[name] = "truncated"
                log_unexpected("financial statements: read truncated",
                               exc_info=False, collection=name)
            except Exception as exc:
                snapshot.failures[name] = "read"
                log_unexpected("financial statements: read failed",
                               exc_info=False, collection=name,
                               error_type=type(exc).__name__)

    try:
        _read(transaction)
    except Exception as exc:
        # The transaction could not begin, or its empty commit failed: no
        # read of it is vouched for.
        log_unexpected("financial statements: read transaction failed",
                       exc_info=False, error_type=type(_root_error(exc)).__name__)
        lost = fs.Snapshot()
        lost.failures = {name: "read" for name in names}
        return lost
    return snapshot


def row_counts(snapshot: fs.Snapshot) -> dict:
    """``{collection: rows read}`` (absent when the read failed) — ids and
    counts only, for the export log line."""
    return {name: len(getattr(snapshot, name)) for name in fs.COLLECTIONS
            if getattr(snapshot, name) is not None}


def build(period, *, today: date, now: datetime) -> tuple:
    """``(statements, view, row counts)`` for *period*: the snapshot read,
    the model's computation, its labelled presentation."""
    snapshot = read_snapshot()
    statements = fs.compute(snapshot, period, today=today, now=now)
    return statements, fs.present(statements), row_counts(snapshot)


def build_series(periods: list, *, today: date, now: datetime) -> tuple:
    """``(statements of each period, in order; the snapshot)`` — ONE read
    for the whole series (the connector's ``get_financial_statements``), so
    every period of a comparative rests on the same consistent data."""
    snapshot = read_snapshot()
    return [fs.compute(snapshot, p, today=today, now=now) for p in periods], snapshot


#: What the « Journal des radiations et intérêts » reads (lot 3c): the
#: invoices alone — their write-off events are its rows.
JOURNAL_COLLECTIONS = ("invoices",)


def build_write_off_journal(period, *, today: date) -> fs.WriteOffJournal:
    """The journal of *period* (lot 3c): the invoices read through
    :func:`read_snapshot`, then the model's derivation
    (``models/financial_statements.write_off_journal``) — « indisponible »
    when that read failed or was truncated."""
    return fs.write_off_journal(read_snapshot(JOURNAL_COLLECTIONS), period,
                                today=today)
