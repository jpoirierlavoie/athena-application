"""Rectify, OUTSIDE the models, locked administration entries field by
field — their TPS/TVQ split, the supplier's invoice number, the description
and the dossier link — a one-shot migration bound to its plan (the
corrections of the « Comptes payables » verification, the lawyer's
decisions of 2026-11-09).

What it does
------------
The verification of the accounts payable against the administration ledger
found entries whose split, supplier invoice number, dossier or description
disagree with the receipt or with the lawyer's decisions. They are LOCKED (compensée, or behind a completed
reconciliation): ``admin_ledger.update_transaction`` refuses each of them,
as an edit must, and the house correction — a reversal and a new entry —
was declined by the lawyer. This script writes, on each entry the PLAN
names, the target the plan states — and nothing else.

Why it writes outside the models
--------------------------------
CLAUDE.md, « One door per rule »: a declared exception on the pattern of
``scripts/rectify_expense_ventilation`` and ``scripts/reclassify_admin_nature``,
whose helpers (and those of ``scripts/reclassify_admin_category``,
``scripts/reclassify_admin_ledger`` and ``scripts/reclassify_as_invoice_receipt``)
it imports and leaves untouched; it closes with its plan. A LOCKED entry is
ACCEPTED — that is the point: none of these fields moves a balance (the
amount, the sign and the dates stay) or a reconciliation (a reconciliation
keeps gross amounts and the ids it cleared, never a split, a dossier or a
description). The target is held to the MODEL's own rules: a split the
model accepts as is for the entry's amount (``validate_ventilation``,
``_validate_business``), a text ``security.sanitize`` leaves unaltered, a
dossier that exists — and the dossier link carries the snapshot the model's
edit writes with it (``update_transaction``): ``dossier_file_number`` and
``dossier_title`` copied from the dossier, both cleared with the link. A
field the snapshot writes but the plan's « cible » does not name is listed
in the report as such.

What it writes, on each entry: the fields of :data:`BUSINESS_FIELDS` whose
target differs from the stored value, the house stamps (``updated_at``, a
new ``etag``, ``updated_via = "script"`` under ``provenance.writing_via``)
and ONE ``revisions`` trail entry ``{at, via, motif, changes,
updated_at_before, source_sha256}`` whose motif is the plan's, verbatim.
Never the amount, the sign, a date, the status, the kind, the category, the
method, the reference, the counterparty, a link or a provenance stamp of
creation or clearing; no account, no reconciliation, no invoice, no
débours, no trust entry and no file in Storage. The model keeps no
ventilation mode (only the three figures), no TPS/TVQ period and no total
per dossier, category or period: the statements and the ledger compute them
at read. The one denormalized figure, ``admin_accounts.ledger_balance``,
moves with an amount only. The registers are not DAV-exposed: no CTag.

ONE TRANSACTION PER ENTRY (the lawyer's rule of 2026-11-09), in plan order:
each re-reads its entry — and the dossier it links — and commits only if
the entry is still the version the dry run read (its ``update_time``), the
dossier still carries the snapshot the payload copies, and the merged
document passes the entry's invariants (every other field unchanged, the
split summing to the amount, the model accepting it, the lock unchanged).
A refusal rolls that transaction back and STOPS the run: what was written
before stays, reported; a re-run is idempotent and resumes.

The plan
--------
A JSON object (UTF-8), outside the repository::

    {"note": "…",
     "comptes": {"carte": {"id": "…", "solde_affiche_cents": 0,
                           "plancher": "AAAA-MM-JJ"}, …},
     "dossiers": {"<id>": {"file_number": "…", "title": "…", "status": "…"}},
     "numeros": [{"numero": "A1",
                  "ecritures": [{"ecriture": "…",
                                 "attendu": {…},     # STATED_EXPECTED
                                 "cible": {…},       # TARGET_FIELDS
                                 "motif": "…"}]}],
     "modele": {"ecriture": "…", "attendu": {…}, "etag": "…"},
     "controles": {"periodes": [{"compte": "carte", "du": "…", "au": "…",
                                 "delta": {"brut": 0, "net": 0, "tps": 0,
                                           "tvq": 0}}],
                   "ensemble": {"brut": 0, "tps": 0, "tvq": 0},
                   "dossiers": {"<file number>": 0}}}

``attendu`` states the stored values the line expects, in the base's names
(a day as ``AAAA-MM-JJ``; ``null`` for an absent, null or empty value), and
four derived ones: ``lock_reason`` (``admin_ledger._entry_lock_reason``,
never stored), ``created_day`` (the Montréal day of ``created_at``),
``revisions`` (the trail's length) and ``derniere_revision`` (its last
entry, ``{"le": "AAAA-MM-JJ HH:MM"`` Montréal, ``"via": …}``, or ``null``).
``cible`` names the fields to write: the split, ``supplier_invoice_ref``,
``description``, ``dossier_id`` and ``dossier_file_number`` (``null``
clears). ``controles`` states what the plan moves — checked against the
plan's own lines before anything is read, then against the base.

Judging a number — every read is strict
---------------------------------------
A number groups the entries it corrects; any difference refuses the WHOLE
number (its other entries too), never forced; the other numbers still
apply; the plan is never bent to fit the base. An entry is refused when a
stated value differs; when it is absent, not on one of the plan's
accounts, not a dépense, a member of a reversal (seen from either end),
annulée, linked to an invoice or to trust, its amount or day unreadable,
its trail unreadable or nearly full; when its target is one the model
refuses, or a dossier it names is absent or carries another number than
the plan's. An entry whose trail carries this plan (same ``source_sha256``)
and which holds its target is « déjà appliquée »: the migration is
idempotent; the trail without the target is refused (« état mixte »), the
target without the trail too (« déjà la cible »).

The controls
------------
On complete reads — every administration entry, account and
reconciliation, every invoice, the trust register, every dossier and
débours — BEFORE, AFTER SIMULATED (the documents read, merged with the
exact payloads) and, once written, AFTER (re-read):

1. each account's stored ``ledger_balance`` and Σ ``admin_delta``, every
   entry's running balance and sequence — unchanged;
2. each plan entry: only its written fields move, from its stored state to
   its target, its trail by exactly one entry (the others intact); net +
   TPS + TVQ = amount;
3. ``admin_ledger.results_statement`` over each slice the plan states, over
   each account and over the whole journal — moved by exactly what the
   same function says of the written lines alone, as stored and as targeted;
4. the gross total of the entries linked to each of the plan's dossiers —
   moved likewise;
5. each account's lock floor, last reconciliation and the re-proof of every
   completed reconciliation — unchanged; each plan entry's ``lock_reason``
   — « écriture_verrouillée » before and after;
6. every other document read — unchanged (a digest per document).

A control that cannot be computed, or that moves otherwise, refuses
``--appliquer``. The plan's own statements (``controles``) are held to its
lines when it is read, and reported beside the base's figures.

Fingerprint, backup, restoration
--------------------------------
As in ``scripts/reclassify_admin_nature``: the fingerprint covers the plan's
digest, ``db.project``, every planned write (path, the ``update_time``
read, the fields written) and every refused or applied entry; the typed
JSON backup of each document to write — whole — is written, fsynced, read
back and verified before the first transaction. ``--restaurer`` (dry by
default, its own fingerprint) puts the saved fields back, one transaction
per entry, with fresh stamps and a trail entry « Restauration de la
sauvegarde du … — A1 », and refuses everything when an entry's etag is no
longer the migration's.

Running it — from ``athena/``, with ADC and the environment of « Active
gabarits » in DEPLOYMENT.md: even the dry run reads PRODUCTION. A plan,
report, backup or backup directory inside the repository is refused before
anything is created; the report must be a new file.

    python -m scripts.rectify_admin_entries PLAN.json --rapport R.md
    python -m scripts.rectify_admin_entries PLAN.json --rapport R.md \\
        --sauvegarde DOSSIER --appliquer EMPREINTE
    python -m scripts.rectify_admin_entries --restaurer SAUVEGARDE.json \\
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

from google.cloud import firestore

from models import admin_ledger as al
from models import db, provenance
from models import document as document_model
from models import dossier as dossier_model
from models import expense as expense_model
from scripts import reclassify_admin_category as rc
from scripts import reclassify_admin_ledger as rla
from scripts import reclassify_as_invoice_receipt as rx
from scripts.rectify_registers import _addressable
from security import sanitize
from tz import to_mtl

UTC = timezone.utc
TX = al.TRANSACTIONS_COLLECTION
ACCOUNTS = al.ACCOUNTS_COLLECTION
DOSSIERS = dossier_model.COLLECTION
EXPENSES = expense_model.COLLECTION
SPLIT = ("net_amount", "gst_amount", "qst_amount")
TEXTS = ("supplier_invoice_ref", "description")
#: The fields a plan line may target (« cible »).
TARGET_FIELDS = SPLIT + TEXTS + ("dossier_id", "dossier_file_number")
#: The dossier link and the snapshot the model writes with it.
DOSSIER_LINK = ("dossier_id", "dossier_file_number", "dossier_title")
#: Everything a write may change on an entry, besides the trail and the stamps.
BUSINESS_FIELDS = SPLIT + TEXTS + DOSSIER_LINK
_WRITTEN = BUSINESS_FIELDS + ("revisions", "updated_at", "etag", "updated_via")
#: The stored values a line may state, in the base's names.
STORED_EXPECTED = ("account_id", "sequence", "date", "cleared_date", "counterparty",
                   "category", "amount", "net_amount", "gst_amount", "qst_amount", "method",
                   "reference", "supplier_invoice_ref", "description", "dossier_id",
                   "dossier_file_number", "status", "created_via", "etag")
#: The derived values a line may state (never stored).
DERIVED_EXPECTED = ("lock_reason", "created_day", "revisions", "derniere_revision")
#: Every line states these; ``method`` and ``dossier_file_number`` are optional.
REQUIRED_EXPECTED = tuple(k for k in STORED_EXPECTED + DERIVED_EXPECTED
                          if k not in ("method", "dossier_file_number"))
_DAY_EXPECTED = ("date", "cleared_date", "created_day")
_INT_EXPECTED = ("sequence", "amount", "net_amount", "gst_amount", "qst_amount", "revisions")
#: The model entry's stated values (« modele.attendu »): any of these.
MODEL_EXPECTED = ("account_id", "sequence", "amount", "net_amount", "gst_amount",
                  "qst_amount", "created_day", "created_via")
#: The kind every plan entry must have: an expense carries its split.
ENTRY_KIND = "dépense"
LOCKED = "écriture_verrouillée"
#: The model's own cap on a written text (``admin_ledger._sanitize_data``).
TEXT_MAX = 2000
MAX_TRAIL_BEFORE = al._REVISIONS_CAP - 2
FINGERPRINT_HEX = 16
BACKUP_FORMAT = "athena.ecritures-rectifiees/1"
RESTORE_MOTIF = "Restauration de la sauvegarde du {quand} — {numero}"
PENDING, DONE, REFUSED = "à appliquer", "déjà appliquée", "refusée"
#: A number one of whose entries is written and another refused (a run that
#: stopped between them, then a foreign write): never relabelled « refusé ».
PARTIAL = "partiellement appliqué"
RESTORE_PENDING, RESTORE_DONE = rx.RESTORE_PENDING, rx.RESTORE_DONE
_CONTROLS = ("1", "2", "3", "4", "5", "6")
_BACKUP_DOCUMENT_KEYS = ("chemin", "numero", "update_time_lu", "etag_migration", "champs",
                         "avant", "apres")
_NUMBER = re.compile(r"[A-Z][0-9]{1,3}")
_LE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}")
#: The stated-control keys of a slice and of the whole journal.
_SLICE_KEYS = ("brut", "net", "tps", "tvq")
# Outcomes of one entry's transaction.
WRITTEN, LOST_BUT_WRITTEN, NOTHING, UNCERTAIN, ABORTED = (
    "écrite", "écrite (réponse perdue)", "non écrite", "incertaine", "refusée en transaction")

RunRefused = rla.RunRefused
Impossible = rla.Impossible
ControlImpossible = rla.ControlImpossible


# ── values ────────────────────────────────────────────────────────────────


def _blank(value) -> bool:
    return value is None or value == ""


def _norm(value):
    """A stored value in the plan's terms: absent, ``None`` and « » are one."""
    return None if _blank(value) else value


def _day_text(value) -> Optional[str]:
    day = rla._day(value)
    return day.isoformat() if day is not None else None


def _mtl_minute(value) -> Optional[str]:
    instant = rx._aware(value)
    if not isinstance(instant, datetime):
        return None
    return to_mtl(instant).strftime("%Y-%m-%d %H:%M")


def _mtl_day(value) -> Optional[str]:
    instant = rx._aware(value)
    if not isinstance(instant, datetime):
        return None
    return to_mtl(instant).date().isoformat()


def _int_or_none(value) -> Optional[int]:
    return rla._as_int(value)


