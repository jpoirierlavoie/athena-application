"""``scripts/reclassify_admin_category`` — the category of administration
expenses moved OUTSIDE the models, line by line (lot C, 2026-11-07).

The bench is built by the REAL models on the shared fake Firestore: a credit
card whose spring is reconciled — usage-billed AI and cloud charges filed
under « abonnements », a flat software plan that stays there, a supply filed
as « fournitures », all locked — then an unlocked charge on the operations
account, an income and a trust register. A plan in the lawyer's CSV format
(a fictional year, invented amounts, names and ids — the repository is
public) names the targets. These tests pin what the script writes — the
category, one trail entry and the stamps, nothing else — what it refuses
line by line and why, that a refusal, a dry run or a listing writes nothing,
that a second run finds every line applied, the results statement moving
only between the two categories, the controls, the fingerprint, the backup
and the round trip of the restoration.
"""

import copy
import csv
import hashlib
import io
import json
import os
import pathlib
import re
import sys
from datetime import date, datetime, timezone
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
    from scripts import reclassify_admin_category as rc
    from scripts import reclassify_admin_ledger as rla
    from scripts import reclassify_as_invoice_receipt as rx
    from scripts import rectify_registers as rr
    from scripts import verify_admin_integrity as vai
    from scripts import verify_trust_integrity as vti

from google.api_core import exceptions as gexc  # noqa: E402

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
TODAY = "2031-11-20T16:00:00+00:00"
DOSSIER_ID = "3c9d4e5f-6666-4666-8666-000000000001"
CLIENT_ID = "3c9d4e5f-6666-4666-8666-0000000000c1"
INVOICE_ID = "3c9d4e5f-6666-4666-8666-0000000000a1"
TRUST_ID = "3c9d4e5f-6666-4666-8666-0000000000f1"
ABSENT_ID = "3c9d4e5f-6666-4666-8666-0000000000ee"
AI = "Fournisseur IA Exemple"
CLOUD = "Nuage Exemple inc."
FLAT = "Logiciel Exemple"
SOURCE, TARGET = "abonnements", "infonuagique_ia"
MOTIF = "Infonuagique et IA à l'usage — décision du 2031-11-07"
YEAR = "2031-01-01/2031-12-31"
LINES = ("c1", "c2", "c3", "o1")


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


def _tx(entry_or_id) -> str:
    entry_id = entry_or_id if isinstance(entry_or_id, str) else entry_or_id["id"]
    return f"admin_transactions/{entry_id}"


def _ops(fake, since: int) -> list:
    """The write operations committed since commit *since*: a read-only run
    commits none — judged by the operations, never by the count of commits."""
    return [op for record in fake.commits[since:] for op in record.ops]


def _kept(doc: dict) -> dict:
    """Everything but what a write moves: the category, the trail, the stamps."""
    return {k: v for k, v in doc.items() if k not in rc._WRITTEN}


# ── the bench ─────────────────────────────────────────────────────────────


def _entry(account: dict, kind: str, amount: int, day: datetime, *, counterparty: str,
           category=None, cleared=None, **extra) -> dict:
    data = {"account_id": account["id"], "kind": kind, "amount": amount,
            "method": "carte", "counterparty": counterparty, "date": day,
            "description": "", "reference": "", "supplier_invoice_ref": "", **extra}
    if category:
        data["category"] = category
    return _ok(al.create_transaction(data, cleared_date=cleared))


def build_bench(fake, set_attr) -> dict:
    _freeze_today(set_attr, TODAY)
    fake.seed(f"trust_accounts/{TRUST_ID}", {
        "id": TRUST_ID, "name": "Général", "status": "actif", "account_type": "général",
        "book_balance": 0, "bank_balance": 0, "etag": "t0",
    })
    fake.seed(f"dossiers/{DOSSIER_ID}", {
        "id": DOSSIER_ID, "file_number": "2031-007", "title": "Exemple c. Démonstration",
        "client_ids": [CLIENT_ID], "clients": [{"id": CLIENT_ID, "name": "Client Exemple"}],
        "trust_balance": 0, "trust_balance_by_client": {}, "trust_cleared_by_client": {},
    })
    fake.seed(f"invoices/{INVOICE_ID}", {
        "id": INVOICE_ID, "invoice_number": "F-3107", "status": "envoyée", "total": 8800,
        "amount_due": 8800, "amount_paid": 0, "paid_date": None, "etag": "inv-e0",
    })
    ops = _ok(al.create_account({"name": "Opérations", "account_type": "opérations"}))
    card = _ok(al.create_account({"name": "Carte Exemple", "account_type": "carte_crédit"}))
    e: dict = {}
    # The spring, on the card — reconciled just below: every charge LOCKED.
    e["c1"] = _entry(card, "dépense", 2135, _d(3, 4), counterparty=AI, category=SOURCE,
                     net_amount=1857, gst_amount=93, qst_amount=185, cleared=_d(3, 6))
    e["c2"] = _entry(card, "dépense", 4870, _d(4, 4), counterparty=AI, category=SOURCE,
                     net_amount=4236, gst_amount=212, qst_amount=422, cleared=_d(4, 7))
    e["c3"] = _entry(card, "dépense", 1299, _d(5, 2), counterparty=CLOUD, category=SOURCE,
                     cleared=_d(5, 3))
    e["flat"] = _entry(card, "dépense", 2299, _d(5, 10), counterparty=FLAT, category=SOURCE,
                       net_amount=2000, gst_amount=100, qst_amount=199, cleared=_d(5, 11))
    e["paper"] = _entry(card, "dépense", 3449, _d(4, 20), counterparty="Papeterie Exemple",
                        category="fournitures", net_amount=3000, gst_amount=150,
                        qst_amount=299, cleared=_d(4, 21))
    rec = _ok(al.create_reconciliation(card["id"], _d(5, 31), 2135 + 4870 + 1299 + 2299 + 3449))
    _ok(al.complete_reconciliation(rec["id"], []))
    # After the lock, on the operations account: unlocked, its payee in capitals.
    e["o1"] = _entry(ops, "dépense", 1500, _d(6, 5), counterparty=AI.upper(), category=SOURCE,
                     method="virement")
    e["income"] = _entry(ops, "recette_autre", 4500, _d(7, 3), counterparty="Banque Exemple",
                         method="virement", cleared=_d(7, 4))
    deposit = _ok(trust.create_transaction({
        "account_id": TRUST_ID, "direction": "recette", "amount": 6100,
        "purpose": "dépôt_client", "method": "virement", "counterparty": "Client Exemple",
        "dossier_id": DOSSIER_ID, "client_id": CLIENT_ID, "date": _d(5, 6),
        "description": "", "reference": "REF-EX",
    }))
    return {"ops": ops, "card": card, "e": e, "deposit": deposit}


