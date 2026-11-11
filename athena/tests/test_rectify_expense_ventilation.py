"""``scripts/rectify_expense_ventilation`` — the TPS/TVQ split of locked
expenses rectified OUTSIDE the models, line by line.

The bench is built by the REAL models on the shared fake Firestore: a credit
card whose summer is reconciled, three insurance premiums whose tax on
premiums was booked as TVQ, a properly taxed expense, a premium recorded
« sans taxe » after the lock (the plan's untouchable), an operations account
and a trust register. A plan in the lawyer's format (synthetic amounts,
names and ids only — the repository is public) names the targets. These
tests pin what the script writes — the three split figures, a trail entry
and the stamps, nothing else — what it refuses line by line and why, that a
refusal or a dry run writes nothing, that a second run finds every line
applied, the tax totals by period, the fingerprint, the backup and the
round trip of the restoration.
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
    from models import trust
    from scripts import reclassify_admin_ledger as rla
    from scripts import reclassify_as_invoice_receipt as rx
    from scripts import rectify_expense_ventilation as rv
    from scripts import rectify_registers as rr
    from scripts import verify_admin_integrity as vai
    from scripts import verify_trust_integrity as vti

from tests._fake_firestore import install  # noqa: E402
from tests._releve import completer  # noqa: E402

UTC = timezone.utc
TODAY = "2032-11-20T16:00:00+00:00"
DOSSIER_ID = "6d2a1f3b-4444-4444-8444-000000000001"
CLIENT_ID = "6d2a1f3b-4444-4444-8444-0000000000c1"
INVOICE_ID = "6d2a1f3b-4444-4444-8444-0000000000a1"
TRUST_ID = "6d2a1f3b-4444-4444-8444-0000000000f1"
INSURER = "Assureur Exemple"
MOTIF = ("Taxe sur les primes d'assurance, non récupérable — correction du 2032-11-20, "
         "ligne {ligne}")
NET, TAX = 2000, 180
PREMIUM = NET + TAX
LINES = ("p1", "p2", "p3")


def _d(m: int, d: int, y: int = 2032) -> datetime:
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


def _tx(entry_or_id) -> str:
    entry_id = entry_or_id if isinstance(entry_or_id, str) else entry_or_id["id"]
    return f"admin_transactions/{entry_id}"


# ── the bench ─────────────────────────────────────────────────────────────


def _entry(account: dict, kind: str, amount: int, day: datetime, *, counterparty: str,
           cleared=None, **extra) -> dict:
    data = {"account_id": account["id"], "kind": kind, "amount": amount,
            "method": "carte", "counterparty": counterparty, "date": day,
            "description": "", "reference": "", "supplier_invoice_ref": "", **extra}
    return _ok(al.create_transaction(data, cleared_date=cleared))


def _premium(card: dict, day: datetime, cleared: datetime, reference: str, invoice_ref: str):
    return _entry(card, "dépense", PREMIUM, day, counterparty=INSURER, category="assurances",
                  net_amount=NET, gst_amount=0, qst_amount=TAX, cleared=cleared,
                  reference=reference, supplier_invoice_ref=invoice_ref)


def build_bench(fake, set_attr) -> dict:
    _freeze_today(set_attr, TODAY)
    fake.seed(f"trust_accounts/{TRUST_ID}", {
        "id": TRUST_ID, "name": "Général", "status": "actif", "account_type": "général",
        "book_balance": 0, "bank_balance": 0, "etag": "t0",
    })
    fake.seed(f"dossiers/{DOSSIER_ID}", {
        "id": DOSSIER_ID, "file_number": "2032-003", "title": "Exemple c. Démonstration",
        "client_ids": [CLIENT_ID], "clients": [{"id": CLIENT_ID, "name": "Client Exemple"}],
        "trust_balance": 0, "trust_balance_by_client": {}, "trust_cleared_by_client": {},
    })
    fake.seed(f"invoices/{INVOICE_ID}", {
        "id": INVOICE_ID, "invoice_number": "F-3201", "status": "envoyée", "total": 9100,
        "amount_due": 9100, "amount_paid": 0, "paid_date": None, "etag": "inv-e0",
    })
    ops = _ok(al.create_account({"name": "Opérations", "account_type": "opérations"}))
    card = _ok(al.create_account({"name": "Carte Exemple", "account_type": "carte_crédit"}))
    e: dict = {}
    e["p1"] = _premium(card, _d(6, 12), _d(6, 13), "REF-P1", "P-0001")
    e["p2"] = _premium(card, _d(7, 12), _d(7, 14), "REF-P2", "P-0002")
    e["p3"] = _premium(card, _d(8, 12), _d(8, 13), "REF-P3", "")
    e["taxed"] = _entry(card, "dépense", 3449, _d(7, 20), counterparty="Papeterie Exemple",
                        category="fournitures", net_amount=3000, gst_amount=150,
                        qst_amount=299, cleared=_d(7, 21))
    rec = _ok(al.create_reconciliation(card["id"], _d(8, 15), 3 * PREMIUM + 3449))
    _ok(completer(al, rec["id"], []))
    e["model"] = _entry(card, "dépense", PREMIUM, _d(10, 12), counterparty=INSURER,
                        category="assurances", net_amount=PREMIUM, gst_amount=0,
                        qst_amount=0, cleared=_d(10, 13))
    e["income"] = _entry(ops, "recette_autre", 4500, _d(7, 3), counterparty="Banque Exemple",
                         cleared=_d(7, 4))
    deposit = _ok(trust.create_transaction({
        "account_id": TRUST_ID, "direction": "recette", "amount": 7300,
        "purpose": "dépôt_client", "method": "virement", "counterparty": "Client Exemple",
        "dossier_id": DOSSIER_ID, "client_id": CLIENT_ID, "date": _d(5, 6),
        "description": "", "reference": "REF-EX",
    }))
    return {"ops": ops, "card": card, "e": e, "deposit": deposit}


_DAYS = ("date", "cleared_date")


def _stated(name: str, doc: dict):
    """A stored value the way the lawyer's plan states it."""
    value = doc.get(name)
    if isinstance(value, datetime):
        return value.date().isoformat() if name in _DAYS else value.isoformat()
    return value


