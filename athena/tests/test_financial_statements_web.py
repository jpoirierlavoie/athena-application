"""The financial statements' page and PDF — routes, service, models and
templates together (lot B).

The bench is the shared fake Firestore (``tests/_fake_firestore.py``) under
the real routes, readers and templates; « today » is frozen on the one
clock (``utils.deadlines.datetime``). Every amount, name and number is
synthetic, in the fictional year 2031.
"""

import html
import io
import logging
import os
import pathlib
import re
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import admin_ledger as al
    from models import expense, full_reads, invoice, settings, time_entry, trust
    from models import financial_statements as fs
    import routes.auth_routes as auth_routes
    import routes.financial_statements as rfs

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.html_attr import jsattr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc
NOW = datetime(2031, 9, 20, 16, 0, tzinfo=UTC)


def _d(y, m, d):
    return datetime(y, m, d, tzinfo=UTC)


def _fake_modules() -> list:
    modules = [m for n, m in sorted(sys.modules.items())
               if (n.startswith("models.") or n == "dav.sync")
               and getattr(m, "db", None) is not None]
    # Every model the page reads runs on the fake, never on the patched client.
    assert {al, expense, invoice, settings, time_entry, trust} <= set(modules)
    return modules


@pytest.fixture
def clock(monkeypatch):
    from utils import deadlines as dl

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr(dl, "datetime", _Clock)


def _seed(f):
    f.seed("admin_accounts/ops1", {"id": "ops1", "name": "Opérations fictives",
                                   "account_type": "opérations", "status": "actif"})
    f.seed("admin_accounts/card1", {"id": "card1", "name": "Carte fictive",
                                    "account_type": "carte_crédit", "status": "actif"})
    f.seed("invoices/inv1", {
        "id": "inv1", "invoice_number": "2031-F001", "status": "envoyée",
        "date": _d(2031, 8, 4), "subtotal_fees": 100000, "subtotal_expenses": 2500,
        "gst_amount": 5125, "qst_amount": 10224, "total": 117849,
        "retainer_applied": 0, "amount_due": 117849, "amount_paid": 40000,
        "dossier_id": "dos1"})
    f.seed("admin_transactions/a1", {
        "id": "a1", "account_id": "ops1", "kind": "apport", "direction": "recette",
        "amount": 500000, "date": _d(2031, 7, 1), "net_amount": 0, "gst_amount": 0,
        "qst_amount": 0, "status": "compensée"})
    f.seed("admin_transactions/a2", {
        "id": "a2", "account_id": "ops1", "kind": "encaissement_facture",
        "direction": "recette", "amount": 40000, "date": _d(2031, 8, 20),
        "invoice_id": "inv1", "net_amount": 0, "gst_amount": 0, "qst_amount": 0,
        "status": "compensée"})
    f.seed("admin_transactions/a3", {
        "id": "a3", "account_id": "card1", "kind": "dépense", "direction": "déboursé",
        "amount": 22990, "date": _d(2031, 8, 12), "category": "infonuagique_ia",
        "net_amount": 20000, "gst_amount": 1000, "qst_amount": 1990,
        "status": "en_circulation"})
    f.seed("admin_reconciliations/r1", {
        "id": "r1", "account_id": "ops1", "status": "complétée",
        "period_end": _d(2031, 7, 31)})
    f.seed("trust_accounts/tr1", {"id": "tr1", "name": "Fidéicommis fictif",
                                  "account_type": "général", "status": "actif"})
    f.seed("trust_transactions/t1", {
        "id": "t1", "account_id": "tr1", "direction": "recette", "amount": 75000,
        "status": "compensée", "date": _d(2031, 8, 2), "purpose": "dépôt_client",
        "dossier_id": "dos1", "client_id": "c1"})
    f.seed("timeentries/h1", {"id": "h1", "date": _d(2031, 8, 25), "billable": True,
                              "amount": 33000, "invoiced": False, "invoice_id": None})
    f.seed("expenses/x1", {"id": "x1", "date": _d(2031, 8, 26), "amount": 4500,
                           "invoiced": False, "invoice_id": None})


@pytest.fixture
def fake(monkeypatch, clock):
    f = install(monkeypatch, *_fake_modules())
    _seed(f)
    return f


def _app():
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl, jsattr=jsattr,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "")
    app.register_blueprint(rfs.etats_bp)
    app.register_blueprint(auth_routes.auth_bp)
    return app


@pytest.fixture
def client(fake):
    c = _app().test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _page(client, path="/comptabilite/etats/", status=200) -> str:
    resp = client.get(path)
    assert resp.status_code == status, resp.status_code
    return html.unescape(resp.get_data(as_text=True))


