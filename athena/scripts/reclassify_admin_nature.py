"""Reclassify, OUTSIDE the models, the nature of administration entries —
with the category, the TPS/TVQ split and the transfer purpose that nature
carries — a one-shot migration bound to its plan (lot E of the financial
statements, the lawyer's decisions of 2026-11-08).

What it does
------------
On 2026-11-08 the administration ledger gained ``avance_honoraires`` (a
client's advance received in the operations account instead of in trust),
``remboursement_depense`` (a supplier's refund of an expense) and the
purpose « avances » of a virement interne sortant (``transfer_purpose``).
The past entries of those kinds were recorded under the natures that
existed — the advances as « autre recette », counted as revenue; their
transfers to trust unmarked; a supplier's credit as « autre recette » — and
most of them are LOCKED (compensée, or in a reconciled period):
``admin_ledger.update_transaction`` refuses them, as an edit must, and the
house correction (a reversal and a new entry dated today) would move them
out of their period. This script writes, on each entry the lawyer's PLAN
names, the target state — its nature, category, split and purpose — and
nothing else.

Why it writes outside the models
--------------------------------
CLAUDE.md, « One door per rule »: a declared exception, on the pattern of
``scripts/reclassify_admin_category``, whose helpers it imports and leaves
untouched; it closes with its plan. A LOCKED entry is ACCEPTED — that is the
point: a change of nature within one sign moves no balance (the amount and
the direction stay) and no reconciliation (a reconciliation keeps gross
amounts and the ids it cleared); only how the results, the TPS/TVQ and the
statements read the entry moves. The target is the MODEL's own: each line's
target state is judged by ``admin_ledger``'s rules — the passage
(``kind_move_allowed``), the fields the target kind refuses
(``_nature_refusal``, ``_purpose_refusal``, ``NO_DOSSIER_KINDS``), and
``_validate_business``, whose normalized split is the one written.

What it writes, on each entry: the fields of :data:`BUSINESS_FIELDS` whose
target differs from the stored value, the house stamps (``updated_at``, a
new ``etag``, ``updated_via = "script"`` under ``provenance.writing_via``)
and ONE ``revisions`` trail entry ``{at, via, motif, changes,
updated_at_before, source_sha256}``. Never the amount, the direction, a
date, the status, the dossier, a link or the counterparty; no account and no
reconciliation. An entry carrying more than ``_REVISIONS_CAP - 2`` trail
entries is refused, which keeps room for this write and one restoration.
The registers are not DAV-exposed: no CTag.

The plan
--------
A CSV, UTF-8 (a BOM is accepted), outside the repository, its quoting read
strictly, with this header (:data:`CSV_COLUMNS`) and one line per entry::

    ligne,entry_id,date,montant_cents,nature_actuelle,categorie_actuelle,
    objet_actuel,nature_cible,categorie_cible,net_cible,tps_cible,tvq_cible,
    objet_cible,motif

``date`` (the entry's UTC day), ``montant_cents``, ``nature_actuelle``,
``categorie_actuelle`` and ``objet_actuel`` state what the line expects the
base to hold — an empty category or purpose: none. The ``*_cible`` cells
state the target: an empty category or purpose is none; three empty split
cells take the model's split for the target kind (none on a recette, the
canonical one outside the results), and a dépense or a refund names its
three. ``motif`` is the lawyer's reason, stored as is — it must survive
``security.sanitize`` unaltered and carry no leading or trailing space — and
the trail reads « motif — ligne n ». Any error of form refuses the whole run
before anything is read.

Judging a line — every read is strict: a failed read stops the run
------------------------------------------------------------------
A line is REFUSED, with a code and a French reason, and reported — never
forced; the other lines still apply. It is refused when its entry also
appears on another line (both are); is absent; when the stored day, amount,
nature, category or purpose is not the one the line states (« état
changé »); when its entry is a card payment, a correction or an invoice
receipt, a member of a reversal seen from either end, annulée, linked to an
invoice or to a trust entry, its day or amount unreadable, or its trail
unreadable or nearly full; and when its target is one the model refuses. A
line whose entry holds its target with this plan's trail entry (same
``source_sha256``) is « déjà appliquée »: the migration is idempotent; the
target without that trail entry is refused (« déjà la cible »), the trail
entry without the target too (« état mixte »).

The controls, the fingerprint, the backup and the restoration
-------------------------------------------------------------
Those of ``scripts/reclassify_admin_category``, the written fields widened
to :data:`BUSINESS_FIELDS`: five controls BEFORE, AFTER SIMULATED and, once
written, AFTER (re-read) — the ledger's balances, running balances and
sequences; each plan line's entry moving only its written fields, from its
stored state to its target, and its trail by exactly one entry; the results
statement over each year holding a written entry and over the whole
journal, moved by exactly what the same function says of the written lines
alone; the lock floors and the re-proof of every completed reconciliation;
every other document read. A control that cannot be computed, or that moves
otherwise, refuses ``--appliquer``. The fingerprint covers the plan's
digest, ``db.project``, every write (path, the ``update_time`` read, the
fields written) and every refused or applied line with its reasons; the
typed JSON backup of each document — whole — is written, fsynced, read back
and verified before ONE batch guarded by the ``update_time`` each was read
at; ``--restaurer`` (dry by default, its own fingerprint) puts the saved
fields back, all or nothing, and refuses everything when an entry's etag is
no longer the migration's.

Running it — from ``athena/``, with ADC and the environment of « Active
gabarits » in DEPLOYMENT.md: even the dry run reads PRODUCTION. A plan,
report, backup or backup directory inside the repository is refused before
anything is created; the report must be a new file.

    python -m scripts.reclassify_admin_nature PLAN.csv --rapport R.md
    python -m scripts.reclassify_admin_nature PLAN.csv --rapport R.md \\
        --sauvegarde DOSSIER --appliquer EMPREINTE
    python -m scripts.reclassify_admin_nature --restaurer SAUVEGARDE.json \\
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
from scripts import reclassify_admin_category as rc
from scripts import reclassify_admin_ledger as rla
from scripts import reclassify_as_invoice_receipt as rx
from scripts.rectify_registers import _addressable

UTC = timezone.utc
TX = al.TRANSACTIONS_COLLECTION
#: The plan's header, in this order.
CSV_COLUMNS = ("ligne", "entry_id", "date", "montant_cents", "nature_actuelle",
               "categorie_actuelle", "objet_actuel", "nature_cible", "categorie_cible",
               "net_cible", "tps_cible", "tvq_cible", "objet_cible", "motif")
#: The business fields this script may write — a nature and what it carries.
BUSINESS_FIELDS = ("kind", "category", "net_amount", "gst_amount", "qst_amount",
                   "transfer_purpose")
_SPLIT = ("net_amount", "gst_amount", "qst_amount")
#: What a write moves on an entry: its business fields, the trail and the
#: stamps. Control 2 holds every other field to its stored value.
_WRITTEN = BUSINESS_FIELDS + ("revisions", "updated_at", "etag", "updated_via")
#: The kinds whose nature never changes here: minted by their own writers.
_STRUCTURAL_KINDS = ("paiement_carte", al.REVERSAL_KIND, "encaissement_facture")
MOTIF_FORMAT = "{motif} — ligne {ligne}"
MAX_TRAIL_BEFORE = al._REVISIONS_CAP - 2
FINGERPRINT_HEX = 16
BACKUP_FORMAT = "athena.nature-reclassee/1"
RESTORE_MOTIF = "Restauration de la sauvegarde du {quand} — ligne {ligne}"
PENDING, DONE, REFUSED = "à appliquer", "déjà appliquée", "refusée"
RESTORE_PENDING, RESTORE_DONE = rx.RESTORE_PENDING, rx.RESTORE_DONE
_CONTROLS = ("1", "2", "3", "4", "5")
_BACKUP_DOCUMENT_KEYS = ("chemin", "ligne", "update_time_lu", "etag_migration", "champs",
                         "avant", "apres")

RunRefused = rla.RunRefused
Impossible = rla.Impossible


# ── states ────────────────────────────────────────────────────────────────


def state_of(doc: dict) -> dict:
    """The business fields of *doc*, normalized: no category is ``None``, no
    purpose ``""`` (a row written before 2026-11-08 lacks the key), a split
    part an int — or ``None`` when unreadable, which no target ever equals."""
    out: dict = {"kind": doc.get("kind") or "",
                 "category": doc.get("category") or None,
                 "transfer_purpose": doc.get("transfer_purpose") or ""}
    for key in _SPLIT:
        value = doc.get(key)
        out[key] = (0 if value is None else
                    value if isinstance(value, int) and not isinstance(value, bool) else None)
    return out


def _shown(state: dict) -> str:
    """A state in the lawyer's words — nature, category, split, purpose."""
    parts = [al.KIND_LABELS.get(state["kind"], state["kind"] or "?")]
    if state["category"]:
        parts.append(al.ADMIN_CATEGORY_LABELS.get(state["category"], state["category"]))
    if any(state[k] for k in _SPLIT):
        parts.append("ventilation " + "+".join(str(state[k]) for k in _SPLIT))
    if state["transfer_purpose"]:
        parts.append(al.TRANSFER_PURPOSE_LABELS.get(state["transfer_purpose"],
                                                    state["transfer_purpose"]))
    return " · ".join(parts)


