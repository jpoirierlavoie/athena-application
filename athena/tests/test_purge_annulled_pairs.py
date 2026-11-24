"""Tests for ``scripts/purge_annulled_pairs`` — synthetic 2031 bench on FakeFirestore.

All IDs, amounts, account numbers, dossier numbers, invoice numbers and
descriptions are fictional (the repository is public).
"""

import hashlib
import json
import os
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")
os.environ.setdefault("ATHENA_SKIP_DOTENV", "1")

with mock.patch("google.cloud.firestore.Client"):
    from mcp import handlers as mcp_handlers  # noqa: E402
    from models import admin_ledger as al  # noqa: E402
    from models import audit_event as audit_event_model  # noqa: E402
    from models import dossier as dossier_model  # noqa: E402
    from models import fee_payment as fee_payment_model  # noqa: E402
    from models import invoice as invoice_model  # noqa: E402
    from models import settings as settings_model  # noqa: E402
    from models import trust  # noqa: E402
    from scripts import findings_review  # noqa: E402
    from scripts import purge_annulled_pairs as pap  # noqa: E402
    from scripts import reclassify_admin_ledger as rla  # noqa: E402
    from scripts import reclassify_as_invoice_receipt as rx  # noqa: E402
    from scripts import verify_admin_integrity as vai  # noqa: E402
    from scripts import verify_trust_integrity as vti  # noqa: E402

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc

# Fictional IDs (never production UUIDs)
ACC_TRUST = "11111111-1111-4111-8111-000000000001"
ACC_WS = "11111111-1111-4111-8111-000000000002"
ACC_CARD = "11111111-1111-4111-8111-000000000003"
ACC_BN = "11111111-1111-4111-8111-000000000004"

DOS_014 = "22222222-2222-4222-8222-000000000014"
DOS_027 = "22222222-2222-4222-8222-000000000027"
DOS_034 = "22222222-2222-4222-8222-000000000034"

CLI_A = "33333333-3333-4333-8333-00000000000a"
CLI_B = "33333333-3333-4333-8333-00000000000b"
CLI_C = "33333333-3333-4333-8333-00000000000c"

INV_F063 = "44444444-4444-4444-8444-000000000063"
INV_F067 = "44444444-4444-4444-8444-000000000067"

# Deleted IDs (10 total)
P1_ORIG = "55555555-5555-4555-8555-000000000130"
P1_REV = "55555555-5555-4555-8555-000000000134"
P2_ORIG = "55555555-5555-4555-8555-000000000022"
P2_REV = "55555555-5555-4555-8555-000000000026"
P3_ORIG = "55555555-5555-4555-8555-000000000023"
P3_REV = "55555555-5555-4555-8555-000000000027"
P4_WS_ORIG = "55555555-5555-4555-8555-000000000029"
P4_CD_ORIG = "55555555-5555-4555-8555-000000000152"
P4_WS_REV = "55555555-5555-4555-8555-000000000032"
P4_CD_REV = "55555555-5555-4555-8555-000000000170"

# Kept / replacement IDs
T_124 = "66666666-6666-4666-8666-000000000124"
T_125 = "66666666-6666-4666-8666-000000000125"
T_131 = "66666666-6666-4666-8666-000000000131"
T_132 = "66666666-6666-4666-8666-000000000132"
T_133 = "66666666-6666-4666-8666-000000000133"

WS_2 = "77777777-7777-4777-8777-000000000002"
WS_33 = "77777777-7777-4777-8777-000000000033"
CD_171 = "77777777-7777-4777-8777-000000000171"
BN_1 = "77777777-7777-4777-8777-000000000901"

# Out-of-scope earlier reversal (must remain untouched & listed)
OOS_WS_10 = "88888888-8888-4888-8888-000000000010"
OOS_WS_11 = "88888888-8888-4888-8888-000000000011"

DESC_133_BEFORE = (
    "Réinscription à la carte-client du client B (remplace l'écriture 130); "
    "courriel reçu le 2031-11-16 à 14h00."
)
DESC_133_AFTER = "Courriel reçu le 2031-11-16 à 14h00."


def _d(day: str) -> datetime:
    y, m, d = (int(p) for p in day.split("-"))
    return datetime(y, m, d, tzinfo=UTC)


def _ts(day: str, hour: int = 12) -> datetime:
    y, m, d = (int(p) for p in day.split("-"))
    return datetime(y, m, d, hour, 0, tzinfo=UTC)


def _freeze_clock(monkeypatch, iso_day: str = "2031-11-23T15:00:00+00:00") -> None:
    from utils import deadlines as dl

    frozen = datetime.fromisoformat(iso_day)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(dl, "datetime", _Clock)


