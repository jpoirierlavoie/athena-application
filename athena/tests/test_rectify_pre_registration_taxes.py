"""``scripts/rectify_pre_registration_taxes`` — the TPS/TVQ of the purchases
dated before the firm's TPS/TVQ registration, zeroed OUTSIDE the models:
net brought up to the gross amount (the lawyer's decision of 2026-11-10).

The bench is built by the REAL models on the shared fake Firestore: an
unreconciled account of purchases paid personally (dépenses with TPS/TVQ
before and after the cut-off, one on the cut-off day, one the day before,
one with a trail, one without tax, a supplier's refund), and a card whose
reconciliation locks a taxed purchase. A plan in the lawyer's JSON format (a
fictional year, invented amounts and names — the repository is public)
names the rule and states the set's totals. These tests pin what the script
writes — the split, one trail entry, the stamps, nothing else, one
transaction per entry —, the rule's boundary (UTC days), what blocks the
run (a total that differs, any refused entry of the set), the controls, the
effect on the financial statements, the fingerprint, the backup and the
restoration.
"""

import copy
import hashlib
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
    from scripts import reclassify_admin_category as rc
    from scripts import reclassify_admin_ledger as rla
    from scripts import reclassify_as_invoice_receipt as rx
    from scripts import rectify_admin_entries as rae
    from scripts import rectify_pre_registration_taxes as rpt
    from scripts import rectify_registers as rr
    from scripts import verify_admin_integrity as vai
    from scripts import verify_trust_integrity as vti

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
TODAY = "2031-11-20T16:00:00+00:00"
CUTOFF = "2031-07-01"
MOTIF = ("Achat antérieur à l'inscription fictive du 1er juillet 2031 : TPS et TVQ non "
         "récupérables, net porté au montant (décision fictive)")
#: The entries the rule must zero, in day order.
SET = ("r1", "p1", "p2", "c1", "p3")
NBSP = chr(0xA0)


def _d(m: int, d: int, y: int = 2031, hour: int = 0) -> datetime:
    return datetime(y, m, d, hour, tzinfo=UTC)


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
    return {k: v for k, v in doc.items() if k not in rpt._WRITTEN}


def _entry(account: dict, kind: str, amount: int, day: datetime, split=None, *,
           category: str, counterparty: str, cleared=None, method: str = "carte") -> dict:
    data = {"account_id": account["id"], "kind": kind, "amount": amount, "category": category,
            "method": method, "counterparty": counterparty, "date": day, "description": "",
            "reference": "", "supplier_invoice_ref": ""}
    if split:
        data.update(zip(("net_amount", "gst_amount", "qst_amount"), split))
    return _ok(al.create_transaction(data, cleared_date=cleared))


def build_bench(fake, set_attr) -> dict:
    _freeze_today(set_attr, TODAY)
    perso = _ok(al.create_account({"name": "Carte d'appoint Exemple",
                                   "account_type": "carte_crédit"}))
    card = _ok(al.create_account({"name": "Carte Exemple", "account_type": "carte_crédit"}))
    e: dict = {}
    e["r1"] = _entry(perso, "remboursement_depense", 639, _d(2, 3), (556, 28, 55),
                     category="internet", counterparty="Fournisseur Internet Exemple")
    e["p1"] = _entry(perso, "dépense", 9778, _d(2, 9), (8505, 425, 848), category="internet",
                     counterparty="Fournisseur Internet Exemple")
    e["p2"] = _entry(perso, "dépense", 159010, _d(3, 26), (138300, 6915, 13795),
                     category="formation", counterparty="Formation Exemple")
    e["n1"] = _entry(perso, "dépense", 2470, _d(4, 14), category="fournitures",
                     counterparty="Papeterie Exemple")
    e["c1"] = _entry(card, "dépense", 4864, _d(5, 11), (4230, 212, 422), category="abonnements",
                     counterparty="Logiciel Exemple", cleared=_d(5, 12))
    e["p3"] = _entry(perso, "dépense", 3173, _d(6, 30), (2760, 138, 275), category="fournitures",
                     counterparty="Papeterie Exemple")
    for text in ("Fournitures de bureau", "Fournitures de bureau (facture)"):
        _ok(al.update_transaction(e["p3"]["id"], {"description": text}))
    e["b1"] = _entry(perso, "dépense", 8963, _d(7, 1), (7795, 390, 778), category="téléphone",
                     counterparty="Téléphonie Exemple")
    e["a1"] = _entry(perso, "dépense", 2236, _d(7, 27), (1945, 97, 194),
                     category="abonnements", counterparty="Logiciel Exemple")
    rec = _ok(al.create_reconciliation(card["id"], _d(6, 30), 4864))
    _ok(al.complete_reconciliation(rec["id"], []))
    return {"perso": perso, "card": card, "e": e}