def state_of(doc: dict) -> dict:
    """The business fields of *doc*, normalized as the model writes them: a
    split part an int (``None`` when unreadable, which no target equals), a
    text « » when absent, no dossier ``None`` with an empty snapshot."""
    out: dict = {}
    for key in SPLIT:
        value = doc.get(key)
        out[key] = 0 if value is None else _int_or_none(value)
    for key in TEXTS + ("dossier_file_number", "dossier_title"):
        value = doc.get(key)
        out[key] = "" if value is None else value
    out["dossier_id"] = doc.get("dossier_id") or None
    return out


def _shown_state(state: dict) -> str:
    parts = ["ventilation " + " + ".join(rla._money(state[k]) for k in SPLIT)]
    if state["supplier_invoice_ref"]:
        parts.append(f"facture {state['supplier_invoice_ref']}")
    parts.append(f"dossier {state['dossier_file_number'] or '—'}")
    if state["description"]:
        parts.append(f"description de {len(state['description'])} caractère(s)")
    return " · ".join(parts)


def equivalent_mode(doc: dict) -> str:
    """The ``ventilation`` argument of ``record_admin_entry`` that writes the
    entry's stored split — the model keeps the three figures, never a mode."""
    amount = _int_or_none(doc.get("amount"))
    split = tuple(_int_or_none(doc.get(k)) for k in SPLIT)
    if amount is None or None in split:
        return "illisible"
    if split == (amount, 0, 0):
        return "sans_taxe"
    if split == al.extract_taxes_from_gross(amount):
        return "ventiler"
    return "détaillée"


# ── the plan file ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EntrySpec:
    numero: str
    entry_id: str
    expected: dict      # name → value in the plan's terms (days as text)
    target: dict        # name → value; texts « » and no dossier ``None`` when cleared
    motif: str

    @property
    def path(self) -> str:
        return f"{TX}/{self.entry_id}"


@dataclass(frozen=True)
class NumberSpec:
    numero: str
    entries: tuple


@dataclass
class Spec:
    """The plan, its form checked — nothing read yet."""

    note: str
    accounts: dict           # key → {"id", "solde_affiche_cents", "plancher"}
    dossiers: dict           # id → {"file_number", "title"?, "status"?}
    numbers: list
    model: dict              # {"ecriture", "attendu", "etag"}
    slices: list             # [(account key, first day, last day, stated delta)]
    whole: dict              # stated delta over the whole journal
    dossier_deltas: dict     # file number → stated delta (cents)

    @property
    def entries(self) -> list:
        return [e for n in self.numbers for e in n.entries]

    def account_ids(self) -> set:
        return {a["id"] for a in self.accounts.values()}


def _refuse(message: str) -> RunRefused:
    return RunRefused(f"plan mal formé : {message}")


def _int(value, where: str, *, minimum: Optional[int] = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or (
            minimum is not None and value < minimum):
        raise _refuse(f"« {where} » doit être un entier" + (
            f" ({minimum} ou plus)" if minimum is not None else ""))
    return value


def _day(value, where: str) -> str:
    parsed = rla._day(rx._plan_value("date", value, where)) if value is not None else None
    if parsed is None:
        raise _refuse(f"« {where} » doit être un jour AAAA-MM-JJ")
    return parsed.isoformat()


def _text(value, where: str) -> str:
    """A text the model would store unaltered: non-empty, no space at its
    edges, at most :data:`TEXT_MAX` characters, unchanged by ``sanitize``."""
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > TEXT_MAX or sanitize(value, max_length=TEXT_MAX) != value):
        raise _refuse(f"« {where} » doit être un texte non vide, sans espace en bordure, "
                      f"d'au plus {TEXT_MAX} caractères, que l'enregistrement n'altère pas")
    return value


def _id(value, where: str) -> str:
    if not _addressable(value):
        raise _refuse(f"« {where} » n'est pas un identifiant")
    return value


def _expected_value(name: str, value, where: str):
    if value is None:
        return None
    if name in _DAY_EXPECTED:
        return _day(value, where)
    if name in _INT_EXPECTED:
        return _int(value, where, minimum=0)
    if name == "derniere_revision":
        if not isinstance(value, dict):
            raise _refuse(f"« {where} » doit être un objet ou null")
        rx._exact_keys(value, ("le", "via"), where)
        if not isinstance(value["le"], str) or not _LE.fullmatch(value["le"]):
            raise _refuse(f"« {where}.le » doit être « AAAA-MM-JJ HH:MM » (heure de Montréal)")
        if not isinstance(value["via"], str) or not value["via"]:
            raise _refuse(f"« {where}.via » doit être un texte non vide")
        return {"le": value["le"], "via": value["via"]}
    if not isinstance(value, str) or value == "":
        raise _refuse(f"« {where} » : un texte non vide ou null est attendu")
    return value


def _parse_expected(raw, where: str, names: tuple, required: tuple) -> dict:
    if not isinstance(raw, dict):
        raise _refuse(f"« {where} » doit être un objet")
    missing = [n for n in required if n not in raw]
    extra = sorted(set(raw) - set(names), key=str)
    if missing or extra:
        parts = ([f"manque {', '.join(missing)}"] if missing else []) + (
            [f"inconnu {', '.join(map(str, extra))}"] if extra else [])
        raise _refuse(f"« {where} » — {' ; '.join(parts)}")
    return {name: _expected_value(name, raw[name], f"{where}.{name}") for name in raw}


def _parse_target(raw, where: str, expected: dict, dossiers: dict) -> dict:
    if not isinstance(raw, dict) or not raw:
        raise _refuse(f"« {where} » doit être un objet non vide")
    extra = sorted(set(raw) - set(TARGET_FIELDS), key=str)
    if extra:
        raise _refuse(f"« {where} » — inconnu {', '.join(map(str, extra))}")
    target: dict = {}
    for key in SPLIT:
        if key in raw:
            target[key] = _int(raw[key], f"{where}.{key}", minimum=0)
    for key in TEXTS:
        if key in raw:
            target[key] = "" if raw[key] is None else _text(raw[key], f"{where}.{key}")
    if "dossier_file_number" in raw and "dossier_id" not in raw:
        raise _refuse(f"« {where}.dossier_file_number » ne va qu'avec dossier_id")
    if "dossier_id" in raw:
        dossier_id = raw["dossier_id"]
        if dossier_id is not None:
            _id(dossier_id, f"{where}.dossier_id")
            if dossier_id not in dossiers:
                raise _refuse(f"« {where}.dossier_id » : le dossier doit figurer dans « dossiers »")
        target["dossier_id"] = dossier_id
        if "dossier_file_number" in raw:
            number = raw["dossier_file_number"]
            stated = dossiers[dossier_id]["file_number"] if dossier_id else None
            if number != stated:
                raise _refuse(f"« {where}.dossier_file_number » : {number!r}, le dossier visé "
                              f"porte {stated!r} dans « dossiers »")
            # Named by the plan; its value is the dossier's (``target_state``).
            target["dossier_file_number"] = "" if number is None else number
    if any(k in target for k in SPLIT):
        split = [target.get(k, expected.get(k)) for k in SPLIT]
        if None in split or sum(split) != expected.get("amount"):
            raise _refuse(f"« {where} » : net + TPS + TVQ doit égaler le montant attendu")
    return target


def _parse_dossiers(raw) -> dict:
    if not isinstance(raw, dict):
        raise _refuse("« dossiers » doit être un objet")
    out = {}
    for dossier_id, item in raw.items():
        where = f"dossiers.{dossier_id}"
        _id(dossier_id, where)
        if not isinstance(item, dict) or "file_number" not in item or set(item) - {
                "file_number", "title", "status"}:
            raise _refuse(f"« {where} » : file_number (et, au besoin, title, status)")
        out[dossier_id] = {k: _expected_value("texte", v, f"{where}.{k}") for k, v in item.items()}
        if out[dossier_id]["file_number"] is None:
            raise _refuse(f"« {where}.file_number » doit être un texte")
    return out


def _parse_accounts(raw) -> dict:
    if not isinstance(raw, dict) or not raw:
        raise _refuse("« comptes » doit être un objet non vide")
    out, seen = {}, set()
    for key, item in raw.items():
        where = f"comptes.{key}"
        if not isinstance(item, dict):
            raise _refuse(f"« {where} » doit être un objet")
        rx._exact_keys(item, ("id", "solde_affiche_cents", "plancher"), where)
        account_id = _id(item["id"], f"{where}.id")
        if account_id in seen:
            raise _refuse(f"« {where}.id » : un compte une seule fois")
        seen.add(account_id)
        out[key] = {"id": account_id,
                    "solde_affiche_cents": _int(item["solde_affiche_cents"],
                                                f"{where}.solde_affiche_cents"),
                    "plancher": _day(item["plancher"], f"{where}.plancher")}
    return out


def _parse_controls(raw, accounts: dict) -> tuple[list, dict, dict]:
    if not isinstance(raw, dict):
        raise _refuse("« controles » doit être un objet")
    rx._exact_keys(raw, ("periodes", "ensemble", "dossiers"), "controles")
    slices = []
    if not isinstance(raw["periodes"], list):
        raise _refuse("« controles.periodes » doit être une liste")
    for i, item in enumerate(raw["periodes"]):
        where = f"controles.periodes[{i}]"
        if not isinstance(item, dict):
            raise _refuse(f"« {where} » doit être un objet")
        rx._exact_keys(item, ("compte", "du", "au", "delta"), where)
        if item["compte"] not in accounts:
            raise _refuse(f"« {where}.compte » doit nommer un des « comptes »")
        first, last = _day(item["du"], f"{where}.du"), _day(item["au"], f"{where}.au")
        if first > last:
            raise _refuse(f"« {where} » doit aller d'un jour à un jour postérieur")
        delta = item["delta"]
        if not isinstance(delta, dict) or not delta or set(delta) - set(_SLICE_KEYS):
            raise _refuse(f"« {where}.delta » : des clés parmi {', '.join(_SLICE_KEYS)}")
        slices.append((item["compte"], first, last,
                       {k: _int(v, f"{where}.delta.{k}") for k, v in delta.items()}))
    whole = raw["ensemble"]
    if not isinstance(whole, dict) or not whole or set(whole) - set(_SLICE_KEYS):
        raise _refuse(f"« controles.ensemble » : des clés parmi {', '.join(_SLICE_KEYS)}")
    whole = {k: _int(v, f"controles.ensemble.{k}") for k, v in whole.items()}
    deltas = raw["dossiers"]
    if not isinstance(deltas, dict):
        raise _refuse("« controles.dossiers » doit être un objet")
    deltas = {str(k): _int(v, f"controles.dossiers.{k}") for k, v in deltas.items()}
    return slices, whole, deltas


def parse_plan(data: dict) -> Spec:
    """The plan's form, judged before any read — any defect refuses."""
    rx._exact_keys({k: v for k, v in data.items() if k != "note"},
                   ("comptes", "dossiers", "numeros", "modele", "controles"), "plan")
    note = data.get("note", "")
    if not isinstance(note, str):
        raise _refuse("« note » doit être un texte")
    accounts = _parse_accounts(data["comptes"])
    dossiers = _parse_dossiers(data["dossiers"])
    raw_numbers = data["numeros"]
    if not isinstance(raw_numbers, list) or not raw_numbers:
        raise _refuse("« numeros » doit être une liste non vide")
    numbers, seen_numbers, seen_entries = [], set(), set()
    for i, item in enumerate(raw_numbers):
        where = f"numeros[{i}]"
        if not isinstance(item, dict):
            raise _refuse(f"« {where} » doit être un objet")
        rx._exact_keys(item, ("numero", "ecritures"), where)
        numero = item["numero"]
        if not isinstance(numero, str) or not _NUMBER.fullmatch(numero) or numero in seen_numbers:
            raise _refuse(f"« {where}.numero » : une lettre et un nombre (A1), une seule fois")
        seen_numbers.add(numero)
        raw_entries = item["ecritures"]
        if not isinstance(raw_entries, list) or not raw_entries:
            raise _refuse(f"« {where}.ecritures » doit être une liste non vide")
        entries = []
        for j, raw in enumerate(raw_entries):
            w = f"{where}.ecritures[{j}]"
            if not isinstance(raw, dict):
                raise _refuse(f"« {w} » doit être un objet")
            rx._exact_keys(raw, ("ecriture", "attendu", "cible", "motif"), w)
            entry_id = _id(raw["ecriture"], f"{w}.ecriture")
            if entry_id in seen_entries:
                raise _refuse(f"« {w}.ecriture » : une écriture une seule fois dans le plan")
            seen_entries.add(entry_id)
            expected = _parse_expected(raw["attendu"], f"{w}.attendu",
                                       STORED_EXPECTED + DERIVED_EXPECTED, REQUIRED_EXPECTED)
            if expected["account_id"] not in {a["id"] for a in accounts.values()}:
                raise _refuse(f"« {w}.attendu.account_id » doit être un des « comptes »")
            if expected.get("dossier_id") and expected["dossier_id"] not in dossiers:
                raise _refuse(f"« {w}.attendu.dossier_id » : le dossier doit figurer dans "
                              f"« dossiers »")
            for name in ("account_id", "sequence", "date", "amount"):
                if expected.get(name) is None:
                    raise _refuse(f"« {w}.attendu.{name} » ne peut pas être null")
            target = _parse_target(raw["cible"], f"{w}.cible", expected, dossiers)
            entries.append(EntrySpec(numero=numero, entry_id=entry_id, expected=expected,
                                     target=target, motif=_text(raw["motif"], f"{w}.motif")))
        numbers.append(NumberSpec(numero=numero, entries=tuple(entries)))

    model = data["modele"]
    if not isinstance(model, dict):
        raise _refuse("« modele » doit être un objet")
    rx._exact_keys(model, ("ecriture", "attendu", "etag"), "modele")
    model_id = _id(model["ecriture"], "modele.ecriture")
    if model_id in seen_entries:
        raise _refuse("« modele.ecriture » ne peut pas être une écriture du plan")
    if not isinstance(model["etag"], str) or not model["etag"]:
        raise _refuse("« modele.etag » doit être un texte non vide")
    model = {"ecriture": model_id, "etag": model["etag"],
             "attendu": _parse_expected(model["attendu"], "modele.attendu", MODEL_EXPECTED, ())}
    slices, whole, dossier_deltas = _parse_controls(data["controles"], accounts)
    numbers_by_file = {d["file_number"] for d in dossiers.values()}
    unknown = sorted(set(dossier_deltas) - numbers_by_file)
    if unknown:
        raise _refuse(f"« controles.dossiers » : dossier(s) absent(s) de « dossiers » : "
                      f"{', '.join(unknown)}")
    spec = Spec(note=note, accounts=accounts, dossiers=dossiers, numbers=numbers, model=model,
                slices=slices, whole=whole, dossier_deltas=dossier_deltas)
    _check_statements(spec)
    return spec