def _build_bench(monkeypatch):
    _freeze_clock(monkeypatch)
    fake = install(
        monkeypatch,
        pap,
        al,
        trust,
        invoice_model,
        audit_event_model,
        dossier_model,
        settings_model,
        fee_payment_model,
        rla,
        rx,
        vti,
        vai,
    )
    # Seed settings/cabinet for verify_trust_integrity D23 check
    fake.seed(
        "settings/cabinet",
        {"id": "cabinet", "nom": "Me Test Avocat", "organisation": "Cabinet Test"},
    )
    # Accounts
    fake.seed(
        f"trust_accounts/{ACC_TRUST}",
        {
            "id": ACC_TRUST,
            "name": "Fidéicommis général",
            "account_type": "général",
            "status": "actif",
            "book_balance": 350000,
            "bank_balance": 350000,
        },
    )
    fake.seed(
        f"admin_accounts/{ACC_WS}",
        {
            "id": ACC_WS,
            "name": "Wealthsimple",
            "account_type": "opérations",
            "status": "actif",
            "ledger_balance": -200000 + 80000,
        },
    )
    fake.seed(
        f"admin_accounts/{ACC_CARD}",
        {
            "id": ACC_CARD,
            "name": "Carte crédit",
            "account_type": "carte_crédit",
            "status": "actif",
            "ledger_balance": -50000 + 200000,
        },
    )
    fake.seed(
        f"admin_accounts/{ACC_BN}",
        {
            "id": ACC_BN,
            "name": "Banque Nationale",
            "account_type": "opérations",
            "status": "actif",
            "ledger_balance": 1000,
        },
    )
    # Counters (seqs 1..8 on trust for simplicity: 1..8)
    # We'll use realistic sequences matching the test plan: 1, 2, 3(=130), 4(=131), 5(=132), 6(=133), 7(=134)
    fake.seed(f"counters/trust-{ACC_TRUST}", {"seq": 7})
    fake.seed(f"counters/admin-{ACC_WS}", {"seq": 33})
    fake.seed(f"counters/admin-{ACC_CARD}", {"seq": 171})
    fake.seed(f"counters/admin-{ACC_BN}", {"seq": 1})

    # Dossiers (including one closed zero-balance trust dossier to mirror production)
    fake.seed(
        "dossiers/22222222-2222-4222-8222-000000000001",
        {
            "id": "22222222-2222-4222-8222-000000000001",
            "file_number": "2031-001",
            "title": "Dossier 001 fermé",
            "status": "fermé",
            "trust_balance": 0,
            "trust_balance_by_client": {CLI_A: 0},
            "trust_cleared_by_client": {CLI_A: 0},
        },
    )
    fake.seed(
        f"dossiers/{DOS_014}",
        {
            "id": DOS_014,
            "file_number": "2031-914",
            "title": "Dossier 914",
            "status": "actif",
            "trust_balance": 100000,
            "trust_balance_by_client": {CLI_A: 100000},
            "trust_cleared_by_client": {CLI_A: 100000},
        },
    )
    fake.seed(
        f"dossiers/{DOS_027}",
        {
            "id": DOS_027,
            "file_number": "2031-927",
            "title": "Dossier 927",
            "status": "actif",
            "trust_balance": 100000,
            "trust_balance_by_client": {CLI_A: 100000},
            "trust_cleared_by_client": {CLI_A: 100000},
        },
    )
    fake.seed(
        f"dossiers/{DOS_034}",
        {
            "id": DOS_034,
            "file_number": "2031-934",
            "title": "Dossier 934",
            "status": "actif",
            "trust_balance": 150000,
            "trust_balance_by_client": {CLI_C: 150000, CLI_B: 0},
            "trust_cleared_by_client": {CLI_C: 150000, CLI_B: 0},
        },
    )

    # Trust transactions (sequences 1..7)
    # seq 1 (T_125): +180000 on 2031-10-15 (CLI_A, DOS_014, compensée) -> bal 180000
    # seq 2 (T_124): -80000 fee payment on 2031-11-06 (CLI_A, DOS_014, compensée) -> bal 100000
    # seq 3 (P1_ORIG): +125000 on 2031-11-16 (CLI_B, DOS_034, annulée) -> bal 225000
    # seq 4 (T_131): +100000 on 2031-11-16 (CLI_A, DOS_027, compensée) -> bal 325000 (after purge: 200000)
    # seq 5 (T_132): +75000 on 2031-11-16 (CLI_C, DOS_034, compensée) -> bal 400000 (after purge: 275000)
    # seq 6 (T_133): +75000 on 2031-11-16 (CLI_C, DOS_034, compensée) -> bal 475000 (after purge: 350000)
    # seq 7 (P1_REV): -125000 on 2031-11-23 (CLI_B, DOS_034, annulée) -> bal 350000
    def _trust_doc(
        tid,
        seq,
        day,
        direction,
        amount,
        purpose,
        status,
        bal_acct,
        bal_cli,
        dos_id,
        dos_num,
        cli_id,
        desc="Écriture",
        cleared=None,
        rev_by=None,
        reverses=None,
        invoice_id=None,
    ):
        return {
            "id": tid,
            "account_id": ACC_TRUST,
            "sequence": seq,
            "date": _d(day),
            "direction": direction,
            "amount": amount,
            "purpose": purpose,
            "method": "virement",
            "counterparty": "Cabinet Test" if purpose == "virement_honoraires" else "Tiers",
            "dossier_id": dos_id,
            "dossier_file_number": dos_num,
            "client_id": cli_id,
            "client_name": "Client",
            "status": status,
            "cleared_date": _d(cleared) if cleared else None,
            "reconciliation_id": None,
            "balance_after_account": bal_acct,
            "balance_after_client": bal_cli,
            "description": desc,
            "invoice_id": invoice_id,
            "reverses_id": reverses,
            "reversed_by_id": rev_by,
            "related_transaction_id": None,
            "created_at": _ts(day, 10 + seq),
            "updated_at": _ts(day, 10 + seq),
            "created_via": "web",
            "updated_via": "web",
            "etag": f"etag-{tid}",
        }

    fake.seed(
        f"trust_transactions/{T_125}",
        _trust_doc(
            T_125, 1, "2031-10-15", "recette", 180000, "avance_honoraires",
            "compensée", 180000, 180000, DOS_014, "2031-914", CLI_A, cleared="2031-10-15",
        ),
    )
    fake.seed(
        f"trust_transactions/{T_124}",
        _trust_doc(
            T_124, 2, "2031-11-06", "déboursé", 80000, "virement_honoraires",
            "compensée", 100000, 100000, DOS_014, "2031-914", CLI_A,
            cleared="2031-11-06", invoice_id=INV_F063,
        ),
    )
    fake.seed(
        f"trust_transactions/{P1_ORIG}",
        _trust_doc(
            P1_ORIG, 3, "2031-11-16", "recette", 125000, "avance_honoraires",
            "annulée", 225000, 125000, DOS_034, "2031-934", CLI_B, rev_by=P1_REV,
        ),
    )
    fake.seed(
        f"trust_transactions/{T_131}",
        _trust_doc(
            T_131, 4, "2031-11-16", "recette", 100000, "avance_honoraires",
            "compensée", 325000, 100000, DOS_027, "2031-927", CLI_A, cleared="2031-11-16",
        ),
    )
    fake.seed(
        f"trust_transactions/{T_132}",
        _trust_doc(
            T_132, 5, "2031-11-16", "recette", 75000, "avance_honoraires",
            "compensée", 400000, 75000, DOS_034, "2031-934", CLI_C, cleared="2031-11-16",
        ),
    )
    fake.seed(
        f"trust_transactions/{T_133}",
        _trust_doc(
            T_133, 6, "2031-11-16", "recette", 75000, "avance_honoraires",
            "compensée", 475000, 150000, DOS_034, "2031-934", CLI_C,
            desc=DESC_133_BEFORE, cleared="2031-11-16",
        ),
    )
    fake.seed(
        f"trust_transactions/{P1_REV}",
        _trust_doc(
            P1_REV, 7, "2031-11-23", "déboursé", 125000, "correction",
            "annulée", 350000, 0, DOS_034, "2031-934", CLI_B, reverses=P1_ORIG,
        ),
    )

    # Reconciliations
    fake.seed(
        "trust_reconciliations/rec-trust-oct",
        {
            "id": "rec-trust-oct",
            "account_id": ACC_TRUST,
            "period_end": _d("2031-10-31"),
            "statement_balance": 180000,
            "book_balance": 180000,
            "outstanding_total": 0,
            "in_transit_total": 0,
            "variance": 0,
            "status": "complétée",
            "completed_date": _ts("2031-11-01", 10),
        },
    )
    fake.seed(
        "admin_reconciliations/rec-card-oct",
        {
            "id": "rec-card-oct",
            "account_id": ACC_CARD,
            "period_end": _d("2031-10-15"),
            "statement_balance": 50000,  # owed positive -> ledger -50000
            "book_balance": -50000,
            "outstanding_total": 0,
            "in_transit_total": 0,
            "variance": 0,
            "status": "complétée",
            "completed_date": _ts("2031-10-16", 10),
        },
    )

    # Invoices
    fake.seed(
        f"invoices/{INV_F063}",
        {
            "id": INV_F063,
            "invoice_number": "2031-F903",
            "dossier_id": DOS_014,
            "client_id": CLI_A,
            "date": _d("2031-11-01"),
            "due_date": _d("2031-12-01"),
            "status": "envoyée",
            "subtotal_fees": 108000,
            "subtotal_expenses": 0,
            "subtotal": 108000,
            "gst_amount": 5400,
            "qst_amount": 10773,
            "total": 124173,
            "retainer_applied": 0,
            "amount_due": 124173,
            "amount_paid": 80000,
            "paid_date": _d("2031-11-16"),  # residual from annulled P2
            "etag": "etag-inv-903",
        },
    )
    fake.seed(
        f"invoices/{INV_F063}/lineitems/li1",
        {"id": "li1", "type": "fee", "amount": 108000, "taxable": True},
    )
    fake.seed(
        f"invoices/{INV_F067}",
        {
            "id": INV_F067,
            "invoice_number": "2031-F907",
            "dossier_id": DOS_027,
            "client_id": CLI_A,
            "date": _d("2031-10-05"),
            "due_date": _d("2031-11-05"),
            "status": "payée",
            "subtotal_fees": 50000,
            "subtotal_expenses": 0,
            "subtotal": 50000,
            "gst_amount": 2500,
            "qst_amount": 4988,
            "total": 57488,
            "retainer_applied": 0,
            "amount_due": 57488,
            "amount_paid": 0,
            "paid_date": None,
            "etag": "etag-inv-907",
        },
    )

    # Admin transactions
    def _admin_doc(
        tid,
        aid,
        seq,
        day,
        kind,
        direction,
        amount,
        status,
        dos_id=None,
        dos_num="",
        inv_id=None,
        inv_num="",
        trust_id=None,
        rev_by=None,
        reverses=None,
        reverses_kind=None,
        related=None,
        cleared=None,
        cat=None,
        net=0,
    ):
        return {
            "id": tid,
            "account_id": aid,
            "sequence": seq,
            "date": _d(day),
            "kind": kind,
            "direction": direction,
            "amount": amount,
            "net_amount": net,
            "gst_amount": 0,
            "qst_amount": 0,
            "category": cat,
            "method": "virement",
            "counterparty": "Tiers",
            "description": "Opération",
            "status": status,
            "cleared_date": _d(cleared) if cleared else None,
            "dossier_id": dos_id,
            "dossier_file_number": dos_num,
            "invoice_id": inv_id,
            "invoice_number": inv_num,
            "trust_transaction_id": trust_id,
            "reverses_id": reverses,
            "reversed_by_id": rev_by,
            "reverses_kind": reverses_kind,
            "related_transaction_id": related,
            "revisions": [],
            "created_at": _ts(day, 9),
            "updated_at": _ts(day, 9),
            "created_via": "web",
            "updated_via": "web",
            "etag": f"etag-{tid}",
        }

    # Standing payment on F903
    fake.seed(
        f"admin_transactions/{WS_2}",
        _admin_doc(
            WS_2, ACC_WS, 2, "2031-11-06", "encaissement_facture", "recette",
            80000, "compensée", dos_id=DOS_014, dos_num="2031-914", inv_id=INV_F063,
            inv_num="2031-F903", trust_id=T_124, cleared="2031-11-06",
        ),
    )
    # Out-of-scope earlier reversal pair in WS (2031-10-10)
    fake.seed(
        f"admin_transactions/{OOS_WS_10}",
        _admin_doc(
            OOS_WS_10, ACC_WS, 10, "2031-10-10", "dépense", "déboursé",
            5000, "annulée", rev_by=OOS_WS_11, cat="fournitures", net=5000,
        ),
    )
    fake.seed(
        f"admin_transactions/{OOS_WS_11}",
        _admin_doc(
            OOS_WS_11, ACC_WS, 11, "2031-10-10", "correction", "recette",
            5000, "annulée", reverses=OOS_WS_10, reverses_kind="dépense",
            cat="fournitures", net=5000,
        ),
    )
    # P2 (WS 22 & 26)
    fake.seed(
        f"admin_transactions/{P2_ORIG}",
        _admin_doc(
            P2_ORIG, ACC_WS, 22, "2031-11-16", "encaissement_facture", "recette",
            44173, "annulée", dos_id=DOS_014, dos_num="2031-914", inv_id=INV_F063,
            inv_num="2031-F903", rev_by=P2_REV,
        ),
    )
    fake.seed(
        f"admin_transactions/{P2_REV}",
        _admin_doc(
            P2_REV, ACC_WS, 26, "2031-11-23", "correction", "déboursé",
            44173, "annulée", dos_id=DOS_014, dos_num="2031-914", reverses=P2_ORIG,
            reverses_kind="encaissement_facture",
        ),
    )
    # P3 (WS 23 & 27)
    fake.seed(
        f"admin_transactions/{P3_ORIG}",
        _admin_doc(
            P3_ORIG, ACC_WS, 23, "2031-11-16", "avance_honoraires", "recette",
            95000, "annulée", dos_id=DOS_027, dos_num="2031-927", rev_by=P3_REV,
        ),
    )
    fake.seed(
        f"admin_transactions/{P3_REV}",
        _admin_doc(
            P3_REV, ACC_WS, 27, "2031-11-23", "correction", "déboursé",
            95000, "annulée", dos_id=DOS_027, dos_num="2031-927", reverses=P3_ORIG,
            reverses_kind="avance_honoraires",
        ),
    )
    # P4 (4 docs: WS 29, CD 152, WS 32, CD 170)
    fake.seed(
        f"admin_transactions/{P4_WS_ORIG}",
        _admin_doc(
            P4_WS_ORIG, ACC_WS, 29, "2031-11-17", "paiement_carte", "déboursé",
            200000, "annulée", related=P4_CD_ORIG, rev_by=P4_WS_REV,
        ),
    )
    fake.seed(
        f"admin_transactions/{P4_CD_ORIG}",
        _admin_doc(
            P4_CD_ORIG, ACC_CARD, 152, "2031-11-17", "paiement_carte", "recette",
            200000, "annulée", related=P4_WS_ORIG, rev_by=P4_CD_REV,
        ),
    )
    fake.seed(
        f"admin_transactions/{P4_WS_REV}",
        _admin_doc(
            P4_WS_REV, ACC_WS, 32, "2031-11-23", "correction", "recette",
            200000, "annulée", reverses=P4_WS_ORIG, reverses_kind="paiement_carte",
        ),
    )
    fake.seed(
        f"admin_transactions/{P4_CD_REV}",
        _admin_doc(
            P4_CD_REV, ACC_CARD, 170, "2031-11-23", "correction", "déboursé",
            200000, "annulée", reverses=P4_CD_ORIG, reverses_kind="paiement_carte",
        ),
    )
    # Replacement card payment (WS 33 + CD 171)
    fake.seed(
        f"admin_transactions/{WS_33}",
        _admin_doc(
            WS_33, ACC_WS, 33, "2031-11-16", "paiement_carte", "déboursé",
            200000, "compensée", related=CD_171, cleared="2031-11-16",
        ),
    )
    fake.seed(
        f"admin_transactions/{CD_171}",
        _admin_doc(
            CD_171, ACC_CARD, 171, "2031-11-16", "paiement_carte", "recette",
            200000, "compensée", related=WS_33, cleared="2031-11-16",
        ),
    )
    # Card initial charge (-50000 before 2031-10-15)
    fake.seed(
        "admin_transactions/card-init-1",
        _admin_doc(
            "card-init-1", ACC_CARD, 1, "2031-10-01", "dépense", "déboursé",
            50000, "compensée", cleared="2031-10-15", cat="fournitures", net=50000,
        ),
    )
    # Banque Nationale (untouchable, balance 1000)
    fake.seed(
        f"admin_transactions/{BN_1}",
        _admin_doc(
            BN_1, ACC_BN, 1, "2031-10-01", "apport", "recette",
            1000, "compensée", cleared="2031-10-01",
        ),
    )

    # Replay journal entries (named key with tool prefix, expired key, and scanned ID key)
    k1 = "reverse_register_entry-2031-934-20311123-synth-4"
    doc_k1 = hashlib.sha256(f"reverse_register_entry:{k1}".encode("utf-8")).hexdigest()
    fake.seed(
        f"mcp_idempotency/{doc_k1}",
        {
            "tool": "reverse_register_entry",
            "status": "committed",
            "expire_at": _ts("2031-11-24", 15),
            "result": {"id": P1_REV},
        },
    )
    doc_k2 = hashlib.sha256("record_admin_entry:general-20311123-synth-9".encode("utf-8")).hexdigest()
    fake.seed(
        f"mcp_idempotency/{doc_k2}",
        {
            "tool": "record_admin_entry",
            "status": "committed",
            "expire_at": _ts("2031-11-22", 15),  # expired
            "result": {"id": P4_WS_ORIG},
        },
    )
    fake.seed(
        "mcp_idempotency/extra-scan-hit",
        {
            "tool": "clear_register_entries",
            "status": "committed",
            "expire_at": _ts("2031-11-24", 15),
            "result": {"ids": [P2_ORIG]},
        },
    )
    return fake


