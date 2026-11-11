"""The journals, newest first and paginated (2026-11-10, the lawyer's
request): the administration journal (every account), the trust journal's
FILTERED path (its unfiltered path already paged newest first) and the
carte-client.

Each view reads its period as before, orders it newest first and slices one
page with ``pagination.paginate`` — the « page » mode of the shared pager
(``components/pagination.html``), 15 rows (``pagination.PAGE_SIZE``). So
every figure stays exact: the administration « Solde » is computed over the
whole period BEFORE slicing, the trust « Solde » is stored on each row. The
exports keep their own read — oldest first, the whole period — whatever page
the screen shows.

The bench is the shared fake Firestore (``tests/_fake_firestore.py``) under
the real routes, models and templates. Every amount, name and number here is
synthetic (a fictional year)."""

import csv
import io
import os
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import admin_ledger as al
    from models import trust
    import routes.admin_ledger as ra
    import routes.dossiers as dossiers_routes
    import routes.invoices as invoices_routes
    import routes.trust as rt

from flask import Flask  # noqa: E402

from pagination import PAGE_SIZE  # noqa: E402
from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.html_attr import jsattr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc
HX = {"HX-Request": "true"}
N_ADMIN = 40          # three pages: 15 + 15 + 10
N_TRUST = 40
N_CARD = 20           # two pages: 15 + 5
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}( \*)?")