def stated_of(fake, bench, keys=SET) -> dict:
    """The set's totals per kind, summed from the stored entries."""
    out = {k: dict.fromkeys(rpt.FIGURES, 0) for k in ("dépense", "remboursement_depense")}
    for key in keys:
        doc = fake.peek(_tx(bench["e"][key]))
        slot = out[doc["kind"]]
        slot["lignes"] += 1
        slot["brut"] += doc["amount"]
        slot["net"] += doc["net_amount"]
        slot["tps"] += doc["gst_amount"]
        slot["tvq"] += doc["qst_amount"]
    return out


def build_plan(fake, bench, keys=SET) -> dict:
    return {"avant": CUTOFF, "natures": ["dépense", "remboursement_depense"], "motif": MOTIF,
            "note": "Banc d'essai fictif.", "attendu": stated_of(fake, bench, keys)}


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
        code = rpt.main([str(a) for a in argv])
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
    return install(monkeypatch, *mods, rla, rx, rc, rae, rpt, rr, vai, vti)


@pytest.fixture
def bench(fake, monkeypatch):
    return build_bench(fake, monkeypatch.setattr)


@pytest.fixture
def cli(tmp_path, capsys):
    return Cli(tmp_path, capsys)


def _snapshot(fake) -> dict:
    return {name: fake.peek_collection(name)
            for name in ("admin_transactions", "admin_accounts", "admin_reconciliations",
                         "invoices", "trust_transactions", "counters", "dossiers", "expenses")}


def _without(snapshot: dict, *paths: str) -> dict:
    out = copy.deepcopy(snapshot)
    for path in paths:
        collection, doc_id = path.split("/")
        out[collection].pop(doc_id, None)
    return out


def _paths(bench, keys=SET) -> list:
    return [_tx(bench["e"][k]) for k in keys]


def _taxes_before_cutoff(fake) -> tuple:
    rows = [r for r in fake.peek_collection("admin_transactions").values()
            if r["date"].date().isoformat() < CUTOFF]
    statement = al.results_statement(rows)
    return statement["gst"], statement["qst"], statement["total_expenses"]


def _backup(cli) -> pathlib.Path:
    (path,) = list(cli.backups.iterdir())
    return path


def _stop_at(monkeypatch, n: int):
    """The n-th entry's transaction refuses — written nothing; the run stops
    there. Returns the undo (never monkeypatch.undo(): it would undo the
    fake Firestore too)."""
    real = rpt.apply_entry
    calls = {"n": 0}

    def apply_entry(write, payload, **kwargs):
        calls["n"] += 1
        if calls["n"] == n:
            return rpt.ABORTED, "refus simulé"
        return real(write, payload, **kwargs)
    monkeypatch.setattr(rpt, "apply_entry", apply_entry)
    return lambda: monkeypatch.setattr(rpt, "apply_entry", real)


# ══════════════════════════════════════════════════════════════════════
# 1. The dry run, then the write
# ══════════════════════════════════════════════════════════════════════


def test_a_dry_run_writes_nothing_and_finds_the_stated_set(fake, bench, cli):
    before, commits = _snapshot(fake), len(fake.commits)
    code, out, report, fingerprint = cli.dry(cli.plan(build_plan(fake, bench)))
    assert code == 0, out + report
    assert _ops(fake, commits) == [] and _snapshot(fake) == before
    assert re.fullmatch(r"[0-9a-f]{16}", fingerprint)
    assert ("Ensemble : 5 écriture(s) — 5 à appliquer, 0 déjà appliquée(s), 0 refusée(s) ; "
            "écritures à écrire : 5.") in report
    assert "**ÉCART**" not in report and "Écarts (simulé) : aucun" in report
    assert "Contrôle 8 : conforme." in report
    # Control 8 gates the run: its title never calls it merely reported.
    assert "## 10. Effet sur les états financiers (contrôle 8)" in report
    assert "(contrôle 8, rapporté)" not in report
    assert "les contrôles 1 à 8, simulés, sont conformes" in report
    assert f"--sauvegarde DOSSIER --appliquer {fingerprint}" in out


