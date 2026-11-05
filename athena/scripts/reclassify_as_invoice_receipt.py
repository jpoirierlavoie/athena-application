"""Reclassify ONE administration entry as the receipt of an invoice — a
one-shot migration that writes OUTSIDE the models.

What it does
------------
A client's payment on an invoice was booked in the administration journal as
another recette: the lawyer's own contribution (« apport »), an « autre
recette » or an incoming internal transfer. The entry is locked for the model
(compensée, behind a completed reconciliation), and an invoice link is never
editable anyway (``admin_ledger._entry_lock_reason``): the application's
correction is a reversal and a new entry, which the lawyer declined. This
script turns the entry into the ``encaissement_facture`` the application
would have recorded, and carries its amount onto the invoice, from a PLAN the
lawyer approved, kept outside the public repository (it names a client, an
invoice and the reason).

Why it writes outside the models
--------------------------------
CLAUDE.md, « One door per rule »: this script is a declared exception, like
``scripts/reclassify_admin_ledger`` — whose pure helpers it imports, leaving
it and its tests untouched — and it closes with its plan. It writes, in ONE
batch, what ``admin_ledger._stage_create`` writes for an encaissement, and
nothing else:

* on the entry, among ``kind``, ``invoice_id``, ``invoice_number``,
  ``dossier_id``, ``dossier_file_number``, ``dossier_title``,
  ``counterparty`` and ``description``, those whose value changes — the
  invoice number and the three dossier fields taken from the INVOICE, as the
  create path takes them; plus the house stamps (``updated_at``, a new
  ``etag``, ``updated_via = "script"``, under ``provenance.writing_via``) and
  ONE ``revisions`` trail entry, the shape of the October migrations:
  ``{at, via, motif, changes, updated_at_before, source_sha256}``. Never an
  amount, a sign, an account, a date, a status, a sequence, a tax split, a
  trust or reversal link: the bank movement is a fact, only its nature was
  misjudged;
* on the invoice, what ``invoice.payment_updates`` — the ONE computation of
  ``amount_paid``, ``paid_date`` and the status flip — returns for
  ``amount_paid + amount``, reduced to the fields that change, with its
  stamps. ``paid_date`` is the plan's choice between two values only: the
  stored one (the invoice's latest payment) or the entry's day (what the
  create path writes — it assumes payments are recorded in order);
* the account is not written: no money moves (``ledger_balance`` stays
  Σ ``admin_delta``), and no reconciliation is touched — they keep totals and
  the ids they cleared, never a kind or a counterparty;
* the registers and the invoices are not DAV-exposed: no CTag to bump.

The plan
--------
A JSON object (UTF-8), outside the repository::

    {"motif": "…",                       # the trail's motif, verbatim
     "note": "…",                        # optional, for the reader
     "compte": {"id": "…", "plancher": "AAAA-MM-JJ", "solde_cents": 0},
     "ecriture": {"id": "…", "attendu": {…}, "cible": {…}},
     "facture": {"id": "…", "attendu": {…}, "cible": {…}},
     "intouchables": [{"chemin": "collection/id", "etag": "…"}],
     "encours": {"avant": 0, "apres": 0},
     "periode": {"du": "AAAA-MM-JJ", "au": "AAAA-MM-JJ"}}

``attendu`` states every stored value the run expects (:data:`ENTRY_EXPECTED`,
:data:`INVOICE_EXPECTED`): a date-only field as its day, ``created_at`` as an
ISO 8601 instant with its offset, ``revisions`` as the trail's length,
``null`` for an absent or null value. ``cible`` names the targets
(:data:`ENTRY_TARGETS`, :data:`INVOICE_TARGETS`). Any difference between
``attendu`` and the base refuses the run: the plan is never bent to fit the
base.

Judging — every read is strict: a failed read stops the run, it never passes
for an absence
-----------------------------------------------------------------------------
* the guards of an encaissement (``_stage_create``): an « opérations »
  account, open; an issued invoice; the amount within the LIVE balance; and
  ``payment_updates`` must accept the payment;
* the entry: a simple recette of :data:`SOURCE_KINDS`, linked to no invoice,
  no trust entry and no reversal, not annulée, with no category and the zero
  split of a recette, and a trail with room for this write and one
  restoration;
* the targets: the invoice's own number and dossier snapshots; a
  counterparty and a description that ``security.sanitize`` leaves
  unaltered; on the invoice, the amount paid plus the entry's amount, the
  status the model computes, and one of the two ``paid_date`` values above.

A run that finds the entry at its targets with this plan's trail entry (same
``source_sha256``) reports « déjà appliquée » and writes nothing; part done
and part not is refused.

The controls
------------
Computed on complete reads — every administration entry, account and
reconciliation, every invoice, the trust register — BEFORE, AFTER SIMULATED
(the documents read, merged with the exact payloads) and, once written, AFTER
(re-read):

1. each account's stored ``ledger_balance`` and Σ ``admin_delta``, every
   entry's running balance (``admin_ledger.running_balances`` in ``(day,
   sequence)`` order) and sequence — unchanged;
2. the entry's amount, sign, day, clearing day, status, sequence and
   account — unchanged;
3. ``admin_ledger.results_statement`` and ``owner_equity`` over the plan's
   period and over the whole journal — moved by exactly what the same two
   functions say of the entry alone, before and after;
4. the invoice's amount paid, balance (``invoice.balance_of``), status,
   amounts and line items; Σ of its standing receipts (check 8 of
   ``scripts/verify_admin_integrity``), equal to the amount paid once
   written; and the firm's outstanding total (Σ ``balance_of`` over the
   issued invoices) — moved by minus the amount;
5. each account's lock floor and last reconciliation, and the re-proof of
   every completed reconciliation — unchanged; the entry's ``lock_reason``
   (derived at read, never stored) is reported before and after;
6. every other document read — entries, accounts, reconciliations,
   invoices, trust entries — unchanged (a digest per document);

plus check 10 of ``scripts/verify_trust_integrity``: every fee payment whose
candidate manual recettes change is listed. A control that cannot be
computed, or that moves otherwise, refuses ``--appliquer``.

Fingerprint, backup, restoration
--------------------------------
The fingerprint is the SHA-256 (its first 16 hex characters) of the plan's
digest, ``db.project`` and every planned write (path, the ``update_time``
read, the business fields written). ``--appliquer`` takes the fingerprint of
the approved dry run: anything that moved since changes it. Before the
batch, a typed JSON (instants as ``{"$ts": iso}``) of both documents —
whole, with the ``update_time`` read and the etag the migration writes — is
written in ``--sauvegarde``, fsynced, read back and verified. Each document
of the batch is guarded by the ``update_time`` it was read at; a commit
whose answer is lost is re-read and reported written, not written, or
uncertain. ``--restaurer`` (dry by default, its own fingerprint) puts the
saved fields back in one batch, with fresh stamps and a trail entry
« Restauration de la sauvegarde du … », and refuses everything when either
document's etag is no longer the migration's.

Running it — from ``athena/``, with ADC and the environment of « Active
gabarits » in DEPLOYMENT.md: even the dry run reads PRODUCTION, and the
report names ``db.project``. A plan, report, backup or backup directory
inside the repository is refused before anything is created; the report
must be a new file.

    python -m scripts.reclassify_as_invoice_receipt PLAN.json --rapport R.md
    python -m scripts.reclassify_as_invoice_receipt PLAN.json --rapport R.md \\
        --sauvegarde DOSSIER --appliquer EMPREINTE
    python -m scripts.reclassify_as_invoice_receipt --restaurer SAUVEGARDE.json \\
        --rapport R.md [--appliquer EMPREINTE]

Afterwards, run both integrity scripts and compare their findings BY TEXT.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from models import admin_ledger as al
from models import db, provenance, trust
from models import invoice as invoice_model
from scripts import reclassify_admin_ledger as rla
from scripts import verify_trust_integrity as vti
from scripts.rectify_registers import (
    PlanRefused,
    _addressable,
    _check_text,
    _parse_day,
    _parse_instant,
)

UTC = timezone.utc
TX = al.TRANSACTIONS_COLLECTION
ACCOUNTS = al.ACCOUNTS_COLLECTION
RECS = al.RECONCILIATIONS_COLLECTION
INVOICES = invoice_model.COLLECTION
TRUST_TX = trust.TRANSACTIONS_COLLECTION
TARGET_KIND = "encaissement_facture"
#: The kinds an entry may come from: the simple recettes other than an
#: encaissement — derived from the model's own map of implied signs.
SOURCE_KINDS = tuple(k for k, d in al._KIND_DIRECTION.items()
                     if d == "recette" and k != TARGET_KIND)
#: Every stored value of the entry the plan must state (« attendu »).
ENTRY_EXPECTED = (
    "account_id", "sequence", "date", "cleared_date", "kind", "direction", "method",
    "amount", "net_amount", "gst_amount", "qst_amount", "category", "counterparty",
    "description", "reference", "supplier_invoice_ref", "dossier_id",
    "dossier_file_number", "dossier_title", "invoice_id", "invoice_number", "status",
    "trust_transaction_id", "reverses_id", "reversed_by_id", "related_transaction_id",
    "reconciliation_id", "created_at", "created_via", "cleared_via", "revisions", "etag",
)
#: Every stored value of the invoice the plan must state.
INVOICE_EXPECTED = (
    "invoice_number", "date", "due_date", "dossier_id", "dossier_file_number",
    "dossier_title", "client_id", "client_name", "subtotal_fees", "subtotal_expenses",
    "subtotal", "gst_amount", "qst_amount", "total", "retainer_applied", "amount_due",
    "amount_paid", "paid_date", "status", "etag",
)
#: What a write may change — the fingerprint and the backup keep these;
#: everything else in a payload is a stamp or the trail.
ENTRY_TARGETS = ("kind", "invoice_id", "invoice_number", "dossier_id",
                 "dossier_file_number", "dossier_title", "counterparty", "description")
INVOICE_TARGETS = ("amount_paid", "paid_date", "status")
#: The invoice fields an encaissement copies onto its entry (``_stage_create``).
_FROM_INVOICE = ("invoice_number", "dossier_id", "dossier_file_number", "dossier_title")
_DAY_FIELDS = ("date", "cleared_date", "due_date", "paid_date")
_INSTANT_FIELDS = ("created_at",)
#: The entry's facts no write may move (control 2).
_ENTRY_FACTS = ("amount", "direction", "date", "cleared_date", "status", "sequence", "account_id")
#: The invoice amounts no write may move (control 4).
_INVOICE_AMOUNTS = ("subtotal_fees", "subtotal_expenses", "subtotal", "gst_amount",
                    "qst_amount", "total", "retainer_applied", "amount_due")
#: The keys a reconciliation holds (``admin_ledger.create_reconciliation`` and
#: the stamps): none of them names an entry's kind or counterparty.
_RECONCILIATION_KEYS = frozenset((
    "id", "account_id", "period_end", "statement_balance", "book_balance",
    "outstanding_total", "in_transit_total", "variance", "status", "completed_date",
    "cleared_transaction_ids", "notes", "created_at", "created_via", "updated_at",
    "updated_via", "etag", "mcp_updated_at",
))
#: The collections an untouchable document may live in (the run reads them whole).
_READ_COLLECTIONS = (TX, ACCOUNTS, RECS, INVOICES, TRUST_TX)
#: A written entry keeps room for this write AND one restoration.
MAX_TRAIL_BEFORE = al._REVISIONS_CAP - 2
TEXT_MAX = 2000
FINGERPRINT_HEX = 16
BACKUP_FORMAT = "athena.encaissement-reclasse/1"
RESTORE_MOTIF = "Restauration de la sauvegarde du {quand}"
PENDING, DONE = "à appliquer", "déjà appliquée"
RESTORE_PENDING, RESTORE_DONE = "à restaurer", "déjà restaurée"
_HEX16 = re.compile(r"[0-9a-f]{16}")

RunRefused = rla.RunRefused
Impossible = rla.Impossible
ControlImpossible = rla.ControlImpossible


# ── values ────────────────────────────────────────────────────────────────


def _aware(value):
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _v(value) -> str:
    """A value as the report shows it — ``null`` and « » kept apart, a day
    at midnight UTC as its day, any other instant in full."""
    if value is None:
        return "null"
    if isinstance(value, datetime):
        instant = _aware(value).astimezone(UTC)
        if (instant.hour, instant.minute, instant.second, instant.microsecond) == (0, 0, 0, 0):
            return instant.date().isoformat()
        return instant.isoformat()
    if isinstance(value, str):
        return f"« {value} »"
    return str(value)


def _base_value(name: str, doc: dict):
    """A stored value in the plan's terms: ``revisions`` as its length."""
    if name == "revisions":
        return len(doc.get("revisions") or [])
    return _aware(doc.get(name))


