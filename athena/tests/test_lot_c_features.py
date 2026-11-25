"""Comprehensive tests for Phase 3 (Lot C) features.

Covers:
1. Generalized dual-leg Admin account-to-account transfers (ops<->ops, ops->card,
   card->ops, card<->card), immediately born ``compensée``, atomic dual reversal,
   lock floors, financial-statements net neutrality (unexplained == 0), and
   verify_admin_integrity checks.
2. Dossier + client-linked Operations -> Trust client-advance bridge: atomic
   dual-leg creation immediately born ``compensée``, bank/client/ledger updates,
   financial-statements liability settlement, reconciliation locks, Barreau art. 59
   reversal guard, web routes, and verify_trust_integrity + verify_admin_integrity.
3. Configurable annual contractual overdue interest rate in cabinet settings.
4. Manual overdue interest notices (« Avis d'intérêts »): simple Actual/365 math,
   TPS/TVQ exemption, sequential AAAA-AI### counter, art. 1571 C.c.Q. payment
   imputation, write-off tax-relief cap on principal alone, _settled_status / void
   protection, dated reversal, .docx generation via avis_interets active template,
   financial statements + rollforward (unexplained == 0), Journal des radiations et
   intérêts (HTML / CSV / PDF), and integrity Checks 12 & 13.
"""

from __future__ import annotations

import io
import os
import pathlib
import sys
import zipfile
from datetime import date, datetime, timezone
from unittest import mock

import pytest
from flask import Flask

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import (
        admin_ledger as al,
        advance_transfer,
        doc_template as tpl,
        document as doc_model,
        financial_statements as fs,
        invoice as invoice_model,
        settings as settings_model,
        trust as trust_model,
    )
    import routes.admin_ledger as admin_routes
    import routes.invoices as invoice_routes
    import routes.journal_radiations as journal_routes
    import routes.settings as settings_routes
    import routes.trust as trust_routes
    from mcp import handlers, write_support
    from scripts import verify_admin_integrity, verify_trust_integrity
    from services import avis_interets, comptabilite, financial_statements as fs_svc
    from tz import to_mtl

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402
from tests._frozen_clock import freeze_deadlines  # noqa: E402
from tests.test_financial_statements import (  # noqa: E402
    BOOKS,
    _snapshot as _fs_snapshot,
)
from utils import format_fr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc
TODAY = date(2031, 3, 20)
UID = "uid-lawyer-1"


def _d(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def _fake_modules() -> list:
    return [
        m
        for n, m in sorted(sys.modules.items())
        if (n.startswith("models.") or n == "dav.sync")
        and getattr(m, "db", None) is not None
    ]


@pytest.fixture
def fake(monkeypatch):
    freeze_deadlines(monkeypatch, datetime(2031, 3, 20, 12, tzinfo=UTC))
    db = install(monkeypatch, *_fake_modules())
    monkeypatch.setattr(verify_admin_integrity, "db", db)
    monkeypatch.setattr(verify_trust_integrity, "db", db)
    monkeypatch.setattr(write_support, "db", db)
    return db


def _web_app(*blueprints) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(_ATHENA / "templates"),
    )
    app.config["TESTING"] = True
    app.secret_key = "test-secret"
    app.jinja_env.globals["csrf_token"] = lambda: "test-csrf"
    app.jinja_env.globals["csp_nonce"] = "test-nonce"
    app.jinja_env.globals["ms"] = ms
    app.jinja_env.globals["current_theme"] = "light"
    app.jinja_env.filters["cents_fr"] = lambda c: format_fr.format_cents_fr(c) if c is not None else ""
    app.jinja_env.filters["date_fr"] = format_fr.format_date_fr
    app.jinja_env.filters["to_mtl"] = to_mtl
    app.jinja_env.filters["jsattr"] = lambda v: str(v)

    @app.before_request
    def _auth():
        from flask import session
        session["user_id"] = UID
        session["email"] = "test@example.com"
        session["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)

    for bp in blueprints:
        app.register_blueprint(bp)
    for ep, rule in [
        ("auth.login", "/login"),
        ("invoices.invoice_list", "/factures/"),
        ("documents.document_detail", "/documents/<document_id>"),
        ("documents.document_download", "/documents/<document_id>/telecharger"),
        ("dossiers.dossier_detail", "/dossiers/<dossier_id>"),
        ("parties.partie_detail", "/parties/<partie_id>"),
        ("comptabilite.index", "/comptabilite/"),
        ("comptabilite.hub", "/comptabilite/hub"),
        ("financial_statements.index", "/finances/"),
        ("trust.index", "/fideicommis/"),
        ("admin_ledger.journal", "/administration/"),
        ("admin_ledger.entry_detail", "/administration/ecritures/<tx_id>"),
        ("admin_ledger.entry_new", "/administration/nouvelle"),
        ("settings.index", "/parametres/"),
    ]:
        if ep not in app.view_functions:
            app.add_url_rule(rule, endpoint=ep, view_func=lambda **kw: "")
    return app


def _seed_admin_accounts(fake):
    ops1, e1 = al.create_account({"name": "Opérations Principale", "account_type": "opérations"})
    ops2, e2 = al.create_account({"name": "Épargne Cabinet", "account_type": "opérations"})
    card1, e3 = al.create_account({"name": "Visa Cabinet", "account_type": "carte_crédit"})
    card2, e4 = al.create_account({"name": "Mastercard Cabinet", "account_type": "carte_crédit"})
    assert not (e1 or e2 or e3 or e4)
    return ops1["id"], ops2["id"], card1["id"], card2["id"]


def _seed_trust_and_dossier(fake):
    tr1, errs = trust_model.create_account({"name": "Fidéicommis général", "account_type": "général"})
    assert errs == []
    dossier_id = "d1111111-1111-4111-8111-111111111111"
    client_id = "c1111111-1111-4111-8111-111111111111"
    fake.seed(
        f"dossiers/{dossier_id}",
        {
            "id": dossier_id,
            "file_number": "2031-001",
            "title": "Client A c. Défendeur B",
            "client_ids": [client_id],
            "clients": [{"id": client_id, "name": "Client Alpha"}],
            "trust_balance": 0,
            "trust_balance_by_client": {},
            "trust_cleared_by_client": {},
            "status": "actif",
        },
    )
    fake.seed(
        f"parties/{client_id}",
        {
            "id": client_id,
            "kind": "personne_physique",
            "first_name": "Client",
            "last_name": "Alpha",
            "name": "Client Alpha",
        },
    )
    return tr1["id"], dossier_id, client_id


# ══════════════════════════════════════════════════════════════════════
# 1. Dual-leg immediate-compensée Admin account transfers
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "pair_builder, expect_kind",
    [
        (lambda o1, o2, c1, c2: (o1, o2), ("virement_interne_sortant", "virement_interne_entrant")),
        (lambda o1, o2, c1, c2: (o1, c1), ("paiement_carte", "paiement_carte")),
        (lambda o1, o2, c1, c2: (c1, o1), ("virement_interne_sortant", "virement_interne_entrant")),
        (lambda o1, o2, c1, c2: (c1, c2), ("virement_interne_sortant", "virement_interne_entrant")),
    ],
)
def test_admin_transfers_are_dual_leg_and_born_compensee(fake, pair_builder, expect_kind):
    o1, o2, c1, c2 = _seed_admin_accounts(fake)
    src, dst = pair_builder(o1, o2, c1, c2)
    tx_day = _d(2031, 3, 5)
    report = comptabilite.enregistrer_virement_comptes_admin(
        src, dst, 25000, tx_day, "virement", reference="VIR-01", description="Transfert"
    )
    assert report["ok"] is True and report["errors"] == []
    leg = report["entry"]
    other = report["counter_leg"]
    assert leg["kind"] == expect_kind[0]
    assert leg["status"] == "compensée"
    assert leg["cleared_date"] == tx_day
    assert other["kind"] == expect_kind[1]
    assert other["status"] == "compensée"
    assert other["cleared_date"] == tx_day
    assert other["related_transaction_id"] == leg["id"]
    assert other["account_id"] == dst

    # Integrity checks pass with zero problems/notes.
    problems, notes = verify_admin_integrity.collect()
    assert problems == [] and notes == []

    # Financial statements: internal transfers net to 0 in equity & stay out of income.
    snap = _fs_snapshot(
        accounts=list(fake.peek_collection("admin_accounts").values()),
        transactions=list(fake.peek_collection("admin_transactions").values()),
    )
    period = fs.periods.Period("mois", 2031, 3)
    st = fs.compute(snap, period, today=TODAY, now=datetime(2031, 3, 20, 12, tzinfo=UTC), books_start=BOOKS)
    assert st.sections["resultats"]["revenue"] == 0
    assert st.sections["avoir"]["unexplained"] == 0
    assert st.sections["avoir"]["virements"] == 0
    point_keys = {p["key"] for p in st.points}
    assert "avance_possible" not in point_keys
    assert "remise_possible" not in point_keys