def _plan_dict() -> dict:
    return {
        "motif": "Suppression d'une paire annulée — vérification des journaux du 2031-11-23",
        "date_reference": "2031-11-23",
        "periode_etats": {"du": "2031-11-01", "au": "2031-11-30"},
        "comptes": {
            "fideicommis": {
                "id": ACC_TRUST,
                "registre": "fideicommis",
                "nom_court": "Fidéicommis",
                "solde_attendu_cents": 350000,
                "derniere_conciliation_attendue": "2031-10-31",
                "delta_nombre_ecritures": -2,
            },
            "wealthsimple": {
                "id": ACC_WS,
                "registre": "administration",
                "nom_court": "Wealthsimple",
                "solde_attendu_cents": -120000,
                "delta_nombre_ecritures": -6,
            },
            "carte": {
                "id": ACC_CARD,
                "registre": "administration",
                "nom_court": "Carte de crédit",
                "solde_attendu_cents": -150000,
                "plancher_attendu": "2031-10-15",
                "delta_nombre_ecritures": -2,
            },
            "banque_nationale": {
                "id": ACC_BN,
                "registre": "administration",
                "nom_court": "Banque Nationale",
                "solde_attendu_cents": 1000,
                "delta_nombre_ecritures": 0,
                "intouchable": True,
            },
        },
        "paires": [
            {
                "code": "P1",
                "registre": "fideicommis",
                "documents": [
                    {
                        "role": "Original",
                        "id": P1_ORIG,
                        "account_id": ACC_TRUST,
                        "sequence": 3,
                        "date": "2031-11-16",
                        "nature": "avance_honoraires",
                        "direction": "recette",
                        "amount": 125000,
                        "status": "annulée",
                        "reverses_id": None,
                        "reversed_by_id": P1_REV,
                        "related_transaction_id": None,
                        "dossier_file_number": "2031-934",
                        "client_id": CLI_B,
                    },
                    {
                        "role": "Contre-passation",
                        "id": P1_REV,
                        "account_id": ACC_TRUST,
                        "sequence": 7,
                        "date": "2031-11-23",
                        "nature": "correction",
                        "direction": "déboursé",
                        "amount": 125000,
                        "status": "annulée",
                        "reverses_id": P1_ORIG,
                        "reversed_by_id": None,
                        "related_transaction_id": None,
                        "dossier_file_number": "2031-934",
                        "client_id": CLI_B,
                    },
                ],
                "corrections_derivees": {
                    "recalcul_soldes_courants": True,
                    "attendus_soldes": {
                        T_131: {
                            "sequence": 4,
                            "balance_after_account_avant": 325000,
                            "balance_after_account_apres": 200000,
                            "balance_after_client": 100000,
                        },
                        T_132: {
                            "sequence": 5,
                            "balance_after_account_avant": 400000,
                            "balance_after_account_apres": 275000,
                            "balance_after_client": 75000,
                        },
                        T_133: {
                            "sequence": 6,
                            "balance_after_account_avant": 475000,
                            "balance_after_account_apres": 350000,
                            "balance_after_client": 150000,
                        },
                    },
                    "description_ecriture": {
                        "id": T_133,
                        "sequence": 6,
                        "retirer_debut": "Réinscription à la carte-client",
                        "retirer_fin_incluse": "; ",
                        "majuscule_mot": "courriel",
                    },
                },
            },
            {
                "code": "P2",
                "registre": "administration",
                "documents": [
                    {
                        "role": "Original",
                        "id": P2_ORIG,
                        "account_id": ACC_WS,
                        "sequence": 22,
                        "date": "2031-11-16",
                        "nature": "encaissement_facture",
                        "direction": "recette",
                        "amount": 44173,
                        "status": "annulée",
                        "reverses_id": None,
                        "reversed_by_id": P2_REV,
                        "related_transaction_id": None,
                        "invoice_id": INV_F063,
                        "invoice_number": "2031-F903",
                    },
                    {
                        "role": "Contre-passation",
                        "id": P2_REV,
                        "account_id": ACC_WS,
                        "sequence": 26,
                        "date": "2031-11-23",
                        "nature": "correction",
                        "direction": "déboursé",
                        "amount": 44173,
                        "status": "annulée",
                        "reverses_id": P2_ORIG,
                        "reversed_by_id": None,
                        "related_transaction_id": None,
                    },
                ],
                "corrections_derivees": {
                    "facture": {
                        "id": INV_F063,
                        "numero": "2031-F903",
                        "attendu_avant": {
                            "status": "envoyée",
                            "amount_paid": 80000,
                            "balance": 44173,
                            "payment_basis": "recorded",
                            "paid_date": "2031-11-16",
                        },
                        "attendu_apres": {
                            "status": "envoyée",
                            "amount_paid": 80000,
                            "balance": 44173,
                            "payment_basis": "recorded",
                            "paid_date": "2031-11-06",
                        },
                        "paiement_restant_attendu": {
                            "trust_id": T_124,
                            "trust_seq": 2,
                            "admin_id": WS_2,
                            "admin_seq": 2,
                        },
                    }
                },
            },
            {
                "code": "P3",
                "registre": "administration",
                "documents": [
                    {
                        "role": "Original",
                        "id": P3_ORIG,
                        "account_id": ACC_WS,
                        "sequence": 23,
                        "date": "2031-11-16",
                        "nature": "avance_honoraires",
                        "direction": "recette",
                        "amount": 95000,
                        "status": "annulée",
                        "reverses_id": None,
                        "reversed_by_id": P3_REV,
                        "related_transaction_id": None,
                        "dossier_file_number": "2031-927",
                    },
                    {
                        "role": "Contre-passation",
                        "id": P3_REV,
                        "account_id": ACC_WS,
                        "sequence": 27,
                        "date": "2031-11-23",
                        "nature": "correction",
                        "direction": "déboursé",
                        "amount": 95000,
                        "status": "annulée",
                        "reverses_id": P3_ORIG,
                        "reversed_by_id": None,
                        "related_transaction_id": None,
                    },
                ],
            },
            {
                "code": "P4",
                "registre": "administration",
                "documents": [
                    {
                        "role": "Original, volet Wealthsimple",
                        "id": P4_WS_ORIG,
                        "account_id": ACC_WS,
                        "sequence": 29,
                        "date": "2031-11-17",
                        "nature": "paiement_carte",
                        "direction": "déboursé",
                        "amount": 200000,
                        "status": "annulée",
                        "reverses_id": None,
                        "reversed_by_id": P4_WS_REV,
                        "related_transaction_id": P4_CD_ORIG,
                    },
                    {
                        "role": "Original, volet carte",
                        "id": P4_CD_ORIG,
                        "account_id": ACC_CARD,
                        "sequence": 152,
                        "date": "2031-11-17",
                        "nature": "paiement_carte",
                        "direction": "recette",
                        "amount": 200000,
                        "status": "annulée",
                        "reverses_id": None,
                        "reversed_by_id": P4_CD_REV,
                        "related_transaction_id": P4_WS_ORIG,
                    },
                    {
                        "role": "Contre-passation, volet Wealthsimple",
                        "id": P4_WS_REV,
                        "account_id": ACC_WS,
                        "sequence": 32,
                        "date": "2031-11-23",
                        "nature": "correction",
                        "direction": "recette",
                        "amount": 200000,
                        "status": "annulée",
                        "reverses_id": P4_WS_ORIG,
                        "reversed_by_id": None,
                        "related_transaction_id": None,
                    },
                    {
                        "role": "Contre-passation, volet carte",
                        "id": P4_CD_REV,
                        "account_id": ACC_CARD,
                        "sequence": 170,
                        "date": "2031-11-23",
                        "nature": "correction",
                        "direction": "déboursé",
                        "amount": 200000,
                        "status": "annulée",
                        "reverses_id": P4_CD_ORIG,
                        "reversed_by_id": None,
                        "related_transaction_id": None,
                    },
                ],
            },
        ],
        "ecritures_remplacement": [
            {
                "registre": "fideicommis",
                "id": T_133,
                "sequence": 6,
                "date": "2031-11-16",
                "amount": 75000,
                "client_id": CLI_C,
                "cleared_date": "2031-11-16",
            },
            {
                "registre": "fideicommis",
                "id": T_132,
                "sequence": 5,
                "date": "2031-11-16",
                "amount": 75000,
                "cleared_date": "2031-11-16",
            },
            {
                "registre": "administration",
                "id": WS_33,
                "sequence": 33,
                "date": "2031-11-16",
                "amount": 200000,
                "related_transaction_id": CD_171,
            },
        ],
        "controles": {
            "fideicommis_by_dossier": {
                "2031-914": 100000,
                "2031-927": 100000,
                "2031-934": 150000,
            },
            "carte_client_vide": {
                "dossier_file_number": "2031-934",
                "client_id": CLI_B,
            },
            "factures_intouchables": ["2031-F907"],
        },
        "cles_idempotence": [
            "reverse_register_entry-2031-934-20311123-synth-4",
            "record_admin_entry-general-20311123-synth-9",
        ],
    }


