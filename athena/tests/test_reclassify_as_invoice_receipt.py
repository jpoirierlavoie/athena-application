"""``scripts/reclassify_as_invoice_receipt`` — one administration entry turned
into the receipt of an invoice, written OUTSIDE the models.

The bench is built by the REAL models on the shared fake Firestore: an
operations account whose first quarter is reconciled, a client's invoice
paid twice through the ledger, a payment of the same client booked as the
lawyer's « apport » inside the locked quarter, a receipt on another invoice,
a card and a trust register. A plan in the lawyer's format (synthetic
amounts, names and ids only — the repository is public) names the change.
These tests pin what the script writes — what the application writes for an
encaissement, and nothing else — what it refuses and why, that a refusal or
a dry run writes nothing, that a second run finds it applied, the controls,
the fingerprint, the backup and the round trip of the restoration.
"""

import copy
import hashlib
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone
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
    from models import invoice as invoice_model
    from models import trust
    from scripts import reclassify_admin_ledger as rla
    from scripts import reclassify_as_invoice_receipt as rx
    from scripts import rectify_registers as rr
    from scripts import verify_admin_integrity as vai
    from scripts import verify_trust_integrity as vti

from tests._accounting_history import legacy_fee_entry  # noqa: E402
from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
TODAY = "2031-06-20T16:00:00+00:00"
DOSSIER_ID = "5c1f0e2a-3333-4333-8333-000000000001"
CLIENT_ID = "5c1f0e2a-3333-4333-8333-0000000000c1"
INVOICE_ID = "5c1f0e2a-3333-4333-8333-0000000000a1"
OTHER_INVOICE_ID = "5c1f0e2a-3333-4333-8333-0000000000a2"
TRUST_ID = "5c1f0e2a-3333-4333-8333-0000000000f1"
FILE_NUMBER = "2031-014"
TITLE = "Exemple inc. c. Démonstration et al."
CLIENT = "M. Client Exemple"
OWNER = "Me Avocat Exemple"
MOTIF = ("Paiement partiel du client sur la facture F-3101, inscrit par erreur comme "
         "apport (correction du 2031-06-20)")
DESCRIPTION = "Paiement partiel de la facture F-3101 (virement du 9 mars 2031)."
APPORT = 4321
FIRST = 30017
DUE = 51394
SETTLING_LAST = DUE - FIRST - APPORT
PARTIAL_LAST = 11056


def _d(m: int, d: int, y: int = 2031) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def _freeze_today(set_attr, iso: str) -> None:
    from utils import deadlines as dl

    frozen = datetime.fromisoformat(iso)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    set_attr(dl, "datetime", _Clock)


def _ok(result):
    value, errors = result
    assert errors == [], errors
    return value


def _set(fake, path: str, **fields) -> None:
    fake.external_write(path, {**fake.peek(path), **fields})


# ── the bench ─────────────────────────────────────────────────────────────


def _entry(account: dict, kind: str, amount: int, day: datetime, *, counterparty: str,
           cleared=None, **extra) -> dict:
    data = {"account_id": account["id"], "kind": kind, "amount": amount,
            "method": "virement", "counterparty": counterparty, "date": day,
            "description": "", "reference": "", "supplier_invoice_ref": "", **extra}
    return _ok(al.create_transaction(data, cleared_date=cleared))


def _invoice_doc(invoice_id: str, number: str, status: str, due: int, **extra) -> dict:
    return {
        "id": invoice_id, "invoice_number": number, "status": status,
        "date": _d(12, 5, 2030), "due_date": _d(1, 4),
        "dossier_id": DOSSIER_ID, "dossier_file_number": FILE_NUMBER, "dossier_title": TITLE,
        "client_id": CLIENT_ID, "client_name": CLIENT,
        "subtotal_fees": 41000, "subtotal_expenses": 3700, "subtotal": 44700,
        "gst_amount": 2235, "qst_amount": 4459, "total": due, "retainer_applied": 0,
        "amount_due": due, "amount_paid": 0, "paid_date": None, "notes": "",
        "etag": f"etag-{number}", **extra,
    }