# ── the plan file ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LineSpec:
    ligne: int
    entry_id: str
    day: date                    # the stored UTC day the line expects
    amount: int                  # the stored amount the line expects, cents
    kind: str                    # the stored nature the line expects
    category: Optional[str]      # the stored category the line expects (None: none)
    purpose: str                 # the stored purpose the line expects ("": none)
    target_kind: str
    target_category: Optional[str]
    target_split: Optional[tuple]    # (net, tps, tvq), or None: the model's
    target_purpose: str
    motif: str

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


def _cents_cell(text: str, what: str) -> int:
    """A split part: an integer of cents, zero allowed."""
    if not text.isdigit() or not text.isascii():
        raise RunRefused(f"{what} : un entier de cents (0 ou plus) est attendu, lu « {text} »")
    return int(text)


def _split_cells(cells: dict, where: str) -> Optional[tuple]:
    given = [cells[k] for k in ("net_cible", "tps_cible", "tvq_cible")]
    if all(v == "" for v in given):
        return None
    if any(v == "" for v in given):
        raise RunRefused(f"{where} : net_cible, tps_cible et tvq_cible vont ensemble — "
                         f"les trois ou aucune")
    return tuple(_cents_cell(v, f"{where}, {name}")
                 for v, name in zip(given, ("net_cible", "tps_cible", "tvq_cible")))


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
            kind=_required(c["nature_actuelle"], f"{where}, nature_actuelle"),
            category=c["categorie_actuelle"] or None,
            purpose=c["objet_actuel"],
            target_kind=_required(c["nature_cible"], f"{where}, nature_cible"),
            target_category=c["categorie_cible"] or None,
            target_split=_split_cells(c, where),
            target_purpose=c["objet_cible"],
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
            records = list(csv.reader(io.StringIO(text, newline=""), strict=True))
        except csv.Error as exc:
            raise RunRefused(f"CSV illisible ({exc})") from None
        return _parse_records(records)
    except RunRefused as exc:
        raise RunRefused(f"plan mal formé : {exc}") from None