def test_one_transaction_per_entry_zeroes_the_taxes_and_nothing_else(fake, bench, cli):
    stored = {k: fake.peek(_tx(bench["e"][k])) for k in bench["e"]}
    snapshot = _snapshot(fake)
    locks = {k: al._entry_lock_reason(stored[k], None) for k in SET}
    commits = len(fake.commits)
    plan_path = cli.migrate(build_plan(fake, bench))
    digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    # One commit per entry, each one update of its entry, in day order.
    assert [list(r.ops) for r in fake.commits[commits:]] == [[("update", p)]
                                                             for p in _paths(bench)]
    for key in SET:
        after, before = fake.peek(_tx(bench["e"][key])), stored[key]
        assert _kept(after) == _kept(before), key
        assert (after["net_amount"], after["gst_amount"], after["qst_amount"]) == (
            before["amount"], 0, 0), key
        assert after["updated_via"] == "script" and after["etag"] != before["etag"]
        assert after["revisions"][:-1] == (before.get("revisions") or [])
        trail = after["revisions"][-1]
        assert (trail["motif"], trail["via"], trail["source_sha256"]) == (MOTIF, "script", digest)
        assert trail["changes"] == {k: [before[k], after[k]] for k in rpt.SPLIT
                                    if before[k] != after[k]}
        assert al._entry_lock_reason(after, None) == locks[key]
    assert locks["c1"] == "écriture_verrouillée" and locks["p1"] is None
    # Outside the rule: untaxed, on the cut-off day, after it — untouched.
    for key in ("n1", "b1", "a1"):
        assert fake.peek(_tx(bench["e"][key])) == stored[key], key
    assert _without(_snapshot(fake), *_paths(bench)) == _without(snapshot, *_paths(bench))
    assert vai.main() == 0


def test_no_tax_is_left_before_the_cutoff_and_none_moves_after(fake, bench, cli):
    after_cutoff = [r for r in fake.peek_collection("admin_transactions").values()
                    if r["date"].date().isoformat() >= CUTOFF]
    statement_after = al.results_statement(after_cutoff)
    gst, qst, gross = _taxes_before_cutoff(fake)
    assert (gst, qst) == (7690 - 28, 15340 - 55)
    cli.migrate(build_plan(fake, bench))
    assert _taxes_before_cutoff(fake) == (0, 0, gross)
    after_cutoff = [r for r in fake.peek_collection("admin_transactions").values()
                    if r["date"].date().isoformat() >= CUTOFF]
    assert al.results_statement(after_cutoff) == statement_after
    refund = fake.peek(_tx(bench["e"]["r1"]))
    assert (refund["net_amount"], refund["gst_amount"], refund["qst_amount"]) == (639, 0, 0)


def test_the_boundary_is_the_utc_day_strictly_before_the_cutoff(fake, bench, cli):
    """2031-07-01 at midnight UTC is still 2031-06-30 in Montréal: judged by
    the UTC day, the cut-off day keeps its taxes; the day before loses them."""
    stored = fake.peek(_tx(bench["e"]["b1"]))
    cli.migrate(build_plan(fake, bench))
    assert fake.peek(_tx(bench["e"]["b1"])) == stored
    assert fake.peek(_tx(bench["e"]["p3"]))["gst_amount"] == 0


def test_a_second_run_finds_every_entry_applied_and_writes_nothing(fake, bench, cli):
    plan_path = cli.migrate(build_plan(fake, bench))
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, report, fingerprint = cli.dry(plan_path)
    assert code == 0, out + report
    assert "Ensemble : 5 écriture(s) — 0 à appliquer, 5 déjà appliquée(s), 0 refusée(s)" in report
    figures = report.split("## 2. Contrôle 7 — ce que le plan énonce")[1].split("## 3.")[0]
    assert "**ÉCART**" not in figures
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 0 and "Rien à écrire." in report
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot


def test_a_changed_plan_file_after_a_write_blocks(fake, bench, cli):
    plan = build_plan(fake, bench)
    cli.migrate(plan)
    plan["note"] = "Plan modifié après l'application."
    code, out, report, _fp = cli.dry(cli.plan(plan))
    assert code == 1 and "le plan ne se plie pas à la base" in report


# ══════════════════════════════════════════════════════════════════════
# 2. What blocks — never forced
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("kind, deltas", [
    ("dépense", {"lignes": 1}),
    ("dépense", {"tps": -1, "net": 1}),          # net + TPS + TVQ = gross still holds
    ("dépense", {"tvq": 1, "net": -1}),
    ("remboursement_depense", {"brut": 5, "net": 5}),
])
def test_a_stated_total_that_differs_blocks(fake, bench, cli, kind, deltas):
    plan = build_plan(fake, bench)
    for name, delta in deltas.items():
        plan["attendu"][kind][name] += delta
    commits = len(fake.commits)
    code, out, report, _fp = cli.dry(cli.plan(plan))
    assert code == 1 and f"« attendu.{kind}." in report, report
    assert _ops(fake, commits) == []