def build_bench(fake, set_attr, *, settles: bool = True) -> dict:
    _freeze_today(set_attr, TODAY)
    fake.seed(f"trust_accounts/{TRUST_ID}", {
        "id": TRUST_ID, "name": "Général", "status": "actif", "account_type": "général",
        "book_balance": 0, "bank_balance": 0, "etag": "t0",
    })
    fake.seed(f"dossiers/{DOSSIER_ID}", {
        "id": DOSSIER_ID, "file_number": FILE_NUMBER, "title": TITLE,
        "client_ids": [CLIENT_ID], "clients": [{"id": CLIENT_ID, "name": CLIENT}],
        "trust_balance": 0, "trust_balance_by_client": {}, "trust_cleared_by_client": {},
    })
    ops = _ok(al.create_account({"name": "Opérations", "account_type": "opérations"}))
    card = _ok(al.create_account({"name": "Carte Exemple", "account_type": "carte_crédit"}))
    fake.seed(f"invoices/{INVOICE_ID}", _invoice_doc(INVOICE_ID, "F-3101", "en_retard", DUE))
    fake.seed(f"invoices/{INVOICE_ID}/lineitems/l1", {
        "id": "l1", "description": "Honoraires", "amount": 41000})
    fake.seed(f"invoices/{OTHER_INVOICE_ID}",
              _invoice_doc(OTHER_INVOICE_ID, "F-3102", "envoyée", 26400))
    e: dict = {}
    e["first"] = _entry(ops, "encaissement_facture", FIRST, _d(12, 20, 2030), counterparty=CLIENT,
                        invoice_id=INVOICE_ID, cleared=_d(12, 20, 2030))
    e["expense"] = _entry(ops, "dépense", 7013, _d(2, 3), counterparty="Fournisseur Exemple",
                          category="fournitures", net_amount=6100, gst_amount=305,
                          qst_amount=608, cleared=_d(2, 4))
    e["apport"] = _entry(ops, "apport", APPORT, _d(3, 9), counterparty=OWNER, cleared=_d(3, 9))
    e["other"] = _entry(ops, "recette_autre", 1730, _d(3, 15), counterparty="Banque Exemple",
                        cleared=_d(3, 16))
    rec = _ok(al.create_reconciliation(ops["id"], _d(3, 31), FIRST - 7013 + APPORT + 1730))
    _ok(al.complete_reconciliation(rec["id"], []))
    e["model"] = _entry(ops, "encaissement_facture", 18250, _d(5, 2), counterparty="Autre Client",
                        invoice_id=OTHER_INVOICE_ID)
    e["last"] = _entry(ops, "encaissement_facture",
                       SETTLING_LAST if settles else PARTIAL_LAST, _d(6, 15),
                       counterparty=CLIENT, invoice_id=INVOICE_ID, cleared=_d(6, 15))
    e["charge"] = _entry(card, "dépense", 2875, _d(5, 10), counterparty="Papeterie Exemple",
                         category="fournitures")
    deposit = _ok(trust.create_transaction({
        "account_id": TRUST_ID, "direction": "recette", "amount": 64000,
        "purpose": "dépôt_client", "method": "virement", "counterparty": CLIENT,
        "dossier_id": DOSSIER_ID, "client_id": CLIENT_ID, "date": _d(1, 6),
        "description": "", "reference": "REF-EX",
    }))
    _ok(trust.clear_transaction(deposit["id"], _d(1, 7)))
    return {"ops": ops, "card": card, "e": e, "deposit": deposit}


_DAYS = ("date", "cleared_date", "due_date", "paid_date")


def _stated(name: str, doc: dict):
    """A stored value the way the lawyer's plan states it."""
    if name == "revisions":
        return len(doc.get("revisions") or [])
    value = doc.get(name)
    if isinstance(value, datetime):
        return value.date().isoformat() if name in _DAYS else value.isoformat()
    return value


def _outstanding(fake) -> int:
    return sum(invoice_model.balance_of(inv)
               for inv in fake.peek_collection("invoices").values()
               if inv.get("status") in ("envoyée", "en_retard"))


def build_plan(fake, bench, *, status: str = "payée") -> dict:
    entry = fake.peek(_tx(bench["e"]["apport"]))
    invoice = fake.peek(f"invoices/{INVOICE_ID}")
    model = fake.peek(_tx(bench["e"]["model"]))
    other = fake.peek(f"invoices/{OTHER_INVOICE_ID}")
    before = _outstanding(fake)
    return {
        "motif": MOTIF,
        "note": "Banc de test.",
        "compte": {"id": bench["ops"]["id"], "plancher": "2031-03-31",
                   "solde_cents": fake.peek(f"admin_accounts/{bench['ops']['id']}")["ledger_balance"]},
        "ecriture": {
            "id": entry["id"],
            "attendu": {n: _stated(n, entry) for n in rx.ENTRY_EXPECTED},
            "cible": {"kind": "encaissement_facture", "invoice_id": INVOICE_ID,
                      "invoice_number": "F-3101", "dossier_id": DOSSIER_ID,
                      "dossier_file_number": FILE_NUMBER, "dossier_title": TITLE,
                      "counterparty": CLIENT, "description": DESCRIPTION},
        },
        "facture": {
            "id": INVOICE_ID,
            "attendu": {n: _stated(n, invoice) for n in rx.INVOICE_EXPECTED},
            "cible": {"amount_paid": invoice["amount_paid"] + entry["amount"],
                      "paid_date": _stated("paid_date", invoice), "status": status},
        },
        "intouchables": [{"chemin": _tx(model), "etag": model["etag"]},
                         {"chemin": f"invoices/{OTHER_INVOICE_ID}", "etag": other["etag"]}],
        "encours": {"avant": before, "apres": before - entry["amount"]},
        "periode": {"du": "2031-01-01", "au": "2031-03-31"},
    }