# ── the plan's statements, against its own lines ──────────────────────────


def _pseudo(expected: dict, values: dict) -> dict:
    """An entry as the plan describes it — before (``values`` = its
    expectations) or after (its expectations under its target)."""
    return {"id": "plan", "kind": ENTRY_KIND, "direction": "déboursé",
            "account_id": expected["account_id"], "date": expected["date"],
            "category": expected.get("category"), "amount": expected["amount"],
            "dossier_id": values.get("dossier_id"),
            **{k: values.get(k) or 0 for k in SPLIT}}


def _plan_pairs(spec: Spec) -> list:
    pairs = []
    for entry in spec.entries:
        before = {k: entry.expected.get(k) for k in SPLIT + ("dossier_id",)}
        after = {**before, **{k: v for k, v in entry.target.items() if k in before}}
        pairs.append((entry.expected, _pseudo(entry.expected, before),
                      _pseudo(entry.expected, after)))
    return pairs


def _slice_delta(before: dict, after: dict) -> dict:
    """The stated-control figures of two results statements: gross, net,
    TPS and TVQ of the expenses."""
    return {"brut": after["total_expenses"] - before["total_expenses"],
            "net": sum(after["expenses_net"].values()) - sum(before["expenses_net"].values()),
            "tps": after["gst"] - before["gst"], "tvq": after["qst"] - before["qst"]}


def _dossier_delta(pairs: list, dossiers: dict) -> dict:
    out = {d["file_number"]: 0 for d in dossiers.values()}
    for _expected, before, after in pairs:
        for doc, sign in ((before, -1), (after, 1)):
            dossier = dossiers.get(doc.get("dossier_id") or "")
            if dossier:
                out[dossier["file_number"]] += sign * int(doc["amount"])
    return out


def _check_statements(spec: Spec) -> None:
    """The plan's « controles » held to its own lines — a statement the lines
    do not imply is a transcription error, refused before anything is read."""
    pairs = _plan_pairs(spec)
    problems = []
    for key, first, last, stated in spec.slices:
        account_id = spec.accounts[key]["id"]
        mine = [(b, a) for e, b, a in pairs
                if e["account_id"] == account_id and first <= e["date"] <= last]
        got = _slice_delta(al.results_statement([b for b, _ in mine]),
                           al.results_statement([a for _, a in mine]))
        for k, v in stated.items():
            if got[k] != v:
                problems.append(f"{key} du {first} au {last}, {k} : énoncé {v}, lignes {got[k]}")
    got = _slice_delta(al.results_statement([b for _, b, _ in pairs]),
                       al.results_statement([a for _, _, a in pairs]))
    for k, v in spec.whole.items():
        if got[k] != v:
            problems.append(f"ensemble, {k} : énoncé {v}, lignes {got[k]}")
    moved = _dossier_delta(pairs, spec.dossiers)
    for number, v in spec.dossier_deltas.items():
        if moved.get(number, 0) != v:
            problems.append(f"dossier {number} : énoncé {v}, lignes {moved.get(number, 0)}")
    for number, v in moved.items():
        if v and number not in spec.dossier_deltas:
            problems.append(f"dossier {number} : les lignes le déplacent de {v}, aucun énoncé")
    if problems:
        raise _refuse("les contrôles énoncés ne concordent pas avec les lignes — "
                      + " ; ".join(problems))


# ── reads ─────────────────────────────────────────────────────────────────


@dataclass
class World:
    """Everything the run reads, read once: the administration registers and
    the controls' collections (``rla.read_store``), every dossier and every
    débours."""

    store: Any
    dossiers: dict              # id → document
    dossier_snaps: dict
    expenses: Optional[list]
    expenses_error: str


def read_world() -> World:
    store = rla.read_store()
    try:
        snaps = {s.id: s for s in db.collection(DOSSIERS).stream()}
    except Exception as exc:
        raise RunRefused(f"lecture des dossiers impossible ({type(exc).__name__}) — rien n'a "
                         f"été écrit") from None
    expenses, error = None, ""
    try:
        expenses = [{**rla._doc(s), "id": s.id} for s in db.collection(EXPENSES).stream()]
    except Exception as exc:
        error = type(exc).__name__
    return World(store=store, dossiers={k: rla._doc(s) for k, s in snaps.items()},
                 dossier_snaps=snaps, expenses=expenses, expenses_error=error)


# ── judging ───────────────────────────────────────────────────────────────


def _derived(name: str, doc: dict, floor: Optional[datetime]):
    if name == "lock_reason":
        return al._entry_lock_reason(doc, floor)
    if name == "created_day":
        return _mtl_day(doc.get("created_at"))
    if name == "revisions":
        return rc._trail_length(doc)
    if name == "derniere_revision":
        trail = doc.get("revisions")
        if not isinstance(trail, list) or not trail:
            return None
        last = trail[-1]
        if not isinstance(last, dict):
            return {"le": None, "via": None}
        return {"le": _mtl_minute(last.get("at")), "via": last.get("via")}
    raise KeyError(name)


def base_value(name: str, doc: dict, floor: Optional[datetime]):
    """A stored or derived value in the plan's terms."""
    if name in DERIVED_EXPECTED:
        return _derived(name, doc, floor)
    if name in ("date", "cleared_date"):
        return _day_text(doc.get(name)) if not _blank(doc.get(name)) else None
    return _norm(doc.get(name))


def _shown(value) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{k} {rx._v(v)}" for k, v in value.items())
    return rx._v(value)


@dataclass
class Verdict:
    entry: EntrySpec
    state: str = PENDING
    snap: Any = None
    doc: dict = field(default_factory=dict)
    target: dict = field(default_factory=dict)       # the full business state after
    origin: dict = field(default_factory=dict)       # written field → « plan » | « règle du modèle »
    table: list = field(default_factory=list)        # (document, champ, attendu, base, ok)
    reasons: list = field(default_factory=list)      # [(code, French reason)]

    def refuse(self, code: str, message: str) -> None:
        self.state = REFUSED
        self.reasons.append((code, message))


def entry_refusals(doc: dict, spec: Spec, store) -> list:
    """``[(code, French reason)]`` refusing ANY entry this script would write,
    the plan's own expectations apart."""
    out = []
    if doc.get("account_id") not in spec.account_ids():
        out.append(("compte_hors_plan", "l'écriture n'est sur aucun des comptes du plan"))
    if doc.get("kind") != ENTRY_KIND or doc.get("direction") != "déboursé":
        out.append(("nature", f"nature « {doc.get('kind')} », sens « {doc.get('direction')} » : "
                              f"seule une dépense se rectifie ici"))
    amount = doc.get("amount")
    if _int_or_none(amount) is None or amount <= 0:
        out.append(("montant_illisible", "montant illisible"))
    if rla._day(doc.get("date")) is None:
        out.append(("date_illisible", "date illisible"))
    if (doc.get("reverses_id") or doc.get("reversed_by_id")
            or doc.get("id") in rc._named_by_a_correction(store)):
        out.append(("lien_contre_passation", "membre d'une contre-passation : la correction "
                                             "copie l'original, la paire ne se compenserait plus"))
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
                                 f"et d'une restauration"))
    if None in (state_of(doc)[k] for k in SPLIT):
        out.append(("ventilation_illisible", "ventilation illisible"))
    return out


def _dossier_problems(dossier_id: str, role: str, world: World, spec: Spec) -> list:
    """``[(code, French reason)]`` when the dossier read is not the one the
    plan's « dossiers » describes, or is absent."""
    dossier = world.dossiers.get(dossier_id)
    if dossier is None:
        return [("dossier_introuvable", f"le dossier {role} {dossier_id} est introuvable")]
    out = []
    for name, want in spec.dossiers[dossier_id].items():
        have = _norm(dossier.get(name))
        if have != want:
            out.append(("dossier_autre", f"le dossier {role} {dossier_id} porte {name} "
                                         f"{_shown(have)}, le plan {_shown(want)}"))
    return out


def _snapshot_reconciliations(doc: dict, store) -> list:
    """``[(reconciliation id, [keys])]``: the reconciliations of the entry's
    account that cite it AND hold a key outside the model's shape — where a
    split, a dossier or a description could be recorded (rule 2.5)."""
    out = []
    for rec in store.recs_of(doc.get("account_id")):
        extra = sorted(set(rec) - rx._RECONCILIATION_KEYS, key=str)
        if extra and str(doc.get("id")) in json.dumps(rla._canon(rec), ensure_ascii=False,
                                                       default=str):
            out.append((str(rec.get("id")), extra))
    return out


def target_state(entry: EntrySpec, doc: dict, world: World,
                 spec: Spec) -> tuple[Optional[dict], dict, list]:
    """``(target state, origin of each changed field, [])`` — the state the
    MODEL makes of the line's target on this entry — or ``(None, {}, [(code,
    French reason)])``."""
    before = state_of(doc)
    target = dict(before)
    origin: dict = {}
    for key, value in entry.target.items():
        if key in SPLIT or key in TEXTS:
            target[key] = value
        elif key == "dossier_id":
            target["dossier_id"] = value
    problems = []
    if target["dossier_id"] != before["dossier_id"]:
        # Every dossier the change touches — the one it removes, the one it
        # makes — must be the one the plan describes (rule 2.3).
        for role, dossier_id in (("retiré", before["dossier_id"]),
                                 ("visé", target["dossier_id"])):
            if dossier_id and dossier_id in spec.dossiers:
                problems += _dossier_problems(dossier_id, role, world, spec)
        new_id = target["dossier_id"]
        if new_id is None:
            target["dossier_file_number"] = target["dossier_title"] = ""
        elif new_id in world.dossiers:
            # The model's snapshot (``update_transaction``), from the dossier read.
            dossier = world.dossiers[new_id]
            target["dossier_file_number"] = dossier.get("file_number", "")
            target["dossier_title"] = dossier.get("title", "")
    for key in BUSINESS_FIELDS:
        if target[key] != before[key]:
            origin[key] = "plan" if key in entry.target else "règle du modèle"
    kind = doc.get("kind") or ""
    if kind in al.NO_DOSSIER_KINDS and target["dossier_id"]:
        problems.append(("dossier_interdit", al._ABORT_MESSAGES["dossier_interdit"]))
    amount = _int_or_none(doc.get("amount"))
    if amount is not None and None not in (target[k] for k in SPLIT):
        accepted, reason = al.validate_ventilation(doc.get("direction") or "", amount,
                                                   *(target[k] for k in SPLIT), kind=kind)
        if accepted != {k: target[k] for k in SPLIT}:
            problems.append(("ventilation_refusée",
                             f"ventilation {' + '.join(str(target[k]) for k in SPLIT)} : le "
                             f"modèle ne l'accepte pas telle quelle pour {amount} "
                             f"({reason or 'normalisée autrement'})"))
        else:
            merged = {**doc, **{k: target[k] for k in SPLIT + TEXTS}}
            ventilation, reason = al._validate_business(merged)
            if reason:
                problems.append((reason, al._ABORT_MESSAGES.get(reason, reason)))
            elif {k: int(ventilation[k]) for k in SPLIT} != {k: target[k] for k in SPLIT}:
                problems.append(("ventilation_refusée", "le modèle normaliserait la "
                                                        "ventilation autrement"))
    for key in TEXTS:
        if key in origin and target[key] and sanitize(target[key], max_length=TEXT_MAX) != target[key]:
            problems.append(("texte_altéré", f"{key} serait altéré à l'enregistrement"))
    if problems:
        return None, {}, problems
    return target, origin, []