def test_admin_transfer_refuses_same_account_and_reconciled_period_and_reverses_both_legs(fake):
    o1, o2, _c1, _ = _seed_admin_accounts(fake)
    _, errs = al.create_account_transfer(o1, o1, 10000, _d(2031, 3, 5), "virement")
    assert errs and "différents" in errs[0]

    # Seed a completed reconciliation locking o2 through 2031-03-10.
    rec_id = "rec-lock-o2"
    fake.seed(
        f"admin_reconciliations/{rec_id}",
        {
            "id": rec_id,
            "account_id": o2,
            "status": "complétée",
            "period_end": _d(2031, 3, 10),
        },
    )
    _, errs = al.create_account_transfer(o1, o2, 10000, _d(2031, 3, 8), "virement")
    assert errs and "conciliation complétée" in errs[0]
    assert fake.peek_collection("admin_transactions") == {}

    # Transfer on unlocked date succeeds and reverses atomically.
    leg, errs = al.create_account_transfer(o1, o2, 15000, _d(2031, 3, 12), "virement")
    assert errs == []
    rev, errs = al.reverse_transaction(leg["id"], "Erreur de montant")
    assert errs == []
    assert rev["reverses_id"] == leg["id"]
    assert fake.peek(f"admin_accounts/{o1}")["ledger_balance"] == 0
    assert fake.peek(f"admin_accounts/{o2}")["ledger_balance"] == 0


def test_admin_transfer_and_card_payment_web_routes(fake):
    o1, o2, _c1, _ = _seed_admin_accounts(fake)
    app = _web_app(admin_routes.admin_bp, trust_routes.trust_bp)
    client = app.test_client()

    assert client.get("/administration/virement").status_code == 200
    resp = client.post(
        "/administration/virement",
        data={
            "from_account_id": o1,
            "to_account_id": o2,
            "amount": "200,00",
            "date": "2031-03-15",
            "method": "virement",
            "reference": "VIR-WEB",
            "description": "Transfert web",
        },
    )
    assert resp.status_code == 302
    txs = fake.peek_collection("admin_transactions")
    assert len(txs) == 2
    assert all(tx["status"] == "compensée" for tx in txs.values())


# ══════════════════════════════════════════════════════════════════════
# 2. Operations -> Trust client-advance bridge
# ══════════════════════════════════════════════════════════════════════


