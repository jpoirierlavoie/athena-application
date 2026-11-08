"""Reclassify, OUTSIDE the models, the category of administration expenses
— a one-shot migration bound to its plan (lot C of the financial
statements, the lawyer's decision of 2026-11-07).

What it does
------------
On 2026-11-07 the administration ledger gained the dépense category
``infonuagique_ia`` (« Infonuagique et IA (à l'usage) »): cloud and AI
billed by use, a variable cost in the financial statements — a flat plan
stays « abonnements ». The past charges of that kind (API credits, cloud
compute) were filed under other categories, mostly ``abonnements``, and many
of them are LOCKED: compensée, or dated within a reconciled period.
``admin_ledger.update_transaction`` refuses every locked entry
(``_entry_lock_reason``), as an edit must; and the house correction, a
reversal and a new entry, would add two entries per charge to the register
to move a reporting label. This script moves the category of the entries the
lawyer lists, line by line, from a PLAN kept outside the public repository,
and nothing else. ``--lister`` prints the candidates the plan is built from.

Why it writes outside the models
--------------------------------
CLAUDE.md, « One door per rule »: a declared exception, on the pattern of
``scripts/rectify_expense_ventilation``, importing the pure helpers of
``scripts/reclassify_admin_ledger`` and ``scripts/reclassify_as_invoice_receipt``
and leaving them and their tests untouched; it closes with its plan. A LOCKED
entry is ACCEPTED — that is the point: the lock protects figures proved
against the bank, and a category is not one of them. Moving it alters no
balance (the amount and the sign stay), no reconciliation (a reconciliation
keeps gross amounts and the ids it cleared, never a category) and no tax
figure (the TPS/TVQ split stays): only the category the cost is reported
under in ``admin_ledger.results_statement`` moves, between the two
categories of the line, its totals unchanged.

What it guarantees instead of the model: on each entry it writes ``category``
— a code of ``admin_ledger.ADMIN_EXPENSE_CATEGORIES``, the vocabulary the
model validates — plus the house stamps (``updated_at``, a new ``etag``,
``updated_via = "script"``, under ``provenance.writing_via``) and ONE
``revisions`` trail entry ``{at, via, motif, changes, updated_at_before,
source_sha256}``, its motif naming the plan line. Never the amount, the
split, a date, the kind, the sign, the status, the dossier, a link or the
counterparty. No account is written — no money moves — and no
reconciliation. Nothing is trimmed from a trail (« rien ne se supprime »):
an entry carrying more than ``_REVISIONS_CAP - 2`` entries is refused, which
keeps room for this write and one restoration. The registers are not
DAV-exposed: no CTag.

The plan
--------
A CSV, UTF-8 (a BOM is accepted), outside the repository, its quoting read
strictly — the conventions of ``scripts/reclassify_admin_ledger`` — with
this header (:data:`CSV_COLUMNS`) and one line per entry::

    ligne,entry_id,date,montant_cents,categorie_actuelle,categorie_cible,motif

``ligne`` is a positive integer, given once; ``entry_id`` a UUID; ``date``
(the entry's UTC day, AAAA-MM-JJ), ``montant_cents`` and
``categorie_actuelle`` state what the line expects the base to hold;
``categorie_cible`` names the category to write; ``motif`` is the lawyer's
reason, stored as is — it must survive ``security.sanitize`` unaltered and
carry no leading or trailing space — and the trail reads « motif — ligne
n ». Any error of form refuses the whole run before anything is read.

Judging a line — every read is strict: a failed read stops the run
------------------------------------------------------------------
A line is REFUSED, with a code and a French reason, and reported — never
forced; the other lines still apply. It is refused when its entry also
appears on another line (both are refused: no precedence settles them); is
absent; when its target is not a category of the vocabulary, or is its own
``categorie_actuelle``; when the stored day, amount or category is not the
one the line states (« état changé »); when its entry is not a ``dépense``,
not a déboursé, on an account that does not exist, a member of a reversal
seen from either end (its correction carries the original's category: the
pair would no longer net within one category), annulée, linked to an
invoice or to a trust entry, its day or amount unreadable, or its trail
unreadable or holding more than ``_REVISIONS_CAP - 2`` entries. A line whose entry holds its
target with this plan's trail entry (same ``source_sha256``) is « déjà
appliquée »: the migration is idempotent; the target without that trail
entry is refused (« déjà la cible »), the trail entry without the target
too (« état mixte »).

The controls
------------
On complete reads — every administration entry, account and reconciliation,
every invoice, the trust register — BEFORE, AFTER SIMULATED (the documents
read, merged with the exact payloads) and, once written, AFTER (re-read):

1. each account's stored ``ledger_balance`` and Σ ``admin_delta``, every
   entry's running balance and sequence — unchanged;
2. each plan line's entry: a written one moves only its category, from its
   stored value to its target, and its trail by exactly one entry (the
   earlier ones intact) — every other field but the stamps unchanged; an
   entry no line writes, entirely unchanged;
3. ``admin_ledger.results_statement`` over each calendar year holding a
   written entry and over the whole journal — moved by exactly what the same
   function says of the written lines alone, as stored and as the plan
   targets them: gross and net amounts between categories, never a total,
   a TPS or a TVQ;
4. each account's lock floor and last reconciliation, and the re-proof of
   every completed reconciliation — unchanged; each line's ``lock_reason``
   (derived at read, never stored) is reported;
5. every other document read — unchanged (a digest per document).

A control that cannot be computed, or that moves otherwise, refuses
``--appliquer``.

Fingerprint, backup, restoration
--------------------------------
As in ``scripts/rectify_expense_ventilation``: the fingerprint covers the
plan's digest, ``db.project``, every planned write (path, the
``update_time`` read, the category written) and every refused or applied
line with its reasons; the typed JSON backup of each document to write —
whole — is written, fsynced, read back and verified before ONE batch whose
every document is guarded by the ``update_time`` it was read at;
``--restaurer`` (dry by default, its own fingerprint) puts the saved
categories back, all or nothing, with fresh stamps and a trail entry
« Restauration de la sauvegarde du … — ligne n », and refuses everything
when an entry's etag is no longer the migration's.

Running it — from ``athena/``, with ADC and the environment of « Active
gabarits » in DEPLOYMENT.md: even ``--lister`` and the dry run read
PRODUCTION. A plan, report, backup or backup directory inside the
repository is refused before anything is created; the report must be a new
file. ``--lister`` writes nothing and creates no file.

    python -m scripts.reclassify_admin_category --lister abonnements [--beneficiaire TEXTE]
    python -m scripts.reclassify_admin_category PLAN.csv --rapport R.md
    python -m scripts.reclassify_admin_category PLAN.csv --rapport R.md \\
        --sauvegarde DOSSIER --appliquer EMPREINTE
    python -m scripts.reclassify_admin_category --restaurer SAUVEGARDE.json \\
        --rapport R.md [--appliquer EMPREINTE]

Afterwards, run both integrity scripts and compare their findings BY TEXT.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from models import admin_ledger as al
from models import db, provenance
from scripts import reclassify_admin_ledger as rla
from scripts import reclassify_as_invoice_receipt as rx
from scripts.rectify_registers import _addressable

UTC = timezone.utc
TX = al.TRANSACTIONS_COLLECTION
#: The plan's header, in this order.
CSV_COLUMNS = ("ligne", "entry_id", "date", "montant_cents", "categorie_actuelle",
               "categorie_cible", "motif")
#: The one business field this script writes.
FIELD = "category"
#: What a write moves on an entry: the field, the trail and the stamps.
#: Control 2 holds every other field to its stored value.
_WRITTEN = (FIELD, "revisions", "updated_at", "etag", "updated_via")
MOTIF_FORMAT = "{motif} — ligne {ligne}"
#: A written entry keeps room for this write AND one restoration.
MAX_TRAIL_BEFORE = al._REVISIONS_CAP - 2
FINGERPRINT_HEX = 16
BACKUP_FORMAT = "athena.categorie-reclassee/1"
RESTORE_MOTIF = "Restauration de la sauvegarde du {quand} — ligne {ligne}"
PENDING, DONE, REFUSED = "à appliquer", "déjà appliquée", "refusée"
RESTORE_PENDING, RESTORE_DONE = rx.RESTORE_PENDING, rx.RESTORE_DONE
_CONTROLS = ("1", "2", "3", "4", "5")
_BACKUP_DOCUMENT_KEYS = ("chemin", "ligne", "update_time_lu", "etag_migration", "champs",
                         "avant", "apres")

RunRefused = rla.RunRefused
Impossible = rla.Impossible
ControlImpossible = rla.ControlImpossible


# ── the plan file ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LineSpec:
    ligne: int
    entry_id: str
    day: date           # the stored UTC day the line expects
    amount: int         # the stored amount the line expects, cents
    current: str        # the stored category the line expects
    target: str         # the category to write
    motif: str          # the lawyer's reason, verbatim

    @property
    def path(self) -> str:
        return f"{TX}/{self.entry_id}"

    @property
    def trail_motif(self) -> str:
        return MOTIF_FORMAT.format(motif=self.motif, ligne=self.ligne)


def _required(text: str, what: str) -> str:
    if text == "":
        raise RunRefused(f"{what} : valeur requise")
    return text


def _parse_records(records: list) -> list[LineSpec]:
    if not records:
        raise RunRefused("le fichier est vide")
    if tuple(records[0]) != CSV_COLUMNS:
        raise RunRefused("en-tête inattendu — les colonnes attendues, dans cet ordre : "
                         + ",".join(CSV_COLUMNS))
    lines: list[LineSpec] = []
    numbers: set = set()
    for index, cells in enumerate(records[1:], start=1):
        if len(cells) != len(CSV_COLUMNS):
            raise RunRefused(f"enregistrement {index} : {len(cells)} colonne(s), "
                             f"{len(CSV_COLUMNS)} attendues")
        c = dict(zip(CSV_COLUMNS, cells))
        ligne = rla._positive_int(c["ligne"], f"enregistrement {index}, colonne ligne")
        if ligne in numbers:
            raise RunRefused(f"ligne {ligne} : numéro de ligne en double")
        numbers.add(ligne)
        where = f"ligne {ligne}"
        lines.append(LineSpec(
            ligne=ligne,
            entry_id=rla._uuid_cell(c["entry_id"], f"{where}, entry_id"),
            day=rla._day_cell(c["date"], f"{where}, date").date(),
            amount=rla._positive_int(c["montant_cents"], f"{where}, montant_cents"),
            current=_required(c["categorie_actuelle"], f"{where}, categorie_actuelle"),
            target=_required(c["categorie_cible"], f"{where}, categorie_cible"),
            motif=rla._text_cell(c["motif"], f"{where}, motif"),
        ))
    if not lines:
        raise RunRefused("aucune ligne")
    return sorted(lines, key=lambda line: line.ligne)


def parse_csv(data: bytes) -> list[LineSpec]:
    """The plan's lines, its form judged before any read — any defect refuses
    the whole run. A duplicated ``entry_id`` is not a defect of form: both
    lines are refused at judgment, and reported."""
    try:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise RunRefused("le fichier n'est pas en UTF-8") from None
        try:
            # strict: a stray quote ('"abonnements"X') is an error, never a value.
            records = list(csv.reader(io.StringIO(text, newline=""), strict=True))
        except csv.Error as exc:
            raise RunRefused(f"CSV illisible ({exc})") from None
        return _parse_records(records)
    except RunRefused as exc:
        raise RunRefused(f"plan mal formé : {exc}") from None


def _load_plan(path: Path) -> tuple[bytes, str]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RunRefused(f"plan illisible ({type(exc).__name__}) : {path}") from None
    return data, hashlib.sha256(data).hexdigest()


def _duplicates(lines: list) -> dict:
    """``entry_id`` → the line numbers naming it, for ids named twice or more."""
    seen: dict = {}
    for line in lines:
        seen.setdefault(line.entry_id, []).append(line.ligne)
    return {entry_id: numbers for entry_id, numbers in seen.items() if len(numbers) > 1}


# ── what refuses an entry ─────────────────────────────────────────────────


def _trail_length(doc: dict) -> Optional[int]:
    """The trail's length — ``0`` when absent, ``None`` when unreadable."""
    revisions = doc.get("revisions")
    if revisions is None:
        return 0
    return len(revisions) if isinstance(revisions, list) else None