def _compare(entry: EntrySpec, doc: dict, floor: Optional[datetime], verdict: Verdict) -> None:
    label = f"{entry.numero} — séq. {entry.expected.get('sequence')}"
    for name, want in entry.expected.items():
        have = base_value(name, doc, floor)
        ok = have == want
        verdict.table.append((label, name, _shown(want), _shown(have), ok))
        if not ok:
            verdict.refuse(f"écart:{name}", f"{name} : la base porte {_shown(have)}, le plan "
                                            f"attend {_shown(want)}")


def judge_entry(entry: EntrySpec, world: World, spec: Spec, sha: str,
                floors: dict) -> Verdict:
    verdict = Verdict(entry)
    store = world.store
    snap = store.entries.get(entry.entry_id)
    if snap is None:
        verdict.refuse("écriture_introuvable", "écriture introuvable au registre "
                                               "d'administration")
        return verdict
    doc = {**store.rows[entry.entry_id], "id": entry.entry_id}
    verdict.snap, verdict.doc = snap, doc
    floor = floors.get(doc.get("account_id") or "")
    target, origin, problems = target_state(entry, doc, world, spec)
    for code, message in problems:
        verdict.refuse(code, message)
    if target is not None:
        verdict.target, verdict.origin = target, origin
        if rc._trail_of(doc, sha):
            if state_of(doc) == target:
                verdict.state = DONE
            else:
                verdict.refuse("état_mixte", "état mixte : le fil de l'écriture porte ce plan, "
                                             "mais elle ne porte pas la cible")
            return verdict
        if not origin:
            # Rule 2.8 : « un numéro déjà à sa valeur cible est reconnu comme
            # fait, non rejoué » — whoever wrote it (an earlier plan file).
            verdict.state = DONE
            verdict.reasons.append(("déjà_la_cible", "l'écriture porte déjà la cible, sans le "
                                                     "fil de ce plan : reconnue comme faite, "
                                                     "rien n'est rejoué"))
            return verdict
    # Compared even when the target is refused: the report gives every
    # stated value beside the base's, for every number.
    _compare(entry, doc, floor, verdict)
    for code, message in entry_refusals(doc, spec, store):
        verdict.refuse(code, message)
    for rec_id, keys in _snapshot_reconciliations(doc, store):
        verdict.refuse("conciliation_instantané",
                       f"la conciliation {rec_id} cite l'écriture et porte des champs hors de "
                       f"la forme du modèle ({', '.join(keys)}) : rien n'est écrit (règle 2.5)")
    return verdict


@dataclass
class NumberVerdict:
    number: NumberSpec
    verdicts: list

    @property
    def state(self) -> str:
        states = {v.state for v in self.verdicts}
        if REFUSED in states:
            return PARTIAL if DONE in states else REFUSED
        if states == {DONE}:
            return DONE
        return PENDING


def judge_number(number: NumberSpec, world: World, spec: Spec, sha: str,
                 floors: dict) -> NumberVerdict:
    """Every entry of the number judged; one refused refuses the others that
    are still to write — an entry already written stays « déjà appliquée »,
    and the number reads « partiellement appliqué »."""
    verdicts = [judge_entry(e, world, spec, sha, floors) for e in number.entries]
    refused = [v for v in verdicts if v.state == REFUSED]
    if refused:
        for v in verdicts:
            if v.state == PENDING:
                v.refuse("numéro_refusé", f"une autre écriture du numéro {number.numero} est "
                                          f"refusée : le numéro ne s'applique pas")
    return NumberVerdict(number, verdicts)


# ── the controls ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Scope:
    """What the controls are about."""

    entry_ids: tuple
    slices: tuple        # ((account id, first day, last day), …) — first/last as dates
    account_ids: tuple
    dossier_ids: tuple
    #: ((entry id, net, TPS, TVQ, dossier id), …) — what the plan states each
    #: entry held before its correction (control 7); empty for a restoration.
    baseline: tuple = ()


def _slice_key(account_id: str, first, last) -> str:
    return f"{account_id}/{first}/{last}"


def _in_slice(row: dict, account_id: str, first: date, last: date) -> bool:
    return (row.get("account_id") == account_id
            and first <= rx._day_of(row.get("date"), f"écriture {row.get('id')}") <= last)


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
        state = state_of(row)
        amount = _int_or_none(row.get("amount"))
        out[entry_id] = {
            "document": rla._digest(row),
            "faits": rla._digest({k: v for k, v in row.items() if k not in _WRITTEN}),
            "etat": state,
            "somme_juste": (amount is not None and None not in (state[k] for k in SPLIT)
                            and sum(state[k] for k in SPLIT) == amount),
            "fil": rla._digest(trail),
            "fil_moins_un": (rla._digest(trail[:-1])
                             if isinstance(trail, list) and trail else None),
            "longueur": len(trail) if isinstance(trail, list) else None,
        }
    return out


def _control_results(rows: list, scope: Scope) -> dict:
    resolved = rx._resolved(rows)
    out = {}
    for account_id, first, last in scope.slices:
        out[_slice_key(account_id, first, last)] = al.results_statement(
            [r for r in resolved if _in_slice(r, account_id, first, last)])
    for account_id in scope.account_ids:
        out[f"compte/{account_id}"] = al.results_statement(
            [r for r in resolved if r.get("account_id") == account_id])
    out["journal"] = al.results_statement(resolved)
    return out


def _control_dossiers(rows: list, dossier_ids: tuple) -> dict:
    out = {d: {"brut": 0, "nombre": 0} for d in dossier_ids}
    for row in rows:
        slot = out.get(row.get("dossier_id") or "")
        if slot is None:
            continue
        amount = _int_or_none(row.get("amount"))
        if amount is None:
            raise ControlImpossible(f"montant illisible sur l'écriture {row.get('id')}")
        slot["brut"] += amount
        slot["nombre"] += 1
    return out


def _control_lock(rows: list, accounts: dict, recs: list, scope: Scope) -> dict:
    reconciliations = rla._control_reconciliations(rows, accounts, recs)
    by_id = {r.get("id"): r for r in rows}
    return {"conciliations": reconciliations,
            "lock_reasons": {
                i: al._entry_lock_reason(by_id[i], rx._floor_of(
                    reconciliations, by_id[i].get("account_id") or ""))
                for i in scope.entry_ids if i in by_id}}


def _control_others(world: World, rows: list, accounts: dict, recs: list,
                    invoices: list, trust_rows: list, dossiers: list, expenses: list,
                    skip: frozenset) -> dict:
    out = rx._control_others(rows, accounts, recs, invoices, trust_rows, skip)
    for collection, docs in ((DOSSIERS, dossiers), (EXPENSES, expenses)):
        for doc in docs:
            if not doc.get("id"):
                raise ControlImpossible(f"un document de {collection} n'a pas d'identifiant")
            path = f"{collection}/{doc['id']}"
            if path not in skip:
                out[path] = rla._digest(doc)
    return out


def _figures(rows: list, scope: Scope) -> dict:
    resolved = rx._resolved(rows)
    return {
        "tranches": {_slice_key(a, f, l): al.results_statement(
            [r for r in resolved if _in_slice(r, a, f, l)]) for a, f, l in scope.slices},
        "journal": al.results_statement(resolved),
        "dossiers": {d: v["brut"] for d, v in _control_dossiers(rows, scope.dossier_ids).items()},
    }


def _control_statements(rows: list, scope: Scope) -> dict:
    """The instructions' section-5 figures: the base as read, and the same
    base with every plan entry back at the split and dossier the plan states
    it held. Their difference is what the corrections moved — in this run or
    an earlier one (a resumed run, a re-run) — never a figure of this run's
    own « before » alone. Reported, never a gate: control 3 and 4 judge."""
    stated = {entry_id: dict(zip(SPLIT + ("dossier_id",), values))
              for entry_id, *values in scope.baseline}
    reference = [({**row, **stated[row.get("id")]} if row.get("id") in stated else row)
                 for row in rows]
    return {"base": _figures(rows, scope), "reference": _figures(reference, scope)}


def compute_controls(world: World, scope: Scope, payloads: Optional[dict] = None) -> dict:
    """The six controls, on the documents read — merged with *payloads* (by
    path) for the simulation: a payload on ANY document read shows."""
    store = world.store
    rows = store.control_rows(payloads)
    accounts = store.control_accounts(payloads)
    recs = rx._merged(rx.RECS, store.recs, payloads)
    invoices = rx._merged(rx.INVOICES, store.invoices, payloads)
    trust_rows = rx._merged(rx.TRUST_TX, store.trust_rows, payloads)
    dossiers = rx._merged(DOSSIERS, [{**d, "id": i} for i, d in world.dossiers.items()],
                          payloads)
    expenses = rx._merged(EXPENSES, world.expenses, payloads)
    skip = frozenset(f"{TX}/{i}" for i in scope.entry_ids)
    if invoices is None:
        others = Impossible(f"factures illisibles ({store.invoices_error})")
    elif trust_rows is None:
        others = Impossible(f"registre du fidéicommis illisible ({store.trust_error})")
    elif expenses is None:
        others = Impossible(f"débours illisibles ({world.expenses_error})")
    else:
        others = rla._attempt(_control_others, world, rows, accounts, recs, invoices,
                              trust_rows, dossiers, expenses, skip)
    return {
        "1": rla._attempt(rx._control_ledger, rows, accounts),
        "2": rla._attempt(_control_lines, rows, scope.entry_ids),
        "3": rla._attempt(_control_results, rows, scope),
        "4": rla._attempt(_control_dossiers, rows, scope.dossier_ids),
        "5": rla._attempt(_control_lock, rows, accounts, recs, scope),
        "6": others,
        "7": rla._attempt(_control_statements, rows, scope),
    }


@dataclass(frozen=True)
class Expected:
    """What the write must move: each written entry's state ``(before,
    after)`` (control 2), the results statements' flat deltas (control 3)
    and each dossier's gross total (control 4)."""

    moves: dict
    results: dict
    dossiers: dict


def expected_effects(pairs: list, scope: Scope) -> Expected:
    """What the written lines alone move — as stored, then as their targets
    say (never as a payload says: a payload that strays from its plan must
    show in the controls)."""
    def delta(subset: list) -> dict:
        return rla._flat_results_delta(al.results_statement([b for b, _a in subset]),
                                       al.results_statement([a for _b, a in subset]))

    results = {}
    for account_id, first, last in scope.slices:
        results[_slice_key(account_id, first, last)] = delta(
            [(b, a) for b, a in pairs if _in_slice(b, account_id, first, last)])
    for account_id in scope.account_ids:
        results[f"compte/{account_id}"] = delta(
            [(b, a) for b, a in pairs if b.get("account_id") == account_id])
    results["journal"] = delta(pairs)
    dossiers = {d: 0 for d in scope.dossier_ids}
    for b, a in pairs:
        for doc, sign in ((b, -1), (a, 1)):
            if (doc.get("dossier_id") or "") in dossiers:
                dossiers[doc["dossier_id"]] += sign * int(doc.get("amount") or 0)
    moves = {b.get("id"): (state_of(b), state_of(a)) for b, a in pairs}
    return Expected(moves=moves, results=results, dossiers=dossiers)


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
            gaps.append(f"contrôle 2 — écriture {entry_id} : un autre champ que ceux de la "
                        f"cible, le fil et les estampilles a bougé")
        if (b["etat"], a["etat"]) != (old, new):
            gaps.append(f"contrôle 2 — écriture {entry_id} : {_shown_state(b['etat'])} → "
                        f"{_shown_state(a['etat'])}, attendu {_shown_state(old)} → "
                        f"{_shown_state(new)}")
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
        for key, want in expected.results.items():
            got = rla._flat_results_delta(before["3"][key], after["3"][key])
            if got != want:
                gaps.append(f"contrôle 3 ({key}) — écart {got}, attendu {want}")
    if "4" not in blocked:
        for dossier_id, want in expected.dossiers.items():
            got = after["4"][dossier_id]["brut"] - before["4"][dossier_id]["brut"]
            if got != want:
                gaps.append(f"contrôle 4 (dossier {dossier_id}) — écart {got}, attendu {want}")
    if "5" not in blocked:
        if before["5"]["conciliations"] != after["5"]["conciliations"]:
            gaps.extend(f"contrôle 5 — {d}" for d in rla._differences(
                before["5"]["conciliations"], after["5"]["conciliations"]))
        for entry_id in expected.moves:
            reasons = (before["5"]["lock_reasons"].get(entry_id),
                       after["5"]["lock_reasons"].get(entry_id))
            if reasons != (LOCKED, LOCKED):
                gaps.append(f"contrôle 5 — écriture {entry_id} : lock_reason {reasons[0]} → "
                            f"{reasons[1]}, attendu {LOCKED} avant et après")
    return gaps, impossible


