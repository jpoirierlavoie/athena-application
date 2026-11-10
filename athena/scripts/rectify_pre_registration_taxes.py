"""Zero, OUTSIDE the models, the TPS/TVQ of the administration purchases dated
before the firm's TPS/TVQ registration — net brought up to the gross amount
— a one-shot migration bound to its plan (the lawyer's decision of
2026-11-10).

What it does
------------
The firm registered for TPS/TVQ on a given day (the plan's ``avant``): the
tax paid on an earlier purchase was never claimable. The administration ledger still
records it as input-tax credits on some entries (the purchases the connector
inscribed from their receipts). On every entry the RULE of the plan names —
a kind of ``natures`` (``dépense``, ``remboursement_depense``), a UTC day
strictly before ``avant``, a TPS or TVQ other than zero — this script
writes the split ``net_amount = amount``, ``gst_amount = 0``,
``qst_amount = 0``, and nothing else. A supplier's refund of such a purchase
gives no credit back either: it is zeroed the same way.

Why it writes outside the models
--------------------------------
CLAUDE.md, « One door per rule »: a declared exception, on the pattern of
``scripts/reclassify_admin_category`` (which also writes unlocked entries
outside the model) and ``scripts/rectify_admin_entries``, whose helpers it
imports and leaves untouched; it closes with its plan. ``update_transaction``
would accept an unlocked entry, but its trail entry carries no motif and no
plan digest, it trims the trail at 25, it rewrites the whole document, and
it stamps the account on every edit (a write per entry the controls would
have to excuse); it refuses a locked one. A LOCKED entry is accepted here,
as in the earlier scripts: a split moves no balance (the amount, the sign
and the dates stay) and no reconciliation (a reconciliation keeps gross
amounts and the ids it cleared, never a split). The target is held to the
MODEL's own rules: ``validate_ventilation`` must return it unchanged for the
entry's amount and kind, and ``_validate_business`` (on a COPY: it mutates
its argument) must accept it. The model keeps no ventilation mode (only the
three figures), no TPS/TVQ period and no registration date.

What it writes, on each entry: ``net_amount``, ``gst_amount`` and
``qst_amount`` where they change, the house stamps (``updated_at``, a new
``etag``, ``updated_via = "script"`` under ``provenance.writing_via``) and ONE
``revisions`` trail entry ``{at, via, motif, changes, updated_at_before,
source_sha256}`` whose motif is the plan's, verbatim. Never the amount, the
sign, a date, the status, the kind, the category, a link or a stamp of
creation or clearing; no account, no reconciliation, no invoice, no débours,
no trust entry, no file in Storage. The registers are not DAV-exposed: no
CTag.

ONE TRANSACTION PER ENTRY, in day order: each re-reads its entry and commits
only if it is still the version the dry run read (its ``update_time``) and
the merged document passes the entry's invariants. A refusal rolls that
transaction back and STOPS the run; a re-run resumes.

The plan
--------
A JSON object (UTF-8), outside the repository::

    {"avant": "AAAA-MM-JJ",                       # the registration day
     "natures": ["dépense", "remboursement_depense"],
     "motif": "…",                                # the trail's motif, verbatim
     "note": "…",                                 # optional, for the reader
     "attendu": {"dépense": {"lignes": 0, "brut": 0, "net": 0, "tps": 0, "tvq": 0},
                 "remboursement_depense": {…}}}

``attendu`` states, per kind, the set the lawyer approves: how many entries
and their gross, net, TPS and TVQ BEFORE the correction. The dry run derives
the set from the rule — plus every entry whose trail carries this plan's
digest, its « before » split read from that trail entry, so a resumed run or
a re-run still states the same set — and BLOCKS when a count or a total
differs: the plan is never bent to fit the base. Once a run has written,
the plan file is frozen: resume with the same file; restore before changing
it.

The set, and what blocks
------------------------
Days are UTC days (a date-only value is stored at midnight UTC), never
Montréal's. Any refused candidate BLOCKS the run — the plan covers the whole
set: a member of a reversal (from either end), a correction, an annulée
entry, one linked to an invoice or to a trust entry, a trail too full to
keep room for this write and one restoration, an unreadable amount, split
or date, a date not at midnight UTC, a direction the kind contradicts, a
lock a split could move, a target the model refuses, a reconciliation that
cites the entry with a field outside the model's shape, an inexact stored
split (net + TPS + TVQ ≠ montant: its backup could not restore exactly), a
trail carrying the plan without the target (« état mixte »). A taxed entry
whose stored instant is not midnight UTC and whose UTC and Montréal days fall
on opposite sides of the cut-off joins the set and is refused: its day is
ambiguous. An entry this plan wrote and that holds its target is « déjà
appliquée »; an entry zeroed outside this plan leaves the set, and the stated
totals then block (the report lists such entries).

The controls
------------
On complete reads — every administration entry, account and
reconciliation, every invoice, the trust register, every dossier and débours
— BEFORE, AFTER SIMULATED (the documents read, merged with the exact
payloads) and, once written, AFTER (re-read):

1. each account's stored ``ledger_balance`` and Σ ``admin_delta``, every
   entry's running balance and sequence — unchanged;
2. each entry of the set moves only its split, from its before-state to its
   target, its trail by exactly one entry; net + TPS + TVQ = amount;
3. ``admin_ledger.results_statement`` per account and over the whole
   journal, before the cut-off day and from it on — moved by exactly what
   the same function says of the written entries alone;
4. the entries the rule still finds (« reste ») — exactly those not written;
5. each account's lock floor, last reconciliation and the re-proof of every
   completed reconciliation, and each entry's lock reason — unchanged;
6. every other document read — unchanged (a digest per document).

A control that cannot be computed, or that moves otherwise, refuses
``--appliquer``; so do the two judged beside them: the plan's stated totals
against the set (control 7, re-measured on the read after the write), and the
effect on the financial statements (control 8: ``models/financial_statements.
compute`` on the run's own read, plus the trust accounts and the time
entries), each period's figures moved by exactly what the written entries
alone imply.

Fingerprint, backup, restoration
--------------------------------
As in ``scripts/rectify_admin_entries``: the fingerprint covers the plan's
digest, ``db.project``, every planned write (path, the ``update_time`` read,
the fields written) and every entry of the set that is not to write, with
its reasons; the typed JSON backup of each document to write — whole — is
written, fsynced, read back and verified before the first transaction.
``--restaurer`` (dry by default, its own fingerprint) judges each saved
entry on its own: written by this plan and untouched since → restored, one
transaction per entry, with fresh stamps and a trail entry « Restauration de
la sauvegarde du … »; never written by it (a run stopped before it) or since
deleted, or already holding its saved split → nothing to do; written by it
and since reversed, annulled, linked or followed by another write → refused,
and with it the whole restoration (a pair restored on one side would no
longer net).

Running it — from ``athena/``, with ADC and the environment of « Active
gabarits » in DEPLOYMENT.md: even the dry run reads PRODUCTION. A plan,
report, backup or backup directory inside the repository is refused before
anything is created; the report must be a new file.

    python -m scripts.rectify_pre_registration_taxes PLAN.json --rapport R.md
    python -m scripts.rectify_pre_registration_taxes PLAN.json --rapport R.md \\
        --sauvegarde DOSSIER --appliquer EMPREINTE
    python -m scripts.rectify_pre_registration_taxes --restaurer SAUVEGARDE.json \\
        --rapport R.md [--appliquer EMPREINTE]

Afterwards, run both integrity scripts and compare their findings BY TEXT.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from google.cloud import firestore

from models import admin_ledger as al
from models import db, full_reads, provenance
from models import financial_statements as fs
from models import time_entry as time_entry_model
from models import trust
from scripts import reclassify_admin_category as rc
from scripts import reclassify_admin_ledger as rla
from scripts import reclassify_as_invoice_receipt as rx
from scripts import rectify_admin_entries as rae
from scripts.rectify_registers import _addressable
from tz import to_mtl
from utils import periods
from utils.deadlines import today_mtl

UTC = timezone.utc
TX = al.TRANSACTIONS_COLLECTION
SPLIT = ("net_amount", "gst_amount", "qst_amount")
#: What a write moves on an entry: its split, the trail and the stamps.
_WRITTEN = SPLIT + ("revisions", "updated_at", "etag", "updated_via")
#: The figures the plan states per kind, before the correction.
FIGURES = ("lignes", "brut", "net", "tps", "tvq")
#: The lock reasons a split cannot move — accepted, and held unchanged.
ACCEPTED_LOCKS = (None, "écriture_verrouillée", "période_verrouillée")
MAX_TRAIL_BEFORE = rae.MAX_TRAIL_BEFORE
FINGERPRINT_HEX = 16
BACKUP_FORMAT = "athena.taxes-avant-inscription/1"
RESTORE_MOTIF = "Restauration de la sauvegarde du {quand}"
PENDING, DONE, REFUSED = "à appliquer", "déjà appliquée", "refusée"
RESTORE_PENDING, RESTORE_DONE = rx.RESTORE_PENDING, rx.RESTORE_DONE
WRITTEN, LOST_BUT_WRITTEN, NOTHING, UNCERTAIN, ABORTED = (
    rae.WRITTEN, rae.LOST_BUT_WRITTEN, rae.NOTHING, rae.UNCERTAIN, rae.ABORTED)
_CONTROLS = ("1", "2", "3", "4", "5", "6")
_BACKUP_DOCUMENT_KEYS = ("chemin", "ligne", "update_time_lu", "etag_migration", "champs",
                         "avant", "apres")

RunRefused = rla.RunRefused
Impossible = rla.Impossible
ControlImpossible = rla.ControlImpossible


# ── values ────────────────────────────────────────────────────────────────


def _int_or_none(value) -> Optional[int]:
    return rla._as_int(value)


def split_of(doc: dict) -> tuple:
    """``(net, TPS, TVQ)`` as stored — an absent part 0, an unreadable one
    ``None`` (which no target equals)."""
    out = []
    for key in SPLIT:
        value = doc.get(key)
        out.append(0 if value is None else _int_or_none(value))
    return tuple(out)


def target_of(doc: dict) -> Optional[tuple]:
    amount = _int_or_none(doc.get("amount"))
    return None if amount is None else (amount, 0, 0)


def _taxed(doc: dict) -> bool:
    """A TPS or a TVQ other than zero — an unreadable one counts: the rule
    cannot clear it."""
    _net, gst, qst = split_of(doc)
    return gst != 0 or qst != 0


def _at_midnight(value) -> bool:
    instant = rx._aware(value)
    if not isinstance(instant, datetime):
        return False
    instant = instant.astimezone(UTC)
    return (instant.hour, instant.minute, instant.second, instant.microsecond) == (0, 0, 0, 0)


def _shown_split(split) -> str:
    return " + ".join(rla._money(v) for v in split)


# ── the plan file ─────────────────────────────────────────────────────────


@dataclass
class Spec:
    """The plan, its form checked — nothing read yet."""

    cutoff: date
    natures: tuple
    motif: str
    note: str
    stated: dict          # kind → {lignes, brut, net, tps, tvq}


def _refuse(message: str) -> RunRefused:
    return RunRefused(f"plan mal formé : {message}")


def parse_plan(data: dict) -> Spec:
    """The plan's form, judged before any read — any defect refuses."""
    rx._exact_keys({k: v for k, v in data.items() if k != "note"},
                   ("avant", "natures", "motif", "attendu"), "plan")
    note = data.get("note", "")
    if not isinstance(note, str):
        raise _refuse("« note » doit être un texte")
    cutoff = rae._day(data["avant"], "avant")
    natures = data["natures"]
    if (not isinstance(natures, list) or not natures
            or any(not isinstance(k, str) or k not in al.CATEGORY_KINDS for k in natures)
            or len(set(natures)) != len(natures)):
        raise _refuse(f"« natures » : une liste, sans doublon, de "
                      f"{', '.join(al.CATEGORY_KINDS)}")
    motif = rae._text(data["motif"], "motif")
    stated = data["attendu"]
    if not isinstance(stated, dict) or set(stated) != set(natures):
        raise _refuse("« attendu » : un objet par nature de « natures », et rien d'autre")
    out = {}
    for kind, figures in stated.items():
        where = f"attendu.{kind}"
        if not isinstance(figures, dict):
            raise _refuse(f"« {where} » doit être un objet")
        rx._exact_keys(figures, FIGURES, where)
        out[kind] = {k: rae._int(figures[k], f"{where}.{k}", minimum=0) for k in FIGURES}
        if out[kind]["net"] + out[kind]["tps"] + out[kind]["tvq"] != out[kind]["brut"]:
            raise _refuse(f"« {where} » : net + TPS + TVQ doit égaler le brut")
    return Spec(cutoff=date.fromisoformat(cutoff), natures=tuple(natures), motif=motif,
                note=note, stated=out)