@pytest.mark.parametrize("edit, said", [
    (lambda p: p.update(natures=["dépense", "recette_autre"]), "« natures »"),
    (lambda p: p["attendu"].pop("remboursement_depense"), "« attendu »"),
    (lambda p: p.update(avant="2031-13-01"), "avant"),
    (lambda p: p.update(motif="Texte <b>gras</b>"), "que l'enregistrement n'altère pas"),
    (lambda p: p["attendu"]["dépense"].update(net=1), "net + TPS + TVQ doit égaler le brut"),
    (lambda p: p.update(inconnu=1), "inconnu"),
])
def test_a_malformed_plan_is_refused_before_any_read(fake, bench, cli, edit, said):
    plan = build_plan(fake, bench)
    edit(plan)
    reads, commits = len(fake.reads), len(fake.commits)
    code, out, report, _fp = cli.dry(cli.plan(plan))
    assert code == 1 and "plan mal formé" in out and said in out, out
    assert len(fake.reads) == reads and _ops(fake, commits) == []


def _reverse(fake, bench):
    entry = bench["e"]["p1"]
    _ok(al.reverse_transaction(entry["id"], "Contre-passation d'essai"))


def _doubtful_entry(fake, bench) -> None:
    """A taxed purchase stored at 01:00 UTC on the cut-off day — still the
    day before in Montréal (a console edit stores local time): its day is
    ambiguous. The model writes midnight; the hour comes after."""
    entry = _entry(bench["perso"], "dépense", 2282, _d(7, 1), (1985, 99, 198),
                   category="fournitures", counterparty="Papeterie Exemple")
    _set(fake, _tx(entry), date=_d(7, 1, hour=1))


@pytest.mark.parametrize("change, code", [
    (_reverse, "lien_contre_passation"),
    (lambda fake, bench: _set(fake, _tx(bench["e"]["p1"]), invoice_id="facture-exemple"),
     "liée_facture"),
    (lambda fake, bench: _set(fake, _tx(bench["e"]["p1"]), trust_transaction_id="ft-exemple"),
     "liée_fideicommis"),
    (lambda fake, bench: _set(fake, _tx(bench["e"]["p1"]), status="annulée"), "annulée"),
    (lambda fake, bench: _set(fake, _tx(bench["e"]["p1"]),
                              revisions=[{"at": _d(1, 1), "via": "web", "changes": {}}] * 24),
     "fil_plein"),
    (lambda fake, bench: _set(fake, _tx(bench["e"]["p1"]), gst_amount="425"),
     "ventilation_illisible"),
    (lambda fake, bench: _set(fake, _tx(bench["e"]["p1"]), date=_d(2, 9, hour=12)),
     "date_hors_minuit"),
    (lambda fake, bench: _set(fake, _tx(bench["e"]["p1"]), direction="recette"),
     "sens_incohérent"),
    (lambda fake, bench: _set(fake, _tx(bench["e"]["p1"]), category="inconnue"),
     "catégorie_invalide"),
    (lambda fake, bench: _set(fake, _tx(bench["e"]["p1"]), qst_amount=847),
     "ventilation_inexacte"),
    (lambda fake, bench: _set(fake, _tx(bench["e"]["p1"]), amount="9778"), "montant_illisible"),
    (lambda fake, bench: _set(fake, _tx(bench["e"]["p1"]), date=None), "date_illisible"),
    (lambda fake, bench: _set(fake, _tx(bench["e"]["p1"]), revisions=5), "fil_illisible"),
    (lambda fake, bench: _cite_with_snapshot(fake, bench, "c1"), "conciliation_instantané"),
    (_doubtful_entry, "date_hors_minuit"),
])
def test_any_refused_entry_of_the_set_blocks_the_run(fake, bench, cli, change, code):
    plan = build_plan(fake, bench)
    change(fake, bench)
    # The plan states the set as it now stands, so only the refusal blocks —
    # unless a figure is unreadable or inexact: no plan can state it, and the
    # totals differ too.
    if code not in ("ventilation_illisible", "ventilation_inexacte", "montant_illisible"):
        plan["attendu"] = rpt.figures_of(rpt.derive(*_world_spec(plan)), tuple(plan["natures"]))
    commits = len(fake.commits)
    plan_path = cli.plan(plan)
    rc_, out, report, fingerprint = cli.dry(plan_path)
    assert rc_ == 1, report
    assert f"`{code}`" in report and "refusée(s) — le plan couvre tout l'ensemble" in report
    assert "Rien n'a été écrit. Lever le motif du refus" in report
    code_, out, report = cli.apply(plan_path, fingerprint)
    assert code_ == 1 and "Refusé — rien n'a été écrit" in out
    assert _ops(fake, commits) == [] and not list(cli.backups.iterdir())