def _named_by_a_correction(store) -> frozenset:
    """Every entry id a row's ``reverses_id`` names, over the whole register:
    a pair is judged from both ends, so an original whose own
    ``reversed_by_id`` is missing still counts as a member."""
    return frozenset(row.get("reverses_id") for row in store.rows.values()
                     if row.get("reverses_id"))


def entry_refusals(doc: dict, store) -> list[tuple[str, str]]:
    """``[(code, French reason)]`` refusing ANY entry this script would write,
    the plan's own expectations apart — ``--lister`` shows the same verdict."""
    out = []
    kind = doc.get("kind")
    if kind not in al.EXPENSE_KINDS:
        out.append(("pas_une_dépense", (f"nature « {al.KIND_LABELS.get(kind, kind)} » : seule "
                                        f"une dépense porte une catégorie qui se reclasse ici")))
    if doc.get("direction") != "déboursé":
        out.append(("pas_un_déboursé", "l'écriture n'est pas un déboursé"))
    if doc.get("account_id") not in store.accounts:
        out.append(("compte_introuvable", f"compte {doc.get('account_id')} introuvable"))
    amount = doc.get("amount")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        out.append(("montant_illisible", "montant illisible : aucun plan ne peut l'attendre"))
    if rla._day(doc.get("date")) is None:
        out.append(("date_illisible", "date illisible : aucun plan ne peut l'attendre"))
    if (doc.get("reverses_id") or doc.get("reversed_by_id")
            or doc.get("id") in _named_by_a_correction(store)):
        out.append(("lien_contre_passation", ("membre d'une contre-passation : sa correction "
                                              "porte la catégorie de l'original, et la paire ne "
                                              "se compenserait plus dans une même catégorie")))
    if doc.get("status") == "annulée":
        out.append(("annulée", "l'écriture est annulée"))
    if doc.get("invoice_id"):
        out.append(("liée_facture", "l'écriture est liée à une facture"))
    if doc.get("trust_transaction_id"):
        out.append(("liée_fideicommis", "l'écriture est liée à une écriture du fidéicommis"))
    length = _trail_length(doc)
    if length is None:
        out.append(("fil_illisible", "fil de révisions illisible"))
    elif length > MAX_TRAIL_BEFORE:
        out.append(("fil_plein", (f"{length} entrées au fil de révisions — au plus "
                                  f"{MAX_TRAIL_BEFORE}, pour garder la place de cette écriture "
                                  f"et d'une restauration sans rien rogner")))
    return out


def _trail_of(doc: dict, sha: str) -> bool:
    return _trail_length(doc) not in (None, 0) and rx._trail_of(doc, sha)


# ── the controls ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Scope:
    """What the controls are about: every entry a plan line names, and the
    calendar years of the entries written."""

    entry_ids: tuple
    periods: tuple


@dataclass(frozen=True)
class Expected:
    """What the write must move: each written entry's category ``[from,
    to]`` (control 2), and the results statement's flat delta by period
    key (control 3)."""

    moves: dict
    results: dict


def _period_key(first: date, last: date) -> str:
    return f"{first.isoformat()}/{last.isoformat()}"


def _years(docs: list) -> tuple:
    """The calendar years (UTC day) of *docs*, as ``(first day, last day)``."""
    days = [rla._day(doc.get("date")) for doc in docs]
    years = sorted({day.year for day in days if day is not None})
    return tuple((date(year, 1, 1), date(year, 12, 31)) for year in years)


def _control_lines(rows: list, entry_ids: tuple) -> dict:
    by_id = {r.get("id"): r for r in rows}
    out: dict = {}
    for entry_id in entry_ids:
        row = by_id.get(entry_id)
        if row is None:
            out[entry_id] = None
            continue
        trail = row.get("revisions")
        trail = [] if trail is None else trail
        out[entry_id] = {
            "document": rla._digest(row),
            "faits": rla._digest({k: v for k, v in row.items() if k not in _WRITTEN}),
            "categorie": row.get(FIELD),
            "fil": rla._digest(trail),
            "fil_moins_un": (rla._digest(trail[:-1])
                             if isinstance(trail, list) and trail else None),
            "longueur": len(trail) if isinstance(trail, list) else None,
        }
    return out


def _control_periods(rows: list, periods: tuple) -> dict:
    resolved = rx._resolved(rows)
    out = {}
    for first, last in periods:
        in_period = [r for r in resolved
                     if first <= rx._day_of(r.get("date"), f"écriture {r.get('id')}") <= last]
        out[_period_key(first, last)] = al.results_statement(in_period)
    out["journal"] = al.results_statement(resolved)
    return out