# ── reads ─────────────────────────────────────────────────────────────────


@dataclass
class Extra:
    """The two collections the statements read that the run does not
    otherwise need — ``None`` when unreadable. They feed only the
    statements' memo sections (trust funds, unbilled work), which control 8
    does not measure: an unreadable one changes no figure the run judges."""

    trust_accounts: Optional[list]
    time_entries: Optional[list]


def read_all() -> tuple:
    """``(world, extra)`` — ``rae.read_world`` (strict: a failure stops the
    run) and the statements' two other collections (fail-soft)."""
    world = rae.read_world()
    trust_accounts = time_entries = None
    try:
        trust_accounts = full_reads.stream_all(db.collection(trust.ACCOUNTS_COLLECTION),
                                               transaction=None)
    except Exception:  # fail-soft: only the trust-funds memo reads it (see Extra)
        pass
    try:
        time_entries = full_reads.stream_all(db.collection(time_entry_model.COLLECTION),
                                             transaction=None)
    except Exception:  # fail-soft: only the unbilled-work memo reads it (see Extra)
        pass
    return world, Extra(trust_accounts, time_entries)


# ── the set ───────────────────────────────────────────────────────────────


def _resolved_kind(doc: dict, rows: dict) -> str:
    """The kind *doc* counts as: a correction counts as the kind it
    reverses (``results_statement``)."""
    if doc.get("kind") != al.REVERSAL_KIND:
        return doc.get("kind") or ""
    return (doc.get("reverses_kind")
            or (rows.get(doc.get("reverses_id")) or {}).get("kind") or "")


def _trail_entry(doc: dict, sha: str) -> Optional[dict]:
    """The trail entry this plan wrote on *doc*, if any — an unreadable trail
    holds none (``fil_illisible`` refuses such an entry of the set)."""
    trail = doc.get("revisions")
    if not isinstance(trail, list):
        return None
    for item in trail:
        if isinstance(item, dict) and item.get("source_sha256") == sha:
            return item
    return None


def before_split(doc: dict, sha: str) -> tuple:
    """The split *doc* held before this plan — its trail entry's « before »
    values when the plan wrote it, else the stored split."""
    entry = _trail_entry(doc, sha)
    stored = split_of(doc)
    if entry is None:
        return stored
    changes = entry.get("changes") if isinstance(entry.get("changes"), dict) else {}
    out = []
    for key, value in zip(SPLIT, stored):
        pair = changes.get(key)
        if isinstance(pair, list) and len(pair) == 2:
            old = pair[0]
            out.append(0 if old is None else _int_or_none(old))
        else:
            out.append(value)
    return tuple(out)


def matches_rule(doc: dict, rows: dict, spec: Spec) -> bool:
    """The rule: a kind of ``natures`` — a correction counting as the kind
    it reverses —, a UTC day strictly before ``avant`` (its own, or its
    original's for a correction), a TPS or TVQ other than zero."""
    if _resolved_kind(doc, rows) not in spec.natures or not _taxed(doc):
        return False
    days = [rla._day(doc.get("date"))]
    if doc.get("kind") == al.REVERSAL_KIND:
        days.append(rla._day((rows.get(doc.get("reverses_id")) or {}).get("date")))
    return any(d is None or d < spec.cutoff for d in days)


def doubtful_day(doc: dict, spec: Spec) -> bool:
    """A taxed entry of the natures whose stored instant is not midnight UTC
    and whose UTC and Montréal days fall on opposite sides of the cut-off: its
    day is ambiguous — it joins the set, where ``date_hors_minuit`` refuses it."""
    if doc.get("kind") not in spec.natures or not _taxed(doc):
        return False
    value = rx._aware(doc.get("date"))
    if not isinstance(value, datetime) or _at_midnight(value):
        return False
    utc_day = value.astimezone(UTC).date()
    return (utc_day < spec.cutoff) != (to_mtl(value).date() < spec.cutoff)


@dataclass
class Candidate:
    doc: dict
    snap: Any
    before: tuple            # the split before this plan
    target: Optional[tuple]
    state: str = PENDING
    reasons: list = field(default_factory=list)   # [(code, French reason)]
    label: str = ""

    @property
    def entry_id(self) -> str:
        return self.doc["id"]

    @property
    def path(self) -> str:
        return f"{TX}/{self.entry_id}"

    def refuse(self, code: str, message: str) -> None:
        self.state = REFUSED
        self.reasons.append((code, message))


def candidate_refusals(doc: dict, world, floors: dict, *, written: bool = False) -> list:
    """``[(code, French reason)]`` refusing an entry of the set — *written*
    when this plan's trail entry is on it (the room left is then judged as
    before that entry)."""
    store = world.store
    out = []
    kind = doc.get("kind") or ""
    if kind == al.REVERSAL_KIND:
        out.append(("correction", "une contre-passation copie la ventilation de son original : "
                                  "la paire se corrige ensemble, jamais ici"))
    if (doc.get("reverses_id") or doc.get("reversed_by_id")
            or doc.get("id") in rc._named_by_a_correction(store)):
        out.append(("lien_contre_passation", "membre d'une contre-passation : la paire ne se "
                                             "compenserait plus"))
    if doc.get("status") == "annulée":
        out.append(("annulée", "l'écriture est annulée"))
    if doc.get("invoice_id"):
        out.append(("liée_facture", "l'écriture est liée à une facture"))
    if doc.get("trust_transaction_id"):
        out.append(("liée_fideicommis", "l'écriture est liée à une écriture du fidéicommis"))
    amount = doc.get("amount")
    if _int_or_none(amount) is None or amount <= 0:
        out.append(("montant_illisible", "montant illisible"))
    if None in split_of(doc):
        out.append(("ventilation_illisible", "ventilation illisible"))
    elif _int_or_none(amount) is not None and sum(split_of(doc)) != amount:
        out.append(("ventilation_inexacte", "net + TPS + TVQ ≠ montant : la ventilation inscrite "
                                            "ne se restaurerait pas exactement"))
    if rla._day(doc.get("date")) is None:
        out.append(("date_illisible", "date illisible"))
    elif not _at_midnight(doc.get("date")):
        out.append(("date_hors_minuit", "la date n'est pas à minuit UTC : son jour dépend du "
                                        "fuseau — rien n'est jugé sur un jour douteux"))
    if kind in al._KIND_DIRECTION and doc.get("direction") != al._KIND_DIRECTION[kind]:
        out.append(("sens_incohérent", f"sens « {doc.get('direction')} » pour la nature "
                                       f"« {kind} »"))
    length = rc._trail_length(doc)
    if length is not None and written:
        length -= 1
    if length is None:
        out.append(("fil_illisible", "fil de révisions illisible"))
    elif length > MAX_TRAIL_BEFORE:
        out.append(("fil_plein", f"{length} entrées au fil — au plus {MAX_TRAIL_BEFORE}, pour "
                                 f"garder la place de cette écriture et d'une restauration"))
    reason = al._entry_lock_reason(doc, floors.get(doc.get("account_id") or ""))
    if reason not in ACCEPTED_LOCKS:
        out.append(("verrou", f"verrou « {reason} » : la ventilation s'y corrige par son propre "
                              f"chemin"))
    for rec_id, keys in rae._snapshot_reconciliations(doc, store):
        out.append(("conciliation_instantané",
                    f"la conciliation {rec_id} cite l'écriture et porte des champs hors de la "
                    f"forme du modèle ({', '.join(keys)})"))
    return out


def model_refusals(doc: dict, target: tuple) -> list:
    """The model's own judgment of the target split on this entry."""
    kind = doc.get("kind") or ""
    amount = _int_or_none(doc.get("amount"))
    if amount is None or kind == al.REVERSAL_KIND:
        return []
    accepted, reason = al.validate_ventilation(doc.get("direction") or "", amount, *target,
                                               kind=kind)
    if accepted != dict(zip(SPLIT, target)):
        return [("ventilation_refusée", f"le modèle n'accepte pas {_shown_split(target)} telle "
                                        f"quelle ({reason or 'normalisée autrement'})")]
    ventilation, reason = al._validate_business(dict(doc, **dict(zip(SPLIT, target))))
    if reason:
        return [(reason, al._ABORT_MESSAGES.get(reason, reason))]
    if tuple(int(ventilation[k]) for k in SPLIT) != target:
        return [("ventilation_refusée", "le modèle normaliserait la ventilation autrement")]
    return []