def _row(n: int, doc: dict, **cells) -> dict:
    """One plan line in the lawyer's format, its expectations read off the
    stored entry."""
    row = {"ligne": str(n), "entry_id": doc["id"], "date": doc["date"].date().isoformat(),
           "montant_cents": str(doc["amount"]), "categorie_actuelle": doc["category"],
           "categorie_cible": TARGET, "motif": MOTIF}
    assert not set(cells) - set(row), cells
    row.update(cells)
    return row


def build_plan(fake, bench, keys=LINES) -> list:
    return [_row(n, fake.peek(_tx(bench["e"][key]))) for n, key in enumerate(keys, start=1)]


class Cli:
    def __init__(self, tmp_path: pathlib.Path, capsys):
        self.tmp, self.capsys, self.n = tmp_path, capsys, 0
        self.backups = tmp_path / "sauvegardes"
        self.backups.mkdir()

    def _next(self, stem: str, suffix: str) -> pathlib.Path:
        self.n += 1
        return self.tmp / f"{stem}-{self.n}{suffix}"

    def plan(self, rows: list) -> pathlib.Path:
        path = self._next("plan", ".csv")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=rc.CSV_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        return path

    def plan_text(self, text: str) -> pathlib.Path:
        path = self._next("plan", ".csv")
        path.write_bytes(text.encode("utf-8"))
        return path

    def run(self, *argv) -> tuple[int, str]:
        code = rc.main([str(a) for a in argv])
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

    def migrate(self, rows: list) -> pathlib.Path:
        plan_path = self.plan(rows)
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
    return install(monkeypatch, *mods, rla, rx, rc, rr, vai, vti)


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


def _category(fake, bench, key) -> str:
    return fake.peek(_tx(bench["e"][key]))["category"]


def _statement(fake) -> dict:
    return al.results_statement(list(fake.peek_collection("admin_transactions").values()))


def _backup(cli) -> pathlib.Path:
    (path,) = list(cli.backups.iterdir())
    return path


def _invoices_unreadable(monkeypatch) -> None:
    """The invoices' read fails, as ``read_store`` records it: control 5
    cannot be computed — which must block every write."""
    real = rla.read_store

    def read_store():
        store = real()
        store.invoices, store.invoices_error = None, "ServiceUnavailable"
        return store

    monkeypatch.setattr(rla, "read_store", read_store)


UNREADABLE = "contrôle 5 impossible à calculer (factures illisibles (ServiceUnavailable))"


# ══════════════════════════════════════════════════════════════════════
# 1. The dry run, then the write — the category and its trail, nothing else
# ══════════════════════════════════════════════════════════════════════


def test_a_dry_run_writes_nothing_and_the_report_says_everything(fake, bench, cli):
    before, commits = _snapshot(fake), len(fake.commits)
    code, out, report, fingerprint = cli.dry(cli.plan(build_plan(fake, bench)))
    assert code == 0, out + report
    assert _ops(fake, commits) == [] and _snapshot(fake) == before
    assert re.fullmatch(r"[0-9a-f]{16}", fingerprint)
    assert "Mode : à blanc" in report and f"Empreinte : **{fingerprint}**" in report
    assert "Lignes : 4 à appliquer, 0 déjà appliquée(s), 0 refusée(s)." in report
    assert "**ÉCART**" not in report and "Écarts : aucun" in report
    assert "Seul `category` est écrit" in report and "Provenance : `script`" in report
    assert "aucune ne fige la catégorie d'une dépense" in report
    # The three card charges are LOCKED — and accepted; the operations one is not.
    assert report.count("écriture_verrouillée → écriture_verrouillée") == 3
    assert "aucun → aucun" in report
    assert "Une écriture verrouillée est acceptée" in report
    assert f"motif « {MOTIF} — ligne 1 », via `script`" in report
    assert f"--sauvegarde DOSSIER --appliquer {fingerprint}" in out


def test_the_write_is_the_category_its_trail_and_stamps_and_nothing_else(fake, bench, cli):
    for key in LINES:
        _set(fake, _tx(bench["e"][key]), updated_via="web")
    snapshot = _snapshot(fake)
    stored = {k: fake.peek(_tx(bench["e"][k])) for k in LINES}
    # What the script exists for: the model refuses to edit a locked entry.
    entry, errors = al.update_transaction(stored["c1"]["id"], {"category": TARGET})
    assert entry is None and errors
    commits = len(fake.commits)
    plan_path = cli.migrate(build_plan(fake, bench))
    digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    # ONE batch: one update per line, nothing else.
    assert sorted(_ops(fake, commits)) == sorted(("update", path)
                                                 for path in _line_paths(bench))
    for n, key in enumerate(LINES, start=1):
        after, before = fake.peek(_tx(bench["e"][key])), stored[key]
        assert after["category"] == TARGET
        assert _kept(after) == _kept(before) and set(after) == set(before), key
        assert after["updated_via"] == "script" and after["etag"] != before["etag"]
        assert after["revisions"][:-1] == before["revisions"]
        trail = after["revisions"][-1]
        assert trail["motif"] == f"{MOTIF} — ligne {n}"
        assert (trail["via"], trail["source_sha256"]) == ("script", digest)
        assert trail["changes"] == {"category": [SOURCE, TARGET]}
        assert trail["updated_at_before"] == before["updated_at"]
    assert _without(_snapshot(fake), *_line_paths(bench)) == _without(snapshot,
                                                                       *_line_paths(bench))
    assert vai.main() == 0


def test_the_results_statement_moves_only_between_the_two_categories(fake, bench, cli):
    moved = [fake.peek(_tx(bench["e"][k])) for k in LINES]
    gross, net = sum(d["amount"] for d in moved), sum(d["net_amount"] for d in moved)
    plan = rc.prepare(cli.plan(build_plan(fake, bench)))
    assert plan.blocking == [] and plan.periods == ((date(2031, 1, 1), date(2031, 12, 31)),)
    for key in (YEAR, "journal"):
        assert rla._flat_results_delta(plan.before["3"][key], plan.simulated["3"][key]) == {
            f"expenses:{SOURCE}": -gross, f"expenses:{TARGET}": gross,
            f"expenses_net:{SOURCE}": -net, f"expenses_net:{TARGET}": net}, key
    assert plan.before["1"] == plan.simulated["1"]
    assert plan.before["4"] == plan.simulated["4"]
    assert plan.before["5"] == plan.simulated["5"]

    before = _statement(fake)
    cli.migrate(build_plan(fake, bench))
    after = _statement(fake)
    assert after["expenses"][SOURCE] == before["expenses"][SOURCE] - gross == 2299  # the flat plan
    assert after["expenses"][TARGET] == before["expenses"].get(TARGET, 0) + gross
    assert after["expenses_net"][SOURCE] == before["expenses_net"][SOURCE] - net
    assert after["expenses_net"][TARGET] == before["expenses_net"].get(TARGET, 0) + net
    for unchanged in ("revenue", "gst", "qst", "total_revenue", "total_expenses"):
        assert after[unchanged] == before[unchanged], unchanged
    for category in set(before["expenses"]) | set(after["expenses"]):
        if category not in (SOURCE, TARGET):
            assert after["expenses"].get(category) == before["expenses"].get(category)
            assert after["expenses_net"].get(category) == before["expenses_net"].get(category)