def _cite_with_snapshot(fake, bench, key: str) -> None:
    """The card's reconciliation cites the entry and holds a field outside
    the model's shape — where a split could be recorded."""
    entry = bench["e"][key]
    recs = fake.peek_collection("admin_reconciliations")
    rec_id = next(k for k, r in recs.items() if r["account_id"] == entry["account_id"])
    _set(fake, f"admin_reconciliations/{rec_id}", cleared_transaction_ids=[entry["id"]],
         ventilations={entry["id"]: [1, 2, 3]})


def test_a_reversal_refuses_both_its_original_and_its_correction(fake, bench, cli):
    plan = build_plan(fake, bench)
    _reverse(fake, bench)
    plan["attendu"] = rpt.figures_of(rpt.derive(*_world_spec(plan)), tuple(plan["natures"]))
    code, _out, report, _fp = cli.dry(cli.plan(plan))
    assert code == 1
    assert "`lien_contre_passation`" in report and "`correction`" in report


def _world_spec(plan: dict):
    """``(world, spec, sha, floors)`` for :func:`rpt.derive`, the sha that
    of a plan never written (no trail carries it)."""
    world = rae.read_world()
    return world, rpt.parse_plan(plan), "0" * 64, rae._floors(world)


def test_an_entry_added_after_the_dry_run_refuses_the_application(fake, bench, cli):
    plan_path = cli.plan(build_plan(fake, bench))
    code, out, report, fingerprint = cli.dry(plan_path)
    assert code == 0, out + report
    _entry(bench["perso"], "dépense", 1512, _d(6, 6), (1315, 66, 131), category="fournitures",
           counterparty="Papeterie Exemple")
    commits = len(fake.commits)
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 1 and "l'empreinte a changé" in report
    assert _ops(fake, commits) == []


def test_an_entry_added_during_the_run_is_caught_by_control_4(fake, bench, cli, monkeypatch):
    plan_path = cli.plan(build_plan(fake, bench))
    code, out, report, fingerprint = cli.dry(plan_path)
    real = rpt.apply_all

    def apply_all(writes, payloads, **kwargs):
        outcomes = real(writes, payloads, **kwargs)
        _entry(bench["perso"], "dépense", 1512, _d(6, 6), (1315, 66, 131),
               category="fournitures", counterparty="Papeterie Exemple")
        return outcomes
    monkeypatch.setattr(rpt, "apply_all", apply_all)
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 1
    assert "contrôle 4 — la règle trouve encore 1 écriture(s)" in report


# ══════════════════════════════════════════════════════════════════════
# 3. One transaction per entry: a stop, a resume, the guards
# ══════════════════════════════════════════════════════════════════════


def test_a_stopped_run_resumes_and_its_first_backup_restores_every_entry(fake, bench, cli,
                                                                          monkeypatch):
    stored = {k: fake.peek(_tx(bench["e"][k])) for k in SET}
    plan_path = cli.plan(build_plan(fake, bench))
    _code, _out, _report, fingerprint = cli.dry(plan_path)
    resume = _stop_at(monkeypatch, 3)
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 1 and "ARRÊT à L3" in report
    assert "application partielle : 2 écriture(s) sur 5" in report
    statements = report.split("## 10. Effet sur les états financiers")[1].split("## 11.")[0]
    assert "**ÉCART**" not in statements and "Contrôle 8 : conforme." in statements
    resume()
    # The two written read their « before » split from the trail: the set is
    # still the one the plan states.
    code, out, report, fingerprint = cli.dry(plan_path)
    assert code == 0, out + report
    assert "Ensemble : 5 écriture(s) — 3 à appliquer, 2 déjà appliquée(s), 0 refusée(s)" in report
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 0, out + report
    first = sorted(cli.backups.iterdir())[0]
    code, out, text, restore_fp = cli.restore(first)
    assert code == 0, out + text
    code, out, text, _fp = cli.restore(first, restore_fp)
    assert code == 0, out + text
    for key in SET:
        assert rpt.split_of(fake.peek(_tx(bench["e"][key]))) == rpt.split_of(stored[key]), key
    assert vai.main() == 0


def test_an_entry_changed_after_the_read_stops_the_run_in_its_transaction(fake, bench, cli):
    plan = rpt.prepare(cli.plan(build_plan(fake, bench)))
    assert not plan.blocking and len(plan.writes) == 5
    _set(fake, _tx(bench["e"]["p2"]), description="Écrite entre-temps")
    commits = len(fake.commits)
    outcomes = rpt.apply_all(plan.writes, plan.payloads)
    assert [o for _w, o, _d in outcomes] == [rpt.WRITTEN, rpt.WRITTEN, rpt.ABORTED]
    assert "changé depuis la lecture" in outcomes[-1][2]
    assert len(fake.commits) - commits == 2