def _control_lock(rows: list, accounts: dict, recs: list, scope: Scope) -> dict:
    reconciliations = rla._control_reconciliations(rows, accounts, recs)
    by_id = {r.get("id"): r for r in rows}
    return {"conciliations": reconciliations,
            "lock_reasons": {
                i: al._entry_lock_reason(by_id[i], rx._floor_of(
                    reconciliations, by_id[i].get("account_id") or ""))
                for i in scope.entry_ids if i in by_id}}


def compute_controls(store, scope: Scope, payloads: Optional[dict] = None) -> dict:
    """The five controls, on the documents read — merged with *payloads*
    (by path) for the simulation."""
    rows = store.control_rows(payloads)
    accounts = store.control_accounts(payloads)
    recs = rx._merged(rx.RECS, store.recs, payloads)
    invoices = rx._merged(rx.INVOICES, store.invoices, payloads)
    trust_rows = rx._merged(rx.TRUST_TX, store.trust_rows, payloads)
    skip = frozenset(f"{TX}/{i}" for i in scope.entry_ids)
    if invoices is None:
        others = Impossible(f"factures illisibles ({store.invoices_error})")
    elif trust_rows is None:
        others = Impossible(f"registre du fidéicommis illisible ({store.trust_error})")
    else:
        others = rla._attempt(rx._control_others, rows, accounts, recs, invoices, trust_rows,
                              skip)
    return {
        "1": rla._attempt(rx._control_ledger, rows, accounts),
        "2": rla._attempt(_control_lines, rows, scope.entry_ids),
        "3": rla._attempt(_control_periods, rows, scope.periods),
        "4": rla._attempt(_control_lock, rows, accounts, recs, scope),
        "5": others,
    }


def expected_effects(pairs: list, periods: tuple) -> Expected:
    """What the written lines alone move — each entry's category, and
    ``results_statement`` of the lines as stored, then as their targets say
    (never as a payload says: a payload that strays from its plan must show
    in controls 2 and 3), period by period and over the whole journal."""
    def delta(subset: list) -> dict:
        return rla._flat_results_delta(al.results_statement([b for b, _a in subset]),
                                       al.results_statement([a for _b, a in subset]))

    results = {}
    for first, last in periods:
        results[_period_key(first, last)] = delta(
            [(b, a) for b, a in pairs if first <= rx._day_of(b.get("date"), "ligne") <= last])
    results["journal"] = delta(pairs)
    moves = {b.get("id"): [b.get(FIELD), a.get(FIELD)] for b, a in pairs}
    return Expected(moves=moves, results=results)


def _line_gaps(before: dict, after: dict, moves: dict) -> list:
    gaps = []
    for entry_id in sorted(before, key=str):
        b, a = before[entry_id], after.get(entry_id)
        if entry_id not in moves:
            if a != b:
                gaps.append(f"contrôle 2 — écriture {entry_id} : modifiée, alors qu'aucune "
                            f"ligne ne l'écrit")
            continue
        old, new = moves[entry_id]
        if b is None or a is None:
            gaps.append(f"contrôle 2 — écriture {entry_id} absente de la lecture")
            continue
        if a["faits"] != b["faits"]:
            gaps.append(f"contrôle 2 — écriture {entry_id} : un autre champ que la catégorie, "
                        f"le fil et les estampilles a bougé")
        if (b["categorie"], a["categorie"]) != (old, new):
            gaps.append(f"contrôle 2 — écriture {entry_id} : catégorie {rx._v(b['categorie'])} "
                        f"→ {rx._v(a['categorie'])}, attendu {rx._v(old)} → {rx._v(new)}")
        if (b["longueur"] is None or a["longueur"] != b["longueur"] + 1
                or a["fil_moins_un"] != b["fil"]):
            gaps.append(f"contrôle 2 — écriture {entry_id} : le fil ne gagne pas exactement "
                        f"une entrée, les précédentes intactes")
    return gaps


def compare_controls(before: dict, after: dict, expected) -> tuple[list, list]:
    """``(gaps, impossible)``: what moved otherwise than *expected*, and what
    could not be computed."""
    gaps: list[str] = []
    impossible: list[str] = []
    for key in _CONTROLS:
        for value in (before.get(key), after.get(key)):
            if isinstance(value, Impossible):
                impossible.append(f"contrôle {key} impossible à calculer ({value.reason})")
                break
    if not isinstance(expected, Expected):
        reason = expected.reason if isinstance(expected, Impossible) else "absent"
        impossible.append(f"effet attendu impossible à calculer ({reason})")
        return gaps, impossible
    blocked = {item.split(" ")[1] for item in impossible}
    for key in ("1", "5"):
        if key not in blocked and before[key] != after[key]:
            gaps.extend(f"contrôle {key} — {d}" for d in rla._differences(before[key], after[key]))
    if "2" not in blocked:
        gaps += _line_gaps(before["2"], after["2"], expected.moves)
    if "3" not in blocked:
        for key, want in expected.results.items():
            got = rla._flat_results_delta(before["3"][key], after["3"][key])
            if got != want:
                gaps.append(f"contrôle 3 ({key}) — écart {got}, attendu {want}")
    if "4" not in blocked and before["4"]["conciliations"] != after["4"]["conciliations"]:
        gaps.extend(f"contrôle 4 — {d}" for d in rla._differences(
            before["4"]["conciliations"], after["4"]["conciliations"]))
    return gaps, impossible


# ── judging the plan ──────────────────────────────────────────────────────


@dataclass
class Verdict:
    line: LineSpec
    state: str = PENDING
    snap: Any = None
    doc: dict = field(default_factory=dict)
    table: list = field(default_factory=list)      # (document, champ, attendu, base, ok)
    reasons: list = field(default_factory=list)    # [(code, French reason)]

    def refuse(self, code: str, message: str) -> None:
        self.state = REFUSED
        self.reasons.append((code, message))


def _compare(line: LineSpec, doc: dict, verdict: Verdict) -> None:
    """The stored day, amount and category against what the line states."""
    label = f"Ligne {line.ligne}"
    day = rla._day(doc.get("date"))
    amount = rla._as_int(doc.get("amount"))
    stored = doc.get(FIELD)
    for name, want, have, ok in (
            ("date", line.day.isoformat(), day.isoformat() if day else rx._v(doc.get("date")),
             day == line.day),
            ("amount", str(line.amount), rx._v(doc.get("amount")), amount == line.amount),
            (FIELD, rx._v(line.current), rx._v(stored), stored == line.current)):
        verdict.table.append((label, name, want, have, ok))
    if day != line.day:
        verdict.refuse("état_changé:date", f"jour inscrit {day or rx._v(doc.get('date'))}, le "
                                           f"plan attend {line.day}")
    if amount != line.amount:
        verdict.refuse("état_changé:montant", f"montant inscrit {rla._money(doc.get('amount'))}, "
                                              f"le plan attend {rla._money(line.amount)}")
    if stored == line.target:
        verdict.refuse("déjà_la_cible", "la catégorie inscrite est déjà la cible, sans le fil "
                                        "de ce plan")
    elif stored != line.current:
        verdict.refuse("état_changé:catégorie", f"catégorie inscrite {rx._v(stored)}, le plan "
                                                f"attend {rx._v(line.current)}")


def judge_line(line: LineSpec, store, sha: str, duplicates: dict) -> Verdict:
    verdict = Verdict(line)
    if line.entry_id in duplicates:
        others = ", ".join(str(n) for n in duplicates[line.entry_id] if n != line.ligne)
        verdict.refuse("entrée_en_double", f"l'écriture figure aussi à la ligne {others} du "
                                           f"plan : aucune de ces lignes ne s'applique")
        return verdict
    snap = store.entries.get(line.entry_id)
    if snap is None:
        verdict.refuse("écriture_introuvable", "écriture introuvable au registre "
                                               "d'administration")
        return verdict
    doc = {**store.rows[line.entry_id], "id": line.entry_id}
    verdict.snap, verdict.doc = snap, doc
    if line.target not in al.ADMIN_EXPENSE_CATEGORIES:
        verdict.refuse("catégorie_cible_inconnue", f"catégorie cible « {line.target} » "
                                                   f"inconnue — une de : "
                                                   f"{', '.join(al.ADMIN_EXPENSE_CATEGORIES)}")
    if line.target == line.current:
        verdict.refuse("cible_égale_actuelle", f"la cible « {line.target} » est la catégorie "
                                               f"actuelle que le plan attend : la ligne ne "
                                               f"change rien")
    if verdict.reasons:
        return verdict
    if _trail_of(doc, sha):
        if doc.get(FIELD) == line.target:
            verdict.state = DONE
        else:
            verdict.refuse("état_mixte", f"état mixte : le fil de l'écriture porte ce plan, "
                                         f"mais sa catégorie {rx._v(doc.get(FIELD))} n'est pas "
                                         f"la cible")
        return verdict
    _compare(line, doc, verdict)
    for code, message in entry_refusals(doc, store):
        verdict.refuse(code, message)
    return verdict