def _d(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


def _pager(html: str) -> Optional[str]:
    """The shared pager bar alone — never the filter bar, whose controls
    carry the same hx-get and hx-target."""
    match = re.search(r'<nav[^>]*aria-label="Pagination".*?</nav>', html, re.S)
    return match.group(0) if match else None


def _soldes(html: str) -> list[str]:
    """The « Solde » cells of the rows, in order (the header cards use
    font-semibold, never this cell's class)."""
    return re.findall(
        r'text-right whitespace-nowrap font-medium text-gray-900">([^<]*)</td>', html)


def _csv_days(text: str) -> list[str]:
    """The date column of an export's entry rows, its « * » marker (an
    uncleared entry) removed."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))[1:]
    return [r[0].removesuffix(" *") for r in rows if r and _DAY.fullmatch(r[0] or "")]


# ── The administration ledger ──────────────────────────────────────────────


def _admin_entry(i: int, account_id: str = "ops1") -> dict:
    """Entry *i* (1-based): two a day from 2031-01-01, recettes on odd
    sequences, dépenses on even ones — distinct amounts, so every running
    balance is distinct too."""
    recette = i % 2 == 1
    return {
        "id": f"adm-{account_id}-{i:03d}", "account_id": account_id,
        "sequence": i, "date": _d(2031, 1, 1) + timedelta(days=(i - 1) // 2),
        "direction": "recette" if recette else "déboursé",
        "kind": "autre_recette" if recette else "dépense",
        "category": "" if recette else "fournitures",
        "amount": 10_000 + 37 * i if recette else 4_000 + 11 * i,
        "status": "compensée" if i % 3 else "en_circulation",
        "counterparty": f"Contrepartie fictive {i}", "method": "virement",
        "description": "", "reference": "", "supplier_invoice_ref": "",
        "invoice_id": None, "invoice_number": "", "reversed_by_id": None,
        "reverses_id": None, "net_amount": None, "gst_amount": None,
        "qst_amount": None, "dossier_id": "", "revisions": [],
        "created_at": _d(2031, 1, 1), "updated_at": _d(2031, 1, 1), "etag": "e",
    }


def _admin_ids(html: str, account_id: str = "ops1") -> list[str]:
    return re.findall(rf'href="/administration/(adm-{account_id}-\d+)"', html)


def _ledger(fake, n: int = N_ADMIN) -> list[dict]:
    return [fake.peek(f"admin_transactions/adm-ops1-{i:03d}") for i in range(1, n + 1)]


def _expected_running(entries: list[dict], opening: int = 0) -> dict[str, int]:
    """The ledger's own computation over the WHOLE period, in its order
    (date, sequence)."""
    ordered = sorted(entries, key=lambda e: (e["date"], e["sequence"]))
    return {e["id"]: b for e, b in zip(
        ordered, al.running_balances(ordered, opening=opening))}


def _walk_admin(client, url: str, pages: int) -> list[str]:
    seen = []
    for page in range(1, pages + 1):
        seen += _admin_ids(_get(client, f"{url}&page={page}"))
    return seen


@pytest.fixture
def clock(monkeypatch):
    from utils import deadlines as dl

    frozen = datetime(2031, 9, 20, 16, 0, tzinfo=UTC)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(dl, "datetime", _Clock)
    return frozen


@pytest.fixture
def fake(monkeypatch, clock):
    f = install(monkeypatch, *_fake_modules())
    f.seed("admin_accounts/ops1", {
        "id": "ops1", "name": "Opérations", "status": "actif",
        "account_type": "opérations", "ledger_balance": 0, "etag": "e0",
    })
    f.seed("admin_accounts/card1", {
        "id": "card1", "name": "Carte", "status": "actif",
        "account_type": "carte_crédit", "ledger_balance": 0, "etag": "e0",
    })
    for i in range(1, N_ADMIN + 1):
        f.seed(f"admin_transactions/adm-ops1-{i:03d}", _admin_entry(i))
    for i in range(1, 4):
        f.seed(f"admin_transactions/adm-card1-{i:03d}", _admin_entry(i, "card1"))
    f.seed("trust_accounts/tg1", {
        "id": "tg1", "name": "Fidéicommis général", "status": "actif",
        "account_type": "général", "book_balance": 0, "bank_balance": 0,
        "etag": "e0",
    })
    f.seed("dossiers/dos1", {
        "id": "dos1", "file_number": "2031-001", "title": "Dossier fictif",
        "status": "actif", "clients": [{"id": "c1", "name": "Client fictif"},
                                       {"id": "c2", "name": "Autre client"}],
        "client_ids": ["c1", "c2"],
        "trust_balance_by_client": {"c1": 0}, "trust_cleared_by_client": {"c1": 0},
    })
    return f


@pytest.fixture
def client(fake):
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=jsattr,
    )
    for bp in (rt.trust_bp, invoices_routes.invoices_bp, ra.admin_bp,
               dossiers_routes.dossiers_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _get(client, url: str, **headers) -> str:
    resp = client.get(url, headers=headers)
    assert resp.status_code == 200, resp.status_code
    return resp.get_data(as_text=True)


def test_the_admin_journal_shows_the_newest_entries_first(client):
    html = _get(client, "/administration/?account_id=ops1")
    newest_first = [f"adm-ops1-{i:03d}" for i in range(N_ADMIN, 0, -1)]
    assert _admin_ids(html) == newest_first[:PAGE_SIZE]
    pager = _pager(html)
    assert pager is not None
    assert 'hx-target="#admin-rows"' in pager
    assert 'hx-get="/administration/"' in pager
    assert "Page 1 / 3" in pager


def test_each_page_follows_the_last_and_the_last_holds_the_oldest(client):
    seen = _walk_admin(client, "/administration/?account_id=ops1", 3)
    assert seen == [f"adm-ops1-{i:03d}" for i in range(N_ADMIN, 0, -1)]
    # A page beyond the end clamps to the last one, never an empty table.
    last = _get(client, "/administration/?account_id=ops1&page=999")
    assert _admin_ids(last) == [f"adm-ops1-{i:03d}" for i in range(10, 0, -1)]
    assert "Page 3 / 3" in _pager(last)
    # Another account's entries never appear.
    assert not _admin_ids(last, "card1")


def test_the_admin_journal_orders_by_date_then_sequence(client, fake):
    """An entry recorded later but dated earlier — sequence 41, dated
    2031-01-03 — takes its DATE's place, not its sequence's: the ledger's
    order (date, sequence), the order its running balance is computed in."""
    fake.seed("admin_transactions/adm-ops1-041",
              {**_admin_entry(41), "date": _d(2031, 1, 3)})
    entries = _ledger(fake, 41)
    expected = [e["id"] for e in sorted(
        entries, key=lambda e: (e["date"], e["sequence"]), reverse=True)]
    seen = _walk_admin(client, "/administration/?account_id=ops1", 3)
    assert seen == expected
    assert seen[0] == "adm-ops1-040"                  # not the highest sequence
    at = seen.index("adm-ops1-041")
    assert seen[at - 1:at + 2] == ["adm-ops1-007", "adm-ops1-041", "adm-ops1-006"]
    running = _expected_running(entries)
    for page in (1, 2, 3):
        html = _get(client, f"/administration/?account_id=ops1&page={page}")
        assert _soldes(html) == [format_cents_fr(running[i]) for i in _admin_ids(html)]


def test_every_row_keeps_the_balance_of_the_whole_period(client, fake):
    """« Solde » is computed over the whole period, oldest first, BEFORE the
    page is cut: a row shows the same balance on whatever page it falls."""
    expected = _expected_running(_ledger(fake))
    for page in (1, 2, 3):
        html = _get(client, f"/administration/?account_id=ops1&page={page}")
        ids = _admin_ids(html)
        assert ids and _soldes(html) == [format_cents_fr(expected[i]) for i in ids]
    # The top row of page 1 is the period's closing balance.
    first = _soldes(_get(client, "/administration/?account_id=ops1"))[0]
    assert first == format_cents_fr(expected[f"adm-ops1-{N_ADMIN:03d}"])


def test_the_carried_balance_closes_the_last_page_only(client, fake):
    """With a period, the balance carried into it sits UNDER the oldest
    entry, on the last page alone."""
    entries = _ledger(fake)
    # From 2031-01-05: entries 9.. are in the period, 1-8 carried.
    carried = sum(al.admin_delta(e["direction"], e["amount"]) for e in entries[:8])
    url = "/administration/?account_id=ops1&date_from=2031-01-05"
    in_period = [e for e in entries if e["date"] >= _d(2031, 1, 5)]
    expected = _expected_running(in_period, opening=carried)
    label = "Solde reporté au 2031-01-05"
    assert label not in _get(client, url)
    pages = -(-len(in_period) // PAGE_SIZE)
    last = _get(client, f"{url}&page={pages}")
    assert last.index('href="/administration/adm-ops1-009"') < last.index(label)
    ids = _admin_ids(last)
    soldes = _soldes(last)
    assert soldes[:len(ids)] == [format_cents_fr(expected[i]) for i in ids]
    assert format_cents_fr(carried) in last[last.index(label):]


def test_the_carried_balance_shows_when_the_period_fits_on_one_page(client, fake):
    entries = _ledger(fake)
    # From 2031-01-17: entries 33-40 (eight) in the period, 1-32 carried.
    carried = sum(al.admin_delta(e["direction"], e["amount"]) for e in entries[:32])
    html = _get(client, "/administration/?account_id=ops1&date_from=2031-01-17")
    assert _admin_ids(html) == [f"adm-ops1-{i:03d}" for i in range(N_ADMIN, 32, -1)]
    assert _pager(html) is None                         # one page: no bar
    label = "Solde reporté au 2031-01-17"
    assert html.index('href="/administration/adm-ops1-033"') < html.index(label)
    assert format_cents_fr(carried) in html[html.index(label):]


def test_a_full_page_and_its_htmx_twin_show_the_same_rows(client):
    full = _get(client, "/administration/?account_id=ops1&page=2")
    fragment = _get(client, "/administration/?account_id=ops1&page=2", **HX)
    assert "<html" in full and "<html" not in fragment
    assert _admin_ids(full) == _admin_ids(fragment) != []
    assert "Page 2 / 3" in _pager(fragment)
    # The fragment still re-sends the export links and the header, out of band.
    assert 'id="admin-export" class="ml-auto flex gap-2" hx-swap-oob="true"' in fragment
    assert 'id="admin-header" hx-swap-oob="true"' in fragment


def test_a_content_filter_pages_too_and_still_hides_the_balance(client):
    html = _get(client, "/administration/?account_id=ops1&kind=dépense")
    depenses = [f"adm-ops1-{i:03d}" for i in range(N_ADMIN, 0, -2)]
    assert _admin_ids(html) == depenses[:PAGE_SIZE]
    assert ">Solde</th>" not in html
    assert "La colonne « Solde » n'est affichée que sans filtre" in html
    # The pager carries the filter bar (hx-include), so page 2 stays filtered.
    assert 'hx-include="#filters input, #filters select"' in _pager(html)
    page2 = _admin_ids(_get(client, "/administration/?account_id=ops1&kind=dépense&page=2"))
    assert page2 == depenses[PAGE_SIZE:]


def test_a_truncated_register_says_its_newest_entries_are_missing(client, monkeypatch):
    real = al.list_register
    monkeypatch.setattr(ra.al, "list_register",
                        lambda aid, df=None, dt=None, limit=10000:
                        (real(aid, df, dt, limit=5)[0], True))
    html = _get(client, "/administration/?account_id=ops1")
    assert ("Le registre a été tronqué : les écritures les plus récentes de la "
            "période n'y figurent pas") in html
    assert ">Solde</th>" not in html


def test_the_admin_exports_ignore_the_page(client, monkeypatch):
    """The CSV and the PDF read the whole period, oldest first, whatever page
    the screen is on — and the export links never carry one."""
    url = "/administration/export/csv?account_id=ops1&date_from=2031-01-01"
    resp = client.get(url)
    assert resp.status_code == 200
    whole = resp.get_data(as_text=True)
    assert client.get(url + "&page=3").get_data(as_text=True) == whole
    days = _csv_days(whole)
    assert len(days) == N_ADMIN and days == sorted(days)

    captured = {}

    def _capture(rows, **kw):
        captured["rows"] = rows
        return "pdf"

    from utils import admin_journal_pdf
    monkeypatch.setattr(admin_journal_pdf, "build_admin_journal_pdf", _capture)
    client.get("/administration/export/pdf?account_id=ops1&page=2")
    pdf_days = [r["date"] for r in captured["rows"]]
    assert len(pdf_days) == N_ADMIN and pdf_days == sorted(pdf_days)

    page = _get(client, "/administration/?account_id=ops1&page=2")
    links = re.findall(r'href="(/administration/export/[^"]+)"', page)
    assert links and not any("page=" in link for link in links)


# ── The trust journal, filtered ────────────────────────────────────────────


def _trust_entry(i: int, client_id: str = "c1") -> dict:
    """Entry *i* of account tg1: one a day from 2031-02-01, recettes on odd
    sequences; the balances stored as the model would have frozen them."""
    recette = i % 2 == 1
    return {
        "id": f"tt-{i:03d}", "account_id": "tg1", "sequence": i,
        "date": _d(2031, 2, 1) + timedelta(days=i - 1),
        "direction": "recette" if recette else "déboursé",
        "amount": 20_000 + 13 * i if recette else 5_000 + 7 * i,
        "status": "compensée", "purpose": "provision" if recette else "debours",
        "method": "virement", "counterparty": f"Contrepartie {i}",
        "dossier_id": "dos1", "dossier_file_number": "2031-001",
        "client_id": client_id, "client_name": "Client fictif",
        "balance_after_account": 100_000 + 1_000 * i,
        "balance_after_client": 50_000 + 500 * i,
        "created_at": _d(2031, 2, 1) + timedelta(days=i - 1),
    }


@pytest.fixture
def trust_rows(fake):
    for i in range(1, N_TRUST + 1):
        fake.seed(f"trust_transactions/tt-{i:03d}", _trust_entry(i))
    return fake


def _trust_ids(html: str) -> list[str]:
    return re.findall(r'href="/fideicommis/(tt-\d+)"', html)


def test_a_filtered_trust_journal_pages_newest_first_and_whole(client, trust_rows,
                                                               monkeypatch):
    """Every matching entry is reachable, newest first — the old path read
    the account's oldest 600 entries and kept 200 — and it no longer reads
    through that bounded window at all."""
    monkeypatch.setattr(rt.trust, "list_transactions",
                        lambda *a, **k: pytest.fail("the bounded window is gone"))
    recettes = [f"tt-{i:03d}" for i in range(N_TRUST - 1, 0, -2)]
    url = "/fideicommis/?account_id=tg1&direction=recette"
    first = _get(client, url)
    assert _trust_ids(first) == recettes[:PAGE_SIZE]
    pager = _pager(first)
    assert pager is not None and "Page 1 / 2" in pager
    assert 'hx-target="#trust-rows"' in pager
    assert 'hx-get="/fideicommis/"' in pager
    assert '"account_id": "tg1"' in pager
    second = _get(client, url + "&page=2", **HX)
    assert _trust_ids(second) == recettes[PAGE_SIZE:]
    # The fragment re-sends the export links and the header, out of band.
    assert 'id="trust-export"' in second and 'id="trust-header"' in second


def test_a_filter_keeps_the_unfiltered_order_of_the_journal(client, trust_rows):
    """A date edited outside the model — sequence 41 dated before 40: the
    filtered journal keeps the unfiltered one's order, newest sequence
    first (the order its stored balances chain in), never the art. 38
    sheet's (day, sequence)."""
    trust_rows.seed("trust_transactions/tt-041",
                    {**_trust_entry(41), "date": _d(2031, 2, 10)})
    unfiltered = _trust_ids(_get(client, "/fideicommis/?account_id=tg1"))
    filtered = _trust_ids(_get(client, "/fideicommis/?account_id=tg1&direction=recette"))
    assert unfiltered[0] == filtered[0] == "tt-041"
    recettes_on_page = [i for i in unfiltered if int(i[3:]) % 2 == 1]
    assert filtered[:len(recettes_on_page)] == recettes_on_page


def test_a_filtered_period_keeps_whole_days_at_both_ends(client, trust_rows):
    html = _get(client, "/fideicommis/?account_id=tg1&date_from=2031-02-03"
                        "&date_to=2031-02-05")
    assert _trust_ids(html) == ["tt-005", "tt-004", "tt-003"]


def test_a_filter_that_matches_nothing_says_so(client, trust_rows):
    html = _get(client, "/fideicommis/?account_id=tg1&status=annulée")
    assert "Aucune écriture pour ce compte avec ces filtres." in html


def test_a_truncated_filtered_register_says_its_newest_are_missing(client, trust_rows,
                                                                   monkeypatch):
    real = trust.list_register
    monkeypatch.setattr(rt.trust, "list_register",
                        lambda aid, df=None, dt=None, limit=10000:
                        (real(aid, df, dt, limit=5)[0], True))
    html = _get(client, "/fideicommis/?account_id=tg1&direction=recette")
    assert ("Le registre a été tronqué : les écritures les plus récentes de la "
            "période n'y figurent pas") in html


def test_the_unfiltered_trust_journal_keeps_its_cursor_pager(client, trust_rows):
    html = _get(client, "/fideicommis/?account_id=tg1")
    assert _trust_ids(html) == [f"tt-{i:03d}" for i in range(N_TRUST, 0, -1)][:PAGE_SIZE]
    assert '"cursor": "' in _pager(html)        # the cursor mode, unchanged


def test_the_trust_exports_ignore_the_page(client, trust_rows, monkeypatch):
    url = "/fideicommis/export/csv?account_id=tg1&direction=recette"
    resp = client.get(url)
    assert resp.status_code == 200
    whole = resp.get_data(as_text=True)
    assert client.get(url + "&page=2").get_data(as_text=True) == whole
    days = _csv_days(whole)
    assert len(days) == N_TRUST // 2 and days == sorted(days)
    # The export's own day filter (list_transactions, refactored onto
    # _day_bounds / _within_days) keeps both ends whole.
    period = client.get(url + "&date_from=2031-02-03&date_to=2031-02-05")
    assert period.status_code == 200
    assert _csv_days(period.get_data(as_text=True)) == ["2031-02-03", "2031-02-05"]

    captured = {}

    def _capture(rows, **kw):
        captured["rows"] = rows
        return "pdf"

    from utils import trust_journal_pdf
    monkeypatch.setattr(trust_journal_pdf, "build_trust_journal_pdf", _capture)
    client.get("/fideicommis/export/pdf?account_id=tg1&date_from=2031-02-01&page=2")
    soldes = [r["solde"] for r in captured["rows"]]
    assert len(soldes) == N_TRUST and soldes == sorted(soldes)   # oldest first


# ── The carte-client ───────────────────────────────────────────────────────


@pytest.fixture
def card_rows(fake):
    for i in range(1, N_CARD + 1):
        fake.seed(f"trust_transactions/tt-{i:03d}", _trust_entry(i))
    # Another client of the same dossier never appears on c1's card.
    fake.seed("trust_transactions/tt-900", {**_trust_entry(900, "c2"),
                                            "id": "tt-900", "sequence": 900})
    return fake


def _card_balance(tx_id: str) -> str:
    return format_cents_fr(50_000 + 500 * int(tx_id[3:]))


def test_the_card_shows_its_newest_entries_first_one_page_at_a_time(client, card_rows):
    url = "/fideicommis/carte/dos1/c1"
    full = _get(client, url)
    newest_first = [f"tt-{i:03d}" for i in range(N_CARD, 0, -1)]
    assert _trust_ids(full) == newest_first[:PAGE_SIZE]
    assert 'id="card-rows"' in full
    pager = _pager(full)
    assert pager is not None and "Page 1 / 2" in pager
    assert 'hx-target="#card-rows"' in pager
    assert 'hx-get="/fideicommis/carte/dos1/c1"' in pager
    fragment = _get(client, url + "?page=2", **HX)
    assert "<html" not in fragment
    assert _trust_ids(fragment) == newest_first[PAGE_SIZE:]
    assert "tt-900" not in full + fragment
    # Each « Solde » is the client's balance after that entry, as stored.
    for html in (full, fragment):
        assert _soldes(html) == [_card_balance(i) for i in _trust_ids(html)]


def test_the_card_pages_its_period(client, card_rows):
    url = "/fideicommis/carte/dos1/c1?date_from=2031-02-03&date_to=2031-02-19"
    first = _get(client, url)
    assert _trust_ids(first) == [f"tt-{i:03d}" for i in range(19, 4, -1)]
    assert "Page 1 / 2" in _pager(first)
    second = _get(client, url + "&page=2", **HX)
    assert _trust_ids(second) == ["tt-004", "tt-003"]
    assert _soldes(second) == [_card_balance("tt-004"), _card_balance("tt-003")]


def test_the_card_carries_its_parsed_period_never_the_raw_one(client, card_rows):
    """The pager's controls include "#filters input, #filters select": the
    card's period rides in hidden inputs there — the parsed dates, always
    both inputs (htmx logs an error when an include matches nothing)."""
    html = _get(client, "/fideicommis/carte/dos1/c1?date_from=2031-02-01")
    assert '<div id="filters" hidden>' in html
    assert '<input type="hidden" name="date_from" value="2031-02-01">' in html
    assert '<input type="hidden" name="date_to" value="">' in html
    hostile = _get(client, "/fideicommis/carte/dos1/c1"
                           "?date_from=%22%3E%3Cscript%3Ex%3C/script%3E")
    assert "<script>x" not in hostile
    assert '<input type="hidden" name="date_from" value="">' in hostile


def test_the_card_export_is_unchanged_by_the_page(client, card_rows):
    url = "/fideicommis/carte/dos1/c1/export/csv"
    resp = client.get(url)
    assert resp.status_code == 200
    whole = resp.get_data(as_text=True)
    assert client.get(url + "?page=2").get_data(as_text=True) == whole
    days = _csv_days(whole)
    assert len(days) == N_CARD and days == sorted(days)            # oldest first


def test_the_card_register_reports_its_truncation(card_rows):
    rows, truncated = trust.list_card_register("dos1", "c1", limit=5)
    assert truncated is True and len(rows) == 5
    assert [r["sequence"] for r in rows] == [1, 2, 3, 4, 5]
    whole, cut = trust.list_card_register("dos1", "c1")
    assert cut is False and len(whole) == N_CARD
    # The same whole-day bounds as list_card_transactions, which the exports keep.
    bounded, _ = trust.list_card_register("dos1", "c1", _d(2031, 2, 3), _d(2031, 2, 5))
    assert [r["id"] for r in bounded] == ["tt-003", "tt-004", "tt-005"]
    assert bounded == trust.list_card_transactions("dos1", "c1", _d(2031, 2, 3),
                                                   _d(2031, 2, 5))


def test_a_truncated_card_says_its_newest_entries_are_missing(client, card_rows,
                                                             monkeypatch):
    real = trust.list_card_register
    monkeypatch.setattr(rt.trust, "list_card_register",
                        lambda d, c, df=None, dt=None, limit=10000:
                        real(d, c, df, dt, limit=5))
    html = _get(client, "/fideicommis/carte/dos1/c1")
    assert "La carte a été tronquée : les écritures les plus récentes n'y figurent pas." in html


# ── A failed read is never an empty list ───────────────────────────────────


def test_the_card_register_fails_closed(card_rows, monkeypatch):
    class _Down:
        def collection(self, *a, **k):
            raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(trust, "db", _Down())
    with pytest.raises(RuntimeError):
        trust.list_card_register("dos1", "c1")


@pytest.mark.parametrize("url, module, reader", [
    ("/administration/?account_id=ops1", ra.al, "list_register"),
    ("/fideicommis/?account_id=tg1&direction=recette", rt.trust, "list_register"),
    ("/fideicommis/carte/dos1/c1", rt.trust, "list_card_register"),
])
def test_a_failed_read_is_an_error_never_an_empty_list(client, fake, monkeypatch,
                                                       url, module, reader):
    def _down(*a, **k):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(module, reader, _down)
    resp = client.get(url)
    assert resp.status_code == 500
    assert "Aucune écriture" not in resp.get_data(as_text=True)
