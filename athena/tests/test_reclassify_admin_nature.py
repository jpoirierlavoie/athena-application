"""``scripts/reclassify_admin_nature`` — the nature of administration
entries, and what that nature carries, moved OUTSIDE the models, line by
line (lot E, the lawyer's decisions of 2026-11-08).

The bench is built by the REAL models on the shared fake Firestore: a
client's advance received in the operations account as an « autre recette »
and its transfer to trust, unmarked, both cleared; a supplier's credit on
the card, recorded as an « autre recette » and locked by the card's
reconciliation. A plan in the lawyer's CSV format (a fictional year,
invented amounts, names and ids — the repository is public) names the
targets: an « Avance d'honoraires », the purpose « avances », a
« Remboursement de dépense » with its category and split. These tests pin
what the script writes — the fields that change, one trail entry and the
stamps, nothing else —, what it refuses and why (the model's own rules
among them), that a refusal or a dry run writes nothing, that a second run
finds every line applied, the controls, the fingerprint, the backup and the
restoration.
"""

import copy
import csv
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
    from scripts import reclassify_admin_category as rc
    from scripts import reclassify_admin_ledger as rla
    from scripts import reclassify_admin_nature as rn
    from scripts import reclassify_as_invoice_receipt as rx
    from scripts import rectify_registers as rr
    from scripts import verify_admin_integrity as vai
    from scripts import verify_trust_integrity as vti

from tests._fake_firestore import install  # noqa: E402
from tests._releve import completer  # noqa: E402

UTC = timezone.utc
TODAY = "2031-11-20T16:00:00+00:00"
DOSSIER_ID = "3c9d4e5f-7777-4777-8777-000000000001"
CLIENT_ID = "3c9d4e5f-7777-4777-8777-0000000000c1"
TRUST_ID = "3c9d4e5f-7777-4777-8777-0000000000f1"
ABSENT_ID = "3c9d4e5f-7777-4777-8777-0000000000ee"
MOTIF = "Avance d'un client reçue au compte d'opérations — décision du 2031-11-08"
LINES = ("adv", "send", "credit")


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
    return [op for record in fake.commits[since:] for op in record.ops]


def _kept(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in rn._WRITTEN}


def _entry(account: dict, kind: str, amount: int, day: datetime, *, counterparty: str,
           cleared=None, **extra) -> dict:
    data = {"account_id": account["id"], "kind": kind, "amount": amount,
            "method": "virement", "counterparty": counterparty, "date": day,
            "description": "", "reference": "", "supplier_invoice_ref": "", **extra}
    return _ok(al.create_transaction(data, cleared_date=cleared))


def build_bench(fake, set_attr) -> dict:
    _freeze_today(set_attr, TODAY)
    fake.seed(f"trust_accounts/{TRUST_ID}", {
        "id": TRUST_ID, "name": "Général", "status": "actif", "account_type": "général",
        "book_balance": 0, "bank_balance": 0, "etag": "t0",
    })
    fake.seed(f"dossiers/{DOSSIER_ID}", {
        "id": DOSSIER_ID, "file_number": "2031-009", "title": "Exemple c. Démonstration",
        "client_ids": [CLIENT_ID], "clients": [{"id": CLIENT_ID, "name": "Client Exemple"}],
        "trust_balance": 0, "trust_balance_by_client": {}, "trust_cleared_by_client": {},
    })
    ops = _ok(al.create_account({"name": "Opérations", "account_type": "opérations"}))
    card = _ok(al.create_account({"name": "Carte Exemple", "account_type": "carte_crédit"}))
    e: dict = {}
    e["adv"] = _entry(ops, "recette_autre", 100000, _d(6, 5), counterparty="Client Exemple",
                      cleared=_d(6, 6), dossier_id=DOSSIER_ID)
    e["send"] = _entry(ops, "virement_interne_sortant", 100000, _d(6, 11),
                       counterparty="Compte en fidéicommis", cleared=_d(6, 12))
    e["credit"] = _entry(card, "recette_autre", 10502, _d(9, 2),
                         counterparty="Fournisseur IA Exemple", cleared=_d(9, 3), method="carte")
    rec = _ok(al.create_reconciliation(card["id"], _d(9, 30), -10502))
    _ok(completer(al, rec["id"], []))
    _ok(trust.create_transaction({
        "account_id": TRUST_ID, "direction": "recette", "amount": 100000,
        "purpose": "avance_honoraires", "method": "virement", "counterparty": "Client Exemple",
        "dossier_id": DOSSIER_ID, "client_id": CLIENT_ID, "date": _d(6, 11),
        "description": "", "reference": "REF-EX",
    }))
    return {"ops": ops, "card": card, "e": e}