def _plan_value(name: str, value, where: str):
    """A plan value in the base's terms: a day as midnight UTC, an instant
    as an aware datetime, ``revisions`` as a count."""
    if name in _DAY_FIELDS:
        if value is None:
            return None
        day = _parse_day(value)
        if day is None:
            raise RunRefused(f"plan mal formé : « {where} » doit être un jour AAAA-MM-JJ ou null")
        return day
    if name in _INSTANT_FIELDS:
        if value is None:
            return None
        instant = _parse_instant(value)
        if instant is None:
            raise RunRefused(f"plan mal formé : « {where} » doit être un instant ISO 8601 "
                             f"avec son fuseau, ou null")
        return instant
    if name == "revisions":
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RunRefused(f"plan mal formé : « {where} » doit être un entier positif ou nul")
        return value
    if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int))):
        raise RunRefused(f"plan mal formé : « {where} » : un texte, un entier ou null est attendu")
    return value


def _cents(value, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RunRefused(f"plan mal formé : « {where} » doit être un montant entier en cents")
    return value


def _day_of(value, what: str) -> date:
    day = rla._day(value)
    if day is None:
        raise ControlImpossible(f"date illisible ({what})")
    return day


# ── the plan file ─────────────────────────────────────────────────────────


@dataclass
class Spec:
    """The plan, its form checked — nothing read yet."""

    motif: str
    note: str
    account_id: str
    floor: Optional[date]
    balance: int
    entry_id: str
    entry_expected: dict     # name → value in the base's terms
    entry_target: dict
    invoice_id: str
    invoice_expected: dict
    invoice_target: dict     # paid_date in the base's terms
    untouchables: list       # [(path, etag)]
    outstanding: tuple       # (before, after), cents
    period: tuple            # (first day, last day)


def _object(parent: dict, key: str, where: str) -> dict:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise RunRefused(f"plan mal formé : « {where}{key} » doit être un objet")
    return value


def _exact_keys(values: dict, names: tuple, where: str) -> dict:
    missing = [n for n in names if n not in values]
    extra = sorted(set(values) - set(names), key=str)
    if missing or extra:
        parts = ([f"manque {', '.join(missing)}"] if missing else []) + (
            [f"inconnu {', '.join(map(str, extra))}"] if extra else [])
        raise RunRefused(f"plan mal formé : « {where} » — {' ; '.join(parts)}")
    return values


def _doc_id(section: dict, where: str) -> str:
    value = section.get("id")
    if not _addressable(value):
        raise RunRefused(f"plan mal formé : « {where}.id » n'est pas un identifiant")
    return value


def _load_json(path: Path, what: str) -> tuple[dict, str]:
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RunRefused(f"{what} illisible ({type(exc).__name__}) : {path}") from None
    if not isinstance(data, dict):
        raise RunRefused(f"{what} mal formé : un objet JSON est attendu")
    return data, hashlib.sha256(raw).hexdigest()


def _parse_untouchables(raw_list) -> list:
    """``[(path, etag)]`` of the plan's « intouchables » — documents the run
    must find exactly as the plan states them, and leave so."""
    if not isinstance(raw_list, list):
        raise RunRefused("plan mal formé : « intouchables » doit être une liste")
    untouchables = []
    for i, item in enumerate(raw_list):
        where = f"intouchables[{i}]"
        if not isinstance(item, dict):
            raise RunRefused(f"plan mal formé : « {where} » doit être un objet")
        _exact_keys(item, ("chemin", "etag"), where)
        path, etag = item["chemin"], item["etag"]
        parts = path.split("/") if isinstance(path, str) else []
        if len(parts) != 2 or parts[0] not in _READ_COLLECTIONS or not _addressable(parts[1]):
            raise RunRefused(f"plan mal formé : « {where}.chemin » doit être « collection/id » "
                             f"dans {', '.join(_READ_COLLECTIONS)}")
        if not isinstance(etag, str) or not etag:
            raise RunRefused(f"plan mal formé : « {where}.etag » doit être un texte non vide")
        untouchables.append((path, etag))
    return untouchables


def parse_plan(data: dict) -> Spec:
    """The plan's form, judged before any read — any defect refuses."""
    _exact_keys({k: v for k, v in data.items() if k != "note"},
                ("motif", "compte", "ecriture", "facture", "intouchables", "encours", "periode"),
                "plan")
    motif = data.get("motif")
    if not isinstance(motif, str) or not motif.strip() or motif != motif.strip():
        raise RunRefused("plan mal formé : « motif » doit être un texte non vide, sans "
                         "espace en bordure")
    note = data.get("note", "")
    if not isinstance(note, str):
        raise RunRefused("plan mal formé : « note » doit être un texte")

    compte = _exact_keys(_object(data, "compte", ""), ("id", "plancher", "solde_cents"), "compte")
    floor = _plan_value("date", compte["plancher"], "compte.plancher")
    ecriture = _exact_keys(_object(data, "ecriture", ""), ("id", "attendu", "cible"), "ecriture")
    facture = _exact_keys(_object(data, "facture", ""), ("id", "attendu", "cible"), "facture")
    entry_expected = _exact_keys(_object(ecriture, "attendu", "ecriture."), ENTRY_EXPECTED,
                                 "ecriture.attendu")
    entry_target = _exact_keys(_object(ecriture, "cible", "ecriture."), ENTRY_TARGETS,
                               "ecriture.cible")
    invoice_expected = _exact_keys(_object(facture, "attendu", "facture."), INVOICE_EXPECTED,
                                   "facture.attendu")
    invoice_target = _exact_keys(_object(facture, "cible", "facture."), INVOICE_TARGETS,
                                 "facture.cible")

    untouchables = _parse_untouchables(data.get("intouchables"))

    encours = _exact_keys(_object(data, "encours", ""), ("avant", "apres"), "encours")
    periode = _exact_keys(_object(data, "periode", ""), ("du", "au"), "periode")
    first = _plan_value("date", periode["du"], "periode.du")
    last = _plan_value("date", periode["au"], "periode.au")
    if first is None or last is None or first > last:
        raise RunRefused("plan mal formé : « periode » doit aller d'un jour à un jour postérieur")

    return Spec(
        motif=motif, note=note,
        account_id=_doc_id(compte, "compte"),
        floor=floor.date() if floor is not None else None,
        balance=_cents(compte["solde_cents"], "compte.solde_cents"),
        entry_id=_doc_id(ecriture, "ecriture"),
        entry_expected={n: _plan_value(n, v, f"ecriture.attendu.{n}")
                        for n, v in entry_expected.items()},
        entry_target=dict(entry_target),
        invoice_id=_doc_id(facture, "facture"),
        invoice_expected={n: _plan_value(n, v, f"facture.attendu.{n}")
                          for n, v in invoice_expected.items()},
        invoice_target={n: _plan_value(n, v, f"facture.cible.{n}")
                        for n, v in invoice_target.items()},
        untouchables=untouchables,
        outstanding=(_cents(encours["avant"], "encours.avant"),
                     _cents(encours["apres"], "encours.apres")),
        period=(first.date(), last.date()),
    )


# ── reads ─────────────────────────────────────────────────────────────────


def _snapshot(path: str):
    """One document, read strictly — a failed read stops the run."""
    try:
        return db.document(path).get()
    except Exception as exc:
        raise RunRefused(f"lecture de {path} impossible ({type(exc).__name__}) — rien n'a "
                         f"été écrit") from None


def _read_lines(invoice_id: str) -> list[dict]:
    """The invoice's line items, read strictly, in id order."""
    try:
        ref = (db.collection(INVOICES).document(invoice_id)
               .collection(invoice_model.LINE_ITEMS_SUB))
        lines = [{**(s.to_dict() or {}), "id": s.id} for s in ref.stream()]
    except Exception as exc:
        raise RunRefused(f"lignes de la facture illisibles ({type(exc).__name__}) — rien "
                         f"n'a été écrit") from None
    return sorted(lines, key=lambda d: d["id"])


@dataclass
class World:
    """Everything a run reads, read once: the store of the October script,
    the invoice's own snapshot (its ``update_time`` guards the batch) and
    line items."""

    store: Any
    invoice_snap: Any
    lines: list

    def invoices(self, payloads: Optional[dict] = None) -> Optional[list]:
        if self.store.invoices is None:
            return None
        return [rla._merge(inv, (payloads or {}).get(f"{INVOICES}/{inv.get('id')}"))
                for inv in self.store.invoices]


def read_world(invoice_id: str) -> World:
    store = rla.read_store()
    snap = _snapshot(f"{INVOICES}/{invoice_id}")
    if not snap.exists:
        raise RunRefused(f"facture introuvable : {invoice_id}")
    invoice = {**(snap.to_dict() or {}), "id": snap.id}
    if store.invoices is not None:
        # The copy the controls read is the snapshot the batch is guarded on.
        store.invoices = [invoice if inv.get("id") == invoice_id else inv
                          for inv in store.invoices]
    return World(store=store, invoice_snap=snap, lines=_read_lines(invoice_id))


# ── the controls ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Scope:
    """What the controls are about."""

    account_id: str
    entry_id: str
    invoice_id: str
    period: tuple


def _control_ledger(rows: list, accounts: dict) -> dict:
    running: dict = {}
    for account_id in sorted({str(r.get("account_id") or "") for r in rows}):
        mine = [r for r in rows if str(r.get("account_id") or "") == account_id]
        mine.sort(key=lambda r: (_day_of(r.get("date"), f"écriture {r.get('id')}"),
                                 int(r.get("sequence") or 0)))
        for row, balance in zip(mine, al.running_balances(mine)):
            running[str(row.get("id"))] = balance
    return {
        "soldes": rla._control_balances(rows, accounts),
        "soldes_courants": running,
        "sequences": {str(r.get("id")): r.get("sequence") for r in rows},
    }


def _entry_row(rows: list, entry_id: str) -> dict:
    for row in rows:
        if row.get("id") == entry_id:
            return row
    raise ControlImpossible("écriture absente de la lecture")


def _control_entry(rows: list, entry_id: str) -> dict:
    row = _entry_row(rows, entry_id)
    return {k: rla._canon(_aware(row.get(k))) for k in _ENTRY_FACTS}


def _resolved(rows: list) -> list:
    """The rows, each correction stamped with the kind it reverses — the
    stamp the model writes since 2026-10-07, supplied from the whole journal
    for an older one — so that a period's statement resolves a correction
    whose original lies outside the period. The sums stay the model's."""
    by_id = {r.get("id"): r for r in rows}
    out = []
    for row in rows:
        if row.get("kind") == al.REVERSAL_KIND and not row.get("reverses_kind"):
            original = by_id.get(row.get("reverses_id"))
            if original is not None:
                row = {**row, "reverses_kind": original.get("kind") or ""}
        out.append(row)
    return out


def _statement(rows: list) -> dict:
    return {"resultats": al.results_statement(rows), "avoir": al.owner_equity(rows)}


def _control_results(rows: list, period: tuple) -> dict:
    first, last = period
    resolved = _resolved(rows)
    in_period = [r for r in resolved
                 if first <= _day_of(r.get("date"), f"écriture {r.get('id')}") <= last]
    return {"periode": _statement(in_period), "journal": _statement(resolved)}


def _standing_receipts(rows: list, invoice_id: str) -> int:
    """Σ of the invoice's standing encaissements — the predicate of
    ``admin_ledger.sum_invoice_receipts``, on the rows read."""
    return sum(int(r.get("amount") or 0) for r in rows
               if r.get("invoice_id") == invoice_id and r.get("kind") == TARGET_KIND
               and vti._admin_standing(r))


def _control_invoice(invoices: list, rows: list, invoice_id: str, lines: list) -> dict:
    invoice = next((i for i in invoices if i.get("id") == invoice_id), None)
    if invoice is None:
        raise ControlImpossible("facture absente de la lecture")
    return {
        "paye": invoice.get("amount_paid"),
        "solde": invoice_model.balance_of(invoice),
        "statut": invoice.get("status"),
        "paid_date": rla._canon(_aware(invoice.get("paid_date"))),
        "encaissements_debout": _standing_receipts(rows, invoice_id),
        "montants": {k: invoice.get(k) for k in _INVOICE_AMOUNTS},
        "lignes": {"nombre": len(lines), "empreinte": rla._digest(lines)},
        "encours": sum(invoice_model.balance_of(i) for i in invoices
                       if i.get("status") in invoice_model._ISSUED_STATUSES),
    }


def _floor_of(reconciliations: dict, account_id: str) -> Optional[datetime]:
    day = (reconciliations.get(account_id) or {}).get("plancher")
    return rla._midnight(date.fromisoformat(day)) if day else None


def _control_lock(rows: list, accounts: dict, recs: list, scope: Scope) -> dict:
    reconciliations = rla._control_reconciliations(rows, accounts, recs)
    entry = _entry_row(rows, scope.entry_id)
    return {"conciliations": reconciliations,
            "lock_reason": al._entry_lock_reason(entry, _floor_of(reconciliations,
                                                                  scope.account_id))}


def _control_others(rows: list, accounts: dict, recs: list, invoices: list,
                    trust_rows: list, skip: frozenset) -> dict:
    out: dict = {}
    keyed = [(TX, [(r.get("id"), r) for r in rows]), (ACCOUNTS, list(accounts.items())),
             (RECS, [(r.get("id"), r) for r in recs]),
             (INVOICES, [(i.get("id"), i) for i in invoices]),
             (TRUST_TX, [(t.get("id"), t) for t in trust_rows])]
    for collection, docs in keyed:
        for doc_id, doc in docs:
            if not doc_id:
                raise ControlImpossible(f"un document de {collection} n'a pas d'identifiant")
            path = f"{collection}/{doc_id}"
            if path not in skip:
                out[path] = rla._digest(doc)
    return out


def _merged(collection: str, docs: Optional[list], payloads: Optional[dict]) -> Optional[list]:
    """*docs* (each carrying its ``id``) merged with *payloads* by path: the
    simulation sees a payload on ANY document read, not only on the ones a
    plan means to write — a write gone astray must show in the controls."""
    if docs is None or not payloads:
        return docs
    return [rla._merge(doc, payloads.get(f"{collection}/{doc.get('id')}")) for doc in docs]


def compute_controls(world: World, scope: Scope, payloads: Optional[dict] = None) -> dict:
    """The six controls and check 10, on the documents read — merged with
    *payloads* (by path) for the simulation."""
    store = world.store
    rows = store.control_rows(payloads)
    accounts = store.control_accounts(payloads)
    invoices = world.invoices(payloads)
    recs = _merged(RECS, store.recs, payloads)
    trust_rows = _merged(TRUST_TX, store.trust_rows, payloads)
    skip = frozenset((f"{TX}/{scope.entry_id}", f"{INVOICES}/{scope.invoice_id}"))
    unreadable_invoices = Impossible(f"factures illisibles ({store.invoices_error})")
    unreadable_trust = Impossible(f"registre du fidéicommis illisible ({store.trust_error})")
    return {
        "1": rla._attempt(_control_ledger, rows, accounts),
        "2": rla._attempt(_control_entry, rows, scope.entry_id),
        "3": rla._attempt(_control_results, rows, scope.period),
        "4": (rla._attempt(_control_invoice, invoices, rows, scope.invoice_id, world.lines)
              if invoices is not None else unreadable_invoices),
        "5": rla._attempt(_control_lock, rows, accounts, recs, scope),
        "6": (rla._attempt(_control_others, rows, accounts, recs, invoices, trust_rows, skip)
              if invoices is not None and trust_rows is not None
              else (unreadable_invoices if invoices is None else unreadable_trust)),
        "10": (rla._attempt(rla._control_fee_candidates, trust_rows, rows)
               if trust_rows is not None else unreadable_trust),
    }


def _statement_delta(before: dict, after: dict) -> dict:
    equity = {k: after["avoir"][k] - before["avoir"][k]
              for k in before["avoir"] if after["avoir"][k] != before["avoir"][k]}
    return {"resultats": rla._flat_results_delta(before["resultats"], after["resultats"]),
            "avoir": equity}


def expected_statement_delta(entry_before: dict, entry_after: dict) -> dict:
    """What the change of kind does to the totals, said by the same two
    functions of the entry alone."""
    return _statement_delta(_statement([entry_before]), _statement([entry_after]))


@dataclass
class Expected:
    """What the write must move, and by how much."""

    statement: dict          # the entry's own delta (control 3)
    in_period: bool          # does the entry's day fall in the period?
    paid_delta: int          # control 4: +amount (migration), -amount (restoration)


def compare_controls(before: dict, after: dict, expected: Expected) -> tuple[list, list]:
    """``(gaps, impossible)``: what moved otherwise than *expected*, and what
    could not be computed."""
    gaps: list[str] = []
    impossible: list[str] = []
    for key in ("1", "2", "3", "4", "5", "6", "10"):
        for value in (before.get(key), after.get(key)):
            if isinstance(value, Impossible):
                impossible.append(f"contrôle {key} impossible à calculer ({value.reason})")
                break
    blocked = {item.split(" ")[1] for item in impossible}
    for key in ("1", "2", "6"):
        if key not in blocked and before[key] != after[key]:
            gaps.extend(f"contrôle {key} — {d}" for d in rla._differences(before[key], after[key]))
    if "3" not in blocked:
        for scope in ("periode", "journal"):
            want = (expected.statement if scope == "journal" or expected.in_period
                    else {"resultats": {}, "avoir": {}})
            got = _statement_delta(before["3"][scope], after["3"][scope])
            if got != want:
                gaps.append(f"contrôle 3 ({scope}) — écart {got}, attendu {want}")
    if "4" not in blocked:
        # The amount paid moving by exactly the entry's amount needs no check
        # of its own: check 8 once written, the entry joining the standing
        # receipts with its amount unchanged (control 2) and no other entry
        # moving (control 6) imply it — and a check-8 gap found before the
        # write blocks the run here, since a correct write keeps it.
        b, a = before["4"], after["4"]
        delta = expected.paid_delta
        if a["encours"] - b["encours"] != -delta:
            gaps.append(f"contrôle 4 — encours {b['encours']} → {a['encours']}, attendu "
                        f"{-delta:+d}")
        if a["encaissements_debout"] != a["paye"]:
            gaps.append(f"contrôle 4 — encaissements debout {a['encaissements_debout']} ≠ "
                        f"montant payé {a['paye']} (contrôle 8 de l'intégrité)")
        for part in ("montants", "lignes"):
            if a[part] != b[part]:
                gaps.append(f"contrôle 4 — {part} : {rla._differences(b[part], a[part])}")
    if "5" not in blocked and before["5"]["conciliations"] != after["5"]["conciliations"]:
        gaps.extend(f"contrôle 5 — {d}" for d in rla._differences(
            before["5"]["conciliations"], after["5"]["conciliations"]))
    return gaps, impossible


# ── judging the plan ──────────────────────────────────────────────────────


@dataclass
class Plan:
    path: Path
    sha: str
    project: str
    spec: Spec
    world: World
    state: str
    table: list = field(default_factory=list)          # (document, field, attendu, base, ok)
    differences: list = field(default_factory=list)    # section 4
    refusals: list = field(default_factory=list)       # guards and targets
    writes: list = field(default_factory=list)
    payloads: dict = field(default_factory=dict)
    model_paid_date: Any = None                        # what the create path writes
    reconciliations_citing: list = field(default_factory=list)
    reconciliation_keys: list = field(default_factory=list)
    before: dict = field(default_factory=dict)
    simulated: dict = field(default_factory=dict)
    expected: Optional[Expected] = None
    gaps: list = field(default_factory=list)
    impossible: list = field(default_factory=list)
    fingerprint: str = ""

    @property
    def scope(self) -> Scope:
        return Scope(self.spec.account_id, self.spec.entry_id, self.spec.invoice_id,
                     self.spec.period)

    @property
    def entry(self) -> dict:
        return self.world.store.rows[self.spec.entry_id]

    @property
    def invoice(self) -> dict:
        return {**(self.world.invoice_snap.to_dict() or {}), "id": self.spec.invoice_id}

    @property
    def blocking(self) -> list:
        return self.differences + self.refusals + self.gaps + self.impossible


def _compare(label: str, doc: dict, expected: dict, table: list, differences: list) -> None:
    for name, want in expected.items():
        have = _base_value(name, doc)
        ok = have == want
        table.append((label, name, _v(want), _v(have), ok))
        if not ok:
            differences.append(f"{label}, {name} : le plan attend {_v(want)}, la base porte "
                               f"{_v(have)}")


def _compare_untouchables(untouchables: list, table: list, differences: list) -> None:
    """Each untouchable read strictly, its etag the one the plan states."""
    for path, etag in untouchables:
        snap = _snapshot(path)
        doc = snap.to_dict() if snap.exists else None
        have = (doc or {}).get("etag") if doc is not None else "(introuvable)"
        ok = doc is not None and have == etag
        table.append(("Intouchable", path, etag, have, ok))
        if not ok:
            differences.append(f"intouchable {path} : le plan attend l'etag {etag}, la base "
                               f"porte {have}")


def _entry_refusals(entry: dict, spec: Spec) -> list:
    out = []
    kind = entry.get("kind")
    if entry.get("account_id") != spec.account_id:
        out.append("l'écriture n'est pas au compte du plan")
    if kind not in SOURCE_KINDS:
        out.append(f"nature « {kind} » : seule une recette simple ({', '.join(SOURCE_KINDS)}) "
                   f"se reclasse ici")
    if entry.get("direction") != "recette":
        out.append("l'écriture n'est pas une recette")
    if entry.get("invoice_id"):
        out.append("l'écriture est déjà liée à une facture")
    if entry.get("trust_transaction_id"):
        out.append("l'écriture est liée au fidéicommis : elle se corrige de ce côté")
    if entry.get("reverses_id") or entry.get("reversed_by_id"):
        out.append("l'écriture est membre d'une contre-passation")
    if entry.get("status") == "annulée":
        out.append("l'écriture est annulée")
    if entry.get("category"):
        out.append("l'écriture porte une catégorie")
    if any(entry.get(k) for k in ("net_amount", "gst_amount", "qst_amount")):
        out.append("l'écriture porte une ventilation (une recette n'en a pas)")
    amount = entry.get("amount")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        out.append("montant illisible")
    if len(entry.get("revisions") or []) > MAX_TRAIL_BEFORE:
        out.append(f"le fil de l'écriture compte plus de {MAX_TRAIL_BEFORE} entrées : plus de "
                   f"place pour cette écriture et une restauration")
    return out


def _target_refusals(spec: Spec, invoice: dict) -> list:
    out = []
    target = spec.entry_target
    if target["kind"] != TARGET_KIND:
        out.append(f"cible.kind : « {TARGET_KIND} » est attendu")
    if target["invoice_id"] != spec.invoice_id:
        out.append("cible.invoice_id : ce n'est pas la facture du plan")
    from_invoice = {
        "invoice_number": invoice.get("invoice_number", ""),
        "dossier_id": invoice.get("dossier_id") or None,
        "dossier_file_number": invoice.get("dossier_file_number", ""),
        "dossier_title": invoice.get("dossier_title", ""),
    }
    for name in _FROM_INVOICE:
        if target[name] != from_invoice[name]:
            out.append(f"cible.{name} : la facture porte {_v(from_invoice[name])} — un "
                       f"encaissement le prend de la facture")
    for name in ("counterparty", "description"):
        try:
            checked = _check_text(target[name], TEXT_MAX, f"cible.{name}")
        except PlanRefused as exc:
            out.append(str(exc))
            continue
        if checked != target[name]:
            out.append(f"cible.{name} : pas d'espace en bordure")
    return out


def _invoice_refusals(account: dict, invoice: dict, amount: int) -> list:
    out = []
    if account.get("account_type") != "opérations":
        out.append(al._ABORT_MESSAGES["encaissement_carte_interdit"])
    if account.get("status") != "actif":
        out.append(al._ABORT_MESSAGES["compte_fermé"])
    if invoice.get("status") not in al._ISSUED_INVOICE_STATUSES:
        out.append(al._ABORT_MESSAGES["facture_non_émise"])
    elif amount > invoice_model.balance_of(invoice):
        out.append(al._ABORT_MESSAGES["encaissement_excède_solde"])
    return out


def _trail_of(entry: dict, sha: str) -> bool:
    return any(isinstance(r, dict) and r.get("source_sha256") == sha
               for r in entry.get("revisions") or [])


def _write(path: str, role: str, snap, before: dict, business: dict) -> rla.Write:
    collection, doc_id = path.split("/")
    return rla.Write(path, collection, doc_id, 1, role, snap, before, business,
                     {name: [before.get(name), value] for name, value in business.items()})


def _fingerprint(head: dict, writes: list, payloads: dict) -> str:
    rows = [{"chemin": w.path, "update_time": rla._stamp_iso(w.snap.update_time),
             "apres": {k: payloads[w.path][k] for k in sorted(w.business)}}
            for w in writes]
    rows.sort(key=lambda r: r["chemin"])
    return rla._digest({**head, "ecritures": rows})[:FINGERPRINT_HEX]


def _reconciliation_findings(recs: list, account_id: str, entry_id: str) -> tuple[list, list]:
    """The reconciliations that cite the entry, and any key a reconciliation
    holds beyond the model's shape — where a kind or a payee could hide."""
    citing, keys = [], set()
    for rec in recs:
        if rec.get("account_id") != account_id:
            continue
        keys |= set(rec) - _RECONCILIATION_KEYS
        if entry_id in json.dumps(rla._canon(rec), ensure_ascii=False, default=str):
            citing.append(f"{rec.get('id')} (au {_v(rec.get('period_end'))}, {rec.get('status')})")
    return citing, sorted(keys)


def prepare(plan_path: Path) -> Plan:
    data, sha = _load_json(plan_path, "plan")
    spec = parse_plan(data)
    world = read_world(spec.invoice_id)
    store = world.store
    if spec.entry_id not in store.entries:
        raise RunRefused(f"écriture introuvable : {spec.entry_id}")
    account = store.accounts.get(spec.account_id)
    if account is None:
        raise RunRefused(f"compte introuvable : {spec.account_id}")
    plan = Plan(path=plan_path, sha=sha, project=db.project, spec=spec, world=world,
                state=PENDING)
    entry, invoice = plan.entry, plan.invoice
    plan.model_paid_date = entry.get("date")
    plan.reconciliations_citing, plan.reconciliation_keys = _reconciliation_findings(
        store.recs, spec.account_id, spec.entry_id)

    if _trail_of(entry, sha):
        astray = [n for n in ENTRY_TARGETS if entry.get(n) != spec.entry_target[n]]
        if astray:
            plan.refusals.append(f"état mixte : le fil de l'écriture porte ce plan, mais "
                                 f"{', '.join(astray)} ne sont pas à leur cible")
        else:
            plan.state = DONE
    elif entry.get("kind") == TARGET_KIND:
        plan.refusals.append("l'écriture est déjà un encaissement de facture, sans le fil de "
                             "ce plan")

    if plan.state == PENDING:
        _compare("Écriture", entry, spec.entry_expected, plan.table, plan.differences)
        _compare("Facture", invoice, spec.invoice_expected, plan.table, plan.differences)
        reconciliations = rla._attempt(rla._control_reconciliations, store.control_rows(),
                                       store.control_accounts(), store.recs)
        floor = (Impossible(reconciliations.reason) if isinstance(reconciliations, Impossible)
                 else _floor_of(reconciliations, spec.account_id))
        _compare("Compte", {"plancher": floor if not isinstance(floor, Impossible) else None,
                            "solde_cents": account.get("ledger_balance")},
                 {"plancher": rla._midnight(spec.floor) if spec.floor else None,
                  "solde_cents": spec.balance}, plan.table, plan.differences)
        if isinstance(floor, Impossible):
            plan.differences.append(f"plancher de conciliation incalculable ({floor.reason})")
        _compare_untouchables(spec.untouchables, plan.table, plan.differences)
        plan.refusals += _entry_refusals(entry, spec)
        plan.refusals += _target_refusals(spec, invoice)
        plan.refusals += _invoice_refusals(account, invoice, entry.get("amount") or 0)
        _plan_writes(plan)
    elif plan.state == DONE:
        plan.table.append(("Écriture", "état", "cibles + fil de ce plan", "présents", True))

    plan.before = compute_controls(world, plan.scope)
    plan.simulated = compute_controls(world, plan.scope, plan.payloads)
    if plan.state == PENDING and not plan.refusals and plan.writes:
        # What the PLAN's change implies, never what the payload says: a
        # payload that strays from its plan must show in control 3.
        current = {**entry, "id": spec.entry_id}
        targeted = {**current, **spec.entry_target}
        first, last = spec.period
        plan.expected = Expected(
            statement=expected_statement_delta(current, targeted),
            in_period=first <= _day_of(entry.get("date"), "écriture") <= last,
            paid_delta=int(entry["amount"]))
        plan.gaps, plan.impossible = compare_controls(plan.before, plan.simulated, plan.expected)
        plan.gaps += _outstanding_gaps(plan)
    plan.fingerprint = _fingerprint({"plan_sha256": sha, "projet": plan.project},
                                    plan.writes, plan.payloads)
    return plan


def _outstanding_gaps(plan: Plan) -> list:
    before, after = plan.before.get("4"), plan.simulated.get("4")
    if isinstance(before, Impossible) or isinstance(after, Impossible):
        return []
    out = []
    for label, have, want in (("avant", before["encours"], plan.spec.outstanding[0]),
                              ("après", after["encours"], plan.spec.outstanding[1])):
        if have != want:
            out.append(f"encours {label} : {rla._money(have)}, le plan attend {rla._money(want)}")
    return out


def _plan_writes(plan: Plan) -> None:
    """The payloads, judged against the invoice's own computation — a
    refusal leaves the plan without writes."""
    spec, entry, invoice = plan.spec, plan.entry, plan.invoice
    if plan.refusals:
        return
    amount = int(entry["amount"])
    paid = int(invoice.get("amount_paid") or 0)
    target = spec.invoice_target
    choices = [_aware(invoice.get("paid_date")), _aware(entry.get("date"))]
    if target["amount_paid"] != paid + amount:
        plan.refusals.append(f"cible.amount_paid : {_v(paid)} + {_v(amount)} = "
                             f"{_v(paid + amount)}, le plan dit {_v(target['amount_paid'])}")
    if target["paid_date"] is None or target["paid_date"] not in choices:
        plan.refusals.append(f"cible.paid_date : {_v(choices[0])} (inchangée) ou "
                             f"{_v(choices[1])} (la date de l'écriture), rien d'autre")
    if plan.refusals:
        return
    now = datetime.now(UTC)
    with provenance.writing_via("script"):
        try:
            updates = invoice_model.payment_updates(invoice, paid + amount,
                                                    target["paid_date"], now=now)
        except invoice_model.PaymentRefused as refusal:
            plan.refusals.append(f"la facture refuse ce paiement : {refusal}")
            return
        status = updates.get("status", invoice.get("status"))
        if target["status"] != status:
            plan.refusals.append(f"cible.status : le modèle calcule {_v(status)}, le plan "
                                 f"dit {_v(target['status'])}")
            return
        entry_business = {n: spec.entry_target[n] for n in ENTRY_TARGETS
                          if entry.get(n) != spec.entry_target[n]}
        trail = {
            "at": now,
            "via": provenance.current_via(),
            "motif": spec.motif,
            "changes": {n: [entry.get(n), v] for n, v in entry_business.items()},
            "updated_at_before": entry.get("updated_at"),
            "source_sha256": plan.sha,
        }
        entry_payload = {**entry_business,
                         "revisions": list(entry.get("revisions") or []) + [trail],
                         **provenance.update_fields(now)}
    invoice_business = {n: updates[n] for n in INVOICE_TARGETS
                        if n in updates and _aware(invoice.get(n)) != _aware(updates[n])}
    invoice_payload = {**invoice_business,
                       **{k: updates[k] for k in ("updated_at", "etag", "updated_via")}}
    entry_path = f"{TX}/{spec.entry_id}"
    invoice_path = f"{INVOICES}/{spec.invoice_id}"
    plan.writes = [
        _write(entry_path, "écriture", plan.world.store.entries[spec.entry_id],
               {**entry, "id": spec.entry_id}, entry_business),
        _write(invoice_path, "facture", plan.world.invoice_snap, invoice, invoice_business),
    ]
    plan.payloads = {entry_path: entry_payload, invoice_path: invoice_payload}


# ── backup ────────────────────────────────────────────────────────────────


def _backup_body(created: datetime, project: str, source: dict, fingerprint: str,
                 context: dict, writes: list, payloads: dict) -> dict:
    documents = []
    for write in sorted(writes, key=lambda w: w.path):
        payload = payloads[write.path]
        documents.append({
            "chemin": write.path,
            "role": write.role,
            "update_time_lu": rla._stamp_iso(write.snap.update_time),
            "etag_migration": payload["etag"],
            "champs": sorted(write.business),
            "avant": rla._typed(write.before),
            "apres": rla._typed({k: payload[k] for k in sorted(write.business)}),
        })
    return {"format": BACKUP_FORMAT, "cree_le": created.isoformat(), "projet": project,
            "plan": source, "empreinte": fingerprint, "contexte": context,
            "documents": documents, "contenu_sha256": rla._digest(documents)}


def write_backup(directory: Path, plan: Plan) -> tuple[Path, str]:
    """Write, fsync, re-read and verify the backup; ``(path, sha256)``."""
    created = datetime.now(UTC)
    spec = plan.spec
    context = {"compte": spec.account_id, "ecriture": spec.entry_id,
               "facture": spec.invoice_id, "montant": int(plan.entry["amount"]),
               "periode": [spec.period[0].isoformat(), spec.period[1].isoformat()]}
    body = _backup_body(created, plan.project, {"chemin": str(plan.path), "sha256": plan.sha},
                        plan.fingerprint, context, plan.writes, plan.payloads)
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=1, allow_nan=False)
    path = directory / (f"reclassement-encaissement-{created.strftime('%Y%m%dT%H%M%S%fZ')}"
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
    world: Optional[World] = None
    rows: list = field(default_factory=list)     # (chemin, champ, actuel, restauré)
    refusals: list = field(default_factory=list)
    writes: list = field(default_factory=list)
    payloads: dict = field(default_factory=dict)
    before: dict = field(default_factory=dict)
    simulated: dict = field(default_factory=dict)
    expected: Optional[Expected] = None
    gaps: list = field(default_factory=list)
    impossible: list = field(default_factory=list)
    fingerprint: str = ""

    @property
    def blocking(self) -> list:
        return self.refusals + self.gaps + self.impossible


_BACKUP_DOCUMENT_KEYS = ("chemin", "role", "update_time_lu", "etag_migration", "champs",
                         "avant", "apres")


def _check_backup_documents(documents: list) -> None:
    for document in documents:
        if (not isinstance(document, dict) or set(document) != set(_BACKUP_DOCUMENT_KEYS)
                or not isinstance(document["champs"], list)
                or not isinstance(document["avant"], dict)
                or not isinstance(document["apres"], dict)
                or not set(document["champs"]) <= set(ENTRY_TARGETS + INVOICE_TARGETS)):
            raise RunRefused("sauvegarde altérée : un document n'a pas la forme attendue")


def prepare_restore(backup_path: Path) -> RestorePlan:
    body, sha = _load_json(backup_path, "sauvegarde")
    if body.get("format") != BACKUP_FORMAT:
        raise RunRefused(f"sauvegarde d'un autre format ({body.get('format')!r}) — attendu "
                         f"{BACKUP_FORMAT}")
    documents = body.get("documents")
    context = body.get("contexte")
    if (not isinstance(documents, list) or not isinstance(context, dict)
            or body.get("contenu_sha256") != rla._digest(documents)):
        raise RunRefused("sauvegarde altérée : son contenu ne concorde pas avec son empreinte")
    if body.get("projet") != db.project:
        raise RunRefused(f"sauvegarde du projet « {body.get('projet')} », base « {db.project} »")
    _check_backup_documents(documents)
    try:
        period = (date.fromisoformat(context["periode"][0]),
                  date.fromisoformat(context["periode"][1]))
        scope = Scope(context["compte"], context["ecriture"], context["facture"], period)
        amount = int(context["montant"])
    except (KeyError, IndexError, TypeError, ValueError):
        raise RunRefused("sauvegarde altérée : contexte illisible") from None
    by_role = {d.get("role"): d for d in documents if isinstance(d, dict)}
    expected_paths = {"écriture": f"{TX}/{scope.entry_id}",
                      "facture": f"{INVOICES}/{scope.invoice_id}"}
    if {r: (by_role.get(r) or {}).get("chemin") for r in expected_paths} != expected_paths:
        raise RunRefused("sauvegarde altérée : elle ne porte pas l'écriture et la facture de "
                         "son contexte")
    plan = RestorePlan(path=backup_path, sha=sha, project=db.project,
                       created=str(body.get("cree_le")),
                       migration_fingerprint=str(body.get("empreinte")), scope=scope)
    world = read_world(scope.invoice_id)
    plan.world = world
    if scope.entry_id not in world.store.entries:
        raise RunRefused(f"écriture introuvable : {scope.entry_id}")
    current = {"écriture": {**world.store.rows[scope.entry_id], "id": scope.entry_id},
               "facture": {**(world.invoice_snap.to_dict() or {}), "id": scope.invoice_id}}
    snaps = {"écriture": world.store.entries[scope.entry_id], "facture": world.invoice_snap}
    states = {}
    for role, document in by_role.items():
        if role not in expected_paths:
            continue
        doc = current[role]
        before = rla._untyped(document["avant"])
        after = rla._untyped(document["apres"])
        fields = list(document["champs"])
        restored = all(_aware(doc.get(f)) == _aware(before.get(f)) for f in fields)
        if doc.get("etag") == document["etag_migration"]:
            astray = [f for f in fields if _aware(doc.get(f)) != _aware(after.get(f))]
            if astray:
                plan.refusals.append(f"{document['chemin']} : etag de la migration, mais "
                                     f"{', '.join(astray)} n'ont pas sa valeur")
            states[role] = RESTORE_PENDING
        elif restored and (role != "écriture" or _trail_of(doc, sha)):
            states[role] = RESTORE_DONE
        else:
            plan.refusals.append(f"{document['chemin']} ne porte pas l'etag de la migration "
                                 f"(base {doc.get('etag')}, migration "
                                 f"{document['etag_migration']}) : la migration n'y est pas, ou "
                                 f"une écriture l'a suivie — rien ne se restaure par-dessus")
            states[role] = None
        for f in fields:
            plan.rows.append((document["chemin"], f, _v(_aware(doc.get(f))),
                              _v(_aware(before.get(f)))))
    if not plan.refusals:
        if set(states.values()) == {RESTORE_DONE}:
            plan.state = RESTORE_DONE
        elif set(states.values()) != {RESTORE_PENDING}:
            plan.refusals.append("état mixte : un document est restauré, l'autre non")
    if plan.state == RESTORE_PENDING and not plan.refusals:
        now = datetime.now(UTC)
        with provenance.writing_via("script"):
            for role, path in expected_paths.items():
                document = by_role[role]
                doc = current[role]
                before = rla._untyped(document["avant"])
                business = {f: before.get(f) for f in document["champs"]}
                payload = dict(business)
                if role == "écriture":
                    payload["revisions"] = list(doc.get("revisions") or []) + [{
                        "at": now,
                        "via": provenance.current_via(),
                        "motif": RESTORE_MOTIF.format(quand=rla._when(plan.created)),
                        "changes": {f: [doc.get(f), v] for f, v in business.items()},
                        "updated_at_before": doc.get("updated_at"),
                        "source_sha256": sha,
                    }]
                payload.update(provenance.update_fields(now))
                plan.writes.append(_write(path, role, snaps[role], doc, business))
                plan.payloads[path] = payload
    plan.before = compute_controls(world, scope)
    plan.simulated = compute_controls(world, scope, plan.payloads)
    if plan.writes:
        entry = current["écriture"]
        merged = rla._merge(entry, plan.payloads[f"{TX}/{scope.entry_id}"])
        plan.expected = Expected(
            statement=expected_statement_delta(entry, merged),
            in_period=period[0] <= _day_of(entry.get("date"), "écriture") <= period[1],
            paid_delta=-amount)
        plan.gaps, plan.impossible = compare_controls(plan.before, plan.simulated, plan.expected)
    plan.fingerprint = _fingerprint({"sauvegarde_sha256": sha, "projet": plan.project},
                                    plan.writes, plan.payloads)
    return plan


# ── after the write ───────────────────────────────────────────────────────


def after_real(scope: Scope) -> dict:
    """The controls on a fresh read, once written."""
    try:
        world = read_world(scope.invoice_id)
    except RunRefused as exc:
        reason = Impossible(str(exc))
        return {key: reason for key in ("1", "2", "3", "4", "5", "6", "10")}
    return compute_controls(world, scope)


# ── the report ────────────────────────────────────────────────────────────


def _table(header: tuple, rows: list) -> list:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(rla._cell(c) for c in row) + " |" for row in rows]
    return out