def test_a_lost_answer_is_reported_written(fake, bench, cli):
    plan = rpt.prepare(cli.plan(build_plan(fake, bench)))
    write = plan.writes[0]
    assert rpt.apply_entry(write, plan.payloads[write.path]) == (rpt.WRITTEN, "")
    commits = len(fake.commits)
    outcome, detail = rpt.apply_entry(write, plan.payloads[write.path])
    assert outcome == rpt.LOST_BUT_WRITTEN and "réponse perdue" in detail
    assert _ops(fake, commits) == []


@pytest.mark.parametrize("stray", [{"amount": 1}, {"gst_amount": 2, "net_amount": 637}])
def test_an_invariant_broken_inside_the_transaction_writes_nothing(fake, bench, cli, stray):
    plan = rpt.prepare(cli.plan(build_plan(fake, bench)))
    write = plan.writes[0]
    commits = len(fake.commits)
    outcome, _detail = rpt.apply_entry(write, {**plan.payloads[write.path], **stray})
    assert outcome == rpt.ABORTED
    assert _ops(fake, commits) == []


def test_taxes_back_on_an_applied_entry_is_a_mixed_state_that_blocks(fake, bench, cli):
    plan_path = cli.migrate(build_plan(fake, bench))
    _set(fake, _tx(bench["e"]["p1"]), net_amount=8505, gst_amount=425, qst_amount=848)
    code, _out, report, _fp = cli.dry(plan_path)
    assert code == 1 and "`état_mixte`" in report


# ══════════════════════════════════════════════════════════════════════
# 4. The controls, and the statements
# ══════════════════════════════════════════════════════════════════════


def _stray(extra_payloads):
    original = rpt._plan_writes

    def planned(plan):
        original(plan)
        for path, payload in extra_payloads(plan).items():
            plan.payloads[path] = {**plan.payloads.get(path, {}), **payload}
    return planned


@pytest.mark.parametrize("extra, control", [
    (lambda plan: {plan.writes[0].path: {"amount": 640}}, "contrôle 1"),
    (lambda plan: {plan.writes[1].path: {"counterparty": "Autre"}}, "contrôle 2"),
    (lambda plan: {plan.writes[1].path: {"gst_amount": 1, "net_amount": 9777}}, "contrôle 2"),
    (lambda plan: {plan.writes[1].path: {"gst_amount": 1, "net_amount": 9777}}, "contrôle 3"),
    (lambda plan: {plan.writes[1].path: {"gst_amount": 1, "net_amount": 9777}}, "contrôle 4"),
    (lambda plan: {plan.writes[1].path: {"gst_amount": 1, "net_amount": 9777}}, "contrôle 8"),
    (lambda plan: {plan.writes[1].path: {"status": "compensée", "cleared_date": _d(2, 12)}},
     "contrôle 5"),
    (lambda plan: {f"admin_accounts/{plan.writes[0].before['account_id']}": {"name": "Autre"}},
     "contrôle 6"),
])
def test_the_controls_catch_a_write_gone_astray(fake, bench, cli, monkeypatch, extra, control):
    monkeypatch.setattr(rpt, "_plan_writes", _stray(extra))
    commits = len(fake.commits)
    code, _out, report, _fp = cli.dry(cli.plan(build_plan(fake, bench)))
    assert code == 1, report
    assert control in report
    assert _ops(fake, commits) == []


def test_the_statements_move_as_the_written_entries_imply(fake, bench, cli):
    code, out, report, _fp = cli.dry(cli.plan(build_plan(fake, bench)))
    assert code == 0, out + report
    section = report.split("## 10. Effet sur les états financiers")[1].split("## 11.")[0]
    assert "#### année 2031" in section and "#### 2e trimestre 2031" in section
    assert "#### 3e trimestre 2031" in section
    assert "**ÉCART**" not in section and "Indisponible" not in section
    # TPS credits fall by the purchases' TPS less the refund's (76,90 $ less 0,28 $).
    year = section.split("#### année 2031")[1].split("####")[0]
    money = "-76,62" + NBSP + "$"
    assert "| CTI (crédits de TPS) |" in year and f"| {money} | {money} | conforme |" in year
    # The purchases now read « sans TPS ni TVQ ventilées », the refund never.
    assert "| 4 | 4 | conforme |" in year


def test_the_lock_section_says_aucun_for_an_unlocked_entry(fake, bench, cli):
    _code, _out, report, _fp = cli.dry(cli.plan(build_plan(fake, bench)))
    section = report.split("## 8. Verrou et conciliations")[1].split("## 9.")[0]
    assert "aucun (4)" in section and "écriture_verrouillée (1)" in section
    assert "None" not in section