def test_advance_transfer_bridge_happy_path_statements_and_art59_reversal(fake):
    o1, _o2, c1, _ = _seed_admin_accounts(fake)
    tr1, dossier_id, client_id = _seed_trust_and_dossier(fake)

    # First receive a client advance in Operations (creates liability on balance sheet).
    adv_in, errs = al.create_transaction(
        {
            "account_id": o1,
            "direction": "recette",
            "kind": "avance_honoraires",
            "amount": 100000,
            "date": _d(2031, 3, 3),
            "method": "virement",
            "counterparty": "Client Alpha",
            "dossier_id": dossier_id,
        }
    )
    assert errs == []
    _, errs = al.clear_transaction(adv_in["id"], _d(2031, 3, 3))
    assert errs == []

    # Bridge the advance from Ops -> Trust atomically.
    report = comptabilite.enregistrer_virement_avances(
        admin_account_id=o1,
        trust_account_id=tr1,
        dossier_id=dossier_id,
        client_id=client_id,
        amount=100000,
        date_value=_d(2031, 3, 5),
        method="virement",
        reference="AV-01",
        description="Virement d'avance au fidéicommis",
    )
    assert report["ok"] is True and report["errors"] == []
    admin_leg = report["admin_entry"]
    trust_leg = report["trust_entry"]
    assert admin_leg["status"] == "compensée"
    assert admin_leg["cleared_date"] == _d(2031, 3, 5)
    assert admin_leg["kind"] == "virement_interne_sortant"
    assert admin_leg["transfer_purpose"] == "avances"
    assert admin_leg["trust_transaction_id"] == trust_leg["id"]

    assert trust_leg["status"] == "compensée"
    assert trust_leg["cleared_date"] == _d(2031, 3, 5)
    assert trust_leg["purpose"] == "avance_honoraires"
    assert trust_leg["admin_transaction_id"] == admin_leg["id"]
    assert trust_leg["dossier_id"] == dossier_id
    assert trust_leg["client_id"] == client_id

    # Trust & Admin balances updated atomically.
    assert fake.peek(f"admin_accounts/{o1}")["ledger_balance"] == 0
    tr_acc = fake.peek(f"trust_accounts/{tr1}")
    assert tr_acc["book_balance"] == 100000
    assert tr_acc["bank_balance"] == 100000
    dossier = fake.peek(f"dossiers/{dossier_id}")
    assert dossier["trust_balance"] == 100000
    assert dossier["trust_cleared_by_client"][client_id] == 100000

    # Integrity audits clean on both registers!
    a_prob, a_notes = verify_admin_integrity.collect()
    assert a_prob == [] and a_notes == []
    t_prob, t_notes = verify_trust_integrity.collect()
    assert t_prob == [] and t_notes == []

    # Financial statements settle client-advance liability cleanly (advances == 0, unexplained == 0).
    snap = _fs_snapshot(
        accounts=list(fake.peek_collection("admin_accounts").values()),
        transactions=list(fake.peek_collection("admin_transactions").values()),
        trust_accounts=[tr_acc],
        trust_rows=[trust_leg],
    )
    period = fs.periods.Period("mois", 2031, 3)
    st = fs.compute(snap, period, today=TODAY, now=datetime(2031, 3, 20, 12, tzinfo=UTC), books_start=BOOKS)
    assert st.sections["bilan"]["closing"].advances == 0
    assert st.sections["avoir"]["unexplained"] == 0

    # Credit card refused as source account.
    _, errs = advance_transfer.create_advance_transfer(
        admin_account_id=c1,
        trust_account_id=tr1,
        dossier_id=dossier_id,
        client_id=client_id,
        amount=10000,
        date_value=_d(2031, 3, 6),
        method="virement",
    )
    assert errs and "opérations" in errs[0]

    # Standalone single-side reversals are blocked.
    _, errs = al.reverse_transaction(admin_leg["id"], "test")
    assert errs and "virement d'avances" in errs[0].lower()
    _, errs = trust_model.reverse_transaction(trust_leg["id"], "test")
    assert errs and "virement d'avances" in errs[0].lower()

    # Barreau art. 59 shortfall guard blocks reversal if client trust funds were spent.
    disb, errs = trust_model.create_transaction(
        {
            "account_id": tr1,
            "dossier_id": dossier_id,
            "client_id": client_id,
            "direction": "déboursé",
            "purpose": "remise_client",
            "amount": 20000,
            "date": _d(2031, 3, 6),
            "method": "virement",
            "counterparty": "Client Alpha",
        }
    )
    assert errs == []
    _, errs = trust_model.clear_transaction(disb["id"], _d(2031, 3, 6))
    assert errs == []
    _, errs = advance_transfer.reverse_advance_transfer(trust_leg["id"], "Erreur")
    assert errs and "fonds compensés" in errs[0]

    # Once trust funds are restored AND cleared, dual-leg reversal succeeds atomically.
    disb_rev, errs = trust_model.reverse_transaction(disb["id"], "Annulation débourse")
    assert errs == []
    _, errs = trust_model.clear_transaction(disb_rev["id"], _d(TODAY.year, TODAY.month, TODAY.day))
    assert errs == []
    rev_report = comptabilite.contrepasser_ecriture_fideicommis(trust_leg["id"], "Annulation avance")
    assert rev_report["ok"] is True and rev_report["errors"] == []
    assert rev_report["reversal"]["reverses_id"] == trust_leg["id"]
    assert len(rev_report["admin_reversals"]) == 1
    assert rev_report["admin_reversals"][0]["admin_transaction_id"] == admin_leg["id"]
    assert fake.peek(f"trust_accounts/{tr1}")["book_balance"] == 0
    a_prob, a_notes = verify_admin_integrity.collect()
    assert a_prob == [] and a_notes == []
    t_prob, t_notes = verify_trust_integrity.collect()
    assert t_prob == [] and t_notes == []