def _plain(text: str) -> str:
    return text.replace("\N{NO-BREAK SPACE}", " ").replace("\N{NARROW NO-BREAK SPACE}", " ")


def _money(cents: int) -> str:
    return _plain(format_cents_fr(cents))


def _section(page: str, title: str) -> str:
    """The HTML of one section, from its title to the next section."""
    start = page.index(f">{title}</h2>")
    nxt = page.find("<section", start)
    return page[start: nxt if nxt != -1 else len(page)]


def _fail_queries_on(monkeypatch, fake, collection: str) -> None:
    server = fake._fake_server
    real = server.run_query

    def failing(request, metadata=None, **kwargs):
        sq = request["structured_query"]._pb
        if any(f.collection_id == collection for f in sq.from_):
            raise gexc.ServiceUnavailable("injected query failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", failing)


# ══════════════════════════════════════════════════════════════════════════
# 1. The page
# ══════════════════════════════════════════════════════════════════════════


def test_the_default_is_the_last_complete_month_with_its_figures(client):
    page = _plain(_page(client))
    assert "Août 2031 — Du 1er août 2031 au 31 août 2031" in page
    assert "Arrêtée au 31 août 2031." in page
    for title in fs.SECTION_TITLES.values():
        assert f">{title}</h2>" in page, title
    income = _section(page, "État des résultats")
    assert _money(100000) in income                     # honoraires facturés
    assert _money(2500) in income                       # débours refacturés
    assert _money(20000) in income                      # infonuagique et IA
    assert _money(82500) in income                      # bénéfice net
    balance = _section(page, "Bilan")
    assert _money(117849 - 40000) in balance            # comptes clients
    assert _money(22990) in balance                     # carte, solde dû
    assert "Écart non expliqué" in page
    assert "Opérations fictives : concilié jusqu'au 31 juillet 2031" in page


def test_an_open_period_says_it_is_computed_to_today(client):
    page = _page(client, "/comptabilite/etats/?periode=2031-T3")
    assert "Période en cours : calculée au 20 septembre 2031 inclusivement." in page


def test_the_selector_offers_every_period_and_marks_the_shown_one(client):
    page = _page(client, "/comptabilite/etats/?periode=2031-T2")
    assert '<optgroup label="Mois">' in page and '<optgroup label="Années">' in page
    assert '<option value="2031-T2" selected>2e trimestre 2031</option>' in page
    assert '<option value="2025-01">janvier 2025</option>' in page
    assert 'value="2031-10"' not in page


def test_previous_and_next_stop_at_the_books_and_at_today(client):
    page = _page(client, "/comptabilite/etats/?periode=2031-T3")
    assert 'href="/comptabilite/etats/?periode=2031-T2"' in page
    assert 'aria-label="Période suivante"' not in page
    first = _page(client, "/comptabilite/etats/?periode=2025-01")
    assert 'aria-label="Période précédente"' not in first
    assert 'aria-label="Période suivante" title="Période suivante"' in first


@pytest.mark.parametrize("slug, refusal", [
    ("2031-13", "La période demandée est invalide"),
    ("2031-12", "Cette période n'est pas encore commencée."),
    ("2024-12", "Les livres commencent le 1er janvier 2025"),
    ("2031-t3", "La période demandée est invalide"),
])
@pytest.mark.parametrize("path", ["/comptabilite/etats/", "/comptabilite/etats/pdf"])
def test_a_refused_period_answers_400_with_no_figure(client, slug, refusal, path):
    page = _page(client, f"{path}?periode={slug}", status=400)
    assert refusal in page
    assert "État des résultats" not in page and "Télécharger le PDF" not in page


def test_an_anonymous_visitor_is_sent_to_login(fake):
    resp = _app().test_client().get("/comptabilite/etats/?periode=2031-08")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_the_routes_are_get_only(client):
    assert client.post("/comptabilite/etats/").status_code == 405
    assert client.post("/comptabilite/etats/pdf").status_code == 405


def test_every_read_is_inside_one_read_only_transaction(client, fake):
    fake.reset_logs()
    _page(client, "/comptabilite/etats/?periode=2031-08")
    assert fake.reads_outside_transactions() == []
    assert len({r.transaction for r in fake.reads}) == 1
    assert [op for c in fake.commits for op in c.ops] == []


# ══════════════════════════════════════════════════════════════════════════
# 2. Unavailable, never zero
# ══════════════════════════════════════════════════════════════════════════