# ── what refuses an entry, and its target ─────────────────────────────────


def entry_refusals(doc: dict, store) -> list[tuple[str, str]]:
    """``[(code, French reason)]`` refusing ANY entry this script would
    write, the plan's own expectations apart."""
    out = []
    if doc.get("account_id") not in store.accounts:
        out.append(("compte_introuvable", f"compte {doc.get('account_id')} introuvable"))
    amount = doc.get("amount")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        out.append(("montant_illisible", "montant illisible : aucun plan ne peut l'attendre"))
    if rla._day(doc.get("date")) is None:
        out.append(("date_illisible", "date illisible : aucun plan ne peut l'attendre"))
    if (doc.get("reverses_id") or doc.get("reversed_by_id")
            or doc.get("id") in rc._named_by_a_correction(store)):
        out.append(("lien_contre_passation", "membre d'une contre-passation : sa correction "
                                             "garde la nature de l'original, et la paire ne "
                                             "se compenserait plus"))
    if doc.get("status") == "annulée":
        out.append(("annulée", "l'écriture est annulée"))
    if doc.get("invoice_id"):
        out.append(("liée_facture", "l'écriture est liée à une facture"))
    if doc.get("trust_transaction_id"):
        out.append(("liée_fideicommis", "l'écriture est liée à une écriture du fidéicommis"))
    length = rc._trail_length(doc)
    if length is None:
        out.append(("fil_illisible", "fil de révisions illisible"))
    elif length > MAX_TRAIL_BEFORE:
        out.append(("fil_plein", f"{length} entrées au fil de révisions — au plus "
                                 f"{MAX_TRAIL_BEFORE}, pour garder la place de cette écriture "
                                 f"et d'une restauration sans rien rogner"))
    for key in _SPLIT:
        if state_of(doc)[key] is None:
            out.append(("ventilation_illisible", f"{key} illisible"))
            break
    return out