def test_a_line_the_base_no_longer_matches_is_refused_and_the_others_apply(fake, bench, cli):
    rows = build_plan(fake, bench)
    _set(fake, _tx(bench["e"]["c2"]), category="internet")
    stored = fake.peek(_tx(bench["e"]["c2"]))
    plan_path = cli.plan(rows)
    code, out, report, fingerprint = cli.dry(plan_path)
    assert code == 0, report
    assert "Lignes : 3 à appliquer, 0 déjà appliquée(s), 1 refusée(s)." in report
    assert ("`état_changé:catégorie` — catégorie inscrite « internet », le plan attend "
            "« abonnements »") in report
    assert "**ÉCART**" in report
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 0, out + report
    assert [_category(fake, bench, k) for k in ("c1", "c3", "o1")] == [TARGET] * 3
    assert fake.peek(_tx(bench["e"]["c2"])) == stored


# ══════════════════════════════════════════════════════════════════════
# 2. The refusals — each alone, line by line, never forced
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("fields, refusal", [
    ({"kind": "prélèvement"}, "pas_une_dépense"),
    ({"direction": "recette"}, "pas_un_déboursé"),
    ({"reversed_by_id": "r-1"}, "lien_contre_passation"),
    ({"reverses_id": "r-1"}, "lien_contre_passation"),
    ({"status": "annulée"}, "annulée"),
    ({"invoice_id": INVOICE_ID}, "liée_facture"),
    ({"trust_transaction_id": "t-1"}, "liée_fideicommis"),
    ({"account_id": "compte-disparu"}, "compte_introuvable"),
    ({"revisions": [{"at": _d(1, 1), "via": "web", "changes": {}}] * 24}, "fil_plein"),
    ({"revisions": "illisible"}, "fil_illisible"),
])
def test_every_guard_refuses_its_line_alone(fake, bench, cli, fields, refusal):
    # The base altered BEFORE the plan is drawn: the plan matches, the guard refuses.
    _set(fake, _tx(bench["e"]["c2"]), **fields)
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, _out, report, _fp = cli.dry(cli.plan(build_plan(fake, bench)))
    assert code == 0, report
    assert "Lignes : 3 à appliquer, 0 déjà appliquée(s), 1 refusée(s)." in report
    assert f"`{refusal}`" in report
    assert "**ÉCART**" not in report
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot


def test_a_reversed_entry_is_refused_and_its_pair_keeps_one_category(fake, bench, cli):
    reversed_entry = _entry(bench["ops"], "dépense", 777, _d(6, 6), counterparty=AI,
                            category=SOURCE, method="virement")
    _ok(al.reverse_transaction(reversed_entry["id"], "Saisie en double"))
    correction = fake.peek(_tx(fake.peek(_tx(reversed_entry))["reversed_by_id"]))
    assert correction["category"] == SOURCE     # a correction carries its original's category
    rows = build_plan(fake, bench) + [_row(5, fake.peek(_tx(reversed_entry)))]
    plan_path = cli.plan(rows)
    code, _out, report, fingerprint = cli.dry(plan_path)
    assert code == 0, report
    assert "Lignes : 4 à appliquer, 0 déjà appliquée(s), 1 refusée(s)." in report
    assert "`lien_contre_passation` — membre d'une contre-passation" in report
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 0, out + report
    # The pair still nets to zero within ONE category.
    assert fake.peek(_tx(reversed_entry))["category"] == SOURCE
    assert _statement(fake)["expenses"][SOURCE] == 2299


def test_an_original_only_its_correction_names_is_still_a_pair_member(fake, bench, cli):
    """A pair is judged from both ends: an original that carries no trace of
    its reversal — the correction alone names it — is refused all the same,
    in the dry run and in the listing."""
    original = _entry(bench["ops"], "dépense", 777, _d(6, 6), counterparty=AI,
                      category=SOURCE, method="virement")
    stored = fake.peek(_tx(original))
    _ok(al.reverse_transaction(original["id"], "Saisie en double"))
    fake.external_write(_tx(original), stored)       # one-sided: as before the reversal
    assert not fake.peek(_tx(original)).get("reversed_by_id")
    rows = build_plan(fake, bench) + [_row(5, fake.peek(_tx(original)))]
    code, _out, report, _fp = cli.dry(cli.plan(rows))
    assert code == 0, report
    assert "Lignes : 4 à appliquer, 0 déjà appliquée(s), 1 refusée(s)." in report
    assert "`lien_contre_passation` — membre d'une contre-passation" in report
    code, out = cli.run("--lister", SOURCE)
    assert code == 0, out
    assert "refuserait — membre d'une contre-passation" in _listed_line(out, original["id"])


@pytest.mark.parametrize("cells, refusal", [
    ({"categorie_actuelle": "internet"}, "état_changé:catégorie"),
    ({"montant_cents": "2136"}, "état_changé:montant"),
    ({"date": "2031-03-05"}, "état_changé:date"),
    ({"categorie_cible": "logiciels"}, "catégorie_cible_inconnue"),
    ({"categorie_cible": SOURCE}, "cible_égale_actuelle"),
    ({"entry_id": ABSENT_ID}, "écriture_introuvable"),
])
def test_a_line_the_plan_misstates_is_refused_alone(fake, bench, cli, cells, refusal):
    rows = build_plan(fake, bench)
    rows[0].update(cells)
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, _out, report, _fp = cli.dry(cli.plan(rows))
    assert code == 0, report
    assert "Lignes : 3 à appliquer, 0 déjà appliquée(s), 1 refusée(s)." in report
    assert f"`{refusal}`" in report
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot


def test_a_duplicated_entry_refuses_both_its_lines(fake, bench, cli):
    rows = build_plan(fake, bench)
    rows.append(dict(rows[1], ligne="5", categorie_cible="autre"))
    stored = fake.peek(_tx(bench["e"]["c2"]))
    plan_path = cli.plan(rows)
    code, _out, report, fingerprint = cli.dry(plan_path)
    assert code == 0, report
    assert "Lignes : 3 à appliquer, 0 déjà appliquée(s), 2 refusée(s)." in report
    assert report.count("`entrée_en_double`") == 2
    assert "aussi à la ligne 5 du plan" in report and "aussi à la ligne 2 du plan" in report
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 0, out + report
    assert fake.peek(_tx(bench["e"]["c2"])) == stored
    assert [_category(fake, bench, k) for k in ("c1", "c3", "o1")] == [TARGET] * 3