# ── the plan, prepared ────────────────────────────────────────────────────


@dataclass
class Plan:
    path: Path
    sha: str
    project: str
    spec: Spec
    world: Any
    numbers: list                                       # [NumberVerdict]
    table: list = field(default_factory=list)          # comptes, dossiers, modèle
    observations: list = field(default_factory=list)   # non-blocking differences
    differences: list = field(default_factory=list)    # blocking: the untouchable
    writes: list = field(default_factory=list)
    payloads: dict = field(default_factory=dict)
    citing: dict = field(default_factory=dict)         # entry id → reconciliations citing it
    reconciliation_keys: list = field(default_factory=list)
    model_doc: Optional[dict] = None
    before: dict = field(default_factory=dict)
    simulated: dict = field(default_factory=dict)
    expected: Any = None
    gaps: list = field(default_factory=list)
    impossible: list = field(default_factory=list)
    fingerprint: str = ""

    @property
    def verdicts(self) -> list:
        return [v for n in self.numbers for v in n.verdicts]

    @property
    def scope(self) -> Scope:
        accounts = tuple(sorted(self.spec.account_ids()))
        slices = tuple((self.spec.accounts[key]["id"], date.fromisoformat(first),
                        date.fromisoformat(last)) for key, first, last, _ in self.spec.slices)
        baseline = tuple((e.entry_id, *(e.expected.get(k) for k in SPLIT),
                          e.expected.get("dossier_id")) for e in self.spec.entries)
        return Scope(tuple(e.entry_id for e in self.spec.entries), slices, accounts,
                     tuple(sorted(self.spec.dossiers)), baseline)

    @property
    def blocking(self) -> list:
        return self.differences + self.gaps + self.impossible

    def count(self, state: str) -> int:
        return sum(1 for n in self.numbers if n.state == state)


def _floors(world: World) -> dict:
    """Each account's lock floor, from the reconciliations read."""
    out = {}
    for account_id in world.store.accounts:
        floor = None
        for rec in world.store.recs_of(account_id):
            if rec.get("status") == "complétée":
                end = rla._day(rec.get("period_end"))
                if end is None:
                    raise RunRefused(f"conciliation {rec.get('id')} sans fin de période lisible")
                floor = max(floor, end) if floor else end
        out[account_id] = rla._midnight(floor) if floor else None
    return out


def _check_context(plan: Plan, floors: dict) -> None:
    """The plan's accounts, dossiers and model entry, against the base:
    reported; only the model entry's etag (an untouchable) blocks."""
    world, spec = plan.world, plan.spec
    for key, account in spec.accounts.items():
        doc = world.store.accounts.get(account["id"])
        label = f"Compte « {key} »"
        if doc is None:
            plan.table.append((label, "id", account["id"], "(introuvable)", False))
            plan.differences.append(f"compte {key} ({account['id']}) introuvable")
            continue
        shown = al.display_balance(doc.get("account_type", ""), int(doc.get("ledger_balance") or 0))
        floor = floors.get(account["id"])
        rows = (("solde affiché", rla._money(account["solde_affiche_cents"]), rla._money(shown),
                 shown == account["solde_affiche_cents"]),
                ("plancher (dernière conciliation complétée)", account["plancher"],
                 floor.date().isoformat() if floor else "aucun",
                 floor is not None and floor.date().isoformat() == account["plancher"]))
        for name, want, have, ok in rows:
            plan.table.append((label, name, want, have, ok))
            if not ok:
                plan.observations.append(f"{label}, {name} : la base porte {have}, les "
                                         f"instructions {want} — n'empêche rien")
    for dossier_id, stated in spec.dossiers.items():
        doc = world.dossiers.get(dossier_id)
        label = f"Dossier {stated['file_number']}"
        if doc is None:
            plan.table.append((label, "id", dossier_id, "(introuvable)", False))
            continue
        for name, want in stated.items():
            have = _norm(doc.get(name))
            plan.table.append((label, name, _shown(want), _shown(have), have == want))
            if have != want:
                plan.observations.append(f"{label}, {name} : la base porte {_shown(have)}, le "
                                         f"plan {_shown(want)} — refuse tout numéro qui lie ou "
                                         f"délie ce dossier")
    model = spec.model
    snap = world.store.entries.get(model["ecriture"])
    label = "Écriture modèle"
    if snap is None:
        plan.table.append((label, "id", model["ecriture"], "(introuvable)", False))
        plan.differences.append(f"écriture modèle {model['ecriture']} introuvable")
        return
    doc = {**world.store.rows[model["ecriture"]], "id": model["ecriture"]}
    plan.model_doc = doc
    for name, want in model["attendu"].items():
        have = base_value(name, doc, None)
        plan.table.append((label, name, _shown(want), _shown(have), have == want))
        if have != want:
            plan.observations.append(f"{label}, {name} : la base porte {_shown(have)}, les "
                                     f"instructions {_shown(want)} — n'empêche rien")
    plan.table.append((label, "etag (intouchable)", model["etag"], str(doc.get("etag")),
                       doc.get("etag") == model["etag"]))
    if doc.get("etag") != model["etag"]:
        plan.differences.append(f"écriture modèle : le plan attend l'etag {model['etag']}, la "
                                f"base porte {doc.get('etag')}")


def _plan_writes(plan: Plan) -> None:
    """The payloads of the entries to apply — the fields that change, stamps
    under ``writing_via("script")``, one trail entry per entry."""
    now = datetime.now(UTC)
    with provenance.writing_via("script"):
        for verdict in plan.verdicts:
            if verdict.state != PENDING:
                continue
            doc, entry = verdict.doc, verdict.entry
            business = {k: verdict.target[k] for k in BUSINESS_FIELDS if k in verdict.origin}
            changes = {k: [doc.get(k), business[k]] for k in business}
            trail = {
                "at": now,
                "via": provenance.current_via(),
                "motif": entry.motif,
                "changes": changes,
                "updated_at_before": doc.get("updated_at"),
                "source_sha256": plan.sha,
            }
            plan.payloads[entry.path] = {
                **business,
                "revisions": list(doc.get("revisions") or []) + [trail],
                **provenance.update_fields(now),
            }
            plan.writes.append(rla.Write(entry.path, TX, entry.entry_id, 0, entry.numero,
                                         verdict.snap, doc, business, changes))


def _fingerprint(head: dict, writes: list, payloads: dict, verdicts: list) -> str:
    rows = [{"numero": w.role, "chemin": w.path,
             "update_time": rla._stamp_iso(w.snap.update_time),
             "apres": {k: payloads[w.path][k] for k in sorted(w.business)}}
            for w in writes]
    states = [{"numero": v.entry.numero, "ecriture": v.entry.entry_id, "etat": v.state,
               "motifs": [[code, message] for code, message in v.reasons]}
              for v in verdicts if v.state != PENDING]
    return rla._digest({**head, "ecritures": rows, "lignes": states})[:FINGERPRINT_HEX]


def prepare(plan_path: Path) -> Plan:
    """Everything up to the write: read, judge, plan, simulate. Writes
    nothing. Raises :class:`RunRefused`."""
    plan_path = Path(plan_path)
    data, sha = rx._load_json(plan_path, "plan")
    spec = parse_plan(data)
    world = read_world()
    floors = _floors(world)
    plan = Plan(path=plan_path, sha=sha, project=str(db.project), spec=spec, world=world,
                numbers=[judge_number(n, world, spec, sha, floors) for n in spec.numbers])
    _check_context(plan, floors)
    keys: set = set()
    for verdict in plan.verdicts:
        if verdict.doc:
            citing, extra = rx._reconciliation_findings(
                world.store.recs, verdict.doc.get("account_id"), verdict.entry.entry_id)
            plan.citing[verdict.entry.entry_id] = citing
            keys |= set(extra)
    plan.reconciliation_keys = sorted(keys)
    _plan_writes(plan)
    scope = plan.scope
    plan.before = compute_controls(world, scope)
    plan.simulated = compute_controls(world, scope, plan.payloads)
    pending = [v for v in plan.verdicts if v.state == PENDING]
    plan.expected = rla._attempt(expected_effects,
                                 [(v.doc, {**v.doc, **v.target}) for v in pending], scope)
    plan.gaps, plan.impossible = compare_controls(plan.before, plan.simulated, plan.expected)
    plan.fingerprint = _fingerprint({"plan_sha256": sha, "projet": plan.project},
                                    plan.writes, plan.payloads, plan.verdicts)
    return plan


# ── backup ────────────────────────────────────────────────────────────────