def build_plan(fake, bench, keys=LINES) -> dict:
    lines = []
    for n, key in enumerate(keys, start=1):
        doc = fake.peek(_tx(bench["e"][key]))
        lines.append({"ligne": n, "ecriture": doc["id"],
                      "attendu": {k: _stated(k, doc) for k in rv.LINE_EXPECTED},
                      "cible": {"net_amount": doc["amount"], "gst_amount": 0, "qst_amount": 0}})
    model = fake.peek(_tx(bench["e"]["model"]))
    return {
        "motif": MOTIF,
        "note": "Banc de test.",
        "compte": {"id": bench["card"]["id"]},
        "lignes": lines,
        "intouchables": [{"chemin": _tx(model), "etag": model["etag"]}],
        "periodes": [{"du": "2032-04-01", "au": "2032-06-30"},
                     {"du": "2032-07-01", "au": "2032-09-30"}],
    }


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
        code = rv.main([str(a) for a in argv])
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
    return install(monkeypatch, *mods, rla, rx, rv, rr, vai, vti)


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


def _line_paths(bench, keys=LINES) -> list:
    return [_tx(bench["e"][k]) for k in keys]


def _split(fake, bench, key) -> tuple:
    doc = fake.peek(_tx(bench["e"][key]))
    return doc["net_amount"], doc["gst_amount"], doc["qst_amount"]


# ══════════════════════════════════════════════════════════════════════
# 1. The dry run, then the write — the split and its trail, nothing else
# ══════════════════════════════════════════════════════════════════════


