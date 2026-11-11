"""The dashboard's « Factures impayées » tile (plan, lot 0).

The tile summed the live balances of the « envoyée » AND « en_retard »
invoices but linked to the « envoyée » list alone: the late invoices it
counted sat one filter away, behind a link that hid them. It now shows the
total and one link per issued status, each to that status' own filter
(``/factures/?status=…``) with that status' own balance — one call,
``invoice.get_outstanding_totals`` (one strict read of the invoices, summed
per issued status), whose sum ``get_outstanding_total`` is the connector's
``get_agenda`` figure: the tile, its links and ``get_agenda`` cannot
disagree. The connector's other firm-wide « impayé », that of
``get_billing_snapshot``, sums its own rows on the same pair; a test below
pins the two equal. One link over both statuses was never an option: an
``in`` filter with an ``order_by`` and a cursor fails on Firestore
(``mcp/handlers.list_invoices``), and the list would swallow the failure
into an empty page.

Through the real route, model and templates, on the shared fake Firestore
(``tests/_fake_firestore.py``), with invented invoices.
"""

import html
import os
import pathlib
import re
import sys
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest
from google.api_core import exceptions as gexc

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    # Every model the dashboard reads, imported before the fake is installed
    # so that none of them keeps the mocked client.
    from models import dossier, hearing, protocol, task, time_entry, trust  # noqa: F401
    from models import invoice as invoice_model
    import mcp.handlers as handlers  # get_billing_snapshot, the other « impayé »
    import routes.dashboard as dashboard_routes
    import routes.invoices as invoices_routes
    import routes.trust as trust_routes

from flask import Flask  # noqa: E402

from tests import test_compiled_classes as compiled  # noqa: E402
from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc

# Invented invoices of a fictitious year: two sent (one partly paid), one
# late, and one each of the statuses the tile must never count.
_INVOICES = {
    "inv-a": ("2031-F001", "envoyée", 114975, 0),
    "inv-b": ("2031-F002", "envoyée", 80000, 30000),
    "inv-c": ("2031-F003", "en_retard", 57490, 0),
    "inv-d": ("2031-F004", "payée", 20000, 20000),
    "inv-e": ("2031-F005", "brouillon", 10000, 0),
    "inv-f": ("2031-F006", "annulée", 15000, 0),
}
SENT = 114975 + (80000 - 30000)
LATE = 57490


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    f = install(monkeypatch, *_fake_modules())
    for day, (iid, (number, status, due, paid)) in enumerate(sorted(_INVOICES.items()), 1):
        f.seed(f"invoices/{iid}", {
            "id": iid, "invoice_number": number, "status": status,
            "dossier_id": "dos1", "dossier_file_number": "2031-004",
            "dossier_title": "Exemple c. Témoin", "client_id": "c1",
            "client_name": "Cliente Exemple",
            "date": datetime(2031, 3, day, tzinfo=UTC),
            "due_date": datetime(2031, 4, day, tzinfo=UTC),
            "total": due, "retainer_applied": 0,
            "amount_due": due, "amount_paid": paid, "etag": f"{iid}-e0",
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
        jsattr=lambda v: v,
    )
    for bp in (dashboard_routes.dashboard_bp, invoices_routes.invoices_bp,
               trust_routes.trust_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _tile(client) -> str:
    """The tile's markup: from its own <div> to the next tile's <a>."""
    response = client.get("/")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    start = page.rindex("<div", 0, page.index("Factures impayées"))
    end = page.rindex("<a", start, page.index("Sommes en fidéicommis"))
    return page[start:end]


def _links(tile: str) -> list[tuple[str, str]]:
    """``(href, text)`` of each link of the tile, the text's spaces folded."""
    return [(html.unescape(href), " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split()))
            for href, text in re.findall(r'<a href="([^"]*)"[^>]*>(.*?)</a>', tile, re.S)]


def _money(cents: int) -> str:
    return " ".join(format_cents_fr(cents).split())


def test_the_tile_shows_the_total_and_one_link_per_issued_status(client):
    """Fails on the old tile: ONE link, to « envoyée » alone."""
    tile = _tile(client)
    links = _links(tile)
    assert [(urlsplit(href).path, parse_qs(urlsplit(href).query)) for href, _ in links] == [
        ("/factures/", {"status": ["envoyée"]}),
        ("/factures/", {"status": ["en_retard"]}),
    ]
    assert [text for _, text in links] == [f"Envoyées {_money(SENT)}",
                                           f"En retard {_money(LATE)}"]
    # The total: both statuses — never the paid, the draft or the voided.
    total = tile.index("Factures impayées")
    assert _money(SENT + LATE) in " ".join(tile[total:tile.index("<a ")].split())


def test_the_tile_and_get_agenda_read_the_same_sums(client):
    """One authority: the tile's figures are get_outstanding_totals', and its
    total is get_outstanding_total — the connector's get_agenda figure."""
    totals = invoice_model.get_outstanding_totals()
    assert totals == {"envoyée": SENT, "en_retard": LATE}
    assert invoice_model.get_outstanding_total() == SENT + LATE
    tile = _tile(client)
    assert _money(invoice_model.get_outstanding_total()) in " ".join(tile.split())
    links = _links(tile)
    assert len(links) == 2
    for (_href, text), status in zip(links, ("envoyée", "en_retard")):
        assert text.endswith(_money(totals[status]))


def test_each_link_opens_its_own_status_list(client):
    """Followed, each link lists its status' invoices and no other — the
    late invoice the old link hid included."""
    numbers = {status: {n for n, s, _d, _p in _INVOICES.values() if s == status}
               for status in ("envoyée", "en_retard")}
    for href, _text in _links(_tile(client)):
        status = parse_qs(urlsplit(href).query)["status"][0]
        page = client.get(href).get_data(as_text=True)
        assert f'<option value="{status}" selected>' in page
        listed = {n for n, *_ in _INVOICES.values() if f">{n}<" in page}
        assert listed == numbers[status], (status, listed)


def test_get_billing_snapshot_counts_what_the_tile_counts(fake):
    """The connector serves a second firm-wide « impayé »:
    get_billing_snapshot sums balance_of over its OWN read of the same pair,
    so that its total matches the rows it lists, and never calls
    get_outstanding_totals. Nothing but this test ties the two definitions:
    on one store — every status, a payée set by hand with its balance left,
    an imported late invoice, a partly paid one — they agree, so a change to
    one that the other does not follow fails here, not in two contradictory
    answers to claude.ai."""
    extra = {
        "inv-g": ("2030-117", "en_retard", 33000,
                  {"imported": True, "legacy_ref": "facture:2030-117"}),
        "inv-h": ("2031-F007", "payée", 4400, {}),
    }
    for iid, (number, status, due, more) in extra.items():
        fake.seed(f"invoices/{iid}", {
            "id": iid, "invoice_number": number, "status": status,
            "dossier_id": "dos1", "dossier_file_number": "2031-004",
            "dossier_title": "Exemple c. Témoin", "client_id": "c1",
            "client_name": "Cliente Exemple",
            "date": datetime(2031, 3, 9, tzinfo=UTC),
            "due_date": datetime(2031, 4, 9, tzinfo=UTC),
            "total": due, "retainer_applied": 0,
            "amount_due": due, "amount_paid": 0, "etag": f"{iid}-e0", **more,
        })
    snapshot = handlers.get_billing_snapshot({})
    assert snapshot["scope"] == "global"
    assert snapshot["outstanding_cents"] == invoice_model.get_outstanding_total() == (
        SENT + LATE + 33000)


def test_a_failed_sum_keeps_the_tile_and_its_links(client, monkeypatch):
    """Fail-open, display only: should the sums raise all the same, the
    route shows 0,00 $ — the tile's original posture — and both links stay,
    the way to the lists."""
    def _boom(*_a, **_k):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(invoice_model, "get_outstanding_totals", _boom)
    tile = _tile(client)
    links = _links(tile)
    assert [parse_qs(urlsplit(h).query)["status"] for h, _ in links] == [
        ["envoyée"], ["en_retard"]]
    assert [text for _, text in links] == [f"Envoyées {_money(0)}",
                                           f"En retard {_money(0)}"]
    assert " ".join(tile.split()).count(_money(0)) == 3


def _fail_invoice_queries(monkeypatch, fake, times: int) -> list:
    """The next *times* queries on ``invoices`` fail at the transport — a
    Firestore blip, BELOW every model reader, where a real failure happens —
    while every other read stays healthy (the house pattern of
    tests/test_analyse_note_rules.py)."""
    server = fake._fake_server
    real = server.run_query
    failures = []

    def failing(request, metadata=None, **kwargs):
        query = request["structured_query"]._pb
        if any(f.collection_id == "invoices" for f in query.from_) and len(failures) < times:
            failures.append(1)
            raise gexc.ServiceUnavailable("injected query failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", failing)
    return failures


@pytest.mark.parametrize("times", [1, 2], ids=["the-first-invoice-read", "two-invoice-reads"])
def test_a_failed_read_shows_no_partial_figure_and_is_logged(client, fake, monkeypatch,
                                                            caplog, times):
    """A Firestore blip on the invoices shows 0,00 $ on the total and on both
    links, and is logged. Read once per status through list_invoices, which
    swallows a failure into [], one failed read showed « Envoyées 0,00 $ »
    beside the late invoices' real figure — a 0 that only meant « unread » —
    and two showed 0,00 $ everywhere; neither logged anything."""
    failures = _fail_invoice_queries(monkeypatch, fake, times)
    with caplog.at_level("WARNING", logger=invoice_model.logger.name):
        tile = _tile(client)
    assert failures                                     # the blip did happen
    assert [text for _, text in _links(tile)] == [f"Envoyées {_money(0)}",
                                                  f"En retard {_money(0)}"]
    assert " ".join(tile.split()).count(_money(0)) == 3
    assert [r.getMessage() for r in caplog.records if r.name == invoice_model.logger.name] == [
        "get_outstanding_totals: failed: ServiceUnavailable"]


def test_the_links_follow_the_issued_statuses():
    """One source per list: the tile's links are the statuses the sums
    cover, in their order — a status added to one without the other fails
    here, not on the tile."""
    assert tuple(dashboard_routes._OUTSTANDING_LINK_LABELS) == invoice_model._ISSUED_STATUSES


def test_the_tile_uses_compiled_classes_and_44_px_links(client):
    """Change Impact item 6: every class of the rendered tile is in the
    compiled stylesheet. Rule 9: each link is a block of py-1.5 (2 × 6 px)
    around a text-xs label (16 px) and a text-sm amount (20 px): 48 px."""
    tile = _tile(client)
    classes = {c for attr in re.findall(r'class="([^"]+)"', tile) for c in attr.split()}
    assert {"divide-y", "py-1.5", "text-2xl"} <= classes        # not vacuous
    css = compiled._artifact(_ATHENA / "static" / "vendor").read_text(encoding="utf-8")
    selectors = compiled.compiled_selectors(css)
    assert sorted(c for c in classes if not compiled.is_compiled(c, selectors)) == []
    anchors = re.findall(r'<a href="[^"]*"\s+class="([^"]*)">(.*?)</a>', tile, re.S)
    assert len(anchors) == 2
    for link_classes, inner in anchors:
        assert {"block", "py-1.5"} <= set(link_classes.split()), link_classes
        spans = re.findall(r'<span class="([^"]*)">', inner)
        assert [set(s.split()) & {"block", "text-xs", "text-sm"} for s in spans] == [
            {"block", "text-xs"}, {"block", "text-sm"}], spans