def write_backup(directory: Path, plan: Plan) -> tuple[Path, str]:
    """Write, fsync, re-read and verify the backup; ``(path, sha256)``."""
    created = datetime.now(UTC)
    documents = []
    for write in plan.writes:
        payload = plan.payloads[write.path]
        documents.append({
            "chemin": write.path,
            "numero": write.role,
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
        "contexte": {"comptes": sorted(plan.spec.account_ids()),
                     "dossiers": sorted(plan.spec.dossiers),
                     "periodes": [[a, str(f), str(l)] for a, f, l in plan.scope.slices]},
        "documents": documents, "contenu_sha256": rla._digest(documents),
    }
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=1, allow_nan=False)
    path = directory / (f"ecritures-rectifiees-{created.strftime('%Y%m%dT%H%M%S%fZ')}"
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


def entry_invariants(current: dict, merged: dict, payload: dict) -> list:
    """What the merged entry must hold before its transaction commits."""
    problems = []
    for key in sorted(set(current) | set(merged), key=str):
        if key in _WRITTEN:
            continue
        if current.get(key) != merged.get(key) or (key in current) != (key in merged):
            problems.append(f"le champ {key} changerait")
    if set(payload) - set(_WRITTEN):
        problems.append("la charge porte un champ hors de la liste permise")
    state = state_of(merged)
    amount = _int_or_none(merged.get("amount"))
    split = tuple(state[k] for k in SPLIT)
    if amount is None or None in split or sum(split) != amount:
        problems.append("net + TPS + TVQ ≠ montant")
    else:
        accepted, _reason = al.validate_ventilation(merged.get("direction") or "", amount,
                                                    *split, kind=merged.get("kind") or "")
        if accepted != dict(zip(SPLIT, split)):
            problems.append("le modèle n'accepte pas la ventilation telle quelle")
    if al._entry_lock_reason(merged, None) != LOCKED:
        problems.append("l'écriture ne serait plus verrouillée")
    trail_before = current.get("revisions") or []
    trail_after = merged.get("revisions") or []
    if (not isinstance(trail_after, list) or len(trail_after) != len(trail_before) + 1
            or trail_after[:-1] != trail_before):
        problems.append("le fil ne gagnerait pas exactement une entrée")
    if merged.get("updated_via") != "script":
        problems.append("updated_via n'est pas « script »")
    return problems


def _landed(write, payload: dict) -> Optional[bool]:
    try:
        snap = db.collection(TX).document(write.doc_id).get()
    except Exception:
        return None
    return (snap.to_dict() or {}).get("etag") == payload["etag"]


def apply_entry(write, payload: dict, *, snapshot_check: bool = True) -> tuple[str, str]:
    """ONE transaction: re-read the entry (and the dossier it links), check,
    write. ``(outcome, detail)``.

    A link the write MAKES is re-read with the entry: it must exist, and —
    for the migration (*snapshot_check*) — still carry the number and title
    the payload copies. A restoration puts the saved snapshot back as it was
    (``prepare_restore`` judged that dossier's existence the same way)."""
    ref = db.collection(TX).document(write.doc_id)
    read_time = rla._stamp_iso(write.snap.update_time)
    linked = payload.get("dossier_id") if "dossier_id" in payload else None

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
        if linked:
            d_snap = db.collection(DOSSIERS).document(linked).get(transaction=txn)
            d = (d_snap.to_dict() or {}) if d_snap.exists else None
            if d is None:
                raise _Abort("le dossier à lier est introuvable")
            if snapshot_check and (d.get("file_number", ""), d.get("title", "")) != (
                    merged.get("dossier_file_number"), merged.get("dossier_title")):
                raise _Abort("le dossier ne porte plus le numéro et le titre de la passe "
                             "approuvée")
        problems = entry_invariants(current, merged, payload)
        if problems:
            raise _Abort("; ".join(problems))
        txn.update(ref, payload)

    try:
        run(db.transaction())
    except _Landed:
        return LOST_BUT_WRITTEN, "réponse perdue ; la relecture trouve l'écriture"
    except _Abort as abort:
        if _landed(write, payload) is True:
            return LOST_BUT_WRITTEN, "réponse perdue ; la relecture trouve l'écriture"
        return ABORTED, str(abort)
    except Exception as exc:
        landed = _landed(write, payload)
        if landed is True:
            return LOST_BUT_WRITTEN, type(exc).__name__
        if landed is False:
            return NOTHING, type(exc).__name__
        return UNCERTAIN, type(exc).__name__
    return WRITTEN, ""


def apply_all(writes: list, payloads: dict, *, snapshot_check: bool = True) -> list:
    """Each write in its own transaction, in plan order; the first one that
    is not written stops the run. ``[(write, outcome, detail)]``."""
    outcomes = []
    for write in writes:
        outcome, detail = apply_entry(write, payloads[write.path],
                                      snapshot_check=snapshot_check)
        outcomes.append((write, outcome, detail))
        rla._out(f"{write.role} — {write.doc_id} : {outcome}"
                 + (f" ({detail})" if detail else ""))
        if outcome not in (WRITTEN, LOST_BUT_WRITTEN):
            break
    return outcomes


def _outcome_sentence(outcomes: list, total: int) -> str:
    written = [w for w, o, _d in outcomes if o in (WRITTEN, LOST_BUT_WRITTEN)]
    lost = [w for w, o, _d in outcomes if o == LOST_BUT_WRITTEN]
    text = f"Écrit : {len(written)} écriture(s) sur {total}, une transaction chacune"
    if lost:
        text += (f" (la réponse du serveur s'était perdue pour {len(lost)} — la relecture "
                 f"confirme l'écriture)")
    text += "."
    stopped = [(w, o, d) for w, o, d in outcomes if o not in (WRITTEN, LOST_BUT_WRITTEN)]
    if stopped:
        w, o, d = stopped[0]
        text += (f" ARRÊT à {w.role} ({w.doc_id}) : {o}" + (f" — {d}" if d else "")
                 + ". Les écritures suivantes n'ont pas été tentées. Relancez à blanc : la "
                   "migration reprend où elle s'est arrêtée.")
    return text


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
    rows: list = field(default_factory=list)     # (numéro, champ, actuel, restauré)
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


def _check_backup(body: dict) -> tuple[list, Scope]:
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
                or not isinstance(document["numero"], str)
                or not isinstance(document["chemin"], str)
                or not document["chemin"].startswith(f"{TX}/")
                or not _addressable(document["chemin"].split("/", 1)[1])
                or document["chemin"] in seen):
            raise RunRefused("sauvegarde altérée : un document n'a pas la forme attendue")
        seen.add(document["chemin"])
    try:
        accounts = tuple(sorted(str(a) for a in context["comptes"]))
        dossiers = tuple(sorted(str(d) for d in context["dossiers"]))
        slices = tuple((str(a), date.fromisoformat(f), date.fromisoformat(l))
                       for a, f, l in context["periodes"])
    except (KeyError, TypeError, ValueError):
        raise RunRefused("sauvegarde altérée : contexte illisible") from None
    scope = Scope(tuple(d["chemin"].split("/", 1)[1] for d in documents), slices, accounts,
                  dossiers)
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
    world = read_world()
    plan.world = world
    plan_sha = str((body.get("plan") or {}).get("sha256") or "")
    states, current = [], {}
    for document in documents:
        numero = document["numero"]
        entry_id = document["chemin"].split("/", 1)[1]
        champs = document["champs"]
        snap = world.store.entries.get(entry_id)
        if snap is None:
            plan.refusals.append(f"{numero} : écriture {entry_id} introuvable")
            states.append(None)
            continue
        doc = {**world.store.rows[entry_id], "id": entry_id}
        current[entry_id] = (snap, doc)
        saved = rla._untyped(document["avant"])
        before = state_of(saved)
        after = state_of({**saved, **rla._untyped(document["apres"])})
        now_state = state_of(doc)
        length = rc._trail_length(doc)
        trail = doc.get("revisions")
        last = trail[-1] if isinstance(trail, list) and trail else None
        by_the_plan = (bool(plan_sha) and isinstance(last, dict)
                       and last.get("source_sha256") == plan_sha)
        at_after = all(now_state[k] == after[k] for k in champs)
        at_before = all(now_state[k] == before[k] for k in champs)
        if at_after and (doc.get("etag") == document["etag_migration"] or by_the_plan):
            # Written by this run — or by a later run of the same plan (a
            # stopped run resumed), its last trail entry still the plan's.
            if length is None or length + 1 > al._REVISIONS_CAP:
                plan.refusals.append(f"{numero} : le fil de révisions est illisible ou plein — "
                                     f"rien ne se rogne")
            elif ("dossier_id" in champs and before["dossier_id"]
                  and before["dossier_id"] not in world.dossiers):
                plan.refusals.append(f"{numero} : le dossier à rétablir {before['dossier_id']} "
                                     f"est introuvable")
            states.append(RESTORE_PENDING)
        elif at_before:
            # Never written by the run (it stopped before), or already restored.
            plan.notes.append(f"{numero} ({entry_id}) : porte déjà les valeurs sauvegardées — "
                              f"rien à restaurer")
            states.append(RESTORE_DONE)
        else:
            plan.refusals.append(f"{numero} : l'écriture {entry_id} ne porte ni la cible de la "
                                 f"migration ni les valeurs sauvegardées (etag {doc.get('etag')}, "
                                 f"migration {document['etag_migration']}) : une écriture l'a "
                                 f"suivie — rien ne se restaure par-dessus")
            states.append(None)
        for k in champs:
            plan.rows.append((numero, k, rx._v(now_state[k]), rx._v(before[k])))
    if not plan.refusals:
        if set(states) == {RESTORE_DONE}:
            plan.state = RESTORE_DONE
        elif RESTORE_DONE in states and RESTORE_PENDING in states:
            plan.state = RESTORE_PENDING   # a stopped restoration resumes
    pairs = []
    if plan.state == RESTORE_PENDING and not plan.refusals:
        now = datetime.now(UTC)
        with provenance.writing_via("script"):
            for document, state in zip(documents, states):
                if state != RESTORE_PENDING:
                    continue
                entry_id = document["chemin"].split("/", 1)[1]
                snap, doc = current[entry_id]
                saved = rla._untyped(document["avant"])
                business = {k: state_of(saved)[k] for k in document["champs"]}
                changes = {k: [doc.get(k), business[k]] for k in document["champs"]}
                trail = {
                    "at": now,
                    "via": provenance.current_via(),
                    "motif": RESTORE_MOTIF.format(quand=rla._when(plan.created),
                                                  numero=document["numero"]),
                    "changes": changes,
                    "updated_at_before": doc.get("updated_at"),
                    "source_sha256": sha,
                }
                plan.payloads[document["chemin"]] = {
                    **business, "revisions": list(doc.get("revisions") or []) + [trail],
                    **provenance.update_fields(now)}
                plan.writes.append(rla.Write(document["chemin"], TX, entry_id, 0,
                                             document["numero"], snap, doc, business, changes))
                pairs.append((doc, {**doc, **business}))
    plan.before = compute_controls(world, scope)
    plan.simulated = compute_controls(world, scope, plan.payloads)
    if plan.writes:
        plan.expected = rla._attempt(expected_effects, pairs, scope)
        gaps, plan.impossible = compare_controls(plan.before, plan.simulated, plan.expected)
        # A restoration lands on entries the migration wrote: their lock and
        # trail controls hold the same way; nothing else may move.
        plan.gaps = gaps
    plan.fingerprint = _fingerprint({"sauvegarde_sha256": sha, "projet": plan.project},
                                    plan.writes, plan.payloads, [])
    return plan


# ── after the write ───────────────────────────────────────────────────────


def after_real(scope: Scope) -> dict:
    """The controls on a fresh read, once written."""
    try:
        world = read_world()
    except RunRefused as exc:
        reason = Impossible(str(exc))
        return {key: reason for key in _CONTROLS}
    return compute_controls(world, scope)


def _post_write(scope: Scope, before: dict, simulated: dict, expected, outcomes: list) -> tuple:
    """``(real, gaps, drift)`` — the controls re-read after ANY outcome, judged
    against the moves of the entries actually written. A stopped run wrote
    only some: its totals are not the approved pass's (controls 3 and 4 and
    the drift are not compared, ``drift`` is ``None``), but every entry it
    did not write must be untouched and every one it wrote must hold its
    target (control 2), and nothing else may move (1, 5, 6)."""
    real = after_real(scope)
    written = {w.doc_id for w, o, _d in outcomes if o in (WRITTEN, LOST_BUT_WRITTEN)}
    moves = expected.moves if isinstance(expected, Expected) else {}
    if isinstance(expected, Expected) and written == set(moves):
        gaps, impossible = compare_controls(before, real, expected)
        return real, gaps + impossible, rla._differences(simulated, real)
    partial = Expected(moves={k: v for k, v in moves.items() if k in written}, results={},
                       dossiers={})
    gaps, impossible = compare_controls(before, real, partial)
    note = [f"application partielle : {len(written)} écriture(s) sur {len(moves)} — les "
            f"contrôles 3 et 4 et l'écart à la passe approuvée ne se comparent pas"]
    return real, note + gaps + impossible, None


# ── the report ────────────────────────────────────────────────────────────


def _render_trail(doc: dict) -> list:
    return rx._render_trail(doc)


def _slice_text(statement: dict) -> str:
    return (f"dépenses (brut) {rla._money(statement['total_expenses'])} ; nettes "
            f"{rla._money(sum(statement['expenses_net'].values()))} ; TPS "
            f"{rla._money(statement['gst'])} ; TVQ {rla._money(statement['qst'])}")


def _account_name(world: World, account_id: str) -> str:
    return (world.store.accounts.get(account_id) or {}).get("name") or account_id


def _render_controls(plan, before: dict, simulated: dict, real: Optional[dict]) -> list:
    world, scope = plan.world, plan.scope
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
            return (f"{rla._money(shown)} affiché (registre {rla._money(slot.get('ledger_balance'))},"
                    f" Σ {rla._money(slot.get('somme'))})")
        return fn

    def ledger(c):
        return (f"{len(c['soldes'])} compte(s) ; {len(c['soldes_courants'])} soldes courants ; "
                f"{len(c['sequences'])} séquences")

    def lines(c):
        return " ; ".join("absente" if v is None
                          else f"{_shown_state(v['etat'])} (fil {v['longueur']})"
                          for v in c.values())

    def lock(c):
        parts = []
        for account_id in scope.account_ids:
            mine = c["conciliations"].get(account_id) or {}
            proofs = mine.get("preuves") or {}
            bad = sum(1 for p in proofs.values() if p.get("ecart"))
            last = mine.get("derniere") or {}
            parts.append(f"{_account_name(world, account_id)} : plancher {mine.get('plancher')}, "
                         f"dernière {last.get('fin')} ({last.get('statut')}), {len(proofs)} "
                         f"preuve(s), {bad} en écart")
        reasons = sorted({str(r or "aucun") for r in c["lock_reasons"].values()})
        return " ; ".join(parts) + f" ; lock_reason {', '.join(reasons) or '—'}"

    rows = [row(f"1. Solde — {_account_name(world, a)}", "1", balance(a))
            for a in scope.account_ids]
    rows.append(row("1. Soldes courants, séquences", "1", ledger))
    rows.append(row("2. Les écritures du plan : état (fil)", "2", lines))
    for account_id, first, last in scope.slices:
        key = _slice_key(account_id, first, last)
        rows.append(row(f"3. {_account_name(world, account_id)}, du {first} au {last}", "3",
                        lambda c, k=key: _slice_text(c[k])))
    for account_id in scope.account_ids:
        rows.append(row(f"3. {_account_name(world, account_id)}, journal entier", "3",
                        lambda c, k=f"compte/{account_id}": _slice_text(c[k])))
    rows.append(row("3. Journal entier, tous comptes", "3", lambda c: _slice_text(c["journal"])))
    for dossier_id in scope.dossier_ids:
        number = (plan.world.dossiers.get(dossier_id) or {}).get("file_number") or dossier_id
        rows.append(row(f"4. Dossier {number} : écritures rattachées", "4",
                        lambda c, d=dossier_id: f"{rla._money(c[d]['brut'])} ({c[d]['nombre']})"))
    rows += [row("5. Verrou et conciliations", "5", lock),
             row("6. Autres documents lus", "6", lambda c: f"{len(c)} document(s)")]
    return rx._table(columns, rows)