def test_advance_transfer_web_routes_and_dossier_client_search(fake):
    o1, _, _, _ = _seed_admin_accounts(fake)
    tr1, dossier_id, client_id = _seed_trust_and_dossier(fake)
    app = _web_app(admin_routes.admin_bp, trust_routes.trust_bp)
    client = app.test_client()

    assert client.get("/administration/virement-fideicommis").status_code == 200
    search = client.get("/administration/dossier-search?q=2031&clients=1")
    assert search.status_code == 200
    assert "data-clients=" in search.get_data(as_text=True)

    resp = client.post(
        "/administration/virement-fideicommis",
        data={
            "admin_account_id": o1,
            "trust_account_id": tr1,
            "dossier_id": dossier_id,
            "client_id": client_id,
            "amount": "500,00",
            "date": "2031-03-10",
            "method": "virement",
            "reference": "WEB-AV",
        },
    )
    assert resp.status_code == 302
    assert len(fake.peek_collection("admin_transactions")) == 1
    assert len(fake.peek_collection("trust_transactions")) == 1


def test_a_stale_web_reversal_of_an_advance_transfer_the_connector_reversed_is_refused(fake):
    o1, _, _, _ = _seed_admin_accounts(fake)
    tr1, dossier_id, client_id = _seed_trust_and_dossier(fake)
    created, errs = advance_transfer.create_advance_transfer(
        admin_account_id=o1,
        trust_account_id=tr1,
        dossier_id=dossier_id,
        client_id=client_id,
        amount=45000,
        date_value=_d(2031, 3, 10),
        method="virement",
    )
    assert errs == []
    trust_leg = created["trust_entry"]

    # Connector reverses the trust side (bridged reversal reverses both legs atomically).
    connector_res = handlers.reverse_register_entry({
        "register": "trust",
        "tx_id": trust_leg["id"],
        "reason": "Annulation par connecteur",
        "idempotency_key": "k-rev-adv-1",
    })
    assert connector_res["reversed"] is True
    assert connector_res["entity"]["reverses_id"] == trust_leg["id"]

    # Stale web tab from before the connector's reversal tries to reverse the same advance transfer.
    snapshot_trust = dict(fake.peek_collection("trust_transactions"))
    snapshot_admin = dict(fake.peek_collection("admin_transactions"))
    app = _web_app(admin_routes.admin_bp, trust_routes.trust_bp)
    client = app.test_client()
    resp = client.post(
        f"/fideicommis/{trust_leg['id']}/contrepasser",
        data={"expected_etag": trust_leg["etag"], "reason": "Onglet périmé"},
    )
    assert resp.status_code in (200, 400)
    assert "déjà été contre-passée" in resp.get_data(as_text=True)
    web = comptabilite.contrepasser_ecriture_fideicommis(
        trust_leg["id"], "Onglet périmé", expected_etag=trust_leg["etag"]
    )
    assert web["ok"] is False
    assert fake.peek_collection("trust_transactions") == snapshot_trust
    assert fake.peek_collection("admin_transactions") == snapshot_admin


# ══════════════════════════════════════════════════════════════════════
# 3. Cabinet Overdue Interest Rate Setting
# ══════════════════════════════════════════════════════════════════════


def test_cabinet_interest_rate_setting_validation_and_web_save(fake):
    assert settings_model.cabinet_interest_rate_bps() == 0

    _, errs = settings_model.update_cabinet(
        {"nom": "Me Exemple", "overdue_interest_rate": "12,50"}
    )
    assert errs == []
    assert settings_model.cabinet_interest_rate_bps() == 1250

    _, errs = settings_model.update_cabinet(
        {"nom": "Me Exemple", "overdue_interest_rate": "101"}
    )
    assert errs
    _, errs = settings_model.update_cabinet(
        {"nom": "Me Exemple", "overdue_interest_rate": "12.345"}
    )
    assert errs

    app = _web_app(settings_routes.settings_bp)
    client = app.test_client()
    assert client.get("/parametres/").status_code == 200


# ══════════════════════════════════════════════════════════════════════
# 4. Manual Overdue Interest Notices, .docx, Statements, Journal & Integrity
# ══════════════════════════════════════════════════════════════════════


def _seed_overdue_invoice(fake, dossier_id, client_id, *, amount_due=114975, subtotal=100000, gst=5000, qst=9975):
    inv_id = "inv-11111111-1111-4111-8111-111111111111"
    doc = {
        "id": inv_id,
        "invoice_number": "2031-F010",
        "status": "en_retard",
        "dossier_id": dossier_id,
        "dossier_file_number": "2031-001",
        "dossier_title": "Client A c. Défendeur B",
        "client_id": client_id,
        "client_name": "Client Alpha",
        "billing_address": {
            "name": "Client Alpha",
            "street": "100 rue Exemple",
            "unit": "",
            "city": "Montréal",
            "province": "Québec",
            "postal_code": "H2Y 1C6",
        },
        "date": _d(2031, 1, 1),
        "due_date": _d(2031, 2, 1),
        "subtotal_fees": subtotal,
        "subtotal_expenses": 0,
        "subtotal": subtotal,
        "gst_rate": 500,
        "gst_amount": gst,
        "qst_rate": 9975,
        "qst_amount": qst,
        "total": subtotal + gst + qst,
        "retainer_applied": 0,
        "amount_due": amount_due,
        "amount_paid": 0,
        "gst_number": "123456789 RT0001",
        "qst_number": "1234567890 TQ0001",
        "created_at": _d(2031, 1, 1),
        "updated_at": _d(2031, 1, 1),
        "etag": "etag-inv-0",
    }
    fake.seed(f"invoices/{inv_id}", doc)
    return inv_id


