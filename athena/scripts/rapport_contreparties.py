"""The past payees, to add suppliers by hand — READ-ONLY (2026-11-11).

Lot 3 of the suppliers plan. The lawyer enters his suppliers BY HAND in
« Impliqués » (his decision), so he can record their TPS and TVQ numbers,
their usual category and his account number with them. This one-time report
lists the payees his past entries name, most frequent first, to work from.
It writes NOTHING in Athena.

Read, in ONE read-only transaction: every administration entry, every trust
entry and every contact. A failed or truncated read stops the run with no
file, because a partial list would pass for the whole one.

Kept: the payments to third parties — administration « dépense » and
« remboursement_depense », trust « déboursé_tiers ». Corrections are left
out. An entry a correction reversed counts in its own column, never in the
main count.

Names are grouped by their fold (``utils/rapprochement.plier_contrepartie``:
no accents, no case, punctuation as spaces), ONE leading title set aside
(« Me », « Dr », « L'hon. »…, never the whole name); each group lists its
spellings. A name with nothing left to compare (no letter or digit) is
skipped and COUNTED in the terminal summary.

The main file, ``--sortie``, has one row per payee:
Nom | Variantes | Écritures | Contre-passées | Types | Catégorie principale |
Première | Dernière | Déjà au carnet | Remarque.

* « Déjà au carnet »: a contact whose name, legal name or trade name folds
  the same, with its role.
* « Ne sera pas suggéré »: that role is not one the accounting forms will
  suggest (``utils/contact_roles.PAYEE_ROLES``) — change it on the web.

A second file, ``<sortie>-preremplissages.csv``, lists the spellings used on
the drawings, the contributions, the TPS/TVQ remittances and refunds and the
internal transfers, with their count. It is used to confirm the pre-fills of
the forms' lot.

Output safety:

* UTF-8 with a BOM, SEMICOLON-separated, every cell quoted: the lawyer's
  Excel (French, Canada) splits a double-clicked file on « ; », and a
  quoted cell keeps a « ; » or a « = » inside it. (The app's own exports
  use commas; this file is meant to be double-clicked.) A list inside a
  cell is joined with « | »;
* formula prefixes neutralised;
* a path inside the git repository, which is PUBLIC, is refused;
* the terminal prints counts and each file's sha256, never a name.

Delete the files once used: they hold names.

Exit 0 when written, 2 when the read failed (nothing written), 3 when the
path is refused.

Run (production read: GOOGLE_CLOUD_PROJECT and the config variables inline):
    python -m scripts.rapport_contreparties --sortie C:\\Users\\…\\contreparties.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import pathlib
import sys
from collections import Counter, defaultdict
from datetime import date, datetime

from google.cloud import firestore

from models import admin_ledger as al
from models import full_reads, trust
from models import partie as pm
from utils import civilite, contact_roles
from utils.export_csv import neutralize_formula
from utils.rapprochement import plier_contrepartie

# The payments to third parties, by register.
ADMIN_KINDS = ("dépense", "remboursement_depense")
TRUST_PURPOSES = ("déboursé_tiers",)
# The administration kinds whose payee the forms will pre-fill.
PREFILL_KINDS = (
    "prélèvement", "apport", "remise_tps_tvq", "remboursement_tps_tvq",
    "virement_interne_sortant", "virement_interne_entrant",
)

COLUMNS = (
    "Nom", "Variantes", "Écritures", "Contre-passées", "Types",
    "Catégorie principale", "Première", "Dernière", "Déjà au carnet", "Remarque",
)
PREFILL_COLUMNS = ("Type", "Orthographe", "Écritures")
NOT_SUGGESTED = "Ne sera pas suggéré : changez son rôle dans Impliqués."

_REPO = pathlib.Path(__file__).resolve().parents[2]
# The folded titles a name may open with, as word sequences, longest first
# (« L'hon. » folds to two words).
_TITLES = tuple(sorted(
    {tuple(plier_contrepartie(t).split()) for t in civilite.PREFIXES if t},
    key=len, reverse=True))


class ReadFailed(RuntimeError):
    """A read of the snapshot failed or was truncated: nothing is written."""


def fold(name: str) -> str:
    """The grouping key of a payee's or a contact's name: folded, ONE
    leading title set aside — never the whole name (« Me » alone stays
    « me »; « M & M Plomberie » keeps its second « m »)."""
    words = plier_contrepartie(name or "").split()
    for title in _TITLES:
        if len(words) > len(title) and tuple(words[:len(title)]) == title:
            words = words[len(title):]
            break
    return " ".join(words)


def read_snapshot() -> tuple[list[dict], list[dict], list[dict]]:
    """``(administration entries, trust entries, contacts)``, read in ONE
    read-only transaction; :class:`ReadFailed` on any failure."""
    out: dict = {}
    transaction = al.db.transaction(read_only=True)

    @firestore.transactional
    def _read(txn) -> None:
        out["admin"] = al.list_all_transactions_strict(txn)
        out["trust"] = trust.list_all_transactions_strict(txn)
        out["parties"] = full_reads.stream_all(
            pm.db.collection(pm.COLLECTION), transaction=txn)

    try:
        _read(transaction)
    except Exception as exc:
        raise ReadFailed(type(exc).__name__) from exc
    return out["admin"], out["trust"], out["parties"]


def _day(value) -> str:
    """A date-only value (stored at UTC midnight) as « AAAA-MM-JJ » — never
    through to_mtl, which would show the day before (Rule 5)."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return ""