@dataclass
class Plan:
    path: Path
    sha: str
    project: str
    lines: list
    store: Any
    verdicts: list
    writes: list = field(default_factory=list)
    payloads: dict = field(default_factory=dict)
    citing: dict = field(default_factory=dict)         # entry id → reconciliations citing it
    reconciliation_keys: list = field(default_factory=list)
    periods: tuple = ()
    before: dict = field(default_factory=dict)
    simulated: dict = field(default_factory=dict)
    expected: Any = None
    gaps: list = field(default_factory=list)
    impossible: list = field(default_factory=list)
    fingerprint: str = ""

    @property
    def scope(self) -> Scope:
        return Scope(tuple(dict.fromkeys(line.entry_id for line in self.lines)), self.periods)

    @property
    def blocking(self) -> list:
        return self.gaps + self.impossible

    def count(self, state: str) -> int:
        return sum(1 for v in self.verdicts if v.state == state)


def _plan_writes(plan: Plan) -> None:
    """The payloads of the lines to apply — stamps under
    ``writing_via("script")``, one trail entry per entry."""
    now = datetime.now(UTC)
    with provenance.writing_via("script"):
        for verdict in plan.verdicts:
            if verdict.state != PENDING:
                continue
            doc, line = verdict.doc, verdict.line
            business = {FIELD: line.target}
            changes = {FIELD: [doc.get(FIELD), line.target]}
            trail = {
                "at": now,
                "via": provenance.current_via(),
                "motif": line.trail_motif,
                "changes": changes,
                "updated_at_before": doc.get("updated_at"),
                "source_sha256": plan.sha,
            }
            plan.payloads[line.path] = {
                **business,
                "revisions": list(doc.get("revisions") or []) + [trail],
                **provenance.update_fields(now),
            }
            plan.writes.append(rla.Write(line.path, TX, line.entry_id, line.ligne, "écriture",
                                         verdict.snap, doc, business, changes))


def _fingerprint(head: dict, writes: list, payloads: dict, verdicts: list) -> str:
    rows = [{"ligne": w.ligne, "chemin": w.path,
             "update_time": rla._stamp_iso(w.snap.update_time),
             "apres": {k: payloads[w.path][k] for k in sorted(w.business)}}
            for w in sorted(writes, key=lambda w: w.ligne)]
    states = [{"ligne": v.line.ligne, "etat": v.state,
               "motifs": [[code, message] for code, message in v.reasons]}
              for v in sorted(verdicts, key=lambda v: v.line.ligne) if v.state != PENDING]
    return rla._digest({**head, "ecritures": rows, "lignes": states})[:FINGERPRINT_HEX]


def prepare(plan_path: Path) -> Plan:
    """Everything up to the write: read, judge, plan, simulate. Writes
    nothing. Raises :class:`RunRefused`."""
    plan_path = Path(plan_path)
    data, sha = _load_plan(plan_path)
    lines = parse_csv(data)
    store = rla.read_store()
    duplicates = _duplicates(lines)
    plan = Plan(path=plan_path, sha=sha, project=str(db.project), lines=lines, store=store,
                verdicts=[judge_line(line, store, sha, duplicates) for line in lines])
    keys: set = set()
    for verdict in plan.verdicts:
        if verdict.doc and verdict.line.entry_id not in plan.citing:
            citing, extra = rx._reconciliation_findings(
                store.recs, verdict.doc.get("account_id"), verdict.line.entry_id)
            plan.citing[verdict.line.entry_id] = citing
            keys |= set(extra)
    plan.reconciliation_keys = sorted(keys)
    _plan_writes(plan)
    pending = [v for v in plan.verdicts if v.state == PENDING]
    plan.periods = _years([v.doc for v in pending])
    scope = plan.scope
    plan.before = compute_controls(store, scope)
    plan.simulated = compute_controls(store, scope, plan.payloads)
    plan.expected = rla._attempt(expected_effects,
                                 [(v.doc, {**v.doc, FIELD: v.line.target}) for v in pending],
                                 scope.periods)
    plan.gaps, plan.impossible = compare_controls(plan.before, plan.simulated, plan.expected)
    plan.fingerprint = _fingerprint({"plan_sha256": sha, "projet": plan.project},
                                    plan.writes, plan.payloads, plan.verdicts)
    return plan


# ── backup ────────────────────────────────────────────────────────────────