@pytest.fixture
def broken_invoice(fake):
    """An invoice whose money field is not an int: the statements cannot
    compute — and nothing else in the run reads it as a figure."""
    fake.seed("invoices/facture-illisible", {"id": "facture-illisible", "status": "envoyée",
                                             "invoice_number": "2031-F999",
                                             "subtotal_fees": "100"})


def test_statements_that_cannot_be_computed_block_the_run(fake, bench, cli, broken_invoice):
    plan_path = cli.plan(build_plan(fake, bench))
    code, out, report, fingerprint = cli.dry(plan_path)
    assert code == 1
    assert "contrôle 8 (2031) — état indisponible" in report
    assert "Contrôles 1 à 8, simulés : " in out and "conformes" not in out.split(
        "Contrôles 1 à 8, simulés : ")[1].splitlines()[0]
    commits = len(fake.commits)
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 1 and _ops(fake, commits) == []


def test_unreadable_trust_accounts_or_time_entries_change_no_judged_figure(fake, bench):
    """The two fail-soft reads feed only the statements' memo sections
    (trust funds, unbilled work), which control 8 does not measure."""
    world, extra = rpt.read_all()
    cutoff = date.fromisoformat(CUTOFF)
    judged = rpt.statement_figures(world, extra, cutoff)
    assert rpt.statement_figures(world, rpt.Extra(None, None), cutoff) == judged


def test_a_plan_entry_with_a_nearly_full_trail_is_still_done_on_the_next_run(fake, bench, cli):
    trail = [{"at": _d(1, 1), "via": "web", "changes": {}}] * 23
    _set(fake, _tx(bench["e"]["p1"]), revisions=trail)
    plan_path = cli.migrate(build_plan(fake, bench))
    assert len(fake.peek(_tx(bench["e"]["p1"]))["revisions"]) == 24
    code, out, report, _fp = cli.dry(plan_path)
    assert code == 0, out + report
    assert "0 à appliquer, 5 déjà appliquée(s), 0 refusée(s)" in report


def test_an_unreadable_trail_outside_the_set_never_stops_the_run(fake, bench, cli):
    _set(fake, _tx(bench["e"]["a1"]), revisions=5)
    code, out, report, _fp = cli.dry(cli.plan(build_plan(fake, bench)))
    assert code == 0, out + report


def test_an_entry_zeroed_outside_the_plan_leaves_the_set_and_is_listed(fake, bench, cli):
    plan = build_plan(fake, bench)
    _ok(al.update_transaction(bench["e"]["p3"]["id"], {"net_amount": 3173, "gst_amount": 0,
                                                       "qst_amount": 0}))
    code, _out, report, _fp = cli.dry(cli.plan(plan))
    assert code == 1 and "« attendu.dépense.lignes » : le plan énonce 4, la base 3" in report
    outside = report.split("## 3. Hors de la règle")[1].split("## 4.")[0]
    assert "taxes retirées hors de ce plan" in outside and "Papeterie Exemple" in outside


def test_the_entries_outside_the_rule_are_listed_for_memory(fake, bench, cli):
    _entry(bench["perso"], "remboursement_depense", 1477, _d(7, 25), (1285, 64, 128),
           category="internet", counterparty="Fournisseur Internet Exemple")
    code, out, report, _fp = cli.dry(cli.plan(build_plan(fake, bench)))
    assert code == 0, out + report
    outside = report.split("## 3. Hors de la règle")[1].split("## 4.")[0]
    assert "Téléphonie Exemple" in outside and "hors de la règle" in outside
    assert "remboursement daté après la date : à examiner" in outside
    refunds = report.split("## 4. Remboursements")[1].split("## 5.")[0]
    assert "achats du même fournisseur dans l'ensemble : L2" in refunds


# ══════════════════════════════════════════════════════════════════════
# 5. The backup and the restoration
# ══════════════════════════════════════════════════════════════════════