def _tx(entry_or_id) -> str:
    entry_id = entry_or_id if isinstance(entry_or_id, str) else entry_or_id["id"]
    return f"admin_transactions/{entry_id}"


class Cli:
    def __init__(self, tmp_path: pathlib.Path, capsys):
        self.tmp, self.capsys, self.n = tmp_path, capsys, 0
        self.backups = tmp_path / "sauvegardes"
        self.backups.mkdir()

    def _next(self, stem: str, suffix: str) -> pathlib.Path:
        self.n += 1
        return self.tmp / f"{stem}-{self.n}{suffix}"

    def plan(self, data: dict) -> pathlib.Path:
        path = self._next("plan", ".json")
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return path

    def run(self, *argv) -> tuple[int, str]:
        code = rx.main([str(a) for a in argv])
        return code, self.capsys.readouterr().out

    @staticmethod
    def _read(path: pathlib.Path) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else ""

    @staticmethod
    def _fingerprint(out: str) -> str:
        found = re.search(r"Empreinte : ([0-9a-f]{16})", out)
        return found.group(1) if found else ""

    def dry(self, plan_path) -> tuple[int, str, str, str]:
        report = self._next("rapport", ".md")
        code, out = self.run(plan_path, "--rapport", report)
        return code, out, self._read(report), self._fingerprint(out)

    def apply(self, plan_path, fingerprint: str) -> tuple[int, str, str]:
        report = self._next("rapport", ".md")
        code, out = self.run(plan_path, "--rapport", report, "--sauvegarde", self.backups,
                             "--appliquer", fingerprint)
        return code, out, self._read(report)

    def migrate(self, data: dict) -> pathlib.Path:
        plan_path = self.plan(data)
        code, out, report, fingerprint = self.dry(plan_path)
        assert code == 0, out + report
        code, out, report = self.apply(plan_path, fingerprint)
        assert code == 0, out + report
        return plan_path

    def restore(self, backup, fingerprint: str = "") -> tuple[int, str, str, str]:
        report = self._next("rapport", ".md")
        argv = ["--restaurer", backup, "--rapport", report]
        if fingerprint:
            argv += ["--appliquer", fingerprint]
        code, out = self.run(*argv)
        return code, out, self._read(report), self._fingerprint(out)


@pytest.fixture
def fake(monkeypatch):
    mods = [m for n, m in sorted(sys.modules.items())
            if n.startswith("models.") and getattr(m, "db", None) is not None]
    return install(monkeypatch, *mods, rla, rx, rr, vai, vti)


@pytest.fixture
def bench(fake, monkeypatch):
    return build_bench(fake, monkeypatch.setattr)


@pytest.fixture
def cli(tmp_path, capsys):
    return Cli(tmp_path, capsys)


def _snapshot(fake) -> dict:
    return {name: fake.peek_collection(name)
            for name in ("admin_transactions", "admin_accounts", "admin_reconciliations",
                         "invoices", "trust_transactions", "counters")}


def _without(snapshot: dict, *paths: str) -> dict:
    out = copy.deepcopy(snapshot)
    for path in paths:
        collection, doc_id = path.split("/")
        out[collection].pop(doc_id, None)
    return out


# ══════════════════════════════════════════════════════════════════════
# 1. The dry run, then the write — the application's encaissement, nothing else
# ══════════════════════════════════════════════════════════════════════


def test_a_dry_run_writes_nothing_and_the_report_says_everything(fake, bench, cli):
    before, commits = _snapshot(fake), len(fake.commits)
    code, out, report, fingerprint = cli.dry(cli.plan(build_plan(fake, bench)))
    assert code == 0, out + report
    assert len(fake.commits) == commits
    assert _snapshot(fake) == before
    assert re.fullmatch(r"[0-9a-f]{16}", fingerprint)
    assert "Mode : à blanc" in report
    assert f"Projet : `{fake.project}`" in report
    assert f"Empreinte : **{fingerprint}**" in report
    assert "À blanc : rien n'a été écrit" in out
    assert "**ÉCART**" not in report
    # What the lawyer asked to be told: the fields, the provenance, the
    # earlier trail, the lock, the caches, the trace.
    assert "`kind`" in report and "`invoice_id`" in report and "`amount_paid`" in report
    assert "Provenance : `script`" in report
    assert "`lock_reason` (dérivé à la lecture, jamais stocké) : écriture_verrouillée → " \
           "écriture_verrouillée" in report
    assert "Conciliations du compte qui citent l'écriture : aucune." in report
    assert "## 7. Caches" in report and "## 8. Trace" in report


