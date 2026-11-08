"""Accounting periods for the financial statements (lot B, 2026-11-07).

Pure — no Firestore, no Flask, no model import. A period is a calendar
month, a calendar quarter or a calendar year: the lawyer's fiscal year is
the calendar year (his decision of 2026-11-07).

Its slug is the ONE query-string value the statements page reads
(``?periode=``): ``2026-10`` (a month), ``2026-T3`` (a quarter), ``2026``
(a year). The grammar is CLOSED and parsed character by character, never
with a regex (CLAUDE.md « Inputs and errors », CWE-1333: the slug is the
request's). A slug is refused, with a French sentence, when it is
malformed, when its period starts before the books (``books_start``, the
caller's — ``models/financial_statements.BOOKS_START``) or when it starts
after today (Montréal's day — the caller passes ``utils.deadlines
.today_mtl()``, the one « today » of every surface but invoice dates).

A period that holds today is IN PROGRESS: the statements are computed up
to today (``as_of``) and say so. Days are ``datetime.date`` — the caller
buckets a stored date-only value by its UTC calendar day, an instant by
its Montréal day (Architecture Rule 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from utils.format_fr import format_date_fr, month_name_fr

#: The kinds of period, in the order the selector groups them.
KINDS = ("mois", "trimestre", "annee")
#: The selector's group labels, by kind.
GROUP_LABELS = {"mois": "Mois", "trimestre": "Trimestres", "annee": "Années"}

_DIGITS = "0123456789"


@dataclass(frozen=True)
class Period:
    """One calendar month, quarter or year: ``index`` is the month (1–12),
    the quarter (1–4), or 1 for a year; ``end`` is the period's last day,
    inclusive."""

    kind: str
    year: int
    index: int

    @property
    def start(self) -> date:
        if self.kind == "mois":
            return date(self.year, self.index, 1)
        if self.kind == "trimestre":
            return date(self.year, 3 * (self.index - 1) + 1, 1)
        return date(self.year, 1, 1)

    @property
    def end(self) -> date:
        if self.kind == "mois":
            last_month = self.index
        elif self.kind == "trimestre":
            last_month = 3 * self.index
        else:
            last_month = 12
        if last_month == 12:
            return date(self.year, 12, 31)
        return date(self.year, last_month + 1, 1) - timedelta(days=1)

    @property
    def slug(self) -> str:
        if self.kind == "mois":
            return f"{self.year:04d}-{self.index:02d}"
        if self.kind == "trimestre":
            return f"{self.year:04d}-T{self.index}"
        return f"{self.year:04d}"


def _all_digits(text: str) -> bool:
    return bool(text) and all(ch in _DIGITS for ch in text)


def _shape(slug: str) -> Optional[Period]:
    """The period a well-formed slug names, else ``None`` — the grammar
    alone, no bound: ``YYYY``, ``YYYY-MM`` (01–12), ``YYYY-TQ`` (1–4)."""
    if not isinstance(slug, str):
        return None
    if len(slug) == 4 and _all_digits(slug):
        return Period("annee", int(slug), 1)
    if len(slug) != 7 or slug[4] != "-" or not _all_digits(slug[:4]):
        return None
    year = int(slug[:4])
    tail = slug[5:]
    if tail[0] == "T" and len(tail) == 2 and tail[1] in "1234":
        return Period("trimestre", year, int(tail[1]))
    if _all_digits(tail):
        month = int(tail)
        if 1 <= month <= 12:
            return Period("mois", year, month)
    return None


def parse_period(slug: str, *, today: date,
                 books_start: date) -> tuple[Optional[Period], str]:
    """``(period, "")`` for a slug the page may show, else ``(None, the
    French refusal)``: malformed, starting before the books, or starting
    after today."""
    period = _shape(slug)
    if period is None or period.year < 1:
        return None, ("La période demandée est invalide : choisissez un mois "
                      "(AAAA-MM), un trimestre (AAAA-T1 à AAAA-T4) ou une "
                      "année (AAAA).")
    if period.start < books_start:
        return None, (f"Les livres commencent le {format_date_fr(books_start)} : "
                      "aucune période ne les précède.")
    if period.start > today:
        return None, "Cette période n'est pas encore commencée."
    return period, ""


def label_fr(period: Period) -> str:
    """« octobre 2026 », « 1er trimestre 2026 », « année 2026 »."""
    if period.kind == "mois":
        return f"{month_name_fr(period.index)} {period.year}"
    if period.kind == "trimestre":
        rank = "1er" if period.index == 1 else f"{period.index}e"
        return f"{rank} trimestre {period.year}"
    return f"année {period.year}"


def span_fr(period: Period) -> str:
    """« du 1er octobre 2026 au 31 octobre 2026 »."""
    return f"du {format_date_fr(period.start)} au {format_date_fr(period.end)}"


def in_progress(period: Period, today: date) -> bool:
    """True when today falls inside the period (its last day included: the
    day is not over)."""
    return period.start <= today <= period.end


def as_of(period: Period, today: date) -> date:
    """The last day the statements cover: the period's end, or today while
    the period is in progress."""
    return min(period.end, today)


def previous(period: Period) -> Period:
    """The period of the same kind just before."""
    if period.kind == "mois":
        if period.index == 1:
            return Period("mois", period.year - 1, 12)
        return Period("mois", period.year, period.index - 1)
    if period.kind == "trimestre":
        if period.index == 1:
            return Period("trimestre", period.year - 1, 4)
        return Period("trimestre", period.year, period.index - 1)
    return Period("annee", period.year - 1, 1)


def following(period: Period) -> Period:
    """The period of the same kind just after."""
    if period.kind == "mois":
        if period.index == 12:
            return Period("mois", period.year + 1, 1)
        return Period("mois", period.year, period.index + 1)
    if period.kind == "trimestre":
        if period.index == 4:
            return Period("trimestre", period.year + 1, 1)
        return Period("trimestre", period.year, period.index + 1)
    return Period("annee", period.year + 1, 1)


def has_previous(period: Period, books_start: date) -> bool:
    return previous(period).start >= books_start


def has_next(period: Period, today: date) -> bool:
    return following(period).start <= today


def month_of(day: date) -> Period:
    return Period("mois", day.year, day.month)


def default_period(today: date, books_start: date) -> Period:
    """The last COMPLETE month — or, while the books' first month is still
    running, that month (in progress)."""
    last_complete = previous(month_of(today))
    if last_complete.start < books_start:
        return month_of(today)
    return last_complete


def options(today: date, books_start: date) -> list[tuple[str, list[tuple[str, str]]]]:
    """The selector's groups — ``[(group label, [(slug, label), …]), …]``
    in ``KINDS`` order — every period from the books' start to the one that
    holds today, newest first."""
    first = {"mois": Period("mois", books_start.year, books_start.month),
             "trimestre": Period("trimestre", books_start.year,
                                 (books_start.month - 1) // 3 + 1),
             "annee": Period("annee", books_start.year, 1)}
    groups = []
    for kind in KINDS:
        rows = []
        period = first[kind]
        # A quarter or a year that starts before a mid-period books start is
        # refused by parse_period: never offered either.
        while period.start < books_start:
            period = following(period)
        while period.start <= today:
            rows.append((period.slug, label_fr(period)))
            period = following(period)
        groups.append((GROUP_LABELS[kind], list(reversed(rows))))
    return groups