def test_a_category_already_at_the_target_without_this_plan_is_refused(fake, bench, cli):
    rows = build_plan(fake, bench)
    _set(fake, _tx(bench["e"]["c3"]), category=TARGET)
    code, _out, report, _fp = cli.dry(cli.plan(rows))
    assert code == 0, report
    assert "Lignes : 3 à appliquer, 0 déjà appliquée(s), 1 refusée(s)." in report
    assert ("`déjà_la_cible` — la catégorie inscrite est déjà la cible, sans le fil de ce "
            "plan") in report


def test_twenty_three_trail_entries_leave_room_and_nothing_is_trimmed(fake, bench, cli):
    trail = [{"at": _d(1, 1), "via": "web", "changes": {}, "n": i}
             for i in range(rc.MAX_TRAIL_BEFORE)]
    _set(fake, _tx(bench["e"]["c1"]), revisions=trail)
    cli.migrate(build_plan(fake, bench))
    migrated = fake.peek(_tx(bench["e"]["c1"]))["revisions"]
    assert len(migrated) == al._REVISIONS_CAP - 1 and migrated[:-1] == trail
    code, out, report, fingerprint = cli.restore(_backup(cli))
    assert code == 0, out + report
    code, out, report, _fp = cli.restore(_backup(cli), fingerprint)
    assert code == 0, out + report
    restored = fake.peek(_tx(bench["e"]["c1"]))["revisions"]
    assert len(restored) == al._REVISIONS_CAP and restored[:-1] == migrated


@pytest.mark.parametrize("change, said", [
    (lambda rows: rows[1].update(ligne="1"), "numéro de ligne en double"),
    (lambda rows: rows[0].update(entry_id="pas-un-uuid"), "un identifiant (UUID) est attendu"),
    (lambda rows: rows[0].update(montant_cents="21,35"), "un entier positif est attendu"),
    (lambda rows: rows[0].update(date="2031-3-4"), "une date AAAA-MM-JJ est attendue"),
    (lambda rows: rows[0].update(motif=""), "une valeur non vide est attendue"),
    (lambda rows: rows[0].update(motif=" Crédits IA"), "espaces en tête ou en fin"),
    (lambda rows: rows[0].update(motif="Crédits <b>IA</b>"), "serait altéré à l'enregistrement"),
    (lambda rows: rows[0].update(categorie_cible=""), "categorie_cible : valeur requise"),
    (lambda rows: rows[0].update(categorie_actuelle=""), "categorie_actuelle : valeur requise"),
])
def test_a_malformed_plan_is_refused_before_any_read(fake, bench, cli, change, said):
    rows = build_plan(fake, bench)
    change(rows)
    plan_path = cli.plan(rows)
    fake.reset_logs()
    code, out, _report, _fp = cli.dry(plan_path)
    assert code == 1 and "plan mal formé" in out and said in out, out
    assert fake.reads == [] and fake.commits == []


def test_a_plan_of_another_shape_is_refused_whole(fake, bench, cli):
    entry_id = bench["e"]["c1"]["id"]
    header = ",".join(rc.CSV_COLUMNS)
    for text, said in (
            ("ligne,entry_id,categorie_actuelle,categorie_cible,motif\n", "en-tête inattendu"),
            (f'{header}\n1,"{entry_id}"x,2031-03-04,2135,{SOURCE},{TARGET},M\n', "CSV illisible"),
            (f"{header}\n1,{entry_id},2031-03-04,2135\n", "4 colonne(s), 7 attendues"),
            (f"{header}\n", "aucune ligne"),
            ("", "le fichier est vide")):
        code, out, _report, _fp = cli.dry(cli.plan_text(text))
        assert code == 1 and f"plan mal formé : " in out and said in out, (said, out)


def test_an_unreadable_register_stops_the_run_and_the_listing(fake, bench, cli, monkeypatch):
    plan_path = cli.plan(build_plan(fake, bench))

    def refused(*_args, **_kwargs):
        raise gexc.PermissionDenied("lecture refusée")

    monkeypatch.setattr(fake._fake_server, "run_query", refused)
    commits = len(fake.commits)
    code, out, _report, _fp = cli.dry(plan_path)
    assert code == 1 and "lecture du registre d'administration impossible" in out, out
    code, out = cli.run("--lister", SOURCE)
    assert code == 1 and "lecture du registre d'administration impossible" in out, out
    assert _ops(fake, commits) == []


# ══════════════════════════════════════════════════════════════════════
# 3. The fingerprint, the guarded batch, the second run
# ══════════════════════════════════════════════════════════════════════


def test_a_stale_fingerprint_writes_nothing(fake, bench, cli):
    plan_path = cli.plan(build_plan(fake, bench))
    _code, _out, _report, fingerprint = cli.dry(plan_path)
    _set(fake, _tx(bench["e"]["o1"]), description="Note ajoutée après la passe à blanc")
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, _report = cli.apply(plan_path, fingerprint)
    assert code == 1 and "l'empreinte a changé" in out
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot
    assert not list(cli.backups.iterdir())


def test_a_refusal_that_changed_since_the_dry_run_calls_for_a_new_approval(fake, bench, cli):
    """The print covers the refused lines and their reasons, not only the
    writes: a report approved with one reason no longer describes the base."""
    plan_path = cli.plan(build_plan(fake, bench))
    _set(fake, _tx(bench["e"]["c2"]), category="internet")
    _code, _out, report, fingerprint = cli.dry(plan_path)
    assert "catégorie inscrite « internet »" in report
    _set(fake, _tx(bench["e"]["c2"]), category="loyer")       # still refused, otherwise
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, _report = cli.apply(plan_path, fingerprint)
    assert code == 1 and "l'empreinte a changé" in out
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot


def test_a_plan_edited_after_its_dry_run_calls_for_a_new_approval(fake, bench, cli):
    """The fingerprint covers the plan's digest: a motif rewritten after the
    approval — every write and every verdict the same — is another plan."""
    rows = build_plan(fake, bench)
    plan_path = cli.plan(rows)
    _code, _out, _report, fingerprint = cli.dry(plan_path)
    for row in rows:
        row["motif"] = "Autre motif, écrit après l'approbation"
    plan_path.write_bytes(cli.plan(rows).read_bytes())
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, _report = cli.apply(plan_path, fingerprint)
    assert code == 1 and "l'empreinte a changé" in out, out
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot
    assert not list(cli.backups.iterdir())