def test_the_write_is_the_encaissement_the_application_records(fake, bench, cli):
    entry_id = bench["e"]["apport"]["id"]
    snapshot = _snapshot(fake)
    stored = fake.peek(_tx(entry_id))
    invoice = fake.peek(f"invoices/{INVOICE_ID}")
    plan_path = cli.migrate(build_plan(fake, bench))
    digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()

    after = fake.peek(_tx(entry_id))
    assert (after["kind"], after["invoice_id"], after["invoice_number"], after["dossier_id"],
            after["dossier_file_number"], after["dossier_title"], after["counterparty"],
            after["description"]) == (
        "encaissement_facture", INVOICE_ID, "F-3101", DOSSIER_ID, FILE_NUMBER, TITLE, CLIENT,
        DESCRIPTION)
    for key in ("amount", "direction", "status", "date", "cleared_date", "sequence",
                "account_id", "method", "reference", "net_amount", "gst_amount", "qst_amount",
                "category", "trust_transaction_id", "reverses_id", "reversed_by_id",
                "related_transaction_id", "reconciliation_id", "created_at", "created_via",
                "cleared_via", "supplier_invoice_ref"):
        assert after[key] == stored[key], key
    assert after["updated_via"] == "script"
    assert after["etag"] != stored["etag"]
    assert len(after["revisions"]) == len(stored["revisions"]) + 1
    trail = after["revisions"][-1]
    assert (trail["via"], trail["motif"], trail["source_sha256"]) == ("script", MOTIF, digest)
    assert trail["updated_at_before"] == stored["updated_at"]
    assert trail["changes"] == {
        "kind": ["apport", "encaissement_facture"], "invoice_id": [None, INVOICE_ID],
        "invoice_number": ["", "F-3101"], "dossier_id": [None, DOSSIER_ID],
        "dossier_file_number": ["", FILE_NUMBER], "dossier_title": ["", TITLE],
        "counterparty": [OWNER, CLIENT], "description": ["", DESCRIPTION],
    }

    # The invoice: the payment the model computes, the flip to « payée ».
    paid = fake.peek(f"invoices/{INVOICE_ID}")
    assert paid["amount_paid"] == DUE and invoice_model.balance_of(paid) == 0
    assert paid["status"] == "payée"
    assert paid["paid_date"] == invoice["paid_date"]        # the plan kept it
    assert paid["updated_via"] == "script" and paid["etag"] != invoice["etag"]
    for key in ("total", "amount_due", "subtotal", "gst_amount", "qst_amount", "date",
                "due_date", "invoice_number", "notes"):
        assert paid[key] == invoice[key], key

    # Nothing else was written: not the account, not a reconciliation, not
    # another entry or invoice, not the trust register, not a counter.
    assert _without(_snapshot(fake), _tx(entry_id), f"invoices/{INVOICE_ID}") == _without(
        snapshot, _tx(entry_id), f"invoices/{INVOICE_ID}")
    assert vai.main() == 0


def test_the_entry_carries_what_an_encaissement_the_application_records_carries(fake, bench, cli):
    cli.migrate(build_plan(fake, bench))
    reclassified = fake.peek(_tx(bench["e"]["apport"]))
    recorded = fake.peek(_tx(bench["e"]["last"]))
    assert set(reclassified) == set(recorded)
    for key in ("kind", "invoice_id", "invoice_number", "dossier_id", "dossier_file_number",
                "dossier_title", "category", "net_amount", "gst_amount", "qst_amount",
                "direction"):
        assert reclassified[key] == recorded[key], key


def test_a_partial_payment_leaves_the_invoice_overdue(fake, monkeypatch, cli):
    bench = build_bench(fake, monkeypatch.setattr, settles=False)
    before = fake.peek(f"invoices/{INVOICE_ID}")
    cli.migrate(build_plan(fake, bench, status="en_retard"))
    after = fake.peek(f"invoices/{INVOICE_ID}")
    assert after["amount_paid"] == before["amount_paid"] + APPORT
    assert after["status"] == "en_retard"
    assert invoice_model.balance_of(after) == DUE - FIRST - PARTIAL_LAST - APPORT


@pytest.mark.parametrize("choice", ["inchangée", "date_de_l_ecriture"])
def test_paid_date_takes_one_of_its_two_values(fake, bench, cli, choice):
    plan = build_plan(fake, bench)
    if choice == "date_de_l_ecriture":
        plan["facture"]["cible"]["paid_date"] = "2031-03-09"
    cli.migrate(plan)
    expected = _d(3, 9) if choice == "date_de_l_ecriture" else _d(6, 15)
    assert fake.peek(f"invoices/{INVOICE_ID}")["paid_date"] == expected