def derive(world, spec: Spec, sha: str, floors: dict) -> list:
    """The set — the rule's matches and every entry this plan wrote — each
    judged, in day, account and sequence order."""
    store = world.store
    rows = {eid: {**row, "id": eid} for eid, row in store.rows.items()}
    out = []
    for eid, doc in rows.items():
        hit = _trail_entry(doc, sha) is not None
        if not hit and not matches_rule(doc, rows, spec) and not doubtful_day(doc, spec):
            continue
        candidate = Candidate(doc=doc, snap=store.entries[eid], before=before_split(doc, sha),
                              target=target_of(doc))
        for code, message in candidate_refusals(doc, world, floors, written=hit):
            candidate.refuse(code, message)
        if candidate.target is not None and candidate.state != REFUSED:
            for code, message in model_refusals(doc, candidate.target):
                candidate.refuse(code, message)
        if candidate.state != REFUSED:
            at_target = split_of(doc) == candidate.target
            if hit and not at_target:
                candidate.refuse("état_mixte", "état mixte : le fil porte ce plan, mais la "
                                               "ventilation n'est pas la cible")
            elif at_target:
                candidate.state = DONE
        out.append(candidate)
    out.sort(key=lambda c: (rla._day(c.doc.get("date")) or date.min,
                            str(c.doc.get("account_id") or ""),
                            _int_or_none(c.doc.get("sequence")) or 0, c.entry_id))
    for n, candidate in enumerate(out, start=1):
        candidate.label = f"L{n}"
    return out


def figures_of(candidates: list, natures: tuple) -> dict:
    """Per kind: how many entries of the set and their before-split totals."""
    out = {kind: dict.fromkeys(FIGURES, 0) for kind in natures}
    for c in candidates:
        kind = _resolved_kind(c.doc, {})
        slot = out.setdefault(kind, dict.fromkeys(FIGURES, 0))
        net, gst, qst = (v or 0 for v in c.before)
        slot["lignes"] += 1
        slot["brut"] += _int_or_none(c.doc.get("amount")) or 0
        slot["net"] += net
        slot["tps"] += gst
        slot["tvq"] += qst
    return out


# ── the controls ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Scope:
    """What the controls are about: the set's entries and the rule."""

    entry_ids: tuple
    cutoff: date
    natures: tuple
    account_ids: tuple


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
        split = split_of(row)
        amount = _int_or_none(row.get("amount"))
        out[entry_id] = {
            "faits": rla._digest({k: v for k, v in row.items() if k not in _WRITTEN}),
            "ventilation": list(split),
            "somme_juste": (amount is not None and None not in split and sum(split) == amount),
            "fil": rla._digest(trail),
            "fil_moins_un": (rla._digest(trail[:-1])
                             if isinstance(trail, list) and trail else None),
            "longueur": len(trail) if isinstance(trail, list) else None,
        }
    return out


def _window(row: dict, cutoff: date) -> str:
    day = rx._day_of(row.get("date"), f"écriture {row.get('id')}")
    return "avant" if day < cutoff else "après"


def _control_windows(rows: list, scope: Scope) -> dict:
    resolved = rx._resolved(rows)
    out = {}
    for key, mine in [("journal", resolved)] + [
            (f"compte/{a}", [r for r in resolved if r.get("account_id") == a])
            for a in scope.account_ids]:
        out[key] = {w: al.results_statement([r for r in mine if _window(r, scope.cutoff) == w])
                    for w in ("avant", "après")}
    return out


def _remainder(rows: list, scope: Scope) -> list:
    """The entries the rule still finds, sorted."""
    by_id = {r.get("id"): r for r in rows}
    spec = Spec(cutoff=scope.cutoff, natures=scope.natures, motif="", note="", stated={})
    return sorted(str(r.get("id")) for r in rows if matches_rule(r, by_id, spec))


def compute_controls(world, scope: Scope, payloads: Optional[dict] = None) -> dict:
    """The six controls, on the documents read — merged with *payloads* (by
    path) for the simulation: a payload on ANY document read shows."""
    store = world.store
    rows = store.control_rows(payloads)
    accounts = store.control_accounts(payloads)
    recs = rx._merged(rx.RECS, store.recs, payloads)
    invoices = rx._merged(rx.INVOICES, store.invoices, payloads)
    trust_rows = rx._merged(rx.TRUST_TX, store.trust_rows, payloads)
    dossiers = rx._merged(rae.DOSSIERS, [{**d, "id": i} for i, d in world.dossiers.items()],
                          payloads)
    expenses = rx._merged(rae.EXPENSES, world.expenses, payloads)
    skip = frozenset(f"{TX}/{i}" for i in scope.entry_ids)
    if invoices is None:
        others = Impossible(f"factures illisibles ({store.invoices_error})")
    elif trust_rows is None:
        others = Impossible(f"registre du fidéicommis illisible ({store.trust_error})")
    elif expenses is None:
        others = Impossible(f"débours illisibles ({world.expenses_error})")
    else:
        others = rla._attempt(rae._control_others, world, rows, accounts, recs, invoices,
                              trust_rows, dossiers, expenses, skip)
    return {
        "1": rla._attempt(rx._control_ledger, rows, accounts),
        "2": rla._attempt(_control_lines, rows, scope.entry_ids),
        "3": rla._attempt(_control_windows, rows, scope),
        "4": rla._attempt(_remainder, rows, scope),
        "5": rla._attempt(rae._control_lock, rows, accounts, recs, scope),
        "6": others,
    }


@dataclass(frozen=True)
class Expected:
    """What the write must move: each written entry's split ``(before,
    after)`` (control 2), the results statements' flat deltas (control 3)
    and the entries the rule still finds after (control 4)."""

    moves: dict
    results: dict
    remainder: list


def expected_effects(pairs: list, rows: list, scope: Scope) -> Expected:
    """What the written entries alone move — as stored, then as their targets
    say (never as a payload says: a payload that strays from its plan must
    show in the controls)."""
    def delta(subset: list) -> dict:
        return rla._flat_results_delta(al.results_statement([b for b, _a in subset]),
                                       al.results_statement([a for _b, a in subset]))

    results = {}
    for key, mine in [("journal", pairs)] + [
            (f"compte/{a}", [(b, x) for b, x in pairs if b.get("account_id") == a])
            for a in scope.account_ids]:
        results[key] = {w: delta([(b, x) for b, x in mine if _window(b, scope.cutoff) == w])
                        for w in ("avant", "après")}
    after = {a.get("id"): a for _b, a in pairs}
    remainder = _remainder([after.get(r.get("id"), r) for r in rows], scope)
    moves = {b.get("id"): (list(split_of(b)), list(split_of(a))) for b, a in pairs}
    return Expected(moves=moves, results=results, remainder=remainder)


def _line_gaps(before: dict, after: dict, moves: dict) -> list:
    gaps = []
    for entry_id in sorted(before, key=str):
        b, a = before[entry_id], after.get(entry_id)
        if entry_id not in moves:
            if a != b:
                gaps.append(f"contrôle 2 — écriture {entry_id} : modifiée, alors qu'elle n'est "
                            f"pas à écrire")
            continue
        old, new = moves[entry_id]
        if b is None or a is None:
            gaps.append(f"contrôle 2 — écriture {entry_id} absente de la lecture")
            continue
        if a["faits"] != b["faits"]:
            gaps.append(f"contrôle 2 — écriture {entry_id} : un autre champ que la ventilation, "
                        f"le fil et les estampilles a bougé")
        if (b["ventilation"], a["ventilation"]) != (old, new):
            gaps.append(f"contrôle 2 — écriture {entry_id} : {b['ventilation']} → "
                        f"{a['ventilation']}, attendu {old} → {new}")
        if (b["longueur"] is None or a["longueur"] != b["longueur"] + 1
                or a["fil_moins_un"] != b["fil"]):
            gaps.append(f"contrôle 2 — écriture {entry_id} : le fil ne gagne pas exactement "
                        f"une entrée, les précédentes intactes")
        if not a["somme_juste"]:
            gaps.append(f"contrôle 2 — écriture {entry_id} : net + TPS + TVQ ≠ montant")
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
    for key in ("1", "6"):
        if key not in blocked and before[key] != after[key]:
            gaps.extend(f"contrôle {key} — {d}" for d in rla._differences(before[key], after[key]))
    if "2" not in blocked:
        gaps += _line_gaps(before["2"], after["2"], expected.moves)
    if "3" not in blocked:
        for key, windows in expected.results.items():
            for window, want in windows.items():
                got = rla._flat_results_delta(before["3"][key][window], after["3"][key][window])
                if got != want:
                    gaps.append(f"contrôle 3 ({key}, {window} la date) — écart {got}, "
                                f"attendu {want}")
    if "4" not in blocked and after["4"] != expected.remainder:
        gaps.append(f"contrôle 4 — la règle trouve encore {len(after['4'])} écriture(s) "
                    f"({', '.join(after['4'][:6])}{'…' if len(after['4']) > 6 else ''}), "
                    f"attendu {len(expected.remainder)}")
    if "5" not in blocked:
        if before["5"]["conciliations"] != after["5"]["conciliations"]:
            gaps.extend(f"contrôle 5 — {d}" for d in rla._differences(
                before["5"]["conciliations"], after["5"]["conciliations"]))
        if before["5"]["lock_reasons"] != after["5"]["lock_reasons"]:
            gaps.extend(f"contrôle 5 — {d}" for d in rla._differences(
                before["5"]["lock_reasons"], after["5"]["lock_reasons"]))
    return gaps, impossible


# ── control 8: the financial statements ───────────────────────────────────


def _statement_snapshot(world, extra: Extra, payloads: Optional[dict] = None) -> fs.Snapshot:
    """The statements' snapshot built from the run's own read, merged with
    *payloads* for the simulation."""
    store = world.store
    snap = fs.Snapshot()
    snap.admin_accounts = [{**a, "id": aid} for aid, a in store.control_accounts(payloads).items()]
    snap.admin_transactions = store.control_rows(payloads)
    snap.admin_reconciliations = rx._merged(rx.RECS, store.recs, payloads)
    snap.invoices = rx._merged(rx.INVOICES, store.invoices, payloads)
    snap.trust_accounts = extra.trust_accounts
    snap.trust_transactions = rx._merged(rx.TRUST_TX, store.trust_rows, payloads)
    snap.time_entries = extra.time_entries
    snap.expenses = rx._merged(rae.EXPENSES, world.expenses, payloads)
    for name in fs.COLLECTIONS:
        if getattr(snap, name) is None:
            snap.failures[name] = "read"
    return snap