def _write_plan(tmp_path: pathlib.Path, plan: dict) -> pathlib.Path:
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def test_refuses_paths_inside_repo(monkeypatch, tmp_path):
    _build_bench(monkeypatch)
    plan_in_repo = _ATHENA / "tmp_test_plan_inside.json"
    report_out = tmp_path / "r.md"
    code = pap.main([str(plan_in_repo), "--rapport", str(report_out)])
    assert code == 1
    assert not report_out.exists()


def test_dry_run_writes_nothing_and_renders_all_sections(monkeypatch, tmp_path):
    fake = _build_bench(monkeypatch)
    plan_path = _write_plan(tmp_path, _plan_dict())
    report = tmp_path / "rapport-a-blanc.md"

    fake.reset_logs()
    code = pap.main([str(plan_path), "--rapport", str(report)])
    assert code == 0
    assert fake.commits == []
    text = report.read_text(encoding="utf-8")
    assert "Mode : **à blanc**" in text
    assert DESC_133_BEFORE in text
    assert DESC_133_AFTER in text
    assert "2031-11-06" in text
    assert "extra-scan-hit" in text
    assert OOS_WS_10 in text
    assert "register_pair_purge" in text


def test_mismatch_refuses_only_affected_pair_without_modifying_plan(monkeypatch, tmp_path):
    _build_bench(monkeypatch)
    plan = _plan_dict()
    plan["paires"][1]["documents"][0]["amount"] = 99999  # P2 mismatch only
    plan_path = _write_plan(tmp_path, plan)
    raw_before = plan_path.read_text(encoding="utf-8")
    report = tmp_path / "rapport-ecart.md"

    code = pap.main([str(plan_path), "--rapport", str(report)])
    assert code == 1
    assert plan_path.read_text(encoding="utf-8") == raw_before
    text = report.read_text(encoding="utf-8")
    assert "Paire P1 (fideicommis) — « à appliquer »" in text
    assert "Paire P2 (administration) — « refusée »" in text
    assert "Paire P3 (administration) — « à appliquer »" in text
    assert "Paire P4 (administration) — « à appliquer »" in text