def test_any_other_paid_date_is_refused(fake, bench, cli):
    plan = build_plan(fake, bench)
    plan["facture"]["cible"]["paid_date"] = "2031-05-01"
    commits = len(fake.commits)
    code, out, report, _fp = cli.dry(cli.plan(plan))
    assert code == 1
    assert "cible.paid_date" in report
    assert len(fake.commits) == commits


# ══════════════════════════════════════════════════════════════════════
# 2. The plan against the base — never bent to fit
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("path, value", [
    (("ecriture", "attendu", "counterparty"), "Quelqu'un d'autre"),
    (("ecriture", "attendu", "etag"), "etag-ancien"),
    (("ecriture", "attendu", "revisions"), 3),
    (("ecriture", "attendu", "cleared_date"), "2031-03-10"),
    (("ecriture", "attendu", "created_via"), "web"),
    (("facture", "attendu", "amount_paid"), FIRST),
    (("facture", "attendu", "paid_date"), "2030-12-20"),
    (("facture", "attendu", "etag"), "etag-ancien"),
    (("compte", "plancher"), "2031-02-28"),
    (("compte", "solde_cents"), 1),
    (("encours", "avant"), 1),
])
def test_a_value_the_base_does_not_hold_refuses_the_run(fake, bench, cli, path, value):
    plan = build_plan(fake, bench)
    target = plan
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, report, fingerprint = cli.dry(cli.plan(plan))
    assert code == 1, report
    assert "L'application serait refusée" in out
    assert "**L'application est refusée**" in report
    code, _out, report = cli.apply(cli.plan(plan), fingerprint)
    assert code == 1
    assert len(fake.commits) == commits and _snapshot(fake) == snapshot


def test_an_untouchable_that_moved_refuses_the_run(fake, bench, cli):
    plan = build_plan(fake, bench)
    plan["intouchables"][0]["etag"] = "etag-ancien"
    code, _out, report, _fp = cli.dry(cli.plan(plan))
    assert code == 1
    assert f"intouchable {_tx(bench['e']['model'])}" in report


# ══════════════════════════════════════════════════════════════════════
# 3. The guards — each one alone, the base altered BEFORE the plan is drawn
# ══════════════════════════════════════════════════════════════════════


def _alter_entry(**fields):
    def alter(fake, bench):
        _set(fake, _tx(bench["e"]["apport"]), **fields)
    return alter


def _alter_account(**fields):
    def alter(fake, bench):
        _set(fake, f"admin_accounts/{bench['ops']['id']}", **fields)
    return alter


def _alter_invoice(**fields):
    def alter(fake, bench):
        _set(fake, f"invoices/{INVOICE_ID}", **fields)
    return alter


@pytest.mark.parametrize("alter, reason", [
    (_alter_account(account_type="carte_crédit"), "s'encaisse au compte d'opérations"),
    (_alter_account(status="fermé"), "Ce compte est fermé."),
    (_alter_invoice(status="brouillon"), "La facture doit être émise"),
    (_alter_invoice(amount_paid=DUE - 100), "Le montant excède le solde dû"),
    (_alter_entry(trust_transaction_id="t-1"), "liée au fidéicommis"),
    (_alter_entry(reversed_by_id="r-1"), "membre d'une contre-passation"),
    (_alter_entry(status="annulée"), "l'écriture est annulée"),
    (_alter_entry(category="fournitures"), "porte une catégorie"),
    (_alter_entry(net_amount=100), "porte une ventilation"),
    (_alter_entry(kind="dépense", direction="déboursé"), "nature « dépense »"),
    (_alter_entry(invoice_id=OTHER_INVOICE_ID), "déjà liée à une facture"),
    (_alter_entry(revisions=[{"at": _d(1, 1), "via": "web", "changes": {}}] * 24),
     "plus de place"),
])
def test_every_guard_refuses_alone_and_writes_nothing(fake, bench, cli, alter, reason):
    alter(fake, bench)
    plan = build_plan(fake, bench)
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, report, _fp = cli.dry(cli.plan(plan))
    assert code == 1, report
    assert reason in report
    assert "**ÉCART**" not in report        # the plan matched: the guard refused
    assert len(fake.commits) == commits and _snapshot(fake) == snapshot


