"""get_financial_statements — the « Finances » statements through the connector.

The real handler, service and model on the shared fake Firestore
(``tests/_fake_firestore.py``), « today » frozen on the one clock
(``utils.deadlines.datetime``). The payload must be the page's statements
— the same rows, labels and keys as ``models/financial_statements.present``
— for every period asked, all computed from ONE read-only transaction.
Every amount, name and number is synthetic, in the fictional year 2031.
"""

import os
import pathlib
import sys
from datetime import date, datetime, timezone
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
    import mcp.handlers as handlers
    import mcp.tools as tools
    from mcp.output_schemas import OUTPUT_SCHEMAS
    from models import admin_ledger as al
    from models import expense, invoice, time_entry, trust
    from models import financial_statements as fs
    from services import financial_statements as statements_service

from tests._fake_firestore import install  # noqa: E402
from tests.test_mcp_output_schemas import _conforms  # noqa: E402
from utils import periods  # noqa: E402

UTC = timezone.utc
NOW = datetime(2031, 9, 20, 16, 0, tzinfo=UTC)      # noon in Montréal
TODAY = date(2031, 9, 20)
TOOL = "get_financial_statements"


def _d(y, m, d):
    return datetime(y, m, d, tzinfo=UTC)


def _fake_modules() -> list:
    modules = [m for n, m in sorted(sys.modules.items())
               if (n.startswith("models.") or n == "dav.sync")
               and getattr(m, "db", None) is not None]
    # Every model the statements read runs on the fake.
    assert {al, expense, invoice, time_entry, trust} <= set(modules)
    return modules


