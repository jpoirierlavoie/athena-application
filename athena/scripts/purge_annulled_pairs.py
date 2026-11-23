"""Purge, OUTSIDE the models, four annulled accounting register pairs from the
2026-11-23 journal verification — a one-shot migration bound to its plan.

What it does
------------
A journal verification corrected four erroneous register entries by reversal
(contre-passation), leaving four ``annulée`` pairs (ten documents total: two
in trust, six in Wealthsimple, two on the corporate credit card; one pair is a
two-leg card payment whose original and reversal each have two legs). The
correct replacement entries already stand in the registers. On the lawyer's
instruction, this script removes ONLY the ten named documents from the
registers — preserving sequence gaps (never renumbering or decrementing
``counters/*``) — and applies the three derived consequences atomically:

* **Pair P1 (trust):** deletes the original and its reversal, recomputes the
  frozen ``balance_after_account`` on every subsequent entry whose running
  balance moves, rewrites the replacement entry's description as specified by
  the plan, and records one ``revisions`` trail entry (``via = "script"``,
  keeping ``updated_at_before`` so ``scripts/verify_trust_integrity`` check 6
  continues to see the original clearing instant) on every modified trust row.
  The dossier document is left untouched.
* **Pair P2 (operations):** deletes the original invoice receipt and its
  reversal, then recomputes the target invoice's ``amount_paid``, ``paid_date``
  and status through ``models.invoice.payment_updates`` from the remaining
  standing receipts in the administration ledger (never hardcoded).
* **Pairs P3 & P4 (operations / credit card):** deletes each pair atomically (all
  four legs of the card payment P4 in a single Firestore transaction).
* **Permanent audit trail (RLRQ c. B-1, r. 5, art. 30):** writes ONE
  ``audit_events`` document per purged pair (``entity_type =
  "register_pair_purge"``, hidden from the MCP connector via
  ``audit_event.CONNECTOR_HIDDEN_ENTITY_TYPES``) inside the pair's own atomic
  transaction, recording the deleted IDs, accounts, sequences, amounts, dates,
  motif, provenance (``"script"``), timestamp, and backup path + SHA-256.
* **Replay journal:** after the JSON backup is written and verified, deletes
  the named ``mcp_idempotency`` records and any other replay entry referencing
  one of the deleted IDs.

Why it writes outside the models (CLAUDE.md « One door per rule »)
------------------------------------------------------------------
Trust entries are append-only, locked administration entries (including
``paiement_carte`` legs and reversed rows) refuse deletion in
``models/admin_ledger``, and ``invoice.payment_updates`` has no standalone
re-derivation door for ``paid_date`` after a partial reversal. This script is a
declared exception (mirroring ``scripts/reclassify_as_invoice_receipt`` and
``scripts/rectify_admin_entries``):

* every modified document gets ``updated_at``, a fresh ``etag`` and
  ``updated_via = "script"`` under ``provenance.writing_via("script")``;
* every modified trust entry appends a ``revisions`` entry carrying
  ``updated_at_before`` and ``source_sha256``;
* accounting registers and invoices are not DAV-exposed: no CTag is bumped;
* all production IDs, expected values, amounts, and keys stay strictly in the
  external JSON plan (never committed to the public repository).

Running it — from ``athena/``, with ADC and ``.env``: even the dry run reads
PRODUCTION. A plan, report, backup or backup directory inside the repository is
refused before anything is read or created; the report must be a new file.

    python -m scripts.purge_annulled_pairs PLAN.json --rapport R.md
    python -m scripts.purge_annulled_pairs PLAN.json --rapport R.md \\
        --sauvegarde DOSSIER --appliquer EMPREINTE
    python -m scripts.purge_annulled_pairs --restaurer SAUVEGARDE.json \\
        --rapport R.md [--appliquer EMPREINTE]

Afterwards, run both integrity scripts (``verify_admin_integrity``,
``verify_trust_integrity``) and use the generated ``--revue`` entry for the
documented trust numbering gaps.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from google.api_core import exceptions as gexc
from google.cloud import firestore

_ATHENA_DIR = Path(__file__).resolve().parents[1]
if str(_ATHENA_DIR) not in sys.path:
    sys.path.insert(0, str(_ATHENA_DIR))


def _load_env() -> str:
    """Load a ``.env`` if present without overriding existing environment vars."""
    if os.environ.get("ATHENA_SKIP_DOTENV"):
        return ""
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return ""
    path = find_dotenv(usecwd=True)
    if path:
        load_dotenv(path)
    return path or ""


_ENV_PATH = _load_env()

from mcp import tools as mcp_tools  # noqa: E402
from models import admin_ledger as al  # noqa: E402
from models import audit_event as audit_event_model  # noqa: E402
from models import db, provenance, trust  # noqa: E402
from models import dossier as dossier_model  # noqa: E402
from models import financial_statements as fs  # noqa: E402
from models import invoice as invoice_model  # noqa: E402
from scripts import findings_review  # noqa: E402
from scripts import reclassify_admin_ledger as rla  # noqa: E402
from scripts import reclassify_as_invoice_receipt as rx  # noqa: E402
from scripts import verify_trust_integrity as vti  # noqa: E402
from scripts.rectify_registers import _addressable  # noqa: E402
from security import sanitize  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils import deadlines  # noqa: E402
from utils import periods  # noqa: E402

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[2]

TRUST_ACCOUNTS = trust.ACCOUNTS_COLLECTION
TRUST_TX = trust.TRANSACTIONS_COLLECTION
TRUST_RECS = trust.RECONCILIATIONS_COLLECTION
ADMIN_ACCOUNTS = al.ACCOUNTS_COLLECTION
ADMIN_TX = al.TRANSACTIONS_COLLECTION
ADMIN_RECS = al.RECONCILIATIONS_COLLECTION
INVOICES = invoice_model.COLLECTION
DOSSIERS = dossier_model.COLLECTION
COUNTERS = trust.COUNTERS_COLLECTION
AUDIT_EVENTS = audit_event_model.COLLECTION
MCP_IDEMPOTENCY = "mcp_idempotency"

ENTITY_TYPE = "register_pair_purge"
BACKUP_FORMAT = "athena.paires-annulees-purgees/1"
RESTORE_MOTIF = "Restauration de la sauvegarde du {quand} — {paire}"
FINGERPRINT_HEX = 16
MAX_TRAIL_BEFORE = al._REVISIONS_CAP - 2
TEXT_MAX = 2000

PENDING, DONE, REFUSED = "à appliquer", "déjà appliquée", "refusée"
RESTORE_PENDING, RESTORE_DONE = rx.RESTORE_PENDING, rx.RESTORE_DONE
WRITTEN, LOST_BUT_WRITTEN, NOTHING, UNCERTAIN = (
    "écrite",
    "écrite (réponse perdue)",
    "non écrite",
    "incertaine",
)

_SCAN_COLLECTIONS = (
    TRUST_TX,
    ADMIN_TX,
    TRUST_RECS,
    ADMIN_RECS,
    INVOICES,
    DOSSIERS,
    "expenses",
    "timeentries",
    "documents",
    "notes",
    MCP_IDEMPOTENCY,
)

RunRefused = rla.RunRefused
Impossible = rla.Impossible


# ── Pure helpers ──────────────────────────────────────────────────────────


def _int_strict(value: Any, where: str, *, minimum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RunRefused(f"plan mal formé : « {where} » doit être un entier")
    if minimum is not None and value < minimum:
        raise RunRefused(f"plan mal formé : « {where} » doit être ≥ {minimum}")
    return value


def _str_nonempty(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise RunRefused(
            f"plan mal formé : « {where} » doit être un texte non vide sans espaces en bordure"
        )
    return value


def _opt_str(value: Any, where: str) -> Optional[str]:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise RunRefused(f"plan mal formé : « {where} » doit être un texte ou null")
    return value


def _day_parsed(value: Any, where: str) -> date:
    parsed = rla._day(rx._plan_value("date", value, where)) if value is not None else None
    if parsed is None:
        raise RunRefused(f"plan mal formé : « {where} » doit être un jour AAAA-MM-JJ")
    return parsed


def _opt_day_parsed(value: Any, where: str) -> Optional[date]:
    if value is None or value == "":
        return None
    return _day_parsed(value, where)


def _norm_id(value: Any) -> Optional[str]:
    return None if value is None or value == "" else str(value)


def _doc_day_str(value: Any) -> Optional[str]:
    day = rla._day(value)
    return day.isoformat() if day is not None else None


def _contains_any_id(node: Any, ids: set[str]) -> list[str]:
    """Return sorted deleted IDs found anywhere inside *node* (as exact or substring)."""
    found: set[str] = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, str):
            for tid in ids:
                if tid in cur:
                    found.add(tid)
        elif isinstance(cur, dict):
            for k, v in cur.items():
                stack.append(k)
                stack.append(v)
        elif isinstance(cur, (list, tuple)):
            stack.extend(cur)
    return sorted(found)


def _rewrite_description(
    desc: Any, start_marker: str, end_inclusive: str, capitalize_word: str
) -> str:
    """Remove segment [start_marker .. end_inclusive] and capitalize opening word."""
    if not isinstance(desc, str) or not desc:
        raise ValueError("description vide ou illisible")
    count = desc.count(start_marker)
    if count != 1:
        raise ValueError(
            f"le segment « {start_marker} » apparaît {count} fois (1 fois attendu)"
        )
    idx = desc.index(start_marker)
    end_idx = desc.find(end_inclusive, idx + len(start_marker))
    if end_idx == -1:
        raise ValueError(
            f"marqueur de fin « {end_inclusive} » introuvable après « {start_marker} »"
        )
    prefix = desc[:idx]
    rest = desc[end_idx + len(end_inclusive):]
    if not rest.startswith(capitalize_word):
        raise ValueError(
            f"le texte suivant ne commence pas par « {capitalize_word} »"
        )
    cap = capitalize_word[0].upper() + capitalize_word[1:]
    new_desc = prefix + cap + rest[len(capitalize_word):]
    if len(new_desc) > TEXT_MAX or sanitize(new_desc, max_length=TEXT_MAX) != new_desc:
        raise ValueError("la nouvelle description serait altérée par sanitize()")
    if new_desc == desc:
        raise ValueError("la nouvelle description est identique à l'ancienne")
    return new_desc


def _period_from_bounds(first: date, last: date) -> periods.Period:
    """Build a ``periods.Period`` for a calendar month, quarter, or year."""
    for kind in periods.KINDS:
        if kind == "mois":
            cand = periods.Period("mois", first.year, first.month)
        elif kind == "trimestre":
            cand = periods.Period("trimestre", first.year, (first.month - 1) // 3 + 1)
        else:
            cand = periods.Period("annee", first.year, 1)
        if cand.start == first and cand.end == last:
            return cand
    raise RunRefused(
        "plan mal formé : « periode_etats » doit couvrir un mois, un trimestre ou une année"
    )


def _candidate_idempotency_doc_ids(keys: list[str]) -> dict[str, str]:
    """Map ``sha256(tool:key)`` doc IDs back to the matching named key."""
    out: dict[str, str] = {}
    for key in keys:
        for tool in sorted(mcp_tools.WRITE_TOOLS):
            doc_id = hashlib.sha256(f"{tool}:{key}".encode("utf-8")).hexdigest()
            out[doc_id] = f"{key} (clé directe {tool})"
            prefix = f"{tool}-"
            if key.startswith(prefix):
                suffix = key[len(prefix):]
                doc_id_suffix = hashlib.sha256(
                    f"{tool}:{suffix}".encode("utf-8")
                ).hexdigest()
                out[doc_id_suffix] = f"{key} (préfixe {tool}- retiré)"
    return out


# ── Plan parsing ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AccountSpec:
    alias: str
    id: str
    registre: str
    nom_court: str
    solde_attendu_cents: int
    plancher_attendu: Optional[date]
    derniere_conciliation_attendue: Optional[date]
    delta_nombre_ecritures: int
    intouchable: bool


@dataclass(frozen=True)
class DocDeleteSpec:
    role: str
    id: str
    account_id: str
    sequence: int
    date: date
    nature: str
    direction: str
    amount: int
    status: str
    reverses_id: Optional[str]
    reversed_by_id: Optional[str]
    related_transaction_id: Optional[str]
    dossier_file_number: Optional[str] = None
    client_id: Optional[str] = None
    invoice_id: Optional[str] = None
    invoice_number: Optional[str] = None


@dataclass(frozen=True)
class PairSpec:
    code: str
    registre: str
    documents: tuple[DocDeleteSpec, ...]
    corrections_derivees: dict


@dataclass(frozen=True)
class ReplacementSpec:
    registre: str
    id: str
    sequence: int
    date: date
    amount: int
    account_id: Optional[str] = None
    client_id: Optional[str] = None
    cleared_date: Optional[date] = None
    related_transaction_id: Optional[str] = None
    remarque: str = ""


@dataclass(frozen=True)
class PlanSpec:
    motif: str
    note: str
    date_reference: date
    period: periods.Period
    accounts: dict[str, AccountSpec]
    pairs: tuple[PairSpec, ...]
    replacements: tuple[ReplacementSpec, ...]
    fideicommis_by_dossier: dict[str, int]
    carte_client_vide: dict[str, str]
    factures_intouchables: tuple[str, ...]
    idempotency_keys: tuple[str, ...]

    @property
    def all_deleted_ids(self) -> set[str]:
        return {d.id for p in self.pairs for d in p.documents}


def _parse_accounts(raw: Any) -> dict[str, AccountSpec]:
    if not isinstance(raw, dict) or not raw:
        raise RunRefused("plan mal formé : « comptes » doit être un objet non vide")
    out: dict[str, AccountSpec] = {}
    seen_ids: set[str] = set()
    allowed = {
        "id",
        "registre",
        "nom_court",
        "solde_attendu_cents",
        "plancher_attendu",
        "derniere_conciliation_attendue",
        "delta_nombre_ecritures",
        "intouchable",
    }
    required = {"id", "registre", "nom_court", "solde_attendu_cents", "delta_nombre_ecritures"}
    for alias, item in raw.items():
        where = f"comptes.{alias}"
        if not isinstance(item, dict):
            raise RunRefused(f"plan mal formé : « {where} » doit être un objet")
        missing = required - set(item)
        extra = set(item) - allowed
        if missing or extra:
            raise RunRefused(
                f"plan mal formé : « {where} » — clés invalides ({sorted(missing | extra)})"
            )
        aid = _str_nonempty(item["id"], f"{where}.id")
        if not _addressable(aid) or aid in seen_ids:
            raise RunRefused(f"plan mal formé : « {where}.id » invalide ou en double")
        seen_ids.add(aid)
        reg = item["registre"]
        if reg not in ("fideicommis", "administration"):
            raise RunRefused(f"plan mal formé : « {where}.registre » inconnu ({reg!r})")
        intouchable = bool(item.get("intouchable", False))
        delta = _int_strict(item["delta_nombre_ecritures"], f"{where}.delta_nombre_ecritures")
        if delta > 0:
            raise RunRefused(f"plan mal formé : « {where}.delta_nombre_ecritures » doit être ≤ 0")
        out[str(alias)] = AccountSpec(
            alias=str(alias),
            id=aid,
            registre=reg,
            nom_court=_str_nonempty(item["nom_court"], f"{where}.nom_court"),
            solde_attendu_cents=_int_strict(
                item["solde_attendu_cents"], f"{where}.solde_attendu_cents"
            ),
            plancher_attendu=_opt_day_parsed(
                item.get("plancher_attendu"), f"{where}.plancher_attendu"
            ),
            derniere_conciliation_attendue=_opt_day_parsed(
                item.get("derniere_conciliation_attendue"),
                f"{where}.derniere_conciliation_attendue",
            ),
            delta_nombre_ecritures=delta,
            intouchable=intouchable,
        )
    return out


def _parse_doc_delete(raw: Any, where: str, known_accounts: set[str]) -> DocDeleteSpec:
    if not isinstance(raw, dict):
        raise RunRefused(f"plan mal formé : « {where} » doit être un objet")
    required = {
        "role",
        "id",
        "account_id",
        "sequence",
        "date",
        "nature",
        "direction",
        "amount",
        "status",
        "reverses_id",
        "reversed_by_id",
        "related_transaction_id",
    }
    optional = {"dossier_file_number", "client_id", "invoice_id", "invoice_number"}
    missing = required - set(raw)
    extra = set(raw) - (required | optional)
    if missing or extra:
        raise RunRefused(
            f"plan mal formé : « {where} » — clés invalides ({sorted(missing | extra)})"
        )
    did = _str_nonempty(raw["id"], f"{where}.id")
    if not _addressable(did):
        raise RunRefused(f"plan mal formé : « {where}.id » invalide")
    aid = _str_nonempty(raw["account_id"], f"{where}.account_id")
    if aid not in known_accounts:
        raise RunRefused(f"plan mal formé : « {where}.account_id » hors de « comptes »")
    direction = raw["direction"]
    if direction not in ("recette", "déboursé"):
        raise RunRefused(f"plan mal formé : « {where}.direction » invalide")
    if raw["status"] != "annulée":
        raise RunRefused(f"plan mal formé : « {where}.status » doit être « annulée »")
    return DocDeleteSpec(
        role=_str_nonempty(raw["role"], f"{where}.role"),
        id=did,
        account_id=aid,
        sequence=_int_strict(raw["sequence"], f"{where}.sequence", minimum=1),
        date=_day_parsed(raw["date"], f"{where}.date"),
        nature=_str_nonempty(raw["nature"], f"{where}.nature"),
        direction=direction,
        amount=_int_strict(raw["amount"], f"{where}.amount", minimum=1),
        status=raw["status"],
        reverses_id=_opt_str(raw["reverses_id"], f"{where}.reverses_id"),
        reversed_by_id=_opt_str(raw["reversed_by_id"], f"{where}.reversed_by_id"),
        related_transaction_id=_opt_str(
            raw["related_transaction_id"], f"{where}.related_transaction_id"
        ),
        dossier_file_number=_opt_str(
            raw.get("dossier_file_number"), f"{where}.dossier_file_number"
        ),
        client_id=_opt_str(raw.get("client_id"), f"{where}.client_id"),
        invoice_id=_opt_str(raw.get("invoice_id"), f"{where}.invoice_id"),
        invoice_number=_opt_str(raw.get("invoice_number"), f"{where}.invoice_number"),
    )


def _parse_pairs(raw: Any, known_accounts: set[str]) -> tuple[PairSpec, ...]:
    if not isinstance(raw, list) or not raw:
        raise RunRefused("plan mal formé : « paires » doit être une liste non vide")
    pairs: list[PairSpec] = []
    seen_codes: set[str] = set()
    seen_ids: set[str] = set()
    for i, item in enumerate(raw):
        where = f"paires[{i}]"
        if not isinstance(item, dict):
            raise RunRefused(f"plan mal formé : « {where} » doit être un objet")
        allowed = {"code", "registre", "documents", "corrections_derivees"}
        if not {"code", "registre", "documents"} <= set(item) or set(item) - allowed:
            raise RunRefused(f"plan mal formé : « {where} » — clés invalides")
        code = _str_nonempty(item["code"], f"{where}.code")
        if code in seen_codes:
            raise RunRefused(f"plan mal formé : code de paire « {code} » en double")
        seen_codes.add(code)
        reg = item["registre"]
        if reg not in ("fideicommis", "administration"):
            raise RunRefused(f"plan mal formé : « {where}.registre » inconnu")
        raw_docs = item["documents"]
        if not isinstance(raw_docs, list) or len(raw_docs) not in (2, 4):
            raise RunRefused(
                f"plan mal formé : « {where}.documents » doit compter 2 ou 4 documents"
            )
        docs = []
        for j, d in enumerate(raw_docs):
            spec = _parse_doc_delete(d, f"{where}.documents[{j}]", known_accounts)
            if spec.id in seen_ids:
                raise RunRefused(f"plan mal formé : document « {spec.id} » cité deux fois")
            seen_ids.add(spec.id)
            docs.append(spec)
        corr = item.get("corrections_derivees") or {}
        if not isinstance(corr, dict):
            raise RunRefused(f"plan mal formé : « {where}.corrections_derivees » invalide")
        pairs.append(
            PairSpec(code=code, registre=reg, documents=tuple(docs), corrections_derivees=corr)
        )
    return tuple(pairs)


def _parse_replacements(raw: Any) -> tuple[ReplacementSpec, ...]:
    if not isinstance(raw, list):
        raise RunRefused("plan mal formé : « ecritures_remplacement » doit être une liste")
    out: list[ReplacementSpec] = []
    for i, item in enumerate(raw):
        where = f"ecritures_remplacement[{i}]"
        if not isinstance(item, dict):
            raise RunRefused(f"plan mal formé : « {where} » doit être un objet")
        reg = item.get("registre")
        if reg not in ("fideicommis", "administration"):
            raise RunRefused(f"plan mal formé : « {where}.registre » inconnu")
        rid = _str_nonempty(item.get("id"), f"{where}.id")
        if not _addressable(rid):
            raise RunRefused(f"plan mal formé : « {where}.id » invalide")
        out.append(
            ReplacementSpec(
                registre=reg,
                id=rid,
                sequence=_int_strict(item.get("sequence"), f"{where}.sequence", minimum=1),
                date=_day_parsed(item.get("date"), f"{where}.date"),
                amount=_int_strict(item.get("amount"), f"{where}.amount", minimum=1),
                account_id=_opt_str(item.get("account_id"), f"{where}.account_id"),
                client_id=_opt_str(item.get("client_id"), f"{where}.client_id"),
                cleared_date=_opt_day_parsed(
                    item.get("cleared_date"), f"{where}.cleared_date"
                ),
                related_transaction_id=_opt_str(
                    item.get("related_transaction_id"),
                    f"{where}.related_transaction_id",
                ),
                remarque=str(item.get("remarque") or ""),
            )
        )
    return tuple(out)


def parse_plan(data: dict) -> PlanSpec:
    required = {
        "motif",
        "date_reference",
        "periode_etats",
        "comptes",
        "paires",
        "ecritures_remplacement",
        "controles",
        "cles_idempotence",
    }
    optional = {"note"}
    missing = required - set(data)
    extra = set(data) - (required | optional)
    if missing or extra:
        raise RunRefused(
            f"plan mal formé : clés invalides ({sorted(missing | extra)})"
        )
    motif = _str_nonempty(data["motif"], "motif")
    note = str(data.get("note") or "")
    ref_date = _day_parsed(data["date_reference"], "date_reference")
    per = data["periode_etats"]
    if not isinstance(per, dict) or set(per) != {"du", "au"}:
        raise RunRefused("plan mal formé : « periode_etats » doit contenir du et au")
    first = _day_parsed(per["du"], "periode_etats.du")
    last = _day_parsed(per["au"], "periode_etats.au")
    if first > last:
        raise RunRefused("plan mal formé : « periode_etats » incohérente")
    period = _period_from_bounds(first, last)

    accounts = _parse_accounts(data["comptes"])
    known_ids = {a.id for a in accounts.values()}
    pairs = _parse_pairs(data["paires"], known_ids)
    replacements = _parse_replacements(data["ecritures_remplacement"])

    ctrl = data["controles"]
    if not isinstance(ctrl, dict) or set(ctrl) != {
        "fideicommis_by_dossier",
        "carte_client_vide",
        "factures_intouchables",
    }:
        raise RunRefused("plan mal formé : « controles » invalide")
    by_dos = ctrl["fideicommis_by_dossier"]
    if not isinstance(by_dos, dict):
        raise RunRefused("plan mal formé : « controles.fideicommis_by_dossier » invalide")
    fidei_by_dos = {
        _str_nonempty(k, f"fideicommis_by_dossier.{k}"): _int_strict(
            v, f"fideicommis_by_dossier.{k}"
        )
        for k, v in by_dos.items()
    }
    ccv = ctrl["carte_client_vide"]
    if not isinstance(ccv, dict) or set(ccv) != {"dossier_file_number", "client_id"}:
        raise RunRefused("plan mal formé : « controles.carte_client_vide » invalide")
    carte_vide = {
        "dossier_file_number": _str_nonempty(
            ccv["dossier_file_number"], "carte_client_vide.dossier_file_number"
        ),
        "client_id": _str_nonempty(ccv["client_id"], "carte_client_vide.client_id"),
    }
    untouchable_invs = ctrl["factures_intouchables"]
    if not isinstance(untouchable_invs, list):
        raise RunRefused("plan mal formé : « controles.factures_intouchables » invalide")
    factures_intouchables = tuple(
        _str_nonempty(x, f"factures_intouchables[{i}]")
        for i, x in enumerate(untouchable_invs)
    )
    raw_keys = data["cles_idempotence"]
    if not isinstance(raw_keys, list):
        raise RunRefused("plan mal formé : « cles_idempotence » doit être une liste")
    idempotency_keys = tuple(
        _str_nonempty(x, f"cles_idempotence[{i}]") for i, x in enumerate(raw_keys)
    )
    return PlanSpec(
        motif=motif,
        note=note,
        date_reference=ref_date,
        period=period,
        accounts=accounts,
        pairs=pairs,
        replacements=replacements,
        fideicommis_by_dossier=fidei_by_dos,
        carte_client_vide=carte_vide,
        factures_intouchables=factures_intouchables,
        idempotency_keys=idempotency_keys,
    )


# ── Complete strict Firestore read ────────────────────────────────────────


@dataclass
class World:
    docs: dict[str, dict[str, dict]]         # collection -> {id: dict}
    snaps: dict[str, dict[str, Any]]         # collection -> {id: DocumentSnapshot}
    invoice_lines: dict[str, list[dict]]     # invoice_id -> list of lineitem dicts
    expenses: list[dict]
    time_entries: list[dict]

    def rows(self, collection: str) -> list[dict]:
        return list(self.docs[collection].values())

    def doc(self, collection: str, doc_id: str) -> Optional[dict]:
        return self.docs[collection].get(doc_id)

    def snap(self, collection: str, doc_id: str) -> Any:
        return self.snaps[collection].get(doc_id)


def _read_collection(collection: str) -> tuple[dict[str, dict], dict[str, Any]]:
    try:
        docs: dict[str, dict] = {}
        snaps: dict[str, Any] = {}
        for snap in db.collection(collection).stream():
            data = {**(snap.to_dict() or {}), "id": snap.id}
            docs[snap.id] = data
            snaps[snap.id] = snap
        return docs, snaps
    except Exception as exc:
        raise RunRefused(
            f"lecture stricte de {collection} impossible ({type(exc).__name__}) — rien n'a été écrit"
        ) from None


def read_world(spec: PlanSpec) -> World:
    collections = (
        TRUST_ACCOUNTS,
        TRUST_TX,
        TRUST_RECS,
        ADMIN_ACCOUNTS,
        ADMIN_TX,
        ADMIN_RECS,
        INVOICES,
        DOSSIERS,
        COUNTERS,
        AUDIT_EVENTS,
        MCP_IDEMPOTENCY,
        "expenses",
        "timeentries",
        "documents",
        "notes",
    )
    docs: dict[str, dict[str, dict]] = {}
    snaps: dict[str, dict[str, Any]] = {}
    for col in collections:
        docs[col], snaps[col] = _read_collection(col)

    invoice_lines: dict[str, list[dict]] = {}
    for pair in spec.pairs:
        fac = pair.corrections_derivees.get("facture")
        if isinstance(fac, dict) and fac.get("id"):
            iid = str(fac["id"])
            try:
                ref = (
                    db.collection(INVOICES)
                    .document(iid)
                    .collection(invoice_model.LINE_ITEMS_SUB)
                )
                lines = [{**(s.to_dict() or {}), "id": s.id} for s in ref.stream()]
            except Exception as exc:
                raise RunRefused(
                    f"lecture des lignes de la facture {iid} impossible "
                    f"({type(exc).__name__}) — rien n'a été écrit"
                ) from None
            lines.sort(key=lambda d: d["id"])
            invoice_lines[iid] = lines

    return World(
        docs=docs,
        snaps=snaps,
        invoice_lines=invoice_lines,
        expenses=list(docs["expenses"].values()),
        time_entries=list(docs["timeentries"].values()),
    )


# ── Per-pair judgment & write staging ─────────────────────────────────────


@dataclass
class DocWrite:
    collection: str
    doc_id: str
    role: str
    snap: Any
    before: dict
    business: dict
    payload: dict
    changes: dict[str, list]


@dataclass
class IdempotencyTarget:
    doc_id: str
    snap: Any
    doc: dict
    reason: str
    expired: bool


@dataclass
class PairVerdict:
    spec: PairSpec
    state: str = PENDING
    table: list[tuple[str, str, str, str, bool]] = field(default_factory=list)
    differences: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    deletes: list[tuple[str, str, Any, dict, str]] = field(default_factory=list)
    updates: list[DocWrite] = field(default_factory=list)
    extra_reads: list[tuple[str, str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    desc_preview: Optional[tuple[str, str]] = None
    invoice_preview: Optional[dict] = None

    @property
    def blocking(self) -> list[str]:
        return self.differences + self.refusals


def _compare_field(
    verdict: PairVerdict, label: str, name: str, want: Any, have: Any
) -> None:
    ok = want == have
    verdict.table.append((label, name, rx._v(want), rx._v(have), ok))
    if not ok:
        verdict.differences.append(
            f"{verdict.spec.code} · {label} · {name} : le plan attend "
            f"{rx._v(want)}, la base porte {rx._v(have)}"
        )


def _check_pair_docs(pair: PairSpec, world: World, verdict: PairVerdict) -> bool:
    """Compare every Section 3 document against Firestore; return True if eligible."""
    col = TRUST_TX if pair.registre == "fideicommis" else ADMIN_TX
    nature_key = "purpose" if pair.registre == "fideicommis" else "kind"

    present = [d.id for d in pair.documents if d.id in world.docs[col]]
    if not present:
        # Check idempotency: audit event + derived state already applied
        audit_ok = any(
            ev.get("entity_type") == ENTITY_TYPE
            and isinstance(ev.get("migration"), dict)
            and ev["migration"].get("paire") == pair.code
            and set(ev["migration"].get("ids") or []) == {d.id for d in pair.documents}
            for ev in world.docs[AUDIT_EVENTS].values()
        )
        if audit_ok and _derived_already_done(pair, world, verdict):
            verdict.state = DONE
            verdict.table.append(
                (pair.code, "état", "supprimée + trace", "déjà appliquée", True)
            )
            return False
        verdict.state = REFUSED
        verdict.refusals.append(
            f"{pair.code} — état mixte : documents absents mais trace ou état "
            f"dérivé incomplet"
        )
        return False

    if len(present) != len(pair.documents):
        missing = [d.id for d in pair.documents if d.id not in world.docs[col]]
        verdict.state = REFUSED
        verdict.refusals.append(
            f"{pair.code} — état mixte : certains documents existent encore "
            f"({', '.join(present)}), d'autres manquent ({', '.join(missing)})"
        )
        return False

    net_sum = 0
    for dspec in pair.documents:
        doc = world.docs[col][dspec.id]
        snap = world.snaps[col][dspec.id]
        label = f"{dspec.role} ({dspec.id[:8]})"
        _compare_field(verdict, label, "account_id", dspec.account_id, doc.get("account_id"))
        _compare_field(verdict, label, "sequence", dspec.sequence, doc.get("sequence"))
        _compare_field(
            verdict, label, "date", dspec.date.isoformat(), _doc_day_str(doc.get("date"))
        )
        _compare_field(verdict, label, nature_key, dspec.nature, doc.get(nature_key))
        _compare_field(verdict, label, "direction", dspec.direction, doc.get("direction"))
        _compare_field(verdict, label, "amount", dspec.amount, doc.get("amount"))
        _compare_field(verdict, label, "status", dspec.status, doc.get("status"))
        _compare_field(
            verdict, label, "reverses_id", dspec.reverses_id, _norm_id(doc.get("reverses_id"))
        )
        _compare_field(
            verdict,
            label,
            "reversed_by_id",
            dspec.reversed_by_id,
            _norm_id(doc.get("reversed_by_id")),
        )
        _compare_field(
            verdict,
            label,
            "related_transaction_id",
            dspec.related_transaction_id,
            _norm_id(doc.get("related_transaction_id")),
        )
        if dspec.dossier_file_number is not None:
            _compare_field(
                verdict,
                label,
                "dossier_file_number",
                dspec.dossier_file_number,
                _norm_id(doc.get("dossier_file_number")),
            )
        if dspec.client_id is not None:
            _compare_field(
                verdict, label, "client_id", dspec.client_id, _norm_id(doc.get("client_id"))
            )
        if dspec.invoice_id is not None:
            _compare_field(
                verdict, label, "invoice_id", dspec.invoice_id, _norm_id(doc.get("invoice_id"))
            )
        if dspec.invoice_number is not None:
            _compare_field(
                verdict,
                label,
                "invoice_number",
                dspec.invoice_number,
                _norm_id(doc.get("invoice_number")),
            )
        net_sum += (
            int(doc.get("amount") or 0)
            if doc.get("direction") == "recette"
            else -int(doc.get("amount") or 0)
        )
        verdict.deletes.append((col, dspec.id, snap, copy.deepcopy(doc), dspec.role))

    if net_sum != 0:
        verdict.refusals.append(
            f"{pair.code} — la somme algébrique de la paire vaut {net_sum} cents (0 attendu)"
        )

    # Verify external collections and structural transaction links don't reference these deleted docs
    deleted_ids = {d.id for d in pair.documents}
    desc_target_id = (pair.corrections_derivees.get("description_ecriture") or {}).get("id")
    for check_col in (TRUST_RECS, ADMIN_RECS, INVOICES, DOSSIERS, "expenses", "timeentries", "documents", "notes"):
        for doc_id, doc in world.docs[check_col].items():
            hits = _contains_any_id(doc, deleted_ids)
            if hits:
                verdict.refusals.append(
                    f"{pair.code} — {check_col}/{doc_id} cite encore {', '.join(hits)}"
                )
    for tx_col in (TRUST_TX, ADMIN_TX):
        for tx_id, tx in world.docs[tx_col].items():
            if tx_col == col and tx_id in deleted_ids:
                continue
            if tx_col == TRUST_TX and tx_id == desc_target_id:
                # Structural links must not point to deleted IDs; description is rewritten by P1
                probe = {
                    k: v
                    for k, v in tx.items()
                    if k not in ("description", "revisions")
                }
                hits = _contains_any_id(probe, deleted_ids)
            else:
                hits = _contains_any_id(tx, deleted_ids)
            if hits:
                verdict.refusals.append(
                    f"{pair.code} — {tx_col}/{tx_id} (séq. {tx.get('sequence')}) "
                    f"cite encore {', '.join(hits)}"
                )

    if verdict.blocking:
        verdict.state = REFUSED
        verdict.deletes.clear()
        return False
    return True


def _derived_already_done(pair: PairSpec, world: World, verdict: PairVerdict) -> bool:
    corr = pair.corrections_derivees
    if corr.get("recalcul_soldes_courants"):
        attendus = corr.get("attendus_soldes") or {}
        for tid, exp in attendus.items():
            doc = world.docs[TRUST_TX].get(tid)
            if not doc or doc.get("balance_after_account") != exp.get("balance_after_account_apres"):
                return False
    desc_spec = corr.get("description_ecriture")
    if isinstance(desc_spec, dict):
        doc = world.docs[TRUST_TX].get(desc_spec.get("id"))
        if not doc or desc_spec["retirer_debut"] in (doc.get("description") or ""):
            return False
    fac_spec = corr.get("facture")
    if isinstance(fac_spec, dict):
        inv = world.docs[INVOICES].get(fac_spec["id"])
        if not inv:
            return False
        after_exp = fac_spec["attendu_apres"]
        if (
            inv.get("status") != after_exp["status"]
            or int(inv.get("amount_paid") or 0) != after_exp["amount_paid"]
            or invoice_model.balance_of(inv) != after_exp["balance"]
            or _doc_day_str(inv.get("paid_date")) != after_exp["paid_date"]
        ):
            return False
    return True


def _stage_p1_derived(
    pair: PairSpec,
    world: World,
    plan_sha: str,
    motif: str,
    now: datetime,
    verdict: PairVerdict,
) -> None:
    corr = pair.corrections_derivees
    account_id = pair.documents[0].account_id
    deleted_ids = {d.id for d in pair.documents}

    all_txs = sorted(
        (
            copy.deepcopy(t)
            for t in world.docs[TRUST_TX].values()
            if t.get("account_id") == account_id
        ),
        key=lambda t: int(t.get("sequence") or 0),
    )
    old_running = trust.recompute_running_balances(all_txs, "journal")
    old_by_id = {t["id"]: bal for t, bal in zip(all_txs, old_running)}

    remaining_txs = [t for t in all_txs if t["id"] not in deleted_ids]
    new_running = trust.recompute_running_balances(remaining_txs, "journal")
    new_by_id = {t["id"]: bal for t, bal in zip(remaining_txs, new_running)}

    # Also verify client running balances across the account's remaining rows
    by_couple: dict[tuple[str, str], list[dict]] = {}
    for t in remaining_txs:
        if t.get("dossier_id") and t.get("client_id"):
            by_couple.setdefault((t["dossier_id"], t["client_id"]), []).append(t)
    expected_client_bal: dict[str, int] = {}
    for couple_rows in by_couple.values():
        couple_rows.sort(
            key=lambda r: (r.get("created_at") or 0, int(r.get("sequence") or 0))
        )
        for t, bal in zip(
            couple_rows, trust.recompute_running_balances(couple_rows, "carte")
        ):
            expected_client_bal[t["id"]] = bal

    attendus = corr.get("attendus_soldes") or {}
    desc_spec = corr.get("description_ecriture") or {}
    desc_target_id = desc_spec.get("id")

    min_deleted_seq = min(d.sequence for d in pair.documents)
    moved_ids = [
        t["id"]
        for t in remaining_txs
        if int(t.get("sequence") or 0) > min_deleted_seq
        and new_by_id[t["id"]] != old_by_id[t["id"]]
    ]
    if set(moved_ids) != set(attendus):
        verdict.refusals.append(
            f"{pair.code} — écritures dont le solde courant bouge ({moved_ids}) "
            f"diffèrent du plan ({sorted(attendus)})"
        )
        return

    target_ids = list(dict.fromkeys(moved_ids + ([desc_target_id] if desc_target_id else [])))
    for tid in target_ids:
        doc = world.docs[TRUST_TX].get(tid)
        snap = world.snaps[TRUST_TX].get(tid)
        if doc is None or snap is None:
            verdict.refusals.append(f"{pair.code} — écriture dérivée {tid} introuvable")
            return
        if len(doc.get("revisions") or []) > MAX_TRAIL_BEFORE:
            verdict.refusals.append(
                f"{pair.code} — fil de révisions de {tid} plein (> {MAX_TRAIL_BEFORE})"
            )
            return

        label = f"Fidéicommis séq. {doc.get('sequence')} ({tid[:8]})"
        stored_acct_bal = doc.get("balance_after_account")
        if stored_acct_bal != old_by_id[tid]:
            verdict.refusals.append(
                f"{pair.code} — {label} : solde courant stocké ({stored_acct_bal}) "
                f"≠ recomputé avant ({old_by_id[tid]})"
            )
            return

        business: dict[str, Any] = {}
        if tid in attendus:
            exp = attendus[tid]
            _compare_field(verdict, label, "sequence", exp["sequence"], doc.get("sequence"))
            _compare_field(
                verdict,
                label,
                "balance_after_account_avant",
                exp["balance_after_account_avant"],
                stored_acct_bal,
            )
            _compare_field(
                verdict,
                label,
                "balance_after_account_apres",
                exp["balance_after_account_apres"],
                new_by_id[tid],
            )
            _compare_field(
                verdict,
                label,
                "balance_after_client",
                exp["balance_after_client"],
                doc.get("balance_after_client"),
            )
            if expected_client_bal.get(tid) != doc.get("balance_after_client"):
                verdict.refusals.append(
                    f"{pair.code} — {label} : balance_after_client stocké "
                    f"({doc.get('balance_after_client')}) ≠ recomputé "
                    f"({expected_client_bal.get(tid)})"
                )
                return
            if new_by_id[tid] != stored_acct_bal:
                business["balance_after_account"] = new_by_id[tid]

        if tid == desc_target_id:
            if doc.get("sequence") != desc_spec.get("sequence"):
                verdict.refusals.append(
                    f"{pair.code} — description {tid} : séquence {doc.get('sequence')} "
                    f"≠ {desc_spec.get('sequence')}"
                )
                return
            old_desc = doc.get("description") or ""
            try:
                new_desc = _rewrite_description(
                    old_desc,
                    desc_spec["retirer_debut"],
                    desc_spec["retirer_fin_incluse"],
                    desc_spec["majuscule_mot"],
                )
            except ValueError as exc:
                verdict.refusals.append(
                    f"{pair.code} — réécriture de la description 133 refusée : {exc}"
                )
                return
            business["description"] = new_desc
            verdict.desc_preview = (old_desc, new_desc)

        if not business:
            continue

        changes = {k: [doc.get(k), v] for k, v in business.items()}
        with provenance.writing_via("script"):
            trail = {
                "at": now,
                "via": provenance.current_via(),
                "motif": f"Correction dérivée ({pair.code}) — {motif}",
                "changes": changes,
                "updated_at_before": doc.get("updated_at"),
                "source_sha256": plan_sha,
            }
            payload = {
                **business,
                "revisions": list(doc.get("revisions") or []) + [trail],
                **provenance.update_fields(now),
            }
        verdict.updates.append(
            DocWrite(
                collection=TRUST_TX,
                doc_id=tid,
                role=f"fidéicommis_séq_{doc.get('sequence')}",
                snap=snap,
                before=copy.deepcopy(doc),
                business=business,
                payload=payload,
                changes=changes,
            )
        )


def _stage_p2_derived(
    pair: PairSpec, world: World, now: datetime, verdict: PairVerdict
) -> None:
    fac = pair.corrections_derivees.get("facture")
    if not isinstance(fac, dict):
        return
    iid = str(fac["id"])
    inv = world.docs[INVOICES].get(iid)
    snap = world.snaps[INVOICES].get(iid)
    if inv is None or snap is None:
        verdict.refusals.append(f"{pair.code} — facture {iid} introuvable")
        return

    beyond = invoice_model.repair_script_refusal(inv)
    if beyond:
        verdict.refusals.append(f"{pair.code} — la facture {beyond}")
        return

    label = f"Facture {fac['numero']} ({iid[:8]})"
    avant = fac["attendu_avant"]
    _compare_field(verdict, label, "invoice_number", fac["numero"], inv.get("invoice_number"))
    _compare_field(verdict, label, "status", avant["status"], inv.get("status"))
    _compare_field(
        verdict, label, "amount_paid", avant["amount_paid"], int(inv.get("amount_paid") or 0)
    )
    _compare_field(verdict, label, "balance", avant["balance"], invoice_model.balance_of(inv))
    basis_before = "recorded" if int(inv.get("amount_paid") or 0) else "none"
    _compare_field(verdict, label, "payment_basis", avant["payment_basis"], basis_before)
    _compare_field(
        verdict, label, "paid_date", avant["paid_date"], _doc_day_str(inv.get("paid_date"))
    )

    deleted_ids = {d.id for d in pair.documents}
    remaining_standing = [
        r
        for r in world.docs[ADMIN_TX].values()
        if r.get("invoice_id") == iid
        and r["id"] not in deleted_ids
        and al.receipt_stands(r)
    ]
    remaining_standing.sort(
        key=lambda r: (rla._day(r.get("date")) or date.min, int(r.get("sequence") or 0))
    )

    rem_exp = fac.get("paiement_restant_attendu") or {}
    rem_admin = world.docs[ADMIN_TX].get(rem_exp.get("admin_id") or "")
    rem_trust = world.docs[TRUST_TX].get(rem_exp.get("trust_id") or "")
    if rem_admin is None or rem_admin.get("sequence") != rem_exp.get("admin_seq"):
        verdict.refusals.append(
            f"{pair.code} — encaissement restant attendu ({rem_exp.get('admin_id')}) absent ou altéré"
        )
        return
    if rem_trust is None or rem_trust.get("sequence") != rem_exp.get("trust_seq"):
        verdict.refusals.append(
            f"{pair.code} — retrait fidéicommis restant ({rem_exp.get('trust_id')}) absent ou altéré"
        )
        return
    if rem_admin.get("trust_transaction_id") != rem_trust["id"]:
        verdict.refusals.append(
            f"{pair.code} — l'encaissement restant ne pointe plus vers le retrait fidéicommis attendu"
        )
        return
    for r in remaining_standing:
        verdict.extra_reads.append((ADMIN_TX, r["id"], world.snaps[ADMIN_TX][r["id"]]))

    derived_paid = sum(int(r.get("amount") or 0) for r in remaining_standing)
    dates = [al._midnight_utc(r.get("date")) for r in remaining_standing]
    if any(d is None for d in dates):
        verdict.refusals.append(f"{pair.code} — date illisible sur un encaissement restant")
        return
    derived_paid_date = max(dates) if dates else None

    with provenance.writing_via("script"):
        try:
            updates = invoice_model.payment_updates(
                inv, derived_paid, derived_paid_date, now=now
            )
        except invoice_model.PaymentRefused as exc:
            verdict.refusals.append(f"{pair.code} — payment_updates refuse : {exc}")
            return

    after_inv = {**inv, **updates}
    apres = fac["attendu_apres"]
    after_basis = "recorded" if int(after_inv.get("amount_paid") or 0) else "none"
    _compare_field(verdict, label, "status (après)", apres["status"], after_inv.get("status"))
    _compare_field(
        verdict,
        label,
        "amount_paid (après)",
        apres["amount_paid"],
        int(after_inv.get("amount_paid") or 0),
    )
    _compare_field(
        verdict,
        label,
        "balance (après)",
        apres["balance"],
        invoice_model.balance_of(after_inv),
    )
    _compare_field(verdict, label, "payment_basis (après)", apres["payment_basis"], after_basis)
    _compare_field(
        verdict,
        label,
        "paid_date (après)",
        apres["paid_date"],
        _doc_day_str(after_inv.get("paid_date")),
    )

    targets = ("amount_paid", "paid_date", "status")
    business = {
        k: updates[k]
        for k in targets
        if k in updates and rx._aware(inv.get(k)) != rx._aware(updates[k])
    }
    if not business:
        verdict.notes.append(f"{pair.code} — la facture {fac['numero']} est déjà à sa valeur dérivée.")
        return
    payload = {
        **business,
        **{k: updates[k] for k in ("updated_at", "etag", "updated_via")},
    }
    changes = {k: [inv.get(k), v] for k, v in business.items()}
    verdict.invoice_preview = {
        "numero": fac["numero"],
        "id": iid,
        "standing_receipts": [
            {
                "id": r["id"],
                "sequence": r.get("sequence"),
                "date": _doc_day_str(r.get("date")),
                "amount": int(r.get("amount") or 0),
            }
            for r in remaining_standing
        ],
        "before": {
            "status": inv.get("status"),
            "amount_paid": int(inv.get("amount_paid") or 0),
            "balance": invoice_model.balance_of(inv),
            "payment_basis": basis_before,
            "paid_date": _doc_day_str(inv.get("paid_date")),
        },
        "after": {
            "status": after_inv.get("status"),
            "amount_paid": int(after_inv.get("amount_paid") or 0),
            "balance": invoice_model.balance_of(after_inv),
            "payment_basis": after_basis,
            "paid_date": _doc_day_str(after_inv.get("paid_date")),
        },
    }
    verdict.updates.append(
        DocWrite(
            collection=INVOICES,
            doc_id=iid,
            role=f"facture_{fac['numero']}",
            snap=snap,
            before=copy.deepcopy(inv),
            business=business,
            payload=payload,
            changes=changes,
        )
    )


def _judge_pair(
    pair: PairSpec, world: World, plan_sha: str, motif: str, now: datetime
) -> PairVerdict:
    verdict = PairVerdict(spec=pair)
    if not _check_pair_docs(pair, world, verdict):
        return verdict
    if pair.corrections_derivees.get("recalcul_soldes_courants") or pair.corrections_derivees.get(
        "description_ecriture"
    ):
        _stage_p1_derived(pair, world, plan_sha, motif, now, verdict)
    if pair.corrections_derivees.get("facture"):
        _stage_p2_derived(pair, world, now, verdict)
    if verdict.blocking:
        verdict.state = REFUSED
        verdict.deletes.clear()
        verdict.updates.clear()
    else:
        verdict.state = PENDING
    return verdict


def _check_replacements(spec: PlanSpec, world: World) -> list[tuple[str, str, bool, str]]:
    rows: list[tuple[str, str, bool, str]] = []
    for rep in spec.replacements:
        col = TRUST_TX if rep.registre == "fideicommis" else ADMIN_TX
        doc = world.docs[col].get(rep.id)
        if doc is None:
            rows.append((rep.id, rep.registre, False, "introuvable"))
            continue
        problems = []
        if doc.get("sequence") != rep.sequence:
            problems.append(f"séq {doc.get('sequence')} ≠ {rep.sequence}")
        if _doc_day_str(doc.get("date")) != rep.date.isoformat():
            problems.append(f"date {_doc_day_str(doc.get('date'))} ≠ {rep.date.isoformat()}")
        if doc.get("amount") != rep.amount:
            problems.append(f"montant {doc.get('amount')} ≠ {rep.amount}")
        if rep.client_id is not None and doc.get("client_id") != rep.client_id:
            problems.append(f"client {doc.get('client_id')} ≠ {rep.client_id}")
        if rep.cleared_date is not None and _doc_day_str(doc.get("cleared_date")) != rep.cleared_date.isoformat():
            problems.append(
                f"cleared_date {_doc_day_str(doc.get('cleared_date'))} ≠ {rep.cleared_date.isoformat()}"
            )
        if (
            rep.related_transaction_id is not None
            and doc.get("related_transaction_id") != rep.related_transaction_id
        ):
            problems.append(
                f"related {doc.get('related_transaction_id')} ≠ {rep.related_transaction_id}"
            )
        if doc.get("status") == "annulée" or doc.get("reversed_by_id"):
            problems.append("statut annulée ou contre-passée")
        rows.append(
            (rep.id, rep.registre, not problems, "conforme" if not problems else " ; ".join(problems))
        )
    return rows


def _find_idempotency_targets(
    spec: PlanSpec, world: World, active_deleted_ids: set[str], now: datetime
) -> list[IdempotencyTarget]:
    candidate_map = _candidate_idempotency_doc_ids(list(spec.idempotency_keys))
    targets: list[IdempotencyTarget] = []
    for doc_id in sorted(world.docs[MCP_IDEMPOTENCY]):
        doc = world.docs[MCP_IDEMPOTENCY][doc_id]
        snap = world.snaps[MCP_IDEMPOTENCY][doc_id]
        reasons: list[str] = []
        if doc_id in candidate_map:
            reasons.append(f"clé nommée : {candidate_map[doc_id]}")
        id_hits = _contains_any_id(doc, active_deleted_ids)
        if id_hits:
            reasons.append(f"cite id supprimé : {', '.join(id_hits)}")
        key_hits = [k for k in spec.idempotency_keys if _contains_any_id(doc, {k})]
        if key_hits and doc_id not in candidate_map:
            reasons.append(f"cite clé : {', '.join(key_hits)}")
        if not reasons:
            continue
        exp = doc.get("expire_at")
        expired = False
        if isinstance(exp, datetime):
            exp_utc = exp if exp.tzinfo else exp.replace(tzinfo=UTC)
            expired = exp_utc < now
        targets.append(
            IdempotencyTarget(
                doc_id=doc_id,
                snap=snap,
                doc=copy.deepcopy(doc),
                reason=" ; ".join(reasons),
                expired=expired,
            )
        )
    return targets


# ── Controls & simulation ─────────────────────────────────────────────────


def _simulate_world(
    world: World, verdicts: list[PairVerdict], idem_targets: list[IdempotencyTarget]
) -> World:
    sim = copy.deepcopy(
        World(
            docs={k: dict(v) for k, v in world.docs.items()},
            snaps={},
            invoice_lines=world.invoice_lines,
            expenses=world.expenses,
            time_entries=world.time_entries,
        )
    )
    for v in verdicts:
        if v.state != PENDING:
            continue
        for col, doc_id, _snap, _before, _role in v.deletes:
            sim.docs[col].pop(doc_id, None)
        for upd in v.updates:
            base = sim.docs[upd.collection][upd.doc_id]
            sim.docs[upd.collection][upd.doc_id] = {**base, **copy.deepcopy(upd.payload)}
    for tgt in idem_targets:
        sim.docs[MCP_IDEMPOTENCY].pop(tgt.doc_id, None)
    return sim


def _reproof_trust_recs(txs: list[dict], recs: list[dict]) -> dict[str, dict]:
    by_id = {t["id"]: t for t in txs}
    out: dict[str, dict] = {}
    for rec in recs:
        if rec.get("status") != "complétée":
            continue
        end = rla._day(rec.get("period_end"))
        if end is None:
            raise ControlImpossible(f"conciliation fidéicommis {rec.get('id')} sans period_end")
        book = outstanding = in_transit = 0
        for t in txs:
            day = rla._day(t.get("date"))
            if day is None or day > end:
                continue
            deltas = trust.compute_deltas(
                t.get("direction", ""), int(t.get("amount") or 0), t.get("status", "")
            )
            book += deltas["book"]
            status = t.get("status")
            counted = status == "en_circulation"
            if status == "compensée":
                cd = rla._day(t.get("cleared_date"))
                counted = cd is not None and cd > end
            elif status == "annulée" and t.get("reversed_by_id") and not t.get("reverses_id"):
                rev = by_id.get(t.get("reversed_by_id"))
                if rev is None or rla._day(rev.get("date")) is None:
                    raise ControlImpossible(
                        f"contre-passation de {t.get('id')} illisible pour as-of"
                    )
                counted = rla._day(rev.get("date")) > end
            if counted and t.get("direction") == "déboursé":
                outstanding += int(t.get("amount") or 0)
            elif counted and t.get("direction") == "recette":
                in_transit += int(t.get("amount") or 0)
        stmt = rec.get("statement_balance")
        if isinstance(stmt, bool) or not isinstance(stmt, int):
            raise ControlImpossible(f"conciliation fidéicommis {rec.get('id')} sans solde")
        variance = trust.reconciliation_variance(stmt, book, outstanding, in_transit)
        out[str(rec.get("id"))] = {
            "period_end": end.isoformat(),
            "variance": variance,
            "pre_as_of": vti._pre_as_of(rec),
        }
    return out


def _compute_controls(world: World, spec: PlanSpec, today: date, now: datetime) -> dict:
    trust_rows = list(world.docs[TRUST_TX].values())
    admin_rows = list(world.docs[ADMIN_TX].values())
    dossiers = list(world.docs[DOSSIERS].values())

    # 1. Fidéicommis
    trust_spec = next(a for a in spec.accounts.values() if a.registre == "fideicommis")
    f_txs = sorted(
        (t for t in trust_rows if t.get("account_id") == trust_spec.id),
        key=lambda t: int(t.get("sequence") or 0),
    )
    f_acc = world.docs[TRUST_ACCOUNTS].get(trust_spec.id) or {}
    f_book = sum(
        trust.compute_deltas(t.get("direction", ""), int(t.get("amount") or 0), t.get("status", ""))[
            "book"
        ]
        for t in f_txs
    )
    f_bank = sum(
        trust.compute_deltas(t.get("direction", ""), int(t.get("amount") or 0), t.get("status", ""))[
            "bank"
        ]
        for t in f_txs
    )
    f_cleared = sum(
        trust.compute_deltas(t.get("direction", ""), int(t.get("amount") or 0), t.get("status", ""))[
            "cleared"
        ]
        for t in f_txs
    )
    f_running = trust.recompute_running_balances(f_txs, "journal")
    f_running_ok = all(
        t.get("balance_after_account") == bal for t, bal in zip(f_txs, f_running)
    )
    f_recs = [r for r in world.docs[TRUST_RECS].values() if r.get("account_id") == trust_spec.id]
    f_completed = [r for r in f_recs if r.get("status") == "complétée"]
    f_last_rec = max(
        (rla._day(r.get("period_end")).isoformat() for r in f_completed if rla._day(r.get("period_end"))),
        default=None,
    )
    f_proofs = _reproof_trust_recs(f_txs, f_recs)

    by_dossier = {}
    for d in dossiers:
        if d.get("trust_balance_by_client"):
            fn = d.get("file_number") or d["id"]
            by_dossier[fn] = int(d.get("trust_balance") or 0)
    by_dossier_nonzero = {k: v for k, v in sorted(by_dossier.items()) if v != 0}
    recalc_by_dossier: dict[str, int] = {}
    for t in f_txs:
        fn = t.get("dossier_file_number") or t.get("dossier_id") or ""
        if not fn:
            continue
        delta = trust.compute_deltas(
            t.get("direction", ""), int(t.get("amount") or 0), t.get("status", "")
        )["book"]
        recalc_by_dossier[fn] = recalc_by_dossier.get(fn, 0) + delta
    recalc_by_dossier_nonzero = {
        k: v for k, v in sorted(recalc_by_dossier.items()) if v != 0
    }

    ccv = spec.carte_client_vide
    ccv_dossier = next(
        (d for d in dossiers if d.get("file_number") == ccv["dossier_file_number"]), None
    )
    ccv_cid = ccv["client_id"]
    ccv_rows = (
        [
            t
            for t in trust_rows
            if t.get("dossier_id") == ccv_dossier["id"] and t.get("client_id") == ccv_cid
        ]
        if ccv_dossier
        else []
    )
    ccv_book = sum(
        trust.compute_deltas(t.get("direction", ""), int(t.get("amount") or 0), t.get("status", ""))[
            "book"
        ]
        for t in ccv_rows
    )
    ccv_stored = (
        int((ccv_dossier.get("trust_balance_by_client") or {}).get(ccv_cid, 0) or 0)
        if ccv_dossier
        else 0
    )

    # 2-4. Administration accounts
    admin_info: dict[str, dict] = {}
    for alias, aspec in spec.accounts.items():
        if aspec.registre != "administration":
            continue
        acc = world.docs[ADMIN_ACCOUNTS].get(aspec.id) or {}
        mine = sorted(
            (r for r in admin_rows if r.get("account_id") == aspec.id),
            key=lambda r: int(r.get("sequence") or 0),
        )
        recalc_ledger = sum(
            al.admin_delta(r.get("direction", ""), int(r.get("amount") or 0)) for r in mine
        )
        stored_ledger = int(acc.get("ledger_balance") or 0)
        disp_recalc = al.display_balance(acc.get("account_type", "opérations"), recalc_ledger)
        recs_ctrl = rla._control_reconciliations(mine, {aspec.id: acc}, list(world.docs[ADMIN_RECS].values()))
        acc_rec = recs_ctrl.get(aspec.id) or {}
        admin_info[alias] = {
            "id": aspec.id,
            "stored_ledger": stored_ledger,
            "recalc_ledger": recalc_ledger,
            "display_balance": disp_recalc,
            "plancher": acc_rec.get("plancher"),
            "preuves": acc_rec.get("preuves") or {},
            "count": len(mine),
            "max_seq": max((int(r.get("sequence") or 0) for r in mine), default=0),
            "digest": rla._digest(mine) if aspec.intouchable else "",
        }

    # 5. Factures
    target_fac = next(
        (
            p.corrections_derivees["facture"]
            for p in spec.pairs
            if isinstance(p.corrections_derivees.get("facture"), dict)
        ),
        None,
    )
    target_inv_state = {}
    other_invoices = []
    untouchable_summary = {}
    for iid, inv in sorted(world.docs[INVOICES].items()):
        if target_fac and iid == target_fac["id"]:
            standing_sum = sum(
                int(r.get("amount") or 0)
                for r in admin_rows
                if r.get("invoice_id") == iid and al.receipt_stands(r)
            )
            target_inv_state = {
                "id": iid,
                "numero": inv.get("invoice_number"),
                "status": inv.get("status"),
                "amount_paid": int(inv.get("amount_paid") or 0),
                "balance": invoice_model.balance_of(inv),
                "payment_basis": "recorded" if int(inv.get("amount_paid") or 0) else "none",
                "paid_date": _doc_day_str(inv.get("paid_date")),
                "sum_standing_receipts": standing_sum,
            }
        else:
            other_invoices.append(inv)
            if inv.get("invoice_number") in spec.factures_intouchables:
                untouchable_summary[inv["invoice_number"]] = {
                    "id": iid,
                    "status": inv.get("status"),
                    "amount_paid": int(inv.get("amount_paid") or 0),
                    "balance": invoice_model.balance_of(inv),
                    "paid_date": _doc_day_str(inv.get("paid_date")),
                }

    # 6. Dangling IDs across collections
    deleted_ids = spec.all_deleted_ids
    dangling: dict[str, list[str]] = {}
    for col in _SCAN_COLLECTIONS:
        for doc_id, doc in sorted(world.docs[col].items()):
            if col in (TRUST_TX, ADMIN_TX) and doc_id in deleted_ids:
                continue
            hits = _contains_any_id(doc, deleted_ids)
            if hits:
                dangling[f"{col}/{doc_id}"] = hits

    # 7. Financial statements for spec.period
    snap = fs.Snapshot(
        admin_accounts=list(world.docs[ADMIN_ACCOUNTS].values()),
        admin_transactions=admin_rows,
        admin_reconciliations=list(world.docs[ADMIN_RECS].values()),
        invoices=list(world.docs[INVOICES].values()),
        trust_accounts=list(world.docs[TRUST_ACCOUNTS].values()),
        trust_transactions=trust_rows,
        time_entries=world.time_entries,
        expenses=world.expenses,
    )
    st = fs.compute(snap, spec.period, today=today, now=now)
    res = st.sections.get("resultats")
    avoir = st.sections.get("avoir")
    if res is None or avoir is None or st.unavailable:
        raise ControlImpossible(
            f"états financiers indisponibles ({st.unavailable})"
        )
    fin = {
        "revenus": int(res["revenue"]),
        "depenses_total": int(sum(res["cost_totals"].values())),
        "depenses_direct": int(res["cost_totals"]["direct"]),
        "depenses_variable": int(res["cost_totals"]["variable"]),
        "depenses_fixe": int(res["cost_totals"]["fixe"]),
        "benefice_net": int(res["net_income"]),
        "prelevements": int(avoir["prelevements"]),
        "apports": int(avoir["apports"]),
        "ecart_non_explique": int(avoir["unexplained"]),
    }

    # 8. Entry counts & counters
    counters = {
        cid: int((c.get("seq") or 0))
        for cid, c in sorted(world.docs[COUNTERS].items())
    }

    return {
        "fideicommis": {
            "id": trust_spec.id,
            "stored_book": int(f_acc.get("book_balance") or 0),
            "stored_bank": int(f_acc.get("bank_balance") or 0),
            "recalc_book": f_book,
            "recalc_cleared": f_cleared,
            "recalc_bank": f_bank,
            "running_ok": f_running_ok,
            "by_dossier": dict(sorted(by_dossier.items())),
            "by_dossier_nonzero": by_dossier_nonzero,
            "recalc_by_dossier_nonzero": recalc_by_dossier_nonzero,
            "carte_client_vide_count": len(ccv_rows),
            "carte_client_vide_book": ccv_book,
            "carte_client_vide_stored": ccv_stored,
            "last_reconciliation_date": f_last_rec,
            "preuves": f_proofs,
            "count": len(f_txs),
            "max_seq": max((int(t.get("sequence") or 0) for t in f_txs), default=0),
            "missing_seqs": sorted(
                set(
                    range(
                        1,
                        max(
                            counters.get(trust._counter_id(trust_spec.id), 0),
                            max((int(t.get("sequence") or 0) for t in f_txs), default=0),
                        )
                        + 1,
                    )
                )
                - {int(t.get("sequence") or 0) for t in f_txs if isinstance(t.get("sequence"), int)}
            ),
        },
        "administration": admin_info,
        "facture_cible": target_inv_state,
        "autres_factures_digest": rla._digest(other_invoices),
        "factures_intouchables": untouchable_summary,
        "dangling": dangling,
        "etats_financiers": fin,
        "counters": counters,
    }


def _evaluate_controls(
    spec: PlanSpec, before: dict, after: dict, verdicts: list[PairVerdict]
) -> list[str]:
    """Compare BEFORE and AFTER (simulated or real) against all 8 Section 5 checks."""
    gaps: list[str] = []
    f_b, f_a = before["fideicommis"], after["fideicommis"]
    f_spec = next(a for a in spec.accounts.values() if a.registre == "fideicommis")
    if f_a["stored_book"] != f_spec.solde_attendu_cents or f_a["recalc_book"] != f_spec.solde_attendu_cents:
        gaps.append(
            f"5.1 fidéicommis : solde aux livres stocké {f_a['stored_book']}, recalculé "
            f"{f_a['recalc_book']} (attendu {f_spec.solde_attendu_cents})"
        )
    if f_a["stored_bank"] != f_spec.solde_attendu_cents or f_a["recalc_bank"] != f_spec.solde_attendu_cents:
        gaps.append(
            f"5.1 fidéicommis : solde compensé stocké {f_a['stored_bank']}, recalculé "
            f"{f_a['recalc_bank']} (attendu {f_spec.solde_attendu_cents})"
        )
    if not f_a["running_ok"]:
        gaps.append("5.1 fidéicommis : balance_after_account ne concorde pas sur toutes les lignes")
    if f_a["by_dossier"] != f_b["by_dossier"]:
        gaps.append("5.1 fidéicommis by_dossier : modifié entre avant et après")
    if f_a["by_dossier_nonzero"] != spec.fideicommis_by_dossier:
        gaps.append(
            f"5.1 fidéicommis by_dossier (stocké non nul) : {f_a['by_dossier_nonzero']} ≠ "
            f"{spec.fideicommis_by_dossier}"
        )
    if f_a["recalc_by_dossier_nonzero"] != spec.fideicommis_by_dossier:
        gaps.append(
            f"5.1 fidéicommis by_dossier (recalculé) : {f_a['recalc_by_dossier_nonzero']} ≠ "
            f"{spec.fideicommis_by_dossier}"
        )
    if f_a["carte_client_vide_count"] != 0 or f_a["carte_client_vide_book"] != 0:
        gaps.append(
            f"5.1 carte-client vide : {f_a['carte_client_vide_count']} écriture(s), "
            f"solde {f_a['carte_client_vide_book']} (0 attendu)"
        )
    exp_rec = f_spec.derniere_conciliation_attendue.isoformat() if f_spec.derniere_conciliation_attendue else None
    if f_a["last_reconciliation_date"] != exp_rec:
        gaps.append(
            f"5.1 last_reconciliation_date : {f_a['last_reconciliation_date']} ≠ {exp_rec}"
        )
    for rid, info in f_a["preuves"].items():
        if not info["pre_as_of"] and info["variance"] != 0:
            gaps.append(f"5.1 conciliation fidéicommis {rid} : écart {info['variance']}")
        if info != f_b["preuves"].get(rid):
            gaps.append(f"5.1 conciliation fidéicommis {rid} a changé")

    for alias, aspec in spec.accounts.items():
        if aspec.registre != "administration":
            continue
        ab, aa = before["administration"][alias], after["administration"][alias]
        if aa["display_balance"] != aspec.solde_attendu_cents:
            gaps.append(
                f"5.{alias} : solde affiché {aa['display_balance']} ≠ {aspec.solde_attendu_cents}"
            )
        if aa["stored_ledger"] != aa["recalc_ledger"]:
            gaps.append(
                f"5.{alias} : ledger_balance stocké {aa['stored_ledger']} ≠ recalculé {aa['recalc_ledger']}"
            )
        exp_plancher = aspec.plancher_attendu.isoformat() if aspec.plancher_attendu else None
        if aspec.plancher_attendu is not None and aa["plancher"] != exp_plancher:
            gaps.append(f"5.{alias} : plancher {aa['plancher']} ≠ {exp_plancher}")
        for rid, pinfo in aa["preuves"].items():
            if pinfo.get("ecart"):
                gaps.append(f"5.{alias} conciliation {rid} : écart {pinfo['ecart']}")
        if aspec.intouchable and ab["digest"] != aa["digest"]:
            gaps.append(f"5.4 {aspec.nom_court} : des écritures ont été touchées")

    fac = after["facture_cible"]
    if fac and fac["amount_paid"] != fac["sum_standing_receipts"]:
        gaps.append(
            f"5.5 facture {fac['numero']} : amount_paid {fac['amount_paid']} ≠ "
            f"somme des encaissements debout {fac['sum_standing_receipts']}"
        )
    if before["autres_factures_digest"] != after["autres_factures_digest"]:
        gaps.append("5.5 d'autres factures ont été modifiées")

    if after["dangling"]:
        gaps.append(
            f"5.6 références résiduelles vers les id supprimés : {sorted(after['dangling'])}"
        )

    if before["etats_financiers"] != after["etats_financiers"]:
        gaps.append(
            f"5.7 états financiers modifiés : avant={before['etats_financiers']} "
            f"après={after['etats_financiers']}"
        )
    if after["etats_financiers"]["ecart_non_explique"] != 0:
        gaps.append(
            f"5.7 états financiers : écart non expliqué = {after['etats_financiers']['ecart_non_explique']}"
        )

    # 5.8 counts + counters unchanged
    if before["counters"] != after["counters"]:
        gaps.append("5.8 les compteurs de séquence ont été modifiés")

    eligible_codes = {v.spec.code for v in verdicts if v.state == PENDING}
    all_eligible = len(eligible_codes) == len(spec.pairs)
    if all_eligible:
        if f_a["count"] - f_b["count"] != f_spec.delta_nombre_ecritures:
            gaps.append(
                f"5.8 fidéicommis : delta écritures {f_a['count'] - f_b['count']} ≠ "
                f"{f_spec.delta_nombre_ecritures}"
            )
        for alias, aspec in spec.accounts.items():
            if aspec.registre != "administration":
                continue
            ab, aa = before["administration"][alias], after["administration"][alias]
            if aa["count"] - ab["count"] != aspec.delta_nombre_ecritures:
                gaps.append(
                    f"5.8 {alias} : delta écritures {aa['count'] - ab['count']} ≠ "
                    f"{aspec.delta_nombre_ecritures}"
                )

    return gaps


def _scan_out_of_scope(spec: PlanSpec, world: World) -> list[dict]:
    """Section 7 read-only scan: other reversals or annulled entries not in the 10 IDs."""
    target_ids = spec.all_deleted_ids
    out: list[dict] = []
    for reg, col, nkey in (
        ("fidéicommis", TRUST_TX, "purpose"),
        ("administration", ADMIN_TX, "kind"),
    ):
        for doc_id, doc in sorted(world.docs[col].items()):
            if doc_id in target_ids:
                continue
            is_rev = bool(
                doc.get("reverses_id")
                or doc.get("reversed_by_id")
                or doc.get("status") == "annulée"
                or doc.get(nkey) == "correction"
            )
            if not is_rev:
                continue
            out.append(
                {
                    "registre": reg,
                    "id": doc_id,
                    "account_id": doc.get("account_id", ""),
                    "sequence": doc.get("sequence"),
                    "date": _doc_day_str(doc.get("date")) or "?",
                    "nature": doc.get(nkey, ""),
                    "direction": doc.get("direction", ""),
                    "amount": int(doc.get("amount") or 0),
                    "status": doc.get("status", ""),
                    "reverses_id": _norm_id(doc.get("reverses_id")),
                    "reversed_by_id": _norm_id(doc.get("reversed_by_id")),
                }
            )
    return out


def _scan_receipts(verdicts: list[PairVerdict]) -> list[str]:
    notes: list[str] = []
    for v in verdicts:
        for _col, did, _snap, doc, _role in v.deletes:
            path = doc.get("receipt_storage_path")
            if path:
                notes.append(f"{v.spec.code} · `{did}` porte une pièce justificative (`{path}`)")
    return notes


# ── Plan assembly & fingerprint ───────────────────────────────────────────


@dataclass
class PreparedRun:
    plan_path: Path
    plan_sha: str
    project: str
    today: date
    now: datetime
    spec: PlanSpec
    world: World
    verdicts: list[PairVerdict]
    replacements: list[tuple[str, str, bool, str]]
    idem_targets: list[IdempotencyTarget]
    out_of_scope: list[dict]
    receipt_notes: list[str]
    before: dict
    simulated: dict
    gaps: list[str]
    fingerprint: str

    @property
    def eligible_verdicts(self) -> list[PairVerdict]:
        return [v for v in self.verdicts if v.state == PENDING]

    @property
    def blocking(self) -> list[str]:
        out: list[str] = []
        for v in self.verdicts:
            out.extend(v.blocking)
        for _rid, _reg, ok, msg in self.replacements:
            if not ok:
                out.append(f"écriture de remplacement {_rid} : {msg}")
        out.extend(self.gaps)
        return out


def _compute_fingerprint(
    plan_sha: str,
    project: str,
    verdicts: list[PairVerdict],
    idem_targets: list[IdempotencyTarget],
) -> str:
    pair_rows = []
    for v in verdicts:
        pair_rows.append(
            {
                "code": v.spec.code,
                "state": v.state,
                "deletes": sorted(
                    (col, did, rla._stamp_iso(snap.update_time))
                    for col, did, snap, _doc, _role in v.deletes
                ),
                "updates": sorted(
                    (
                        u.collection,
                        u.doc_id,
                        rla._stamp_iso(u.snap.update_time),
                        {k: u.payload[k] for k in sorted(u.business)},
                    )
                    for u in v.updates
                ),
            }
        )
    idem_rows = sorted(
        (t.doc_id, rla._stamp_iso(t.snap.update_time)) for t in idem_targets
    )
    return rla._digest(
        {
            "plan_sha256": plan_sha,
            "projet": project,
            "paires": pair_rows,
            "idempotency": idem_rows,
        }
    )[:FINGERPRINT_HEX]


def prepare(plan_path: Path) -> PreparedRun:
    data, plan_sha = rx._load_json(plan_path, "plan")
    spec = parse_plan(data)
    world = read_world(spec)
    now = deadlines.now_utc()
    today = deadlines.today_mtl()

    verdicts = [_judge_pair(p, world, plan_sha, spec.motif, now) for p in spec.pairs]
    active_deleted_ids = {
        did
        for v in verdicts
        if v.state in (PENDING, DONE)
        for d in v.spec.documents
        for did in (d.id,)
    }
    replacements = _check_replacements(spec, world)
    idem_targets = _find_idempotency_targets(spec, world, active_deleted_ids, now)
    out_of_scope = _scan_out_of_scope(spec, world)
    receipt_notes = _scan_receipts(verdicts)

    before = _compute_controls(world, spec, today, now)
    sim_world = _simulate_world(world, verdicts, idem_targets)
    simulated = _compute_controls(sim_world, spec, today, now)
    gaps = _evaluate_controls(spec, before, simulated, verdicts)
    fingerprint = _compute_fingerprint(plan_sha, db.project, verdicts, idem_targets)

    return PreparedRun(
        plan_path=plan_path,
        plan_sha=plan_sha,
        project=db.project,
        today=today,
        now=now,
        spec=spec,
        world=world,
        verdicts=verdicts,
        replacements=replacements,
        idem_targets=idem_targets,
        out_of_scope=out_of_scope,
        receipt_notes=receipt_notes,
        before=before,
        simulated=simulated,
        gaps=gaps,
        fingerprint=fingerprint,
    )


# ── Backup (typed JSON, fsynced, verified before any write) ───────────────


def write_backup(
    directory: Path, run: PreparedRun, audit_ids: dict[str, str]
) -> tuple[Path, str]:
    created = deadlines.now_utc()
    suppressions = []
    modifications = []
    for v in run.eligible_verdicts:
        for col, did, snap, doc, role in v.deletes:
            suppressions.append(
                {
                    "paire": v.spec.code,
                    "collection": col,
                    "id": did,
                    "role": role,
                    "update_time_lu": rla._stamp_iso(snap.update_time),
                    "document": rla._typed(doc),
                }
            )
        for upd in v.updates:
            mod_entry: dict[str, Any] = {
                "paire": v.spec.code,
                "collection": upd.collection,
                "id": upd.doc_id,
                "role": upd.role,
                "update_time_lu": rla._stamp_iso(upd.snap.update_time),
                "etag_migration": upd.payload["etag"],
                "champs": sorted(upd.business),
                "avant": rla._typed(upd.before),
                "apres": rla._typed({k: upd.payload[k] for k in sorted(upd.business)}),
            }
            if upd.collection == INVOICES:
                mod_entry["lineitems"] = rla._typed(
                    run.world.invoice_lines.get(upd.doc_id) or []
                )
            modifications.append(mod_entry)

    idem_saved = [
        {
            "id": t.doc_id,
            "update_time_lu": rla._stamp_iso(t.snap.update_time),
            "raison": t.reason,
            "expiree": t.expired,
            "document": rla._typed(t.doc),
        }
        for t in run.idem_targets
    ]
    contenu = {
        "suppressions": suppressions,
        "modifications": modifications,
        "idempotency_supprimes": idem_saved,
    }
    body = {
        "format": BACKUP_FORMAT,
        "cree_le": created.isoformat(),
        "projet": run.project,
        "provenance": "script",
        "motif": run.spec.motif,
        "plan": {"chemin": str(run.plan_path), "sha256": run.plan_sha},
        "empreinte": run.fingerprint,
        "audit_events_crees": [
            {"paire": code, "id": aid} for code, aid in sorted(audit_ids.items())
        ],
        **contenu,
        "contenu_sha256": rla._digest(contenu),
    }
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=1, allow_nan=False)
    path = directory / (
        f"purge-paires-annulees-{created.strftime('%Y%m%dT%H%M%S%fZ')}"
        f"-{run.fingerprint}.json"
    )
    with open(path, "x", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    reread = path.read_bytes()
    parsed = json.loads(reread.decode("utf-8"))
    parsed_contenu = {
        "suppressions": parsed.get("suppressions"),
        "modifications": parsed.get("modifications"),
        "idempotency_supprimes": parsed.get("idempotency_supprimes"),
    }
    if parsed != json.loads(text) or parsed.get("contenu_sha256") != rla._digest(
        parsed_contenu
    ):
        raise OSError("la sauvegarde relue ne concorde pas")
    for saved, orig_pair in zip(
        parsed["suppressions"],
        [item for v in run.eligible_verdicts for item in v.deletes],
    ):
        if rla._untyped(saved["document"]) != orig_pair[3]:
            raise OSError(f"la sauvegarde relue de {saved['id']} ne concorde pas")
    return path, hashlib.sha256(reread).hexdigest()


# ── Transactional pair application ────────────────────────────────────────


class _ConflictAbort(Exception):
    pass


class _AlreadyApplied(Exception):
    pass


def _build_audit_doc(
    verdict: PairVerdict,
    audit_id: str,
    motif: str,
    plan_sha: str,
    fingerprint: str,
    backup_path: Path,
    backup_sha: str,
    now: datetime,
) -> dict:
    pair = verdict.spec
    seqs = ", ".join(str(d.sequence) for d in pair.documents)
    title = sanitize(
        f"Purge paire {pair.code} — {pair.registre} (séq. {seqs})", max_length=300
    )
    dossier_ids = {
        (doc.get("dossier_id") or "")
        for _c, _id, _s, doc, _r in verdict.deletes
        if doc.get("dossier_id")
    }
    dossier_id = next(iter(dossier_ids)) if len(dossier_ids) == 1 else ""
    return {
        "id": audit_id,
        "at": now,
        "entity_type": ENTITY_TYPE,
        "entity_id": pair.documents[0].id,
        "dossier_id": dossier_id,
        "snapshot_min": {"title": title, "status": "annulée"},
        "created_at": now,
        "migration": {
            "paire": pair.code,
            "registre": pair.registre,
            "ids": [d.id for d in pair.documents],
            "sequences": [
                {"id": d.id, "account_id": d.account_id, "sequence": d.sequence}
                for d in pair.documents
            ],
            "comptes": sorted({d.account_id for d in pair.documents}),
            "montants_cents": [
                {"id": d.id, "direction": d.direction, "amount": d.amount}
                for d in pair.documents
            ],
            "dates": [
                {"id": d.id, "date": d.date.isoformat()} for d in pair.documents
            ],
            "motif": motif,
            "provenance": "script",
            "horodatage": now,
            "sauvegarde_chemin": str(backup_path),
            "sauvegarde_sha256": backup_sha,
            "plan_sha256": plan_sha,
            "empreinte": fingerprint,
            "documents_modifies": [u.doc_id for u in verdict.updates],
        },
    }


def _settle_pair(verdict: PairVerdict, audit_id: str) -> str:
    try:
        audit_snap = db.collection(AUDIT_EVENTS).document(audit_id).get()
        del_snaps = [
            db.collection(col).document(did).get()
            for col, did, _s, _d, _r in verdict.deletes
        ]
        upd_snaps = [
            (db.collection(u.collection).document(u.doc_id).get(), u.payload["etag"])
            for u in verdict.updates
        ]
    except Exception:
        return UNCERTAIN
    if (
        audit_snap.exists
        and all(not s.exists for s in del_snaps)
        and all((s.to_dict() or {}).get("etag") == etag for s, etag in upd_snaps)
    ):
        return LOST_BUT_WRITTEN
    if (
        not audit_snap.exists
        and all(s.exists for s in del_snaps)
        and all((s.to_dict() or {}).get("etag") != etag for s, etag in upd_snaps)
    ):
        return NOTHING
    return UNCERTAIN


def apply_pair(
    verdict: PairVerdict,
    audit_id: str,
    run: PreparedRun,
    backup_path: Path,
    backup_sha: str,
) -> tuple[str, str]:
    del_refs = [
        (db.collection(col).document(did), snap.update_time)
        for col, did, snap, _doc, _role in verdict.deletes
    ]
    upd_refs = [
        (db.collection(u.collection).document(u.doc_id), u.snap.update_time, u.payload)
        for u in verdict.updates
    ]
    extra_refs = [
        (db.collection(col).document(eid), snap.update_time)
        for col, eid, snap in verdict.extra_reads
    ]
    audit_ref = db.collection(AUDIT_EVENTS).document(audit_id)

    @firestore.transactional
    def _txn(txn) -> None:
        # 1. ALL reads first (never ReadAfterWrite)
        audit_snap = audit_ref.get(transaction=txn)
        del_snaps = [(ref, expected_ut, ref.get(transaction=txn)) for ref, expected_ut in del_refs]
        upd_snaps = [
            (ref, expected_ut, payload, ref.get(transaction=txn))
            for ref, expected_ut, payload in upd_refs
        ]
        extra_snaps = [
            (ref, expected_ut, ref.get(transaction=txn)) for ref, expected_ut in extra_refs
        ]

        if audit_snap.exists and all(not s.exists for _, _, s in del_snaps):
            raise _AlreadyApplied()

        for ref, expected_ut, s in del_snaps:
            if not s.exists:
                raise _ConflictAbort(f"{ref.path} introuvable dans la transaction")
            if s.update_time != expected_ut:
                raise _ConflictAbort(f"{ref.path} modifié depuis la lecture")
        for ref, expected_ut, _p, s in upd_snaps:
            if not s.exists:
                raise _ConflictAbort(f"{ref.path} introuvable dans la transaction")
            if s.update_time != expected_ut:
                raise _ConflictAbort(f"{ref.path} modifié depuis la lecture")
        for ref, expected_ut, s in extra_snaps:
            if not s.exists or s.update_time != expected_ut:
                raise _ConflictAbort(f"{ref.path} modifié depuis la lecture")

        now = deadlines.now_utc()
        audit_doc = _build_audit_doc(
            verdict,
            audit_id,
            run.spec.motif,
            run.plan_sha,
            run.fingerprint,
            backup_path,
            backup_sha,
            now,
        )
        # 2. Staged writes
        for ref, _ut, _s in del_snaps:
            txn.delete(ref)
        for ref, _ut, payload, _s in upd_snaps:
            txn.update(ref, payload)
        txn.create(audit_ref, audit_doc)

    try:
        _txn(db.transaction())
        return WRITTEN, ""
    except _AlreadyApplied:
        return LOST_BUT_WRITTEN, "déjà appliquée dans une tentative antérieure"
    except _ConflictAbort as exc:
        return NOTHING, str(exc)
    except Exception as exc:
        return _settle_pair(verdict, audit_id), type(exc).__name__


def delete_idempotency_targets(targets: list[IdempotencyTarget]) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    for t in targets:
        ref = db.collection(MCP_IDEMPOTENCY).document(t.doc_id)
        try:
            ref.delete(option=db.write_option(last_update_time=t.snap.update_time))
            results.append((t.doc_id, "supprimée"))
        except gexc.NotFound:
            results.append((t.doc_id, "déjà absente (TTL)"))
        except gexc.FailedPrecondition:
            results.append((t.doc_id, "modifiée depuis la lecture — ignorée"))
        except Exception as exc:
            results.append((t.doc_id, f"erreur ({type(exc).__name__})"))
    return results


# ── Restoration (--restaurer) ─────────────────────────────────────────────


@dataclass
class RestorePair:
    code: str
    state: str
    deletes: list[dict]
    mods: list[dict]
    refusals: list[str] = field(default_factory=list)


@dataclass
class RestorePlan:
    path: Path
    sha: str
    project: str
    created: str
    migration_fingerprint: str
    pairs: list[RestorePair]
    fingerprint: str
    blocking: list[str]


def prepare_restore(backup_path: Path) -> RestorePlan:
    body, sha = rx._load_json(backup_path, "sauvegarde")
    if body.get("format") != BACKUP_FORMAT:
        raise RunRefused(
            f"sauvegarde d'un autre format ({body.get('format')!r}) — attendu {BACKUP_FORMAT}"
        )
    if body.get("projet") != db.project:
        raise RunRefused(
            f"sauvegarde du projet « {body.get('projet')} », base « {db.project} »"
        )
    contenu = {
        "suppressions": body.get("suppressions"),
        "modifications": body.get("modifications"),
        "idempotency_supprimes": body.get("idempotency_supprimes"),
    }
    if body.get("contenu_sha256") != rla._digest(contenu):
        raise RunRefused("sauvegarde altérée : contenu_sha256 discordant")

    suppressions = contenu["suppressions"] or []
    modifications = contenu["modifications"] or []
    by_pair: dict[str, dict[str, list]] = {}
    for item in suppressions:
        by_pair.setdefault(item["paire"], {"del": [], "mod": []})["del"].append(item)
    for item in modifications:
        by_pair.setdefault(item["paire"], {"del": [], "mod": []})["mod"].append(item)

    restore_pairs: list[RestorePair] = []
    blocking: list[str] = []
    fp_rows = []
    for code in sorted(by_pair):
        grp = by_pair[code]
        refusals: list[str] = []
        del_states = set()
        for item in grp["del"]:
            snap = db.collection(item["collection"]).document(item["id"]).get()
            if snap.exists:
                if snap.to_dict() == rla._untyped(item["document"]):
                    del_states.add(RESTORE_DONE)
                else:
                    refusals.append(
                        f"{item['collection']}/{item['id']} existe avec un autre contenu"
                    )
            else:
                del_states.add(RESTORE_PENDING)
        mod_items = []
        for item in grp["mod"]:
            snap = db.collection(item["collection"]).document(item["id"]).get()
            if not snap.exists:
                refusals.append(f"{item['collection']}/{item['id']} introuvable")
                continue
            cur = snap.to_dict() or {}
            avant = rla._untyped(item["avant"])
            apres = rla._untyped(item["apres"])
            fields = list(item["champs"])
            if cur.get("etag") == item["etag_migration"]:
                if any(rx._aware(cur.get(f)) != rx._aware(apres.get(f)) for f in fields):
                    refusals.append(
                        f"{item['collection']}/{item['id']} : etag migration mais champs divergents"
                    )
                else:
                    del_states.add(RESTORE_PENDING)
                    mod_items.append({**item, "_snap": snap, "_cur": cur})
            elif all(rx._aware(cur.get(f)) == rx._aware(avant.get(f)) for f in fields):
                del_states.add(RESTORE_DONE)
            else:
                refusals.append(
                    f"{item['collection']}/{item['id']} ne porte plus l'etag de la migration"
                )
        if refusals:
            state = REFUSED
            blocking.extend(refusals)
        elif del_states == {RESTORE_DONE}:
            state = RESTORE_DONE
        elif del_states == {RESTORE_PENDING}:
            state = RESTORE_PENDING
        else:
            state = REFUSED
            msg = f"{code} — état mixte à la restauration"
            refusals.append(msg)
            blocking.append(msg)
        restore_pairs.append(
            RestorePair(
                code=code, state=state, deletes=grp["del"], mods=mod_items, refusals=refusals
            )
        )
        fp_rows.append(
            {
                "code": code,
                "state": state,
                "ids": sorted(d["id"] for d in grp["del"]),
                "mods": sorted(
                    (m["collection"], m["id"], rla._stamp_iso(m["_snap"].update_time))
                    for m in mod_items
                ),
            }
        )

    fp = rla._digest({"sauvegarde_sha256": sha, "projet": db.project, "paires": fp_rows})[
        :FINGERPRINT_HEX
    ]
    return RestorePlan(
        path=backup_path,
        sha=sha,
        project=db.project,
        created=str(body.get("cree_le")),
        migration_fingerprint=str(body.get("empreinte")),
        pairs=restore_pairs,
        fingerprint=fp,
        blocking=blocking,
    )


def apply_restore_pair(rpair: RestorePair, rplan: RestorePlan) -> tuple[str, str]:
    now = deadlines.now_utc()
    del_refs = [
        (db.collection(item["collection"]).document(item["id"]), rla._untyped(item["document"]))
        for item in rpair.deletes
    ]
    mod_specs = []
    for item in rpair.mods:
        cur = item["_cur"]
        avant = rla._untyped(item["avant"])
        fields = list(item["champs"])
        business = {f: avant.get(f) for f in fields}
        with provenance.writing_via("script"):
            payload = {**business, **provenance.update_fields(now)}
            if item["collection"] == TRUST_TX:
                payload["revisions"] = list(cur.get("revisions") or []) + [
                    {
                        "at": now,
                        "via": provenance.current_via(),
                        "motif": RESTORE_MOTIF.format(
                            quand=rla._when(rplan.created), paire=rpair.code
                        ),
                        "changes": {f: [cur.get(f), business[f]] for f in fields},
                        "updated_at_before": cur.get("updated_at"),
                        "source_sha256": rplan.sha,
                    }
                ]
        mod_specs.append(
            (
                db.collection(item["collection"]).document(item["id"]),
                item["_snap"].update_time,
                payload,
            )
        )

    @firestore.transactional
    def _txn(txn) -> None:
        del_snaps = [(ref, doc, ref.get(transaction=txn)) for ref, doc in del_refs]
        mod_snaps = [
            (ref, expected_ut, payload, ref.get(transaction=txn))
            for ref, expected_ut, payload in mod_specs
        ]
        for ref, _doc, s in del_snaps:
            if s.exists:
                raise _ConflictAbort(f"{ref.path} existe déjà")
        for ref, expected_ut, _p, s in mod_snaps:
            if not s.exists or s.update_time != expected_ut:
                raise _ConflictAbort(f"{ref.path} modifié depuis la lecture")
        for ref, doc, _s in del_snaps:
            txn.create(ref, doc)
        for ref, _ut, payload, _s in mod_snaps:
            txn.update(ref, payload)

    try:
        _txn(db.transaction())
        return WRITTEN, ""
    except _ConflictAbort as exc:
        return NOTHING, str(exc)
    except Exception as exc:
        return UNCERTAIN, type(exc).__name__


# ── Reporting & CLI ───────────────────────────────────────────────────────


def _trust_gap_review_snippet(after_ctrl: dict, ref_day: date) -> Optional[dict]:
    f = after_ctrl["fideicommis"]
    missing = f["missing_seqs"]
    if not missing:
        return None
    text = (
        f"compte {f['id']}: numéro(s) {vti._number_ranges(missing)} absent(s) du "
        f"registre — l'application ne supprime jamais une écriture au "
        f"fidéicommis : celle-ci l'a été hors de l'application. Le registre "
        f"de l'art. 38 doit être complet, même quand les soldes figés autour "
        f"du trou concordent encore ; à rétablir depuis une sauvegarde, ou à "
        f"expliquer (revue)."
    )
    key = findings_review.finding_key(text, {})
    return {
        "cle": key,
        "revu_le": ref_day.isoformat(),
        "motif": (
            f"Suppression de la paire annulée P1 (séquences "
            f"{vti._number_ranges(missing)}) — vérification des journaux du "
            f"{ref_day.isoformat()}"
        ),
    }


def _render_report(
    run: PreparedRun,
    *,
    mode: str,
    approved: Optional[str] = None,
    refusal: Optional[list[str]] = None,
    pair_outcomes: Optional[list[tuple[str, str, str]]] = None,
    idem_outcomes: Optional[list[tuple[str, str]]] = None,
    backup: Optional[tuple[Path, str]] = None,
    real: Optional[dict] = None,
    real_gaps: Optional[list[str]] = None,
    drift: Optional[list[str]] = None,
    audit_ids: Optional[dict[str, str]] = None,
) -> str:
    out = [
        f"# Purge des paires d'écritures annulées — vérification du {run.spec.date_reference.isoformat()}",
        "",
        f"- Date : {rla._now_line()}",
        f"- Mode : **{mode}**",
        f"- Projet : `{run.project}`",
        f"- Plan : `{run.plan_path}` (SHA-256 `{run.plan_sha}`)",
        f"- Empreinte : **{run.fingerprint}**",
    ]
    if approved:
        out.append(f"- Empreinte approuvée : `{approved}`")
    if backup:
        out += [
            f"- Sauvegarde permanente : `{backup[0]}`",
            f"- SHA-256 de la sauvegarde : `{backup[1]}`",
        ]
    out += ["", "## Plan de suppression par paire (1 transaction atomique par paire)", ""]
    plan_rows = []
    for v in run.verdicts:
        del_summary = ", ".join(
            f"{d.role} `{d.id}` (séq. {d.sequence}, {d.date.isoformat()}, "
            f"{d.direction} {rla._money(d.amount)})"
            for d in v.spec.documents
        )
        upd_summary = (
            ", ".join(f"{u.role} `{u.doc_id}` ({', '.join(sorted(u.business))})" for u in v.updates)
            or "aucune écriture dérivée"
        )
        plan_rows.append((v.spec.code, v.spec.registre, v.state, del_summary, upd_summary))
    out += rx._table(
        ("Paire", "Registre", "État", "Suppressions", "Mises à jour dérivées"),
        plan_rows,
    )
    out += ["", "## 1. Statut par paire et comparaisons", ""]
    for v in run.verdicts:
        out += [f"### Paire {v.spec.code} ({v.spec.registre}) — « {v.state} »", ""]
        if v.table:
            out += rx._table(
                ("Élément", "Champ", "Attendu (plan)", "Base", "Concordance"),
                [(e, f, w, h, "conforme" if ok else "**ÉCART**") for e, f, w, h, ok in v.table],
            )
            out.append("")
        if v.blocking:
            out.append("**Blocages / écarts sur cette paire (non appliquée) :**")
            out += [f"- {b}" for b in v.blocking]
            out.append("")
        if v.desc_preview:
            old_d, new_d = v.desc_preview
            out += [
                "#### Description de l'écriture de fidéicommis 133 (avant / après)",
                "",
                f"- Avant : `{old_d}`",
                f"- Après : `{new_d}`",
                "",
            ]
        if v.invoice_preview:
            ip = v.invoice_preview
            out += [
                f"#### Facture {ip['numero']} (`{ip['id']}`)",
                "",
                "- Encaissements debout restants après suppression de P2 : "
                + (
                    ", ".join(
                        f"séq. {r['sequence']} (`{r['id']}`, {r['date']}, {rla._money(r['amount'])})"
                        for r in ip["standing_receipts"]
                    )
                    or "aucun"
                ),
            ]
            out += rx._table(
                ("Champ", "Avant", "Après (dérivé par le modèle)"),
                [
                    (k, str(ip["before"][k]), str(ip["after"][k]))
                    for k in ("status", "amount_paid", "balance", "payment_basis", "paid_date")
                ],
            )
            out.append("")

    if refusal:
        out += ["## Refus d'exécution", ""] + [f"- {r}" for r in refusal] + [""]

    out += ["## 2. Écritures de remplacement (contrôle seulement)", ""]
    out += rx._table(
        ("ID", "Registre", "État", "Détail"),
        [
            (rid, reg, "conforme" if ok else "**ÉCART**", msg)
            for rid, reg, ok, msg in run.replacements
        ],
    )
    out += ["", "## 3. Architecture des soldes, paiements et trace", "",
            "- **Soldes courants :** stockés au fidéicommis (`balance_after_account`, recalculés "
            "atomiquement sur les écritures postérieures à 130) ; calculés à la lecture en "
            "administration (`admin_ledger.running_balances`).",
            "- **Paiements de facture :** stockés sur `admin_transactions` (`encaissement_facture`) "
            "et projetés sur la facture (`amount_paid`, `paid_date`, `status`) par "
            "`invoice.payment_updates` ; aucune table de paiements séparée.",
            "- **Trace d'audit :** 1 document par paire dans `audit_events` (`entity_type = "
            f"`{ENTITY_TYPE}``, masqué du connecteur MCP), écrit dans la même transaction "
            "que la suppression, avec provenance `script` et référence à la sauvegarde JSON.",
            "- **Emplacement prévu de la sauvegarde :** dossier externe passé par `--sauvegarde` "
            f"(fichier `purge-paires-annulees-<UTC>-{run.fingerprint}.json`, vérifié par "
            "relecture et `fsync` avant toute écriture)."]
    if audit_ids:
        out += ["", "Identifiants `audit_events` créés : " + ", ".join(
            f"{k}: `{v}`" for k, v in sorted(audit_ids.items())
        )]

    out += ["", "## 4. Journal de rejeu MCP (`mcp_idempotency`)", ""]
    if run.idem_targets:
        out += rx._table(
            ("Document ID", "Expirée", "Raison"),
            [(t.doc_id, "oui" if t.expired else "non", t.reason) for t in run.idem_targets],
        )
    else:
        out.append("Aucun enregistrement correspondant trouvé dans `mcp_idempotency`.")
    if idem_outcomes:
        out += ["", "Issue sur `mcp_idempotency` :"] + [
            f"- `{did}` : {st}" for did, st in idem_outcomes
        ]

    if pair_outcomes:
        out += ["", "## 5. Issue des transactions par paire", ""] + [
            f"- Paire {code} : **{outcome}**" + (f" ({detail})" if detail else "")
            for code, outcome, detail in pair_outcomes
        ]

    out += ["", "## 6. Contrôles avant et après (Section 5)", ""]
    cols = ("Contrôle", "Avant", "Après (simulé)") + (("Après (réel)",) if real else ())
    after_show = real or run.simulated

    def _row_ctrl(label: str, getter):
        cells = [label, getter(run.before), getter(run.simulated)]
        if real:
            cells.append(getter(real))
        return tuple(cells)

    ctrl_rows = [
        _row_ctrl(
            "1. Fidéicommis soldes (livres / compensé)",
            lambda c: f"{c['fideicommis']['recalc_book']} / {c['fideicommis']['recalc_bank']} "
            f"(running_ok={c['fideicommis']['running_ok']})",
        ),
        _row_ctrl(
            "1. Fidéicommis by_dossier (non nuls)",
            lambda c: "; ".join(f"{k}={v}" for k, v in c["fideicommis"]["by_dossier_nonzero"].items()),
        ),
        _row_ctrl(
            f"1. Carte-client vide {run.spec.carte_client_vide['dossier_file_number']}",
            lambda c: f"{c['fideicommis']['carte_client_vide_count']} écr., "
            f"recalc={c['fideicommis']['carte_client_vide_book']}, "
            f"dossier={c['fideicommis']['carte_client_vide_stored']}",
        ),
        _row_ctrl(
            "1. Dernière conciliation fidéicommis",
            lambda c: str(c["fideicommis"]["last_reconciliation_date"]),
        ),
        _row_ctrl(
            "2. Wealthsimple solde",
            lambda c: str((c["administration"].get("wealthsimple") or {}).get("display_balance")),
        ),
        _row_ctrl(
            "3. Carte de crédit (solde dû / plancher)",
            lambda c: f"{(c['administration'].get('carte') or {}).get('display_balance')} / "
            f"{(c['administration'].get('carte') or {}).get('plancher')}",
        ),
        _row_ctrl(
            "4. Banque Nationale (solde)",
            lambda c: str(
                (c["administration"].get("banque_nationale") or {}).get("display_balance")
            ),
        ),
        _row_ctrl(
            f"5. Facture {run.before['facture_cible'].get('numero', 'cible')} (statut / payé / solde / paid_date)",
            lambda c: (
                f"{c['facture_cible'].get('status')} / {c['facture_cible'].get('amount_paid')} / "
                f"{c['facture_cible'].get('balance')} / {c['facture_cible'].get('paid_date')}"
            ),
        ),
        _row_ctrl(
            "5. Factures intouchables",
            lambda c: "; ".join(
                f"{num}: {info['status']} ({info['amount_paid']}/{info['balance']})"
                for num, info in sorted(c["factures_intouchables"].items())
            ),
        ),
        _row_ctrl(
            "6. Références résiduelles vers id supprimés",
            lambda c: f"{len(c['dangling'])} référence(s)",
        ),
        _row_ctrl(
            f"7. États financiers ({run.spec.period.start.isoformat()[:7]} : rev / dép / prél / app / écart)",
            lambda c: (
                f"{c['etats_financiers']['revenus']} / "
                f"{c['etats_financiers']['depenses_total']} / "
                f"{c['etats_financiers']['prelevements']} / "
                f"{c['etats_financiers']['apports']} / "
                f"{c['etats_financiers']['ecart_non_explique']}"
            ),
        ),
        _row_ctrl(
            "8. Nombre d'écritures (fidéicommis / WS / carte / BN)",
            lambda c: (
                f"{c['fideicommis']['count']} / "
                f"{(c['administration'].get('wealthsimple') or {}).get('count')} / "
                f"{(c['administration'].get('carte') or {}).get('count')} / "
                f"{(c['administration'].get('banque_nationale') or {}).get('count')}"
            ),
        ),
    ]
    out += rx._table(cols, ctrl_rows)
    all_gaps = run.gaps + list(real_gaps or [])
    out += ["", "**Écarts de contrôles :** " + ("aucun" if not all_gaps else "; ".join(all_gaps))]
    if drift:
        out += ["**Dérive simulé ↔ réel :** " + "; ".join(drift)]

    if run.receipt_notes:
        out += ["", "### Pièces justificatives sur les écritures supprimées", ""] + [
            f"- {n}" for n in run.receipt_notes
        ]

    snippet = _trust_gap_review_snippet(after_show, run.spec.date_reference)
    if snippet:
        out += [
            "",
            "### Entrée `--revue` pour le contrôle 12 (`verify_trust_integrity`)",
            "",
            "```json",
            json.dumps([snippet], ensure_ascii=False, indent=2),
            "```",
        ]

    out += ["", "## 7. Écritures hors périmètre (contre-passations et annulées conservées)", ""]
    if run.out_of_scope:
        out += rx._table(
            ("Registre", "ID", "Séq.", "Date", "Nature", "Sens", "Montant", "Statut"),
            [
                (
                    r["registre"],
                    r["id"],
                    r["sequence"],
                    r["date"],
                    r["nature"],
                    r["direction"],
                    r["amount"],
                    r["status"],
                )
                for r in run.out_of_scope
            ],
        )
    else:
        out.append("Aucune autre contre-passation ni écriture annulée dans les registres.")

    return "\n".join(out) + "\n"


def _render_restore_report(
    rplan: RestorePlan,
    *,
    mode: str,
    approved: Optional[str] = None,
    refusal: Optional[list[str]] = None,
    outcomes: Optional[list[tuple[str, str, str]]] = None,
) -> str:
    out = [
        "# Restauration des paires annulées",
        "",
        f"- Date : {rla._now_line()}",
        f"- Mode : **{mode}**",
        f"- Projet : `{rplan.project}`",
        f"- Sauvegarde : `{rplan.path}` (SHA-256 `{rplan.sha}`)",
        f"- Empreinte : **{rplan.fingerprint}**",
    ]
    if approved:
        out.append(f"- Empreinte approuvée : `{approved}`")
    out += ["", "## Paires", ""]
    for p in rplan.pairs:
        out.append(f"- **{p.code}** : {p.state} ({len(p.deletes)} suppression(s), {len(p.mods)} modification(s))")
        for r in p.refusals:
            out.append(f"  - REFUS : {r}")
    if refusal:
        out += ["", "## Refus", ""] + [f"- {r}" for r in refusal]
    if outcomes:
        out += ["", "## Issue", ""] + [
            f"- {code} : **{o}**" + (f" ({d})" if d else "") for code, o, d in outcomes
        ]
    return "\n".join(out) + "\n"


def _run_migration(
    report: Path, source: Path, backup_dir: Optional[Path], approved: Optional[str]
) -> int:
    try:
        run = prepare(source)
    except RunRefused as exc:
        return rla._finish(
            report,
            f"# Purge des paires annulées\n\n- Date : {rla._now_line()}\n- Plan : `{source}`\n\n"
            f"**Refusé — rien n'a été écrit** : {exc}\n",
            [f"Refusé — rien n'a été écrit : {exc}"],
            1,
        )

    summary = [
        f"Purge des paires annulées — {'à blanc' if approved is None else 'application'}",
        f"Projet : {run.project}",
        f"Plan : {run.plan_path} (SHA-256 {run.plan_sha})",
        "Paires : "
        + ", ".join(f"{v.spec.code}={v.state}" for v in run.verdicts),
        f"Journal de rejeu : {len(run.idem_targets)} entrée(s) ciblée(s)",
        "Contrôles simulés : " + ("conformes" if not run.gaps else f"{len(run.gaps)} écart(s)"),
        f"Empreinte : {run.fingerprint}",
    ]
    if approved is None:
        text = _render_report(run, mode="à blanc")
        if run.blocking:
            summary.append(
                f"Des blocages existent ({len(run.blocking)}) — voir le rapport. Rien n'a été écrit."
            )
            return rla._finish(report, text, summary, 1)
        if run.eligible_verdicts or run.idem_targets:
            summary.append(
                "À blanc : rien n'a été écrit. Pour appliquer après approbation : "
                f"--sauvegarde DOSSIER --appliquer {run.fingerprint}"
            )
        else:
            summary.append("À blanc : rien à appliquer (déjà appliquée).")
        return rla._finish(report, text, summary, 0)

    refusal: list[str] = []
    if approved != run.fingerprint:
        refusal.append(
            f"l'empreinte a changé ({approved} → {run.fingerprint}) : relancez à blanc"
        )
    if run.gaps:
        refusal.extend(run.gaps)
    for _rid, _reg, ok, msg in run.replacements:
        if not ok:
            refusal.append(f"écriture de remplacement {_rid} : {msg}")
    if refusal:
        text = _render_report(run, mode="application refusée", approved=approved, refusal=refusal)
        return rla._finish(report, text, summary + [f"Refusé : {r}" for r in refusal], 1)

    if not run.eligible_verdicts and not run.idem_targets:
        text = _render_report(run, mode="application (déjà fait)", approved=approved)
        return rla._finish(report, text, summary + ["Rien à écrire : déjà appliquée."], 0)

    audit_ids = {v.spec.code: str(uuid.uuid4()) for v in run.eligible_verdicts}
    try:
        backup = write_backup(backup_dir, run, audit_ids)
    except Exception as exc:
        msg = f"sauvegarde impossible ({type(exc).__name__}) — rien n'a été écrit"
        text = _render_report(
            run, mode="application refusée", approved=approved, refusal=[msg]
        )
        return rla._finish(report, text, summary + [f"Refusé : {msg}"], 1)
    rla._out(f"Sauvegarde : {backup[0]} (SHA-256 {backup[1]})")

    pair_outcomes: list[tuple[str, str, str]] = []
    for v in run.eligible_verdicts:
        outcome, detail = apply_pair(v, audit_ids[v.spec.code], run, backup[0], backup[1])
        pair_outcomes.append((v.spec.code, outcome, detail))
        rla._out(f"Paire {v.spec.code} : {outcome}" + (f" ({detail})" if detail else ""))

    idem_outcomes = delete_idempotency_targets(run.idem_targets) if run.idem_targets else []

    real_world = read_world(run.spec)
    real = _compute_controls(real_world, run.spec, run.today, deadlines.now_utc())
    real_gaps = _evaluate_controls(run.spec, run.before, real, run.verdicts)
    drift = rla._differences(run.simulated, real)

    text = _render_report(
        run,
        mode="application",
        approved=approved,
        pair_outcomes=pair_outcomes,
        idem_outcomes=idem_outcomes,
        backup=backup,
        real=real,
        real_gaps=real_gaps,
        drift=drift,
        audit_ids=audit_ids,
    )
    ok_all = (
        all(o in (WRITTEN, LOST_BUT_WRITTEN) for _, o, _ in pair_outcomes)
        and not real_gaps
        and not drift
    )
    summary.append(f"Sauvegarde : {backup[0]}")
    summary.append("Contrôles après réel : " + ("conformes" if not real_gaps else f"{len(real_gaps)} écart(s)"))
    return rla._finish(report, text, summary, 0 if ok_all else 1)


def _run_restore(report: Path, source: Path, approved: Optional[str]) -> int:
    try:
        rplan = prepare_restore(source)
    except RunRefused as exc:
        return rla._finish(
            report,
            f"# Restauration\n\n- Date : {rla._now_line()}\n- Sauvegarde : `{source}`\n\n"
            f"**Refusé** : {exc}\n",
            [f"Refusé : {exc}"],
            1,
        )
    summary = [
        f"Restauration — {'à blanc' if approved is None else 'application'}",
        f"Projet : {rplan.project}",
        f"Sauvegarde : {rplan.path} (SHA-256 {rplan.sha})",
        f"Empreinte : {rplan.fingerprint}",
    ]
    if approved is None:
        text = _render_restore_report(rplan, mode="restauration à blanc")
        return rla._finish(report, text, summary, 1 if rplan.blocking else 0)
    refusal = []
    if approved != rplan.fingerprint:
        refusal.append(f"empreinte modifiée ({approved} → {rplan.fingerprint})")
    refusal.extend(rplan.blocking)
    if refusal:
        text = _render_restore_report(
            rplan, mode="restauration refusée", approved=approved, refusal=refusal
        )
        return rla._finish(report, text, summary + refusal, 1)
    pending = [p for p in rplan.pairs if p.state == RESTORE_PENDING]
    if not pending:
        text = _render_restore_report(rplan, mode="restauration (déjà restaurée)", approved=approved)
        return rla._finish(report, text, summary + ["Déjà restaurée."], 0)
    outcomes = []
    for p in pending:
        o, d = apply_restore_pair(p, rplan)
        outcomes.append((p.code, o, d))
    text = _render_restore_report(
        rplan, mode="restauration", approved=approved, outcomes=outcomes
    )
    ok = all(o in (WRITTEN, LOST_BUT_WRITTEN) for _, o, _ in outcomes)
    return rla._finish(report, text, summary, 0 if ok else 1)


def main(argv: Optional[list[str]] = None) -> int:
    parser = rla._Parser(
        prog="python -m scripts.purge_annulled_pairs",
        description="Purge hors modèle de quatre paires annulées (à blanc par défaut).",
    )
    parser.add_argument("plan", nargs="?", help="le plan JSON (hors du dépôt public)")
    parser.add_argument("--rapport", metavar="RAPPORT.md", help="le rapport à écrire")
    parser.add_argument(
        "--sauvegarde", metavar="DOSSIER", help="dossier de sauvegarde (hors du dépôt)"
    )
    parser.add_argument(
        "--appliquer", metavar="EMPREINTE", help="appliquer avec l'empreinte approuvée"
    )
    parser.add_argument(
        "--restaurer", metavar="SAUVEGARDE.json", help="restaurer depuis une sauvegarde"
    )
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