def test_a_dry_run_writes_nothing_and_the_report_says_everything(fake, bench, cli):
    before, commits = _snapshot(fake), len(fake.commits)
    code, out, report, fingerprint = cli.dry(cli.plan(build_plan(fake, bench)))
    assert code == 0, out + report
    assert len(fake.commits) == commits and _snapshot(fake) == before
    assert re.fullmatch(r"[0-9a-f]{16}", fingerprint)
    assert "Mode : à blanc" in report and f"Empreinte : **{fingerprint}**" in report
    assert "Lignes : 3 à appliquer, 0 déjà appliquée(s), 0 refusée(s)." in report
    assert "**ÉCART**" not in report
    # What the lawyer asked to be told: the fields and the mode, the
    # provenance, the reconciliation snapshots, the filing periods.
    assert "`net_amount`, `gst_amount` et `qst_amount`" in report
    assert "« sans_taxe »" in report and "Provenance : `script`" in report
    assert "aucune ne fige le net, la TPS ou la TVQ" in report
    assert "le modèle n'en enregistre aucune" in report
    assert "écriture_verrouillée → écriture_verrouillée" in report


def test_the_write_is_the_split_its_trail_and_nothing_else(fake, bench, cli):
    snapshot = _snapshot(fake)
    stored = {k: fake.peek(_tx(bench["e"][k])) for k in LINES}
    plan_path = cli.migrate(build_plan(fake, bench))
    digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    for n, key in enumerate(LINES, start=1):
        after = fake.peek(_tx(bench["e"][key]))
        assert (after["net_amount"], after["gst_amount"], after["qst_amount"]) == (PREMIUM, 0, 0)
        for field in ("amount", "direction", "kind", "category", "date", "cleared_date",
                      "status", "sequence", "account_id", "reference", "supplier_invoice_ref",
                      "counterparty", "method", "created_at", "created_via", "cleared_via",
                      "reconciliation_id"):
            assert after[field] == stored[key][field], (key, field)
        assert after["updated_via"] == "script" and after["etag"] != stored[key]["etag"]
        assert len(after["revisions"]) == len(stored[key]["revisions"]) + 1
        trail = after["revisions"][-1]
        assert trail["motif"] == MOTIF.format(ligne=n)
        assert (trail["via"], trail["source_sha256"]) == ("script", digest)
        assert trail["changes"] == {"net_amount": [NET, PREMIUM], "qst_amount": [TAX, 0]}
        assert trail["updated_at_before"] == stored[key]["updated_at"]
    assert _without(_snapshot(fake), *_line_paths(bench)) == _without(snapshot,
                                                                       *_line_paths(bench))
    assert vai.main() == 0


def test_a_line_the_base_does_not_match_is_refused_and_the_others_apply(fake, bench, cli):
    plan = build_plan(fake, bench)
    plan["lignes"][1]["attendu"]["etag"] = "etag-ancien"
    stored = fake.peek(_tx(bench["e"]["p2"]))
    plan_path = cli.plan(plan)
    code, out, report, fingerprint = cli.dry(plan_path)
    assert code == 0, report
    assert "Lignes : 2 à appliquer, 0 déjà appliquée(s), 1 refusée(s)." in report
    assert "Ligne 2, etag : le plan attend « etag-ancien »" in report
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 0, out + report
    assert _split(fake, bench, "p1") == (PREMIUM, 0, 0)
    assert _split(fake, bench, "p3") == (PREMIUM, 0, 0)
    assert fake.peek(_tx(bench["e"]["p2"])) == stored


def test_the_tax_totals_move_by_period_and_nothing_else(fake, bench, cli):
    plan = rv.prepare(cli.plan(build_plan(fake, bench)))
    assert plan.blocking == []
    before, after = plan.before["3"], plan.simulated["3"]
    for key, lines in (("2032-04-01/2032-06-30", 1), ("2032-07-01/2032-09-30", 2),
                       ("journal", 3)):
        assert after[key]["qst"] - before[key]["qst"] == -lines * TAX, key
        assert (after[key]["expenses_net"]["assurances"]
                - before[key]["expenses_net"]["assurances"]) == lines * TAX, key
        for unchanged in ("gst", "total_expenses", "total_revenue"):
            assert after[key][unchanged] == before[key][unchanged], (key, unchanged)
    assert plan.before["1"] == plan.simulated["1"]
    assert plan.before["4"] == plan.simulated["4"]
    assert plan.before["5"] == plan.simulated["5"]