@pytest.fixture
def fake(monkeypatch):
    from utils import deadlines as dl

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr(dl, "datetime", _Clock)
    f = install(monkeypatch, *_fake_modules())
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
    f.seed("invoices/inv2", {
        "id": "inv2", "invoice_number": "2031-F002", "status": "envoyée",
        "date": _d(2031, 7, 15), "subtotal_fees": 60000, "subtotal_expenses": 0,
        "gst_amount": 3000, "qst_amount": 5985, "total": 68985,
        "retainer_applied": 0, "amount_due": 68985, "amount_paid": 0,
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
    return f


def _call(**args) -> dict:
    return handlers.get_financial_statements(args)


def _fail_queries_on(monkeypatch, fake, collection: str) -> None:
    server = fake._fake_server
    real = server.run_query

    def failing(request, metadata=None, **kwargs):
        sq = request["structured_query"]._pb
        if any(f.collection_id == collection for f in sq.from_):
            raise gexc.ServiceUnavailable("injected query failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", failing)


def _section(period: dict, key: str) -> dict:
    (section,) = [s for s in period["sections"] if s["key"] == key]
    return section


def _row(section: dict, key: str) -> dict:
    (row,) = [r for r in section["rows"] if r["key"] == key]
    return row


# ══════════════════════════════════════════════════════════════════════════
# The contract, and the page's own statements
# ══════════════════════════════════════════════════════════════════════════


def test_the_payload_conforms_and_is_the_page_s_statements(fake):
    payload = handlers.get_financial_statements(
        {"periods": ["2031-07", "2031-08", "2031-T3"]})
    _conforms("get_financial_statements", payload)
    assert [p["period"] for p in payload["periods"]] == ["2031-07", "2031-08", "2031-T3"]
    for got in payload["periods"]:
        period, _ = periods.parse_period(got["period"], today=TODAY,
                                         books_start=fs.BOOKS_START)
        _st, view, _counts = statements_service.build(period, today=TODAY, now=NOW)
        assert got["label"] == view["period_label"] and got["span"] == view["span"]
        assert got["as_of_line"] == view["as_of_line"]
        assert [(k["key"], k["label"], k["value_cents"]) for k in got["kpis"]] == [
            (k["key"], k["label"], k["value"]) for k in view["kpis"]]
        assert [s["key"] for s in got["sections"]] == list(fs.SECTION_TITLES)
        for mine, page in zip(got["sections"], view["sections"]):
            assert (mine["title"], mine["columns"], mine["notes"], mine["unavailable"]) == (
                page["title"], page["columns"], page["notes"], page["unavailable"])
            assert [(r["key"], r["label"], r["level"], r["style"], r["note"],
                     r["values_cents"]) for r in mine["rows"]] == [
                (r["key"], r["label"], r["level"], r["style"], r["note"], r["values"])
                for r in page["rows"]]


def test_money_carries_its_display_and_a_heading_carries_none(fake):
    (period,) = _call(periods=["2031-08"])["periods"]
    income = _section(period, "resultats")
    fees = _row(income, "honoraires")
    assert fees["values_cents"] == [100000]
    assert fees["values_display"] == [tools.format_cents(100000)]
    heading = _row(income, "produits")
    assert heading["values_cents"] == [None] and heading["values_display"] == [None]


def test_a_period_holding_today_says_it_is_computed_to_today(fake):
    payload = _call(periods=["2031-08", "2031-T3"])
    august, quarter = payload["periods"]
    assert (august["in_progress"], august["as_of"], august["end"]) == (
        False, "2031-08-31", "2031-08-31")
    assert (quarter["kind"], quarter["start"], quarter["end"]) == (
        "trimestre", "2031-07-01", "2031-09-30")
    assert quarter["in_progress"] is True and quarter["as_of"] == "2031-09-20"


def test_every_period_comes_from_one_read_only_transaction(fake, monkeypatch):
    server = fake._fake_server
    real = server.begin_transaction
    begun = []

    def spy(request, metadata=None, **kwargs):
        resp = real(request, metadata=metadata, **kwargs)
        begun.append(server._txns[resp.transaction].read_only)
        return resp

    monkeypatch.setattr(server, "begin_transaction", spy)
    fake.reset_logs()
    _call(periods=["2031-06", "2031-07", "2031-08", "2031-T2", "2031"])
    assert begun == [True]
    assert fake.reads_outside_transactions() == []
    assert len({r.transaction for r in fake.reads}) == 1
    assert [op for c in fake.commits for op in c.ops] == []


def test_row_keys_are_unique_in_each_section_and_line_periods_up(fake):
    payload = _call(periods=["2031-07", "2031-08"])
    for period in payload["periods"]:
        for section in period["sections"]:
            keys = [r["key"] for r in section["rows"]]
            assert all(keys), section["key"]
            assert len(keys) == len(set(keys)), section["key"]
    july, august = payload["periods"]
    fees = [_row(_section(p, "resultats"), "honoraires")["values_cents"]
            for p in (july, august)]
    assert fees == [[60000], [100000]]
    balance = _section(august, "bilan")
    assert {"encaisse:ops1", "carte:card1", "avoir_net"} <= {
        r["key"] for r in balance["rows"]}
    assert _row(_section(august, "fideicommis"), "compte:tr1")["values_cents"] == [
        0, 75000]
    # A category keyed by its class: the same code may fall in two.
    assert "categorie:variable:infonuagique_ia" in {
        r["key"] for r in _section(august, "resultats")["rows"]}


def test_one_category_in_two_classes_keeps_two_keys(fake):
    """A bailiff's fee filed with its dossier is a direct cost; one filed
    without lands in the fixed costs (and is a point): the same category in
    two classes of one period — the class in the key keeps both rows."""
    for rid, dossier in (("b1", "dos1"), ("b2", "")):
        fake.seed(f"admin_transactions/{rid}", {
            "id": rid, "account_id": "ops1", "kind": "dépense",
            "direction": "déboursé", "amount": 11498, "date": _d(2031, 8, 14),
            "category": "huissier", "dossier_id": dossier, "net_amount": 10000,
            "gst_amount": 500, "qst_amount": 998, "status": "compensée"})
    (period,) = _call(periods=["2031-08"], sections=["resultats"])["periods"]
    keys = [r["key"] for r in _section(period, "resultats")["rows"]]
    assert len(keys) == len(set(keys))
    assert {"categorie:direct:huissier", "categorie:fixe:huissier"} <= set(keys)
    direct = _row(_section(period, "resultats"), "categorie:direct:huissier")
    assert direct["values_cents"] == [10000]


def test_only_the_sections_asked_for_come_in_the_page_s_order(fake):
    (period,) = _call(periods=["2031-08"], sections=["taxes", "resultats"])["periods"]
    assert [s["key"] for s in period["sections"]] == ["resultats", "taxes"]
    assert [k["key"] for k in period["kpis"]] == [
        "produits", "benefice_net", "encaisse", "comptes_clients"]
    assert "points" in period and "points_incomplete" in period


def test_the_reconciliations_are_the_snapshot_s_once(fake):
    payload = _call(periods=["2031-07", "2031-08"])
    assert payload["reconciliations"] == [
        {"account_id": "card1", "name": "Carte fictive", "reconciled_through": None},
        {"account_id": "ops1", "name": "Opérations fictives",
         "reconciled_through": "2031-07-31"},
    ]
    assert payload["reconciliations_unavailable"] == ""
    assert payload["books_start"] == fs.BOOKS_START.isoformat()
    assert payload["computed_at"].startswith("2031-09-20T12:00:00")


# ══════════════════════════════════════════════════════════════════════════
# Unavailable, never zero — and the points, capped and said so
# ══════════════════════════════════════════════════════════════════════════


def test_a_failed_collection_makes_its_sections_unavailable(fake, monkeypatch):
    _fail_queries_on(monkeypatch, fake, "invoices")
    payload = _call(periods=["2031-08"])
    _conforms("get_financial_statements", payload)
    (period,) = payload["periods"]
    income = _section(period, "resultats")
    assert income["unavailable"].startswith("Lecture impossible (factures)")
    assert income["rows"] == []
    kpis = {k["key"]: k for k in period["kpis"]}
    assert kpis["produits"]["value_cents"] is None
    assert kpis["produits"]["value_display"] is None
    assert _section(period, "fideicommis")["unavailable"] == ""
    assert period["points_incomplete"]


def test_unread_reconciliations_are_said(fake, monkeypatch):
    _fail_queries_on(monkeypatch, fake, "admin_reconciliations")
    payload = _call(periods=["2031-08"])
    _conforms("get_financial_statements", payload)
    assert payload["reconciliations"] == []
    assert payload["reconciliations_unavailable"].startswith(
        "Lecture impossible (conciliations d'administration)")


@pytest.mark.parametrize("rpc, error", [
    ("begin_transaction", gexc.ServiceUnavailable),
    ("commit", gexc.DeadlineExceeded),
])
def test_a_snapshot_that_read_nothing_is_refused(fake, monkeypatch, rpc, error):
    def boom(*args, **kwargs):
        raise error("injected")

    monkeypatch.setattr(fake._fake_server, rpc, boom)
    with pytest.raises(tools.ToolArgumentError, match="illisibles pour le moment"):
        _call(periods=["2031-08"])


def test_a_point_s_ids_are_capped_and_say_so(fake):
    for n in range(25):
        fake.seed(f"admin_transactions/s{n:02d}", {
            "id": f"s{n:02d}", "account_id": "ops1", "kind": "dépense",
            "direction": "déboursé", "amount": 1000 + n, "date": _d(2031, 8, 5),
            "category": "fournitures", "net_amount": 1000 + n, "gst_amount": 0,
            "qst_amount": 0, "status": "compensée", "counterparty": "Fournisseur fictif"})
    payload = _call(periods=["2031-08"])
    _conforms("get_financial_statements", payload)
    (point,) = [p for p in payload["periods"][0]["points"]
                if p["key"] == "sans_ventilation"]
    assert point["count"] == 25
    assert len(point["ids"]) == len(point["refs"]) == tools.STATEMENT_POINT_REFS_MAX
    assert point["truncated"] is True
    assert point["amount_cents"] == sum(1000 + n for n in range(25))
    assert point["measured_gap_cents"] is None and point["measured_gap_display"] is None


def test_an_uncapped_point_is_not_truncated(fake):
    fake.seed("invoices/inv3", {
        "id": "inv3", "invoice_number": "2031-F003", "status": "brouillon",
        "date": _d(2031, 8, 28), "subtotal_fees": 20000, "subtotal_expenses": 0,
        "gst_amount": 1000, "qst_amount": 1995, "total": 22995,
        "retainer_applied": 0, "amount_due": 22995, "amount_paid": 0,
        "dossier_id": "dos1"})
    (period,) = _call(periods=["2031-08"])["periods"]
    (draft,) = [p for p in period["points"] if p["key"] == "brouillons"]
    assert (draft["count"], draft["ids"], draft["amount_cents"]) == (
        1, ["2031-F003"], 22995)
    for point in period["points"]:
        assert point["truncated"] is (point["count"] > tools.STATEMENT_POINT_REFS_MAX)


# ══════════════════════════════════════════════════════════════════════════
# Refusals — in French, before any read
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("args, message", [
    ({"periods": ["2031-13"]}, "Période « 2031-13 » : La période demandée est invalide"),
    ({"periods": ["2024-12"]}, "Période « 2024-12 » : Les livres commencent le"),
    ({"periods": ["2031-10"]}, "Période « 2031-10 » : Cette période n'est pas encore"),
    ({"periods": ["2031-08", "2031-08"]}, "La période « 2031-08 » est demandée deux fois."),
    ({"periods": []}, "Indiquez au moins une période"),
    ({"periods": ["2031-08"], "sections": ["bilan", "bilan"]},
     "Une section est demandée deux fois"),
])
def test_a_refused_argument_reads_nothing(fake, args, message):
    fake.reset_logs()
    with pytest.raises(tools.ToolArgumentError) as caught:
        handlers.get_financial_statements(args)
    assert str(caught.value.args[0]).startswith(message)
    assert fake.reads == []


def test_the_schema_bounds_the_series_and_names_the_sections():
    schema = tools.TOOLS[TOOL]["input_schema"]
    many = [f"2030-{m:02d}" for m in range(1, 13)] + [f"2031-{m:02d}" for m in range(1, 14)]
    assert len(many) == tools.STATEMENT_PERIODS_MAX + 1
    assert tools.validate_args(schema, {"periods": many})
    assert tools.validate_args(schema, {"periods": many[:-1]}) == []
    assert tools.validate_args(schema, {"periods": ["2031-08"], "sections": ["bilans"]})
    assert tools.validate_args(schema, {}) != []


def test_the_section_vocabularies_are_the_model_s():
    assert tools._STATEMENT_SECTIONS == list(fs.SECTION_TITLES)
    section = OUTPUT_SCHEMAS[TOOL]["properties"]["periods"]["items"]["properties"][
        "sections"]["items"]
    assert section["properties"]["key"]["enum"] == list(fs.SECTION_TITLES)
    kind = OUTPUT_SCHEMAS[TOOL]["properties"]["periods"]["items"]["properties"]["kind"]
    assert kind["enum"] == list(periods.KINDS)


def test_it_is_an_ordinary_read():
    assert TOOL not in tools.WRITE_TOOLS
    assert tools.required_scope(TOOL) == "athena:read"
    (descriptor,) = [d for d in tools.list_tool_descriptors(None) if d["name"] == TOOL]
    assert descriptor["annotations"]["readOnlyHint"] is True
    assert descriptor["annotations"]["openWorldHint"] is False


# ══════════════════════════════════════════════════════════════════════════
# Write-offs (« radiation », lot 3a, part 2/3) — rows the schema already holds
# ══════════════════════════════════════════════════════════════════════════


def _event(id_, day, amount, gst, qst, *, kind="radiation", reverses=""):
    return {"id": id_, "kind": kind, "date": day, "amount": amount,
            "gst_amount": gst, "qst_amount": qst, "tax_claimed": bool(gst or qst),
            "reason": "", "reverses_id": reverses, "created_at": day, "created_via": "web"}


def _write_off(fake) -> tuple:
    """inv1's remainder (1 178,49 $ less the 400 $ paid) written off on 15
    September 2031, its TPS/TVQ claimed; inv2 written off on 2 September and
    the radiation cancelled on the 10th. Returns inv1's (TPS, TVQ) parts."""
    inv1 = {"id": "inv1", "invoice_number": "2031-F001", "status": "payée",
            "date": _d(2031, 8, 4), "subtotal_fees": 100000, "subtotal_expenses": 2500,
            "gst_amount": 5125, "qst_amount": 10224, "total": 117849,
            "retainer_applied": 0, "amount_due": 117849, "amount_paid": 40000,
            "dossier_id": "dos1"}
    gst, qst = invoice.write_off_tax_parts(inv1, 77849)
    inv1["write_offs"] = [_event("w1", _d(2031, 9, 15), 77849, gst, qst)]
    fake.seed("invoices/inv1", inv1)
    inv2 = {"id": "inv2", "invoice_number": "2031-F002", "status": "en_retard",
            "date": _d(2031, 7, 15), "subtotal_fees": 60000, "subtotal_expenses": 0,
            "gst_amount": 3000, "qst_amount": 5985, "total": 68985,
            "retainer_applied": 0, "amount_due": 68985, "amount_paid": 0,
            "dossier_id": "dos1",
            "write_offs": [_event("w2", _d(2031, 9, 2), 68985, 3000, 5985),
                           _event("w3", _d(2031, 9, 10), 68985, 3000, 5985,
                                  kind="contre_passation", reverses="w2")]}
    fake.seed("invoices/inv2", inv2)
    return gst, qst


def test_a_write_off_reaches_the_connector_in_the_page_s_rows(fake):
    gst, qst = _write_off(fake)
    payload = _call(periods=["2031-08", "2031-09"])
    _conforms(TOOL, payload)                    # no schema change: the rows fit
    august, september = payload["periods"]
    for got in (august, september):
        period, _ = periods.parse_period(got["period"], today=TODAY,
                                         books_start=fs.BOOKS_START)
        _st, view, _counts = statements_service.build(period, today=TODAY, now=NOW)
        for mine, page in zip(got["sections"], view["sections"]):
            assert mine["notes"] == page["notes"]
            assert [(r["key"], r["label"], r["level"], r["style"], r["values_cents"])
                    for r in mine["rows"]] == [
                (r["key"], r["label"], r["level"], r["style"], r["values"])
                for r in page["rows"]]
    assert "radiations" not in {r["key"] for r in _section(august, "resultats")["rows"]}
    kpis = {k["key"]: k["value_cents"] for k in august["kpis"]}
    assert kpis["comptes_clients"] == 77849 + 68985
    down = _row(_section(september, "resultats"), "radiations")
    assert down["values_cents"] == [-(77849 - gst - qst)] and down["level"] == 1
    assert down["values_display"] == [tools.format_cents(-(77849 - gst - qst))]
    taxes = _section(september, "taxes")
    assert _row(taxes, "tps_creances_irrecouvrables")["values_cents"] == [-gst]
    assert _row(taxes, "tvq_creances_irrecouvrables")["values_cents"] == [-qst]
    adjustments = _section(september, "redressements")
    assert [(r["key"], r["values_cents"]) for r in adjustments["rows"]] == [
        ("groupe:radiations", [None, None]),
        ("radiation:w2", [3000, 5985]), ("radiation:w3", [-3000, -5985]),
        ("radiation:w1", [gst, qst])]
    assert {k["key"]: k["value_cents"] for k in september["kpis"]}["comptes_clients"] == 68985


def test_the_revenue_sentence_names_the_write_off_rows_the_model_prints(fake):
    _write_off(fake)
    (september,) = _call(periods=["2031-09"])["periods"]
    printed = {r["key"] for s in september["sections"] for r in s["rows"]}
    description = tools.TOOLS[TOOL]["description"]
    assert "write-offs" in description and "radiation" in description
    for key in ("radiations", "tps_creances_irrecouvrables", "tvq_creances_irrecouvrables"):
        assert f"`{key}`" in description and key in printed, key