def _contacts_by_key(parties: list[dict]) -> dict[str, list[dict]]:
    """Every contact under each key its names fold to: the full name
    without title, the legal name, the trade name."""
    index: dict[str, list[dict]] = defaultdict(list)
    for partie in parties:
        names = {
            civilite.full_name(partie, honorific=False),
            partie.get("organization_name", "") or "",
            partie.get("trade_name", "") or "",
        }
        for key in {fold(n) for n in names if n}:
            if key:
                index[key].append(partie)
    return index


def payee_rows(admin: list[dict], trust_rows: list[dict],
               parties: list[dict]) -> tuple[list[dict], int]:
    """``(one row per payee, most entries first; the number of entries
    skipped because their name folds to nothing)``."""
    groups: dict[str, dict] = {}
    skipped = 0
    for register, rows, kinds, field in (
        ("Administration", admin, ADMIN_KINDS, "kind"),
        ("Fidéicommis", trust_rows, TRUST_PURPOSES, "purpose"),
    ):
        for row in rows:
            kind = row.get(field) or ""
            name = (row.get("counterparty") or "").strip()
            # A correction is none of these kinds: left out.
            if kind not in kinds or not name:
                continue
            key = fold(name)
            if not key:
                skipped += 1
                continue
            group = groups.setdefault(key, {
                "spellings": Counter(), "count": 0, "reversed": 0,
                "kinds": Counter(), "categories": Counter(), "days": [],
            })
            group["spellings"][name] += 1
            if row.get("reversed_by_id"):
                group["reversed"] += 1
                continue
            group["count"] += 1
            group["kinds"][f"{register} — {kind}"] += 1
            if row.get("category"):
                group["categories"][row["category"]] += 1
            day = _day(row.get("date"))
            if day:
                group["days"].append(day)

    known = _contacts_by_key(parties)
    rows_out = []
    for key, group in groups.items():
        spellings = group["spellings"].most_common()
        matches = known.get(key, [])
        roles = sorted({p.get("contact_role", "") for p in matches})
        category = (group["categories"].most_common(1) or [("", 0)])[0][0]
        rows_out.append({
            "Nom": spellings[0][0],
            "Variantes": " | ".join(f"{s} ({n})" for s, n in spellings[1:]),
            "Écritures": group["count"],
            "Contre-passées": group["reversed"],
            "Types": " | ".join(f"{k} ({n})" for k, n in group["kinds"].most_common()),
            "Catégorie principale": al.ADMIN_CATEGORY_LABELS.get(category, category),
            "Première": min(group["days"]) if group["days"] else "",
            "Dernière": max(group["days"]) if group["days"] else "",
            "Déjà au carnet": " | ".join(
                contact_roles.role_label(r) or r for r in roles),
            "Remarque": (NOT_SUGGESTED if roles and not any(
                r in contact_roles.PAYEE_ROLES for r in roles) else ""),
        })
    rows_out.sort(key=lambda r: (-r["Écritures"], fold(r["Nom"])))
    return rows_out, skipped