def _statement_checks(plan: Plan, real: Optional[dict]) -> list:
    """``[(énoncé, mesure, instructions, base, ok)]`` — the instructions'
    section-5 figures beside the base's (after, simulated or real), each
    measured by control 7: the base minus the same base with every plan
    entry back at the values the plan states it held."""
    spec, world = plan.spec, plan.world
    after = real if real else plan.simulated
    which = "réel" if real else "simulé"
    seven = rx._get(after, "7")
    rows = []

    def measured(fn):
        if not isinstance(seven, dict):
            return None
        try:
            return fn(seven["reference"], seven["base"])
        except (KeyError, TypeError):
            return None

    for key, first, last, stated in spec.slices:
        k = _slice_key(spec.accounts[key]["id"], date.fromisoformat(first),
                       date.fromisoformat(last))
        delta = measured(lambda ref, base: _slice_delta(ref["tranches"][k], base["tranches"][k]))
        for name, v in stated.items():
            have = None if delta is None else delta[name]
            rows.append((f"{key}, du {first} au {last}", name, v, have, have == v))
    delta = measured(lambda ref, base: _slice_delta(ref["journal"], base["journal"]))
    for name, v in spec.whole.items():
        have = None if delta is None else delta[name]
        rows.append(("ensemble des corrections", name, v, have, have == v))
    by_number = {d["file_number"]: i for i, d in spec.dossiers.items()}
    for number, v in spec.dossier_deltas.items():
        dossier_id = by_number[number]
        have = measured(lambda ref, base: base["dossiers"][dossier_id] - ref["dossiers"][dossier_id])
        rows.append((f"dossier {number}", "écritures rattachées", v, have, have == v))
    for key, account in spec.accounts.items():
        b1, a1 = rx._get(plan.before, "1"), rx._get(after, "1")
        for label, c in (("avant", b1), (which, a1)):
            if not isinstance(c, dict):
                rows.append((f"compte « {key} », solde affiché ({label})", "solde",
                             account["solde_affiche_cents"], None, False))
                continue
            slot = c["soldes"].get(account["id"]) or {}
            acc = world.store.accounts.get(account["id"]) or {}
            shown = al.display_balance(acc.get("account_type", ""),
                                       int(slot.get("ledger_balance") or 0))
            rows.append((f"compte « {key} », solde affiché ({label})", "solde",
                         account["solde_affiche_cents"], shown,
                         shown == account["solde_affiche_cents"]))
    return rows


def _statements_vs_base(plan: Plan, real: Optional[dict]) -> list:
    which = "réel" if real else "simulé"
    rows = [(label, name, rla._money(want), rla._money(have) if have is not None else "—",
             rx._verdict(ok)) for label, name, want, have, ok in _statement_checks(plan, real)]
    return rx._table(("Énoncé des instructions (section 5)", "Mesure", "Instructions",
                      f"Base (après {which})", ""), rows)


def _render_eight(plan: Plan, gaps: list, real: Optional[dict]) -> list:
    """The instructions' eight controls (section 5), each with its result:
    the script's controls and the section-5 figures that answer it."""
    checks = _statement_checks(plan, real)
    blocked = [g for g in gaps if g.startswith("contrôle")]

    def gap_free(*keys):
        bad = [g for g in blocked if any(g.startswith(f"contrôle {k} ") for k in keys)]
        return (not bad), bad

    def figures(prefix):
        rows = [c for c in checks if c[0].startswith(prefix)]
        bad = [f"{label} ({name})" for label, name, _w, _h, ok in rows if not ok]
        return (not bad) and bool(rows), bad

    def balances():
        ok1, bad1 = gap_free("1")
        ok2, bad2 = figures("compte « ")
        return ok1 and ok2, bad1 + bad2

    slice_label = ", ".join(f"{key} du {first} au {last}"
                            for key, first, last, _ in plan.spec.slices) or "—"
    items = [
        ("1", "Soldes affichés des comptes, conformes aux instructions et inchangés ; soldes "
              "courants, montants bruts et séquences de toutes les écritures inchangés",
         balances()),
        ("2", "Chaque écriture modifiée : montant, date, date de compensation, statut, "
              "référence, catégorie, nature et contrepartie inchangés", gap_free("2")),
        ("3", f"Dépenses nettes, TPS, TVQ et brut de la tranche énoncée ({slice_label})",
         figures(tuple(f"{key}, du " for key, *_ in plan.spec.slices) or ("—",))),
        ("4", "Chaque ventilation corrigée passe de la valeur énoncée à la cible, brut "
              "inchangé", gap_free("2", "3")),
        ("5", "Ensemble des corrections : TPS, TVQ et brut", figures("ensemble")),
        ("6", "Total des écritures rattachées à chaque dossier énoncé", figures("dossier ")),
        ("7", "Plancher et dernière conciliation de chaque compte inchangés ; `lock_reason` "
              "« écriture_verrouillée » avant et après pour chaque écriture écrite",
         gap_free("5")),
        ("8", "Factures, débours, paiements, encaissements et registre du fidéicommis : aucun "
              "document modifié (le script n'écrit que des écritures d'administration et "
              "n'ouvre pas Storage : aucun PDF)", gap_free("6")),
    ]
    rows = [(n, label, rx._verdict(ok), "; ".join(bad) or "—") for n, label, (ok, bad) in items]
    return rx._table(("N°", "Contrôle des instructions", "Résultat", "Écarts"), rows)


def _render_lines_table(plan: Plan) -> list:
    world = plan.world
    rows = []
    for number in plan.numbers:
        for v in number.verdicts:
            doc = v.doc
            rows.append((
                number.number.numero + (f" ({PARTIAL})" if number.state == PARTIAL else ""),
                f"`{v.entry.entry_id}`",
                str(doc.get("sequence")) if doc else "—",
                _account_name(world, doc.get("account_id")) if doc else "—",
                rx._v(rx._aware(doc.get("date"))) if doc else "—",
                rla._money(doc.get("amount")) if doc else "—",
                (doc.get("counterparty") or "—") if doc else "—",
                v.state,
                (", ".join(f"`{k}`" for k in v.origin) or "—") if v.state == PENDING else "—",
                "; ".join(f"`{code}` — {message}" for code, message in v.reasons) or "—"))
    return rx._table(("N°", "Écriture", "Séq.", "Compte", "Jour", "Montant", "Contrepartie",
                      "État", "Champs à écrire", "Motifs"), rows)


def _render_refused_targets(plan: Plan) -> list:
    """The target each refused entry's plan line names, beside the base —
    shown, never written."""
    out = []
    for verdict in plan.verdicts:
        if verdict.state != REFUSED:
            continue
        doc, entry = verdict.doc, verdict.entry
        rows = []
        for key, want in entry.target.items():
            have = state_of(doc)[key] if doc else None
            rows.append((f"`{key}`", rx._v(have) if doc else "—", rx._v(want)))
        out += [f"### {entry.numero} — écriture `{entry.entry_id}` (séq. "
                f"{doc.get('sequence') if doc else '?'}) : cible du plan, non appliquée", ""]
        out += rx._table(("Champ", "Base", "Cible du plan"), rows)
        out.append("")
    return out


def _render_writes(plan: Plan) -> list:
    out = []
    by_path = {w.path: w for w in plan.writes}
    for verdict in plan.verdicts:
        write = by_path.get(verdict.entry.path)
        if write is None:
            continue
        payload = plan.payloads[write.path]
        revisions = payload.get("revisions")
        revisions = revisions if isinstance(revisions, list) else []
        trail = revisions[-1] if revisions and isinstance(revisions[-1], dict) else {}
        out += [f"### {write.role} — écriture `{write.doc_id}` (séq. "
                f"{write.before.get('sequence')})", ""]
        out += rx._table(("Champ", "Avant", "Après", "Origine"),
                         [(f"`{k}`", rx._v(old), rx._v(new), verdict.origin.get(k, "?"))
                          for k, (old, new) in write.changes.items()])
        out += ["", (f"Estampilles : `updated_at`, `etag`, `updated_via` = "
                     f"`{payload.get('updated_via')}` ; `revisions` : "
                     f"{len(write.before.get('revisions') or [])} → {len(revisions)}, motif "
                     f"« {trail.get('motif')} », via `{trail.get('via')}`."), ""]
    return out or ["Aucun."]


def _render_modes(plan: Plan) -> list:
    out = []
    for verdict in plan.verdicts:
        if not verdict.doc or not any(k in verdict.entry.target for k in SPLIT):
            continue
        out.append(f"- {verdict.entry.numero} (séq. {verdict.doc.get('sequence')}) : "
                   f"ventilation inscrite "
                   f"{' + '.join(rla._money(verdict.doc.get(k)) for k in SPLIT)} — aucun mode "
                   f"stocké ; elle équivaut à « {equivalent_mode(verdict.doc)} » de "
                   f"`record_admin_entry`. Aucun champ de mode n'est ajouté.")
    model = plan.model_doc
    if model is not None:
        keys = sorted(k for k in model if "ventil" in k.lower() or "mode" in k.lower())
        out.append(f"- Écriture modèle (séq. {model.get('sequence')}) : ventilation "
                   f"{' + '.join(rla._money(model.get(k)) for k in SPLIT)} sur "
                   f"{rla._money(model.get('amount'))} ; champ de mode : "
                   + (", ".join(f"`{k}`" for k in keys) if keys else "aucun")
                   + f" — elle équivaut à « {equivalent_mode(model)} ».")
    return out


def _dossier_observations(plan: Plan) -> list:
    """For each entry whose dossier link changes: the débours of the dossiers
    involved dated the entry's day or of its amount, and the copies of its
    pièce justificative filed in a dossier — READ ONLY, reported, never a
    reason to refuse (a link moves no débours and no document)."""
    world = plan.world
    out = []
    for verdict in plan.verdicts:
        doc, entry = verdict.doc, verdict.entry
        if not doc or "dossier_id" not in entry.target:
            continue
        old_id, new_id = doc.get("dossier_id") or None, entry.target["dossier_id"]
        day, amount = rla._day(doc.get("date")), _int_or_none(doc.get("amount"))
        out.append(f"### {entry.numero} — séq. {doc.get('sequence')}, "
                   f"{rla._money(amount)} du {day}")
        out.append("")
        for role, dossier_id in (("dossier retiré", old_id), ("dossier visé", new_id)):
            if not dossier_id:
                continue
            number = (world.dossiers.get(dossier_id) or {}).get("file_number") or dossier_id
            if world.expenses is None:
                out.append(f"- {role} {number} : débours illisibles ({world.expenses_error})")
                continue
            matches = sorted(
                (e for e in world.expenses
                 if e.get("dossier_id") == dossier_id
                 and (rla._day(e.get("date")) == day or e.get("amount") == amount)),
                key=lambda e: (str(rla._day(e.get("date"))), str(e.get("id"))))
            if not matches:
                out.append(f"- {role} {number} : aucun débours du {day} ni de "
                           f"{rla._money(amount)}")
            for e in matches:
                facture = (f"facturé ({e.get('invoice_id')})" if e.get("invoice_id")
                           else "non facturé")
                out.append(f"- {role} {number} : débours `{e.get('id')}` du "
                           f"{rla._day(e.get('date'))}, {rla._money(e.get('amount'))}, "
                           f"{facture} — {str(e.get('description') or '')[:120]}")
        if doc.get("receipt_storage_path"):
            try:
                copies = document_model.find_receipt_copies(entry.entry_id)
            except Exception as exc:
                out.append(f"- pièce justificative jointe ; copies au dossier illisibles "
                           f"({type(exc).__name__})")
            else:
                shown = ", ".join(f"`{c.get('id')}` au dossier "
                                  f"{c.get('dossier_file_number') or c.get('dossier_id')}"
                                  for c in copies) or "aucune"
                out.append(f"- pièce justificative jointe ; copies versées : {shown} (une copie "
                           f"reste où elle est : le lien ne déplace aucun document)")
        else:
            out.append("- aucune pièce justificative jointe à l'écriture")
        out.append("")
    return out or ["Aucun numéro ne change de dossier."]


def _render_trails(plan: Plan) -> list:
    out = []
    for verdict in plan.verdicts:
        if not verdict.doc:
            continue
        trail = verdict.doc.get("revisions") or []
        if not trail:
            continue
        out += [f"### {verdict.entry.numero} — séq. {verdict.doc.get('sequence')} "
                f"(`{verdict.entry.entry_id}`), {len(trail)} révision(s)", ""]
        out += _render_trail(verdict.doc)
        out.append("")
    return out or ["Aucune écriture du plan ne porte de révision."]


def _render_trace(plan: Plan, backup_dir: Optional[Path], report: Optional[Path],
                  backup: Optional[tuple]) -> list:
    if backup:
        where = f"`{backup[0]}` (SHA-256 `{backup[1]}`)"
    elif backup_dir is not None:
        where = (f"dans `{backup_dir}`, avant la première transaction : "
                 f"`ecritures-rectifiees-<horodatage UTC>-{plan.fingerprint}.json`")
    else:
        where = "dans le dossier que nommera `--sauvegarde`, avant la première transaction"
    return [f"- Sauvegarde : {where} — chaque écriture à écrire, entière et typée, avec "
            f"l'`update_time` lu et l'etag que la migration écrit ; relue et vérifiée avant "
            f"toute écriture ; restaurable par `--restaurer`.",
            "- Trace : ce rapport" + (f" (`{report}`)" if report else "")
            + " ; les rapports d'application, de relance et de restauration à blanc vont à "
              "côté ; chaque écriture écrite garde au fil `revisions` une entrée dont "
              f"`source_sha256` est le SHA-256 du plan (`{plan.sha}`)."]