def target_state(line: LineSpec, doc: dict) -> tuple[Optional[dict], list]:
    """``(target state, [])`` — the state the MODEL makes of the line's
    target on this entry — or ``(None, [(code, French reason)])``."""
    kind_before = doc.get("kind") or ""
    kind_after = line.target_kind
    if kind_before in _STRUCTURAL_KINDS:
        return None, [("nature_structurelle",
                       f"une écriture « {al.KIND_LABELS.get(kind_before, kind_before)} » ne "
                       f"change pas de nature ici : son propre chemin l'a inscrite")]
    if kind_after not in al._EDITABLE_KINDS:
        return None, [("nature_cible_invalide",
                       f"nature cible « {kind_after} » : une de "
                       f"{', '.join(al._EDITABLE_KINDS)}")]
    out = []
    if not al.kind_move_allowed(kind_before, kind_after):
        out.append(("passage_non_permis", f"le passage {kind_before} → {kind_after} n'est "
                                          f"pas permis (admin_ledger.KIND_MOVES)"))
    supplied: dict = {"category": line.target_category or "",
                      "transfer_purpose": line.target_purpose}
    if line.target_split is not None:
        supplied.update(zip(_SPLIT, line.target_split))
    reason = al._nature_refusal(kind_after, supplied) or al._purpose_refusal(kind_after, supplied)
    if reason:
        out.append((reason, al._ABORT_MESSAGES[reason]))
    if kind_after in al.NO_DOSSIER_KINDS and doc.get("dossier_id"):
        out.append(("dossier_interdit", al._ABORT_MESSAGES["dossier_interdit"]))
    if kind_after in al.CATEGORY_KINDS and line.target_split is None:
        out.append(("ventilation_requise", "une dépense ou un remboursement nomme sa "
                                           "ventilation : net_cible, tps_cible, tvq_cible"))
    if (kind_after not in al.CATEGORY_KINDS and kind_after not in al.NON_RESULT_KINDS
            and line.target_split is not None):
        out.append(("ventilation_hors_dépense", f"« {al.KIND_LABELS[kind_after]} » ne se "
                                                f"ventile pas"))
    if out:
        return None, out
    merged = {**doc, "kind": kind_after, "category": line.target_category,
              "transfer_purpose": line.target_purpose,
              **dict(zip(_SPLIT, line.target_split or (None, None, None)))}
    ventilation, reason = al._validate_business(merged)
    if reason:
        return None, [(reason, al._ABORT_MESSAGES.get(reason, reason))]
    # A purpose on another kind than a virement interne sortant was refused
    # above (``_purpose_refusal``): the one left is the target's.
    target = {"kind": kind_after,
              "category": merged.get("category") or None,
              "transfer_purpose": line.target_purpose,
              **{k: int(ventilation[k]) for k in _SPLIT}}
    return target, []


# ── the controls ──────────────────────────────────────────────────────────


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
            "etat": state_of(row),
            "fil": rla._digest(trail),
            "fil_moins_un": (rla._digest(trail[:-1])
                             if isinstance(trail, list) and trail else None),
            "longueur": len(trail) if isinstance(trail, list) else None,
        }
    return out


def compute_controls(store, scope: rc.Scope, payloads: Optional[dict] = None) -> dict:
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
        "3": rla._attempt(rc._control_periods, rows, scope.periods),
        "4": rla._attempt(rc._control_lock, rows, accounts, recs, scope),
        "5": others,
    }