@pytest.mark.parametrize("section, key, value, reason", [
    ("ecriture", "kind", "recette_autre", "cible.kind"),
    ("ecriture", "invoice_id", OTHER_INVOICE_ID, "cible.invoice_id"),
    ("ecriture", "invoice_number", "F-9999", "cible.invoice_number"),
    ("ecriture", "dossier_id", "5c1f0e2a-3333-4333-8333-000000000009", "cible.dossier_id"),
    ("ecriture", "dossier_title", "", "cible.dossier_title"),
    ("ecriture", "counterparty", "M. <b>Client</b> Exemple", "serait altéré"),
    ("ecriture", "description", " Paiement", "pas d'espace en bordure"),
    ("facture", "amount_paid", DUE - 1, "cible.amount_paid"),
    ("facture", "status", "en_retard", "le modèle calcule « payée »"),
])
def test_a_target_the_application_would_not_write_is_refused(fake, bench, cli, section, key,
                                                            value, reason):
    plan = build_plan(fake, bench)
    plan[section]["cible"][key] = value
    commits = len(fake.commits)
    code, _out, report, _fp = cli.dry(cli.plan(plan))
    assert code == 1, report
    assert reason in report
    assert len(fake.commits) == commits


def test_a_malformed_plan_is_refused_before_any_read(fake, bench, cli):
    plan = build_plan(fake, bench)
    del plan["ecriture"]["attendu"]["etag"]
    code, out, _report, _fp = cli.dry(cli.plan(plan))
    assert code == 1
    assert "manque etag" in out


# ══════════════════════════════════════════════════════════════════════
# 4. The fingerprint, the guarded batch, the second run
# ══════════════════════════════════════════════════════════════════════


def test_a_stale_fingerprint_writes_nothing(fake, bench, cli):
    plan_path = cli.plan(build_plan(fake, bench))
    code, _out, _report, fingerprint = cli.dry(plan_path)
    assert code == 0
    _set(fake, f"invoices/{INVOICE_ID}", notes="Note ajoutée après la passe à blanc")
    snapshot = _snapshot(fake)
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 1
    assert "l'empreinte a changé" in out
    assert _snapshot(fake) == snapshot
    assert not list(cli.backups.iterdir())


def test_a_document_written_between_the_read_and_the_batch_aborts_it(fake, bench, cli,
                                                                     monkeypatch):
    plan_path = cli.plan(build_plan(fake, bench))
    _code, _out, _report, fingerprint = cli.dry(plan_path)
    original = rx.write_backup

    def backup_then_race(directory, plan):
        result = original(directory, plan)
        _set(fake, f"invoices/{INVOICE_ID}", notes="Écrite entre la lecture et le lot")
        return result

    monkeypatch.setattr(rx, "write_backup", backup_then_race)
    entry_before = fake.peek(_tx(bench["e"]["apport"]))
    code, out, _report = cli.apply(plan_path, fingerprint)
    assert code == 1
    assert "Rien n'a été écrit" in out
    assert fake.peek(_tx(bench["e"]["apport"])) == entry_before
    assert fake.peek(f"invoices/{INVOICE_ID}")["amount_paid"] == FIRST + SETTLING_LAST


def test_a_second_run_finds_the_migration_applied_and_writes_nothing(fake, bench, cli):
    plan_path = cli.migrate(build_plan(fake, bench))
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, report, fingerprint = cli.dry(plan_path)
    assert code == 0, report
    assert "« déjà appliquée »" in report
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 0
    assert "Rien à écrire : la migration est déjà appliquée." in out
    assert len(fake.commits) == commits and _snapshot(fake) == snapshot


def test_a_migration_part_done_is_refused(fake, bench, cli):
    plan_path = cli.migrate(build_plan(fake, bench))
    _set(fake, _tx(bench["e"]["apport"]), description="")
    code, _out, report, _fp = cli.dry(plan_path)
    assert code == 1
    assert "état mixte" in report


# ══════════════════════════════════════════════════════════════════════
# 5. The controls
# ══════════════════════════════════════════════════════════════════════


def test_the_controls_move_by_the_amount_and_nothing_else(fake, bench, cli):
    plan = rx.prepare(cli.plan(build_plan(fake, bench)))
    assert plan.blocking == []
    before, after = plan.before, plan.simulated
    assert before["1"] == after["1"] and before["2"] == after["2"] and before["6"] == after["6"]
    assert before["5"]["conciliations"] == after["5"]["conciliations"]
    assert (before["5"]["lock_reason"], after["5"]["lock_reason"]) == (
        "écriture_verrouillée", "écriture_verrouillée")
    period_before, period_after = before["3"]["periode"], after["3"]["periode"]
    assert (period_after["resultats"]["revenue"]["encaissement_facture"]
            - period_before["resultats"]["revenue"]["encaissement_facture"]) == APPORT
    assert period_after["avoir"]["contributions"] - period_before["avoir"]["contributions"] == -APPORT
    for key in ("total_expenses", "gst", "qst"):
        assert period_after["resultats"][key] == period_before["resultats"][key], key
    assert after["4"]["encours"] - before["4"]["encours"] == -APPORT
    assert after["4"]["encaissements_debout"] == after["4"]["paye"] == DUE
    assert before["4"]["lignes"] == after["4"]["lignes"]