# ══════════════════════════════════════════════════════════════════════
# 2. The guards — each one alone, the base altered BEFORE the plan is drawn
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("fields, reason", [
    ({"kind": "prélèvement", "category": None}, "nature « prélèvement »"),
    ({"reversed_by_id": "r-1"}, "membre d'une contre-passation"),
    ({"status": "annulée"}, "l'écriture est annulée"),
    ({"invoice_id": INVOICE_ID}, "liée à une facture ou au fidéicommis"),
    ({"trust_transaction_id": "t-1"}, "liée à une facture ou au fidéicommis"),
    ({"direction": "recette"}, "l'écriture n'est pas un déboursé"),
    ({"revisions": [{"at": _d(1, 1), "via": "web", "changes": {}}] * 24}, "plus de place"),
    ({"net_amount": PREMIUM, "qst_amount": 0}, "déjà la cible, sans le fil de ce plan"),
])
def test_every_guard_refuses_its_line_alone(fake, bench, cli, fields, reason):
    _set(fake, _tx(bench["e"]["p2"]), **fields)
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, _out, report, _fp = cli.dry(cli.plan(build_plan(fake, bench)))
    assert code == 0, report
    assert "Lignes : 2 à appliquer, 0 déjà appliquée(s), 1 refusée(s)." in report
    assert reason in report
    assert "**ÉCART**" not in report        # the plan matched: the guard refused
    assert len(fake.commits) == commits and _snapshot(fake) == snapshot


@pytest.mark.parametrize("target", [
    {"net_amount": NET, "gst_amount": 0, "qst_amount": 0},
    {"net_amount": 0, "gst_amount": 0, "qst_amount": 0},
])
def test_a_target_the_model_would_not_store_as_is_is_refused(fake, bench, cli, target):
    plan = build_plan(fake, bench)
    plan["lignes"][0]["cible"] = target
    code, _out, report, _fp = cli.dry(cli.plan(plan))
    assert code == 0, report
    assert "le modèle ne l'accepte pas telle quelle" in report


def test_a_plan_on_the_wrong_account_refuses_every_line(fake, bench, cli):
    plan = build_plan(fake, bench)
    plan["compte"]["id"] = bench["ops"]["id"]
    code, out, report, _fp = cli.dry(cli.plan(plan))
    assert "Lignes : 0 à appliquer, 0 déjà appliquée(s), 3 refusée(s)." in report
    assert report.count("l'écriture n'est pas au compte du plan") == 3
    assert "rien n'est à écrire" in out and code == 0


def test_an_untouchable_that_moved_refuses_the_whole_run(fake, bench, cli):
    plan = build_plan(fake, bench)
    plan["intouchables"][0]["etag"] = "etag-ancien"
    snapshot = _snapshot(fake)
    plan_path = cli.plan(plan)
    code, _out, report, fingerprint = cli.dry(plan_path)
    assert code == 1
    assert "**L'application est refusée**" in report
    code, _out, _report = cli.apply(plan_path, fingerprint)
    assert code == 1 and _snapshot(fake) == snapshot


@pytest.mark.parametrize("change, message", [
    (lambda p: p.update(motif="Sans le numéro de ligne"), "{ligne}"),
    (lambda p: p["lignes"][0]["cible"].update(qst_amount=-1), "positif ou nul"),
    (lambda p: p["lignes"].append(dict(p["lignes"][0], ligne=9)), "une seule fois"),
    (lambda p: p.update(periodes=[]), "« periodes »"),
])
def test_a_malformed_plan_is_refused_before_any_read(fake, bench, cli, change, message):
    plan = build_plan(fake, bench)
    change(plan)
    code, out, _report, _fp = cli.dry(cli.plan(plan))
    assert code == 1 and "plan mal formé" in out and message in out