def expected_effects(pairs: list, periods: tuple) -> rc.Expected:
    """What the written lines alone move: each entry's state ``(before,
    after)`` (control 2), and ``results_statement`` of the lines as stored,
    then as their targets say (never as a payload says: a payload that
    strays from its plan must show in controls 2 and 3), period by period
    and over the whole journal."""
    def delta(subset: list) -> dict:
        return rla._flat_results_delta(al.results_statement([b for b, _a in subset]),
                                       al.results_statement([a for _b, a in subset]))

    results = {}
    for first, last in periods:
        results[rc._period_key(first, last)] = delta(
            [(b, a) for b, a in pairs if first <= rx._day_of(b.get("date"), "ligne") <= last])
    results["journal"] = delta(pairs)
    moves = {b.get("id"): (state_of(b), state_of(a)) for b, a in pairs}
    return rc.Expected(moves=moves, results=results)


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
            gaps.append(f"contrôle 2 — écriture {entry_id} : un autre champ que la nature, "
                        f"ce qu'elle porte, le fil et les estampilles a bougé")
        if (b["etat"], a["etat"]) != (old, new):
            gaps.append(f"contrôle 2 — écriture {entry_id} : {_shown(b['etat'])} → "
                        f"{_shown(a['etat'])}, attendu {_shown(old)} → {_shown(new)}")
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
    if not isinstance(expected, rc.Expected):
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
    target: dict = field(default_factory=dict)
    table: list = field(default_factory=list)      # (document, champ, attendu, base, ok)
    reasons: list = field(default_factory=list)    # [(code, French reason)]

    def refuse(self, code: str, message: str) -> None:
        self.state = REFUSED
        self.reasons.append((code, message))


def _compare(line: LineSpec, doc: dict, verdict: Verdict) -> None:
    """The stored day, amount, nature, category and purpose against what the
    line states."""
    label = f"Ligne {line.ligne}"
    day = rla._day(doc.get("date"))
    amount = rla._as_int(doc.get("amount"))
    stored = state_of(doc)
    rows = (
        ("date", line.day.isoformat(), day.isoformat() if day else rx._v(doc.get("date")),
         day == line.day, "date"),
        ("amount", str(line.amount), rx._v(doc.get("amount")), amount == line.amount, "montant"),
        ("kind", line.kind, stored["kind"], stored["kind"] == line.kind, "nature"),
        ("category", rx._v(line.category), rx._v(stored["category"]),
         stored["category"] == line.category, "catégorie"),
        ("transfer_purpose", rx._v(line.purpose), rx._v(stored["transfer_purpose"]),
         stored["transfer_purpose"] == line.purpose, "objet"),
    )
    for name, want, have, ok, code in rows:
        verdict.table.append((label, name, want, have, ok))
        if not ok:
            verdict.refuse(f"état_changé:{code}", f"{name} inscrit {have}, le plan attend {want}")


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
    target, refusals = target_state(line, doc)
    for code, message in refusals:
        verdict.refuse(code, message)
    if target is None:
        return verdict
    verdict.target = target
    if rc._trail_of(doc, sha):
        if state_of(doc) == target:
            verdict.state = DONE
        else:
            verdict.refuse("état_mixte", f"état mixte : le fil de l'écriture porte ce plan, "
                                         f"mais elle est {_shown(state_of(doc))}")
        return verdict
    if state_of(doc) == target:
        verdict.refuse("déjà_la_cible", "l'écriture porte déjà la cible, sans le fil de ce plan")
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
    def scope(self) -> rc.Scope:
        return rc.Scope(tuple(dict.fromkeys(line.entry_id for line in self.lines)),
                        self.periods)

    @property
    def blocking(self) -> list:
        return self.gaps + self.impossible

    def count(self, state: str) -> int:
        return sum(1 for v in self.verdicts if v.state == state)


def _plan_writes(plan: Plan) -> None:
    """The payloads of the lines to apply — the fields that change, stamps
    under ``writing_via("script")``, one trail entry per entry."""
    now = datetime.now(UTC)
    with provenance.writing_via("script"):
        for verdict in plan.verdicts:
            if verdict.state != PENDING:
                continue
            doc, line = verdict.doc, verdict.line
            before = state_of(doc)
            business = {k: verdict.target[k] for k in BUSINESS_FIELDS
                        if verdict.target[k] != before[k]}
            changes = {k: [before[k], business[k]] for k in business}
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


