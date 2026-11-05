"""Rectify, OUTSIDE the models, the TPS/TVQ split (« ventilation ») of
locked administration expenses — a one-shot migration bound to its plan.

What it does
------------
An expense's ventilation says how much of its amount is the net cost and how
much is TPS and TVQ the firm claims back. On a LOCKED entry — compensée,
behind a completed reconciliation — a wrong split cannot be corrected in the
application: ``admin_ledger.update_transaction`` refuses every locked entry
(``_entry_lock_reason``), and the house correction, a reversal and a new
entry, was declined by the lawyer for this case (the verification of
2026-10-22: a provincial tax on insurance premiums booked as TVQ, which opens
no input tax refund). This script writes the split the lawyer approved, line
by line, from a PLAN kept outside the public repository.

Why it writes outside the models
--------------------------------
CLAUDE.md, « One door per rule »: a declared exception, like
``scripts/reclassify_admin_ledger`` and
``scripts/reclassify_as_invoice_receipt``, whose pure helpers it imports,
leaving them and their tests untouched; it closes with its plan. On each
entry it writes ``net_amount``, ``gst_amount`` and ``qst_amount`` — those
whose value changes, and only to a split ``admin_ledger.validate_ventilation``
returns unchanged for the entry's own amount (``record_admin_entry``'s
« sans_taxe » is ``net = amount`` with no tax: the model stores no mode, only
the three figures) — plus the house stamps (``updated_at``, a new ``etag``,
``updated_via = "script"``, under ``provenance.writing_via``) and ONE
``revisions`` trail entry ``{at, via, motif, changes, updated_at_before,
source_sha256}``. Never the amount, the sign, a date, the status, the kind,
the category or a link: the bank movement is a fact, only its split was
misjudged. No account is written — no money moves — and no reconciliation:
they keep gross figures and the ids they cleared, never a split. The model
keeps no TPS/TVQ filing period. The registers are not DAV-exposed: no CTag.

The plan
--------
A JSON object (UTF-8), outside the repository::

    {"motif": "… ligne {ligne}",          # the trail's motif; {ligne} → the line
     "note": "…",                         # optional, for the reader
     "compte": {"id": "…"},
     "lignes": [{"ligne": 1, "ecriture": "…",
                 "attendu": {…},          # LINE_EXPECTED
                 "cible": {"net_amount": 0, "gst_amount": 0, "qst_amount": 0}}],
     "intouchables": [{"chemin": "collection/id", "etag": "…"}],
     "periodes": [{"du": "AAAA-MM-JJ", "au": "AAAA-MM-JJ"}]}

``attendu`` states the stored values the line expects (a date-only field as
its day, ``null`` for an absent or null value). A line whose entry differs
in any of them is REFUSED and reported, never forced — the other lines still
apply; the plan is never bent to fit the base. An untouchable whose etag
moved refuses the whole run.

Judging a line — every read is strict
-------------------------------------
A line is refused when its entry is not on the plan's account; is not a
``dépense`` (a recette carries no split; the four natures outside the
results carry their canonical one; a correction or a card payment is
structural); is a member of a reversal (its correction copies the split:
the pair would no longer net to zero); is annulée, linked to an invoice or a
trust entry, or carries more than ``_REVISIONS_CAP - 2`` trail entries; or
when its target is not a split the model accepts as is, or is already the
stored one. A line whose entry holds its target with this plan's trail entry
(same ``source_sha256``) is « déjà appliquée »: the migration is idempotent.
The trail entry without the target is refused (« état mixte »).

The controls
------------
On complete reads — every administration entry, account and reconciliation,
every invoice, the trust register — BEFORE, AFTER SIMULATED (the documents
read, merged with the exact payloads) and, once written, AFTER (re-read):

1. each account's stored ``ledger_balance`` and Σ ``admin_delta``, every
   entry's running balance and sequence — unchanged;
2. each line's amount, sign, days, status, sequence, account, kind and
   category — unchanged; net + TPS + TVQ = amount is reported (control 3
   implies it: each split is held to its target, a split of the unchanged
   amount the model accepts as is);
3. ``admin_ledger.results_statement`` over each of the plan's periods and
   over the whole journal — moved by exactly what the same function says of
   the applied lines alone, as stored and as the plan targets them: the
   expenses' net, TPS and TVQ, never a gross amount;
4. each account's lock floor and last reconciliation, and the re-proof of
   every completed reconciliation — unchanged; each line's ``lock_reason``
   (derived at read, never stored) is reported;
5. every other document read — unchanged (a digest per document).

A control that cannot be computed, or that moves otherwise, refuses
``--appliquer``.

Fingerprint, backup, restoration
--------------------------------
As in ``scripts/reclassify_as_invoice_receipt``: the fingerprint covers the
plan's digest, ``db.project``, every planned write (path, the
``update_time`` read, the fields written) and every refused or applied
line; the typed JSON backup of each document to write — whole — is written,
fsynced, read back and verified before ONE batch whose every document is
guarded by the ``update_time`` it was read at; ``--restaurer`` (dry by
default, its own fingerprint) puts the saved splits back, all or nothing,
with fresh stamps and a trail entry « Restauration de la sauvegarde du … —
ligne n », and refuses everything when an entry's etag is no longer the
migration's.

Running it — from ``athena/``, with ADC and the environment of « Active
gabarits » in DEPLOYMENT.md: even the dry run reads PRODUCTION. A plan,
report, backup or backup directory inside the repository is refused before
anything is created; the report must be a new file.

    python -m scripts.rectify_expense_ventilation PLAN.json --rapport R.md
    python -m scripts.rectify_expense_ventilation PLAN.json --rapport R.md \\
        --sauvegarde DOSSIER --appliquer EMPREINTE
    python -m scripts.rectify_expense_ventilation --restaurer SAUVEGARDE.json \\
        --rapport R.md [--appliquer EMPREINTE]

Afterwards, run both integrity scripts and compare their findings BY TEXT.
"""

