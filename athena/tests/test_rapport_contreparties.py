"""The read-only report of past payees (scripts/rapport_contreparties.py).

It reads both registers and the contacts in one read-only transaction on
``tests/_fake_firestore.py``. The tests check that it:

* keeps the payments to third parties, groups spellings, counts reversed
  entries apart and leaves corrections out;
* sets aside ONE leading title (« Me », « L'hon. ») and counts a name with
  nothing to compare;
* finds contacts by name, legal name or trade name, and flags the roles the
  forms will not suggest;
* writes a semicolon-separated, fully quoted CSV for French Excel, with
  formulas neutralised;
* writes nothing on a failed read or inside the public repository;
* prints no name.

Every name here is invented.
"""

import csv
import io
import os
import pathlib
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
    from models import partie as pm
    from models import trust
    from scripts import rapport_contreparties as rc

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
SUPPLIER = "Papeterie Exemple inc."
BAILIFF_FIRM = "Huissiers Fictifs"
CANARY = "Zéphyrin Canari"
BOM = "\ufeff"


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, *_fake_modules())


def _admin(tx_id: str, kind: str, name: str, day: int, **over) -> dict:
    row = {
        "id": tx_id, "account_id": "ops1", "kind": kind, "counterparty": name,
        "date": datetime(2031, 3, day, tzinfo=UTC), "amount": 1000 + day,
        "category": "fournitures" if kind in rc.ADMIN_KINDS else None,
        "reversed_by_id": None,
    }
    row.update(over)
    return row


def _trust(tx_id: str, purpose: str, name: str, day: int) -> dict:
    return {"id": tx_id, "purpose": purpose, "counterparty": name,
            "date": datetime(2031, 4, day, tzinfo=UTC), "reversed_by_id": None}


def _contact(pid: str, role: str, **over) -> dict:
    doc = pm._default_doc()
    doc.update({"id": pid, "contact_role": role, **over})
    return doc


@pytest.fixture
def ledger(db):
    for row in (
        _admin("a1", "dépense", SUPPLIER, 2),
        _admin("a2", "dépense", "PAPETERIE EXEMPLE INC", 5),
        _admin("a3", "dépense", SUPPLIER, 9),
        _admin("a4", "remboursement_depense", SUPPLIER, 12),
        _admin("a5", "dépense", SUPPLIER, 14, reversed_by_id="a6"),
        _admin("a6", "correction", SUPPLIER, 15),
        _admin("a7", "encaissement_facture", "Client Fictif", 16),
        _admin("a8", "dépense", "Me Prénom Avocat", 17),
        _admin("a9", "dépense", "Commerce Enseigne", 18),
        _admin("a10", "dépense", "Client Fictif", 19),
        _admin("a11", "dépense", "=CMD|calc", 20),
        _admin("a12", "prélèvement", CANARY, 21),
        _admin("a13", "prélèvement", CANARY, 22),
        _admin("a14", "remise_tps_tvq", "Revenu Québec", 23),
        _admin("a15", "dépense", "L'hon. Juge Fictif", 24),
        _admin("a16", "dépense", "M & M Plomberie", 25),
        _admin("a17", "dépense", "Plomberie", 26),
        _admin("a18", "dépense", "—", 27),
        _admin("a19", "dépense", "Fournisseur X;=1+1;", 28),
    ):
        db.seed(f"admin_transactions/{row['id']}", row)
    for row in (
        _trust("t1", "déboursé_tiers", BAILIFF_FIRM, 3),
        _trust("t2", "déboursé_tiers", BAILIFF_FIRM, 8),
        _trust("t3", "avance_honoraires", "Client Fictif", 9),
    ):
        db.seed(f"trust_transactions/{row['id']}", row)
    for doc in (
        _contact("p1", "avocat_adverse", type="individual", prefix="Me",
                 first_name="Prénom", last_name="Avocat"),
        _contact("p2", "fournisseur", type="organization",
                 organization_name="Société Légale inc.", trade_name="Commerce Enseigne"),
        _contact("p3", "client", type="organization",
                 organization_name="Client Fictif"),
        _contact("p4", "expert", type="individual", prefix="L'hon.",
                 first_name="Juge", last_name="Fictif"),
    ):
        db.seed(f"parties/{doc['id']}", doc)
    return db


def _rows():
    return rc.payee_rows(*rc.read_snapshot())


def _by_name(rows: list[dict]) -> dict:
    return {r["Nom"]: r for r in rows}


def test_the_payees_are_grouped_counted_and_kept_to_third_parties(ledger):
    rows, skipped = _rows()
    by = _by_name(rows)
    paper = by[SUPPLIER]
    # Four standing entries (3 dépenses + 1 refund); the reversed one apart;
    # the correction and the encaissement nowhere.
    assert paper["Écritures"] == 4 and paper["Contre-passées"] == 1
    assert paper["Variantes"] == "PAPETERIE EXEMPLE INC (1)"
    assert paper["Types"] == ("Administration — dépense (3) | "
                              "Administration — remboursement_depense (1)")
    assert paper["Catégorie principale"] == al.ADMIN_CATEGORY_LABELS["fournitures"]
    assert (paper["Première"], paper["Dernière"]) == ("2031-03-02", "2031-03-12")
    assert by[BAILIFF_FIRM]["Écritures"] == 2
    assert by[BAILIFF_FIRM]["Types"] == "Fidéicommis — déboursé_tiers (2)"
    # Only third-party payments: no drawing, no remittance, no advance — the
    # client paid a trust advance AND was paid once as a supplier: only the
    # payment counts.
    assert CANARY not in by and "Revenu Québec" not in by
    assert by["Client Fictif"]["Écritures"] == 1
    assert by["Client Fictif"]["Types"] == "Administration — dépense (1)"
    # Most entries first.
    assert rows[0]["Nom"] == SUPPLIER
    # « — » has nothing to compare: skipped, and counted.
    assert "—" not in by and skipped == 1