def test_apply_executes_atomically_passes_integrity_and_idempotent_rerun(
    monkeypatch, tmp_path
):
    fake = _build_bench(monkeypatch)
    plan_path = _write_plan(tmp_path, _plan_dict())
    dry_report = tmp_path / "dry.md"
    assert pap.main([str(plan_path), "--rapport", str(dry_report)]) == 0
    fp = pap.prepare(plan_path).fingerprint

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    apply_report = tmp_path / "apply.md"
    fake.reset_logs()
    assert (
        pap.main(
            [
                str(plan_path),
                "--rapport",
                str(apply_report),
                "--sauvegarde",
                str(backup_dir),
                "--appliquer",
                fp,
            ]
        )
        == 0
    )

    # All 10 deleted docs are gone
    for did in (P1_ORIG, P1_REV):
        assert fake.peek(f"trust_transactions/{did}") is None
    for did in (P2_ORIG, P2_REV, P3_ORIG, P3_REV, P4_WS_ORIG, P4_CD_ORIG, P4_WS_REV, P4_CD_REV):
        assert fake.peek(f"admin_transactions/{did}") is None

    # P4 deleted all 4 docs + created 1 audit event in ONE single transaction commit
    p4_commit = [
        c
        for c in fake.commits
        if ("delete", f"admin_transactions/{P4_WS_ORIG}") in c.ops
    ]
    assert len(p4_commit) == 1
    p4_ops = dict((path, op) for op, path in p4_commit[0].ops)
    for did in (P4_WS_ORIG, P4_CD_ORIG, P4_WS_REV, P4_CD_REV):
        assert p4_ops[f"admin_transactions/{did}"] == "delete"
    assert any(
        op == "create" and path.startswith("audit_events/")
        for op, path in p4_commit[0].ops
    )

    # P1 derived updates: balances 131-133, description 133, script revisions
    assert fake.peek(f"trust_transactions/{T_131}")["balance_after_account"] == 200000
    assert fake.peek(f"trust_transactions/{T_132}")["balance_after_account"] == 275000
    t133 = fake.peek(f"trust_transactions/{T_133}")
    assert t133["balance_after_account"] == 350000
    assert t133["description"] == DESC_133_AFTER
    assert t133["updated_via"] == "script"
    assert t133["revisions"][-1]["via"] == "script"
    assert t133["revisions"][-1]["updated_at_before"] is not None

    # Dossier 2031-034 left untouched (empty client B card still present at 0)
    dos34 = fake.peek(f"dossiers/{DOS_034}")
    assert dos34["trust_balance_by_client"][CLI_B] == 0

    # Invoice 2031-F903 derived paid_date == 2031-11-06
    inv63 = fake.peek(f"invoices/{INV_F063}")
    assert inv63["paid_date"] == _d("2031-11-06")
    assert inv63["amount_paid"] == 80000
    assert inv63["status"] == "envoyée"
    assert inv63["updated_via"] == "script"

    # Counters untouched (no renumbering)
    assert fake.peek(f"counters/trust-{ACC_TRUST}")["seq"] == 7
    assert fake.peek(f"counters/admin-{ACC_WS}")["seq"] == 33
    assert fake.peek(f"counters/admin-{ACC_CARD}")["seq"] == 171

    # Audit events: 4 entries created, hidden from MCP list_deletions
    audits = fake.peek_collection("audit_events")
    assert len(audits) == 4
    assert all(ev["entity_type"] == "register_pair_purge" for ev in audits.values())
    assert all(ev["migration"]["provenance"] == "script" for ev in audits.values())
    mcp_res = mcp_handlers.list_deletions({"limit": 50})
    assert mcp_res["items"] == []

    # Idempotency entries deleted
    assert fake.peek_collection("mcp_idempotency") == {}

    # Out-of-scope reversal preserved
    assert fake.peek(f"admin_transactions/{OOS_WS_10}") is not None
    assert fake.peek(f"admin_transactions/{OOS_WS_11}") is not None

    # Integrity scripts after migration
    admin_problems, _ = vai.collect()
    assert admin_problems == []
    trust_problems, _ = vti.collect()
    assert len(trust_problems) == 1
    assert isinstance(trust_problems[0], findings_review.Reviewable)
    assert "3, 7" in trust_problems[0]

    # Generated --revue snippet clears verify_trust_integrity exit code to 0
    rev_file = tmp_path / "revue.json"
    snippet = pap._trust_gap_review_snippet(
        pap.prepare(plan_path).before, datetime(2031, 11, 23, tzinfo=UTC).date()
    )
    rev_file.write_text(json.dumps([snippet]), encoding="utf-8")
    assert vti.main(["--revue", str(rev_file)]) == 0

    # Idempotent second run writes nothing
    rerun_report = tmp_path / "rerun.md"
    fake.reset_logs()
    assert pap.main([str(plan_path), "--rapport", str(rerun_report)]) == 0
    assert fake.commits == []
    assert "« déjà appliquée »" in rerun_report.read_text(encoding="utf-8")


