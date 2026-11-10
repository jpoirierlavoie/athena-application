"""``scripts/rectify_admin_entries`` — locked administration entries
rectified OUTSIDE the models, field by field: their TPS/TVQ split, the
supplier's invoice number, the description and the dossier link (the
corrections of the « Comptes payables » verification, the lawyer's decisions
of 2026-11-09).

The bench is built by the REAL models on the shared fake Firestore: a card
and an operations account whose dépenses are cleared and behind a completed
reconciliation each — locked —, five dossiers (one closed) and a model entry
on a third account written by the connector. A plan in the lawyer's JSON
format (a fictional year, invented amounts, names and ids — the repository
is public) names the targets by number, one of them over two entries. These
tests pin what the script writes — the fields that change, the dossier
snapshot the model writes with a link, one trail entry and the stamps,
nothing else —, that it writes ONE TRANSACTION PER ENTRY and stops at the
first refused one, what it refuses and why (a whole number at a time), that
a dry run writes nothing, that a second run finds every number applied, the
controls and the plan's own statements, the fingerprint, the backup and the
restoration.
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
    from models import provenance
    from scripts import reclassify_admin_category as rc
    from scripts import reclassify_admin_ledger as rla
    from scripts import reclassify_as_invoice_receipt as rx
    from scripts import rectify_admin_entries as rae
    from scripts import rectify_registers as rr
    from scripts import verify_admin_integrity as vai
    from scripts import verify_trust_integrity as vti
    from tz import to_mtl

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
TODAY = "2031-11-20T16:00:00+00:00"
D_311 = "4d1e5f60-8888-4888-8888-0000000000a1"
D_342 = "4d1e5f60-8888-4888-8888-0000000000a2"
D_367 = "4d1e5f60-8888-4888-8888-0000000000a3"
D_385 = "4d1e5f60-8888-4888-8888-0000000000a4"
DOSSIERS = {
    D_311: {"file_number": "2031-311", "title": "Exemple c. Démonstration", "status": "actif"},
    D_342: {"file_number": "2031-342", "title": "Exemple c. Illustration", "status": "actif"},
    D_367: {"file_number": "2031-367", "title": "Témoin c. Échantillon", "status": "fermé"},
    D_385: {"file_number": "2031-385", "title": "Modèle c. Gabarit", "status": "actif"},
}
TELECOM_TEXT = ("Facture de juillet 2031 ventilée selon la pièce : TPS et TVQ du forfait "
                "seulement, frais d'activation non taxables.")
DESC_TEXT = "Forfait d'octobre 2031, pièce vérifiée."
#: number → [(bench key, target, motif)]
NUMBERS = {
    "A1": [("split", {"net_amount": 2436, "gst_amount": 122, "qst_amount": 0},
            "Facture IA-73015 : TPS de 1,22 $, aucune TVQ (correction du 2031-11-09)")],
    "A2": [("ref", {"net_amount": 2265, "qst_amount": 226, "supplier_invoice_ref": "HB-20931"},
            "Facture HB-20931 : TVQ facturée (correction du 2031-11-09)")],
    "A3": [("unlink", {"dossier_id": None, "dossier_file_number": None},
            "Débours sans dossier, selon la décision de l'avocat du 2031-11-09")],
    "A4": [("telecom", {"net_amount": 9800, "gst_amount": 435, "qst_amount": 868,
                        "description": TELECOM_TEXT},
            "Taxes du forfait seulement (correction du 2031-11-09)")],
    "A5": [("desc", {"description": DESC_TEXT},
            "Description mise à jour (correction du 2031-11-09)")],
    "A6": [("pair1", {"dossier_id": D_311}, "Honoraires d'un confrère rattachés au dossier 2031-311"),
           ("pair2", {"dossier_id": D_342}, "Honoraires d'un confrère rattachés au dossier 2031-342")],
    "A7": [("closed", {"dossier_id": D_367, "dossier_file_number": "2031-367"},
            "Consultation rattachée au dossier 2031-367")],
}
ENTRY_KEYS = [key for rows in NUMBERS.values() for key, _t, _m in rows]
CONTROLS = {
    "periodes": [{"compte": "carte", "du": "2031-06-01", "au": "2031-10-31",
                  "delta": {"brut": 0, "net": -15, "tps": 11, "tvq": 4}}],
    "ensemble": {"brut": 0, "net": 128, "tps": -37, "tvq": -91},
    "dossiers": {"2031-385": -2440, "2031-311": 19000, "2031-342": 19000, "2031-367": 36217},
}


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
    return {k: v for k, v in doc.items() if k not in rae._WRITTEN}


def _entry(account: dict, amount: int, day: datetime, cleared: datetime, *, category: str,
           counterparty: str, method: str = "carte", **extra) -> dict:
    data = {"account_id": account["id"], "kind": "dépense", "amount": amount,
            "category": category, "method": method, "counterparty": counterparty,
            "date": day, "description": "", "reference": "", "supplier_invoice_ref": "",
            **extra}
    return _ok(al.create_transaction(data, cleared_date=cleared))


def build_bench(fake, set_attr) -> dict:
    _freeze_today(set_attr, TODAY)
    for dossier_id, fields in DOSSIERS.items():
        fake.seed(f"dossiers/{dossier_id}", {"id": dossier_id, **fields})
    fake.seed("expenses/5e2f6a70-9999-4999-8999-000000000001", {
        "id": "5e2f6a70-9999-4999-8999-000000000001", "dossier_id": D_311,
        "date": _d(8, 5), "amount": 19000, "description": "Honoraires d'un confrère (exemple)",
        "invoiced": False, "invoice_id": None})
    card = _ok(al.create_account({"name": "Carte Exemple", "account_type": "carte_crédit"}))
    ops = _ok(al.create_account({"name": "Opérations Exemple", "account_type": "opérations"}))
    perso = _ok(al.create_account({"name": "Carte d'appoint Exemple",
                                   "account_type": "carte_crédit"}))
    e: dict = {}
    e["closed"] = _entry(card, 36217, _d(4, 22), _d(4, 23), category="avocat",
                         counterparty="Cabinet Exemple", net_amount=31500, gst_amount=1575,
                         qst_amount=3142, supplier_invoice_ref="CE-2031-0417", reference="U0001")
    e["split"] = _entry(card, 2558, _d(6, 24), _d(6, 25), category="infonuagique_ia",
                        counterparty="Fournisseur IA Exemple", net_amount=2225,
                        gst_amount=111, qst_amount=222, supplier_invoice_ref="IA-73015",
                        reference="Z0001")
    e["unlink"] = _entry(card, 2440, _d(7, 8), _d(7, 10), category="recherche",
                         counterparty="Registre Exemple", dossier_id=D_385, reference="U0002")
    e["ref"] = _entry(card, 2491, _d(9, 2), _d(9, 2), category="infonuagique_ia",
                      counterparty="Hébergeur Exemple", reference="Z0002")
    e["desc"] = _entry(card, 7077, _d(10, 4), _d(10, 5), category="téléphone",
                       counterparty="Téléphonie Exemple", net_amount=6155, gst_amount=308,
                       qst_amount=614, description="Ancienne description, à confirmer.",
                       reference="U0003", supplier_invoice_ref="TEL-55102")
    e["telecom"] = _entry(ops, 11103, _d(7, 17), _d(7, 17), category="téléphone",
                          counterparty="Téléphonie Exemple", method="virement",
                          net_amount=9657, gst_amount=483, qst_amount=963,
                          supplier_invoice_ref="TEL-55187")
    e["pair1"] = _entry(ops, 19000, _d(8, 5), _d(8, 19), category="avocat",
                        counterparty="Me Exemple", method="virement")
    e["pair2"] = _entry(ops, 19000, _d(8, 5), _d(8, 19), category="avocat",
                        counterparty="Me Exemple", method="virement")
    rec = _ok(al.create_reconciliation(card["id"], _d(10, 20), 36217 + 2558 + 2440 + 2491
                                       + 7077))
    _ok(al.complete_reconciliation(rec["id"], []))
    rec = _ok(al.create_reconciliation(ops["id"], _d(10, 24), -(11103 + 19000 + 19000)))
    _ok(al.complete_reconciliation(rec["id"], []))
    with provenance.writing_via("mcp"):
        model = _entry(perso, 3334, _d(11, 3), _d(11, 4), category="fournitures",
                       counterparty="Papeterie Exemple", net_amount=2900, gst_amount=145,
                       qst_amount=289)
    return {"card": card, "ops": ops, "perso": perso, "e": e, "model": model}


def _none(value):
    return None if value in (None, "") else value


def expected_of(doc: dict) -> dict:
    """What a plan line states of a stored entry, in the plan's terms."""
    trail = doc.get("revisions") or []
    last = trail[-1] if trail else None
    return {
        "account_id": doc["account_id"], "sequence": doc["sequence"],
        "date": doc["date"].date().isoformat(),
        "cleared_date": doc["cleared_date"].date().isoformat(),
        "counterparty": doc["counterparty"], "category": doc["category"],
        "amount": doc["amount"], "net_amount": doc["net_amount"],
        "gst_amount": doc["gst_amount"], "qst_amount": doc["qst_amount"],
        "method": doc["method"], "reference": _none(doc.get("reference")),
        "supplier_invoice_ref": _none(doc.get("supplier_invoice_ref")),
        "description": _none(doc.get("description")), "dossier_id": doc.get("dossier_id"),
        "dossier_file_number": _none(doc.get("dossier_file_number")),
        "status": doc["status"], "lock_reason": "écriture_verrouillée",
        "created_day": to_mtl(doc["created_at"]).date().isoformat(),
        "created_via": _none(doc.get("created_via")),
        "revisions": len(trail),
        "derniere_revision": (None if last is None else
                              {"le": to_mtl(last["at"]).strftime("%Y-%m-%d %H:%M"),
                               "via": last["via"]}),
        "etag": doc["etag"],
    }