def test_one_leading_title_is_set_aside_never_more(ledger):
    by = _by_name(_rows()[0])
    # « Me » and « L'hon. » (two words once folded) aside, the contacts match.
    assert by["Me Prénom Avocat"]["Déjà au carnet"] == "Avocat(e)"
    assert by["L'hon. Juge Fictif"]["Déjà au carnet"] == "Expert(e)"
    # « M & M Plomberie » keeps its second « m »: not « Plomberie ».
    assert by["M & M Plomberie"]["Écritures"] == 1
    assert by["Plomberie"]["Écritures"] == 1
    assert rc.fold("Me") == "me"


def test_contacts_are_found_by_trade_name_and_wrong_roles_flagged(ledger):
    by = _by_name(_rows()[0])
    assert by["Me Prénom Avocat"]["Remarque"] == ""
    # The trade name finds the supplier.
    assert by["Commerce Enseigne"]["Déjà au carnet"] == "Fournisseur"
    # A payee filed as a client will not be suggested: flagged.
    assert by["Client Fictif"]["Déjà au carnet"] == "Client"
    assert by["Client Fictif"]["Remarque"] == rc.NOT_SUGGESTED
    assert by[SUPPLIER]["Déjà au carnet"] == ""


def test_the_prefill_spellings_are_their_own_list(ledger):
    admin, _trust_rows, _parties = rc.read_snapshot()
    rows = rc.prefill_rows(admin)
    assert rows == [
        {"Type": al.KIND_LABELS["prélèvement"], "Orthographe": CANARY,
         "Écritures": 2},
        {"Type": al.KIND_LABELS["remise_tps_tvq"], "Orthographe": "Revenu Québec",
         "Écritures": 1},
    ]


def _read_csv(path: pathlib.Path) -> tuple[str, list[list[str]]]:
    text = path.read_bytes().decode("utf-8")
    assert text.startswith(BOM)  # Excel opens it as UTF-8
    body = text[len(BOM):]
    return body, list(csv.reader(io.StringIO(body), delimiter=";"))


def test_main_writes_french_excel_csv_neutralised_and_prints_no_name(
        ledger, tmp_path, capsys):
    out = tmp_path / "contreparties.csv"
    assert rc.main(["--sortie", str(out)], repo=tmp_path / "depot") == 0
    body, main_rows = _read_csv(out)
    assert tuple(main_rows[0]) == rc.COLUMNS
    # Every cell quoted: a « ; » or « = » stays inside its cell.
    first_line = body.split("\r\n", 1)[0]
    assert first_line == ";".join(f'"{c}"' for c in rc.COLUMNS)
    names = [r[0] for r in main_rows[1:]]
    assert "'=CMD|calc" in names and "Fournisseur X;=1+1;" in names
    assert all(len(r) == len(rc.COLUMNS) for r in main_rows)
    second = tmp_path / "contreparties-preremplissages.csv"
    assert tuple(_read_csv(second)[1][0]) == rc.PREFILL_COLUMNS
    printed = capsys.readouterr().out
    for name in (SUPPLIER, BAILIFF_FIRM, CANARY, "Client Fictif", "Commerce",
                 "Juge"):
        assert name not in printed
    assert "sha256" in printed
    assert "1 écriture(s) sans nom comparable" in printed


def test_a_failed_read_writes_nothing(ledger, tmp_path, monkeypatch):
    def _broken(_txn):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(al, "list_all_transactions_strict", _broken)
    out = tmp_path / "contreparties.csv"
    assert rc.main(["--sortie", str(out)], repo=tmp_path / "depot") == 2
    assert not out.exists()
    assert not (tmp_path / "contreparties-preremplissages.csv").exists()


def test_a_truncated_read_writes_nothing(ledger, tmp_path, monkeypatch):
    def _truncated(_txn):
        raise rc.full_reads.TruncatedRead("too many")

    monkeypatch.setattr(trust, "list_all_transactions_strict", _truncated)
    out = tmp_path / "contreparties.csv"
    assert rc.main(["--sortie", str(out)], repo=tmp_path / "depot") == 2
    assert not out.exists()


def test_a_path_inside_the_public_repository_is_refused(ledger, tmp_path):
    repo = tmp_path / "depot"
    (repo / "athena").mkdir(parents=True)
    out = repo / "athena" / "contreparties.csv"
    assert rc.main(["--sortie", str(out)], repo=repo) == 3
    assert not out.exists()
    # The default repository is this one.
    assert (rc._REPO / "athena" / "scripts" / "rapport_contreparties.py").is_file()
    assert rc.inside_repository(_ATHENA / "x.csv")