def _minimal_interest_docx() -> bytes:
    buf = io.BytesIO()
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    ct = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    body = (
        '<w:p><w:r><w:t>AVIS {{ai.</w:t></w:r><w:r><w:t>numero}}</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>{{ai.montant}} — {{ai.taux}} ({{ai.jours}} j)</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>Facture {{facture.numero}} solde {{facture.solde_restant}}</w:t></w:r></w:p>'
    )
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", ct)
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        zf.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {w}><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def test_overdue_interest_notices_lifecycle_imputation_tax_cap_docx_statements_and_journal(fake, monkeypatch):
    o1, _, _, _ = _seed_admin_accounts(fake)
    _tr1, dossier_id, client_id = _seed_trust_and_dossier(fake)
    inv_id = _seed_overdue_invoice(fake, dossier_id, client_id)

    # Without a configured cabinet rate, charging refuses before minting a counter.
    _, errs = invoice_model.charge_overdue_interest(
        inv_id, expected_etag=fake.peek(f"invoices/{inv_id}")["etag"]
    )
    assert errs and "Aucun taux" in errs[0]
    assert "avis-interets-2031" not in fake.peek_collection("counters")

    # Configure 12,00 % annual interest rate.
    _, errs = settings_model.update_cabinet(
        {"nom": "Me Exemple", "overdue_interest_rate": "12,00"}
    )
    assert errs == []

    # Future notice_date is refused.
    _, errs = invoice_model.charge_overdue_interest(
        inv_id,
        expected_etag=fake.peek(f"invoices/{inv_id}")["etag"],
        notice_date=date(2031, 3, 25),
    )
    assert errs and "futur" in errs[0]

    # Issue notice #1 for 2031-02-01 -> 2031-03-01 (28 days) at 12.00 % on 1149.75 $.
    expected_1 = invoice_model.compute_simple_interest(114975, 1200, 28)
    assert expected_1 == 1058
    rep1, errs = invoice_model.charge_overdue_interest(
        inv_id,
        expected_etag=fake.peek(f"invoices/{inv_id}")["etag"],
        notice_date=date(2031, 3, 1),
        reason="Avis mensuel",
    )
    assert errs == []
    ev1 = rep1["interest_charge"]
    assert ev1["interest_notice_number"] == "2031-AI001"
    assert ev1["amount"] == 1058
    assert invoice_model.balance_of(rep1["invoice"]) == 114975 + 1058

    # Voiding is refused while an interest notice stands.
    _, errs = invoice_model.void_invoice_report(
        inv_id, expected_etag=rep1["invoice"]["etag"]
    )
    assert errs and "avis d'intérêts" in errs[0].lower()

    # Partial payment of 2000 cents settles the 1058 interest FIRST + 942 of principal (art. 1571 C.c.Q.).
    enc_res = comptabilite.enregistrer_ecriture_administration(
        {
            "account_id": o1,
            "direction": "recette",
            "kind": "encaissement_facture",
            "invoice_id": inv_id,
            "amount": 2000,
            "date": _d(2031, 3, 5),
            "method": "virement",
            "counterparty": "Client Alpha",
            "dossier_id": dossier_id,
        }
    )
    assert enc_res["ok"] is True and enc_res["errors"] == []
    enc = enc_res["entry"]
    stored_inv = fake.peek(f"invoices/{inv_id}")
    assert invoice_model.principal_and_interest_paid(stored_inv) == (942, 1058)
    assert invoice_model.unpaid_principal_of(stored_inv) == 114975 - 942

    # Notice #2 starts from 2031-03-01 (period_end of #1) through 2031-03-15 (14 days) on unpaid principal 114033.
    expected_2 = invoice_model.compute_simple_interest(114033, 1200, 14)
    rep2, errs = invoice_model.charge_overdue_interest(
        inv_id,
        expected_etag=stored_inv["etag"],
        notice_date=date(2031, 3, 15),
    )
    assert errs == []
    ev2 = rep2["interest_charge"]
    assert ev2["interest_notice_number"] == "2031-AI002"
    assert ev2["period_start"] == _d(2031, 3, 1)
    assert ev2["amount"] == expected_2

    # Generating a .docx for standing notice #2 using an active avis_interets template.
    docx_bytes = _minimal_interest_docx()
    bucket = FakeBucket()
    monkeypatch.setattr(tpl.storage, "bucket", lambda: bucket)
    monkeypatch.setattr(doc_model.storage, "bucket", lambda: bucket)

    created_tpl, errs = tpl.create_template(
        io.BytesIO(docx_bytes),
        "avis_interets.docx",
        len(docx_bytes),
        {"name": "Avis d'intérêts", "category": "correspondance", "kind": "avis_interets"},
        UID,
    )
    assert errs == []
    _, errs = tpl.set_active_template(created_tpl["id"], par=UID, expected_etag=created_tpl["etag"])
    assert errs == []

    gen1 = avis_interets.generer_avis_interets(inv_id, ev2["id"], resolve_uid=lambda: UID)
    assert gen1.reused is False
    assert doc_model.INTEREST_NOTICE_TAG in gen1.document["tags"]
    gen2 = avis_interets.generer_avis_interets(inv_id, ev2["id"], resolve_uid=lambda: UID)
    assert gen2.reused is True and gen2.document["id"] == gen1.document["id"]
    with zipfile.ZipFile(io.BytesIO(bucket.objects[gen1.document["storage_path"]].data)) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    assert "2031-AI002" in xml
    assert "12" in xml

    # Cancel notice #2 cleanly.
    _, errs = invoice_model.reverse_interest_charge(
        inv_id, ev2["id"], expected_etag=fake.peek(f"invoices/{inv_id}")["etag"], reason="Remise commerciale"
    )
    assert errs == []
    stored_inv = fake.peek(f"invoices/{inv_id}")
    assert invoice_model.interest_charged_of(stored_inv) == 1058

    # Financial statements check: interest revenue + receivables + zero unexplained equity variance!
    snap = _fs_snapshot(
        accounts=[fake.peek(f"admin_accounts/{o1}")],
        transactions=[fake.peek(f"admin_transactions/{enc['id']}")],
        invoices=[stored_inv],
    )
    period_q1 = fs.periods.Period("trimestre", 2031, 1)
    st = fs.compute(snap, period_q1, today=TODAY, now=datetime(2031, 3, 20, 12, tzinfo=UTC), books_start=BOOKS)
    assert st.sections["resultats"]["interest_charges"] == 1058
    assert st.sections["resultats"]["revenue"] == 100000 + 1058
    assert st.sections["bilan"]["closing"].receivables == 114975 + 1058 - 2000
    assert st.sections["avoir"]["unexplained"] == 0
    presented = fs.present(st)
    resultats_keys = [r["key"] for s in presented["sections"] if s["key"] == "resultats" for r in s["rows"]]
    assert "interets_factures" in resultats_keys

    # Journal des radiations et intérêts (HTML, CSV, PDF) renders notice #1 and cancelled #2 cleanly.
    assert "n'existent pas encore" not in fs.JOURNAL_INTEREST_SENTENCE
    journal = fs.write_off_journal(snap, period_q1, today=TODAY)
    assert len(journal.interest_entries) == 3
    assert journal.interest_total == 1058

    app = _web_app(journal_routes.journal_radiations_bp, invoice_routes.invoices_bp)
    web = app.test_client()
    page = web.get("/comptabilite/radiations/?periode=2031-T1")
    assert page.status_code == 200
    html_text = page.get_data(as_text=True)
    assert "2031-AI001" in html_text
    assert "2031-AI002" in html_text
    assert "n'existent pas encore" not in html_text

    csv_resp = web.get("/comptabilite/radiations/export/csv?periode=2031-T1")
    assert csv_resp.status_code == 200
    csv_text = csv_resp.get_data(as_text=True).lstrip("\ufeff")
    assert "2031-AI001" in csv_text and "2031-AI002" in csv_text

    pdf_resp = web.get("/comptabilite/radiations/export/pdf?periode=2031-T1")
    assert pdf_resp.status_code == 200 and pdf_resp.get_data()[:4] == b"%PDF"

    # Invoice detail & interets GET form routes work cleanly.
    assert web.get(f"/factures/{inv_id}").status_code == 200
    assert web.get(f"/factures/{inv_id}/interets").status_code == 200

    # Charge Notice #3 first -> Mar 5's payment still extinguished 942 of principal and
    # unpaid principal remains 114033 under chronological art. 1571 C.c.Q. imputation.
    rep3, errs = invoice_model.charge_overdue_interest(
        inv_id, expected_etag=fake.peek(f"invoices/{inv_id}")["etag"], notice_date=date(2031, 3, 18)
    )
    assert errs == []
    ev3 = rep3["interest_charge"]
    stored_with_3 = fake.peek(f"invoices/{inv_id}")
    assert invoice_model.principal_and_interest_paid(stored_with_3, [enc]) == (942, 1058)
    assert invoice_model.unpaid_principal_of(stored_with_3, [enc]) == 114033
    # Notice #3 stands unpaid before write-off -> bad-debt TPS/TVQ caps strictly on unpaid principal!
    wo_rep, errs = invoice_model.write_off_remainder(
        inv_id, expected_etag=rep3["invoice"]["etag"], reason="Insolvable"
    )
    assert errs == []
    wo = wo_rep["write_off"]
    unpaid_principal = 114975 - 942
    expected_gst, expected_qst = invoice_model.write_off_tax_parts(fake.peek(f"invoices/{inv_id}"), unpaid_principal)
    assert wo["amount"] == unpaid_principal + ev3["amount"]
    assert wo["gst_amount"] == expected_gst
    assert wo["qst_amount"] == expected_qst
    assert wo_rep["invoice"]["status"] == "payée"

    # Cancel interest notice while standing radiation exists is refused cleanly.
    _, errs = invoice_model.reverse_interest_charge(
        inv_id, ev3["id"], expected_etag=wo_rep["invoice"]["etag"]
    )
    assert errs and "radiation" in errs[0].lower()

    # Admin integrity Checks 12 & 13 pass on clean data.
    _, errs = al.clear_transaction(enc["id"], _d(2031, 3, 5))
    assert errs == []
    a_prob, a_notes = verify_admin_integrity.collect()
    assert a_prob == [] and a_notes == []

    # Check 12 catches duplicate AAAA-AI### numbers across two invoices!
    inv2_id = "inv-22222222-2222-4222-8222-222222222222"
    fake.seed(
        f"invoices/{inv2_id}",
        {
            **fake.peek(f"invoices/{inv_id}"),
            "id": inv2_id,
            "invoice_number": "2031-F011",
            "amount_paid": 0,
            "write_offs": [],
            "interest_charges": [
                {
                    **ev1,
                    "id": "dup-ai-1",
                    "interest_notice_number": "2031-AI001",
                }
            ],
            "status": "en_retard",
        },
    )
    a_prob2, _ = verify_admin_integrity.collect()
    assert any("2031-AI001" in p for p in a_prob2)