from __future__ import annotations

import hashlib
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
SPLIT = ("net_amount", "gst_amount", "qst_amount")
#: The stored values each line states (« attendu »), compared to the base.
LINE_EXPECTED = ("sequence", "date", "cleared_date", "reference", "supplier_invoice_ref",
                 "amount", "net_amount", "gst_amount", "qst_amount", "etag")
#: A line's facts no write may move (control 2).
_LINE_FACTS = ("amount", "direction", "date", "cleared_date", "status", "sequence",
               "account_id", "kind", "category")
LINE_MARK = "{ligne}"
#: A written entry keeps room for this write AND one restoration.
MAX_TRAIL_BEFORE = al._REVISIONS_CAP - 2
FINGERPRINT_HEX = 16
BACKUP_FORMAT = "athena.ventilation-rectifiee/1"
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
    expected: dict      # name → value in the base's terms
    target: dict        # the three split figures, cents

    @property
    def path(self) -> str:
        return f"{TX}/{self.entry_id}"


@dataclass
class Spec:
    """The plan, its form checked — nothing read yet."""

    motif: str
    note: str
    account_id: str
    lines: list
    untouchables: list   # [(path, etag)]
    periods: tuple       # ((first day, last day), …)

    def motif_of(self, ligne: int) -> str:
        return self.motif.replace(LINE_MARK, str(ligne))