def test_restore_recovers_deleted_and_modified_docs(monkeypatch, tmp_path):
    fake = _build_bench(monkeypatch)
    plan_path = _write_plan(tmp_path, _plan_dict())
    fp = pap.prepare(plan_path).fingerprint
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    assert (
        pap.main(
            [
                str(plan_path),
                "--rapport",
                str(tmp_path / "apply.md"),
                "--sauvegarde",
                str(backup_dir),
                "--appliquer",
                fp,
            ]
        )
        == 0
    )
    backup_file = next(backup_dir.glob("purge-paires-annulees-*.json"))
    rfp = pap.prepare_restore(backup_file).fingerprint
    restore_report = tmp_path / "restore.md"
    assert (
        pap.main(
            [
                "--restaurer",
                str(backup_file),
                "--rapport",
                str(restore_report),
                "--appliquer",
                rfp,
            ]
        )
        == 0
    )
    assert fake.peek(f"trust_transactions/{P1_ORIG}") is not None
    assert fake.peek(f"trust_transactions/{T_133}")["balance_after_account"] == 475000
    assert fake.peek(f"trust_transactions/{T_133}")["description"] == DESC_133_BEFORE
    assert fake.peek(f"invoices/{INV_F063}")["paid_date"] == _d("2031-11-16")
    assert fake.peek(f"admin_transactions/{P4_CD_REV}") is not None


def test_p2_refuses_invoice_carrying_write_off(monkeypatch, tmp_path):
    fake = _build_bench(monkeypatch)
    inv = fake.peek(f"invoices/{INV_F063}")
    inv["write_offs"] = [
        {
            "id": "wo-1",
            "kind": "radiation",
            "date": _d("2031-11-20"),
            "amount": 44173,
            "gst_amount": 0,
            "qst_amount": 0,
            "tax_claimed": False,
            "reason": "Test",
            "reverses_id": "",
            "credit_note_number": "2031-NC001",
            "created_at": _ts("2031-11-20"),
            "created_via": "web",
        }
    ]
    fake.seed(f"invoices/{INV_F063}", inv)
    plan_path = _write_plan(tmp_path, _plan_dict())
    report = tmp_path / "wo.md"
    assert pap.main([str(plan_path), "--rapport", str(report)]) == 1
    assert "Paire P2 (administration) — « refusée »" in report.read_text(encoding="utf-8")