def test_reverse_interest_charge_settles_paid_invoice_and_enforces_lifo(fake):
    """Regression tests for reviewer-discovered Lot C defects:
    1) Cancelling the standing interest notice when amount_paid == amount_due
       must transition the invoice to 'payée' (Invariant I4).
    2) An older standing interest notice cannot be cancelled out-of-order while
       a newer standing notice depends on it (LIFO).
    3) available_transitions excludes 'annulée' while an interest notice stands.
    4) _UNREADABLE_OPENINGS covers interest_charges and unreadable_transition_field
       catches corrupt interest_charges on 'payée' invoices.
    """
    o1, _, _, _ = _seed_admin_accounts(fake)
    _, dossier_id, client_id = _seed_trust_and_dossier(fake)
    inv_id = _seed_overdue_invoice(
        fake, dossier_id, client_id, amount_due=10000, subtotal=10000, gst=0, qst=0
    )
    _, errs = settings_model.update_cabinet(
        {"nom": "Me Exemple", "overdue_interest_rate": "12,00"}
    )
    assert errs == []

    # Charge AI1 on 2031-03-01 (92 cents).
    rep1, errs = invoice_model.charge_overdue_interest(
        inv_id, expected_etag=fake.peek(f"invoices/{inv_id}")["etag"], notice_date=date(2031, 3, 1)
    )
    assert errs == []
    ev1 = rep1["interest_charge"]
    assert "annulée" not in invoice_model.available_transitions(rep1["invoice"], today=TODAY)

    # Pay exact principal 10000 on 2031-03-05 -> balance = 92, status stays en_retard.
    pay = comptabilite.enregistrer_ecriture_administration(
        {
            "account_id": o1,
            "direction": "recette",
            "kind": "encaissement_facture",
            "invoice_id": inv_id,
            "amount": 10000,
            "date": _d(2031, 3, 5),
            "method": "virement",
            "counterparty": "Client Alpha",
            "dossier_id": dossier_id,
        }
    )
    assert pay["ok"] is True

    # Cancel AI1 -> balance reaches 0 and invoice settles to 'payée' (I4)!
    rev1, errs = invoice_model.reverse_interest_charge(
        inv_id, ev1["id"], expected_etag=fake.peek(f"invoices/{inv_id}")["etag"], reason="Remise"
    )
    assert errs == []
    assert invoice_model.balance_of(rev1["invoice"]) == 0
    assert rev1["invoice"]["status"] == "payée"
    _, errs = al.clear_transaction(pay["entry"]["id"], _d(2031, 3, 5))
    assert errs == []
    a_prob, a_notes = verify_admin_integrity.collect()
    assert a_prob == [] and a_notes == []

    # Second invoice with two stacked notices: AI1 then AI2 -> LIFO reversal enforced.
    inv2_id = "inv-33333333-3333-4333-8333-333333333333"
    fake.seed(
        f"invoices/{inv2_id}",
        {
            **fake.peek(f"invoices/{inv_id}"),
            "id": inv2_id,
            "invoice_number": "2031-F012",
            "amount_due": 100000,
            "amount_paid": 0,
            "paid_date": None,
            "write_offs": [],
            "interest_charges": [],
            "status": "en_retard",
            "etag": "etag-inv2-0",
        },
    )
    r_a1, errs = invoice_model.charge_overdue_interest(
        inv2_id, expected_etag="etag-inv2-0", notice_date=date(2031, 3, 1)
    )
    assert errs == []
    r_a2, errs = invoice_model.charge_overdue_interest(
        inv2_id, expected_etag=r_a1["invoice"]["etag"], notice_date=date(2031, 3, 15)
    )
    assert errs == []

    # Out-of-order reversal of AI1 is refused while AI2 stands.
    _, errs = invoice_model.reverse_interest_charge(
        inv2_id, r_a1["interest_charge"]["id"], expected_etag=r_a2["invoice"]["etag"]
    )
    assert errs and "plus récent" in errs[0]

    # Detail page hides cancel on AI1 and offers it on AI2; hides Annuler while standing.
    app = _web_app(invoice_routes.invoices_bp)
    web = app.test_client()
    detail_html = web.get(f"/factures/{inv2_id}").get_data(as_text=True)
    assert f"/factures/{inv2_id}/interets/{r_a2['interest_charge']['id']}/annulation" in detail_html
    assert f"/factures/{inv2_id}/interets/{r_a1['interest_charge']['id']}/annulation" not in detail_html
    assert f"/factures/{inv2_id}/void" not in detail_html

    # Unreadable openings & payée transition authority cover interest_charges.
    assert set(invoice_model._UNREADABLE_OPENINGS) == set(invoice_model._BALANCE_FIELDS)
    msg = invoice_model.unreadable_amount_message("interest_charges", "x")
    assert msg.startswith("Les avis d'intérêts enregistrés sur cette facture sont illisibles")
    corrupt_paid = {
        **fake.peek(f"invoices/{inv2_id}"),
        "status": "payée",
        "amount_paid": 100000,
        "interest_charges": [{"bad": 1}],
    }
    assert invoice_model.unreadable_transition_field(corrupt_paid) == "interest_charges"