def test_a_fingerprint_approved_on_another_project_writes_nothing(fake, bench, cli,
                                                                  monkeypatch):
    plan_path = cli.plan(build_plan(fake, bench))
    _code, _out, _report, fingerprint = cli.dry(plan_path)
    monkeypatch.setattr(fake, "project", "autre-projet")
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, _report = cli.apply(plan_path, fingerprint)
    assert code == 1 and "l'empreinte a changé" in out, out
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot


@pytest.mark.parametrize("cause, said", [
    ("impossible", UNREADABLE),
    ("écart", "contrôle 1 — "),
])
def test_a_blocking_control_refuses_even_the_fingerprint_it_printed(fake, bench, cli,
                                                                    monkeypatch, cause, said):
    """A dry run whose controls are impossible or moved still prints its
    fingerprint: applying it writes nothing — a fingerprint names a run, it
    never vouches for it."""
    if cause == "impossible":
        _invoices_unreadable(monkeypatch)
    else:
        monkeypatch.setattr(rc, "_plan_writes", _stray(
            lambda plan: {plan.lines[0].path: {"amount": 2136}}))
    plan_path = cli.plan(build_plan(fake, bench))
    code, out, report, fingerprint = cli.dry(plan_path)
    assert code == 1 and said in report and fingerprint, out + report
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 1 and f"Refusé — rien n'a été écrit : {said}" in out, out
    assert "Mode : application refusée" in report
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot
    assert not list(cli.backups.iterdir())


def test_a_backup_that_cannot_be_written_writes_nothing(fake, bench, cli, monkeypatch):
    plan_path = cli.plan(build_plan(fake, bench))
    _code, _out, _report, fingerprint = cli.dry(plan_path)

    def full_disk(_directory, _plan):
        raise OSError("disque plein")

    monkeypatch.setattr(rc, "write_backup", full_disk)
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 1 and "sauvegarde impossible (OSError) — rien n'a été écrit" in out, out
    assert "Mode : application refusée" in report
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot


def test_a_backup_that_does_not_read_back_whole_writes_nothing(fake, bench, cli, monkeypatch):
    """Each saved document is re-read and compared with the entry as read: an
    encoder that loses a field — the saved entry would not restore whole —
    stops the run before the batch."""
    plan_path = cli.plan(build_plan(fake, bench))
    _code, _out, _report, fingerprint = cli.dry(plan_path)
    real = rla._typed
    monkeypatch.setattr(rla, "_typed", lambda value: (
        {k: v for k, v in real(value).items() if k != "counterparty"}
        if isinstance(value, dict) else real(value)))
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, _report = cli.apply(plan_path, fingerprint)
    assert code == 1 and "sauvegarde impossible (OSError) — rien n'a été écrit" in out, out
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot


def test_a_document_written_between_the_read_and_the_batch_aborts_it(fake, bench, cli,
                                                                     monkeypatch):
    plan_path = cli.plan(build_plan(fake, bench))
    _code, _out, _report, fingerprint = cli.dry(plan_path)
    original = rc.write_backup

    def backup_then_race(directory, plan):
        result = original(directory, plan)
        _set(fake, _tx(bench["e"]["c3"]), description="Écrite entre la lecture et le lot")
        return result

    monkeypatch.setattr(rc, "write_backup", backup_then_race)
    code, out, _report = cli.apply(plan_path, fingerprint)
    assert code == 1 and "Rien n'a été écrit" in out
    for key in LINES:
        assert _category(fake, bench, key) == SOURCE, key


def test_a_second_run_finds_every_line_applied_and_writes_nothing(fake, bench, cli):
    plan_path = cli.migrate(build_plan(fake, bench))
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, report, fingerprint = cli.dry(plan_path)
    assert code == 0, report
    assert "Lignes : 0 à appliquer, 4 déjà appliquée(s), 0 refusée(s)." in report
    assert "rien n'est à écrire" in out and "--appliquer" not in out
    code, out, _report = cli.apply(plan_path, fingerprint)
    assert code == 0 and "Rien à écrire." in out
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot


def test_a_line_part_done_is_refused(fake, bench, cli):
    plan_path = cli.migrate(build_plan(fake, bench))
    _set(fake, _tx(bench["e"]["c2"]), category=SOURCE)
    code, _out, report, _fp = cli.dry(plan_path)
    assert code == 0, report
    assert "`état_mixte`" in report
    assert "Lignes : 0 à appliquer, 3 déjà appliquée(s), 1 refusée(s)." in report


# ══════════════════════════════════════════════════════════════════════
# 4. The controls
# ══════════════════════════════════════════════════════════════════════


def _stray(extra_payloads):
    original = rc._plan_writes

    def planned(plan):
        original(plan)
        for path, payload in extra_payloads(plan).items():
            plan.payloads[path] = {**plan.payloads.get(path, {}), **payload}
    return planned


def _flat_path(plan) -> str:
    return _tx(next(i for i, r in plan.store.rows.items() if r.get("counterparty") == FLAT))


def _card_path(plan) -> str:
    return f"admin_accounts/{plan.store.rows[plan.lines[0].entry_id]['account_id']}"


@pytest.mark.parametrize("extra, control", [
    (lambda plan: {plan.lines[0].path: {"amount": 2136}}, "contrôle 1"),
    (lambda plan: {plan.lines[0].path: {"net_amount": 2135, "gst_amount": 0, "qst_amount": 0}},
     "contrôle 3"),
    (lambda plan: {plan.lines[0].path: {"counterparty": "Autre Exemple"}}, "contrôle 2"),
    (lambda plan: {plan.lines[0].path: {"category": "autre"}}, "contrôle 2"),
    (lambda plan: {plan.lines[0].path: {"revisions": []}}, "contrôle 2"),
    (lambda plan: {_flat_path(plan): {"category": TARGET}}, "contrôle 5"),
    (lambda plan: {_card_path(plan): {"ledger_balance": 1}}, "contrôle 1"),
    (lambda plan: {f"invoices/{INVOICE_ID}": {"notes": "Écrite par erreur"}}, "contrôle 5"),
    (lambda plan: {f"trust_transactions/{plan.store.trust_rows[0]['id']}":
                   {"description": "Écrite par erreur"}}, "contrôle 5"),
    (lambda plan: {f"admin_reconciliations/{plan.store.recs[0]['id']}":
                   {"notes": "Écrite par erreur"}}, "contrôle 5"),
])
def test_the_controls_catch_a_write_gone_astray(fake, bench, cli, monkeypatch, extra, control):
    monkeypatch.setattr(rc, "_plan_writes", _stray(extra))
    commits = len(fake.commits)
    code, _out, report, _fp = cli.dry(cli.plan(build_plan(fake, bench)))
    assert code == 1, report
    assert control in report
    assert _ops(fake, commits) == []