def _stray(extra_payloads):
    """A ``_plan_writes`` that also plans *extra_payloads* — a write going
    astray, which only the controls can see."""
    original = rx._plan_writes

    def planned(plan):
        original(plan)
        for path, payload in extra_payloads(plan).items():
            plan.payloads[path] = {**plan.payloads.get(path, {}), **payload}
    return planned


@pytest.mark.parametrize("extra, control", [
    (lambda plan: {f"admin_transactions/{plan.spec.entry_id}": {"amount": APPORT + 1}},
     "contrôle 2"),
    (lambda plan: {f"admin_accounts/{plan.spec.account_id}": {"ledger_balance": 1}},
     "contrôle 1"),
    (lambda plan: {f"invoices/{plan.spec.invoice_id}": {"amount_paid": DUE - 1}},
     "contrôle 4"),
    (lambda plan: {f"invoices/{OTHER_INVOICE_ID}": {"notes": "Écrite par erreur"}},
     "contrôle 6"),
    (lambda plan: {f"admin_transactions/{plan.spec.entry_id}": {"kind": "recette_autre"}},
     "contrôle 3"),
])
def test_the_controls_catch_a_write_gone_astray(fake, bench, cli, monkeypatch, extra, control):
    monkeypatch.setattr(rx, "_plan_writes", _stray(extra))
    commits = len(fake.commits)
    code, _out, report, _fp = cli.dry(cli.plan(build_plan(fake, bench)))
    assert code == 1, report
    assert control in report
    assert len(fake.commits) == commits


def _perturb(path: tuple, change):
    def perturb(after: dict) -> None:
        target = after
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = change(target[path[-1]])
    return perturb


@pytest.mark.parametrize("path, change, gap", [
    (("3", "periode", "resultats", "total_expenses"), lambda v: v + 1, "contrôle 3 (periode)"),
    (("3", "journal", "avoir", "drawings"), lambda v: v + 1, "contrôle 3 (journal)"),
    (("4", "montants", "total"), lambda v: v + 1, "contrôle 4 — montants"),
    (("4", "lignes", "nombre"), lambda v: v + 1, "contrôle 4 — lignes"),
    (("5", "conciliations"), lambda v: {**v, "autre-compte": {"plancher": "2031-04-30"}},
     "contrôle 5"),
])
def test_the_real_read_after_the_write_names_what_moved_meanwhile(fake, bench, cli, path,
                                                                  change, gap):
    """What a concurrent write would show on the re-read once written: the
    controls compare the AFTER read with the BEFORE one, not with themselves."""
    plan = rx.prepare(cli.plan(build_plan(fake, bench)))
    assert plan.blocking == []
    after = copy.deepcopy(plan.simulated)
    _perturb(path, change)(after)
    gaps, impossible = rx.compare_controls(plan.before, after, plan.expected)
    assert impossible == []
    assert any(g.startswith(gap) for g in gaps), gaps


def test_the_outstanding_control_catches_an_invoice_leaving_the_issued_ones(fake, monkeypatch,
                                                                          cli):
    """The payment right, the receipts right — but the invoice's status
    gone astray takes its whole balance out of the firm's outstanding."""
    bench = build_bench(fake, monkeypatch.setattr, settles=False)
    monkeypatch.setattr(rx, "_plan_writes", _stray(
        lambda plan: {f"invoices/{plan.spec.invoice_id}": {"status": "brouillon"}}))
    code, _out, report, _fp = cli.dry(cli.plan(build_plan(fake, bench, status="en_retard")))
    assert code == 1, report
    assert "contrôle 4 — encours" in report


def test_a_receipts_gap_found_before_the_write_blocks_it(fake, monkeypatch, cli):
    """Check 8 broken before (an amount paid that no entry records): a
    correct write keeps the gap, and the run stops for the lawyer to look."""
    bench = build_bench(fake, monkeypatch.setattr, settles=False)
    invoice = fake.peek(f"invoices/{INVOICE_ID}")
    _set(fake, f"invoices/{INVOICE_ID}", amount_paid=invoice["amount_paid"] + 7)
    code, _out, report, _fp = cli.dry(cli.plan(build_plan(fake, bench, status="en_retard")))
    assert code == 1, report
    assert "contrôle 8 de l'intégrité" in report