def test_a_failed_collection_makes_its_sections_unavailable(client, fake, monkeypatch, caplog):
    _fail_queries_on(monkeypatch, fake, "invoices")
    caplog.set_level(logging.ERROR, logger="pallas.unexpected")
    page = _plain(_page(client, "/comptabilite/etats/?periode=2031-08"))
    income = _section(page, "État des résultats")
    assert "Indisponible — Lecture impossible (factures)" in income
    assert "0,00 $" not in income
    assert "indisponible" in page                       # the KPI cards
    trust_memo = _section(page, "Note — fonds en fidéicommis (hors bilan)")
    assert _money(75000) in trust_memo
    lines = [r.json_fields for r in caplog.records if hasattr(r, "json_fields")]
    assert {"event": "unexpected", "collection": "invoices",
            "error_type": "ServiceUnavailable"}.items() <= lines[0].items()


def test_a_truncated_read_is_never_a_figure(client, monkeypatch):
    # Two accounts pass a cap of two; the three entries do not.
    monkeypatch.setattr(full_reads, "FULL_READ_CAP", 2)
    page = _plain(_page(client, "/comptabilite/etats/?periode=2031-08"))
    income = _section(page, "État des résultats")
    assert "Lecture tronquée (registre d'administration)" in income
    assert "0,00 $" not in income