def _verdict(ok: bool) -> str:
    return "conforme" if ok else "**ÉCART**"


def _get(controls: Optional[dict], key: str):
    return None if controls is None else controls.get(key)


def _shown(value, fn) -> str:
    if value is None:
        return "—"
    if isinstance(value, Impossible):
        return f"impossible ({value.reason})"
    try:
        return fn(value)
    except (KeyError, TypeError):
        return "—"


def _statement_text(part: dict) -> str:
    r, a = part["resultats"], part["avoir"]
    return (f"encaissements de factures {rla._money(r['revenue'].get('encaissement_facture', 0))}"
            f" ; autres recettes {rla._money(r['revenue'].get('recette_autre', 0))}"
            f" ; dépenses {rla._money(r['total_expenses'])} ; TPS {rla._money(r['gst'])}"
            f" ; TVQ {rla._money(r['qst'])} ; apports {rla._money(a['contributions'])}"
            f" ; prélèvements {rla._money(a['drawings'])}")


def _render_controls(scope: Scope, before: dict, simulated: dict, real: Optional[dict],
                     account_id: str) -> list:
    first, last = scope.period
    columns = ("Contrôle", "Avant", "Après (simulé)") + (("Après (réel)",) if real else ())

    def row(label, key, fn):
        cells = [label, _shown(_get(before, key), fn), _shown(_get(simulated, key), fn)]
        if real:
            cells.append(_shown(_get(real, key), fn))
        return tuple(cells)

    def ledger(c):
        mine = c["soldes"].get(account_id) or {}
        return (f"solde {rla._money(mine.get('ledger_balance'))} (Σ {rla._money(mine.get('somme'))})"
                f" ; {len(c['soldes_courants'])} soldes courants ; "
                f"{len(c['sequences'])} séquences")

    def entry(c):
        return (f"{rla._money(c['amount'])}, {c['direction']}, du {str(c['date'])[:10]}, "
                f"compensée le {str(c['cleared_date'])[:10]}, {c['status']}, "
                f"séq. {c['sequence']}")

    def invoice(c):
        return (f"payé {rla._money(c['paye'])}, solde {rla._money(c['solde'])}, {c['statut']}, "
                f"paid_date {str(c['paid_date'])[:10]} ; encaissements debout "
                f"{rla._money(c['encaissements_debout'])} ; total "
                f"{rla._money(c['montants']['total'])} ; {c['lignes']['nombre']} ligne(s)")

    def lock(c):
        mine = c["conciliations"].get(account_id) or {}
        proofs = mine.get("preuves") or {}
        bad = sum(1 for p in proofs.values() if p.get("ecart"))
        last_rec = mine.get("derniere") or {}
        return (f"plancher {mine.get('plancher')} ; dernière {last_rec.get('fin')} "
                f"({last_rec.get('statut')}) ; {len(proofs)} preuve(s), {bad} en écart ; "
                f"lock_reason {c['lock_reason']}")

    rows = [
        row("1. Solde du compte, soldes courants, séquences", "1", ledger),
        row("2. L'écriture : montant, sens, dates, statut", "2", entry),
        row(f"3. Du {first} au {last}", "3", lambda c: _statement_text(c["periode"])),
        row("3. Journal entier", "3", lambda c: _statement_text(c["journal"])),
        row("4. La facture", "4", invoice),
        row("4. Encours du cabinet", "4", lambda c: rla._money(c["encours"])),
        row("5. Verrou et conciliations", "5", lock),
        row("6. Autres documents lus", "6", lambda c: f"{len(c)} document(s)"),
    ]
    return _table(columns, rows)