def test_control_2_catches_a_write_on_an_entry_no_line_writes(fake, bench, cli, monkeypatch):
    """A refused line's entry is skipped by control 5 (it is in the plan's
    scope): control 2 alone must see it written."""
    rows = build_plan(fake, bench)
    rows[0].update(categorie_cible=SOURCE)              # refused: cible_égale_actuelle
    monkeypatch.setattr(rc, "_plan_writes", _stray(
        lambda plan: {plan.lines[0].path: {"description": "Écrite par erreur"}}))
    commits = len(fake.commits)
    code, _out, report, _fp = cli.dry(cli.plan(rows))
    assert code == 1, report
    assert (f"contrôle 2 — écriture {bench['e']['c1']['id']} : modifiée, alors qu'aucune "
            f"ligne ne l'écrit") in report
    assert _ops(fake, commits) == []


def _perturb(path: tuple, change):
    def perturb(after: dict) -> None:
        target = after
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = change(target[path[-1]])
    return perturb


@pytest.mark.parametrize("path, change, gap", [
    (("3", YEAR, "gst"), lambda v: v + 1, f"contrôle 3 ({YEAR})"),
    (("3", "journal", "total_expenses"), lambda v: v + 1, "contrôle 3 (journal)"),
    (("4", "conciliations"), lambda v: {**v, "autre-compte": {"plancher": "2031-09-30"}},
     "contrôle 4"),
])
def test_the_real_read_after_the_write_names_what_moved_meanwhile(fake, bench, cli, path,
                                                                  change, gap):
    plan = rc.prepare(cli.plan(build_plan(fake, bench)))
    after = copy.deepcopy(plan.simulated)
    _perturb(path, change)(after)
    gaps, impossible = rc.compare_controls(plan.before, after, plan.expected)
    assert impossible == []
    assert any(g.startswith(gap) for g in gaps), gaps


def test_once_written_a_fresh_read_reports_what_landed_beside_the_plan(fake, bench, cli,
                                                                     monkeypatch):
    """The controls run again on a fresh read after the batch: a write that
    landed beside the plan is named — never « conformes » — and the outcome,
    written, is never hidden."""
    plan_path = cli.plan(build_plan(fake, bench))
    _code, _out, _report, fingerprint = cli.dry(plan_path)
    real_commit, flat = rla.commit, _tx(bench["e"]["flat"])

    def commit_and_another_write(writes, payloads):
        outcome = real_commit(writes, payloads)
        _set(fake, flat, category=TARGET)
        return outcome

    monkeypatch.setattr(rla, "commit", commit_and_another_write)
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 0, out + report
    assert re.search(r"Contrôles après réel : \d+ écart\(s\)", out), out
    assert re.search(r"Écart à la passe approuvée : \d+ différence\(s\)", out), out
    (real_line,) = [line for line in report.splitlines()
                    if line.startswith("- Contrôles après réel : ")]
    assert f"contrôle 3 ({YEAR})" in real_line and "contrôle 5 — " in real_line, real_line
    assert "Écart entre l'après réel et l'après simulé : aucun" not in report


@pytest.mark.parametrize("name, change", [
    ("faits", lambda v: "autre"),
    ("categorie", lambda v: "autre"),
    ("longueur", lambda v: v + 1),
])
def test_control_2_holds_each_written_entry_to_its_category_and_one_trail_entry(
        fake, bench, cli, name, change):
    plan = rc.prepare(cli.plan(build_plan(fake, bench)))
    assert rc.compare_controls(plan.before, plan.simulated, plan.expected) == ([], [])
    after = copy.deepcopy(plan.simulated)
    entry_id = plan.lines[0].entry_id
    after["2"][entry_id][name] = change(after["2"][entry_id][name])
    gaps, _impossible = rc.compare_controls(plan.before, after, plan.expected)
    assert any(g.startswith(f"contrôle 2 — écriture {entry_id}") for g in gaps), gaps


# ══════════════════════════════════════════════════════════════════════
# 5. The backup and the restoration
# ══════════════════════════════════════════════════════════════════════


def test_the_backup_is_on_disk_and_verified_when_the_batch_starts(fake, bench, cli,
                                                                  monkeypatch):
    plan_path = cli.plan(build_plan(fake, bench))
    fingerprint = cli.dry(plan_path)[3]
    real_commit, commits, entered = rla.commit, len(fake.commits), []

    def after_the_backup(writes, payloads):
        assert _ops(fake, commits) == []
        (backup,) = cli.backups.iterdir()
        body = json.loads(backup.read_text(encoding="utf-8"))
        assert body["contenu_sha256"] == rla._digest(body["documents"])
        assert {d["chemin"]: d["etag_migration"] for d in body["documents"]} == {
            write.path: payloads[write.path]["etag"] for write in writes}
        entered.append(len(writes))
        return real_commit(writes, payloads)

    monkeypatch.setattr(rla, "commit", after_the_backup)
    code, out, _report = cli.apply(plan_path, fingerprint)
    assert code == 0, out
    assert entered == [len(LINES)]


def test_the_backup_holds_each_entry_whole_and_the_restoration_round_trips(fake, bench, cli):
    for key in LINES:
        _set(fake, _tx(bench["e"][key]), updated_via="web")
    stored = {k: fake.peek(_tx(bench["e"][k])) for k in LINES}
    statement = _statement(fake)
    plan_path = cli.migrate(build_plan(fake, bench))
    migrated = {k: fake.peek(_tx(bench["e"][k])) for k in LINES}
    backup = _backup(cli)
    body = json.loads(backup.read_text(encoding="utf-8"))
    assert body["format"] == rc.BACKUP_FORMAT and body["projet"] == fake.project
    assert body["plan"]["sha256"] == hashlib.sha256(plan_path.read_bytes()).hexdigest()
    assert body["contexte"] == {"periodes": [["2031-01-01", "2031-12-31"]]}
    assert [d["champs"] for d in body["documents"]] == [["category"]] * len(LINES)
    assert {d["ligne"]: rla._untyped(d["avant"]) for d in body["documents"]} == {
        n: stored[k] for n, k in enumerate(LINES, start=1)}
    assert body["documents"][0]["avant"]["date"] == {"$ts": "2031-03-04T00:00:00+00:00"}

    code, out, report, fingerprint = cli.restore(backup)
    assert code == 0, out + report
    assert "Écarts : aucun" in report
    commits = len(fake.commits)
    code, out, report, _fp = cli.restore(backup, fingerprint)
    assert code == 0, out + report
    assert sorted(_ops(fake, commits)) == sorted(("update", path)
                                                 for path in _line_paths(bench))
    digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    for n, key in enumerate(LINES, start=1):
        restored = fake.peek(_tx(bench["e"][key]))
        assert restored["category"] == SOURCE
        assert _kept(restored) == _kept(stored[key])
        # As the pattern restores: nothing trimmed from the trail, fresh stamps.
        assert restored["revisions"][:-1] == migrated[key]["revisions"]
        assert len(restored["revisions"]) == len(stored[key]["revisions"]) + 2
        trail = restored["revisions"][-1]
        assert trail["motif"].startswith("Restauration de la sauvegarde du ")
        assert trail["motif"].endswith(f"— ligne {n}")
        assert trail["changes"] == {"category": [TARGET, SOURCE]}
        assert (trail["via"], trail["source_sha256"]) == ("script", digest)
        assert restored["updated_via"] == "script"
        assert restored["etag"] not in (stored[key]["etag"], migrated[key]["etag"])
    assert _statement(fake) == statement
    assert vai.main() == 0
    code, out, report, _fp = cli.restore(backup)
    assert code == 0 and "« déjà restaurée »" in report and "rien n'est à restaurer" in out