# ══════════════════════════════════════════════════════════════════════
# 3. The fingerprint, the guarded batch, the second run
# ══════════════════════════════════════════════════════════════════════


def test_a_stale_fingerprint_writes_nothing(fake, bench, cli):
    plan_path = cli.plan(build_plan(fake, bench))
    _code, _out, _report, fingerprint = cli.dry(plan_path)
    _set(fake, _tx(bench["e"]["p1"]), description="Note ajoutée après la passe à blanc")
    snapshot = _snapshot(fake)
    code, out, _report = cli.apply(plan_path, fingerprint)
    assert code == 1 and "l'empreinte a changé" in out
    assert _snapshot(fake) == snapshot and not list(cli.backups.iterdir())


def test_a_document_written_between_the_read_and_the_batch_aborts_it(fake, bench, cli,
                                                                     monkeypatch):
    plan_path = cli.plan(build_plan(fake, bench))
    _code, _out, _report, fingerprint = cli.dry(plan_path)
    original = rv.write_backup

    def backup_then_race(directory, plan):
        result = original(directory, plan)
        _set(fake, _tx(bench["e"]["p3"]), description="Écrite entre la lecture et le lot")
        return result

    monkeypatch.setattr(rv, "write_backup", backup_then_race)
    code, out, _report = cli.apply(plan_path, fingerprint)
    assert code == 1 and "Rien n'a été écrit" in out
    for key in LINES:
        assert _split(fake, bench, key) == (NET, 0, TAX), key


def test_a_second_run_finds_every_line_applied_and_writes_nothing(fake, bench, cli):
    plan_path = cli.migrate(build_plan(fake, bench))
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, report, fingerprint = cli.dry(plan_path)
    assert code == 0, report
    assert "Lignes : 0 à appliquer, 3 déjà appliquée(s), 0 refusée(s)." in report
    assert "rien n'est à écrire" in out and "--appliquer" not in out
    code, out, _report = cli.apply(plan_path, fingerprint)
    assert code == 0 and "Rien à écrire." in out
    assert len(fake.commits) == commits and _snapshot(fake) == snapshot


def test_a_line_part_done_is_refused(fake, bench, cli):
    plan_path = cli.migrate(build_plan(fake, bench))
    _set(fake, _tx(bench["e"]["p2"]), net_amount=NET, qst_amount=TAX)
    code, _out, report, _fp = cli.dry(plan_path)
    assert code == 0
    assert "état mixte" in report
    assert "Lignes : 0 à appliquer, 2 déjà appliquée(s), 1 refusée(s)." in report


# ══════════════════════════════════════════════════════════════════════
# 4. The controls
# ══════════════════════════════════════════════════════════════════════


def _stray(extra_payloads):
    original = rv._plan_writes

    def planned(plan):
        original(plan)
        for path, payload in extra_payloads(plan).items():
            plan.payloads[path] = {**plan.payloads.get(path, {}), **payload}
    return planned