_TARGETS = {
    "adv": {"nature_cible": "avance_honoraires"},
    "send": {"nature_cible": "virement_interne_sortant", "objet_cible": "avances"},
    "credit": {"nature_cible": "remboursement_depense", "categorie_cible": "infonuagique_ia",
               "net_cible": "10002", "tps_cible": "500", "tvq_cible": "0"},
}


def _row(n: int, doc: dict, key: str = "", **cells) -> dict:
    """One plan line in the lawyer's format, its expectations read off the
    stored entry, its target the bench's."""
    row = {"ligne": str(n), "entry_id": doc["id"], "date": doc["date"].date().isoformat(),
           "montant_cents": str(doc["amount"]), "nature_actuelle": doc["kind"],
           "categorie_actuelle": doc.get("category") or "",
           "objet_actuel": doc.get("transfer_purpose") or "",
           "nature_cible": "", "categorie_cible": "", "net_cible": "", "tps_cible": "",
           "tvq_cible": "", "objet_cible": "", "motif": MOTIF}
    row.update(_TARGETS.get(key, {}))
    assert not set(cells) - set(row), cells
    row.update(cells)
    return row


def build_plan(fake, bench, keys=LINES) -> list:
    return [_row(n, fake.peek(_tx(bench["e"][key])), key) for n, key in enumerate(keys, start=1)]


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
            writer = csv.DictWriter(fh, fieldnames=rn.CSV_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        return path

    def run(self, *argv) -> tuple[int, str]:
        code = rn.main([str(a) for a in argv])
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
    return install(monkeypatch, *mods, rla, rx, rc, rn, rr, vai, vti)


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


def _statement(fake) -> dict:
    return al.results_statement(list(fake.peek_collection("admin_transactions").values()))


def _backup(cli) -> pathlib.Path:
    (path,) = list(cli.backups.iterdir())
    return path


# ══════════════════════════════════════════════════════════════════════
# 1. The dry run, then the write
# ══════════════════════════════════════════════════════════════════════


def test_a_dry_run_writes_nothing_and_the_report_says_everything(fake, bench, cli):
    before, commits = _snapshot(fake), len(fake.commits)
    code, out, report, fingerprint = cli.dry(cli.plan(build_plan(fake, bench)))
    assert code == 0, out + report
    assert _ops(fake, commits) == [] and _snapshot(fake) == before
    assert re.fullmatch(r"[0-9a-f]{16}", fingerprint)
    assert "Lignes : 3 à appliquer, 0 déjà appliquée(s), 0 refusée(s)." in report
    assert "**ÉCART**" not in report and "Écarts : aucun" in report
    assert "Avance d'honoraires" in report and "Remboursement de dépense" in report
    assert "Avances à verser au fidéicommis" in report
    assert f"--sauvegarde DOSSIER --appliquer {fingerprint}" in out


def test_the_write_is_the_target_its_trail_and_stamps_and_nothing_else(fake, bench, cli):
    stored = {k: fake.peek(_tx(bench["e"][k])) for k in LINES}
    snapshot = _snapshot(fake)
    # What the script exists for: the model refuses to edit a locked entry.
    entry, errors = al.update_transaction(stored["credit"]["id"], {"kind": "remboursement_depense"})
    assert entry is None and errors
    commits = len(fake.commits)
    plan_path = cli.migrate(build_plan(fake, bench))
    digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    assert sorted(_ops(fake, commits)) == sorted(("update", p) for p in _line_paths(bench))
    expected = {
        "adv": {"kind": ["recette_autre", "avance_honoraires"]},
        "send": {"transfer_purpose": ["", "avances"]},
        "credit": {"kind": ["recette_autre", "remboursement_depense"],
                   "category": [None, "infonuagique_ia"],
                   "net_amount": [0, 10002], "gst_amount": [0, 500]},
    }
    for n, key in enumerate(LINES, start=1):
        after, before = fake.peek(_tx(bench["e"][key])), stored[key]
        assert _kept(after) == _kept(before), key
        for field_name, (_old, new) in expected[key].items():
            assert after[field_name] == new, (key, field_name)
        assert after["updated_via"] == "script" and after["etag"] != before["etag"]
        assert after["revisions"][:-1] == before["revisions"]
        trail = after["revisions"][-1]
        assert trail["motif"] == f"{MOTIF} — ligne {n}"
        assert (trail["via"], trail["source_sha256"]) == ("script", digest)
        assert trail["changes"] == expected[key]
        assert trail["updated_at_before"] == before["updated_at"]
    assert _without(_snapshot(fake), *_line_paths(bench)) == _without(snapshot,
                                                                       *_line_paths(bench))
    assert vai.main() == 0


def test_the_results_move_as_the_targets_say(fake, bench, cli):
    before = _statement(fake)
    cli.migrate(build_plan(fake, bench))
    after = _statement(fake)
    # The advance and the credit leave the revenue; the credit lowers the AI costs.
    assert after["revenue"]["recette_autre"] == before["revenue"]["recette_autre"] - 110502
    assert after["expenses_net"]["infonuagique_ia"] == -10002
    assert after["expenses"]["infonuagique_ia"] == -10502
    assert (after["gst"], after["qst"]) == (before["gst"] - 500, before["qst"])
    # No balance moves: the advance in, its transfer out; the card's credit.
    assert fake.peek(f"admin_accounts/{bench['ops']['id']}")["ledger_balance"] == 0
    assert fake.peek(f"admin_accounts/{bench['card']['id']}")["ledger_balance"] == 10502


# ══════════════════════════════════════════════════════════════════════
# 2. The refusals — each alone, never forced
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("key, cells, refusal", [
    ("adv", {"nature_cible": "prélèvement"}, "passage_non_permis"),
    ("adv", {"nature_cible": "correction"}, "nature_cible_invalide"),
    ("adv", {"nature_cible": "avance_honoraires", "objet_cible": "avances"},
     "objet_virement_interdit"),
    ("adv", {"nature_cible": "avance_honoraires", "categorie_cible": "loyer"},
     "catégorie_interdite"),
    ("credit", {"net_cible": "", "tps_cible": "", "tvq_cible": ""}, "ventilation_requise"),
    ("credit", {"categorie_cible": ""}, "catégorie_requise"),
    ("credit", {"categorie_cible": "logiciels"}, "catégorie_invalide"),
    ("credit", {"tps_cible": "499"}, "ventilation_invalide"),
    ("credit", {"nature_cible": "recette_autre", "categorie_cible": ""},
     "ventilation_hors_dépense"),
    ("send", {"objet_cible": "épargne"}, "objet_virement_invalide"),
    ("adv", {"nature_actuelle": "apport"}, "état_changé:nature"),
    ("adv", {"montant_cents": "100001"}, "état_changé:montant"),
    ("adv", {"date": "2031-06-04"}, "état_changé:date"),
    ("send", {"objet_actuel": "avances"}, "état_changé:objet"),
    ("credit", {"categorie_actuelle": "loyer"}, "état_changé:catégorie"),
    ("adv", {"entry_id": ABSENT_ID}, "écriture_introuvable"),
])
def test_a_line_the_plan_misstates_or_the_model_refuses_is_refused_alone(
        fake, bench, cli, key, cells, refusal):
    rows = build_plan(fake, bench)
    rows[LINES.index(key)].update(cells)
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, _out, report, _fp = cli.dry(cli.plan(rows))
    assert code == 0, report
    assert "Lignes : 2 à appliquer, 0 déjà appliquée(s), 1 refusée(s)." in report, report
    assert f"`{refusal}`" in report
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot


@pytest.mark.parametrize("fields, refusal", [
    ({"status": "annulée"}, "annulée"),
    ({"invoice_id": "inv-1"}, "liée_facture"),
    ({"trust_transaction_id": "t-1"}, "liée_fideicommis"),
    ({"reversed_by_id": "r-1"}, "lien_contre_passation"),
    ({"revisions": [{"at": _d(1, 1), "via": "web", "changes": {}}] * 24}, "fil_plein"),
])
def test_every_guard_refuses_its_line_alone(fake, bench, cli, fields, refusal):
    _set(fake, _tx(bench["e"]["adv"]), **fields)
    code, _out, report, _fp = cli.dry(cli.plan(build_plan(fake, bench)))
    assert code == 0, report
    assert "Lignes : 2 à appliquer, 0 déjà appliquée(s), 1 refusée(s)." in report
    assert f"`{refusal}`" in report


def test_an_original_only_its_correction_names_is_a_pair_member(fake, bench, cli):
    other = _entry(bench["ops"], "recette_autre", 777, _d(7, 1), counterparty="Client Exemple")
    stored = fake.peek(_tx(other))
    _ok(al.reverse_transaction(other["id"], "Saisie en double"))
    fake.external_write(_tx(other), stored)            # one-sided: as before the reversal
    rows = build_plan(fake, bench) + [_row(4, fake.peek(_tx(other)), "adv")]
    code, _out, report, _fp = cli.dry(cli.plan(rows))
    assert code == 0, report
    assert "Lignes : 3 à appliquer, 0 déjà appliquée(s), 1 refusée(s)." in report
    assert "`lien_contre_passation`" in report


def test_a_duplicated_entry_refuses_both_its_lines(fake, bench, cli):
    rows = build_plan(fake, bench)
    rows.append(dict(rows[0], ligne="4"))
    code, _out, report, _fp = cli.dry(cli.plan(rows))
    assert code == 0, report
    assert "Lignes : 2 à appliquer, 0 déjà appliquée(s), 2 refusée(s)." in report
    assert report.count("`entrée_en_double`") == 2


def test_an_entry_already_at_its_target_without_the_plan_is_refused(fake, bench, cli):
    rows = build_plan(fake, bench)
    _set(fake, _tx(bench["e"]["send"]), transfer_purpose="avances")
    rows[1].update(objet_actuel="avances")
    code, _out, report, _fp = cli.dry(cli.plan(rows))
    assert code == 0, report
    assert "`déjà_la_cible`" in report


@pytest.mark.parametrize("cells, said", [
    ({"net_cible": "10002", "tps_cible": "", "tvq_cible": ""}, "les trois ou aucune"),
    ({"net_cible": "100,02", "tps_cible": "5", "tvq_cible": "0"}, "un entier de cents"),
    ({"nature_cible": ""}, "nature_cible : valeur requise"),
    ({"motif": ""}, "une valeur non vide est attendue"),
])
def test_a_malformed_plan_is_refused_before_any_read(fake, bench, cli, cells, said):
    rows = build_plan(fake, bench)
    rows[2].update(cells)
    plan_path = cli.plan(rows)
    fake.reset_logs()
    code, out, _report, _fp = cli.dry(plan_path)
    assert code == 1 and "plan mal formé" in out and said in out, out
    assert fake.reads == [] and fake.commits == []


# ══════════════════════════════════════════════════════════════════════
# 3. The fingerprint, the backup gate, the second run
# ══════════════════════════════════════════════════════════════════════


def test_a_stale_fingerprint_writes_nothing(fake, bench, cli):
    plan_path = cli.plan(build_plan(fake, bench))
    _code, _out, _report, fingerprint = cli.dry(plan_path)
    _set(fake, _tx(bench["e"]["adv"]), description="Note ajoutée après la passe à blanc")
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, _report = cli.apply(plan_path, fingerprint)
    assert code == 1 and "l'empreinte a changé" in out
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot
    assert not list(cli.backups.iterdir())


def test_a_blocking_control_refuses_even_the_fingerprint_it_printed(fake, bench, cli,
                                                                    monkeypatch):
    real = rla.read_store

    def read_store():
        store = real()
        store.invoices, store.invoices_error = None, "ServiceUnavailable"
        return store

    monkeypatch.setattr(rla, "read_store", read_store)
    plan_path = cli.plan(build_plan(fake, bench))
    code, out, report, fingerprint = cli.dry(plan_path)
    said = "contrôle 5 impossible à calculer (factures illisibles (ServiceUnavailable))"
    assert code == 1 and said in report and fingerprint, out + report
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, _report = cli.apply(plan_path, fingerprint)
    assert code == 1 and f"Refusé — rien n'a été écrit : {said}" in out, out
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot
    assert not list(cli.backups.iterdir())


def test_a_backup_that_cannot_be_written_writes_nothing(fake, bench, cli, monkeypatch):
    plan_path = cli.plan(build_plan(fake, bench))
    _code, _out, _report, fingerprint = cli.dry(plan_path)

    def full_disk(_directory, _plan):
        raise OSError("disque plein")

    monkeypatch.setattr(rn, "write_backup", full_disk)
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, _report = cli.apply(plan_path, fingerprint)
    assert code == 1 and "sauvegarde impossible (OSError) — rien n'a été écrit" in out, out
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot


def test_a_second_run_finds_every_line_applied_and_writes_nothing(fake, bench, cli):
    plan_path = cli.migrate(build_plan(fake, bench))
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, report, fingerprint = cli.dry(plan_path)
    assert code == 0, report
    assert "Lignes : 0 à appliquer, 3 déjà appliquée(s), 0 refusée(s)." in report
    code, out, _report = cli.apply(plan_path, fingerprint)
    assert code == 0 and "Rien à écrire." in out
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot


# ══════════════════════════════════════════════════════════════════════
# 4. The controls
# ══════════════════════════════════════════════════════════════════════


def _stray(extra_payloads):
    original = rn._plan_writes

    def planned(plan):
        original(plan)
        for path, payload in extra_payloads(plan).items():
            plan.payloads[path] = {**plan.payloads.get(path, {}), **payload}
    return planned


@pytest.mark.parametrize("extra, control", [
    (lambda plan: {plan.lines[0].path: {"amount": 100001}}, "contrôle 1"),
    (lambda plan: {plan.lines[0].path: {"counterparty": "Autre"}}, "contrôle 2"),
    (lambda plan: {plan.lines[2].path: {"category": "loyer"}}, "contrôle 2"),
    (lambda plan: {plan.lines[2].path: {"gst_amount": 0, "net_amount": 10502}}, "contrôle 2"),
])
def test_the_controls_catch_a_write_gone_astray(fake, bench, cli, monkeypatch, extra, control):
    monkeypatch.setattr(rn, "_plan_writes", _stray(extra))
    commits = len(fake.commits)
    code, _out, report, _fp = cli.dry(cli.plan(build_plan(fake, bench)))
    assert code == 1, report
    assert control in report
    assert _ops(fake, commits) == []


# ══════════════════════════════════════════════════════════════════════
# 5. The backup and the restoration
# ══════════════════════════════════════════════════════════════════════


def test_the_backup_holds_each_entry_whole_and_the_restoration_round_trips(fake, bench, cli):
    stored = {k: fake.peek(_tx(bench["e"][k])) for k in LINES}
    statement = _statement(fake)
    cli.migrate(build_plan(fake, bench))
    migrated = {k: fake.peek(_tx(bench["e"][k])) for k in LINES}
    backup = _backup(cli)
    body = json.loads(backup.read_text(encoding="utf-8"))
    assert body["format"] == rn.BACKUP_FORMAT and body["projet"] == fake.project
    assert [d["champs"] for d in body["documents"]] == [
        ["kind"], ["transfer_purpose"], ["category", "gst_amount", "kind", "net_amount"]]
    code, out, report, fingerprint = cli.restore(backup)
    assert code == 0, out + report
    commits = len(fake.commits)
    code, out, report, _fp = cli.restore(backup, fingerprint)
    assert code == 0, out + report
    assert sorted(_ops(fake, commits)) == sorted(("update", p) for p in _line_paths(bench))
    for key in LINES:
        restored = fake.peek(_tx(bench["e"][key]))
        assert rn.state_of(restored) == rn.state_of(stored[key]), key
        assert _kept(restored) == _kept(stored[key])
        assert restored["revisions"][:-1] == migrated[key]["revisions"]
        assert restored["revisions"][-1]["motif"].startswith("Restauration de la sauvegarde du ")
    assert _statement(fake) == statement
    assert vai.main() == 0
    code, out, report, _fp = cli.restore(backup)
    assert code == 0 and "« déjà restaurée »" in report


def test_a_restoration_refuses_over_a_later_write(fake, bench, cli):
    cli.migrate(build_plan(fake, bench))
    _set(fake, _tx(bench["e"]["adv"]), description="Écrite après", etag="etag-ulterieur")
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, _out, report, _fp = cli.restore(_backup(cli))
    assert code == 1
    assert "ne porte pas l'etag de la migration" in report
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot


def test_a_card_payment_leg_never_changes_its_nature(fake, bench, cli):
    """A card payment, a correction and an invoice receipt are written by
    their own paths: refused before any target is judged."""
    report: dict = {}
    _ok(al.create_card_payment(bench["ops"]["id"], bench["card"]["id"], 5000,
                               _d(10, 1), "virement", _report_out=report))
    leg = fake.peek(_tx(report["legs"][0]))
    rows = build_plan(fake, bench) + [_row(4, leg, "adv")]
    code, _out, text, _fp = cli.dry(cli.plan(rows))
    assert code == 0, text
    assert "`nature_structurelle`" in text


def test_a_tampered_backup_is_refused(fake, bench, cli):
    cli.migrate(build_plan(fake, bench))
    body = json.loads(_backup(cli).read_text(encoding="utf-8"))
    # A field outside what the migration writes — its « after » consistent.
    body["documents"][0]["champs"] = ["amount"]
    body["documents"][0]["apres"] = {"amount": 1}
    body["contenu_sha256"] = rla._digest(body["documents"])
    forged = cli.tmp / "alteree.json"
    forged.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    code, out, _report, _fp = cli.restore(forged)
    assert code == 1 and "un document n'a pas la forme attendue" in out


def test_paths_inside_the_repository_are_refused(fake, bench, cli, tmp_path):
    plan_path = cli.plan(build_plan(fake, bench))
    code, out = cli.run(plan_path, "--rapport", rla.REPO / "rapport-interdit.md")
    assert code == 1 and "dans le dépôt (public)" in out
    assert not (rla.REPO / "rapport-interdit.md").exists()