def test_the_backup_holds_each_entry_whole_and_the_restoration_round_trips(fake, bench, cli):
    stored = {k: fake.peek(_tx(bench["e"][k])) for k in SET}
    cli.migrate(build_plan(fake, bench))
    migrated = {k: fake.peek(_tx(bench["e"][k])) for k in SET}
    backup = _backup(cli)
    body = json.loads(backup.read_text(encoding="utf-8"))
    assert body["format"] == rpt.BACKUP_FORMAT and body["projet"] == fake.project
    assert [d["ligne"] for d in body["documents"]] == ["L1", "L2", "L3", "L4", "L5"]
    assert rla._untyped(body["documents"][0]["avant"]) == stored["r1"]
    code, out, report, fingerprint = cli.restore(backup)
    assert code == 0, out + report
    commits = len(fake.commits)
    code, out, report, _fp = cli.restore(backup, fingerprint)
    assert code == 0, out + report
    assert [list(r.ops) for r in fake.commits[commits:]] == [[("update", p)]
                                                             for p in _paths(bench)]
    for key in SET:
        restored = fake.peek(_tx(bench["e"][key]))
        assert rpt.split_of(restored) == rpt.split_of(stored[key]), key
        assert _kept(restored) == _kept(stored[key])
        assert restored["revisions"][:-1] == migrated[key]["revisions"]
        assert restored["revisions"][-1]["motif"].startswith("Restauration de la sauvegarde du ")
    assert vai.main() == 0
    code, out, report, _fp = cli.restore(backup)
    assert code == 0 and "« déjà restaurée »" in report


def test_a_restoration_refuses_an_entry_reversed_since_the_migration(fake, bench, cli):
    """Restored on one side only, a reversal pair would no longer net."""
    cli.migrate(build_plan(fake, bench))
    _reverse(fake, bench)
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, _out, report, fingerprint = cli.restore(_backup(cli))
    assert code == 1
    assert "a été contre-passée, annulée ou liée depuis la migration" in report
    code, _out, report, _fp = cli.restore(_backup(cli), fingerprint or "0" * 16)
    assert code == 1
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot


def test_a_stopped_run_restores_what_it_wrote_though_an_unwritten_entry_changed(
        fake, bench, cli, monkeypatch):
    stored = {k: fake.peek(_tx(bench["e"][k])) for k in SET}
    plan_path = cli.plan(build_plan(fake, bench))
    _code, _out, _report, fingerprint = cli.dry(plan_path)
    resume = _stop_at(monkeypatch, 3)
    cli.apply(plan_path, fingerprint)
    resume()
    # The entry the run stopped at changes afterwards (corrected by hand).
    _ok(al.update_transaction(bench["e"]["p2"]["id"], {"description": "Corrigée à la main"}))
    code, out, text, restore_fp = cli.restore(_backup(cli))
    assert code == 0, out + text
    assert text.count("jamais écrite par la migration") == 3
    code, out, text, _fp = cli.restore(_backup(cli), restore_fp)
    assert code == 0, out + text
    for key in ("r1", "p1"):
        assert rpt.split_of(fake.peek(_tx(bench["e"][key]))) == rpt.split_of(stored[key]), key


def test_a_deleted_entry_leaves_the_others_restorable(fake, bench, cli):
    stored = {k: fake.peek(_tx(bench["e"][k])) for k in SET}
    cli.migrate(build_plan(fake, bench))
    _ok(al.delete_transaction(bench["e"]["p3"]["id"]))
    code, out, text, fingerprint = cli.restore(_backup(cli))
    assert code == 0, out + text
    assert "supprimée depuis — rien à restaurer" in text
    code, out, text, _fp = cli.restore(_backup(cli), fingerprint)
    assert code == 0, out + text
    for key in ("r1", "p1", "p2", "c1"):
        assert rpt.split_of(fake.peek(_tx(bench["e"][key]))) == rpt.split_of(stored[key]), key


def test_a_restoration_refuses_over_a_later_write(fake, bench, cli):
    cli.migrate(build_plan(fake, bench))
    _set(fake, _tx(bench["e"]["p2"]), net_amount=159009, gst_amount=1, qst_amount=0,
         etag="etag-ulterieur")
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, _out, report, _fp = cli.restore(_backup(cli))
    assert code == 1
    assert "une écriture l'a suivie — rien ne se restaure par-dessus" in report
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot


def test_a_tampered_backup_is_refused(fake, bench, cli):
    cli.migrate(build_plan(fake, bench))
    body = json.loads(_backup(cli).read_text(encoding="utf-8"))
    body["documents"][0]["champs"] = ["amount"]
    body["documents"][0]["apres"] = {"amount": 1}
    body["contenu_sha256"] = rla._digest(body["documents"])
    forged = cli.tmp / "alteree.json"
    forged.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    code, out, _report, _fp = cli.restore(forged)
    assert code == 1 and "un document n'a pas la forme attendue" in out


def test_paths_inside_the_repository_are_refused(fake, bench, cli):
    plan_path = cli.plan(build_plan(fake, bench))
    code, out = cli.run(plan_path, "--rapport", rla.REPO / "rapport-interdit.md")
    assert code == 1 and "dans le dépôt (public)" in out
    assert not (rla.REPO / "rapport-interdit.md").exists()