def test_a_restoration_refuses_over_a_later_write(fake, bench, cli):
    cli.migrate(build_plan(fake, bench))
    # A write of the application: it stamps a new etag.
    _set(fake, _tx(bench["e"]["c1"]), description="Écrite après la migration",
         etag="etag-ecriture-ulterieure")
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, _out, report, _fp = cli.restore(_backup(cli))
    assert code == 1
    assert "ne porte pas l'etag de la migration" in report
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot


def test_a_restoration_approved_before_a_later_change_writes_nothing(fake, bench, cli):
    cli.migrate(build_plan(fake, bench))
    backup = _backup(cli)
    _code, _out, _report, fingerprint = cli.restore(backup)
    # Read since the dry run, the migration's etag kept: its update time moved.
    _set(fake, _tx(bench["e"]["c1"]), description="Modifiée après la passe à blanc")
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, report, _fp = cli.restore(backup, fingerprint)
    assert code == 1 and "l'empreinte a changé" in out, out
    assert "Mode : restauration refusée" in report
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot


def test_a_restoration_fingerprint_is_bound_to_its_backup_file(fake, bench, cli):
    """Approved on one backup, a fingerprint restores from no other file —
    not even one saving the same documents."""
    cli.migrate(build_plan(fake, bench))
    backup = _backup(cli)
    _code, _out, _report, fingerprint = cli.restore(backup)
    body = json.loads(backup.read_text(encoding="utf-8"))
    body["cree_le"] = "2031-11-21T09:00:00+00:00"
    other = cli.tmp / "autre-sauvegarde.json"
    other.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, _report, _fp = cli.restore(other, fingerprint)
    assert code == 1 and "l'empreinte a changé" in out, out
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot


def test_a_blocking_control_refuses_the_approved_restoration(fake, bench, cli, monkeypatch):
    cli.migrate(build_plan(fake, bench))
    backup = _backup(cli)
    _invoices_unreadable(monkeypatch)
    code, out, report, fingerprint = cli.restore(backup)
    assert code == 1 and UNREADABLE in report and fingerprint, out + report
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, _report, _fp = cli.restore(backup, fingerprint)
    assert code == 1 and f"Refusé — rien n'a été écrit : {UNREADABLE}" in out, out
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot


def test_a_category_put_back_by_hand_is_not_a_restoration(fake, bench, cli):
    """Every category back at its saved value, but by later writes (new
    etags), without the restoration's trail entry: refused, never read as
    « déjà restaurée »."""
    cli.migrate(build_plan(fake, bench))
    for key in LINES:
        _set(fake, _tx(bench["e"][key]), category=SOURCE, etag=f"etag-a-la-main-{key}")
    code, out, report, _fp = cli.restore(_backup(cli))
    assert code == 1, out + report
    assert "« déjà restaurée »" not in report
    assert report.count("ne porte pas l'etag de la migration") == len(LINES)


def test_a_restoration_refuses_a_category_moved_under_the_migration_etag(fake, bench, cli):
    cli.migrate(build_plan(fake, bench))
    _set(fake, _tx(bench["e"]["c1"]), category="autre")
    code, _out, report, _fp = cli.restore(_backup(cli))
    assert code == 1
    assert "etag de la migration, mais category n'a pas sa valeur" in report


def test_a_restoration_refuses_a_trail_with_no_room_left(fake, bench, cli):
    """The migration keeps room for one restoration; a trail that filled up
    anyway, under the migration's etag, is refused — nothing is trimmed."""
    cli.migrate(build_plan(fake, bench))
    full = [{"at": _d(1, 1), "via": "web", "changes": {}, "n": i}
            for i in range(al._REVISIONS_CAP)]
    _set(fake, _tx(bench["e"]["c1"]), revisions=full)
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, _out, report, _fp = cli.restore(_backup(cli))
    assert code == 1
    assert "ligne 1 : le fil de révisions est illisible ou plein — rien ne se rogne" in report
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot


def test_a_tampered_backup_is_refused(fake, bench, cli):
    cli.migrate(build_plan(fake, bench))
    body = json.loads(_backup(cli).read_text(encoding="utf-8"))
    body["documents"][0]["avant"]["category"] = "loyer"
    tampered = cli.tmp / "alteree-1.json"
    tampered.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    code, out, _report, _fp = cli.restore(tampered)
    assert code == 1 and "sauvegarde altérée : son contenu ne concorde pas" in out
    # Its digest recomputed, a backup still restores the category alone.
    body["documents"][0]["champs"] = ["amount", "category"]
    body["contenu_sha256"] = rla._digest(body["documents"])
    forged = cli.tmp / "alteree-2.json"
    forged.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    code, out, _report, _fp = cli.restore(forged)
    assert code == 1 and "un document n'a pas la forme attendue" in out


# ══════════════════════════════════════════════════════════════════════
# 6. The command line
# ══════════════════════════════════════════════════════════════════════


def test_paths_inside_the_repository_are_refused_before_anything_is_created(fake, bench, cli,
                                                                           tmp_path):
    inside = rla.REPO / "athena" / "plan-interdit.csv"
    plan_path = cli.plan(build_plan(fake, bench))
    for argv in ([inside, "--rapport", tmp_path / "r1.md"],
                 [plan_path, "--rapport", rla.REPO / "rapport-interdit.md"],
                 [plan_path, "--rapport", tmp_path / "r2.md", "--sauvegarde", rla.REPO,
                  "--appliquer", "0" * 16],
                 ["--restaurer", rla.REPO / "sauvegarde-interdite.json", "--rapport",
                  tmp_path / "r3.md"]):
        code, out = cli.run(*argv)
        assert code == 1 and "dans le dépôt (public)" in out, argv
    assert not (rla.REPO / "rapport-interdit.md").exists()
    assert not any((tmp_path / f"r{n}.md").exists() for n in (1, 2, 3))