def render_report(plan: Plan, *, mode: str, approved: Optional[str] = None,
                  refusal: Optional[list] = None, outcome: Optional[str] = None,
                  backup: Optional[tuple] = None, real: Optional[dict] = None,
                  drift: Optional[list] = None, real_gaps: Optional[list] = None,
                  findings: Optional[list] = None, backup_dir: Optional[Path] = None,
                  report: Optional[Path] = None) -> str:
    out = [
        "# Rectification d'écritures d'administration verrouillées",
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
    if plan.spec.note:
        out += ["", f"Note du plan : {plan.spec.note}"]
    out += ["", "## État", "",
            (f"- Numéros : {plan.count(PENDING)} à appliquer, {plan.count(DONE)} déjà "
             f"appliqué(s), {plan.count(REFUSED)} refusé(s), {plan.count(PARTIAL)} "
             f"partiellement appliqué(s) ; écritures à écrire : {len(plan.writes)}.")]
    blocking = plan.blocking + list(refusal or [])
    if blocking:
        out.append("- **L'application est refusée** :")
        out += [f"  - {item}" for item in dict.fromkeys(blocking)]
    elif plan.writes:
        out.append("- Rien dans la base ne s'oppose à l'application des numéros à appliquer : "
                   "les contrôles simulés sont conformes.")
    if findings:
        out.append("- **Écarts entre les instructions et le modèle réel** (section 4) :")
        out += [f"  - {item}" for item in findings]
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

    out += ["", "## 1. Numéros", ""]
    out += _render_lines_table(plan)
    out += ["", "## 2. Ce que le plan attend, et ce que porte la base", ""]
    table = [r for v in plan.verdicts for r in v.table] + plan.table
    out += rx._table(("Document", "Champ", "Attendu (plan)", "Base", ""),
                     [(d, f, w, h, rx._verdict(ok)) for d, f, w, h, ok in table]) \
        if table else ["(sans objet)"]
    if plan.observations:
        out += ["", "Observations (chacune dit son effet) :"]
        out += [f"- {item}" for item in plan.observations]

    out += ["", "## 3. Changements prévus", ""]
    out += _render_writes(plan)
    refused_targets = _render_refused_targets(plan)
    if refused_targets:
        out += ["", "Numéros refusés — rien n'est écrit pour eux :", ""] + refused_targets
    if plan.writes:
        out.append("Aucun compte, aucune conciliation, aucune facture, aucun débours, aucune "
                   "autre écriture n'est écrit ; une transaction par écriture.")

    out += ["", "## 4. Champs employés", "",
            ("- Ventilation : `net_amount`, `gst_amount`, `qst_amount` ; montant brut : "
             "`amount` (jamais écrit). Numéro de facture : `supplier_invoice_ref`. "
             "Description : `description`. Dossier : `dossier_id`, avec l'instantané "
             "`dossier_file_number` et `dossier_title` que la modification du modèle écrit "
             "avec lui (`admin_ledger.update_transaction`), vidés avec le lien."),
            ("- Provenance : `script` — `updated_via` et `via` du fil, sous "
             "`provenance.writing_via`, comme les migrations précédentes ; `updated_at` et "
             "un nouvel `etag` ; une entrée `{at, via, motif, changes, updated_at_before, "
             "source_sha256}` au fil `revisions`, `changes` donnant `[avant, après]` par champ."),
            ("- `locked` et `lock_reason` ne sont pas stockés : `lock_reason` se dérive à la "
             "lecture (`admin_ledger._entry_lock_reason`), `locked` en découle ; "
             "`revisions_count` est la longueur du fil."),
            "", "### Mode de ventilation", ""]
    out += _render_modes(plan) or ["- Aucune ligne ne touche la ventilation."]

    out += ["", "## 5. Fils de révisions existants", ""]
    out += _render_trails(plan)

    out += ["", "## 6. Verrou et conciliations", ""]
    lock_before = rx._get(plan.before, "5")
    for verdict in plan.verdicts:
        entry = verdict.entry
        shown = rx._shown(lock_before,
                          lambda c, i=entry.entry_id: c["lock_reasons"].get(i) or "aucun")
        out.append(f"- {entry.numero} (`{entry.entry_id}`) : `lock_reason` (dérivé à la "
                   f"lecture, jamais stocké) {shown} ; conciliations qui la citent : "
                   + (", ".join(plan.citing.get(entry.entry_id) or []) or "aucune") + ".")
    if plan.reconciliation_keys:
        out.append("- **Champs des conciliations hors de la forme du modèle** : "
                   + ", ".join(f"`{k}`" for k in plan.reconciliation_keys)
                   + " — une écriture qu'une telle conciliation cite est refusée (règle 2.5).")
    else:
        out.append("- Une conciliation garde des montants bruts (relevé, livre, en circulation) "
                   "et les identifiants cochés : aucune ne consigne la ventilation, le dossier, "
                   "la description ni les taxes d'une écriture. Champs des conciliations hors "
                   "de la forme du modèle : aucun.")
    out.append("- Ni plancher, ni dernière conciliation, ni conciliation n'est écrit.")

    out += ["", "## 7. Contrôles", ""]
    out += _render_controls(plan, plan.before, plan.simulated, real)
    out += ["", "Écarts : " + ("aucun" if not (plan.gaps or plan.impossible)
                               else "; ".join(plan.gaps + plan.impossible))]
    out += ["", "### Les figures de la section 5 des instructions", "",
            ("Chaque figure mesure l'effet des corrections sur la base lue : la base telle "
             "quelle, moins la même base où chaque écriture du plan reprend la ventilation et "
             "le dossier que la section 4 lui donne — une relance ou une reprise se mesure "
             "donc comme une première passe."), ""]
    out += _statements_vs_base(plan, real)
    out += ["", "### Les huit contrôles de la section 5", ""]
    out += _render_eight(plan, list(real_gaps) if real is not None and real_gaps is not None
                         else plan.gaps + plan.impossible, real)

    out += ["", "## 8. Caches", "",
            ("Aucun à recalculer : les totaux de TPS, de TVQ, de dépenses par catégorie, par "
             "période et par dossier se calculent à la lecture (`results_statement`, "
             "`models/financial_statements`) ; le seul solde dénormalisé, "
             "`admin_accounts.ledger_balance`, ne bouge pas (aucun montant ne change). Les "
             "états financiers classent en coûts directs toute dépense liée à un dossier : un "
             "lien ajouté ou retiré y déplace la dépense entre coûts directs, variables et "
             "fixes, à la lecture, sans document écrit.")]
    out += ["", "## 9. Liens de dossier : débours et pièces (observations)", ""]
    out += _dossier_observations(plan)
    out += ["", "## 10. Sauvegarde et trace", ""]
    out += _render_trace(plan, backup_dir, report, backup)
    out += ["", "## 11. Suite", ""]
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
                          real: Optional[dict] = None, drift: Optional[list] = None,
                          real_gaps: Optional[list] = None) -> str:
    out = [
        "# Restauration d'écritures d'administration rectifiées",
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
    out += rx._table(("N°", "Champ", "Actuel", "Restauré"), plan.rows) if plan.rows else ["—"]
    if plan.scope is not None and plan.world is not None:
        out += ["", "## Contrôles", ""]
        out += _render_controls(plan, plan.before, plan.simulated, real)
        out += ["", "Écarts : " + ("aucun" if not (plan.gaps or plan.impossible)
                                   else "; ".join(plan.gaps + plan.impossible))]
    return "\n".join(out) + "\n"


# ── the command line ──────────────────────────────────────────────────────


def _summary(plan: Plan, mode: str) -> list:
    return [
        f"Rectification d'écritures d'administration — {mode}",
        f"Projet : {plan.project}",
        f"Plan : {plan.path} (SHA-256 {plan.sha})",
        (f"Numéros : {plan.count(PENDING)} à appliquer, {plan.count(DONE)} déjà appliqué(s), "
         f"{plan.count(REFUSED)} refusé(s), {plan.count(PARTIAL)} partiellement appliqué(s) ; "
         f"écritures à écrire : {len(plan.writes)}"),
        "Contrôles simulés : " + ("conformes" if not plan.gaps and not plan.impossible else
                                  f"{len(plan.gaps)} écart(s), {len(plan.impossible)} "
                                  f"impossible(s)"),
        f"Empreinte : {plan.fingerprint}",
    ]


def model_findings(plan: Plan) -> list:
    """The differences between the plan's words and the model's — the
    lawyer's rule: reported in the dry run, never adapted to in silence."""
    out = [("noms de champs : les instructions emploient les noms du connecteur "
            "(`amount_cents`, `net_amount_cents`, `gst_amount_cents`, `qst_amount_cents`) ; "
            "le modèle stocke `amount`, `net_amount`, `gst_amount`, `qst_amount` (mêmes "
            "montants en cents) — le plan emploie les noms du modèle"),
           ("`locked`, `lock_reason` et `revisions_count` ne sont pas stockés : ils se "
            "dérivent à la lecture — rien à écrire, ni à garder"),
           ("`lock_floor` et `last_reconciliation_date` ne sont pas stockés sur le compte : "
            "ils se dérivent des conciliations complétées (`admin_ledger._read_lock_floor`), "
            "que le script n'écrit pas"),
           ("aucun mode de ventilation n'est stocké (seulement `net_amount`, `gst_amount`, "
            "`qst_amount`) : aucun champ n'est ajouté")]
    extra = {}
    for verdict in plan.verdicts:
        for key, origin in verdict.origin.items():
            if origin == "règle du modèle":
                extra.setdefault(key, []).append(verdict.entry.numero)
    for key, numbers in extra.items():
        out.append(f"`{key}` : le modèle l'écrit avec `dossier_id` (instantané du dossier, "
                   f"`update_transaction`), les instructions ne le nomment pas pour "
                   f"{', '.join(dict.fromkeys(numbers))}")
    return out


def _run_migration(report: Path, source: Path, backup_dir: Optional[Path],
                   approved: Optional[str]) -> int:
    try:
        plan = prepare(source)
    except RunRefused as exc:
        return rla._finish(report, f"# Rectification d'écritures d'administration\n\n- Date : "
                                   f"{rla._now_line()}\n- Plan : `{source}`\n\n**Refusé — rien "
                                   f"n'a été écrit** : {exc}\n",
                           [f"Refusé — rien n'a été écrit : {exc}"], 1)
    findings = model_findings(plan)
    if approved is None:
        summary = _summary(plan, "à blanc")
        text = render_report(plan, mode="à blanc", findings=findings, backup_dir=backup_dir,
                         report=report)
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
                                                 findings=findings, backup_dir=backup_dir,
                         report=report), summary, 1)
    if not plan.writes:
        summary = _summary(plan, "application") + ["Rien à écrire."]
        return rla._finish(report, render_report(plan, mode="application", approved=approved,
                                                 outcome="Rien à écrire.", findings=findings, backup_dir=backup_dir,
                         report=report),
                           summary, 0)
    try:
        backup = write_backup(backup_dir, plan)
    except Exception as exc:
        message = f"sauvegarde impossible ({type(exc).__name__}) — rien n'a été écrit"
        return rla._finish(report, render_report(plan, mode="application refusée",
                                                 approved=approved, refusal=[message],
                                                 findings=findings, backup_dir=backup_dir,
                         report=report),
                           _summary(plan, "application refusée") + [f"Refusé : {message}"], 1)
    rla._out(f"Sauvegarde : {backup[0]}")
    outcomes = apply_all(plan.writes, plan.payloads)
    sentence = _outcome_sentence(outcomes, len(plan.writes))
    # Said at once: whatever happens next, the outcome is never lost.
    rla._out(sentence)
    summary = _summary(plan, "application") + [f"Sauvegarde : {backup[0]}"]
    real = drift = real_gaps = None
    if outcomes:
        real, real_gaps, drift = _post_write(plan.scope, plan.before, plan.simulated,
                                             plan.expected, outcomes)
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
                         drift=drift, real_gaps=real_gaps, findings=findings, backup_dir=backup_dir,
                         report=report)
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
    outcomes = apply_all(plan.writes, plan.payloads, snapshot_check=False)
    sentence = _outcome_sentence(outcomes, len(plan.writes))
    rla._out(sentence)
    real = drift = real_gaps = None
    if outcomes:
        real, real_gaps, drift = _post_write(plan.scope, plan.before, plan.simulated,
                                             plan.expected, outcomes)
        head.append("Contrôles après réel : " + ("conformes" if not real_gaps
                                                 else f"{len(real_gaps)} écart(s)"))
    text = render_restore_report(plan, mode="restauration", approved=approved,
                                 outcome=sentence, real=real, drift=drift, real_gaps=real_gaps)
    done = all(o in (WRITTEN, LOST_BUT_WRITTEN) for _w, o, _d in outcomes) and len(
        outcomes) == len(plan.writes)
    return rla._finish(report, text, head, 0 if done and not real_gaps else 1)


def main(argv: Optional[list] = None) -> int:
    parser = rla._Parser(prog="python -m scripts.rectify_admin_entries",
                         description="Rectification d'écritures d'administration verrouillées "
                                     "(hors modèle, à blanc par défaut).")
    parser.add_argument("plan", nargs="?", help="le plan JSON approuvé (hors du dépôt public)")
    parser.add_argument("--rapport", metavar="RAPPORT.md",
                        help="le rapport à écrire (hors du dépôt ; nouveau fichier)")
    parser.add_argument("--sauvegarde", metavar="DOSSIER",
                        help="le dossier où écrire la sauvegarde avant la première transaction "
                             "(hors du dépôt)")
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