def prefill_rows(admin: list[dict]) -> list[dict]:
    """Each spelling used on a pre-filled kind, with its count."""
    counts: Counter = Counter()
    for row in admin:
        kind = row.get("kind") or ""
        name = (row.get("counterparty") or "").strip()
        if kind in PREFILL_KINDS and name:
            counts[(kind, name)] += 1
    return [
        {"Type": al.KIND_LABELS.get(kind, kind), "Orthographe": name,
         "Écritures": n}
        for (kind, name), n in sorted(
            counts.items(), key=lambda item: (PREFILL_KINDS.index(item[0][0]),
                                              -item[1], item[0][1]))
    ]


def _csv_bytes(rows: list[dict], columns: tuple[str, ...]) -> bytes:
    """UTF-8 with a BOM, « ; »-separated, every cell quoted (the module
    docstring), every text cell neutralised."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_ALL,
                        lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([
            neutralize_formula(v) if isinstance(v, str) else v
            for v in (row[c] for c in columns)
        ])
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def inside_repository(path: pathlib.Path, repo: pathlib.Path = _REPO) -> bool:
    """Whether *path* lies in the git repository — which is public."""
    resolved = path.resolve()
    root = repo.resolve()
    return resolved == root or root in resolved.parents


def prefill_path(path: pathlib.Path) -> pathlib.Path:
    return path.with_name(f"{path.stem}-preremplissages{path.suffix or '.csv'}")


def main(argv=None, *, repo: pathlib.Path = _REPO) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sortie", required=True,
                        help="Le fichier CSV à écrire, hors du dépôt git.")
    args = parser.parse_args(argv)
    out = pathlib.Path(args.sortie)
    second = prefill_path(out)
    if inside_repository(out, repo) or inside_repository(second, repo):
        print("Refusé : le fichier serait dans le dépôt git, qui est public. "
              "Choisissez un dossier hors du dépôt.")
        return 3
    try:
        admin, trust_rows, parties = read_snapshot()
    except ReadFailed as exc:
        print(f"Lecture impossible ({exc}) : aucun fichier écrit.")
        return 2
    payees, skipped = payee_rows(admin, trust_rows, parties)
    prefills = prefill_rows(admin)
    main_bytes = _csv_bytes(payees, COLUMNS)
    prefill_bytes = _csv_bytes(prefills, PREFILL_COLUMNS)
    out.write_bytes(main_bytes)
    second.write_bytes(prefill_bytes)
    print(f"Lu : {len(admin)} écritures d'administration, {len(trust_rows)} "
          f"de fidéicommis, {len(parties)} contacts.")
    print(f"{len(payees)} bénéficiaires, dont "
          f"{sum(1 for r in payees if r['Déjà au carnet'])} déjà au carnet et "
          f"{sum(1 for r in payees if r['Remarque'])} à changer de rôle.")
    if skipped:
        print(f"{skipped} écriture(s) sans nom comparable (ni lettre ni "
              "chiffre) laissée(s) de côté.")
    for path, data in ((out, main_bytes), (second, prefill_bytes)):
        print(f"Écrit : {path.name} — sha256 {hashlib.sha256(data).hexdigest()}")
    print("Supprimez ces fichiers une fois utilisés : ils contiennent des noms.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