# ══════════════════════════════════════════════════════════════════════════
# 3. The PDF
# ══════════════════════════════════════════════════════════════════════════


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    return _plain("\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages))


def test_the_pdf_renders_the_statements(client, caplog):
    caplog.set_level(logging.INFO, logger="pallas.admin_ledger")
    resp = client.get("/comptabilite/etats/pdf?periode=2031-08")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert 'filename="etats-financiers_2031-08.pdf"' in resp.headers["Content-Disposition"]
    data = resp.data
    assert data.startswith(b"%PDF") and b"NotoSerif" in data and b"Helvetica" not in data
    text = _pdf_text(data)
    assert "États financiers" in text and "Bénéfice net" in text
    assert "Période : août 2031, du 1er août 2031 au 31 août 2031" in text
    assert "Points à vérifier" in text
    assert "Généré le 2031-09-20 12:00" in text            # Montréal time
    (line,) = [r.json_fields for r in caplog.records
               if getattr(r, "json_fields", {}).get("event") == "admin_export"]
    assert line["document"] == "etats_financiers" and line["format"] == "pdf"
    assert line["period_kind"] == "mois"
    assert line["counts"]["invoices"] == 1 and line["counts"]["admin_transactions"] == 3
    assert not [k for k in line if "amount" in k or "cents" in k]


def test_the_pdf_is_refused_when_a_statement_is_unavailable(client, fake, monkeypatch):
    _fail_queries_on(monkeypatch, fake, "trust_transactions")
    resp = client.get("/comptabilite/etats/pdf?periode=2031-08")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(
        "/comptabilite/etats/?periode=2031-08&erreur=pdf_indisponible")
    page = _page(client, "/comptabilite/etats/?periode=2031-08&erreur=pdf_indisponible")
    assert "Le PDF n'a pas été produit" in page


def test_an_unknown_error_code_shows_nothing(client):
    page = _page(client, "/comptabilite/etats/?periode=2031-08&erreur=<b>x</b>")
    assert 'role="alert"' not in page


# ══════════════════════════════════════════════════════════════════════════
# 4. The template
# ══════════════════════════════════════════════════════════════════════════


def test_the_template_runs_no_script_and_no_htmx():
    src = (_ATHENA / "templates" / "comptabilite" / "etats.html").read_text(encoding="utf-8")
    assert "<script" not in src and "hx-" not in src and "=>" not in src
    assert 'method="get"' in src
    assert re.search(r"\bpl-\d", src) is None and re.search(r"\bms-\d", src) is None


def test_the_hub_links_to_the_statements():
    src = (_ATHENA / "templates" / "comptabilite" / "index.html").read_text(encoding="utf-8")
    assert "url_for('etats_financiers.index')" in src


# ══════════════════════════════════════════════════════════════════════════
# 5. The capped reader and the clock
# ══════════════════════════════════════════════════════════════════════════


def test_the_whole_collection_reader_is_capped_and_ids_are_the_documents(fake):
    fake.seed("invoices/inv2", {"id": "stale-id", "invoice_number": "2031-F002",
                                "status": "brouillon"})
    transaction = invoice.db.transaction(read_only=True)
    out = {}

    from google.cloud import firestore

    @firestore.transactional
    def _read(txn):
        out["rows"] = invoice.list_invoices_strict(txn)
        try:
            full_reads.stream_all(invoice.db.collection("invoices"), transaction=txn, cap=1)
        except full_reads.TruncatedRead:
            out["truncated"] = True

    _read(transaction)
    assert sorted(r["id"] for r in out["rows"]) == ["inv1", "inv2"]
    assert out["truncated"] is True


def test_now_utc_is_the_frozen_clock(clock):
    from utils import deadlines

    assert deadlines.now_utc() == NOW
    assert deadlines.today_mtl() == to_mtl(NOW).date()


# ══════════════════════════════════════════════════════════════════════════
# 6. Review of lot B — the transaction's own guarantees and failures
# ══════════════════════════════════════════════════════════════════════════


def test_the_snapshot_transaction_is_begun_read_only(client, fake, monkeypatch):
    server = fake._fake_server
    real = server.begin_transaction
    begun = []

    def spy(request, metadata=None, **kwargs):
        resp = real(request, metadata=metadata, **kwargs)
        begun.append(server._txns[resp.transaction].read_only)
        return resp

    monkeypatch.setattr(server, "begin_transaction", spy)
    _page(client, "/comptabilite/etats/?periode=2031-08")
    assert begun == [True]


@pytest.mark.parametrize("rpc, error", [
    ("begin_transaction", gexc.ServiceUnavailable),
    ("commit", gexc.DeadlineExceeded),
])
def test_a_failed_transaction_makes_every_section_unavailable(client, fake, monkeypatch,
                                                              caplog, rpc, error):
    def boom(*args, **kwargs):
        raise error("injected")

    monkeypatch.setattr(fake._fake_server, rpc, boom)
    caplog.set_level(logging.ERROR, logger="pallas.unexpected")
    page = _plain(_page(client, "/comptabilite/etats/?periode=2031-08"))
    for title in fs.SECTION_TITLES.values():
        assert "Indisponible — Lecture impossible" in _section(page, title), title
    assert "0,00 $" not in page.split("Points à vérifier")[0].split("Télécharger le PDF")[1]
    (line,) = [r.json_fields for r in caplog.records
               if getattr(r, "json_fields", {}).get("event") == "unexpected"
               and r.getMessage() == "financial statements: read transaction failed"]
    assert line["error_type"] == error.__name__
    resp = client.get("/comptabilite/etats/pdf?periode=2031-08")
    assert resp.status_code == 302 and "erreur=pdf_indisponible" in resp.headers["Location"]


def test_unread_reconciliations_are_said_on_the_page_and_in_the_pdf(client, fake, monkeypatch):
    _fail_queries_on(monkeypatch, fake, "admin_reconciliations")
    page = _page(client, "/comptabilite/etats/?periode=2031-08")
    assert "Conciliations indisponibles — Lecture impossible (conciliations d'administration)" in page
    resp = client.get("/comptabilite/etats/pdf?periode=2031-08")
    text = _pdf_text(resp.data)
    assert fs.FLOORS_HEADING in text
    assert "Conciliations indisponibles" in text


# ══════════════════════════════════════════════════════════════════════════
# The income statement's lines (the lawyer's decisions of 2026-11-08)
# ══════════════════════════════════════════════════════════════════════════


def test_the_fees_print_under_their_heading_and_other_income_as_one_line(client, fake):
    """A notary's and a lawyer's fees print beneath « Honoraires
    professionnels », indented one step further, on the page and in the PDF;
    an « autre recette » is part of one « Autres produits » line, never a
    line of its own."""
    for id_, category, amount in (("a4", "notaire", 11000), ("a5", "avocat", 22000)):
        fake.seed(f"admin_transactions/{id_}", {
            "id": id_, "account_id": "ops1", "kind": "dépense", "direction": "déboursé",
            "amount": amount, "date": _d(2031, 8, 14), "category": category,
            "counterparty": "Professionnel fictif", "net_amount": amount,
            "gst_amount": 0, "qst_amount": 0, "status": "compensée"})
    fake.seed("admin_transactions/a6", {
        "id": "a6", "account_id": "ops1", "kind": "recette_autre", "direction": "recette",
        "amount": 1234, "date": _d(2031, 8, 15), "counterparty": "Payeur fictif",
        "net_amount": 0, "gst_amount": 0, "qst_amount": 0, "status": "compensée"})
    income = _plain(_section(_page(client, "/comptabilite/etats/?periode=2031-08"),
                             "État des résultats"))
    nested = '<span class="block ml-3"><span class="block ml-3">'
    assert '<span class="block ml-3">Honoraires professionnels</span>' in income
    assert f"{nested}Notaire</span></span>" in income
    assert f"{nested}Avocat</span></span>" in income
    assert (income.index(">Honoraires professionnels<") < income.index(">Notaire<")
            < income.index(">Avocat<"))
    assert _money(33000) in income and _money(1234) in income
    assert "Payeur fictif" not in income
    text = _pdf_text(client.get("/comptabilite/etats/pdf?periode=2031-08").data)
    for label in ("Honoraires professionnels", "Notaire", "Avocat", "Autres produits"):
        assert label in text, label
    assert "Payeur fictif" not in text