def test_a_fee_payment_whose_candidates_change_is_listed(fake, bench, cli):
    fee = legacy_fee_entry({
        "account_id": TRUST_ID, "amount": APPORT, "method": "virement",
        "counterparty": "Cabinet Exemple", "dossier_id": DOSSIER_ID, "client_id": CLIENT_ID,
        "date": _d(3, 5), "invoice_external_ref": "F-EX-31", "description": "",
        "reference": "",
    })
    code, _out, report, _fp = cli.dry(cli.plan(build_plan(fake, bench)))
    assert code == 0, report
    assert f"{fee['id']} : [] → ['{bench['e']['apport']['id']}']" in report


# ══════════════════════════════════════════════════════════════════════
# 6. The backup and the restoration
# ══════════════════════════════════════════════════════════════════════


def _backup(cli) -> pathlib.Path:
    (path,) = list(cli.backups.iterdir())
    return path


def test_the_backup_holds_both_documents_whole_and_restores_them(fake, bench, cli):
    entry_id = bench["e"]["apport"]["id"]
    entry_before = fake.peek(_tx(entry_id))
    invoice_before = fake.peek(f"invoices/{INVOICE_ID}")
    cli.migrate(build_plan(fake, bench))
    backup = _backup(cli)
    body = json.loads(backup.read_text(encoding="utf-8"))
    assert body["format"] == rx.BACKUP_FORMAT
    saved = {d["role"]: rla._untyped(d["avant"]) for d in body["documents"]}
    assert saved["écriture"] == entry_before
    assert saved["facture"] == invoice_before

    code, out, report, fingerprint = cli.restore(backup)
    assert code == 0, out + report
    code, out, report, _fp = cli.restore(backup, fingerprint)
    assert code == 0, out + report
    restored = fake.peek(_tx(entry_id))
    for key in rx.ENTRY_TARGETS:
        assert restored[key] == entry_before[key], key
    assert len(restored["revisions"]) == len(entry_before["revisions"]) + 2
    assert restored["revisions"][-1]["motif"].startswith("Restauration de la sauvegarde du ")
    invoice = fake.peek(f"invoices/{INVOICE_ID}")
    for key in rx.INVOICE_TARGETS:
        assert invoice[key] == invoice_before[key], key
    assert vai.main() == 0
    code, _out, report, _fp = cli.restore(backup)
    assert code == 0 and "« déjà restaurée »" in report


def test_a_restoration_refuses_fields_moved_under_the_migration_etag(fake, bench, cli):
    """An edit that kept the etag — the Firestore console's — is no
    migration state either: nothing is restored over it."""
    cli.migrate(build_plan(fake, bench))
    _set(fake, _tx(bench["e"]["apport"]), description="Retouchée dans la console")
    snapshot = _snapshot(fake)
    code, _out, report, _fp = cli.restore(_backup(cli))
    assert code == 1
    assert "etag de la migration, mais description n'ont pas sa valeur" in report
    assert _snapshot(fake) == snapshot


def test_a_restoration_refuses_over_a_later_write(fake, bench, cli):
    cli.migrate(build_plan(fake, bench))
    # A write of the application: it stamps a new etag.
    _set(fake, f"invoices/{INVOICE_ID}", notes="Écrite après la migration",
         etag="etag-ecriture-ulterieure")
    snapshot = _snapshot(fake)
    code, _out, report, _fp = cli.restore(_backup(cli))
    assert code == 1
    assert "ne porte pas l'etag de la migration" in report
    assert _snapshot(fake) == snapshot


# ══════════════════════════════════════════════════════════════════════
# 7. The command line
# ══════════════════════════════════════════════════════════════════════


def test_paths_inside_the_repository_are_refused_before_anything_is_created(fake, bench, cli,
                                                                           tmp_path):
    inside = rla.REPO / "athena" / "plan-interdit.json"
    plan_path = cli.plan(build_plan(fake, bench))
    for argv in ([inside, "--rapport", tmp_path / "r1.md"],
                 [plan_path, "--rapport", rla.REPO / "rapport-interdit.md"],
                 [plan_path, "--rapport", tmp_path / "r2.md", "--sauvegarde", rla.REPO,
                  "--appliquer", "0" * 16]):
        code, out = cli.run(*argv)
        assert code == 1
        assert "dans le dépôt (public)" in out
    assert not (rla.REPO / "rapport-interdit.md").exists()
    assert not (tmp_path / "r1.md").exists() and not (tmp_path / "r2.md").exists()


def test_appliquer_needs_a_backup_directory_and_a_fingerprint(fake, bench, cli, tmp_path):
    plan_path = cli.plan(build_plan(fake, bench))
    code, out = cli.run(plan_path, "--rapport", tmp_path / "r.md", "--appliquer", "0" * 16)
    assert code == 1 and "--appliquer exige --sauvegarde" in out
    code, out = cli.run(plan_path, "--rapport", tmp_path / "r.md", "--sauvegarde", cli.backups,
                        "--appliquer", "pas-une-empreinte")
    assert code == 1 and "16 caractères hexadécimaux" in out