def _split_value(value, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RunRefused(f"plan mal formé : « {where} » doit être un montant entier en cents, "
                         f"positif ou nul")
    return value


def _period(item, where: str) -> tuple:
    if not isinstance(item, dict):
        raise RunRefused(f"plan mal formé : « {where} » doit être un objet")
    rx._exact_keys(item, ("du", "au"), where)
    first = rx._plan_value("date", item["du"], f"{where}.du")
    last = rx._plan_value("date", item["au"], f"{where}.au")
    if first is None or last is None or first > last:
        raise RunRefused(f"plan mal formé : « {where} » doit aller d'un jour à un jour postérieur")
    return first.date(), last.date()


def parse_plan(data: dict) -> Spec:
    """The plan's form, judged before any read — any defect refuses."""
    rx._exact_keys({k: v for k, v in data.items() if k != "note"},
                   ("motif", "compte", "lignes", "intouchables", "periodes"), "plan")
    motif = data.get("motif")
    if (not isinstance(motif, str) or not motif.strip() or motif != motif.strip()
            or motif.count(LINE_MARK) != 1):
        raise RunRefused(f"plan mal formé : « motif » doit être un texte non vide, sans espace "
                         f"en bordure, qui porte une fois {LINE_MARK}")
    note = data.get("note", "")
    if not isinstance(note, str):
        raise RunRefused("plan mal formé : « note » doit être un texte")
    compte = rx._exact_keys(rx._object(data, "compte", ""), ("id",), "compte")

    raw_lines = data.get("lignes")
    if not isinstance(raw_lines, list) or not raw_lines:
        raise RunRefused("plan mal formé : « lignes » doit être une liste non vide")
    lines, numbers, entries = [], set(), set()
    for i, item in enumerate(raw_lines):
        where = f"lignes[{i}]"
        if not isinstance(item, dict):
            raise RunRefused(f"plan mal formé : « {where} » doit être un objet")
        rx._exact_keys(item, ("ligne", "ecriture", "attendu", "cible"), where)
        ligne, entry_id = item["ligne"], item["ecriture"]
        if not isinstance(ligne, int) or isinstance(ligne, bool) or ligne <= 0 or ligne in numbers:
            raise RunRefused(f"plan mal formé : « {where}.ligne » doit être un entier positif, "
                             f"une seule fois")
        if not _addressable(entry_id) or entry_id in entries:
            raise RunRefused(f"plan mal formé : « {where}.ecriture » doit être un identifiant, "
                             f"une seule fois")
        numbers.add(ligne)
        entries.add(entry_id)
        expected = rx._exact_keys(rx._object(item, "attendu", f"{where}."), LINE_EXPECTED,
                                  f"{where}.attendu")
        target = rx._exact_keys(rx._object(item, "cible", f"{where}."), SPLIT, f"{where}.cible")
        lines.append(LineSpec(
            ligne=ligne, entry_id=entry_id,
            expected={n: rx._plan_value(n, v, f"{where}.attendu.{n}")
                      for n, v in expected.items()},
            target={n: _split_value(v, f"{where}.cible.{n}") for n, v in target.items()}))

    raw_periods = data.get("periodes")
    if not isinstance(raw_periods, list) or not raw_periods:
        raise RunRefused("plan mal formé : « periodes » doit être une liste non vide")
    return Spec(
        motif=motif, note=note, account_id=rx._doc_id(compte, "compte"),
        lines=sorted(lines, key=lambda line: line.ligne),
        untouchables=rx._parse_untouchables(data.get("intouchables")),
        periods=tuple(_period(item, f"periodes[{i}]") for i, item in enumerate(raw_periods)),
    )


# ── the controls ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Scope:
    """What the controls are about."""

    account_id: str
    entry_ids: tuple
    periods: tuple


def _period_key(first: date, last: date) -> str:
    return f"{first.isoformat()}/{last.isoformat()}"


def _control_lines(rows: list, entry_ids: tuple) -> dict:
    by_id = {r.get("id"): r for r in rows}
    out = {}
    for entry_id in entry_ids:
        row = by_id.get(entry_id)
        if row is None:
            raise ControlImpossible(f"écriture {entry_id} absente de la lecture")
        split = [int(row.get(k) or 0) for k in SPLIT]
        out[entry_id] = {
            "faits": {k: rla._canon(rx._aware(row.get(k))) for k in _LINE_FACTS},
            "ventilation": split,
            "somme_juste": sum(split) == int(row.get("amount") or 0),
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
    floor = rx._floor_of(reconciliations, scope.account_id)
    by_id = {r.get("id"): r for r in rows}
    return {"conciliations": reconciliations,
            "lock_reasons": {i: al._entry_lock_reason(by_id[i], floor)
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


def expected_deltas(pairs: list, periods: tuple) -> dict:
    """What the written lines alone move, period by period and over the
    whole journal — ``results_statement`` of each line as stored, then as
    its target says (never as a payload says: a payload that strays from
    its plan must show in control 3)."""
    def delta(subset: list) -> dict:
        return rla._flat_results_delta(al.results_statement([b for b, _a in subset]),
                                       al.results_statement([a for _b, a in subset]))

    out = {}
    for first, last in periods:
        out[_period_key(first, last)] = delta(
            [(b, a) for b, a in pairs if first <= rx._day_of(b.get("date"), "ligne") <= last])
    out["journal"] = delta(pairs)
    return out


def compare_controls(before: dict, after: dict, expected: dict) -> tuple[list, list]:
    """``(gaps, impossible)``: what moved otherwise than *expected*, and what
    could not be computed."""
    gaps: list[str] = []
    impossible: list[str] = []
    for key in _CONTROLS:
        for value in (before.get(key), after.get(key)):
            if isinstance(value, Impossible):
                impossible.append(f"contrôle {key} impossible à calculer ({value.reason})")
                break
    blocked = {item.split(" ")[1] for item in impossible}
    for key in ("1", "5"):
        if key not in blocked and before[key] != after[key]:
            gaps.extend(f"contrôle {key} — {d}" for d in rla._differences(before[key], after[key]))
    if "2" not in blocked:
        # net + TPS + TVQ = amount is reported, never gated on its own: the
        # amount is a fact checked here, and control 3 holds each split to
        # its target — a split of that amount the model accepts as is.
        for entry_id, b in before["2"].items():
            a = after["2"].get(entry_id)
            if a is None or a["faits"] != b["faits"]:
                gaps.append(f"contrôle 2 — écriture {entry_id} : "
                            f"{rla._differences(b['faits'], (a or {}).get('faits'))}")
    if "3" not in blocked:
        for key, want in expected.items():
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
    table: list = field(default_factory=list)      # (document, field, attendu, base, ok)
    reasons: list = field(default_factory=list)

    def refuse(self, reason: str) -> None:
        self.state = REFUSED
        self.reasons.append(reason)


def _line_refusals(doc: dict, line: LineSpec, spec: Spec) -> list:
    out = []
    kind = doc.get("kind")
    if doc.get("account_id") != spec.account_id:
        out.append("l'écriture n'est pas au compte du plan")
    if kind not in al.EXPENSE_KINDS:
        out.append(f"nature « {kind} » : seule une dépense porte une ventilation qui se "
                   f"rectifie ici")
    if doc.get("direction") != "déboursé":
        out.append("l'écriture n'est pas un déboursé")
    if doc.get("reverses_id") or doc.get("reversed_by_id"):
        out.append("l'écriture est membre d'une contre-passation : sa correction copie la "
                   "ventilation, et la paire ne se compenserait plus")
    if doc.get("status") == "annulée":
        out.append("l'écriture est annulée")
    if doc.get("invoice_id") or doc.get("trust_transaction_id"):
        out.append("l'écriture est liée à une facture ou au fidéicommis")
    if len(doc.get("revisions") or []) > MAX_TRAIL_BEFORE:
        out.append(f"le fil de l'écriture compte plus de {MAX_TRAIL_BEFORE} entrées : plus de "
                   f"place pour cette écriture et une restauration")
    amount = doc.get("amount")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        out.append("montant illisible")
        return out
    accepted, _reason = al.validate_ventilation("déboursé", amount,
                                                *(line.target[k] for k in SPLIT))
    if accepted != line.target:
        shown = " + ".join(rx._v(line.target[k]) for k in SPLIT)
        out.append(f"cible {shown} : le modèle ne l'accepte pas telle quelle pour un montant "
                   f"de {amount}")
    return out


def judge_line(line: LineSpec, store, spec: Spec, sha: str) -> Verdict:
    verdict = Verdict(line)
    snap = store.entries.get(line.entry_id)
    if snap is None:
        verdict.refuse("écriture introuvable")
        return verdict
    doc = {**store.rows[line.entry_id], "id": line.entry_id}
    verdict.snap, verdict.doc = snap, doc
    at_target = all(doc.get(k) == line.target[k] for k in SPLIT)
    if rx._trail_of(doc, sha):
        if at_target:
            verdict.state = DONE
        else:
            verdict.refuse("état mixte : le fil de l'écriture porte ce plan, mais sa "
                           "ventilation n'est pas la cible")
        return verdict
    differences: list = []
    rx._compare(f"Ligne {line.ligne}", doc, line.expected, verdict.table, differences)
    for reason in differences + _line_refusals(doc, line, spec):
        verdict.refuse(reason)
    if at_target and verdict.state == PENDING:
        verdict.refuse("la ventilation inscrite est déjà la cible, sans le fil de ce plan")
    return verdict


@dataclass
class Plan:
    path: Path
    sha: str
    project: str
    spec: Spec
    store: Any
    verdicts: list
    table: list = field(default_factory=list)          # the untouchables
    differences: list = field(default_factory=list)    # the untouchables
    writes: list = field(default_factory=list)
    payloads: dict = field(default_factory=dict)
    citing: dict = field(default_factory=dict)         # entry id → reconciliations citing it
    reconciliation_keys: list = field(default_factory=list)
    before: dict = field(default_factory=dict)
    simulated: dict = field(default_factory=dict)
    expected: dict = field(default_factory=dict)
    gaps: list = field(default_factory=list)
    impossible: list = field(default_factory=list)
    fingerprint: str = ""

    @property
    def scope(self) -> Scope:
        return Scope(self.spec.account_id, tuple(line.entry_id for line in self.spec.lines),
                     self.spec.periods)

    @property
    def blocking(self) -> list:
        return self.differences + self.gaps + self.impossible

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
            business = {k: line.target[k] for k in SPLIT if doc.get(k) != line.target[k]}
            changes = {k: [doc.get(k), v] for k, v in business.items()}
            trail = {
                "at": now,
                "via": provenance.current_via(),
                "motif": plan.spec.motif_of(line.ligne),
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
    states = [{"ligne": v.line.ligne, "etat": v.state, "motifs": v.reasons}
              for v in verdicts if v.state != PENDING]
    return rla._digest({**head, "ecritures": rows, "lignes": states})[:FINGERPRINT_HEX]


def prepare(plan_path: Path) -> Plan:
    data, sha = rx._load_json(plan_path, "plan")
    spec = parse_plan(data)
    store = rla.read_store()
    if spec.account_id not in store.accounts:
        raise RunRefused(f"compte introuvable : {spec.account_id}")
    plan = Plan(path=plan_path, sha=sha, project=db.project, spec=spec, store=store,
                verdicts=[judge_line(line, store, spec, sha) for line in spec.lines])
    rx._compare_untouchables(spec.untouchables, plan.table, plan.differences)
    keys: set = set()
    for line in spec.lines:
        citing, extra = rx._reconciliation_findings(store.recs, spec.account_id, line.entry_id)
        plan.citing[line.entry_id] = citing
        keys |= set(extra)
    plan.reconciliation_keys = sorted(keys)
    _plan_writes(plan)
    scope = plan.scope
    plan.before = compute_controls(store, scope)
    plan.simulated = compute_controls(store, scope, plan.payloads)
    plan.expected = expected_deltas(
        [(v.doc, {**v.doc, **v.line.target}) for v in plan.verdicts if v.state == PENDING],
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
        "contexte": {"compte": plan.spec.account_id,
                     "periodes": [[f.isoformat(), l.isoformat()] for f, l in plan.spec.periods]},
        "documents": documents, "contenu_sha256": rla._digest(documents),
    }
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=1, allow_nan=False)
    path = directory / (f"ventilation-rectifiee-{created.strftime('%Y%m%dT%H%M%S%fZ')}"
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
    before: dict = field(default_factory=dict)
    simulated: dict = field(default_factory=dict)
    expected: dict = field(default_factory=dict)
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
    for document in documents:
        if (not isinstance(document, dict) or set(document) != set(_BACKUP_DOCUMENT_KEYS)
                or not isinstance(document["champs"], list)
                or not set(document["champs"]) <= set(SPLIT)
                or not isinstance(document["avant"], dict)
                or not isinstance(document["apres"], dict)
                or not isinstance(document["chemin"], str)
                or not document["chemin"].startswith(f"{TX}/")):
            raise RunRefused("sauvegarde altérée : un document n'a pas la forme attendue")
    try:
        periods = tuple((date.fromisoformat(f), date.fromisoformat(l))
                        for f, l in context["periodes"])
        scope = Scope(context["compte"],
                      tuple(d["chemin"].split("/", 1)[1] for d in documents), periods)
    except (KeyError, TypeError, ValueError):
        raise RunRefused("sauvegarde altérée : contexte illisible") from None
    return documents, scope


def prepare_restore(backup_path: Path) -> RestorePlan:
    body, sha = rx._load_json(backup_path, "sauvegarde")
    if body.get("format") != BACKUP_FORMAT:
        raise RunRefused(f"sauvegarde d'un autre format ({body.get('format')!r}) — attendu "
                         f"{BACKUP_FORMAT}")
    if body.get("projet") != db.project:
        raise RunRefused(f"sauvegarde du projet « {body.get('projet')} », base « {db.project} »")
    documents, scope = _check_backup(body)
    plan = RestorePlan(path=backup_path, sha=sha, project=db.project,
                       created=str(body.get("cree_le")),
                       migration_fingerprint=str(body.get("empreinte")), scope=scope)
    store = rla.read_store()
    plan.store = store
    states, current = [], {}
    for document in sorted(documents, key=lambda d: d["ligne"]):
        entry_id = document["chemin"].split("/", 1)[1]
        snap = store.entries.get(entry_id)
        if snap is None:
            plan.refusals.append(f"ligne {document['ligne']} : écriture introuvable")
            states.append(None)
            continue
        doc = {**store.rows[entry_id], "id": entry_id}
        current[entry_id] = (snap, doc)
        before = rla._untyped(document["avant"])
        after = rla._untyped(document["apres"])
        fields = list(document["champs"])
        if doc.get("etag") == document["etag_migration"]:
            astray = [f for f in fields if doc.get(f) != after.get(f)]
            if astray:
                plan.refusals.append(f"ligne {document['ligne']} : etag de la migration, mais "
                                     f"{', '.join(astray)} n'ont pas sa valeur")
            states.append(RESTORE_PENDING)
        elif all(doc.get(f) == before.get(f) for f in fields) and rx._trail_of(doc, sha):
            states.append(RESTORE_DONE)
        else:
            plan.refusals.append(f"ligne {document['ligne']} : l'écriture ne porte pas l'etag de "
                                 f"la migration (base {doc.get('etag')}, migration "
                                 f"{document['etag_migration']}) : la migration n'y est pas, ou "
                                 f"une écriture l'a suivie — rien ne se restaure par-dessus")
            states.append(None)
        for f in fields:
            plan.rows.append((document["ligne"], f, rx._v(doc.get(f)), rx._v(before.get(f))))
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
                business = {f: before.get(f) for f in document["champs"]}
                trail = {
                    "at": now,
                    "via": provenance.current_via(),
                    "motif": RESTORE_MOTIF.format(quand=rla._when(plan.created),
                                                  ligne=document["ligne"]),
                    "changes": {f: [doc.get(f), v] for f, v in business.items()},
                    "updated_at_before": doc.get("updated_at"),
                    "source_sha256": sha,
                }
                plan.payloads[document["chemin"]] = {
                    **business, "revisions": list(doc.get("revisions") or []) + [trail],
                    **provenance.update_fields(now)}
                plan.writes.append(rla.Write(document["chemin"], TX, entry_id, document["ligne"],
                                             "écriture", snap, doc, business,
                                             {f: [doc.get(f), v] for f, v in business.items()}))
                pairs.append((doc, {**doc, **business}))
    plan.before = compute_controls(store, scope)
    plan.simulated = compute_controls(store, scope, plan.payloads)
    if plan.writes:
        plan.expected = expected_deltas(pairs, scope.periods)
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


def _post_write(scope: Scope, before: dict, simulated: dict, expected: dict) -> tuple:
    real = after_real(scope)
    gaps, impossible = compare_controls(before, real, expected)
    return real, gaps + impossible, rla._differences(simulated, real)


# ── the report ────────────────────────────────────────────────────────────


def _split_text(values) -> str:
    return " + ".join(rla._money(v) for v in values)


def _period_text(statement: dict) -> str:
    return (f"dépenses (brut) {rla._money(statement['total_expenses'])} ; dépenses nettes "
            f"{rla._money(sum(statement['expenses_net'].values()))} ; TPS "
            f"{rla._money(statement['gst'])} ; TVQ {rla._money(statement['qst'])}")


def _render_controls(scope: Scope, accounts: dict, before: dict, simulated: dict,
                     real: Optional[dict]) -> list:
    columns = ("Contrôle", "Avant", "Après (simulé)") + (("Après (réel)",) if real else ())

    def row(label, key, fn):
        cells = [label, rx._shown(rx._get(before, key), fn), rx._shown(rx._get(simulated, key), fn)]
        if real:
            cells.append(rx._shown(rx._get(real, key), fn))
        return tuple(cells)

    account = accounts.get(scope.account_id) or {}
    label = al.BALANCE_LABELS.get(account.get("account_type", ""), "Solde")

    def ledger(c):
        mine = c["soldes"].get(scope.account_id) or {}
        shown = al.display_balance(account.get("account_type", ""),
                                   int(mine.get("ledger_balance") or 0))
        return (f"{label} {rla._money(shown)} (Σ {rla._money(mine.get('somme'))} au registre) ; "
                f"{len(c['soldes_courants'])} soldes courants ; {len(c['sequences'])} séquences")

    def lines(c):
        return " ; ".join(f"{_split_text(v['ventilation'])}"
                          f"{'' if v['somme_juste'] else ' (somme fausse)'}" for v in c.values())

    def lock(c):
        mine = c["conciliations"].get(scope.account_id) or {}
        proofs = mine.get("preuves") or {}
        bad = sum(1 for p in proofs.values() if p.get("ecart"))
        last = mine.get("derniere") or {}
        reasons = sorted(set(map(str, c["lock_reasons"].values())))
        return (f"plancher {mine.get('plancher')} ; dernière {last.get('fin')} "
                f"({last.get('statut')}) ; {len(proofs)} preuve(s), {bad} en écart ; "
                f"lock_reason {', '.join(reasons)}")

    rows = [row("1. Solde du compte, soldes courants, séquences", "1", ledger),
            row("2. Ventilation des lignes (net + TPS + TVQ)", "2", lines)]
    for first, last in scope.periods:
        key = _period_key(first, last)
        rows.append(row(f"3. Du {first} au {last}", "3", lambda c, k=key: _period_text(c[k])))
    rows += [row("3. Journal entier", "3", lambda c: _period_text(c["journal"])),
             row("4. Verrou et conciliations", "4", lock),
             row("5. Autres documents lus", "5", lambda c: f"{len(c)} document(s)")]
    return rx._table(columns, rows)


def render_report(plan: Plan, *, mode: str, approved: Optional[str] = None,
                  refusal: Optional[list] = None, outcome: Optional[str] = None,
                  backup: Optional[tuple] = None, real: Optional[dict] = None,
                  drift: Optional[list] = None, real_gaps: Optional[list] = None) -> str:
    spec = plan.spec
    out = [
        "# Rectification de la ventilation de dépenses d'administration",
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
    out += ["", "## Motif", "", f"> {spec.motif}"]
    if spec.note:
        out += ["", f"Note du plan : {spec.note}"]
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
    out += rx._table(
        ("Ligne", "Séq.", "Écriture", "Jour", "État", "Ventilation inscrite", "Cible", "Motifs"),
        [(v.line.ligne, v.doc.get("sequence", "?"), f"`{v.line.entry_id}`",
          rx._v(rx._aware(v.doc.get("date"))), v.state,
          _split_text(v.doc.get(k, 0) for k in SPLIT) if v.doc else "—",
          _split_text(v.line.target[k] for k in SPLIT), "; ".join(v.reasons) or "—")
         for v in plan.verdicts])

    out += ["", "## 2. Ce que le plan attend, et ce que porte la base", ""]
    rows = [r for v in plan.verdicts for r in v.table] + plan.table
    out += rx._table(("Document", "Champ", "Attendu (plan)", "Base", ""),
                     [(d, f, w, h, "conforme" if ok else "**ÉCART**")
                      for d, f, w, h, ok in rows]) if rows else ["(sans objet)"]

    out += ["", "## 3. Changements prévus", ""]
    if plan.writes:
        for write in sorted(plan.writes, key=lambda w: w.ligne):
            payload = plan.payloads[write.path]
            trail = payload["revisions"][-1]
            out += [(f"### Ligne {write.ligne} — écriture `{write.doc_id}` "
                     f"(séq. {write.before.get('sequence')})"), ""]
            out += rx._table(("Champ", "Avant", "Après"),
                             [(f"`{k}`", rx._v(old), rx._v(new))
                              for k, (old, new) in write.changes.items()])
            out += ["",
                    (f"Estampilles : `updated_at`, `etag`, `updated_via` = "
                     f"`{payload.get('updated_via')}` ; `revisions` : "
                     f"{len(write.before.get('revisions') or [])} → {len(payload['revisions'])}, "
                     f"motif « {trail['motif']} », via `{trail['via']}`."), ""]
        out.append("Aucun compte, aucune conciliation, aucune autre écriture n'est écrit.")
    else:
        out.append("Aucun.")

    out += ["", "## 4. Champs employés", "",
            ("- La ventilation tient dans `net_amount`, `gst_amount` et `qst_amount` ; le modèle "
             "ne stocke aucun mode de ventilation. « sans_taxe » de `record_admin_entry` vaut "
             "net = montant, TPS = 0, TVQ = 0."),
            ("- Provenance : `script` — `updated_via` et `via` du fil, sous "
             "`provenance.writing_via`, comme les migrations du 7 octobre et du 5 novembre."),
            "", "## 5. Verrou, conciliations, déclarations", ""]
    lock_before = rx._get(plan.before, "4")
    lock_after = rx._get(plan.simulated, "4")
    for line in spec.lines:
        before = rx._shown(lock_before, lambda c, i=line.entry_id: c["lock_reasons"].get(i))
        after = rx._shown(lock_after, lambda c, i=line.entry_id: c["lock_reasons"].get(i))
        out.append(f"- Ligne {line.ligne} : `lock_reason` (dérivé à la lecture, jamais stocké) "
                   f"{before} → {after} ; conciliations qui la citent : "
                   + (", ".join(plan.citing.get(line.entry_id) or []) or "aucune") + ".")
    out += ["- Champs des conciliations hors de la forme du modèle : "
            + (", ".join(f"`{k}`" for k in plan.reconciliation_keys) or "aucun")
            + " — une conciliation garde des montants bruts (relevé, livre, en circulation) "
              "et les identifiants cochés, jamais une ventilation : aucune ne fige le net, "
              "la TPS ou la TVQ.",
            ("- Périodes de déclaration de TPS/TVQ : le modèle n'en enregistre aucune ; aucun "
             "total de taxes n'est figé par période."),
            ("- Ni le plancher, ni la dernière conciliation, ni aucune conciliation ne sont "
             "écrits.")]

    out += ["", "## 6. Contrôles", ""]
    out += _render_controls(plan.scope, plan.store.accounts, plan.before, plan.simulated, real)
    out += ["", "Écarts : " + ("aucun" if not (plan.gaps or plan.impossible)
                               else "; ".join(plan.gaps + plan.impossible))]

    out += ["", "## 7. Caches", "",
            ("Aucun à recalculer : les totaux de taxes et de dépenses se calculent à la lecture "
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
        "# Restauration d'une rectification de ventilation",
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
        out += _render_controls(plan.scope, plan.store.accounts, plan.before, plan.simulated, real)
        out += ["", "Écarts : " + ("aucun" if not (plan.gaps or plan.impossible)
                                   else "; ".join(plan.gaps + plan.impossible))]
    return "\n".join(out) + "\n"


# ── the command line ──────────────────────────────────────────────────────


def _summary(plan: Plan, mode: str) -> list:
    return [
        f"Rectification de ventilation — {mode}",
        f"Projet : {plan.project}",
        f"Plan : {plan.path} (SHA-256 {plan.sha})",
        (f"Lignes : {plan.count(PENDING)} à appliquer, {plan.count(DONE)} déjà appliquée(s), "
         f"{plan.count(REFUSED)} refusée(s)"),
        "Intouchables : " + ("concordent" if not plan.differences
                             else f"{len(plan.differences)} écart(s)"),
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
        return rla._finish(report, f"# Rectification de ventilation\n\n- Date : "
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
    parser = rla._Parser(prog="python -m scripts.rectify_expense_ventilation",
                         description="Rectification de la ventilation de dépenses "
                                     "d'administration (hors modèle, à blanc par défaut).")
    parser.add_argument("plan", nargs="?", help="le plan JSON approuvé (hors du dépôt public)")
    parser.add_argument("--rapport", metavar="RAPPORT.md",
                        help="le rapport à écrire (hors du dépôt ; nouveau fichier)")
    parser.add_argument("--sauvegarde", metavar="DOSSIER",
                        help="le dossier où écrire la sauvegarde avant le lot (hors du dépôt)")
    parser.add_argument("--appliquer", metavar="EMPREINTE",
                        help="écrire — avec l'empreinte de la passe à blanc approuvée")
    parser.add_argument("--restaurer", metavar="SAUVEGARDE.json",
                        help="restaurer les ventilations sauvegardées (à blanc sans --appliquer)")
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