def prepare(plan_path: Path) -> Plan:
    """Everything up to the write: read, judge, plan, simulate. Writes
    nothing. Raises :class:`RunRefused`."""
    plan_path = Path(plan_path)
    data, sha = rc._load_plan(plan_path)
    lines = parse_csv(data)
    store = rla.read_store()
    duplicates = rc._duplicates(lines)
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
    plan.periods = rc._years([v.doc for v in pending])
    scope = plan.scope
    plan.before = compute_controls(store, scope)
    plan.simulated = compute_controls(store, scope, plan.payloads)
    plan.expected = rla._attempt(expected_effects,
                                 [(v.doc, {**v.doc, **v.target}) for v in pending],
                                 scope.periods)
    plan.gaps, plan.impossible = compare_controls(plan.before, plan.simulated, plan.expected)
    plan.fingerprint = rc._fingerprint({"plan_sha256": sha, "projet": plan.project},
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
    path = directory / (f"nature-reclassee-{created.strftime('%Y%m%dT%H%M%S%fZ')}"
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
    scope: Optional[rc.Scope]
    state: str = RESTORE_PENDING
    store: Any = None
    rows: list = field(default_factory=list)     # (ligne, champ, actuel, restauré)
    refusals: list = field(default_factory=list)
    writes: list = field(default_factory=list)
    payloads: dict = field(default_factory=dict)
    before: dict = field(default_factory=dict)
    simulated: dict = field(default_factory=dict)
    expected: Any = None
    gaps: list = field(default_factory=list)
    impossible: list = field(default_factory=list)
    fingerprint: str = ""

    @property
    def blocking(self) -> list:
        return self.refusals + self.gaps + self.impossible


def _check_backup(body: dict) -> tuple[list, rc.Scope]:
    documents, context = body.get("documents"), body.get("contexte")
    if (not isinstance(documents, list) or not documents or not isinstance(context, dict)
            or body.get("contenu_sha256") != rla._digest(documents)):
        raise RunRefused("sauvegarde altérée : son contenu ne concorde pas avec son empreinte")
    seen: set = set()
    for document in documents:
        if (not isinstance(document, dict) or set(document) != set(_BACKUP_DOCUMENT_KEYS)
                or not isinstance(document["champs"], list) or not document["champs"]
                or not set(document["champs"]) <= set(BUSINESS_FIELDS)
                or document["champs"] != sorted(set(document["champs"]))
                or not isinstance(document["avant"], dict)
                or not isinstance(document["apres"], dict)
                or set(document["apres"]) != set(document["champs"])
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
    scope = rc.Scope(tuple(d["chemin"].split("/", 1)[1] for d in documents), periods)
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
    store = rla.read_store()
    plan.store = store
    states, current = [], {}
    for document in sorted(documents, key=lambda d: d["ligne"]):
        ligne = document["ligne"]
        entry_id = document["chemin"].split("/", 1)[1]
        champs = document["champs"]
        snap = store.entries.get(entry_id)
        if snap is None:
            plan.refusals.append(f"ligne {ligne} : écriture introuvable")
            states.append(None)
            continue
        doc = {**store.rows[entry_id], "id": entry_id}
        current[entry_id] = (snap, doc)
        before = state_of(rla._untyped(document["avant"]))
        after = state_of({**rla._untyped(document["avant"]),
                          **rla._untyped(document["apres"])})
        now_state = state_of(doc)
        length = rc._trail_length(doc)
        if doc.get("etag") == document["etag_migration"]:
            if any(now_state[k] != after[k] for k in champs):
                plan.refusals.append(f"ligne {ligne} : etag de la migration, mais l'écriture "
                                     f"n'est plus {_shown(after)}")
            elif length is None or length + 1 > al._REVISIONS_CAP:
                plan.refusals.append(f"ligne {ligne} : le fil de révisions est illisible ou "
                                     f"plein — rien ne se rogne")
            states.append(RESTORE_PENDING)
        elif all(now_state[k] == before[k] for k in champs) and rc._trail_of(doc, sha):
            states.append(RESTORE_DONE)
        else:
            plan.refusals.append(f"ligne {ligne} : l'écriture ne porte pas l'etag de la "
                                 f"migration (base {doc.get('etag')}, migration "
                                 f"{document['etag_migration']}) : la migration n'y est pas, ou "
                                 f"une écriture l'a suivie — rien ne se restaure par-dessus")
            states.append(None)
        for k in champs:
            plan.rows.append((ligne, k, rx._v(now_state[k]), rx._v(before[k])))
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
                before = state_of(rla._untyped(document["avant"]))
                now_state = state_of(doc)
                business = {k: before[k] for k in document["champs"]}
                changes = {k: [now_state[k], before[k]] for k in document["champs"]}
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
    plan.fingerprint = rc._fingerprint({"sauvegarde_sha256": sha, "projet": plan.project},
                                       plan.writes, plan.payloads, [])
    return plan


# ── after the write ───────────────────────────────────────────────────────


def after_real(scope: rc.Scope) -> dict:
    """The controls on a fresh read, once written."""
    try:
        store = rla.read_store()
    except RunRefused as exc:
        reason = Impossible(str(exc))
        return {key: reason for key in _CONTROLS}
    return compute_controls(store, scope)


def _post_write(scope: rc.Scope, before: dict, simulated: dict, expected) -> tuple:
    real = after_real(scope)
    gaps, impossible = compare_controls(before, real, expected)
    return real, gaps + impossible, rla._differences(simulated, real)


# ── the report ────────────────────────────────────────────────────────────


def _render_controls(scope: rc.Scope, store, before: dict, simulated: dict,
                     real: Optional[dict]) -> list:
    columns = ("Contrôle", "Avant", "Après (simulé)") + (("Après (réel)",) if real else ())

    def row(label, key, fn):
        cells = [label, rx._shown(rx._get(before, key), fn), rx._shown(rx._get(simulated, key), fn)]
        if real:
            cells.append(rx._shown(rx._get(real, key), fn))
        return tuple(cells)

    def ledger(c):
        return (f"{len(c['soldes'])} compte(s) ; {len(c['soldes_courants'])} soldes courants ; "
                f"{len(c['sequences'])} séquences")

    def lines(c):
        return " ; ".join("absente" if v is None
                          else f"{_shown(v['etat'])} (fil {v['longueur']})"
                          for v in c.values())

    def period(statement):
        return (f"revenus {rla._money(statement['total_revenue'])} ; dépenses (brut) "
                f"{rla._money(statement['total_expenses'])} ; TPS "
                f"{rla._money(statement['gst'])} ; TVQ {rla._money(statement['qst'])}")

    def lock(c):
        reasons = sorted({str(r or "aucun") for r in c["lock_reasons"].values()})
        return (f"{len(c['conciliations'])} compte(s) concilié(s) ; lock_reason "
                f"{', '.join(reasons) or '—'}")

    rows = [row("1. Soldes des comptes, soldes courants, séquences", "1", ledger),
            row("2. Les écritures du plan : état (fil)", "2", lines)]
    for first, last in scope.periods:
        key = rc._period_key(first, last)
        rows.append(row(f"3. Du {first} au {last}", "3", lambda c, k=key: period(c[k])))
    rows += [row("3. Journal entier", "3", lambda c: period(c["journal"])),
             row("4. Verrou et conciliations", "4", lock),
             row("5. Autres documents lus", "5", lambda c: f"{len(c)} document(s)")]
    return rx._table(columns, rows)


def render_report(plan: Plan, *, mode: str, approved: Optional[str] = None,
                  refusal: Optional[list] = None, outcome: Optional[str] = None,
                  backup: Optional[tuple] = None, real: Optional[dict] = None,
                  drift: Optional[list] = None, real_gaps: Optional[list] = None) -> str:
    store = plan.store
    out = [
        "# Reclassement de la nature d'écritures d'administration",
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
            _shown(state_of(doc)) if doc else "—",
            _shown(v.target) if v.target else "—", v.state, v.line.trail_motif,
            "; ".join(f"`{code}` — {message}" for code, message in v.reasons) or "—"))
    out += rx._table(("Ligne", "Écriture", "Compte", "Jour", "Montant", "Bénéficiaire",
                      "Inscrite", "Cible", "État", "Motif du fil", "Refus"), rows)

    out += ["", "## 2. Ce que le plan attend, et ce que porte la base", ""]
    table = [r for v in plan.verdicts for r in v.table]
    out += rx._table(("Document", "Champ", "Attendu (plan)", "Base", ""),
                     [(d, f, w, h, "conforme" if ok else "**ÉCART**")
                      for d, f, w, h, ok in table]) if table else ["(sans objet)"]

    out += ["", "## 3. Changements prévus", ""]
    if plan.writes:
        for write in sorted(plan.writes, key=lambda w: w.ligne):
            payload = plan.payloads[write.path]
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
            ("- Seuls la nature (`kind`), la catégorie, la ventilation (`net_amount`, "
             "`gst_amount`, `qst_amount`) et l'objet du virement (`transfer_purpose`) qui "
             "changent sont écrits — l'état que le modèle fait de la cible "
             "(`admin_ledger._validate_business`) —, avec les estampilles et une entrée du "
             "fil `revisions` ; jamais le montant, le sens, la date, le statut, le dossier, "
             "un lien ni la contrepartie."),
            ("- Provenance : `script` — `updated_via` et `via` du fil, sous "
             "`provenance.writing_via`."),
            "", "## 5. Verrou et conciliations", ""]
    lock_before = rx._get(plan.before, "4")
    for line in plan.lines:
        before = rx._shown(lock_before,
                           lambda c, i=line.entry_id: c["lock_reasons"].get(i) or "aucun")
        out.append(f"- Ligne {line.ligne} : `lock_reason` (dérivé à la lecture, jamais stocké) "
                   f"{before} ; conciliations qui la citent : "
                   + (", ".join(plan.citing.get(line.entry_id) or []) or "aucune") + ".")
    out += [("- Une écriture verrouillée est acceptée : un changement de nature dans un même "
             "sens ne change aucun solde (le montant et le sens restent) ni aucune "
             "conciliation — seule la lecture qu'en font les résultats, la TPS/TVQ et les "
             "états change."),
            "- Champs des conciliations hors de la forme du modèle : "
            + (", ".join(f"`{k}`" for k in plan.reconciliation_keys) or "aucun") + "."]

    out += ["", "## 6. Contrôles", ""]
    out += _render_controls(plan.scope, store, plan.before, plan.simulated, real)
    out += ["", "Écarts : " + ("aucun" if not (plan.gaps or plan.impossible)
                               else "; ".join(plan.gaps + plan.impossible))]
    out += ["", "## 7. Suite", ""]
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
        "# Restauration d'un reclassement de nature",
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
        out += _render_controls(plan.scope, plan.store, plan.before, plan.simulated, real)
        out += ["", "Écarts : " + ("aucun" if not (plan.gaps or plan.impossible)
                                   else "; ".join(plan.gaps + plan.impossible))]
    return "\n".join(out) + "\n"


# ── the command line ──────────────────────────────────────────────────────


def _summary(plan: Plan, mode: str) -> list:
    return [
        f"Reclassement de nature — {mode}",
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
        return rla._finish(report, f"# Reclassement de nature\n\n- Date : "
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
    parser = rla._Parser(prog="python -m scripts.reclassify_admin_nature",
                         description="Reclassement de la nature d'écritures d'administration "
                                     "(hors modèle, à blanc par défaut).")
    parser.add_argument("plan", nargs="?", help="le plan CSV approuvé (hors du dépôt public)")
    parser.add_argument("--rapport", metavar="RAPPORT.md",
                        help="le rapport à écrire (hors du dépôt ; nouveau fichier)")
    parser.add_argument("--sauvegarde", metavar="DOSSIER",
                        help="le dossier où écrire la sauvegarde avant le lot (hors du dépôt)")
    parser.add_argument("--appliquer", metavar="EMPREINTE",
                        help="écrire — avec l'empreinte de la passe à blanc approuvée")
    parser.add_argument("--restaurer", metavar="SAUVEGARDE.json",
                        help="restaurer les champs sauvegardés (à blanc sans --appliquer)")
    args = parser.parse_args(argv if argv is not None else [])
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