def test_applying_requires_a_backup_directory_and_a_well_formed_fingerprint(fake, bench, cli):
    plan_path = cli.plan(build_plan(fake, bench))
    code, out = cli.run(plan_path, "--rapport", cli.tmp / "r.md", "--appliquer", "0" * 16)
    assert code == 1 and "--appliquer exige --sauvegarde" in out
    code, out = cli.run(plan_path, "--rapport", cli.tmp / "r.md", "--sauvegarde", cli.backups,
                        "--appliquer", "pas-une-empreinte")
    assert code == 1 and "16 caractères hexadécimaux" in out


# ══════════════════════════════════════════════════════════════════════
# 7. The listing — how the lawyer builds his plan
# ══════════════════════════════════════════════════════════════════════


def _listed_line(out: str, entry_id: str) -> str:
    return next(line for line in out.splitlines() if line.startswith("|") and entry_id in line)


def test_the_listing_filters_by_category_and_payee_and_writes_nothing(fake, bench, cli):
    snapshot, commits = _snapshot(fake), len(fake.commits)
    files = set(cli.tmp.iterdir())
    e = bench["e"]
    code, out = cli.run("--lister", SOURCE)
    assert code == 0, out
    assert all(e[k]["id"] in out for k in ("c1", "c2", "c3", "flat", "o1"))
    assert e["paper"]["id"] not in out and e["income"]["id"] not in out
    assert "Écritures : 5 — 4 verrouillée(s), 0 que le script refuserait." in out
    assert "Projet : fake-project" in out and "Rien n'a été écrit." in out
    # The payee, whatever its case.
    code, out = cli.run("--lister", SOURCE, "--beneficiaire", "ia exemple")
    assert code == 0, out
    assert all(e[k]["id"] in out for k in ("c1", "c2", "o1"))
    assert e["c3"]["id"] not in out and e["flat"]["id"] not in out
    assert "Écritures : 3 — 2 verrouillée(s), 0 que le script refuserait." in out
    code, out = cli.run("--lister", "fournitures")
    assert code == 0 and e["paper"]["id"] in out and e["c1"]["id"] not in out
    code, out = cli.run("--lister", "loyer")
    assert code == 0 and "Aucune écriture." in out
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot
    assert set(cli.tmp.iterdir()) == files


def test_the_listing_says_why_an_entry_is_locked_and_whether_it_would_be_refused(
        fake, bench, cli):
    e = bench["e"]
    reversed_entry = _entry(bench["ops"], "dépense", 777, _d(6, 6), counterparty=AI,
                            category=SOURCE, method="virement")
    _ok(al.reverse_transaction(reversed_entry["id"], "Saisie en double"))
    # Uncleared, yet dated inside the reconciled spring.
    _set(fake, _tx(e["flat"]), status="en_circulation", cleared_date=None, cleared_via="")
    # A DRAFT reconciliation of the operations account locks nothing.
    _ok(al.create_reconciliation(bench["ops"]["id"], _d(6, 30), 0))
    code, out = cli.run("--lister", SOURCE)
    assert code == 0, out
    c1 = _listed_line(out, e["c1"]["id"])
    assert "verrouillée : écriture_verrouillée (compensée)" in c1 and "accepterait" in c1
    assert "2031-03-04" in c1 and "| 2135 |" in c1 and AI in c1
    flat = _listed_line(out, e["flat"]["id"])
    assert "verrouillée : période_verrouillée (période conciliée jusqu'au 2031-05-31)" in flat
    o1 = _listed_line(out, e["o1"]["id"])
    assert "non verrouillée" in o1 and "accepterait" in o1 and "Opérations" in o1
    pair = _listed_line(out, reversed_entry["id"])
    assert "verrouillée : écriture_verrouillée (membre d'une contre-passation)" in pair
    assert "refuserait — membre d'une contre-passation" in pair
    # The correction is no dépense: it is not listed.
    correction_id = fake.peek(_tx(reversed_entry))["reversed_by_id"]
    assert correction_id not in out
    assert "Écritures : 6 — 5 verrouillée(s), 1 que le script refuserait." in out


@pytest.mark.parametrize("fields, said", [
    ({"amount": "21,35"}, "refuserait — montant illisible"),
    ({"date": None}, "refuserait — date illisible"),
])
def test_the_listing_refuses_an_entry_no_plan_could_state(fake, bench, cli, fields, said):
    _set(fake, _tx(bench["e"]["c2"]), **fields)
    code, out = cli.run("--lister", SOURCE)
    assert code == 0, out
    assert said in _listed_line(out, bench["e"]["c2"]["id"])
    plan_lines = out.split(",".join(rc.CSV_COLUMNS), 1)[1]
    assert bench["e"]["c2"]["id"] not in plan_lines
    assert bench["e"]["c1"]["id"] in plan_lines


def test_the_listing_prints_plan_lines_the_dry_run_accepts(fake, bench, cli):
    code, out = cli.run("--lister", SOURCE, "--beneficiaire", "exemple")
    assert code == 0, out
    lines = out.splitlines()
    start = lines.index(",".join(rc.CSV_COLUMNS))
    skeleton = []
    for text in lines[start + 1:]:
        if not text[:1].isdigit():
            break
        skeleton.append(text)
    rows = list(csv.DictReader(io.StringIO("\n".join([lines[start]] + skeleton))))
    assert len(rows) == 5 and {r["categorie_cible"] for r in rows} == {""}
    # The lawyer keeps the usage-billed charges — the flat plan stays — and fills the rest.
    rows = [r for r in rows if r["entry_id"] != bench["e"]["flat"]["id"]]
    for n, row in enumerate(rows, start=1):
        row.update(ligne=str(n), categorie_cible=TARGET, motif=MOTIF)
    code, out, report, _fp = cli.dry(cli.plan(rows))
    assert code == 0, out + report
    assert "Lignes : 4 à appliquer, 0 déjà appliquée(s), 0 refusée(s)." in report
    assert "**ÉCART**" not in report


@pytest.mark.parametrize("argv, said", [
    (["--lister", "logiciels"], "catégorie « logiciels » inconnue"),
    (["--lister", SOURCE, "--beneficiaire", "  "], "--beneficiaire attend un texte non vide"),
    (["--beneficiaire", "exemple"], "--beneficiaire ne sert qu'avec --lister"),
    (["--lister", SOURCE, "--rapport", "rapport.md"], "--lister se lance seul"),
    (["--lister", SOURCE, "plan.csv"], "--lister se lance seul"),
])
def test_the_listing_refuses_what_it_cannot_honour_before_any_read(fake, bench, cli, argv,
                                                                   said):
    fake.reset_logs()
    code, out = cli.run(*argv)
    assert code == 1 and said in out, out
    assert fake.reads == [] and fake.commits == []