@pytest.mark.parametrize("extra, control", [
    (lambda plan: {plan.spec.lines[0].path: {"cleared_date": _d(6, 14)}}, "contrôle 2"),
    (lambda plan: {plan.spec.lines[0].path: {"amount": PREMIUM + 1}}, "contrôle 1"),
    (lambda plan: {plan.spec.lines[0].path: {"net_amount": PREMIUM - 1, "qst_amount": 1}},
     "contrôle 3"),
    (lambda plan: {f"admin_accounts/{plan.spec.account_id}": {"ledger_balance": 1}},
     "contrôle 1"),
    (lambda plan: {f"invoices/{INVOICE_ID}": {"notes": "Écrite par erreur"}}, "contrôle 5"),
    (lambda plan: {f"trust_transactions/{plan.store.trust_rows[0]['id']}":
                   {"description": "Écrite par erreur"}}, "contrôle 5"),
    (lambda plan: {f"admin_reconciliations/{plan.store.recs[0]['id']}":
                   {"notes": "Écrite par erreur"}}, "contrôle 5"),
])
def test_the_controls_catch_a_write_gone_astray(fake, bench, cli, monkeypatch, extra, control):
    monkeypatch.setattr(rv, "_plan_writes", _stray(extra))
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
    (("3", "2032-07-01/2032-09-30", "gst"), lambda v: v + 1, "contrôle 3 (2032-07-01/2032-09-30)"),
    (("3", "journal", "total_expenses"), lambda v: v + 1, "contrôle 3 (journal)"),
    (("4", "conciliations"), lambda v: {**v, "autre-compte": {"plancher": "2032-09-30"}},
     "contrôle 4"),
])
def test_the_real_read_after_the_write_names_what_moved_meanwhile(fake, bench, cli, path,
                                                                  change, gap):
    plan = rv.prepare(cli.plan(build_plan(fake, bench)))
    after = copy.deepcopy(plan.simulated)
    _perturb(path, change)(after)
    gaps, impossible = rv.compare_controls(plan.before, after, plan.expected)
    assert impossible == []
    assert any(g.startswith(gap) for g in gaps), gaps


# ══════════════════════════════════════════════════════════════════════
# 5. The backup and the restoration
# ══════════════════════════════════════════════════════════════════════


def _backup(cli) -> pathlib.Path:
    (path,) = list(cli.backups.iterdir())
    return path


def test_the_backup_holds_each_entry_whole_and_restores_it(fake, bench, cli):
    stored = {k: fake.peek(_tx(bench["e"][k])) for k in LINES}
    cli.migrate(build_plan(fake, bench))
    backup = _backup(cli)
    body = json.loads(backup.read_text(encoding="utf-8"))
    assert body["format"] == rv.BACKUP_FORMAT
    saved = {d["ligne"]: rla._untyped(d["avant"]) for d in body["documents"]}
    assert saved == {n: stored[k] for n, k in enumerate(LINES, start=1)}

    code, out, report, fingerprint = cli.restore(backup)
    assert code == 0, out + report
    code, out, report, _fp = cli.restore(backup, fingerprint)
    assert code == 0, out + report
    for n, key in enumerate(LINES, start=1):
        restored = fake.peek(_tx(bench["e"][key]))
        assert (restored["net_amount"], restored["gst_amount"], restored["qst_amount"]) == (
            NET, 0, TAX)
        assert len(restored["revisions"]) == len(stored[key]["revisions"]) + 2
        assert restored["revisions"][-1]["motif"].endswith(f"— ligne {n}")
    assert vai.main() == 0
    code, out, report, _fp = cli.restore(backup)
    assert code == 0 and "« déjà restaurée »" in report and "rien n'est à restaurer" in out


def test_a_restoration_refuses_over_a_later_write(fake, bench, cli):
    cli.migrate(build_plan(fake, bench))
    # A write of the application: it stamps a new etag.
    _set(fake, _tx(bench["e"]["p1"]), description="Écrite après la migration",
         etag="etag-ecriture-ulterieure")
    snapshot = _snapshot(fake)
    code, _out, report, _fp = cli.restore(_backup(cli))
    assert code == 1
    assert "ne porte pas l'etag de la migration" in report
    assert _snapshot(fake) == snapshot


def test_a_restoration_refuses_fields_moved_under_the_migration_etag(fake, bench, cli):
    cli.migrate(build_plan(fake, bench))
    _set(fake, _tx(bench["e"]["p1"]), qst_amount=1, net_amount=PREMIUM - 1)
    code, _out, report, _fp = cli.restore(_backup(cli))
    assert code == 1
    assert "etag de la migration, mais net_amount, qst_amount n'ont pas sa valeur" in report


# ══════════════════════════════════════════════════════════════════════
# 6. The command line
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
        assert code == 1 and "dans le dépôt (public)" in out
    assert not (rla.REPO / "rapport-interdit.md").exists()
    assert not (tmp_path / "r1.md").exists() and not (tmp_path / "r2.md").exists()