def build_plan(fake, bench, numbers=None) -> dict:
    numbers = numbers or NUMBERS
    model = fake.peek(_tx(bench["model"]))
    return {
        "note": "Banc d'essai fictif.",
        "comptes": {
            "carte": {"id": bench["card"]["id"], "solde_affiche_cents": 50783,
                      "plancher": "2031-10-20"},
            "operations": {"id": bench["ops"]["id"], "solde_affiche_cents": -49103,
                           "plancher": "2031-10-24"},
        },
        "dossiers": {k: (dict(v) if k == D_367 else {"file_number": v["file_number"]})
                     for k, v in DOSSIERS.items()},
        "numeros": [{"numero": number, "ecritures": [
            {"ecriture": bench["e"][key]["id"],
             "attendu": expected_of(fake.peek(_tx(bench["e"][key]))),
             "cible": copy.deepcopy(target), "motif": motif}
            for key, target, motif in rows]} for number, rows in numbers.items()],
        "modele": {"ecriture": model["id"], "etag": model["etag"],
                   "attendu": {"account_id": model["account_id"], "sequence": model["sequence"],
                               "amount": 3334, "net_amount": 2900, "gst_amount": 145,
                               "qst_amount": 289, "created_via": "mcp"}},
        "controles": copy.deepcopy(CONTROLS),
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
        code = rae.main([str(a) for a in argv])
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
    return install(monkeypatch, *mods, rla, rx, rc, rae, rr, vai, vti)


@pytest.fixture
def bench(fake, monkeypatch):
    return build_bench(fake, monkeypatch.setattr)


@pytest.fixture
def cli(tmp_path, capsys):
    return Cli(tmp_path, capsys)


def _snapshot(fake) -> dict:
    return {name: fake.peek_collection(name)
            for name in ("admin_transactions", "admin_accounts", "admin_reconciliations",
                         "invoices", "trust_transactions", "counters", "dossiers", "expenses",
                         "documents")}


def _without(snapshot: dict, *paths: str) -> dict:
    out = copy.deepcopy(snapshot)
    for path in paths:
        collection, doc_id = path.split("/")
        out[collection].pop(doc_id, None)
    return out


def _paths(bench, keys=ENTRY_KEYS) -> list:
    return [_tx(bench["e"][k]) for k in keys]


def _statement(fake, account_id=None, first=None, last=None) -> dict:
    rows = [r for r in fake.peek_collection("admin_transactions").values()
            if (account_id is None or r["account_id"] == account_id)
            and (first is None or first <= r["date"].date().isoformat() <= last)]
    return al.results_statement(rows)


def _flat(statement: dict) -> dict:
    return {"brut": statement["total_expenses"], "net": sum(statement["expenses_net"].values()),
            "tps": statement["gst"], "tvq": statement["qst"]}


def _backup(cli) -> pathlib.Path:
    (path,) = list(cli.backups.iterdir())
    return path


# ══════════════════════════════════════════════════════════════════════
# 1. The dry run, then the write
# ══════════════════════════════════════════════════════════════════════


def test_a_dry_run_writes_nothing_and_reports_what_the_model_adds(fake, bench, cli):
    before, commits = _snapshot(fake), len(fake.commits)
    code, out, report, fingerprint = cli.dry(cli.plan(build_plan(fake, bench)))
    assert code == 0, out + report
    assert _ops(fake, commits) == [] and _snapshot(fake) == before
    assert re.fullmatch(r"[0-9a-f]{16}", fingerprint)
    assert ("Numéros : 7 à appliquer, 0 déjà appliqué(s), 0 refusé(s), 0 partiellement "
            "appliqué(s) ; écritures à écrire : 8.") in report
    eight = report.split("### Les huit contrôles de la section 5")[1].split("## 8.")[0]
    assert eight.count("| conforme |") == 8 and "**ÉCART**" not in eight
    assert "**ÉCART**" not in report and "Écarts : aucun" in report
    # The snapshot the model writes with a link, which the plan does not name.
    assert "`dossier_title` : le modèle l'écrit avec `dossier_id`" in report
    assert "`dossier_file_number` : le modèle l'écrit avec `dossier_id`" in report
    assert "pour A6" in report and "règle du modèle" in report
    assert "« sans_taxe »" in report and "« ventiler »" in report
    assert f"--sauvegarde DOSSIER --appliquer {fingerprint}" in out


def test_one_transaction_per_entry_writes_the_target_and_nothing_else(fake, bench, cli):
    stored = {k: fake.peek(_tx(bench["e"][k])) for k in ENTRY_KEYS}
    snapshot = _snapshot(fake)
    # What the script exists for: the model refuses to edit a locked entry.
    entry, errors = al.update_transaction(stored["split"]["id"], {"net_amount": 2436,
                                                                  "gst_amount": 122,
                                                                  "qst_amount": 0})
    assert entry is None and errors
    commits = len(fake.commits)
    plan_path = cli.migrate(build_plan(fake, bench))
    digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    # One commit per entry, each one update of its entry, in plan order.
    records = fake.commits[commits:]
    assert [list(record.ops) for record in records] == [[("update", p)] for p in _paths(bench)]
    snap = {
        "unlink": {"dossier_id": None, "dossier_file_number": "", "dossier_title": ""},
        "pair1": {"dossier_id": D_311, "dossier_file_number": "2031-311",
                  "dossier_title": "Exemple c. Démonstration"},
        "pair2": {"dossier_id": D_342, "dossier_file_number": "2031-342",
                  "dossier_title": "Exemple c. Illustration"},
        "closed": {"dossier_id": D_367, "dossier_file_number": "2031-367",
                   "dossier_title": "Témoin c. Échantillon"},
    }
    for rows in NUMBERS.values():
        for key, target, motif in rows:
            after, before = fake.peek(_tx(bench["e"][key])), stored[key]
            assert _kept(after) == _kept(before), key
            want = {k: ("" if v is None else v) for k, v in target.items()
                    if k != "dossier_id" and k != "dossier_file_number"}
            want.update(snap.get(key, {}))
            for name, value in want.items():
                assert after[name] == value, (key, name)
            changed = {k for k in rae.BUSINESS_FIELDS if after.get(k) != before.get(k)}
            assert changed == set(want) - {k for k, v in want.items() if before.get(k) == v}
            assert after["updated_via"] == "script" and after["etag"] != before["etag"]
            assert after["revisions"][:-1] == (before.get("revisions") or [])
            trail = after["revisions"][-1]
            assert trail["motif"] == motif
            assert (trail["via"], trail["source_sha256"]) == ("script", digest)
            assert trail["changes"] == {k: [before.get(k), after[k]] for k in changed}
            assert trail["updated_at_before"] == before["updated_at"]
            assert al._entry_lock_reason(after, None) == "écriture_verrouillée"
    assert _without(_snapshot(fake), *_paths(bench)) == _without(snapshot, *_paths(bench))
    assert vai.main() == 0


def test_the_results_and_the_dossier_totals_move_as_the_plan_states(fake, bench, cli):
    card, ops = bench["card"]["id"], bench["ops"]["id"]
    before = {"carte": _flat(_statement(fake, card, "2031-06-01", "2031-10-31")),
              "ensemble": _flat(_statement(fake)), "ops": _flat(_statement(fake, ops))}
    cli.migrate(build_plan(fake, bench))
    after = {"carte": _flat(_statement(fake, card, "2031-06-01", "2031-10-31")),
             "ensemble": _flat(_statement(fake)), "ops": _flat(_statement(fake, ops))}
    delta = {s: {k: after[s][k] - before[s][k] for k in after[s]} for s in after}
    assert delta["carte"] == CONTROLS["periodes"][0]["delta"]
    assert delta["ensemble"] == CONTROLS["ensemble"]
    assert delta["ops"] == {"brut": 0, "net": 143, "tps": -48, "tvq": -95}
    rows = list(fake.peek_collection("admin_transactions").values())
    totals = {d["file_number"]: sum(r["amount"] for r in rows if r.get("dossier_id") == k)
              for k, d in DOSSIERS.items()}
    assert totals == {"2031-385": 0, "2031-311": 19000, "2031-342": 19000, "2031-367": 36217}
    # Balances and reconciliations: untouched.
    accounts = fake.peek_collection("admin_accounts")
    assert accounts[card]["ledger_balance"] == -50783
    assert accounts[ops]["ledger_balance"] == -49103


def test_a_second_run_finds_every_number_applied_and_writes_nothing(fake, bench, cli):
    plan_path = cli.migrate(build_plan(fake, bench))
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, report, fingerprint = cli.dry(plan_path)
    assert code == 0, out + report
    assert "Numéros : 0 à appliquer, 7 déjà appliqué(s), 0 refusé(s)" in report
    assert "rien n'est à écrire" in out
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 0 and "Rien à écrire." in report
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot


# ══════════════════════════════════════════════════════════════════════
# 2. What refuses a number — never forced, the others still apply
# ══════════════════════════════════════════════════════════════════════


def test_a_difference_refuses_the_whole_number_and_the_others_still_apply(fake, bench, cli):
    _set(fake, _tx(bench["e"]["pair2"]), counterparty="Me Autre")
    plan = build_plan(fake, bench)
    plan["numeros"][5]["ecritures"][1]["attendu"]["counterparty"] = "Me Exemple"
    pair1, pair2 = (fake.peek(_tx(bench["e"][k])) for k in ("pair1", "pair2"))
    plan_path = cli.plan(plan)
    code, out, report, fingerprint = cli.dry(plan_path)
    # The plan's statements still describe all its lines: they stay held to
    # them, and the base's figures move by the numbers that apply.
    assert "Numéros : 6 à appliquer, 0 déjà appliqué(s), 1 refusé(s)" in report
    assert "`écart:counterparty`" in report and "`numéro_refusé`" in report
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 0, out + report
    assert fake.peek(_tx(bench["e"]["pair1"])) == pair1
    assert fake.peek(_tx(bench["e"]["pair2"])) == pair2
    assert fake.peek(_tx(bench["e"]["split"]))["net_amount"] == 2436


@pytest.mark.parametrize("change, code, numero", [
    (lambda fake, bench: _set(fake, _tx(bench["e"]["split"]), qst_amount=221,
                              net_amount=2226), "écart:net_amount", "A1"),
    (lambda fake, bench: _set(fake, f"dossiers/{D_311}", file_number="2031-312"),
     "dossier_autre", "A6"),
    (lambda fake, bench: _set(fake, f"dossiers/{D_367}", status="actif"), "dossier_autre", "A7"),
    (lambda fake, bench: fake.external_delete(f"dossiers/{D_342}"), "dossier_introuvable", "A6"),
])
def test_each_guard_refuses_its_number(fake, bench, cli, change, code, numero):
    plan = build_plan(fake, bench)
    change(fake, bench)
    _c, _out, report, _fp = cli.dry(cli.plan(plan))
    assert f"`{code}`" in report, report
    assert "1 refusé(s)" in report
    # A refused number still shows every stated value beside the base's.
    assert re.search(rf"\| {numero} — séq\. [0-9]+ \| etag \|", report), report


def test_a_reversal_member_is_refused(fake, bench, cli):
    entry = bench["e"]["desc"]
    _ok(al.reverse_transaction(entry["id"], "Contre-passation d'essai"))
    _c, _out, report, _fp = cli.dry(cli.plan(build_plan(fake, bench)))
    assert "`lien_contre_passation`" in report


def test_an_entry_already_at_its_target_is_recognized_as_done(fake, bench, cli):
    """Rule 2.8 : « un numéro déjà à sa valeur cible est reconnu comme fait,
    non rejoué » — whoever wrote it."""
    plan = build_plan(fake, bench)
    _set(fake, _tx(bench["e"]["desc"]), description=DESC_TEXT)
    stored = fake.peek(_tx(bench["e"]["desc"]))
    plan_path = cli.plan(plan)
    code, out, report, fingerprint = cli.dry(plan_path)
    assert code == 0, out + report
    assert "Numéros : 6 à appliquer, 1 déjà appliqué(s), 0 refusé(s)" in report
    assert "`déjà_la_cible`" in report and "reconnue comme faite" in report
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 0, out + report
    assert fake.peek(_tx(bench["e"]["desc"])) == stored


def test_a_number_written_under_an_earlier_plan_file_is_done_under_the_next(fake, bench, cli):
    """A corrected plan (new bytes, new digest) finds the numbers the first
    one wrote at their target: done, not « état mixte »."""
    plan = build_plan(fake, bench)
    cli.migrate(plan)
    plan["note"] = "Plan corrigé."
    code, out, report, _fp = cli.dry(cli.plan(plan))
    assert code == 0, out + report
    assert "Numéros : 0 à appliquer, 7 déjà appliqué(s), 0 refusé(s)" in report


def test_the_dossier_a_line_unlinks_is_held_to_the_plan(fake, bench, cli):
    plan = build_plan(fake, bench)
    _set(fake, f"dossiers/{D_385}", file_number="2031-386")
    _c, _out, report, _fp = cli.dry(cli.plan(plan))
    assert "`dossier_autre` — le dossier retiré" in report
    assert "1 refusé(s)" in report


def test_a_reconciliation_holding_a_snapshot_refuses_the_entry_it_cites(fake, bench, cli):
    """Rule 2.5 : a reconciliation that cites the entry and holds a field
    outside the model's shape — where a split could be recorded."""
    plan = build_plan(fake, bench)
    entry = bench["e"]["split"]
    recs = fake.peek_collection("admin_reconciliations")
    rec_id = next(k for k, r in recs.items() if r["account_id"] == bench["card"]["id"])
    _set(fake, f"admin_reconciliations/{rec_id}", cleared_transaction_ids=[entry["id"]],
         ventilations={entry["id"]: [2225, 111, 222]})
    _c, _out, report, _fp = cli.dry(cli.plan(plan))
    assert "`conciliation_instantané`" in report
    assert "**Champs des conciliations hors de la forme du modèle** : `ventilations`" in report


def test_a_refused_number_shows_its_target_and_writes_nothing(fake, bench, cli):
    plan = build_plan(fake, bench)
    _set(fake, f"dossiers/{D_367}", status="actif")
    plan_path = cli.plan(plan)
    code, out, report, fingerprint = cli.dry(plan_path)
    assert "A7 — écriture" in report and "cible du plan, non appliquée" in report
    assert "« 2031-367 »" in report.split("cible du plan, non appliquée")[1]
    stored = fake.peek(_tx(bench["e"]["closed"]))
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 0, out + report
    assert fake.peek(_tx(bench["e"]["closed"])) == stored


@pytest.mark.parametrize("edit, said", [
    (lambda p: p["controles"]["periodes"][0]["delta"].update(net=-22), "contrôles énoncés"),
    (lambda p: p["controles"]["dossiers"].update({"2031-311": 18900}), "contrôles énoncés"),
    (lambda p: p["numeros"][4]["ecritures"][0]["cible"].update(
        description="Texte <b>gras</b>"), "que l'enregistrement n'altère pas"),
    (lambda p: p["numeros"][0]["ecritures"][0]["cible"].update(qst_amount=1), "net + TPS + TVQ"),
    (lambda p: p["numeros"][6]["ecritures"][0]["cible"].update(dossier_file_number="2031-368"),
     "dossier_file_number"),
    (lambda p: p["numeros"][0]["ecritures"][0]["cible"].update(amount=1), "inconnu amount"),
    (lambda p: p["numeros"][0]["ecritures"][0]["attendu"].pop("etag"), "manque etag"),
    (lambda p: p["numeros"][0]["ecritures"][0]["attendu"].update(date=None),
     "ne peut pas être null"),
    (lambda p: p["numeros"][1].update(numero="A1"), "une seule fois"),
])
def test_a_malformed_plan_is_refused_before_any_read(fake, bench, cli, edit, said):
    plan = build_plan(fake, bench)
    edit(plan)
    reads, commits = len(fake.reads), len(fake.commits)
    code, out, report, _fp = cli.dry(cli.plan(plan))
    assert code == 1 and "plan mal formé" in out and said in out, out
    assert len(fake.reads) == reads and _ops(fake, commits) == []


def test_a_stale_fingerprint_writes_nothing(fake, bench, cli):
    plan_path = cli.plan(build_plan(fake, bench))
    code, _out, _report, fingerprint = cli.dry(plan_path)
    assert code == 0
    # A stated value moves: the entry is refused, the print changes.
    _set(fake, _tx(bench["e"]["split"]), reference="Z0009")
    snapshot, commits = _snapshot(fake), len(fake.commits)
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 1 and "Refusé — rien n'a été écrit" in out
    assert _ops(fake, commits) == [] and _snapshot(fake) == snapshot
    assert not list(cli.backups.iterdir())


# ══════════════════════════════════════════════════════════════════════
# 3. One transaction per entry: a refusal stops the run, a re-run resumes
# ══════════════════════════════════════════════════════════════════════


def test_an_entry_changed_after_the_read_stops_the_run_in_its_transaction(fake, bench, cli):
    plan_path = cli.plan(build_plan(fake, bench))
    plan = rae.prepare(plan_path)
    assert not plan.blocking and len(plan.writes) == 8
    # Written by someone else between the approved read and its transaction.
    _set(fake, _tx(bench["e"]["unlink"]), description="Écrite entre-temps")
    changed = fake.peek(_tx(bench["e"]["unlink"]))
    commits = len(fake.commits)
    outcomes = rae.apply_all(plan.writes, plan.payloads)
    assert [o for _w, o, _d in outcomes] == [rae.WRITTEN, rae.WRITTEN, rae.ABORTED]
    assert "changé depuis la lecture" in outcomes[-1][2]
    assert [list(r.ops) for r in fake.commits[commits:]] == [
        [("update", p)] for p in _paths(bench, ["split", "ref"])]
    assert fake.peek(_tx(bench["e"]["unlink"])) == changed
    # A re-run sees the two applied and refuses the changed one — the plan is
    # never bent to the base.
    _c, _out, report, _fp = cli.dry(plan_path)
    assert "Numéros : 4 à appliquer, 2 déjà appliqué(s), 1 refusé(s)" in report


def _stop_at(monkeypatch, n: int) -> None:
    """The n-th entry's transaction refuses (a foreign write, a lost
    server) — written nothing; the run stops there."""
    real = rae.apply_entry
    calls = {"n": 0}

    def apply_entry(write, payload, **kwargs):
        calls["n"] += 1
        if calls["n"] == n:
            return rae.ABORTED, "refus simulé"
        return real(write, payload, **kwargs)
    monkeypatch.setattr(rae, "apply_entry", apply_entry)
    # Never monkeypatch.undo(): it would also undo the fake Firestore.
    return lambda: monkeypatch.setattr(rae, "apply_entry", real)


def test_a_stopped_run_is_reported_and_its_backup_restores_what_it_wrote(fake, bench, cli,
                                                                          monkeypatch):
    stored = {k: fake.peek(_tx(bench["e"][k])) for k in ENTRY_KEYS}
    plan_path = cli.plan(build_plan(fake, bench))
    code, out, report, fingerprint = cli.dry(plan_path)
    resume = _stop_at(monkeypatch, 3)
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 1
    assert "ARRÊT à A3" in report and "application partielle : 2 écriture(s) sur 8" in report
    resume()
    for key in ("split", "ref"):
        assert fake.peek(_tx(bench["e"][key])) != stored[key]
    for key in ENTRY_KEYS[2:]:
        assert fake.peek(_tx(bench["e"][key])) == stored[key]
    # The backup of the stopped run restores the two it wrote, and says the
    # others hold their saved values already.
    code, out, text, restore_fp = cli.restore(_backup(cli))
    assert code == 0, out + text
    assert "Rien à restaurer pour :" in text and text.count("porte déjà les valeurs") == 6
    code, out, text, _fp = cli.restore(_backup(cli), restore_fp)
    assert code == 0, out + text
    for key in ENTRY_KEYS:
        assert rae.state_of(fake.peek(_tx(bench["e"][key]))) == rae.state_of(stored[key]), key
    assert vai.main() == 0


def test_a_resumed_run_completes_and_the_section5_figures_read_true(fake, bench, cli,
                                                                     monkeypatch):
    plan_path = cli.plan(build_plan(fake, bench))
    code, out, report, fingerprint = cli.dry(plan_path)
    resume = _stop_at(monkeypatch, 3)
    cli.apply(plan_path, fingerprint)
    resume()
    code, out, report, fingerprint = cli.dry(plan_path)
    assert code == 0, out + report
    assert "Numéros : 5 à appliquer, 2 déjà appliqué(s), 0 refusé(s)" in report
    figures = report.split("### Les figures de la section 5 des instructions")[1].split("###")[0]
    assert "**ÉCART**" not in figures
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 0, out + report
    figures = report.split("### Les figures de la section 5 des instructions")[1].split("###")[0]
    assert "**ÉCART**" not in figures and "Base (après réel)" in figures
    code, out, report, _fp = cli.dry(plan_path)
    assert "Numéros : 0 à appliquer, 7 déjà appliqué(s)" in report
    figures = report.split("### Les figures de la section 5 des instructions")[1].split("###")[0]
    assert "**ÉCART**" not in figures


def test_the_first_backup_of_a_resumed_run_restores_every_entry(fake, bench, cli, monkeypatch):
    """Run 1 stops after two entries, run 2 writes the rest under its own
    backup: the FIRST backup alone restores all of them — an entry a later
    run of the same plan wrote, its last trail entry the plan's, is the
    migration's target."""
    stored = {k: fake.peek(_tx(bench["e"][k])) for k in ENTRY_KEYS}
    plan_path = cli.plan(build_plan(fake, bench))
    _code, _out, _report, fingerprint = cli.dry(plan_path)
    resume = _stop_at(monkeypatch, 3)
    cli.apply(plan_path, fingerprint)
    resume()
    _code, _out, _report, fingerprint = cli.dry(plan_path)
    code, out, report = cli.apply(plan_path, fingerprint)
    assert code == 0, out + report
    first = sorted(cli.backups.iterdir())[0]
    assert len(json.loads(first.read_text(encoding="utf-8"))["documents"]) == 8
    code, out, text, restore_fp = cli.restore(first)
    assert code == 0, out + text
    code, out, text, _fp = cli.restore(first, restore_fp)
    assert code == 0, out + text
    for key in ENTRY_KEYS:
        assert rae.state_of(fake.peek(_tx(bench["e"][key]))) == rae.state_of(stored[key]), key
    assert vai.main() == 0


def test_a_half_applied_number_reads_partially_applied(fake, bench, cli):
    plan_path = cli.plan(build_plan(fake, bench))
    plan = rae.prepare(plan_path)
    six = [w for w in plan.writes if w.doc_id != bench["e"]["pair2"]["id"]][:6]
    assert [o for _w, o, _d in rae.apply_all(six, plan.payloads)] == [rae.WRITTEN] * 6
    _set(fake, _tx(bench["e"]["pair2"]), description="Écrite entre-temps")
    _c, _out, report, _fp = cli.dry(plan_path)
    assert "1 partiellement appliqué(s)" in report
    assert f"| A6 ({rae.PARTIAL}) | `{bench['e']['pair1']['id']}`" in report
    row = next(line for line in report.splitlines()
               if line.startswith(f"| A6 ({rae.PARTIAL}) | `{bench['e']['pair1']['id']}`"))
    assert "| déjà appliquée |" in row and "numéro_refusé" not in row


def test_a_lost_answer_is_reported_written(fake, bench, cli):
    plan = rae.prepare(cli.plan(build_plan(fake, bench)))
    write = plan.writes[0]
    assert rae.apply_entry(write, plan.payloads[write.path]) == (rae.WRITTEN, "")
    commits = len(fake.commits)
    outcome, detail = rae.apply_entry(write, plan.payloads[write.path])
    assert outcome == rae.LOST_BUT_WRITTEN and "réponse perdue" in detail
    assert _ops(fake, commits) == []


def test_an_invariant_broken_inside_the_transaction_writes_nothing(fake, bench, cli):
    plan = rae.prepare(cli.plan(build_plan(fake, bench)))
    write = plan.writes[0]
    payload = {**plan.payloads[write.path], "amount": 1}
    commits = len(fake.commits)
    outcome, detail = rae.apply_entry(write, payload)
    assert outcome == rae.ABORTED and "amount" in detail
    assert _ops(fake, commits) == []


# ══════════════════════════════════════════════════════════════════════
# 4. The controls
# ══════════════════════════════════════════════════════════════════════


def _stray(extra_payloads):
    original = rae._plan_writes

    def planned(plan):
        original(plan)
        for path, payload in extra_payloads(plan).items():
            plan.payloads[path] = {**plan.payloads.get(path, {}), **payload}
    return planned


@pytest.mark.parametrize("extra, control", [
    (lambda plan: {plan.writes[0].path: {"amount": 2559}}, "contrôle 1"),
    (lambda plan: {plan.writes[0].path: {"counterparty": "Autre"}}, "contrôle 2"),
    (lambda plan: {plan.writes[1].path: {"qst_amount": 227, "net_amount": 2264}}, "contrôle 2"),
    (lambda plan: {plan.writes[2].path: {"dossier_id": D_311}}, "contrôle 4"),
    (lambda plan: {f"dossiers/{D_311}": {"title": "Autre"}}, "contrôle 6"),
])
def test_the_controls_catch_a_write_gone_astray(fake, bench, cli, monkeypatch, extra, control):
    monkeypatch.setattr(rae, "_plan_writes", _stray(extra))
    commits = len(fake.commits)
    code, _out, report, _fp = cli.dry(cli.plan(build_plan(fake, bench)))
    assert code == 1, report
    assert control in report
    assert _ops(fake, commits) == []


def test_the_report_holds_the_instructions_figures_beside_the_base(fake, bench, cli):
    code, out, report, _fp = cli.dry(cli.plan(build_plan(fake, bench)))
    assert code == 0, out + report
    section = report.split("### Les figures de la section 5 des instructions")[1]
    section = section.split("## 8.")[0]
    assert "**ÉCART**" not in section
    money = "-24,40" + chr(0xA0) + "$"
    assert f"| dossier 2031-385 | écritures rattachées | {money} | {money} | conforme |" in section


# ══════════════════════════════════════════════════════════════════════
# 5. The backup and the restoration
# ══════════════════════════════════════════════════════════════════════


def test_the_backup_holds_each_entry_whole_and_the_restoration_round_trips(fake, bench, cli):
    stored = {k: fake.peek(_tx(bench["e"][k])) for k in ENTRY_KEYS}
    statement = _statement(fake)
    cli.migrate(build_plan(fake, bench))
    migrated = {k: fake.peek(_tx(bench["e"][k])) for k in ENTRY_KEYS}
    backup = _backup(cli)
    body = json.loads(backup.read_text(encoding="utf-8"))
    assert body["format"] == rae.BACKUP_FORMAT and body["projet"] == fake.project
    assert [d["numero"] for d in body["documents"]] == ["A1", "A2", "A3", "A4", "A5", "A6",
                                                        "A6", "A7"]
    assert body["documents"][2]["champs"] == ["dossier_file_number", "dossier_id",
                                              "dossier_title"]
    assert rla._untyped(body["documents"][0]["avant"]) == stored["split"]
    code, out, report, fingerprint = cli.restore(backup)
    assert code == 0, out + report
    commits = len(fake.commits)
    code, out, report, _fp = cli.restore(backup, fingerprint)
    assert code == 0, out + report
    assert [list(r.ops) for r in fake.commits[commits:]] == [[("update", p)]
                                                             for p in _paths(bench)]
    for key in ENTRY_KEYS:
        restored = fake.peek(_tx(bench["e"][key]))
        assert rae.state_of(restored) == rae.state_of(stored[key]), key
        assert _kept(restored) == _kept(stored[key])
        assert restored["revisions"][:-1] == migrated[key]["revisions"]
        assert restored["revisions"][-1]["motif"].startswith("Restauration de la sauvegarde du ")
    assert _statement(fake) == statement
    assert vai.main() == 0
    code, out, report, _fp = cli.restore(backup)
    assert code == 0 and "« déjà restaurée »" in report


def test_a_restoration_puts_back_an_unlinked_dossier_renamed_since(fake, bench, cli):
    """The restoration puts the SAVED snapshot back — the dossier the entry
    left may have been renamed since: it must still restore, every entry."""
    stored = {k: fake.peek(_tx(bench["e"][k])) for k in ENTRY_KEYS}
    cli.migrate(build_plan(fake, bench))
    _set(fake, f"dossiers/{D_385}", title="Modèle c. Gabarit (renommé)")
    code, out, text, fingerprint = cli.restore(_backup(cli))
    assert code == 0, out + text
    code, out, text, _fp = cli.restore(_backup(cli), fingerprint)
    assert code == 0, out + text
    for key in ENTRY_KEYS:
        assert rae.state_of(fake.peek(_tx(bench["e"][key]))) == rae.state_of(stored[key]), key
    assert fake.peek(_tx(bench["e"]["unlink"]))["dossier_title"] == "Modèle c. Gabarit"


def test_a_restoration_whose_controls_move_exits_1_and_says_so(fake, bench, cli, monkeypatch):
    cli.migrate(build_plan(fake, bench))
    code, out, text, fingerprint = cli.restore(_backup(cli))
    real_after = rae.after_real

    def after_real(scope):
        controls = real_after(scope)
        controls["6"] = {**controls["6"], "invoices/fantome": "x"}
        return controls
    monkeypatch.setattr(rae, "after_real", after_real)
    code, out, text, _fp = cli.restore(_backup(cli), fingerprint)
    assert code == 1
    assert "- Contrôles après réel : contrôle 6 — invoices/fantome" in text


def test_a_restoration_refuses_over_a_later_write(fake, bench, cli):
    cli.migrate(build_plan(fake, bench))
    _set(fake, _tx(bench["e"]["telecom"]), description="Écrite après", etag="etag-ulterieur")
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