def _render_trail(entry: dict) -> list:
    trail = entry.get("revisions") or []
    if not trail:
        return ["Aucune entrée."]
    out = []
    for i, item in enumerate(trail):
        if not isinstance(item, dict):
            out.append(f"- [{i}] (illisible)")
            continue
        changes = " ; ".join(f"`{k}` {_v(v[0] if isinstance(v, list) and v else None)} → "
                             f"{_v(v[1] if isinstance(v, list) and len(v) > 1 else None)}"
                             for k, v in (item.get("changes") or {}).items())
        out.append(f"- [{i}] {_v(item.get('at'))}, via `{item.get('via')}` — {changes}")
        out.append(f"  - motif : {item.get('motif')}")
        if item.get("source_sha256"):
            out.append(f"  - source_sha256 : `{item.get('source_sha256')}`")
        out.append(f"  - updated_at_before : {_v(item.get('updated_at_before'))}")
    return out


def render_report(plan: Plan, *, mode: str, approved: Optional[str] = None,
                  refusal: Optional[list] = None, outcome: Optional[str] = None,
                  backup: Optional[tuple] = None, real: Optional[dict] = None,
                  drift: Optional[list] = None, real_gaps: Optional[list] = None) -> str:
    spec, entry, invoice = plan.spec, plan.entry, plan.invoice
    out = [
        "# Reclassement d'une écriture d'administration en encaissement de facture",
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
    out += ["", "## État", "", f"- Écriture : « {plan.state} »"]
    blocking = plan.blocking + list(refusal or [])
    if blocking:
        out.append("- **L'application est refusée** :")
        out += [f"  - {item}" for item in dict.fromkeys(blocking)]
    elif plan.state == PENDING:
        out.append("- Rien ne s'oppose à l'application : tout ce que le plan attend est dans "
                   "la base, et les contrôles simulés sont conformes.")
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

    out += ["", "## 1. Ce que le plan attend, et ce que porte la base", ""]
    out += _table(("Document", "Champ", "Attendu (plan)", "Base", ""),
                  [(d, f, w, h, "conforme" if ok else "**ÉCART**")
                   for d, f, w, h, ok in plan.table]) if plan.table else ["(sans objet)"]

    out += ["", "## 2. Changements prévus", ""]
    if plan.writes:
        for write in plan.writes:
            payload = plan.payloads[write.path]
            title = (f"Écriture `{spec.entry_id}` (séq. {entry.get('sequence')})"
                     if write.role == "écriture"
                     else f"Facture {invoice.get('invoice_number')} (`{spec.invoice_id}`)")
            out += [f"### {title}", ""]
            out += _table(("Champ", "Avant", "Après"),
                          [(f"`{k}`", _v(_aware(old)), _v(_aware(new)))
                           for k, (old, new) in write.changes.items()])
            stamps = ", ".join(f"`{k}`" for k in ("updated_at", "etag", "updated_via"))
            out += ["", f"Estampilles : {stamps} — `updated_via` = "
                        f"`{payload.get('updated_via')}`, etag `{payload.get('etag')}`."]
            if write.role == "écriture":
                trail = payload["revisions"][-1]
                out += ["", f"Entrée ajoutée au fil (`revisions` : "
                            f"{len(entry.get('revisions') or [])} → {len(payload['revisions'])}) :",
                        f"- via `{trail['via']}`, motif « {trail['motif']} »",
                        f"- updated_at_before : {_v(trail['updated_at_before'])}",
                        f"- source_sha256 : `{trail['source_sha256']}`", ""]
            else:
                kept = [k for k in INVOICE_TARGETS if k not in write.business]
                if kept:
                    out += ["", "Inchangé : " + ", ".join(
                        f"`{k}` ({_v(_aware(invoice.get(k)))})" for k in kept) + "."]
                out += ["", f"`payment_basis` (dérivé de `amount_paid`) : « recorded » avant "
                            f"et après. Le solde (`balance_of`) passe de "
                            f"{rla._money(invoice_model.balance_of(invoice))} à "
                            f"{rla._money(invoice_model.balance_of({**invoice, **payload}))}.", ""]
        out += ["Le compte n'est pas écrit (aucun argent ne bouge) ; aucune conciliation, "
                "aucune autre écriture, aucune autre facture n'est écrite."]
    else:
        out.append("Aucun." if plan.state != DONE else "Aucun : la migration est déjà appliquée.")

    out += ["", "## 3. Champs employés", "",
            f"- Nature : `kind` ({_v(entry.get('kind'))} → {_v(spec.entry_target['kind'])}).",
            "- Lien avec la facture : `invoice_id`, avec `invoice_number`, `dossier_id`, "
            "`dossier_file_number` et `dossier_title` copiés de la facture.",
            "- Montant payé : `amount_paid` sur la facture, seul stocké ; le solde est "
            "`invoice.balance_of` (dérivé) ; `payment_basis` est dérivé par le connecteur ; "
            "la cohérence avec le journal est le contrôle 8 de l'intégrité (Σ des encaissements "
            "liés debout = `amount_paid`).",
            f"- `paid_date` : l'application y mettrait la date de l'écriture "
            f"({_v(plan.model_paid_date)}) ; le plan retient {_v(spec.invoice_target['paid_date'])}.",
            "- Provenance : `script` — `updated_via` des deux documents et `via` du fil, sous "
            "`provenance.writing_via`, comme les migrations du 7 et du 30 octobre.",
            "", "## 4. Fil de révisions de l'écriture, avant", ""]
    out += _render_trail(entry)

    lock_before = _shown(_get(plan.before, "5"), lambda c: c["lock_reason"])
    lock_after = _shown(_get(plan.simulated, "5"), lambda c: c["lock_reason"])
    out += ["", "## 5. Verrou et conciliations", "",
            f"- `lock_reason` (dérivé à la lecture, jamais stocké) : {lock_before} → {lock_after}.",
            "- Conciliations du compte qui citent l'écriture : "
            + (", ".join(plan.reconciliations_citing) or "aucune") + ".",
            "- Champs des conciliations hors de la forme du modèle : "
            + (", ".join(f"`{k}`" for k in plan.reconciliation_keys) or "aucun")
            + " — une conciliation garde des totaux et les identifiants cochés, jamais la "
              "nature ni la contrepartie d'une écriture.",
            "- Ni `last_reconciliation_date`, ni le plancher, ni aucune conciliation ne sont "
            "écrits."]

    out += ["", "## 6. Contrôles", ""]
    out += _render_controls(plan.scope, plan.before, plan.simulated, real, spec.account_id)
    out += ["", "Écarts : " + ("aucun" if not (plan.gaps or plan.impossible)
                               else "; ".join(plan.gaps + plan.impossible))]
    changes = rla.fee_candidate_changes(_get(plan.before, "10"), _get(plan.simulated, "10"))
    out += ["", "Contrôle 10 du fidéicommis (candidats des paiements d'honoraires sans "
                "recette liée) : " + ("aucun changement" if not changes else "; ".join(
                    f"{fee} : {old} → {new}" for fee, old, new in changes)) + "."]

    out += ["", "## 7. Caches", "",
            "Aucun à recalculer : le modèle ne garde ni totaux par période ni encours stockés "
            "(`results_statement`, `owner_equity` et l'encours se calculent à la lecture), et "
            "le seul solde dénormalisé, `admin_accounts.ledger_balance`, ne bouge pas (même "
            "montant, même sens).",
            "", "## 8. Trace", "",
            "- Écriture : l'entrée ajoutée à son fil (`revisions`), avec le motif et chaque "
            "valeur avant et après.",
            "- Facture : aucun historique comparable (pas de fil ; `notes` s'imprime sur la "
            "facture ; `audit_events` ne journalise que les suppressions). L'écriture apparaît "
            "dans le bloc « Paiements » de la facture ; ce rapport et la sauvegarde gardent "
            "l'avant et l'après de la facture.",
            "", "## 9. Suite", ""]
    if plan.state == PENDING and not plan.writes:
        out.append("Rien ne peut s'écrire tant que les refus ci-dessus tiennent.")
    elif approved is None and plan.state == PENDING:
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
        "# Restauration d'un reclassement en encaissement de facture",
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
    out += _table(("Document", "Champ", "Actuel", "Restauré"), plan.rows) if plan.rows else ["—"]
    if plan.scope is not None and plan.world is not None:
        out += ["", "## Contrôles", ""]
        out += _render_controls(plan.scope, plan.before, plan.simulated, real,
                                plan.scope.account_id)
        out += ["", "Écarts : " + ("aucun" if not (plan.gaps or plan.impossible)
                                   else "; ".join(plan.gaps + plan.impossible))]
    return "\n".join(out) + "\n"


# ── the command line ──────────────────────────────────────────────────────


def _check_paths(args) -> tuple[Path, Optional[Path], Path]:
    """Every path is judged BEFORE any file is created."""
    if not args.rapport:
        raise RunRefused("--rapport est requis")
    if bool(args.plan) == bool(args.restaurer):
        raise RunRefused("donnez soit le plan, soit --restaurer SAUVEGARDE.json")
    if args.appliquer is not None and not _HEX16.fullmatch(args.appliquer):
        raise RunRefused("--appliquer attend l'empreinte de la passe à blanc approuvée "
                         "(16 caractères hexadécimaux)")
    if args.restaurer and args.sauvegarde:
        raise RunRefused("--sauvegarde ne sert qu'au reclassement, pas à --restaurer")
    if args.plan and args.appliquer and not args.sauvegarde:
        raise RunRefused("--appliquer exige --sauvegarde DOSSIER (hors du dépôt)")
    report = rla._outside_repo(args.rapport, "le rapport")
    if report.exists():
        raise RunRefused(f"le rapport « {report} » existe déjà — choisissez un autre nom")
    if not report.parent.is_dir():
        raise RunRefused(f"le dossier du rapport « {report.parent} » n'existe pas")
    backup_dir = None
    if args.sauvegarde:
        backup_dir = rla._outside_repo(args.sauvegarde, "le dossier de sauvegarde")
        if not backup_dir.is_dir():
            raise RunRefused(f"le dossier de sauvegarde « {backup_dir} » n'existe pas")
    source = rla._outside_repo(args.plan or args.restaurer,
                               "le plan" if args.plan else "la sauvegarde")
    return report, backup_dir, source


def _summary(plan: Plan, mode: str) -> list:
    return [
        f"Reclassement en encaissement de facture — {mode}",
        f"Projet : {plan.project}",
        f"Plan : {plan.path} (SHA-256 {plan.sha})",
        f"État : {plan.state} ; documents à écrire : {len(plan.writes)}",
        "Base et plan : " + ("concordent" if not plan.differences
                             else f"{len(plan.differences)} écart(s)"),
        "Refus : " + (str(len(plan.refusals)) if plan.refusals else "aucun"),
        "Contrôles simulés : " + ("conformes" if not plan.gaps and not plan.impossible else
                                  f"{len(plan.gaps)} écart(s), {len(plan.impossible)} "
                                  f"impossible(s)"),
        f"Empreinte : {plan.fingerprint}",
    ]


def _post_write(scope: Scope, before: dict, simulated: dict, expected: Expected) -> tuple:
    real = after_real(scope)
    gaps, impossible = compare_controls(before, real, expected)
    drift = rla._differences(simulated, real)
    return real, gaps + impossible, drift


def _run_migration(report: Path, source: Path, backup_dir: Optional[Path],
                   approved: Optional[str]) -> int:
    try:
        plan = prepare(source)
    except RunRefused as exc:
        return rla._finish(report, f"# Reclassement en encaissement de facture\n\n- Date : "
                                   f"{rla._now_line()}\n- Plan : `{source}`\n\n**Refusé — rien "
                                   f"n'a été écrit** : {exc}\n",
                           [f"Refusé — rien n'a été écrit : {exc}"], 1)
    if approved is None:
        summary = _summary(plan, "à blanc")
        if plan.blocking:
            summary.append(f"L'application serait refusée ({len(plan.blocking)} motif(s), voir "
                           f"le rapport). Rien n'a été écrit.")
            return rla._finish(report, render_report(plan, mode="à blanc"), summary, 1)
        if plan.state == DONE:
            summary.append("À blanc : rien n'a été écrit, et rien n'est à écrire : la "
                           "migration est déjà appliquée.")
        else:
            summary.append("À blanc : rien n'a été écrit. Pour écrire, après approbation : "
                           f"--sauvegarde DOSSIER --appliquer {plan.fingerprint}")
        return rla._finish(report, render_report(plan, mode="à blanc"), summary, 0)

    if plan.state == DONE:
        summary = _summary(plan, "application") + ["Rien à écrire : la migration est déjà "
                                                   "appliquée."]
        return rla._finish(report, render_report(plan, mode="application", approved=approved,
                                                 outcome="Rien à écrire."), summary, 0)
    refusal = []
    if approved != plan.fingerprint:
        refusal.append(f"l'empreinte a changé depuis la passe approuvée ({approved} → "
                       f"{plan.fingerprint}) : relancez à blanc et faites-la approuver")
    refusal += plan.blocking
    if not refusal and not plan.writes:
        refusal.append("rien à écrire")
    if refusal:
        summary = _summary(plan, "application refusée")
        summary += [f"Refusé — rien n'a été écrit : {r}" for r in dict.fromkeys(refusal)]
        return rla._finish(report, render_report(plan, mode="application refusée",
                                                 approved=approved, refusal=refusal), summary, 1)
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
    head = [f"Restauration — {'à blanc' if approved is None else 'application'}",
            f"Projet : {plan.project}",
            f"Sauvegarde : {plan.path} (SHA-256 {plan.sha})",
            f"État : {plan.state} ; documents à écrire : {len(plan.writes)} ; "
            f"refus : {len(plan.blocking)}",
            f"Empreinte : {plan.fingerprint}"]
    if approved is None:
        if plan.blocking:
            return rla._finish(report, render_restore_report(plan, mode="restauration à blanc"),
                               head + ["La restauration serait refusée (voir le rapport)."], 1)
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
    parser = rla._Parser(prog="python -m scripts.reclassify_as_invoice_receipt",
                         description="Reclassement d'une écriture d'administration en "
                                     "encaissement de facture (hors modèle, à blanc par "
                                     "défaut).")
    parser.add_argument("plan", nargs="?", help="le plan JSON approuvé (hors du dépôt public)")
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
        report, backup_dir, source = _check_paths(args)
    except RunRefused as exc:
        rla._out(f"Refusé — rien n'a été créé ni écrit : {exc}")
        return 1
    if args.restaurer:
        return _run_restore(report, source, args.appliquer)
    return _run_migration(report, source, backup_dir, args.appliquer)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