def statement_periods(cutoff: date) -> list:
    """The periods control 8 measures: the year of the last day before the
    cut-off, the quarters on either side of it, and the next year — those
    started by today."""
    last = cutoff - timedelta(days=1)

    def quarter(day: date) -> periods.Period:
        return periods.Period("trimestre", day.year, (day.month - 1) // 3 + 1)

    out, seen = [], set()
    for period in (periods.Period("annee", last.year, 1), quarter(last), quarter(cutoff),
                   periods.Period("annee", cutoff.year + 1, 1)):
        if period.slug not in seen and period.start <= today_mtl():
            seen.add(period.slug)
            out.append(period)
    return out


def _statement_figures(snapshot: fs.Snapshot, cutoff: date) -> dict:
    """The figures the correction moves, per period of
    :func:`statement_periods` — a section the statements cannot compute is
    a reason."""
    today, now = today_mtl(), datetime.now(UTC)
    out = {}
    for period in statement_periods(cutoff):
        slug = period.slug
        st = fs.compute(snapshot, period, today=today, now=now)
        sec = st.sections
        if not all(k in sec for k in ("resultats", "taxes", "bilan", "avoir", "flux")):
            missing = [k for k in ("resultats", "taxes", "bilan", "avoir", "flux")
                       if k not in sec]
            out[slug] = Impossible("; ".join(st.unavailable.get(k) or k for k in missing))
            continue
        points = {p["key"]: p["count"] for p in st.points}
        out[slug] = {
            "cti": sec["taxes"]["cti"], "rti": sec["taxes"]["rti"],
            "couts": sum(sec["resultats"]["cost_totals"].values()),
            "benefice": sec["resultats"]["net_income"],
            "tps_tvq_ouverture": sec["taxes"]["opening"],
            "tps_tvq_cloture": sec["taxes"]["closing"],
            "avoir_net_cloture": sec["bilan"]["closing"].net,
            "tresorerie_cloture": sec["flux"]["closing"],
            "ecart_non_explique": sec["avoir"]["unexplained"],
            "sans_ventilation": points.get("sans_ventilation", 0),
        }
    return out


def statement_figures(world, extra: Extra, cutoff: date, payloads: Optional[dict] = None):
    return rla._attempt(_statement_figures, _statement_snapshot(world, extra, payloads), cutoff)


def expected_statement_deltas(pairs: list, cutoff: date) -> dict:
    """What the written entries alone move in each period's figures."""
    today = today_mtl()
    out = {}
    for period in statement_periods(cutoff):
        slug = period.slug
        end = min(period.end, today)
        opening_day = period.start - timedelta(days=1)
        d = dict.fromkeys(("cti", "rti", "couts", "benefice", "tps_tvq_ouverture",
                           "tps_tvq_cloture", "avoir_net_cloture", "tresorerie_cloture",
                           "ecart_non_explique", "sans_ventilation"), 0)
        for b, a in pairs:
            day = rla._day(b.get("date"))
            factor = -1 if b.get("kind") in al.REFUND_KINDS else 1
            (nb, gb, qb), (na, ga, qa) = split_of(b), split_of(a)
            credits = factor * ((gb - ga) + (qb - qa))
            if period.start <= day <= end:
                d["cti"] += factor * (ga - gb)
                d["rti"] += factor * (qa - qb)
                d["couts"] += factor * (na - nb)
                d["benefice"] -= factor * (na - nb)
                if b.get("kind") == "dépense" and not ga and not qa and (gb or qb):
                    d["sans_ventilation"] += 1
            if day <= opening_day:
                d["tps_tvq_ouverture"] += credits
            if day <= end:
                d["tps_tvq_cloture"] += credits
                d["avoir_net_cloture"] -= credits
        out[slug] = d
    return out


def statements_verdict(before, after, expected: dict) -> list:
    """The gaps of control 8: a period the statements cannot compute, or a
    figure that moves otherwise than the written entries alone imply."""
    if isinstance(before, Impossible) or isinstance(after, Impossible):
        reason = (before if isinstance(before, Impossible) else after).reason
        return [f"contrôle 8 impossible à calculer ({reason})"]
    gaps = []
    for slug, want in expected.items():
        b, a = before.get(slug), after.get(slug)
        if isinstance(b, Impossible) or isinstance(a, Impossible) or b is None or a is None:
            reason = next((x.reason for x in (b, a) if isinstance(x, Impossible)), "absente")
            gaps.append(f"contrôle 8 ({slug}) — état indisponible ({reason})")
            continue
        for key, value in want.items():
            if a[key] - b[key] != value:
                gaps.append(f"contrôle 8 ({slug}, {key}) — écart {a[key] - b[key]}, attendu "
                            f"{value}")
    return gaps


def stated_gaps(world, spec: Spec, sha: str) -> list:
    """Control 7 on a read: the set's totals, before-states from the trails,
    against the plan's."""
    derived = figures_of(derive(world, spec, sha, rae._floors(world)), spec.natures)
    gaps = []
    for kind, stated in spec.stated.items():
        for name in FIGURES:
            have = derived.get(kind, {}).get(name, 0)
            if have != stated[name]:
                gaps.append(f"contrôle 7 — « attendu.{kind}.{name} » : le plan énonce "
                            f"{stated[name]}, la base {have}")
    return gaps


# ── the plan, prepared ────────────────────────────────────────────────────


@dataclass
class Plan:
    path: Path
    sha: str
    project: str
    spec: Spec
    world: Any
    extra: Any
    candidates: list
    stated_rows: list = field(default_factory=list)   # (nature, figure, plan, base, ok)
    differences: list = field(default_factory=list)   # blocking: stated totals, refusals
    writes: list = field(default_factory=list)
    payloads: dict = field(default_factory=dict)
    citing: dict = field(default_factory=dict)
    reconciliation_keys: list = field(default_factory=list)
    before: dict = field(default_factory=dict)
    simulated: dict = field(default_factory=dict)
    expected: Any = None
    gaps: list = field(default_factory=list)
    impossible: list = field(default_factory=list)
    statements_before: Any = None
    statements_simulated: Any = None
    statements_expected: dict = field(default_factory=dict)
    statement_gaps: list = field(default_factory=list)
    fingerprint: str = ""

    @property
    def scope(self) -> Scope:
        return Scope(tuple(c.entry_id for c in self.candidates), self.spec.cutoff,
                     self.spec.natures, tuple(sorted(self.world.store.accounts)))

    @property
    def blocking(self) -> list:
        return self.differences + self.gaps + self.impossible + self.statement_gaps

    def count(self, state: str) -> int:
        return sum(1 for c in self.candidates if c.state == state)


def _judge_stated(plan: Plan) -> None:
    derived = figures_of(plan.candidates, plan.spec.natures)
    for kind, stated in plan.spec.stated.items():
        for name in FIGURES:
            have = derived.get(kind, {}).get(name, 0)
            ok = have == stated[name]
            shown = str if name == "lignes" else rla._money
            plan.stated_rows.append((al.KIND_LABELS.get(kind, kind), name, shown(stated[name]),
                                     shown(have), ok))
            if not ok:
                plan.differences.append(f"« attendu.{kind}.{name} » : le plan énonce "
                                        f"{stated[name]}, la base {have} — le plan ne se plie "
                                        f"pas à la base")
    extra = sorted(set(derived) - set(plan.spec.stated))
    for kind in extra:
        plan.differences.append(f"la règle trouve des écritures de nature « {kind} » que le "
                                f"plan n'énonce pas")
    refused = [c for c in plan.candidates if c.state == REFUSED]
    if refused:
        plan.differences.append(f"{len(refused)} écriture(s) de l'ensemble refusée(s) — le plan "
                                f"couvre tout l'ensemble : rien ne s'écrit")


def _plan_writes(plan: Plan) -> None:
    now = datetime.now(UTC)
    with provenance.writing_via("script"):
        for c in plan.candidates:
            if c.state != PENDING:
                continue
            doc = c.doc
            business = {k: v for k, v in zip(SPLIT, c.target) if doc.get(k) != v}
            changes = {k: [doc.get(k), business[k]] for k in business}
            trail = {"at": now, "via": provenance.current_via(), "motif": plan.spec.motif,
                     "changes": changes, "updated_at_before": doc.get("updated_at"),
                     "source_sha256": plan.sha}
            plan.payloads[c.path] = {**business,
                                     "revisions": list(doc.get("revisions") or []) + [trail],
                                     **provenance.update_fields(now)}
            plan.writes.append(rla.Write(c.path, TX, c.entry_id, 0, c.label, c.snap, doc,
                                         business, changes))


def _fingerprint(head: dict, writes: list, payloads: dict, candidates: list) -> str:
    rows = [{"ligne": w.role, "chemin": w.path,
             "update_time": rla._stamp_iso(w.snap.update_time),
             "apres": {k: payloads[w.path][k] for k in sorted(w.business)}}
            for w in writes]
    states = [{"ecriture": c.entry_id, "etat": c.state,
               "update_time": rla._stamp_iso(c.snap.update_time),
               "motifs": [[code, message] for code, message in c.reasons]}
              for c in candidates if c.state != PENDING]
    return rla._digest({**head, "ecritures": rows, "lignes": states})[:FINGERPRINT_HEX]


def prepare(plan_path: Path) -> Plan:
    """Everything up to the write: read, derive, judge, plan, simulate.
    Writes nothing. Raises :class:`RunRefused`."""
    plan_path = Path(plan_path)
    data, sha = rx._load_json(plan_path, "plan")
    spec = parse_plan(data)
    world, extra = read_all()
    floors = rae._floors(world)
    plan = Plan(path=plan_path, sha=sha, project=str(db.project), spec=spec, world=world,
                extra=extra, candidates=derive(world, spec, sha, floors))
    _judge_stated(plan)
    keys: set = set()
    for c in plan.candidates:
        citing, extra_keys = rx._reconciliation_findings(world.store.recs,
                                                         c.doc.get("account_id"), c.entry_id)
        plan.citing[c.entry_id] = citing
        keys |= set(extra_keys)
    plan.reconciliation_keys = sorted(keys)
    # Planned — and shown — even when the run is blocked: --appliquer refuses
    # on ``plan.blocking``, never on an empty list of writes.
    _plan_writes(plan)
    scope = plan.scope
    rows = world.store.control_rows()
    plan.before = compute_controls(world, scope)
    plan.simulated = compute_controls(world, scope, plan.payloads)
    pairs = [(c.doc, {**c.doc, **dict(zip(SPLIT, c.target))}) for c in plan.candidates
             if c.state == PENDING and c.path in plan.payloads]
    plan.expected = rla._attempt(expected_effects, pairs, rows, scope)
    plan.gaps, plan.impossible = compare_controls(plan.before, plan.simulated, plan.expected)
    plan.statements_before = statement_figures(world, extra, spec.cutoff)
    plan.statements_simulated = statement_figures(world, extra, spec.cutoff, plan.payloads)
    plan.statements_expected = expected_statement_deltas(pairs, spec.cutoff)
    plan.statement_gaps = statements_verdict(plan.statements_before, plan.statements_simulated,
                                             plan.statements_expected)
    plan.fingerprint = _fingerprint({"plan_sha256": sha, "projet": plan.project},
                                    plan.writes, plan.payloads, plan.candidates)
    return plan


# ── backup ────────────────────────────────────────────────────────────────


def write_backup(directory: Path, plan: Plan) -> tuple[Path, str]:
    """Write, fsync, re-read and verify the backup; ``(path, sha256)``."""
    created = datetime.now(UTC)
    documents = []
    for write in plan.writes:
        payload = plan.payloads[write.path]
        documents.append({
            "chemin": write.path, "ligne": write.role,
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
        "contexte": {"avant": plan.spec.cutoff.isoformat(), "natures": list(plan.spec.natures),
                     "comptes": sorted(plan.world.store.accounts)},
        "documents": documents, "contenu_sha256": rla._digest(documents),
    }
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=1, allow_nan=False)
    path = directory / (f"taxes-avant-inscription-{created.strftime('%Y%m%dT%H%M%S%fZ')}"
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


# ── one transaction per entry ─────────────────────────────────────────────


class _Abort(Exception):
    """An entry's transaction refuses — rolled back, nothing written."""


class _Landed(Exception):
    """The entry already carries this write's etag: a commit whose answer was
    lost, re-run by the transaction's retry — nothing more to write."""


def entry_invariants(current: dict, merged: dict, payload: dict,
                     target: Optional[tuple]) -> list:
    """What the merged entry must hold before its transaction commits."""
    problems = []
    if set(payload) - set(_WRITTEN):
        problems.append("la charge porte un champ hors de la liste permise")
    for key in sorted(set(current) | set(merged), key=str):
        if key not in _WRITTEN and (current.get(key) != merged.get(key)
                                    or (key in current) != (key in merged)):
            problems.append(f"le champ {key} changerait")
    split = split_of(merged)
    amount = _int_or_none(merged.get("amount"))
    if amount is None or None in split or sum(split) != amount:
        problems.append("net + TPS + TVQ ≠ montant")
    elif merged.get("kind") != al.REVERSAL_KIND:
        accepted, _reason = al.validate_ventilation(merged.get("direction") or "", amount,
                                                    *split, kind=merged.get("kind") or "")
        if accepted != dict(zip(SPLIT, split)):
            problems.append("le modèle n'accepte pas la ventilation telle quelle")
    if target is not None and split != target:
        problems.append(f"la ventilation écrite n'est pas la cible {_shown_split(target)}")
    if al._entry_lock_reason(merged, None) != al._entry_lock_reason(current, None):
        problems.append("le verrou de l'écriture changerait")
    if (merged.get("reverses_id") or merged.get("reversed_by_id")
            or merged.get("status") == "annulée"):
        problems.append("l'écriture est membre d'une contre-passation, ou annulée : la paire ne "
                        "se compenserait plus")
    if merged.get("invoice_id") or merged.get("trust_transaction_id"):
        problems.append("l'écriture est liée à une facture ou au fidéicommis")
    trail_before = current.get("revisions") or []
    trail_after = merged.get("revisions") or []
    if (not isinstance(trail_after, list) or len(trail_after) != len(trail_before) + 1
            or trail_after[:-1] != trail_before):
        problems.append("le fil ne gagnerait pas exactement une entrée")
    if merged.get("updated_via") != "script":
        problems.append("updated_via n'est pas « script »")
    return problems


def apply_entry(write, payload: dict, *, zeroing: bool = True) -> tuple[str, str]:
    """ONE transaction: re-read the entry, check, write. ``(outcome,
    detail)``. *zeroing* holds the written split to net = amount, no tax
    (the migration); a restoration puts the saved split back."""
    ref = db.collection(TX).document(write.doc_id)
    read_time = rla._stamp_iso(write.snap.update_time)

    @firestore.transactional
    def run(txn) -> None:
        snap = ref.get(transaction=txn)
        if not snap.exists:
            raise _Abort("l'écriture est introuvable")
        current = snap.to_dict() or {}
        if current.get("etag") == payload["etag"]:
            raise _Landed()
        if rla._stamp_iso(snap.update_time) != read_time:
            raise _Abort("l'écriture a changé depuis la lecture de la passe approuvée")
        merged = rla._merge(current, payload)
        problems = entry_invariants(current, merged, payload,
                                    target_of(current) if zeroing else None)
        if problems:
            raise _Abort("; ".join(problems))
        txn.update(ref, payload)

    try:
        run(db.transaction())
    except _Landed:
        return LOST_BUT_WRITTEN, "réponse perdue ; la relecture trouve l'écriture"
    except _Abort as abort:
        if rae._landed(write, payload) is True:
            return LOST_BUT_WRITTEN, "réponse perdue ; la relecture trouve l'écriture"
        return ABORTED, str(abort)
    except Exception as exc:
        landed = rae._landed(write, payload)
        if landed is True:
            return LOST_BUT_WRITTEN, type(exc).__name__
        if landed is False:
            return NOTHING, type(exc).__name__
        return UNCERTAIN, type(exc).__name__
    return WRITTEN, ""


def apply_all(writes: list, payloads: dict, *, zeroing: bool = True) -> list:
    """Each write in its own transaction, in order; the first one that is not
    written stops the run. ``[(write, outcome, detail)]``."""
    outcomes = []
    for write in writes:
        outcome, detail = apply_entry(write, payloads[write.path], zeroing=zeroing)
        outcomes.append((write, outcome, detail))
        rla._out(f"{write.role} — {write.doc_id} : {outcome}"
                 + (f" ({detail})" if detail else ""))
        if outcome not in (WRITTEN, LOST_BUT_WRITTEN):
            break
    return outcomes


# ── after the write ───────────────────────────────────────────────────────


def after_real(scope: Scope) -> tuple:
    """``(controls, statement figures, world)`` on a fresh read, once
    written — ``world`` is ``None`` when the read failed."""
    try:
        world, extra = read_all()
    except RunRefused as exc:
        reason = Impossible(str(exc))
        return {key: reason for key in _CONTROLS}, reason, None
    return (compute_controls(world, scope), statement_figures(world, extra, scope.cutoff),
            world)


@dataclass
class After:
    """What the read after the write says: the controls, the statements'
    figures and what the written entries alone imply for them, the gaps, and
    the drift from the approved pass (``None`` after a partial run)."""

    real: dict
    figures: Any
    statements_expected: dict
    gaps: list
    drift: Optional[list]


def _post_write(scope: Scope, before: dict, simulated: dict, expected, outcomes: list,
                rows: list, *, statements_before=None, spec: Optional[Spec] = None,
                sha: str = "") -> After:
    """Re-read after ANY outcome; judge controls 1 to 6 against the moves of
    the entries actually written (a stopped run wrote only some), control 8
    against what those entries alone imply, and control 7 on the new read."""
    real, figures, world = after_real(scope)
    written = {w.doc_id for w, o, _d in outcomes if o in (WRITTEN, LOST_BUT_WRITTEN)}
    moves = expected.moves if isinstance(expected, Expected) else {}
    by_id = {r.get("id"): r for r in rows}
    pairs = [(by_id[i], {**by_id[i], **dict(zip(SPLIT, moves[i][1]))})
             for i in sorted(written) if i in by_id and i in moves]
    if isinstance(expected, Expected) and written == set(moves):
        gaps, impossible = compare_controls(before, real, expected)
        gaps, drift = gaps + impossible, rla._differences(simulated, real)
    else:
        partial = rla._attempt(expected_effects, pairs, rows, scope)
        g, impossible = compare_controls(before, real, partial)
        gaps = [f"application partielle : {len(written)} écriture(s) sur {len(moves)} — l'écart "
                f"à la passe approuvée ne se compare pas"] + g + impossible
        drift = None
    statement_expected = expected_statement_deltas(pairs, scope.cutoff)
    if statements_before is not None:
        gaps += statements_verdict(statements_before, figures, statement_expected)
    if spec is not None:
        gaps += (stated_gaps(world, spec, sha) if world is not None
                 else ["contrôle 7 impossible à calculer (lecture après écriture impossible)"])
    return After(real, figures, statement_expected, gaps, drift)


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
    world: Any = None
    rows: list = field(default_factory=list)     # (ligne, champ, actuel, restauré)
    refusals: list = field(default_factory=list)
    notes: list = field(default_factory=list)
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


def _check_backup(body: dict) -> tuple[list, Scope, str]:
    documents, context = body.get("documents"), body.get("contexte")
    if (not isinstance(documents, list) or not documents or not isinstance(context, dict)
            or body.get("contenu_sha256") != rla._digest(documents)):
        raise RunRefused("sauvegarde altérée : son contenu ne concorde pas avec son empreinte")
    seen: set = set()
    for document in documents:
        if (not isinstance(document, dict) or set(document) != set(_BACKUP_DOCUMENT_KEYS)
                or not isinstance(document["champs"], list) or not document["champs"]
                or not set(document["champs"]) <= set(SPLIT)
                or document["champs"] != sorted(set(document["champs"]))
                or not isinstance(document["avant"], dict)
                or not isinstance(document["apres"], dict)
                or set(document["apres"]) != set(document["champs"])
                or not isinstance(document["ligne"], str)
                or not isinstance(document["chemin"], str)
                or not document["chemin"].startswith(f"{TX}/")
                or not _addressable(document["chemin"].split("/", 1)[1])
                or document["chemin"] in seen):
            raise RunRefused("sauvegarde altérée : un document n'a pas la forme attendue")
        seen.add(document["chemin"])
    try:
        cutoff = date.fromisoformat(context["avant"])
        natures = tuple(str(k) for k in context["natures"])
        accounts = tuple(sorted(str(a) for a in context["comptes"]))
        plan_sha = str((body.get("plan") or {})["sha256"])
    except (KeyError, TypeError, ValueError):
        raise RunRefused("sauvegarde altérée : contexte illisible") from None
    scope = Scope(tuple(d["chemin"].split("/", 1)[1] for d in documents), cutoff, natures,
                  accounts)
    return documents, scope, plan_sha


def prepare_restore(backup_path: Path) -> RestorePlan:
    backup_path = Path(backup_path)
    body, sha = rx._load_json(backup_path, "sauvegarde")
    if body.get("format") != BACKUP_FORMAT:
        raise RunRefused(f"sauvegarde d'un autre format ({body.get('format')!r}) — attendu "
                         f"{BACKUP_FORMAT}")
    if body.get("projet") != str(db.project):
        raise RunRefused(f"sauvegarde du projet « {body.get('projet')} », base « {db.project} »")
    documents, scope, plan_sha = _check_backup(body)
    plan = RestorePlan(path=backup_path, sha=sha, project=str(db.project),
                       created=str(body.get("cree_le")),
                       migration_fingerprint=str(body.get("empreinte")), scope=scope)
    world, _extra = read_all()
    plan.world = world
    states, current = [], {}
    for document in documents:
        ligne = document["ligne"]
        entry_id = document["chemin"].split("/", 1)[1]
        champs = document["champs"]
        snap = world.store.entries.get(entry_id)
        if snap is None:
            # The read is complete (``read_world`` raises otherwise): proven absent.
            plan.notes.append(f"{ligne} ({entry_id}) : supprimée depuis — rien à restaurer")
            states.append(RESTORE_DONE)
            continue
        doc = {**world.store.rows[entry_id], "id": entry_id}
        current[entry_id] = (snap, doc)
        saved = rla._untyped(document["avant"])
        before = dict(zip(SPLIT, split_of(saved)))
        after = dict(zip(SPLIT, split_of({**saved, **rla._untyped(document["apres"])})))
        now = dict(zip(SPLIT, split_of(doc)))
        trail = doc.get("revisions")
        last = trail[-1] if isinstance(trail, list) and trail else None
        by_the_plan = isinstance(last, dict) and last.get("source_sha256") == plan_sha
        at_after = all(now[k] == after[k] for k in champs)
        at_before = all(now[k] == before[k] for k in champs)
        length = rc._trail_length(doc)
        migrated = (doc.get("etag") == document["etag_migration"]
                    or _trail_entry(doc, plan_sha) is not None)
        tied = (doc.get("reverses_id") or doc.get("reversed_by_id")
                or entry_id in rc._named_by_a_correction(world.store)
                or doc.get("status") == "annulée" or doc.get("invoice_id")
                or doc.get("trust_transaction_id"))
        if not migrated:
            # A run that stopped before it: the migration never wrote it,
            # whatever happened to it since.
            plan.notes.append(f"{ligne} ({entry_id}) : jamais écrite par la migration — rien à "
                              f"restaurer")
            states.append(RESTORE_DONE)
        elif tied:
            plan.refusals.append(f"{ligne} : l'écriture {entry_id} a été contre-passée, annulée ou "
                                 f"liée depuis la migration — la restaurer romprait la paire ou "
                                 f"le lien : rien ne se restaure")
            states.append(None)
        elif at_after and (doc.get("etag") == document["etag_migration"] or by_the_plan):
            if length is None or length + 1 > al._REVISIONS_CAP:
                plan.refusals.append(f"{ligne} : le fil de révisions est illisible ou plein — "
                                     f"rien ne se rogne")
            states.append(RESTORE_PENDING)
        elif at_before:
            plan.notes.append(f"{ligne} ({entry_id}) : porte déjà la ventilation sauvegardée — "
                              f"rien à restaurer")
            states.append(RESTORE_DONE)
        else:
            plan.refusals.append(f"{ligne} : l'écriture {entry_id} ne porte ni la cible de la "
                                 f"migration ni la ventilation sauvegardée (etag "
                                 f"{doc.get('etag')}) : une écriture l'a suivie — rien ne se "
                                 f"restaure par-dessus")
            states.append(None)
        for k in champs:
            plan.rows.append((ligne, k, rx._v(now[k]), rx._v(before[k])))
    if not plan.refusals and states and set(states) == {RESTORE_DONE}:
        plan.state = RESTORE_DONE
    pairs = []
    if plan.state == RESTORE_PENDING and not plan.refusals:
        when = datetime.now(UTC)
        with provenance.writing_via("script"):
            for document, state in zip(documents, states):
                if state != RESTORE_PENDING:
                    continue
                entry_id = document["chemin"].split("/", 1)[1]
                snap, doc = current[entry_id]
                saved = dict(zip(SPLIT, split_of(rla._untyped(document["avant"]))))
                business = {k: saved[k] for k in document["champs"]}
                changes = {k: [doc.get(k), business[k]] for k in document["champs"]}
                trail = {"at": when, "via": provenance.current_via(),
                         "motif": RESTORE_MOTIF.format(quand=rla._when(plan.created)),
                         "changes": changes, "updated_at_before": doc.get("updated_at"),
                         "source_sha256": sha}
                plan.payloads[document["chemin"]] = {
                    **business, "revisions": list(doc.get("revisions") or []) + [trail],
                    **provenance.update_fields(when)}
                plan.writes.append(rla.Write(document["chemin"], TX, entry_id, 0,
                                             document["ligne"], snap, doc, business, changes))
                pairs.append((doc, {**doc, **business}))
    plan.before = compute_controls(world, scope)
    plan.simulated = compute_controls(world, scope, plan.payloads)
    if plan.writes:
        plan.expected = rla._attempt(expected_effects, pairs, world.store.control_rows(), scope)
        plan.gaps, plan.impossible = compare_controls(plan.before, plan.simulated, plan.expected)
    plan.fingerprint = _fingerprint({"sauvegarde_sha256": sha, "projet": plan.project},
                                    plan.writes, plan.payloads, [])
    return plan


# ── the report ────────────────────────────────────────────────────────────


def _account_name(world, account_id) -> str:
    return (world.store.accounts.get(account_id) or {}).get("name") or str(account_id)


def _window_text(statement: dict) -> str:
    return (f"brut {rla._money(statement['total_expenses'])} ; net "
            f"{rla._money(sum(statement['expenses_net'].values()))} ; TPS "
            f"{rla._money(statement['gst'])} ; TVQ {rla._money(statement['qst'])}")


def _render_controls(world, scope: Scope, before: dict, simulated: dict,
                     real: Optional[dict]) -> list:
    columns = ("Contrôle", "Avant", "Après (simulé)") + (("Après (réel)",) if real else ())

    def row(label, key, fn):
        cells = [label, rx._shown(rx._get(before, key), fn),
                 rx._shown(rx._get(simulated, key), fn)]
        if real:
            cells.append(rx._shown(rx._get(real, key), fn))
        return tuple(cells)

    def balance(account_id):
        def fn(c):
            slot = c["soldes"].get(account_id) or {}
            account = world.store.accounts.get(account_id) or {}
            shown = al.display_balance(account.get("account_type", ""),
                                       int(slot.get("ledger_balance") or 0))
            return (f"{rla._money(shown)} affiché (registre "
                    f"{rla._money(slot.get('ledger_balance'))}, Σ {rla._money(slot.get('somme'))})")
        return fn

    def ledger(c):
        return (f"{len(c['soldes'])} compte(s) ; {len(c['soldes_courants'])} soldes courants ; "
                f"{len(c['sequences'])} séquences")

    def lines(c):
        values = [v for v in c.values() if v is not None]
        taxed = sum(1 for v in values if v["ventilation"][1] or v["ventilation"][2])
        return (f"{len(values)} écriture(s), dont {taxed} avec TPS ou TVQ ; fils "
                f"{sum(v['longueur'] or 0 for v in values)} entrée(s)")

    def lock(c):
        parts = []
        for account_id in scope.account_ids:
            mine = c["conciliations"].get(account_id) or {}
            proofs = mine.get("preuves") or {}
            if not proofs and not mine.get("plancher"):
                continue
            bad = sum(1 for p in proofs.values() if p.get("ecart"))
            parts.append(f"{_account_name(world, account_id)} : plancher {mine.get('plancher')}, "
                         f"{len(proofs)} preuve(s), {bad} en écart")
        reasons = sorted({str(r or "aucun") for r in c["lock_reasons"].values()})
        return " ; ".join(parts) + f" ; verrous des écritures : {', '.join(reasons) or '—'}"

    rows = [row(f"1. Solde — {_account_name(world, a)}", "1", balance(a))
            for a in scope.account_ids]
    rows.append(row("1. Soldes courants, séquences", "1", ledger))
    rows.append(row("2. Les écritures de l'ensemble", "2", lines))
    for key, label in [("journal", "tous comptes")] + [
            (f"compte/{a}", _account_name(world, a)) for a in scope.account_ids]:
        for window, words in (("avant", f"avant le {scope.cutoff}"),
                              ("après", f"à partir du {scope.cutoff}")):
            rows.append(row(f"3. {label}, {words}", "3",
                            lambda c, k=key, w=window: _window_text(c[k][w])))
    rows.append(row("4. Écritures que la règle trouve encore", "4", lambda c: f"{len(c)}"))
    rows += [row("5. Verrou et conciliations", "5", lock),
             row("6. Autres documents lus", "6", lambda c: f"{len(c)} document(s)")]
    return rx._table(columns, rows)


_STATEMENT_LABELS = {
    "cti": "CTI (crédits de TPS)", "rti": "RTI (crédits de TVQ)", "couts": "Coûts (nets)",
    "benefice": "Bénéfice net", "tps_tvq_ouverture": "TPS/TVQ à remettre — ouverture",
    "tps_tvq_cloture": "TPS/TVQ à remettre — clôture",
    "avoir_net_cloture": "Avoir net — clôture", "tresorerie_cloture": "Trésorerie — clôture",
    "ecart_non_explique": "Écart non expliqué",
    "sans_ventilation": "Point « dépenses sans TPS ni TVQ ventilées » (nombre)",
}


def _render_statements(before, after, expected: dict, which: str) -> list:
    """Control 8: each period's figures, before and after, their move beside
    what the written entries alone imply."""
    if isinstance(before, Impossible) or isinstance(after, Impossible):
        reason = (before if isinstance(before, Impossible) else after).reason
        return [f"Impossible à calculer ({reason})."]
    out = []
    for slug, want in expected.items():
        b, a = before.get(slug), after.get(slug)
        out += ["", f"#### {periods.label_fr(periods._shape(slug))}", ""]
        if isinstance(b, Impossible) or isinstance(a, Impossible) or b is None or a is None:
            reason = next((x.reason for x in (b, a) if isinstance(x, Impossible)), "—")
            out.append(f"Indisponible ({reason}).")
            continue
        rows = []
        for key, label in _STATEMENT_LABELS.items():
            moved = a[key] - b[key]
            shown = str if key == "sans_ventilation" else rla._money
            rows.append((label, shown(b[key]), shown(a[key]), shown(moved), shown(want[key]),
                         rx._verdict(moved == want[key])))
        out += rx._table(("Figure", "Avant", f"Après ({which})", "Écart", "Attendu", ""), rows)
    return out


def _render_set(plan: Plan) -> list:
    world = plan.world
    rows = []
    for c in plan.candidates:
        doc = c.doc
        rows.append((c.label, f"`{c.entry_id}`", _account_name(world, doc.get("account_id")),
                     rx._v(rx._aware(doc.get("date"))), al.KIND_LABELS.get(doc.get("kind"),
                                                                           doc.get("kind")),
                     doc.get("counterparty") or "—",
                     al.ADMIN_CATEGORY_LABELS.get(doc.get("category"), doc.get("category") or "—"),
                     rla._money(doc.get("amount")), _shown_split(c.before),
                     _shown_split(c.target) if c.target else "—",
                     rae.equivalent_mode({**doc, **dict(zip(SPLIT, c.before))}),
                     c.state,
                     "; ".join(f"`{code}` — {message}" for code, message in c.reasons) or "—"))
    return rx._table(("L", "Écriture", "Compte", "Jour", "Nature", "Contrepartie", "Catégorie",
                      "Montant", "Ventilation avant", "Après", "Mode équivalent avant", "État",
                      "Motifs"), rows) if rows else ["Aucune écriture."]


def _render_outside(plan: Plan) -> list:
    """For memory: the taxed entries of the natures dated from the cut-off day
    to the end of its month — the rule leaves them —, and every taxed refund
    dated from the cut-off on."""
    world, spec = plan.world, plan.spec
    month_end = (date(spec.cutoff.year + (spec.cutoff.month == 12), spec.cutoff.month % 12 + 1, 1)
                 - timedelta(days=1))
    rows = []
    for eid, row in world.store.rows.items():
        doc = {**row, "id": eid}
        day = rla._day(doc.get("date"))
        if day is None or doc.get("kind") not in spec.natures:
            continue
        late_refund = (doc.get("kind") in al.REFUND_KINDS and day >= spec.cutoff
                       and _taxed(doc))
        if _taxed(doc) and (spec.cutoff <= day <= month_end or late_refund):
            rows.append((rx._v(rx._aware(doc.get("date"))), al.KIND_LABELS.get(doc.get("kind")),
                         doc.get("counterparty") or "—", rla._money(doc.get("amount")),
                         _shown_split(split_of(doc)),
                         "remboursement daté après la date : à examiner" if late_refund
                         else "à partir de la date : hors de la règle"))
        elif (day < spec.cutoff and not _taxed(doc) and _trail_entry(doc, plan.sha) is None
              and _split_changed_in_trail(doc)):
            rows.append((rx._v(rx._aware(doc.get("date"))), al.KIND_LABELS.get(doc.get("kind")),
                         doc.get("counterparty") or "—", rla._money(doc.get("amount")),
                         _shown_split(split_of(doc)),
                         "taxes retirées hors de ce plan (fil) : l'ensemble ne la compte plus"))
    rows.sort(key=lambda r: r[0])
    out = [f"La règle prend les jours strictement antérieurs au {spec.cutoff} (jour UTC) : une "
           f"écriture datée du {spec.cutoff} même garde ses taxes.", ""]
    out += (rx._table(("Jour", "Nature", "Contrepartie", "Montant", "Ventilation", "Note"), rows)
            if rows else ["Aucune."])
    earliest = [rla._day(c.doc.get("date")) for c in plan.candidates]
    early = sum(1 for d in earliest if d is not None and d < fs.BOOKS_START)
    out += ["", f"Écritures de l'ensemble antérieures au début des livres ({fs.BOOKS_START}) : "
                f"{early}."]
    return out


def _split_changed_in_trail(doc: dict) -> bool:
    """A trail entry that moved the entry's TPS or TVQ."""
    trail = doc.get("revisions")
    return isinstance(trail, list) and any(
        isinstance(item, dict) and isinstance(item.get("changes"), dict)
        and ({"gst_amount", "qst_amount"} & set(item["changes"])) for item in trail)


def _render_refunds(plan: Plan) -> list:
    """Each refund of the set beside the same supplier's purchases of the set."""
    refunds = [c for c in plan.candidates if c.doc.get("kind") in al.REFUND_KINDS]
    if not refunds:
        return ["Aucun remboursement dans l'ensemble."]
    out = []
    for r in refunds:
        payee = (r.doc.get("counterparty") or "").casefold()
        mates = [c for c in plan.candidates if c.doc.get("kind") == "dépense"
                 and (c.doc.get("counterparty") or "").casefold() == payee]
        out.append(f"- {r.label} : remboursement de {rla._money(r.doc.get('amount'))} du "
                   f"{rx._v(rx._aware(r.doc.get('date')))} ({r.doc.get('counterparty')}), "
                   f"ventilation {_shown_split(r.before)} → {_shown_split(r.target or ())} ; "
                   f"achats du même fournisseur dans l'ensemble : "
                   + (", ".join(f"{c.label} ({rx._v(rx._aware(c.doc.get('date')))}, "
                                f"{rla._money(c.doc.get('amount'))})" for c in mates) or "aucun"))
    return out


def _render_trails(plan: Plan) -> list:
    out = []
    for c in plan.candidates:
        trail = c.doc.get("revisions")
        if not trail:
            continue
        if not isinstance(trail, list):
            out += [f"### {c.label} — `{c.entry_id}` : fil illisible", ""]
            continue
        out += [f"### {c.label} — `{c.entry_id}`, {len(trail)} révision(s)", ""]
        out += rx._render_trail(c.doc)
        out.append("")
    return out or ["Aucune écriture de l'ensemble ne porte de révision."]


def _render_trace(plan: Plan, backup_dir: Optional[Path], report: Optional[Path],
                  backup: Optional[tuple]) -> list:
    if backup:
        where = f"`{backup[0]}` (SHA-256 `{backup[1]}`)"
    elif backup_dir is not None:
        where = (f"dans `{backup_dir}`, avant la première transaction : "
                 f"`taxes-avant-inscription-<horodatage UTC>-{plan.fingerprint}.json`")
    else:
        where = "dans le dossier que nommera `--sauvegarde`, avant la première transaction"
    return [f"- Sauvegarde : {where} — chaque écriture à écrire, entière et typée ; relue et "
            f"vérifiée avant toute écriture ; restaurable par `--restaurer`.",
            "- Trace : ce rapport" + (f" (`{report}`)" if report else "")
            + " et ceux de l'application, de la relance et de la restauration à blanc, à "
              "côté ; chaque écriture écrite garde au fil `revisions` une entrée dont "
              f"`source_sha256` est le SHA-256 du plan (`{plan.sha}`)."]


def render_report(plan: Plan, *, mode: str, approved: Optional[str] = None,
                  refusal: Optional[list] = None, outcome: Optional[str] = None,
                  backup: Optional[tuple] = None, real: Optional[dict] = None,
                  real_statements=None, real_statements_expected: Optional[dict] = None,
                  drift: Optional[list] = None,
                  real_gaps: Optional[list] = None, backup_dir: Optional[Path] = None,
                  report: Optional[Path] = None) -> str:
    world, spec = plan.world, plan.spec
    out = [
        "# Taxes des achats antérieurs à l'inscription aux fichiers de la TPS et de la TVQ",
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
    out += ["", f"Règle : nature parmi {', '.join(f'« {k} »' for k in spec.natures)}, jour UTC "
                f"strictement antérieur au **{spec.cutoff}**, TPS ou TVQ non nulle → net = "
                f"montant, TPS = 0, TVQ = 0.", "", f"Motif du fil : « {spec.motif} »"]
    if spec.note:
        out += ["", f"Note du plan : {spec.note}"]
    out += ["", "## État", "",
            (f"- Ensemble : {len(plan.candidates)} écriture(s) — {plan.count(PENDING)} à "
             f"appliquer, {plan.count(DONE)} déjà appliquée(s), {plan.count(REFUSED)} "
             f"refusée(s) ; écritures à écrire : {len(plan.writes)}.")]
    blocking = plan.blocking + list(refusal or [])
    if blocking:
        out.append("- **L'application est refusée** :")
        out += [f"  - {item}" for item in dict.fromkeys(blocking)]
    elif plan.writes:
        out.append("- Rien ne s'oppose à l'application : l'ensemble est celui que le plan "
                   "énonce, et les contrôles 1 à 8, simulés, sont conformes.")
    if outcome:
        out += ["", "## Issue", "", outcome]
    if backup:
        out += ["", f"- Sauvegarde : `{backup[0]}`", f"- SHA-256 de la sauvegarde : `{backup[1]}`"]
    if drift is not None:
        out.append("- Écart entre l'après réel et l'après simulé : "
                   + ("aucun" if not drift else "; ".join(drift)))
    if real_gaps is not None:
        out.append("- Contrôles après réel : " + ("conformes" if not real_gaps
                                                  else "; ".join(real_gaps)))

    out += ["", "## 1. L'ensemble", ""]
    out += _render_set(plan)
    out += ["", "## 2. Contrôle 7 — ce que le plan énonce, et ce que porte la base", ""]
    out += rx._table(("Nature", "Mesure", "Plan", "Base", ""),
                     [(n, m, p, b, rx._verdict(ok)) for n, m, p, b, ok in plan.stated_rows])
    out += ["", "## 3. Hors de la règle, pour mémoire", ""]
    out += _render_outside(plan)
    out += ["", "## 4. Remboursements", ""]
    out += _render_refunds(plan)
    out += ["", "## 5. Changements prévus", ""]
    if plan.writes:
        rows = []
        for write in plan.writes:
            payload = plan.payloads[write.path]
            revisions = payload.get("revisions") or []
            rows.append((write.role, f"`{write.doc_id}`",
                         "; ".join(f"`{k}` {rx._v(old)} → {rx._v(new)}"
                                   for k, (old, new) in write.changes.items()),
                         f"{len(write.before.get('revisions') or [])} → {len(revisions)}"))
        out += rx._table(("L", "Écriture", "Champs", "Fil"), rows)
        out += ["", ("Estampilles : `updated_at`, `etag`, `updated_via` = `script` ; une entrée "
                     "de fil par écriture, motif du plan, via `script`. Aucun compte, aucune "
                     "conciliation, aucune facture, aucun débours, aucune autre écriture n'est "
                     "écrit ; une transaction par écriture.")]
    else:
        out.append("Aucun.")
    out += ["", "## 6. Champs employés", "",
            ("- Ventilation : `net_amount`, `gst_amount`, `qst_amount` (cents) ; montant brut "
             "`amount`, jamais écrit. Le modèle ne stocke aucun mode de ventilation : « net = "
             "montant, sans taxe » est le « sans_taxe » de `record_admin_entry`."),
            ("- Provenance : `script` — `updated_via` et `via` du fil, sous "
             "`provenance.writing_via` ; `updated_at` et un nouvel `etag` ; une entrée `{at, "
             "via, motif, changes, updated_at_before, source_sha256}`, `changes` donnant "
             "`[avant, après]` par champ."),
            ("- Hors du modèle : `update_transaction` accepterait une écriture non verrouillée, "
             "mais son fil ne porte ni motif ni empreinte du plan, il rogne le fil à 25, "
             "réécrit tout le document et estampille le compte à chaque modification ; il "
             "refuse une écriture verrouillée. La cible est jugée par `validate_ventilation` "
             "et `_validate_business`.")]
    out += ["", "## 7. Fils de révisions existants", ""]
    out += _render_trails(plan)
    out += ["", "## 8. Verrou et conciliations", ""]
    lock_before = rx._get(plan.before, "5")
    reasons = rx._shown(lock_before, lambda c: ", ".join(
        f"{r} ({n})" for r, n in sorted(
            {str(x or "aucun"): list(c["lock_reasons"].values()).count(x)
             for x in c["lock_reasons"].values()}.items())))
    out.append(f"- Verrou des écritures de l'ensemble (dérivé à la lecture, jamais stocké) : "
               f"{reasons} ; il doit rester le même.")
    cited = sorted({r for c in plan.candidates for r in plan.citing.get(c.entry_id) or []})
    out.append("- Conciliations qui citent une écriture de l'ensemble : "
               + (", ".join(cited) or "aucune") + ".")
    if plan.reconciliation_keys:
        out.append("- **Champs des conciliations hors de la forme du modèle** : "
                   + ", ".join(f"`{k}`" for k in plan.reconciliation_keys) + ".")
    else:
        out.append("- Une conciliation garde des montants bruts et les identifiants cochés : "
                   "aucune ne consigne de ventilation. Champs hors de la forme du modèle : "
                   "aucun.")
    out += ["", "## 9. Contrôles 1 à 6", ""]
    out += _render_controls(world, plan.scope, plan.before, plan.simulated, real)
    out += ["", "Écarts (simulé) : " + ("aucun" if not (plan.gaps or plan.impossible)
                                        else "; ".join(plan.gaps + plan.impossible))]
    if real_gaps is not None:
        out.append("Écarts (réel) : " + ("aucun" if not real_gaps else "; ".join(real_gaps)))
    out += ["", "## 10. Effet sur les états financiers (contrôle 8)", "",
            ("Calculé par `models/financial_statements.compute` sur la lecture du script ; la "
             "colonne « Attendu » est ce que les seules écritures écrites impliquent, et un écart "
             "arrête la course comme ceux des contrôles 1 à 6. Le point « "
             "Dépenses sans TPS ni TVQ ventilées » liste désormais les dépenses corrigées : "
             "c'est voulu (aucun crédit n'y est réclamable).")]
    after_statements = real_statements if real_statements is not None else plan.statements_simulated
    expected = (real_statements_expected if real_statements_expected is not None
                else plan.statements_expected)
    verdict = statements_verdict(plan.statements_before, after_statements, expected)
    out += ["", "Contrôle 8 : " + ("conforme" if not verdict else "; ".join(verdict)) + "."]
    out += _render_statements(plan.statements_before, after_statements, expected,
                              "réel" if real_statements is not None else "simulé")
    out += ["", "## 11. Caches", "",
            ("Aucun à recalculer : les totaux de TPS, de TVQ, de coûts et les états se calculent "
             "à la lecture ; le seul solde dénormalisé, `admin_accounts.ledger_balance`, ne "
             "bouge pas (aucun montant ne change).")]
    out += ["", "## 12. Sauvegarde et trace", ""]
    out += _render_trace(plan, backup_dir, report, backup)
    out += ["", "## 13. Suite", ""]
    if outcome and backup:
        out.append("Relancer les deux contrôles d'intégrité (`scripts.verify_admin_integrity`, "
                   "`scripts.verify_trust_integrity`) et comparer leurs constats par texte ; "
                   "relancer à blanc : tout doit être « déjà appliquée ».")
    elif blocking:
        out.append("Rien n'a été écrit. Lever le motif du refus (ci-dessus), sans plier le plan à "
                   "la base, puis relancer à blanc.")
    elif not plan.writes:
        out.append("Rien à écrire.")
    else:
        out.append(f"Après approbation : `--sauvegarde DOSSIER --appliquer {plan.fingerprint}` ; "
                   f"puis les deux contrôles d'intégrité, comparés par texte.")
    return "\n".join(out) + "\n"


def render_restore_report(plan: RestorePlan, *, mode: str, approved: Optional[str] = None,
                          refusal: Optional[list] = None, outcome: Optional[str] = None,
                          real: Optional[dict] = None, drift: Optional[list] = None,
                          real_gaps: Optional[list] = None) -> str:
    out = [
        "# Restauration des taxes des achats antérieurs à l'inscription",
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
    if real_gaps is not None:
        out.append("- Contrôles après réel : " + ("conformes" if not real_gaps
                                                  else "; ".join(real_gaps)))
    if plan.notes:
        out += ["", "Rien à restaurer pour :"] + [f"- {note}" for note in plan.notes]
    out += ["", "## Champs", ""]
    out += rx._table(("L", "Champ", "Actuel", "Restauré"), plan.rows) if plan.rows else ["—"]
    if plan.scope is not None and plan.world is not None:
        out += ["", "## Contrôles", ""]
        out += _render_controls(plan.world, plan.scope, plan.before, plan.simulated, real)
        out += ["", "Écarts (simulé) : " + ("aucun" if not (plan.gaps or plan.impossible)
                                            else "; ".join(plan.gaps + plan.impossible))]
        if real_gaps is not None:
            out.append("Écarts (réel) : " + ("aucun" if not real_gaps else "; ".join(real_gaps)))
    return "\n".join(out) + "\n"


# ── the command line ──────────────────────────────────────────────────────


def _summary(plan: Plan, mode: str) -> list:
    return [
        f"Taxes avant l'inscription — {mode}",
        f"Projet : {plan.project}",
        f"Plan : {plan.path} (SHA-256 {plan.sha})",
        (f"Ensemble : {len(plan.candidates)} écriture(s) — {plan.count(PENDING)} à appliquer, "
         f"{plan.count(DONE)} déjà appliquée(s), {plan.count(REFUSED)} refusée(s) ; écritures "
         f"à écrire : {len(plan.writes)}"),
        "Contrôles 1 à 8, simulés : " + ("conformes" if not plan.blocking else
                                         f"{len(plan.blocking)} motif(s) de refus"),
        f"Empreinte : {plan.fingerprint}",
    ]


def _run_migration(report: Path, source: Path, backup_dir: Optional[Path],
                   approved: Optional[str]) -> int:
    try:
        plan = prepare(source)
    except RunRefused as exc:
        return rla._finish(report, f"# Taxes avant l'inscription\n\n- Date : {rla._now_line()}\n"
                                   f"- Plan : `{source}`\n\n**Refusé — rien n'a été écrit** : "
                                   f"{exc}\n", [f"Refusé — rien n'a été écrit : {exc}"], 1)
    if approved is None:
        summary = _summary(plan, "à blanc")
        text = render_report(plan, mode="à blanc", backup_dir=backup_dir, report=report)
        if plan.blocking:
            summary.append(f"L'application serait refusée ({len(plan.blocking)} motif(s), voir "
                           f"le rapport). Rien n'a été écrit.")
            return rla._finish(report, text, summary, 1)
        if plan.writes:
            summary.append("À blanc : rien n'a été écrit. Pour écrire, après approbation : "
                           f"--sauvegarde DOSSIER --appliquer {plan.fingerprint}")
        else:
            summary.append("À blanc : rien n'a été écrit, et rien n'est à écrire.")
        return rla._finish(report, text, summary, 0)
    refusal = []
    if approved != plan.fingerprint:
        refusal.append(f"l'empreinte a changé depuis la passe approuvée ({approved} → "
                       f"{plan.fingerprint}) : relancez à blanc et faites-la approuver")
    refusal += plan.blocking
    if refusal:
        summary = _summary(plan, "application refusée")
        summary += [f"Refusé — rien n'a été écrit : {r}" for r in dict.fromkeys(refusal)]
        return rla._finish(report, render_report(plan, mode="application refusée",
                                                 approved=approved, refusal=refusal,
                                                 backup_dir=backup_dir, report=report),
                           summary, 1)
    if not plan.writes:
        summary = _summary(plan, "application") + ["Rien à écrire."]
        return rla._finish(report, render_report(plan, mode="application", approved=approved,
                                                 outcome="Rien à écrire.", report=report),
                           summary, 0)
    try:
        backup = write_backup(backup_dir, plan)
    except Exception as exc:
        message = f"sauvegarde impossible ({type(exc).__name__}) — rien n'a été écrit"
        return rla._finish(report, render_report(plan, mode="application refusée",
                                                 approved=approved, refusal=[message],
                                                 report=report),
                           _summary(plan, "application refusée") + [f"Refusé : {message}"], 1)
    rla._out(f"Sauvegarde : {backup[0]}")
    outcomes = apply_all(plan.writes, plan.payloads)
    sentence = rae._outcome_sentence(outcomes, len(plan.writes))
    rla._out(sentence)
    summary = _summary(plan, "application") + [f"Sauvegarde : {backup[0]}"]
    real = real_statements = real_statements_expected = drift = real_gaps = None
    if outcomes:
        after = _post_write(plan.scope, plan.before, plan.simulated, plan.expected, outcomes,
                            plan.world.store.control_rows(),
                            statements_before=plan.statements_before, spec=plan.spec,
                            sha=plan.sha)
        real, real_statements, real_gaps, drift = after.real, after.figures, after.gaps, after.drift
        real_statements_expected = after.statements_expected
        summary.append("Contrôles après réel : " + ("conformes" if not real_gaps
                                                    else f"{len(real_gaps)} écart(s)"))
        summary.append("Écart à la passe approuvée : " + (
            "non comparé (application partielle)" if drift is None
            else "aucun" if not drift else f"{len(drift)} différence(s)"))
        summary.append("Relancez les deux contrôles d'intégrité (scripts.verify_admin_integrity, "
                       "scripts.verify_trust_integrity) et comparez leurs constats par texte.")
    detail = "\n".join(f"- {w.role} — `{w.doc_id}` : {o}" + (f" ({d})" if d else "")
                       for w, o, d in outcomes)
    text = render_report(plan, mode="application", approved=approved,
                         outcome=sentence + "\n\n" + detail, backup=backup, real=real,
                         real_statements=real_statements,
                         real_statements_expected=real_statements_expected, drift=drift,
                         real_gaps=real_gaps, report=report)
    done = all(o in (WRITTEN, LOST_BUT_WRITTEN) for _w, o, _d in outcomes) and len(
        outcomes) == len(plan.writes)
    return rla._finish(report, text, summary, 0 if done and not real_gaps else 1)


def _run_restore(report: Path, source: Path, approved: Optional[str]) -> int:
    try:
        plan = prepare_restore(source)
    except RunRefused as exc:
        return rla._finish(report, f"# Restauration\n\n- Date : {rla._now_line()}\n"
                                   f"- Sauvegarde : `{source}`\n\n**Refusé — rien n'a été "
                                   f"écrit** : {exc}\n",
                           [f"Refusé — rien n'a été écrit : {exc}"], 1)
    head = [f"Restauration — {'à blanc' if approved is None else 'application'}",
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
    outcomes = apply_all(plan.writes, plan.payloads, zeroing=False)
    sentence = rae._outcome_sentence(outcomes, len(plan.writes))
    rla._out(sentence)
    real = drift = real_gaps = None
    if outcomes:
        after = _post_write(plan.scope, plan.before, plan.simulated, plan.expected, outcomes,
                            plan.world.store.control_rows())
        real, real_gaps, drift = after.real, after.gaps, after.drift
        head.append("Contrôles après réel : " + ("conformes" if not real_gaps
                                                 else f"{len(real_gaps)} écart(s)"))
    text = render_restore_report(plan, mode="restauration", approved=approved,
                                 outcome=sentence, real=real, drift=drift, real_gaps=real_gaps)
    done = all(o in (WRITTEN, LOST_BUT_WRITTEN) for _w, o, _d in outcomes) and len(
        outcomes) == len(plan.writes)
    return rla._finish(report, text, head, 0 if done and not real_gaps else 1)


def main(argv: Optional[list] = None) -> int:
    parser = rla._Parser(prog="python -m scripts.rectify_pre_registration_taxes",
                         description="Taxes des achats antérieurs à l'inscription aux fichiers "
                                     "de la TPS et de la TVQ (hors modèle, à blanc par défaut).")
    parser.add_argument("plan", nargs="?", help="le plan JSON approuvé (hors du dépôt public)")
    parser.add_argument("--rapport", metavar="RAPPORT.md",
                        help="le rapport à écrire (hors du dépôt ; nouveau fichier)")
    parser.add_argument("--sauvegarde", metavar="DOSSIER",
                        help="le dossier où écrire la sauvegarde avant la première transaction "
                             "(hors du dépôt)")
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