def write_backup(directory: Path, plan: Plan) -> tuple[Path, str]:
    """Write, fsync, re-read and verify the backup; ``(path, sha256)``."""
    created = datetime.now(UTC)
    documents = []
    for write in sorted(plan.writes, key=lambda w: w.ligne):
        payload = plan.payloads[write.path]
        documents.append({
            "chemin": write.path,
            "ligne": write.ligne,
            "update_time_lu": rla._stamp_iso(write.snap.update_time),
            "etag_migration": payload["etag"],
            "champs": sorted(write.business),
            "avant": rla._typed(write.before),
            "apres": rla._typed({k: payload[k] for k in sorted(write.business)}),
        })
    body = {
        "format": BACKUP_FORMAT, "cree_le": created.isoformat(), "projet": plan.project,
        "plan": {"chemin": str(plan.path), "sha256": plan.sha},
        "empreinte": plan.fingerprint,
        "contexte": {"periodes": [[f.isoformat(), l.isoformat()] for f, l in plan.periods]},
        "documents": documents, "contenu_sha256": rla._digest(documents),
    }
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=1, allow_nan=False)
    path = directory / (f"categorie-reclassee-{created.strftime('%Y%m%dT%H%M%S%fZ')}"
                        f"-{plan.fingerprint}.json")
    with open(path, "x", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    reread = path.read_bytes()
    parsed = json.loads(reread.decode("utf-8"))
    if parsed != json.loads(text) or parsed["contenu_sha256"] != rla._digest(parsed["documents"]):
        raise OSError("la sauvegarde relue ne concorde pas")
    by_path = {w.path: w for w in plan.writes}
    for document in parsed["documents"]:
        if rla._untyped(document["avant"]) != by_path[document["chemin"]].before:
            raise OSError(f"la sauvegarde relue de {document['chemin']} ne concorde pas")
    return path, hashlib.sha256(reread).hexdigest()


# ── restoration ───────────────────────────────────────────────────────────


@dataclass
class RestorePlan:
    path: Path
    sha: str
    project: str
    created: str
    migration_fingerprint: str
    scope: Optional[Scope]
    state: str = RESTORE_PENDING
    store: Any = None
    rows: list = field(default_factory=list)     # (ligne, champ, actuel, restauré)
    refusals: list = field(default_factory=list)
    writes: list = field(default_factory=list)
    payloads: dict = field(default_factory=dict)
    involved: tuple = ()
    before: dict = field(default_factory=dict)
    simulated: dict = field(default_factory=dict)
    expected: Any = None
    gaps: list = field(default_factory=list)
    impossible: list = field(default_factory=list)
    fingerprint: str = ""

    @property
    def blocking(self) -> list:
        return self.refusals + self.gaps + self.impossible


def _check_backup(body: dict) -> tuple[list, Scope]:
    documents, context = body.get("documents"), body.get("contexte")
    if (not isinstance(documents, list) or not documents or not isinstance(context, dict)
            or body.get("contenu_sha256") != rla._digest(documents)):
        raise RunRefused("sauvegarde altérée : son contenu ne concorde pas avec son empreinte")
    seen: set = set()
    for document in documents:
        if (not isinstance(document, dict) or set(document) != set(_BACKUP_DOCUMENT_KEYS)
                or document["champs"] != [FIELD]
                or not isinstance(document["avant"], dict)
                or not isinstance(document["apres"], dict)
                or FIELD not in document["avant"] or FIELD not in document["apres"]
                or not isinstance(document["ligne"], int) or isinstance(document["ligne"], bool)
                or not isinstance(document["chemin"], str)
                or not document["chemin"].startswith(f"{TX}/")
                or not _addressable(document["chemin"].split("/", 1)[1])
                or document["chemin"] in seen):
            raise RunRefused("sauvegarde altérée : un document n'a pas la forme attendue")
        seen.add(document["chemin"])
    try:
        periods = tuple((date.fromisoformat(f), date.fromisoformat(l))
                        for f, l in context["periodes"])
    except (KeyError, TypeError, ValueError):
        raise RunRefused("sauvegarde altérée : contexte illisible") from None
    scope = Scope(tuple(d["chemin"].split("/", 1)[1] for d in documents), periods)
    return documents, scope


def prepare_restore(backup_path: Path) -> RestorePlan:
    backup_path = Path(backup_path)
    body, sha = rx._load_json(backup_path, "sauvegarde")
    if body.get("format") != BACKUP_FORMAT:
        raise RunRefused(f"sauvegarde d'un autre format ({body.get('format')!r}) — attendu "
                         f"{BACKUP_FORMAT}")
    if body.get("projet") != str(db.project):
        raise RunRefused(f"sauvegarde du projet « {body.get('projet')} », base « {db.project} »")
    documents, scope = _check_backup(body)
    plan = RestorePlan(path=backup_path, sha=sha, project=str(db.project),
                       created=str(body.get("cree_le")),
                       migration_fingerprint=str(body.get("empreinte")), scope=scope)
    plan.involved = tuple(sorted({str(rla._untyped(d[side]).get(FIELD))
                                  for d in documents for side in ("avant", "apres")}))
    store = rla.read_store()
    plan.store = store
    states, current = [], {}
    for document in sorted(documents, key=lambda d: d["ligne"]):
        ligne = document["ligne"]
        entry_id = document["chemin"].split("/", 1)[1]
        snap = store.entries.get(entry_id)
        if snap is None:
            plan.refusals.append(f"ligne {ligne} : écriture introuvable")
            states.append(None)
            continue
        doc = {**store.rows[entry_id], "id": entry_id}
        current[entry_id] = (snap, doc)
        before = rla._untyped(document["avant"])
        after = rla._untyped(document["apres"])
        length = _trail_length(doc)
        if doc.get("etag") == document["etag_migration"]:
            if doc.get(FIELD) != after.get(FIELD):
                plan.refusals.append(f"ligne {ligne} : etag de la migration, mais {FIELD} n'a "
                                     f"pas sa valeur")
            elif length is None or length + 1 > al._REVISIONS_CAP:
                plan.refusals.append(f"ligne {ligne} : le fil de révisions est illisible ou "
                                     f"plein — rien ne se rogne")
            states.append(RESTORE_PENDING)
        elif doc.get(FIELD) == before.get(FIELD) and _trail_of(doc, sha):
            states.append(RESTORE_DONE)
        else:
            plan.refusals.append(f"ligne {ligne} : l'écriture ne porte pas l'etag de la "
                                 f"migration (base {doc.get('etag')}, migration "
                                 f"{document['etag_migration']}) : la migration n'y est pas, ou "
                                 f"une écriture l'a suivie — rien ne se restaure par-dessus")
            states.append(None)
        plan.rows.append((ligne, FIELD, rx._v(doc.get(FIELD)), rx._v(before.get(FIELD))))
    if not plan.refusals:
        if set(states) == {RESTORE_DONE}:
            plan.state = RESTORE_DONE
        elif set(states) != {RESTORE_PENDING}:
            plan.refusals.append("état mixte : une ligne est restaurée, une autre non")
    pairs = []
    if plan.state == RESTORE_PENDING and not plan.refusals:
        now = datetime.now(UTC)
        with provenance.writing_via("script"):
            for document in sorted(documents, key=lambda d: d["ligne"]):
                entry_id = document["chemin"].split("/", 1)[1]
                snap, doc = current[entry_id]
                before = rla._untyped(document["avant"])
                business = {FIELD: before[FIELD]}
                changes = {FIELD: [doc.get(FIELD), before[FIELD]]}
                trail = {
                    "at": now,
                    "via": provenance.current_via(),
                    "motif": RESTORE_MOTIF.format(quand=rla._when(plan.created),
                                                  ligne=document["ligne"]),
                    "changes": changes,
                    "updated_at_before": doc.get("updated_at"),
                    "source_sha256": sha,
                }
                plan.payloads[document["chemin"]] = {
                    **business, "revisions": list(doc.get("revisions") or []) + [trail],
                    **provenance.update_fields(now)}
                plan.writes.append(rla.Write(document["chemin"], TX, entry_id, document["ligne"],
                                             "écriture", snap, doc, business, changes))
                pairs.append((doc, {**doc, **business}))
    plan.before = compute_controls(store, scope)
    plan.simulated = compute_controls(store, scope, plan.payloads)
    if plan.writes:
        plan.expected = rla._attempt(expected_effects, pairs, scope.periods)
        plan.gaps, plan.impossible = compare_controls(plan.before, plan.simulated, plan.expected)
    plan.fingerprint = _fingerprint({"sauvegarde_sha256": sha, "projet": plan.project},
                                    plan.writes, plan.payloads, [])
    return plan


# ── after the write ───────────────────────────────────────────────────────


def after_real(scope: Scope) -> dict:
    """The controls on a fresh read, once written."""
    try:
        store = rla.read_store()
    except RunRefused as exc:
        reason = Impossible(str(exc))
        return {key: reason for key in _CONTROLS}
    return compute_controls(store, scope)


def _post_write(scope: Scope, before: dict, simulated: dict, expected) -> tuple:
    real = after_real(scope)
    gaps, impossible = compare_controls(before, real, expected)
    return real, gaps + impossible, rla._differences(simulated, real)


# ── the listing (--lister) ────────────────────────────────────────────────


def _floors(recs: list) -> dict:
    """Each account's lock floor — the period end of its latest COMPLETED
    reconciliation (``admin_ledger._read_lock_floor``), as midnight UTC. A
    completed reconciliation whose end is unreadable stops the run: its
    account's lock would be unknown, and an unknown lock never reads as
    none."""
    ends: dict = {}
    for rec in recs:
        if rec.get("status") != "complétée":
            continue
        end = rla._day(rec.get("period_end"))
        if end is None:
            raise RunRefused(f"conciliation {rec.get('id')} sans fin de période lisible — le "
                             f"verrou de son compte est inconnu")
        account_id = rec.get("account_id") or ""
        ends[account_id] = max(ends.get(account_id, end), end)
    return {account_id: rla._midnight(end) for account_id, end in ends.items()}


def _lock_text(doc: dict, reason: Optional[str], floor: Optional[datetime]) -> str:
    """The model's lock reason (``_entry_lock_reason``), with what explains it."""
    if reason is None:
        return "non verrouillée"
    detail = ""
    if reason == "période_verrouillée" and floor is not None:
        detail = f"période conciliée jusqu'au {floor.date().isoformat()}"
    elif reason == "écriture_verrouillée":
        paired = bool(doc.get("reverses_id") or doc.get("reversed_by_id"))
        detail = ("membre d'une contre-passation" if paired
                  else al.TX_STATUS_LABELS.get(doc.get("status"), str(doc.get("status"))).lower())
    return f"verrouillée : {reason}" + (f" ({detail})" if detail else "")


def candidates(store, category: str, payee: Optional[str]) -> list[dict]:
    """The dépenses filed under *category* — whose counterparty contains
    *payee*, case aside, when given — in day, account and sequence order."""
    needle = payee.casefold() if payee is not None else None
    out = []
    for entry_id, row in store.rows.items():
        doc = {**row, "id": entry_id}
        if doc.get("kind") not in al.EXPENSE_KINDS or doc.get(FIELD) != category:
            continue
        if needle is not None and needle not in str(doc.get("counterparty") or "").casefold():
            continue
        out.append(doc)
    return sorted(out, key=lambda d: (rla._day(d.get("date")) or date.min,
                                      str(d.get("account_id") or ""),
                                      rla._as_int(d.get("sequence")) or 0, d["id"]))


def list_candidates(category: str, payee: Optional[str]) -> int:
    """``--lister``: what a plan may name — read-only, no file created."""
    if category not in al.ADMIN_EXPENSE_CATEGORIES:
        rla._out(f"Refusé — rien n'a été lu ni écrit : catégorie « {category} » inconnue — une "
                 f"de : {', '.join(al.ADMIN_EXPENSE_CATEGORIES)}")
        return 1
    if payee is not None and not payee.strip():
        rla._out("Refusé — rien n'a été lu ni écrit : --beneficiaire attend un texte non vide")
        return 1
    try:
        store = rla.read_store()
        floors = _floors(store.recs)
    except RunRefused as exc:
        rla._out(f"Refusé — rien n'a été écrit : {exc}")
        return 1
    docs = candidates(store, category, payee)
    rows, skeleton = [], []
    locked = refused = 0
    for doc in docs:
        account_id = doc.get("account_id") or ""
        floor = floors.get(account_id)
        reason = al._entry_lock_reason(doc, floor)
        refusals = entry_refusals(doc, store)
        locked += reason is not None
        refused += bool(refusals)
        day = rla._day(doc.get("date"))
        account = store.accounts.get(account_id) or {}
        rows.append((f"`{doc['id']}`", account.get("name") or account_id or "—",
                     day.isoformat() if day else "?", rla._money(doc.get("amount")),
                     doc.get("amount"), doc.get("counterparty") or "—", doc.get(FIELD),
                     _lock_text(doc, reason, floor),
                     ("refuserait — " + " ; ".join(m for _c, m in refusals)) if refusals
                     else "accepterait"))
        if not refusals:
            skeleton.append(rla._reserialize([str(len(skeleton) + 1), doc["id"], day.isoformat(),
                                              str(doc["amount"]), category, "", ""]))
    title = (f"Dépenses classées « {category} » ({al.ADMIN_CATEGORY_LABELS[category]})"
             + (f", bénéficiaire contenant « {payee} »" if payee is not None else "")
             + " — liste en lecture seule")
    out = [title, f"Projet : {db.project}",
           (f"Écritures : {len(docs)} — {locked} verrouillée(s), {refused} que le script "
            f"refuserait."), ""]
    if rows:
        out += rx._table(("Écriture", "Compte", "Jour", "Montant", "Cents", "Bénéficiaire",
                          "Catégorie", "Verrou", "Le script"), rows)
    else:
        out.append("Aucune écriture.")
    if skeleton:
        out += ["", ("Lignes de plan des écritures que le script accepterait (categorie_cible "
                     "et motif à remplir, puis ne garder que les écritures à reclasser) :"),
                ",".join(CSV_COLUMNS)] + skeleton
        out += ["", ("Le plan est un CSV hors du dépôt. Ensuite, à blanc : "
                     "python -m scripts.reclassify_admin_category PLAN.csv --rapport RAPPORT.md")]
    out.append("Rien n'a été écrit.")
    for line in out:
        rla._out(line)
    return 0


# ── the report ────────────────────────────────────────────────────────────


def _scope_accounts(store, scope: Scope) -> list:
    return sorted({str((store.rows.get(i) or {}).get("account_id") or "")
                   for i in scope.entry_ids if i in store.rows})


def _render_controls(scope: Scope, store, before: dict, simulated: dict,
                     real: Optional[dict], involved: tuple) -> list:
    columns = ("Contrôle", "Avant", "Après (simulé)") + (("Après (réel)",) if real else ())

    def row(label, key, fn):
        cells = [label, rx._shown(rx._get(before, key), fn), rx._shown(rx._get(simulated, key), fn)]
        if real:
            cells.append(rx._shown(rx._get(real, key), fn))
        return tuple(cells)

    accounts = _scope_accounts(store, scope)

    def name(account_id):
        return (store.accounts.get(account_id) or {}).get("name") or account_id or "?"

    def ledger(c):
        parts = []
        for account_id in accounts:
            account = store.accounts.get(account_id) or {}
            mine = c["soldes"].get(account_id) or {}
            label = al.BALANCE_LABELS.get(account.get("account_type", ""), "Solde")
            shown = al.display_balance(account.get("account_type", ""),
                                       int(mine.get("ledger_balance") or 0))
            parts.append(f"{name(account_id)} : {label} {rla._money(shown)} "
                         f"(Σ {rla._money(mine.get('somme'))} au registre)")
        return " ; ".join(parts + [f"{len(c['soldes_courants'])} soldes courants",
                                   f"{len(c['sequences'])} séquences"])

    def lines(c):
        return " ; ".join("absente" if v is None
                          else f"{rx._v(v['categorie'])} (fil {v['longueur']})"
                          for v in c.values())

    def period(statement):
        parts = [f"dépenses (brut) {rla._money(statement['total_expenses'])}"]
        for category in involved:
            parts.append(f"{category} {rla._money(statement['expenses'].get(category, 0))} "
                         f"(net {rla._money(statement['expenses_net'].get(category, 0))})")
        return " ; ".join(parts + [f"TPS {rla._money(statement['gst'])}",
                                   f"TVQ {rla._money(statement['qst'])}"])

    def lock(c):
        parts = []
        for account_id in accounts:
            mine = c["conciliations"].get(account_id) or {}
            proofs = mine.get("preuves") or {}
            bad = sum(1 for p in proofs.values() if p.get("ecart"))
            last = mine.get("derniere")
            last_text = (f"dernière {last.get('fin')} ({last.get('statut')})" if last
                         else "aucune conciliation")
            parts.append(f"{name(account_id)} : plancher {mine.get('plancher') or 'aucun'} ; "
                         f"{last_text} ; {len(proofs)} preuve(s), {bad} en écart")
        reasons = sorted({str(r or "aucun") for r in c["lock_reasons"].values()})
        return " ; ".join(parts + [f"lock_reason {', '.join(reasons) or '—'}"])

    rows = [row("1. Soldes des comptes, soldes courants, séquences", "1", ledger),
            row("2. Les écritures du plan : catégorie (fil)", "2", lines)]
    for first, last in scope.periods:
        key = _period_key(first, last)
        rows.append(row(f"3. Du {first} au {last}", "3", lambda c, k=key: period(c[k])))
    rows += [row("3. Journal entier", "3", lambda c: period(c["journal"])),
             row("4. Verrou et conciliations", "4", lock),
             row("5. Autres documents lus", "5", lambda c: f"{len(c)} document(s)")]
    return rx._table(columns, rows)


def _involved(plan: Plan) -> tuple:
    """The categories the plan's lines name — the ones control 3 shows."""
    names = set()
    for line in plan.lines:
        names.add(line.current)
        if line.target in al.ADMIN_EXPENSE_CATEGORIES:
            names.add(line.target)
    return tuple(sorted(names))


def render_report(plan: Plan, *, mode: str, approved: Optional[str] = None,
                  refusal: Optional[list] = None, outcome: Optional[str] = None,
                  backup: Optional[tuple] = None, real: Optional[dict] = None,
                  drift: Optional[list] = None, real_gaps: Optional[list] = None) -> str:
    store = plan.store
    out = [
        "# Reclassement de la catégorie de dépenses d'administration",
        "",
        f"- Date : {rla._now_line()}",
        f"- Mode : {mode}",
        f"- Projet : `{plan.project}`",
        f"- Plan : `{plan.path}`",
        f"- SHA-256 du plan : `{plan.sha}`",
        f"- Empreinte : **{plan.fingerprint}**",
    ]
    if approved:
        out.append(f"- Empreinte approuvée : `{approved}`")
    out += ["", "## État", "",
            (f"- Lignes : {plan.count(PENDING)} à appliquer, {plan.count(DONE)} déjà "
             f"appliquée(s), {plan.count(REFUSED)} refusée(s).")]
    blocking = plan.blocking + list(refusal or [])
    if blocking:
        out.append("- **L'application est refusée** :")
        out += [f"  - {item}" for item in dict.fromkeys(blocking)]
    elif plan.writes:
        out.append("- Rien ne s'oppose à l'application des lignes à appliquer : les contrôles "
                   "simulés sont conformes.")
    if outcome:
        out += ["", "## Issue", "", outcome]
    if backup:
        out += ["", f"- Sauvegarde : `{backup[0]}`",
                f"- SHA-256 de la sauvegarde : `{backup[1]}`"]
    if drift is not None:
        out.append("- Écart entre l'après réel et l'après simulé : "
                   + ("aucun" if not drift else "; ".join(drift)))
    if real_gaps is not None:
        out.append("- Contrôles après réel : " + ("conformes" if not real_gaps
                                                  else "; ".join(real_gaps)))

    out += ["", "## 1. Lignes", ""]
    rows = []
    for v in plan.verdicts:
        doc = v.doc
        account = (store.accounts.get(doc.get("account_id")) or {}) if doc else {}
        rows.append((
            v.line.ligne, f"`{v.line.entry_id}`",
            (account.get("name") or doc.get("account_id") or "?") if doc else "—",
            rx._v(rx._aware(doc.get("date"))) if doc else "—",
            rla._money(doc.get("amount")) if doc else "—",
            (doc.get("counterparty") or "—") if doc else "—",
            rx._v(doc.get(FIELD)) if doc else "—", f"« {v.line.target} »", v.state,
            v.line.trail_motif,
            "; ".join(f"`{code}` — {message}" for code, message in v.reasons) or "—"))
    out += rx._table(("Ligne", "Écriture", "Compte", "Jour", "Montant", "Bénéficiaire",
                      "Catégorie inscrite", "Cible", "État", "Motif du fil", "Refus"), rows)

    out += ["", "## 2. Ce que le plan attend, et ce que porte la base", ""]
    table = [r for v in plan.verdicts for r in v.table]
    out += rx._table(("Document", "Champ", "Attendu (plan)", "Base", ""),
                     [(d, f, w, h, "conforme" if ok else "**ÉCART**")
                      for d, f, w, h, ok in table]) if table else ["(sans objet)"]

    out += ["", "## 3. Changements prévus", ""]
    if plan.writes:
        for write in sorted(plan.writes, key=lambda w: w.ligne):
            payload = plan.payloads[write.path]
            # Read defensively: a payload gone astray is when this is read.
            revisions = payload.get("revisions")
            revisions = revisions if isinstance(revisions, list) else []
            trail = revisions[-1] if revisions and isinstance(revisions[-1], dict) else {}
            out += [(f"### Ligne {write.ligne} — écriture `{write.doc_id}` "
                     f"(séq. {write.before.get('sequence')})"), ""]
            out += rx._table(("Champ", "Avant", "Après"),
                             [(f"`{k}`", rx._v(old), rx._v(new))
                              for k, (old, new) in write.changes.items()])
            out += ["",
                    (f"Estampilles : `updated_at`, `etag`, `updated_via` = "
                     f"`{payload.get('updated_via')}` ; `revisions` : "
                     f"{len(write.before.get('revisions') or [])} → {len(revisions)}, "
                     f"motif « {trail.get('motif')} », via `{trail.get('via')}`."), ""]
        out.append("Aucun compte, aucune conciliation, aucune autre écriture n'est écrit.")
    else:
        out.append("Aucun.")

    out += ["", "## 4. Champs employés", "",
            ("- Seul `category` est écrit sur chaque écriture — un code de "
             "`admin_ledger.ADMIN_EXPENSE_CATEGORIES` —, avec les estampilles et une entrée du "
             "fil `revisions` ; jamais le montant, la ventilation (`net_amount`, `gst_amount`, "
             "`qst_amount`), la date, la nature, le sens, le statut, le dossier, un lien ni la "
             "contrepartie."),
            ("- Provenance : `script` — `updated_via` et `via` du fil, sous "
             "`provenance.writing_via`, comme les migrations du 7 octobre et du 5 novembre."),
            "", "## 5. Verrou, conciliations, taxes", ""]
    lock_before = rx._get(plan.before, "4")
    lock_after = rx._get(plan.simulated, "4")
    for line in plan.lines:
        before = rx._shown(lock_before,
                           lambda c, i=line.entry_id: c["lock_reasons"].get(i) or "aucun")
        after = rx._shown(lock_after,
                          lambda c, i=line.entry_id: c["lock_reasons"].get(i) or "aucun")
        out.append(f"- Ligne {line.ligne} : `lock_reason` (dérivé à la lecture, jamais stocké) "
                   f"{before} → {after} ; conciliations qui la citent : "
                   + (", ".join(plan.citing.get(line.entry_id) or []) or "aucune") + ".")
    out += [("- Une écriture verrouillée est acceptée : une catégorie ne change aucun solde (le "
             "montant et le sens restent), aucune conciliation et aucun total de taxes (la "
             "ventilation reste) — seule la catégorie sous laquelle la dépense est présentée "
             "change."),
            "- Champs des conciliations hors de la forme du modèle : "
            + (", ".join(f"`{k}`" for k in plan.reconciliation_keys) or "aucun")
            + " — une conciliation garde des montants bruts (relevé, livre, en circulation) "
              "et les identifiants cochés, jamais une catégorie : aucune ne fige la catégorie "
              "d'une dépense.",
            ("- Ni le plancher, ni la dernière conciliation, ni aucune conciliation ne sont "
             "écrits.")]

    out += ["", "## 6. Contrôles", ""]
    out += _render_controls(plan.scope, store, plan.before, plan.simulated, real, _involved(plan))
    out += ["", "Écarts : " + ("aucun" if not (plan.gaps or plan.impossible)
                               else "; ".join(plan.gaps + plan.impossible))]

    out += ["", "## 7. Caches", "",
            ("Aucun à recalculer : les totaux par catégorie se calculent à la lecture "
             "(`results_statement`), et le seul solde dénormalisé, "
             "`admin_accounts.ledger_balance`, ne bouge pas (même montant)."),
            "", "## 8. Suite", ""]
    if not plan.writes:
        out.append("Rien à écrire.")
    elif approved is None:
        out.append(f"Après approbation : `--sauvegarde DOSSIER --appliquer {plan.fingerprint}` ; "
                   f"puis les deux contrôles d'intégrité, comparés par texte.")
    else:
        out.append("Relancer les deux contrôles d'intégrité (`scripts.verify_admin_integrity`, "
                   "`scripts.verify_trust_integrity`) et comparer leurs constats par texte.")
    return "\n".join(out) + "\n"


def render_restore_report(plan: RestorePlan, *, mode: str, approved: Optional[str] = None,
                          refusal: Optional[list] = None, outcome: Optional[str] = None,
                          real: Optional[dict] = None, drift: Optional[list] = None) -> str:
    out = [
        "# Restauration d'un reclassement de catégorie",
        "",
        f"- Date : {rla._now_line()}",
        f"- Mode : {mode}",
        f"- Projet : `{plan.project}`",
        f"- Sauvegarde : `{plan.path}` (créée le {plan.created})",
        f"- SHA-256 de la sauvegarde : `{plan.sha}`",
        f"- Empreinte de la migration : `{plan.migration_fingerprint}`",
        f"- Empreinte : **{plan.fingerprint}**",
    ]
    if approved:
        out.append(f"- Empreinte approuvée : `{approved}`")
    out += ["", "## État", "", f"- « {plan.state} »"]
    blocking = plan.blocking + list(refusal or [])
    if blocking:
        out.append("- **La restauration est refusée** :")
        out += [f"  - {item}" for item in dict.fromkeys(blocking)]
    if outcome:
        out += ["", "## Issue", "", outcome]
    if drift is not None:
        out.append("- Écart entre l'après réel et l'après simulé : "
                   + ("aucun" if not drift else "; ".join(drift)))
    out += ["", "## Champs", ""]
    out += rx._table(("Ligne", "Champ", "Actuel", "Restauré"), plan.rows) if plan.rows else ["—"]
    if plan.scope is not None and plan.store is not None:
        out += ["", "## Contrôles", ""]
        out += _render_controls(plan.scope, plan.store, plan.before, plan.simulated, real,
                                plan.involved)
        out += ["", "Écarts : " + ("aucun" if not (plan.gaps or plan.impossible)
                                   else "; ".join(plan.gaps + plan.impossible))]
    return "\n".join(out) + "\n"


# ── the command line ──────────────────────────────────────────────────────


def _summary(plan: Plan, mode: str) -> list:
    return [
        f"Reclassement de catégorie — {mode}",
        f"Projet : {plan.project}",
        f"Plan : {plan.path} (SHA-256 {plan.sha})",
        (f"Lignes : {plan.count(PENDING)} à appliquer, {plan.count(DONE)} déjà appliquée(s), "
         f"{plan.count(REFUSED)} refusée(s)"),
        "Contrôles simulés : " + ("conformes" if not plan.gaps and not plan.impossible else
                                  f"{len(plan.gaps)} écart(s), {len(plan.impossible)} "
                                  f"impossible(s)"),
        f"Empreinte : {plan.fingerprint}",
    ]


def _run_migration(report: Path, source: Path, backup_dir: Optional[Path],
                   approved: Optional[str]) -> int:
    try:
        plan = prepare(source)
    except RunRefused as exc:
        return rla._finish(report, f"# Reclassement de catégorie\n\n- Date : "
                                   f"{rla._now_line()}\n- Plan : `{source}`\n\n**Refusé — rien "
                                   f"n'a été écrit** : {exc}\n",
                           [f"Refusé — rien n'a été écrit : {exc}"], 1)
    if approved is None:
        summary = _summary(plan, "à blanc")
        if plan.blocking:
            summary.append(f"L'application serait refusée ({len(plan.blocking)} motif(s), voir "
                           f"le rapport). Rien n'a été écrit.")
            return rla._finish(report, render_report(plan, mode="à blanc"), summary, 1)
        if plan.writes:
            summary.append("À blanc : rien n'a été écrit. Pour écrire, après approbation : "
                           f"--sauvegarde DOSSIER --appliquer {plan.fingerprint}")
        else:
            summary.append("À blanc : rien n'a été écrit, et rien n'est à écrire.")
        return rla._finish(report, render_report(plan, mode="à blanc"), summary, 0)

    refusal = []
    if approved != plan.fingerprint:
        refusal.append(f"l'empreinte a changé depuis la passe approuvée ({approved} → "
                       f"{plan.fingerprint}) : relancez à blanc et faites-la approuver")
    refusal += plan.blocking
    if refusal:
        summary = _summary(plan, "application refusée")
        summary += [f"Refusé — rien n'a été écrit : {r}" for r in dict.fromkeys(refusal)]
        return rla._finish(report, render_report(plan, mode="application refusée",
                                                 approved=approved, refusal=refusal), summary, 1)
    if not plan.writes:
        summary = _summary(plan, "application") + ["Rien à écrire."]
        return rla._finish(report, render_report(plan, mode="application", approved=approved,
                                                 outcome="Rien à écrire."), summary, 0)
    try:
        backup = write_backup(backup_dir, plan)
    except Exception as exc:
        message = f"sauvegarde impossible ({type(exc).__name__}) — rien n'a été écrit"
        return rla._finish(report, render_report(plan, mode="application refusée",
                                                 approved=approved, refusal=[message]),
                           _summary(plan, "application refusée") + [f"Refusé : {message}"], 1)
    outcome, error = rla.commit(plan.writes, plan.payloads)
    sentence = rla._outcome_sentence(outcome, error, len(plan.writes))
    # Said at once: whatever happens next, the outcome is never lost.
    rla._out(sentence)
    real = drift = real_gaps = None
    summary = _summary(plan, "application") + [f"Sauvegarde : {backup[0]}"]
    if outcome in (rla.WRITTEN, rla.LOST_BUT_WRITTEN):
        real, real_gaps, drift = _post_write(plan.scope, plan.before, plan.simulated,
                                             plan.expected)
        summary.append("Contrôles après réel : " + ("conformes" if not real_gaps
                                                    else f"{len(real_gaps)} écart(s)"))
        summary.append("Écart à la passe approuvée : " + ("aucun" if not drift
                                                          else f"{len(drift)} différence(s)"))
        summary.append("Relancez les deux contrôles d'intégrité (scripts.verify_admin_integrity, "
                       "scripts.verify_trust_integrity) et comparez leurs constats par texte.")
    text = render_report(plan, mode="application", approved=approved, outcome=sentence,
                         backup=backup, real=real, drift=drift, real_gaps=real_gaps)
    code = 0 if outcome in (rla.WRITTEN, rla.LOST_BUT_WRITTEN) else 1
    return rla._finish(report, text, summary, code)


def _run_restore(report: Path, source: Path, approved: Optional[str]) -> int:
    try:
        plan = prepare_restore(source)
    except RunRefused as exc:
        return rla._finish(report, f"# Restauration\n\n- Date : {rla._now_line()}\n"
                                   f"- Sauvegarde : `{source}`\n\n**Refusé — rien n'a été "
                                   f"écrit** : {exc}\n",
                           [f"Refusé — rien n'a été écrit : {exc}"], 1)
    head = [(f"Restauration — {'à blanc' if approved is None else 'application'}"),
            f"Projet : {plan.project}",
            f"Sauvegarde : {plan.path} (SHA-256 {plan.sha})",
            (f"État : {plan.state} ; documents à écrire : {len(plan.writes)} ; "
             f"refus : {len(plan.blocking)}"),
            f"Empreinte : {plan.fingerprint}"]
    if approved is None:
        if plan.blocking:
            return rla._finish(report, render_restore_report(plan, mode="restauration à blanc"),
                               head + ["La restauration serait refusée (voir le rapport)."], 1)
        if plan.state == RESTORE_DONE:
            head.append("À blanc : rien n'a été écrit, et rien n'est à restaurer.")
        else:
            head.append("À blanc : rien n'a été écrit. Pour restaurer, après approbation : "
                        f"--appliquer {plan.fingerprint}")
        return rla._finish(report, render_restore_report(plan, mode="restauration à blanc"),
                           head, 0)
    if plan.state == RESTORE_DONE:
        return rla._finish(report, render_restore_report(plan, mode="restauration",
                                                         approved=approved,
                                                         outcome="Rien à écrire."),
                           head + ["Rien à écrire : déjà restaurée."], 0)
    refusal = []
    if approved != plan.fingerprint:
        refusal.append(f"l'empreinte a changé ({approved} → {plan.fingerprint}) : relancez à "
                       f"blanc")
    refusal += plan.blocking
    if not refusal and not plan.writes:
        refusal.append("rien à restaurer")
    if refusal:
        return rla._finish(report, render_restore_report(plan, mode="restauration refusée",
                                                         approved=approved, refusal=refusal),
                           head + [f"Refusé — rien n'a été écrit : {r}"
                                   for r in dict.fromkeys(refusal)], 1)
    outcome, error = rla.commit(plan.writes, plan.payloads)
    sentence = rla._outcome_sentence(outcome, error, len(plan.writes))
    rla._out(sentence)
    real = drift = None
    if outcome in (rla.WRITTEN, rla.LOST_BUT_WRITTEN):
        real, real_gaps, drift = _post_write(plan.scope, plan.before, plan.simulated,
                                             plan.expected)
        head.append("Contrôles après réel : " + ("conformes" if not real_gaps
                                                 else f"{len(real_gaps)} écart(s)"))
    text = render_restore_report(plan, mode="restauration", approved=approved,
                                 outcome=sentence, real=real, drift=drift)
    return rla._finish(report, text, head,
                       0 if outcome in (rla.WRITTEN, rla.LOST_BUT_WRITTEN) else 1)


def main(argv: Optional[list] = None) -> int:
    parser = rla._Parser(prog="python -m scripts.reclassify_admin_category",
                         description="Reclassement de la catégorie de dépenses "
                                     "d'administration (hors modèle, à blanc par défaut).")
    parser.add_argument("plan", nargs="?", help="le plan CSV approuvé (hors du dépôt public)")
    parser.add_argument("--rapport", metavar="RAPPORT.md",
                        help="le rapport à écrire (hors du dépôt ; nouveau fichier)")
    parser.add_argument("--sauvegarde", metavar="DOSSIER",
                        help="le dossier où écrire la sauvegarde avant le lot (hors du dépôt)")
    parser.add_argument("--appliquer", metavar="EMPREINTE",
                        help="écrire — avec l'empreinte de la passe à blanc approuvée")
    parser.add_argument("--restaurer", metavar="SAUVEGARDE.json",
                        help="restaurer les catégories sauvegardées (à blanc sans --appliquer)")
    parser.add_argument("--lister", metavar="CATEGORIE",
                        help="lister, sans rien écrire, les dépenses classées sous CATEGORIE")
    parser.add_argument("--beneficiaire", metavar="TEXTE",
                        help="avec --lister : seulement les bénéficiaires qui contiennent "
                             "TEXTE (sans égard à la casse)")
    args = parser.parse_args(argv if argv is not None else [])
    if args.lister is not None or args.beneficiaire is not None:
        if args.lister is None:
            rla._out("Refusé — rien n'a été lu ni écrit : --beneficiaire ne sert qu'avec "
                     "--lister CATEGORIE")
            return 1
        others = [name for name, value in (("le plan", args.plan), ("--rapport", args.rapport),
                                           ("--sauvegarde", args.sauvegarde),
                                           ("--appliquer", args.appliquer),
                                           ("--restaurer", args.restaurer)) if value is not None]
        if others:
            rla._out(f"Refusé — rien n'a été lu ni écrit : --lister se lance seul (sans "
                     f"{', '.join(others)}) — il lit la base et n'écrit rien")
            return 1
        return list_candidates(args.lister, args.beneficiaire)
    try:
        report, backup_dir, source = rx._check_paths(args)
    except RunRefused as exc:
        rla._out(f"Refusé — rien n'a été créé ni écrit : {exc}")
        return 1
    if args.restaurer:
        return _run_restore(report, source, args.appliquer)
    return _run_migration(report, source, backup_dir, args.appliquer)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