def test_cross_month_transfer_reversals_and_interest_route_fail_closed(fake, monkeypatch):
    """Cross-month reversals of card payment, card->ops, card<->card, ops<->ops,
    and advance transfers keep unexplained == 0 and integrity clean; interest
    form route fails closed on receipt or cabinet read errors; INTEREST_GEOMETRY
    fits widest values and empty-radiation CSV export still renders interest rows.
    """
    from reportlab.pdfbase import pdfmetrics
    from utils import journal_radiations_pdf as jpdf

    o1, o2, c1, c2 = _seed_admin_accounts(fake)
    tr1, dossier_id, client_id = _seed_trust_and_dossier(fake)
    inv_id = _seed_overdue_invoice(fake, dossier_id, client_id)

    # March 31 transfers + April 2 reversals (clock at 2031-04-20).
    freeze_deadlines(monkeypatch, datetime(2031, 4, 20, 12, tzinfo=UTC))
    al.create_transaction(
        {
            "account_id": o1,
            "direction": "recette",
            "kind": "avance_honoraires",
            "amount": 40000,
            "date": _d(2031, 3, 1),
            "method": "virement",
            "counterparty": "Client Alpha",
            "dossier_id": dossier_id,
        }
    )
    l_pc, errs = al.create_card_payment(o1, c1, 50000, _d(2031, 3, 31), "virement")
    assert errs == []
    l_c2o, errs = al.create_account_transfer(c1, o2, 30000, _d(2031, 3, 31), "virement")
    assert errs == []
    l_c2c, errs = al.create_account_transfer(c1, c2, 20000, _d(2031, 3, 31), "virement")
    assert errs == []
    l_o2o, errs = al.create_account_transfer(o1, o2, 10000, _d(2031, 3, 31), "virement")
    assert errs == []
    adv_rep, errs = advance_transfer.create_advance_transfer(
        admin_account_id=o1,
        trust_account_id=tr1,
        dossier_id=dossier_id,
        client_id=client_id,
        amount=40000,
        date_value=_d(2031, 3, 31),
        method="virement",
    )
    assert errs == []

    for leg in (l_pc, l_c2o, l_c2c, l_o2o):
        _, errs = al.reverse_transaction(leg["id"], "Annulation avril", reversal_date=_d(2031, 4, 2))
        assert errs == []
    _, errs = advance_transfer.reverse_advance_transfer(adv_rep["trust_entry"]["id"], "Annul avance")
    assert errs == []

    snap = _fs_snapshot(
        accounts=list(fake.peek_collection("admin_accounts").values()),
        transactions=list(fake.peek_collection("admin_transactions").values()),
        trust_accounts=list(fake.peek_collection("trust_accounts").values()),
        trust_rows=list(fake.peek_collection("trust_transactions").values()),
    )
    for period in (fs.periods.Period("mois", 2031, 3), fs.periods.Period("mois", 2031, 4)):
        st = fs.compute(
            snap, period, today=date(2031, 4, 20),
            now=datetime(2031, 4, 20, 12, tzinfo=UTC), books_start=BOOKS,
        )
        assert st.sections["avoir"]["unexplained"] == 0
        assert st.sections["avoir"]["virements"] == 0
    assert fs.compute(
        snap, fs.periods.Period("mois", 2031, 4), today=date(2031, 4, 20),
        now=datetime(2031, 4, 20, 12, tzinfo=UTC), books_start=BOOKS,
    ).sections["bilan"]["closing"].advances == 40000

    a_prob, a_notes = verify_admin_integrity.collect()
    t_prob, t_notes = verify_trust_integrity.collect()
    assert a_prob == [] and a_notes == []
    assert t_prob == [] and t_notes == []

    # Interest form GET fails closed on receipt or cabinet read errors (never 500).
    app = _web_app(invoice_routes.invoices_bp, journal_routes.journal_radiations_bp)
    web = app.test_client()
    with mock.patch.object(invoice_routes, "list_invoice_receipts_strict", side_effect=RuntimeError("blip")):
        r_fail = web.get(f"/factures/{inv_id}/interets")
        assert r_fail.status_code == 302
        assert "n%27ont+pas+pu" in r_fail.location or "erreur=" in r_fail.location
    with mock.patch.object(settings_model, "cabinet_interest_rate_bps", side_effect=settings_model.CabinetIllisible()):
        r_cab = web.get(f"/factures/{inv_id}/interets")
        assert r_cab.status_code == 302 and "erreur=" in r_cab.location

    # INTEREST_GEOMETRY width budget fits widest values and clips client only.
    assert abs(sum(r for r, _ in jpdf.INTEREST_GEOMETRY.values()) - 1.0) < 1e-9
    assert [k for k, (_r, clip) in jpdf.INTEREST_GEOMETRY.items() if clip] == ["client"]
    assert set(jpdf.INTEREST_GEOMETRY) == {k for k, _l, _s in fs.INTEREST_JOURNAL_COLUMNS}
    widths = jpdf.widths(fs.INTEREST_JOURNAL_COLUMNS, geometry=jpdf.INTEREST_GEOMETRY)
    widest = {
        "date": "2031-03-20",
        "interest_notice_number": "2031-AI1000",
        "invoice_number": "2026-001-100",
        "client": "Client Exemple",
        "kind": "Contre-passation",
        "period": "2031-01-01 → 2031-12-31 (365 j)",
        "principal": 12_345_678_90,
        "rate": "100,00 %",
        "receivables": -12_345_678_90,
        "interest_revenue": -12_345_678_90,
        "state": "en vigueur",
    }
    for (k, label, _s), cell, w in zip(
        fs.INTEREST_JOURNAL_COLUMNS,
        jpdf.row_cells(widest, fs.INTEREST_JOURNAL_COLUMNS, widths, jpdf.INTEREST_GEOMETRY),
        widths,
    ):
        assert not cell.endswith("…")
        assert pdfmetrics.stringWidth(cell, jpdf._FONT, jpdf._SIZE) <= w - 2 * jpdf._PAD
        assert pdfmetrics.stringWidth(label, jpdf._FONT_BOLD, jpdf._SIZE) <= w - 2 * jpdf._PAD
    # Empty write-offs + standing interest notice still exports cleanly in CSV/PDF (neutralizing CSV formulas).
    _, errs = settings_model.update_cabinet({"nom": "Me Exemple", "overdue_interest_rate": "12,00"})
    assert errs == []
    inv_formula = {**fake.peek(f"invoices/{inv_id}"), "client_name": "=HYPERLINK(\"http://bad\")"}
    fake.seed(f"invoices/{inv_id}", inv_formula)
    _, errs = invoice_model.charge_overdue_interest(
        inv_id, expected_etag=inv_formula["etag"], notice_date=date(2031, 3, 15)
    )
    assert errs == []
    csv_only_ai = web.get("/comptabilite/radiations/export/csv?periode=2031-03")
    assert csv_only_ai.status_code == 200
    csv_body = csv_only_ai.get_data(as_text=True)
    assert "2031-AI001" in csv_body
    assert "'=HYPERLINK" in csv_body
    pdf_only_ai = web.get("/comptabilite/radiations/export/pdf?periode=2031-03")
    assert pdf_only_ai.status_code == 200 and pdf_only_ai.get_data()[:4] == b"%PDF"
