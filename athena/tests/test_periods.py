"""Tests for utils/periods.py — the financial statements' periods (lot B).

The grammar is closed (``YYYY``, ``YYYY-MM``, ``YYYY-TQ``) and refused in
French when malformed, before the books or after today; the arithmetic is
calendar months, quarters and years. Every year here is fictitious: the
books' start and « today » are the test's own, never the lawyer's.
"""

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import periods as p  # noqa: E402
from utils.format_fr import month_name_fr  # noqa: E402

BOOKS = date(2031, 1, 1)
TODAY = date(2033, 5, 14)


def _ok(slug: str, today: date = TODAY, books: date = BOOKS) -> p.Period:
    period, error = p.parse_period(slug, today=today, books_start=books)
    assert error == "" and period is not None, (slug, error)
    return period


def _refused(slug, today: date = TODAY, books: date = BOOKS) -> str:
    period, error = p.parse_period(slug, today=today, books_start=books)
    assert period is None and error, slug
    return error


# ── The grammar ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("slug, kind, start, end", [
    ("2032", "annee", date(2032, 1, 1), date(2032, 12, 31)),
    ("2032-01", "mois", date(2032, 1, 1), date(2032, 1, 31)),
    ("2032-02", "mois", date(2032, 2, 1), date(2032, 2, 29)),   # leap year
    ("2031-02", "mois", date(2031, 2, 1), date(2031, 2, 28)),
    ("2032-12", "mois", date(2032, 12, 1), date(2032, 12, 31)),
    ("2032-T1", "trimestre", date(2032, 1, 1), date(2032, 3, 31)),
    ("2032-T2", "trimestre", date(2032, 4, 1), date(2032, 6, 30)),
    ("2032-T3", "trimestre", date(2032, 7, 1), date(2032, 9, 30)),
    ("2032-T4", "trimestre", date(2032, 10, 1), date(2032, 12, 31)),
])
def test_each_slug_names_its_calendar_span(slug, kind, start, end):
    period = _ok(slug)
    assert (period.kind, period.start, period.end) == (kind, start, end)
    assert period.slug == slug


@pytest.mark.parametrize("slug", [
    "", "2032-1", "2032-001", "2032-13", "2032-00", "2032-T0", "2032-T5",
    "2032-t3", "2032-Q3", "2032/10", "32-10", "２０３２", "2032-١٠",
    " 2032", "2032 ", "2032-10-01", "abcd", "-2032", "2032-T", "2032-T33", "0000",
    None, 2032,
])
def test_a_malformed_slug_is_refused_in_french(slug):
    assert _refused(slug).startswith("La période demandée est invalide")


def test_a_period_before_the_books_is_refused():
    assert _refused("2030-12") == (
        "Les livres commencent le 1er janvier 2031 : aucune période ne les "
        "précède.")
    assert _refused("2030").startswith("Les livres commencent")


def test_a_period_straddling_a_mid_year_books_start_is_refused():
    books = date(2031, 3, 1)
    assert _refused("2031", books=books).startswith("Les livres commencent")
    assert _refused("2031-T1", books=books).startswith("Les livres commencent")
    assert _ok("2031-03", books=books).start == books
    assert _ok("2031-T2", books=books).start == date(2031, 4, 1)


def test_a_period_that_has_not_started_is_refused():
    assert _refused("2033-06") == "Cette période n'est pas encore commencée."
    assert _refused("2033-T3").startswith("Cette période")
    assert _refused("2034").startswith("Cette période")


def test_the_period_holding_today_is_accepted_and_in_progress():
    for slug in ("2033-05", "2033-T2", "2033"):
        period = _ok(slug)
        assert p.in_progress(period, TODAY)
        assert p.as_of(period, TODAY) == TODAY
    closed = _ok("2033-04")
    assert not p.in_progress(closed, TODAY)
    assert p.as_of(closed, TODAY) == date(2033, 4, 30)


def test_its_last_day_is_still_in_progress():
    period = _ok("2033-05", today=date(2033, 5, 31))
    assert p.in_progress(period, date(2033, 5, 31))
    assert not p.in_progress(period, date(2033, 6, 1))


# ── Labels ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("slug, label", [
    ("2032-10", "octobre 2032"),
    ("2032-08", "août 2032"),
    ("2032-T1", "1er trimestre 2032"),
    ("2032-T2", "2e trimestre 2032"),
    ("2032-T4", "4e trimestre 2032"),
    ("2032", "année 2032"),
])
def test_labels_are_french(slug, label):
    assert p.label_fr(_ok(slug)) == label


def test_the_span_reads_as_a_sentence():
    assert p.span_fr(_ok("2032-T1")) == "du 1er janvier 2032 au 31 mars 2032"


def test_month_names_cover_the_year_and_refuse_the_rest():
    assert [month_name_fr(m) for m in (1, 2, 8, 12)] == [
        "janvier", "février", "août", "décembre"]
    for bad in (0, 13, "3", True, 2.0):
        with pytest.raises(ValueError):
            month_name_fr(bad)


# ── Navigation ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("slug, before, after", [
    ("2032-01", "2031-12", "2032-02"),
    ("2032-12", "2032-11", "2033-01"),
    ("2032-T1", "2031-T4", "2032-T2"),
    ("2032-T4", "2032-T3", "2033-T1"),
    ("2032", "2031", "2033"),
])
def test_previous_and_following_stay_in_kind(slug, before, after):
    period = _ok(slug)
    assert p.previous(period).slug == before
    assert p.following(period).slug == after


def test_navigation_stops_at_the_books_and_at_today():
    assert not p.has_previous(_ok("2031-01"), BOOKS)
    assert p.has_previous(_ok("2031-02"), BOOKS)
    assert not p.has_next(_ok("2033-05"), TODAY)
    assert p.has_next(_ok("2033-04"), TODAY)
    assert not p.has_next(_ok("2033"), TODAY)


def test_the_default_is_the_last_complete_month():
    assert p.default_period(TODAY, BOOKS).slug == "2033-04"
    assert p.default_period(date(2033, 1, 3), BOOKS).slug == "2032-12"
    # The books' first month, still running: that month.
    assert p.default_period(date(2031, 1, 20), BOOKS).slug == "2031-01"


# ── The selector ────────────────────────────────────────────────────────


def test_options_run_from_the_books_to_today_newest_first():
    groups = p.options(TODAY, BOOKS)
    assert [label for label, _rows in groups] == ["Mois", "Trimestres", "Années"]
    months, quarters, years = (dict(rows) for _label, rows in groups)
    assert list(months)[0] == "2033-05" and list(months)[-1] == "2031-01"
    assert len(months) == 12 + 12 + 5
    assert list(quarters) == ["2033-T2", "2033-T1", "2032-T4", "2032-T3",
                              "2032-T2", "2032-T1", "2031-T4", "2031-T3",
                              "2031-T2", "2031-T1"]
    assert list(years) == ["2033", "2032", "2031"]
    assert years["2033"] == "année 2033"


def test_every_option_parses_back_to_itself():
    for _label, rows in p.options(TODAY, BOOKS):
        for slug, label in rows:
            period = _ok(slug)
            assert period.slug == slug and p.label_fr(period) == label


def test_options_never_offer_a_period_straddling_the_books_start():
    books = date(2031, 3, 1)
    groups = dict(p.options(date(2031, 8, 9), books))
    assert [s for s, _l in groups["Trimestres"]] == ["2031-T3", "2031-T2"]
    assert groups["Années"] == []
    for _label, rows in p.options(date(2031, 8, 9), books):
        for slug, _l in rows:
            _ok(slug, today=date(2031, 8, 9), books=books)
