"""Invoice Firestore CRUD, tax computation, and line-item management.

``create_invoice`` has two branches. The ordinary one — the GENERATED path —
allocates a year-sequential number from the transactional counter. The
IMPORT branch (keyword-only arguments, unreachable from ``request.form``)
keeps a number the previous system already issued, never touches the
counter, and refuses rather than write anything it cannot reconcile — a book
of account is complete or it is wrong.

Everything ``create_invoice`` decides before its transaction lives in ONE
function, :func:`plan_invoice` — the source selection, the refusals, the
totals, the issuance checks and the warnings — so that a preview of an
invoice (the connector's read tool, lot 3b) and the write can never drift:
the retired ``dry_run`` doubled every write into two model calls that the
handlers had to keep in step by hand, and that is exactly the drift this
shape removes.

A brouillon is corrected by :func:`update_invoice_draft` — notes, due date
and a fresh billing-address snapshot, never a money figure or a line item.

RETIRED KEY: ``payment_terms`` (« Conditions », retired by the lawyer's
decision of 2026-11-11 — no note d'honoraires placeholder ever printed it).
No creation stores it (``_CREATE_DATA_KEYS`` drops it), no default writes
it (``_default_doc``), and a draft correction naming it is refused by name
(``DRAFT_FIELDS``). An invoice written before keeps it: no migration and no
purge — the exception to « purge on save » this module declares, since no
write after creation replaces the document (each is a partial ``update()``
of the keys it changes, never the merged ``set()``) — and nothing reads it:
no page, export, note d'honoraires or connector output shows it, and no
writer copies it forward.

Invoice dates are UTC (the lawyer's decision of 2026-11-07). :func:`plan_invoice`
refuses a date only when it falls AFTER the current UTC calendar day
(``utils.deadlines.today_utc`` — the one UTC « today » Architecture Rule 5
names), so the latest date it accepts is the UTC day: already tomorrow on a
Montréal evening, when an invoice made at a month's or a year's end may carry
the next period's date (its number still follows the Montréal year, and
:func:`number_year_warning` says so). The connector's ``create_invoice`` and
``preview_invoice`` keep their own stricter Montréal-day check, by the same
decision (``mcp/handlers._invoice_date``). The default due date is the
invoice date plus one calendar month (:func:`default_due_date`).

LATENESS is a fact, derived (lot 2 of « Factures : retard, radiation,
intérêts », the lawyer's decisions of 2026-11-11). :func:`is_overdue` is its
one authority: an issued invoice (envoyée or en_retard) with a balance owing
(:func:`balance_of`) whose due date, prorogued on the CIVIL calendar
(:data:`LATENESS_REGIME` — a due date on a Saturday, a Sunday or a public
holiday is payable until the next business day), fell before today in
Montréal (``utils.deadlines.is_past_due``). The stored « en_retard » follows
it: the daily sweep at 09:00, Montréal time (``routes/taches_factures``,
behind ``Config.FACTURES_RETARD_ACTIF``) marks an overdue « envoyée » through
:func:`update_status_report`, and :func:`available_transitions` offers
« Marquer en retard » only while the invoice is overdue and « Retirer le
retard » only while it is not — the correction of a mark made before the
due date, never a waiver. Every caller names its « today »; the status
write judges with ``today_mtl`` inside its own transaction. A stored amount
the rule cannot read is named by its field, never judged and never quoted
(:func:`unreadable_balance_field`); the status write refuses by the same
name any amount :func:`available_transitions` cannot read, on every status
(:func:`unreadable_transition_field`).

WRITE-OFFS (« radiation » — lot 3a of « Factures : retard, radiation,
intérêts », the lawyer's decisions of 2026-11-11, D2 and D3, and the
defaults adopted with them). A write-off is a dated EVENT on the invoice,
never a register entry: no money moves, so ``amount_paid`` is never touched
(check 8 of ``scripts/verify_admin_integrity`` stays exact). The events live
in ``write_offs``, an APPEND-ONLY array — absent on every invoice until its
first write-off and read as ``[]`` there (no creation writes it, no
migration). Each event::

    {"id": <uuid4, minted here>,
     "kind": "radiation" | "contre_passation",       # WRITE_OFF_KINDS
     "date": <the Montréal day it was recorded, at midnight UTC — never
              backdated>,
     "amount": <cents > 0, taxes included>,
     "gst_amount": <cents ≥ 0>, "qst_amount": <cents ≥ 0>,
     "tax_claimed": <bool — both parts 0 when False>,
     "reason": <at most 500 characters, '' allowed — refused, never
                altered, when security.sanitize would change it>,
     "reverses_id": <'' on a radiation; on a contre_passation, the id of
                     the radiation it cancels, whose four figures it
                     repeats>,
     "credit_note_number": <a radiation only — « AAAA-NC### », its credit
                            note's number (lot 3b); absent on a
                            contre_passation>,
     "created_at": <instant>, "created_via": <provenance.VALID_VIA>}

CREDIT NOTES (lot 3b — the lawyer's decision 8 of 2026-11-12, which refines
D2): toward the client a write-off is a PRICE REDUCTION documented by a
credit note, and in the books a BAD DEBT (``models/financial_statements``:
an expense « Créances irrécouvrables », revenue staying as billed). Each
radiation takes its credit note's number from a yearly series
« AAAA-NC### » — the Montréal year of the event's date, a sequence of 3
digits rolling to 4 and more past 999 — read and incremented in the
door's own transaction from ``counters/notes-credit-{year}``, exactly as an
invoice's number comes from ``counters/invoices-{year}``: never reused,
never decremented. A cancelled radiation's credit note keeps its number for
ever, and is « annulée » — derived: its radiation no longer stands. A
contre_passation takes no number. (The credit note's Word document is lot
3d; the note d'honoraires' row names it.)

An event is never edited and never removed, so it carries no etag (Rule 7);
a radiation STANDS while no contre_passation names it. :func:`write_off_events`
is the one judge of a well-formed array: a malformed one RAISES
(:class:`WriteOffsUnreadable`) wherever it is read — never counted as 0 —
and :func:`unreadable_balance_field` names it (``write_offs``).
:func:`balance_of` subtracts what stands (:func:`written_off_of`). Two doors
write the events, each ONE transaction against the invoice's etag
(required): :func:`write_off_remainder` writes off the WHOLE remainder of an
overdue invoice (:func:`write_off_refusal`, judged on ``today_mtl``), its
TPS/TVQ parts in proportion of the invoice's ``total``
(:func:`write_off_tax_parts`), numbers its credit note, and settles it
« payée » — no new status: a
derived label says how (:func:`settlement_label`); :func:`reverse_write_off`
cancels one standing radiation by a « contre_passation » dated that day,
which reopens it « en_retard ». While a radiation stands, a payment is
refused in both registers (``facture_radiee``) and so are the void
(``radiation_inscrite``) and « Rouvrir »: « Annuler la radiation » first.
The one-shot repair scripts refuse an invoice that carries one
(:func:`repair_script_refusal`).
"""

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from models import aggregation_values, concurrency, db, full_reads, provenance
from models.record_ids import is_addressable_id
from pagination import PAGE_SIZE, decode_cursor, encode_cursor
from security import sanitize
from utils import deadlines
from utils.deadlines import today_mtl, today_utc
from utils.format_fr import format_cents_fr
from utils.logging_setup import (
    log_invoice_event,
    log_invoice_write_off,
    log_unexpected,
    sanitize_log_value,
)
from utils.recours import MONTHS, add_period

logger = logging.getLogger(__name__)


# Firestore collection paths
COLLECTION = "invoices"
LINE_ITEMS_SUB = "lineitems"
COUNTERS_COLLECTION = "counters"


class _SourceConflictError(Exception):
    """Raised when a billing source changed concurrently during invoicing."""


class _DuplicateNumberError(Exception):
    """Raised inside the creation transaction when an IMPORTED number is
    already borne by another invoice — aborts, never writes."""


class _OwnCommitLandedError(Exception):
    """Raised inside a RE-RUN of the creation transaction that finds THIS
    call's own invoice id already written — an earlier attempt landed.

    The id is minted fresh by :func:`create_invoice` and known to no other
    writer, so a source naming it (or an invoice bearing the imported number
    under it) can only be this very call's commit. The real ``transactional``
    decorator re-runs its body after an ``Aborted`` commit, and the commit
    RPC is itself retried on ``ServiceUnavailable`` under the same
    transaction id: a first commit that LANDED with its answer lost, whose
    retry is then answered ``Aborted``, re-runs the body over the landed
    writes. Read as a source conflict (or a duplicate number), that re-run
    said « nothing was created — no number consumed » over an invoice that
    exists under a consumed number (review of the fixups of lot 3). It is
    answered :data:`CREATE_OUTCOME_UNCERTAIN` instead."""


# :func:`create_invoice`'s answer when its transaction RAISED (fixups of lot
# 3). Every other error it returns is CERTAIN to precede any write — a
# refused plan, a failed first-of-year seed (reads only), a source or
# number conflict raised inside the transaction body, which aborts it (a
# RE-RUN of that body over an earlier attempt that landed is told apart by
# the call's own invoice id — :class:`_OwnCommitLandedError` — and answered
# with this constant too). A raise out of the transaction is not: a commit
# RPC that fails does not
# prove the commit did not land (the server can apply it and the answer be
# lost), so the invoice MAY exist, under a consumed number. It is therefore
# never worded « nothing was created », on either surface: the web form
# shows it as is, and the connector recognizes THIS constant to keep its
# idempotency claim and say the outcome is uncertain.
CREATE_OUTCOME_UNCERTAIN = (
    "L'enregistrement de la facture a échoué d'une façon qui ne permet pas "
    "de savoir si elle a été créée : vérifiez la liste des factures du "
    "dossier avant de réessayer."
)


class _StatusRefused(Exception):
    """Refusal raised inside the status transaction (aborts, never writes).

    ``reason`` is the machine-stable code the observability event carries.
    """

    def __init__(self, message: str, reason: str = "transition_refusee") -> None:
        super().__init__(message)
        self.reason = reason


class _VoidRefused(Exception):
    """Refusal raised inside the void transaction (aborts, never writes).

    ``reason`` is the machine-stable code the observability event carries;
    the French sentence is ``str(exc)``.
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class _DraftRefused(Exception):
    """Refusal raised inside the draft-edit transaction (aborts, never writes).

    ``reason`` is the machine-stable code the observability event carries.
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# The two registers that can carry a payment imputed on an invoice. String
# LITERALS on purpose — importing models.admin_ledger / models.trust here
# would close an import cycle (both import this module). The test suite pins
# them against each module's own TRANSACTIONS_COLLECTION.
_ADMIN_TRANSACTIONS = "admin_transactions"
_TRUST_TRANSACTIONS = "trust_transactions"

# The keys ``create_invoice`` accepts from its caller's ``data``. Everything
# else on the stored document is decided HERE — the number, the totals, the
# retained sources, the provenance — and the payment state above all: a
# caller-supplied ``status``/``amount_paid``/``paid_date`` used to reach
# storage verbatim, which only the two callers' own whitelists prevented
# (a « payée » invoice with a payment on no ledger is exactly what the
# single-writer doctrine of 2026-08-17 exists to make impossible). A key
# outside this set is dropped, never merged — the retired ``payment_terms``
# among them (module docstring).
_CREATE_DATA_KEYS = frozenset({
    "dossier_id", "dossier_file_number", "dossier_title",
    "client_id", "client_name", "billing_address",
    "date", "due_date", "notes",
    "retainer_applied", "gst_number", "qst_number",
    "legacy_ref",
    # Honoured by provenance.stamp_create only when it is a VALID via — the
    # connector's creators have always named themselves.
    "created_via",
})


# Valid statuses and their French labels
VALID_STATUSES = ("brouillon", "envoyée", "payée", "en_retard", "annulée")

#: Les statuts d'une facture ÉMISE — celle qui peut encore recevoir un
#: paiement, et dont le solde constitue une créance. Recopié plutôt
#: qu'importé de `models.admin_ledger` / `models.trust`, qui en tiennent
#: chacun le leur : doctrine du vocabulaire du dépôt.
_ISSUED_STATUSES = ("envoyée", "en_retard")

#: The statuses of a BILLED invoice — the financial statements' revenue
#: (lot B, the lawyer's decision of 2026-11-07: billed basis, in the period
#: of the invoice date, before tax). A brouillon is not billed yet; an
#: annulée invoice counts in no period, whenever it was voided.
REVENUE_STATUSES = ("envoyée", "en_retard", "payée")

STATUS_LABELS = {
    "brouillon": "Brouillon",
    "envoyée": "Envoyée",
    "payée": "Payée",
    "en_retard": "En retard",
    "annulée": "Annulée",
}

# Allowed status transitions.
#
# « payée » ne se POSE plus à la main (2026-08-17). La comptabilité étant
# l'unique écrivain d'un paiement, le seul chemin vers ce statut est la
# bascule automatique de record_payment — qui écrit le champ directement et
# n'emprunte PAS cette table. Un bouton « Marquer comme payée » rouvrait
# exactement la porte que le retrait du formulaire d'encaissement venait de
# fermer : un statut affirmant un paiement, sans montant, sans date, invisible
# au grand livre. Dix-neuf factures en production sont dans cet état.
#
# En retour, « payée » cesse d'être un cul-de-sac : la sortie existe pour
# corriger les statuts posés à la main AVANT cette doctrine. Elle est refermée
# dès qu'un paiement est INSCRIT — voir available_transitions, sans quoi
# « Rouvrir » puis « Annuler » libérerait les heures d'une facture réellement
# encaissée. (void_invoice regarde désormais l'argent lui-même — 2026-09-26 —
# mais la sortie refermée reste la première ligne : un bouton ne s'affiche
# pas pour être refusé.)
#
# « annulée » figure dans la table comme une ACTION offerte par la fiche, et
# cette action emprunte void_invoice — jamais update_status, qui la refuse
# d'entrée (le changement de statut nu laissait toutes les sources
# facturées pour toujours : void refusait ensuite « déjà annulée » et
# delete_invoice refusait les références restées accrochées).
#
# « en_retard » ↔ « envoyée » suit l'échéance depuis le 2026-11-11 (lot 2 du
# plan « Factures : retard, radiation, intérêts ») : le retard est un FAIT,
# dérivé par is_overdue, et le balayage quotidien de 9 h le pose de lui-même.
# La table garde les deux arêtes ; available_transitions n'offre « en_retard »
# que tant que la facture est en retard, et le retour à « envoyée » — « Retirer
# le retard » — que tant qu'elle ne l'est PAS : la correction d'une marque
# posée avant l'échéance, jamais une dispense (décision de l'avocat).
STATUS_TRANSITIONS = {
    "brouillon": ("envoyée", "annulée"),
    "envoyée": ("en_retard", "annulée"),
    "en_retard": ("envoyée", "annulée"),
    "payée": ("envoyée",),
}


def _standing_admin_encaissement(row: dict) -> bool:
    """An administration entry whose payment on its invoice still STANDS.

    The predicate of ``models.admin_ledger.receipt_stands`` (that of
    ``sum_invoice_receipts`` and of the note d'honoraires' payment rows) —
    ``encaissement_facture``, not annulée, not contre-passée — copied rather
    than imported (import cycle; the test suite pins the two against each
    other on the same rows).
    """
    return (
        row.get("kind") == "encaissement_facture"
        and row.get("status") != "annulée"
        and not row.get("reversed_by_id")
    )


def _standing_trust_fee_payment(row: dict) -> bool:
    """A trust « paiement d'honoraires » (``virement_honoraires``) that still
    stands: not annulée, not contre-passé. Its reversal is minted as a
    ``correction`` row carrying no invoice_id, and the original gains
    ``reversed_by_id`` — an annulée original is the uncleared case."""
    return (
        row.get("purpose") == "virement_honoraires"
        and row.get("status") != "annulée"
        and not row.get("reversed_by_id")
    )


#: The calendar an invoice's due date is prorogued on before lateness is
#: judged (the lawyer's decision of 2026-11-11): the CIVIL one of
#: ``utils.deadlines`` — Saturdays, Sundays and the public holidays of art.
#: 61(23) L.i., never the 26 December and 2 January that art. 82 C.p.c.
#: adds for civil procedure alone. A due date on a non-business day is
#: payable until the next business day; the invoice is late the day after.
LATENESS_REGIME = deadlines.CIVIL


def _montreal_day(today: object) -> date:
    """*today* when it is a calendar date — the caller's Montréal « today »
    (``utils.deadlines.today_mtl``), named on every call. Anything else is
    refused: ``None`` would let ``deadlines.is_past_due`` read the clock
    itself, behind the caller's back, and a ``datetime`` is an instant, never
    a day (it does not even compare with one)."""
    if not isinstance(today, date) or isinstance(today, datetime):
        raise TypeError(
            "today : un jour de calendrier de Montréal "
            "(utils.deadlines.today_mtl()), jamais None ni un instant"
        )
    return today


def is_overdue(invoice: dict, *, today: date) -> bool:
    """True when *invoice* is LATE on *today* — the one authority of
    lateness (module docstring). PURE; three conditions, all required:

    * its status is an issued one (``_ISSUED_STATUSES``: envoyée or
      en_retard) — a brouillon owes nothing yet, a payée or an annulée
      nothing any more;
    * a balance is owing (:func:`balance_of` > 0);
    * its due date, prorogued on :data:`LATENESS_REGIME`, fell strictly
      before *today* (``utils.deadlines.is_past_due``) — due on a Saturday,
      payable until Monday, late on Tuesday. No due date, never late.

    *today* is the caller's Montréal day (``utils.deadlines.today_mtl``),
    required: the rule never reads a clock of its own. A stored amount
    :func:`balance_of` cannot read raises, as everywhere it is read."""
    _montreal_day(today)
    if invoice.get("status", "") not in _ISSUED_STATUSES:
        return False
    if balance_of(invoice) <= 0:
        return False
    return deadlines.is_past_due(
        invoice.get("due_date"), today=today, regime=LATENESS_REGIME)


def first_overdue_day(invoice: dict) -> Optional[date]:
    """The first Montréal day *invoice* counts as late — the day after its
    due date prorogued on :data:`LATENESS_REGIME` — or ``None`` when it has
    no readable due date. PURE, and blind to the status and the balance: it
    dates lateness, :func:`is_overdue` decides it. The day the refusals of
    :func:`update_status_report` name."""
    due = deadlines.effective_due(invoice.get("due_date"), regime=LATENESS_REGIME)
    return None if due is None else due + timedelta(days=1)


def days_overdue(invoice: dict, *, today: date) -> int:
    """How many days *invoice* has been late on *today*: ``0`` unless
    :func:`is_overdue`, else 1 on its :func:`first_overdue_day` and one more
    each day after — counted from the PROROGUED due date, never the stored
    one. PURE."""
    if not is_overdue(invoice, today=today):
        return 0
    return (today - first_overdue_day(invoice)).days + 1


def _payment_recorded(invoice: dict) -> bool:
    """Whether a payment is RECORDED on *invoice* (``amount_paid`` > 0) —
    how :func:`available_transitions` and, since 2026-11-12,
    :func:`void_invoice_report` read it to that end, on every status:
    an absent, None, empty or zero amount is none (what a « payée » set by
    hand before 2026-08-17 may carry). Not its only reading: an issued
    invoice's lateness reads ``amount_paid`` through :func:`balance_of`,
    strictly, where a None or empty one is unreadable (third review of
    lot 2). Raises on a stored amount ``int()`` cannot read, which
    :func:`unreadable_transition_field` names beforehand."""
    return int(invoice.get("amount_paid", 0) or 0) > 0


def available_transitions(
    invoice: dict,
    *,
    today: date,
    receipts: Optional[list[dict]] = None,
    trust_payments: Optional[list[dict]] = None,
) -> tuple[str, ...]:
    """Les transitions que CETTE facture peut réellement prendre.

    ``STATUS_TRANSITIONS`` est indexée sur le seul statut : elle ne sait pas
    distinguer un « payée » posé à la main d'un « payée » qu'un encaissement a
    produit. C'est pourtant cette différence qui décide du chemin de
    correction — le premier se rouvre ici, le second se corrige par
    contre-passation, qui réduit le paiement ET rouvre la facture d'elle-même
    (``models/admin_ledger.reverse_transaction``, dans la transaction de la
    contre-passation depuis le lot 5a).

    Sans ce filtre, la réouverture serait un trou : « Rouvrir » puis
    « Annuler » libérerait les heures et dépenses d'une facture réellement
    encaissée. ``void_invoice`` le refuse désormais lui-même, mais une
    autorité qui offrirait l'action pour la voir refusée serait un défaut de
    conception.

    Pour la même raison, « annulée » n'est plus offerte dès qu'un paiement
    est INSCRIT (``amount_paid > 0``, quel que soit le statut) :
    ``void_invoice`` la refuserait. *receipts* — les écritures
    d'administration imputées sur la facture, que la fiche lit déjà
    (``list_invoice_receipts``) — ferme aussi le cas de dérive où une
    écriture tient encore alors que ``amount_paid`` vaut 0.
    *trust_payments* — les écritures du fidéicommis qui la visent
    (``models.trust.list_invoice_fee_payments``) — ferme le dernier : un
    paiement d'honoraires debout dont la recette d'administration
    automatique a échoué (elle est fail-open), donc sans montant inscrit ni
    écriture d'administration. Une fonction pure ne voit que ce qu'on lui
    donne ; si une lecture d'affichage a échoué, ``void_invoice_report``
    relit tout dans sa transaction et refuse, et la route affiche le motif
    en bandeau.

    UNE seule autorité, consommée par ``update_status`` ET par la fiche : un
    bouton qui s'affiche pour être refusé est un défaut de conception.

    Le retard suit l'échéance (lot 2, 2026-11-11) : « en_retard » n'est
    offert à une facture envoyée que tant que :func:`is_overdue` la dit en
    retard ; le retour à « envoyée » — « Retirer le retard » — ne l'est à une
    facture en retard que tant qu'elle ne l'est PAS (une marque posée avant
    l'échéance). *today*, le jour de Montréal de l'appelant, est REQUIS : une
    fonction pure ne lit aucune horloge.

    Une radiation qui TIENT (lot 3a, :func:`standing_write_offs`) ferme les
    mêmes portes qu'un paiement inscrit : « Rouvrir » une facture « payée »
    qu'elle solde, et « Annuler » — :func:`void_invoice_report` la refuse
    (``radiation_inscrite``). La voie est « Annuler la radiation »
    (:func:`reverse_write_off`), qui rouvre la facture d'elle-même. Elle se
    lit sur une facture émise ou payée seulement (:data:`REVENUE_STATUSES`) :
    un brouillon n'est jamais en retard, une facture annulée n'a plus de
    transition.
    """
    _montreal_day(today)
    current = invoice.get("status", "")
    allowed = STATUS_TRANSITIONS.get(current, ())
    if current in _ISSUED_STATUSES:
        overdue = is_overdue(invoice, today=today)
        if current == "envoyée" and not overdue:
            allowed = tuple(s for s in allowed if s != "en_retard")
        if current == "en_retard" and overdue:
            allowed = tuple(s for s in allowed if s != "envoyée")
    paid = _payment_recorded(invoice)
    radiated = current in REVENUE_STATUSES and bool(standing_write_offs(invoice))
    if current == "payée" and (paid or radiated):
        allowed = tuple(s for s in allowed if s != "envoyée")
    if (
        paid
        or radiated
        or any(_standing_admin_encaissement(r) for r in (receipts or ()))
        or any(_standing_trust_fee_payment(r) for r in (trust_payments or ()))
    ):
        allowed = tuple(s for s in allowed if s != "annulée")
    return allowed

# Tax rates stored as basis points (×100 for GST, ×1000 for QST precision)
GST_RATE_BPS = 500       # 5.00%
QST_RATE_BPS = 9975      # 9.975%


def default_due_date(invoice_date: datetime) -> datetime:
    """The due date an invoice gets when none is given: its date plus one
    CALENDAR month, the day clamped to that month's last (31 January → 28
    or 29 February) — ``utils.recours.add_period``, the house's one month
    arithmetic (the lawyer's decision of 2026-11-07; it was 30 days)."""
    return add_period(invoice_date, 1, MONTHS)


# ── Imported (historical) invoice numbers ────────────────────────────────
# A number carried over from the practice's previous system. Free-form on
# purpose — it is ANOTHER system's numbering and never ours to reshape (an
# accounting artifact already sent to a client is never renumbered) — but
# BOUNDED, because it prints on the note d'honoraires, exports to CSV and
# folds into a generated document's display name.
IMPORTED_NUMBER_MAX_LENGTH = 32
_IMPORTED_NUMBER_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    " ./-"
)

# The adjustment line's own ceiling. It prints verbatim on the client's
# invoice, so an over-long value is REFUSED rather than sanitize()d down —
# see _adjustment_line_item.
ADJUSTMENT_DESCRIPTION_MAX_LENGTH = 500


def _default_doc() -> dict:
    """Return a dict with every invoice field set to its default value."""
    return {
        "id": "",
        "invoice_number": "",
        "dossier_id": "",
        "dossier_file_number": "",
        "dossier_title": "",
        "client_id": "",
        "client_name": "",
        "billing_address": {
            "name": "",
            "street": "",
            "unit": "",
            "city": "",
            "province": "QC",
            "postal_code": "",
        },
        "date": None,
        "due_date": None,
        "status": "brouillon",
        # Financials (all in cents)
        "subtotal_fees": 0,
        "subtotal_expenses": 0,
        "subtotal": 0,
        "gst_rate": GST_RATE_BPS,
        "gst_amount": 0,
        "qst_rate": QST_RATE_BPS,
        "qst_amount": 0,
        "total": 0,
        "retainer_applied": 0,
        "amount_due": 0,
        # Payment received (lot P, July 2026). Until this existed, payment
        # was representable ONLY as status == "payée": no amount, no date,
        # and a partial payment could not be expressed at all. `amount_due`
        # is frozen at issuance and stays non-zero on a paid invoice, so it
        # is NOT a balance — the live balance is amount_due − amount_paid,
        # DERIVED (see `balance_of`), never stored.
        "amount_paid": 0,          # cents, 0 ≤ amount_paid ≤ total
        "paid_date": None,         # date-only at midnight UTC; None = unpaid
        # Tax numbers (from config, snapshotted at creation)
        "gst_number": "",
        "qst_number": "",
        "notes": "",
        # Identifiant de l'enregistrement dans le système d'origine,
        # posé par la reprise historique (août 2026) ; « » partout
        # ailleurs. C'est l'ancre anti-doublon DURABLE : la clé
        # d'idempotence MCP expire en 24 h et une reprise s'étale sur
        # des jours, donc seul un identifiant porté par la donnée
        # elle-même permet à une reprise interrompue de retrouver ce
        # qu'elle a déjà écrit. Jamais sérialisé en vCard ni en iCal.
        "legacy_ref": "",
        # La facture a-t-elle été REPRISE d'un ancien système (sous son
        # numéro d'origine, jamais tiré du compteur) ? Posé par
        # create_invoice LUI-MÊME, de `invoice_number` — jamais lu des
        # données d'un appelant (hors _CREATE_DATA_KEYS) — depuis les
        # correctifs du lot 3 : IMP-07 (« facture importée encore au
        # brouillon ») ne peut plus confondre une reprise avec une facture
        # que create_invoice vient d'émettre. ABSENT sur les factures
        # antérieures (aucune migration) : mcp.import_audit.is_imported
        # les juge alors sur `legacy_ref` et `created_via`.
        "imported": False,
        "created_at": None,
        "updated_at": None,
        "etag": "",
    }


def _default_line_item() -> dict:
    """Return a dict with every line item field set to its default value."""
    return {
        "id": "",
        "type": "fee",
        "source_id": "",
        "date": None,
        "description": "",
        "hours": None,
        "rate": None,
        "amount": 0,
        "taxable": True,
    }


def _sanitize_data(data: dict) -> dict:
    """Sanitize all string values in *data*."""
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            out[key] = sanitize(val, max_length=2000)
        elif isinstance(val, dict):
            out[key] = _sanitize_data(val)
        else:
            out[key] = val
    return out


def _validate(data: dict) -> list[str]:
    """Return a list of validation error messages (empty = valid)."""
    errors: list[str] = []

    if not data.get("dossier_id", "").strip():
        errors.append("Un dossier doit être associé à cette facture.")

    if not data.get("date"):
        errors.append("La date de la facture est requise.")

    return errors


# ── Tax computation ──────────────────────────────────────────────────────


def compute_totals(line_items: list[dict]) -> dict:
    """Compute subtotals, taxes, and total from a list of line items.

    All monetary values are integer cents. Uses Python Decimal for tax
    computation to avoid floating-point issues, as required by CLAUDE.md.
    """
    subtotal_fees = 0
    subtotal_expenses = 0
    taxable_amount = 0

    for item in line_items:
        amt = item.get("amount", 0)
        if item.get("type") == "fee":
            subtotal_fees += amt
        else:
            subtotal_expenses += amt
        if item.get("taxable", True):
            taxable_amount += amt

    subtotal = subtotal_fees + subtotal_expenses

    # Tax computation with Decimal (QST is NOT compounded on GST since 2013)
    taxable_dec = Decimal(taxable_amount)
    gst_amount = int(
        (taxable_dec * Decimal("0.05")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    qst_amount = int(
        (taxable_dec * Decimal("0.09975")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )

    total = subtotal + gst_amount + qst_amount

    return {
        "subtotal_fees": subtotal_fees,
        "subtotal_expenses": subtotal_expenses,
        "subtotal": subtotal,
        "gst_amount": gst_amount,
        "qst_amount": qst_amount,
        "total": total,
    }


# ── Invoice number generation ────────────────────────────────────────────


def _scan_max_invoice_seq(prefix: str) -> int:
    """Return the highest existing invoice sequence for *prefix* via a scan.

    Used only to seed the transactional counter the first time it is created
    for a given year, so pre-counter numbering continues without duplicates.
    """
    max_seq = 0
    for doc in db.collection(COLLECTION).stream():
        num = doc.to_dict().get("invoice_number", "")
        if num.startswith(prefix):
            try:
                max_seq = max(max_seq, int(num[len(prefix):]))
            except (ValueError, IndexError):
                continue
    return max_seq


# « YYYY-FNNN » — the canonical scheme again since the 2026-08-12 user
# decision (the per-file « {file_number}-NN » scheme of 2026-07-17 lasted
# four weeks — the six invoices it minted keep their numbers for ever, an
# accounting artifact sent to a client is never renumbered). The year is the
# MONTRÉAL calendar year (``today_mtl`` — the house clock seam; the UTC year
# would stamp a Dec 31 evening invoice with the next millésime). Sequence:
# 3-digit zero-padded, rolling to 4+ past 999, from the monotonic
# transactional counter ``counters/invoices-{year}`` — never reused, never
# decremented; a deleted invoice leaves a hole. On the first use of a year
# the counter is seeded by ``_scan_max_invoice_seq`` so pre-counter numbering
# continues without duplicates (per-file numbers never match the « YYYY-F »
# prefix and are ignored). A number is never guessed, which could mint a
# colliding one: any failure aborts the creation.
#
# The allocation happens INSIDE ``create_invoice``'s own transaction
# (2026-09-26). It used to run in a transaction of its own, committed BEFORE
# the invoice's: a source conflict, a duplicate import number or any other
# abort of the invoice transaction then left the number consumed with no
# invoice under it — a hole in the fee-journal sequence the lawyer could not
# explain. Now the counter write commits with the invoice or not at all.
# There is deliberately no standalone allocator any more: one that commits on
# its own IS the defect.
#
# An IMPORTED historical number never reaches the counter (user decision
# 2026-08-16): ``create_invoice`` resolves it through
# ``_clean_imported_number``, and on that path the counter is neither read
# nor written.


def _invoice_counter_ref(year: str):
    """The transactional counter of *year*'s « YYYY-F » sequence."""
    return db.collection(COUNTERS_COLLECTION).document(f"invoices-{year}")


def _counter_seed(counter_ref, prefix: str) -> int:
    """The seed of a year's FIRST allocation: the highest number already
    issued under *prefix*, or 0 when the counter already exists.

    Read OUTSIDE the invoice transaction on purpose — a full-collection
    stream does not belong inside one — and only when the counter document
    is absent. A concurrent first-of-year creation computes the same seed;
    both transactions then read the absent counter and contend, and the
    loser's retry re-reads the counter the winner wrote. Raises on any read
    failure: seeding from a guess could reissue a number.
    """
    if counter_ref.get().exists:
        return 0
    return _scan_max_invoice_seq(prefix)


def _next_invoice_number(counter_snapshot, seed: int, prefix: str) -> tuple[str, int]:
    """``(number, next_seq)`` from the counter as read INSIDE the transaction."""
    current = (
        int((counter_snapshot.to_dict() or {}).get("seq", 0))
        if counter_snapshot.exists else 0
    )
    next_seq = max(current, seed) + 1
    return f"{prefix}{next_seq:03d}", next_seq


# ── Billing address snapshot ─────────────────────────────────────────────


def billing_address_from(partie: dict) -> dict:
    """Snapshot the billing address from a partie record.

    Lifted VERBATIM from ``routes/invoices._build_billing_address`` so the web
    form and the MCP connector cannot drift on which block an invoice
    snapshots — the same client billed at two different addresses depending
    on which surface issued the invoice is exactly the failure this prevents.

    The BLOCK: a natural person's is the one
    ``utils.template_fields.uses_work_block`` chooses — personal first,
    unless a professional role — the lawyer's decision of 2026-10-30 (an
    employee client's invoice went to the employer's address). An
    organization keeps the older rule: work address first, personal
    fallback. Issued invoices keep their frozen snapshot; a brouillon moves
    only on « rafraîchir l'adresse ». Province defaults to « QC ».
    Note the shape: {name, street, unit, city, province, postal_code}.
    ``utils/invoice_docx._partie_from_billing_address`` maps
    ``billing["name"]`` into the recipient of the Word note d'honoraires,
    and ``selected_address`` returns no ``name`` key at all — so the block
    is chosen there, and the snapshot built here.
    """
    from models.partie import display_name
    from utils.template_fields import uses_work_block

    name = display_name(partie)
    if partie.get("type") != "organization":
        work = uses_work_block(partie)
    else:
        work = bool(partie.get("work_address_street"))
    if work:
        return {
            "name": name,
            "street": partie.get("work_address_street", ""),
            "unit": partie.get("work_address_unit", ""),
            "city": partie.get("work_address_city", ""),
            "province": partie.get("work_address_province", "QC"),
            "postal_code": partie.get("work_address_postal_code", ""),
        }
    return {
        "name": name,
        "street": partie.get("address_street", ""),
        "unit": partie.get("address_unit", ""),
        "city": partie.get("address_city", ""),
        "province": partie.get("address_province", "QC"),
        "postal_code": partie.get("address_postal_code", ""),
    }


# ── Historical import: number, adjustment line ───────────────────────────


def _is_live_sequence_number(number: str) -> bool:
    """True when *number* falls in the namespace our own counter owns.

    Deliberately the WHOLE « {current Montréal year}-F » prefix, matched the
    way ``_scan_max_invoice_seq`` matches it — NOT « prefix + digits ».
    ``int()`` strips whitespace, so « 2026-F 12 » fails ``str.isdigit`` yet
    still parses to 12 in the seeding scan; anything sharing the prefix is
    ours.

    PAST years are not in the namespace: their counter can never be created
    again, ``today_mtl`` only moving forward. FUTURE years are not either,
    for a different reason — their counter does not exist yet, so its first
    use SEEDS from ``_scan_max_invoice_seq``, which sees the imported number
    and continues above it. The CURRENT year is the one hole: its counter
    already exists, seeding will never run for it again, and a planted
    number would be handed out a second time.
    """
    return number.startswith(f"{today_mtl().strftime('%Y')}-F")


def invoice_number_exists(number: str) -> bool:
    """True when an invoice already bears *number*.

    RAISES on query failure. The caller is a dry run asking « would the real
    call be refused? », and a swallowed error answering « no » is exactly the
    lie the dry-run contract exists to prevent. The authoritative check stays
    inside ``create_invoice``'s transaction; this one exists so a preview can
    refuse what the write would refuse.
    """
    wanted = (number or "").strip()
    if not wanted:
        return False
    docs = list(
        db.collection(COLLECTION)
        .where(filter=FieldFilter("invoice_number", "==", wanted))
        .limit(1)
        .stream()
    )
    return bool(docs)


def _clean_imported_number(raw: object) -> tuple[str, list[str]]:
    """Validate a historical invoice number. Returns ("", [erreurs]) on refusal.

    ``sanitize`` is deliberately NOT used here: its whitelist is wider and it
    TRUNCATES. Silently shortening an accounting identifier is exactly the
    corruption this refuses to commit — over-long is an error, never a
    haircut.
    """
    if not isinstance(raw, str):
        return "", ["Le numéro de facture importé doit être une chaîne."]
    number = raw.strip()
    if not number:
        return "", ["Le numéro de facture importé est requis."]
    if len(number) > IMPORTED_NUMBER_MAX_LENGTH:
        return "", [
            "Le numéro de facture importé dépasse "
            f"{IMPORTED_NUMBER_MAX_LENGTH} caractères. Il n'est jamais "
            "tronqué : corrigez-le."
        ]
    bad = sorted({c for c in number if c not in _IMPORTED_NUMBER_CHARS})
    if bad:
        return "", [
            "Le numéro de facture importé contient des caractères non "
            f"admis : {' '.join(bad)}. Lettres, chiffres, espace, point, "
            "barre oblique et trait d'union seulement."
        ]
    if _is_live_sequence_number(number):
        year = today_mtl().strftime("%Y")
        return "", [
            f"« {number} » appartient à la numérotation courante de Pallas "
            f"Athéna ({year}-F…) : un numéro importé ne peut pas emprunter "
            "le millésime en cours, sinon le compteur le réattribuerait plus "
            "tard."
        ]
    return number, []


def _adjustment_line_item(adjustment: dict) -> tuple[Optional[dict], list[str]]:
    """Materialize a named adjustment as ONE extra line item.

    The escape hatch for a legacy invoice whose printed total cannot be
    reconstructed from its lines — a courtesy write-down, a rounding, a
    credit. The gap is written ON the invoice rather than hidden by a
    tolerance or refused outright (user decision 2026-08-16).

    Typed ``fee`` because a courtesy write-down reduces HONORAIRES: that puts
    it in the « Honoraires » column of the Barreau fee journal and leaves
    ``expense_split`` untouched, which only carves up disbursements. It is
    the ONE line item in the system carrying no ``source_id`` — which is why
    the caller must NAME it: an unexplained amount on a client's invoice is
    worse than a refusal.

    ``taxable`` decides whether the adjustment moves the tax base. True (the
    default) reproduces an invoice whose GST/QST were computed on the
    reduced amount; False reproduces one discounted after tax.
    """
    if not isinstance(adjustment, dict):
        return None, ["L'ajustement doit être un objet."]

    amount = adjustment.get("amount_cents")
    if not isinstance(amount, int) or isinstance(amount, bool):
        return None, ["`adjustment.amount_cents` doit être un entier de cents."]
    if amount == 0:
        return None, [
            "Un ajustement de 0 $ n'explique rien : retirez-le, ou donnez le "
            "montant réel de l'écart."
        ]

    raw_description = adjustment.get("description")
    if not isinstance(raw_description, str) or not raw_description.strip():
        return None, [
            "`adjustment.description` est requise : l'écart doit être nommé "
            "sur la facture (« Remise de courtoisie », « Arrondi »…)."
        ]
    description = raw_description.strip()
    if len(description) > ADJUSTMENT_DESCRIPTION_MAX_LENGTH:
        return None, [
            "`adjustment.description` dépasse "
            f"{ADJUSTMENT_DESCRIPTION_MAX_LENGTH} caractères."
        ]
    # Length is already refused above, so this can only strip markup — it can
    # never truncate a description the caller believed was stored whole.
    description = sanitize(description, max_length=ADJUSTMENT_DESCRIPTION_MAX_LENGTH)
    if not description:
        return None, ["`adjustment.description` est vide après nettoyage."]

    taxable = adjustment.get("taxable", True)
    if not isinstance(taxable, bool):
        return None, ["`adjustment.taxable` doit être un booléen."]

    return {
        **_default_line_item(),
        "id": str(uuid.uuid4()),
        "type": "fee",
        "source_id": "",
        "date": None,
        "description": description,
        "hours": None,
        "rate": None,
        "amount": amount,
        "taxable": taxable,
    }, []


# ── The plan of an invoice: every decision taken before the transaction ─


@dataclass
class InvoicePlan:
    """What an invoice built from these sources would be — computed, never
    written.

    Returned by :func:`plan_invoice` WHETHER OR NOT the invoice may be
    issued: a refused plan still carries what it could compute (the skip
    reason of every source, the lines retained so far) so that a preview can
    show WHY, from the same computation the write runs.

    * ``skipped`` — ``{source id: French reason}``, one entry per source
      that cannot become a line (missing, already billed and on which
      invoice, another dossier's, non-billable on the generated path);
    * ``errors`` — the refusals, in French; empty means ``create_invoice``
      proceeds to its number and its transaction. ``refusal_reason`` is the
      machine-stable code of the first one, for the observability event;
    * ``warnings`` — true facts the caller should SHOW, never refusals (the
      number's year differs from the invoice date's; a provision supplied on
      the generated path was ignored);
    * ``due_date`` — the due date the invoice will carry: the caller's, else
      :func:`default_due_date` of its date. Set once the date is accepted,
      so ``create_invoice`` stores what this plan decided.
    """

    line_items: list[dict] = field(default_factory=list)
    valid_entry_ids: list[str] = field(default_factory=list)
    valid_expense_ids: list[str] = field(default_factory=list)
    # etag captured at pre-read time; the transaction re-checks it so a
    # concurrent content edit (hours/amount) aborts instead of snapshotting
    # a stale value into the line items.
    expected_etags: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    totals: dict = field(default_factory=dict)
    retainer_applied: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    refusal_reason: str = ""
    due_date: Optional[datetime] = None

    def refuse(self, errors: list[str], reason: str) -> "InvoicePlan":
        self.errors = list(errors)
        self.refusal_reason = reason
        return self


#: Where the firm's GST/QST registration numbers are entered — named by
#: every refusal that needs them, so the lawyer knows the one fix.
SETTINGS_TAX_NUMBERS_PATH = "« Paramètres → Profil du cabinet »"


def number_year_warning(invoice_date: object, number: str) -> str:
    """'' unless *number* is one of OUR sequence numbers (« YYYY-F… ») whose
    year is not the year of *invoice_date*.

    The number follows the Montréal year of the day the invoice is ISSUED
    (``today_mtl``), never its date — the documented doctrine, and the only
    one that keeps a year's fee-journal sequence contiguous. So an invoice
    dated 31 December and issued on 2 January carries the NEW year's number.
    That is correct, and surprising: this sentence says it before the
    invoice is sent. A number from another system (an import) is not ours to
    comment on — but this function judges the SHAPE only, and an imported
    number may well read « 2019-F014 », so only the generated path may ask
    it (``plan_invoice`` and the web form do; never hand it an import).

    String operations only, no pattern — pure, and linear by construction.
    """
    number = (number or "").strip()
    if not (len(number) >= 6 and number[:4].isdigit() and number[4:6] == "-F"):
        return ""
    when = invoice_date.date() if isinstance(invoice_date, datetime) else invoice_date
    if not isinstance(when, date) or str(when.year) == number[:4]:
        return ""
    return (
        f"La facture est datée de {when.year}, mais son numéro suit l'année "
        f"de son émission ({number[:4]}-F…) : un numéro de facture suit "
        "toujours l'année où la facture est créée, jamais sa date. Vérifiez "
        "la date avant d'envoyer la facture."
    )


def _calendar_day(value: object) -> Optional[date]:
    """The UTC calendar date of a date-only field (stored at midnight UTC)."""
    if isinstance(value, datetime):
        return (value.astimezone(timezone.utc) if value.tzinfo else value).date()
    if isinstance(value, date):
        return value
    return None


def issuance_refusals(
    merged: dict,
    totals: dict,
    *,
    generated: bool,
    client: Optional[dict],
) -> list[tuple[str, str]]:
    """What forbids ISSUING this invoice to its client — pure.

    Returns ``[(French refusal, machine reason), …]``; empty means none.
    *client* is the partie ``merged["client_id"]`` names, as READ by the
    caller (``None`` when the id names nothing on file, or is blank).

    On BOTH paths:

    * a ``client_id`` that names no contact on file. The billing address is
      FROZEN on the invoice, so a snapshot of a client that does not resolve
      is a blank address on a document the client will hold. (An imported
      invoice for a dossier that genuinely has NO client stays possible: the
      previous system issued it, and it is reproduced, not reissued.)

    On the GENERATED path only — an invoice this application issues today:

    * no client at all: an invoice is issued TO a client;
    * a blank client name, or a billing address the caller could not
      snapshot (its name is empty — ``billing_address_from`` always fills it
      from a contact on file, so an empty one means the caller's own read of
      the contact failed);
    * taxes charged under an EMPTY registration number. The numbers are
      snapshotted from the firm profile and never rewritten (there is no
      update of a money figure on an invoice), and 50+ invoices were
      issued with blank numbers before the profile could hold them. Checked per tax, and only when that tax is actually charged: a
      wholly non-taxable invoice needs neither;
    * a due date before the invoice date.

    The import path reproduces historical paper verbatim and is spared
    those four: its numbers, due dates and client are what was sent.
    """
    out: list[tuple[str, str]] = []
    client_id = str(merged.get("client_id") or "").strip()
    if client_id and client is None:
        out.append((
            "Le client du dossier est introuvable : l'adresse de facturation, "
            "figée sur la facture, serait vide. Corrigez les parties du "
            "dossier, puis recréez la facture. Rien n'a été créé.",
            "client_introuvable",
        ))
    if not generated:
        return out
    if not client_id:
        out.append((
            "Le dossier n'a aucun client : une facture ne s'émet qu'au client "
            "d'un dossier. Ajoutez le client au dossier, puis recréez la "
            "facture. Rien n'a été créé.",
            "aucun_client",
        ))
    elif client is not None:
        if not str(merged.get("client_name") or "").strip():
            out.append((
                "Le nom du client du dossier est vide : il s'imprime sur la "
                "facture et au journal des honoraires. Corrigez la fiche du "
                "dossier, puis recréez la facture. Rien n'a été créé.",
                "nom_client_vide",
            ))
        billing = merged.get("billing_address") or {}
        if not str(billing.get("name") or "").strip():
            out.append((
                "L'adresse de facturation du client n'a pas pu être établie "
                "(lecture de sa fiche impossible) : rien n'a été créé. "
                "Réessayez dans un instant.",
                "adresse_non_etablie",
            ))
    for amount_key, number_key, label, reason in (
        ("gst_amount", "gst_number", "TPS", "numero_tps_vide"),
        ("qst_amount", "qst_number", "TVQ", "numero_tvq_vide"),
    ):
        if (int(totals.get(amount_key) or 0) > 0
                and not str(merged.get(number_key) or "").strip()):
            out.append((
                f"Le numéro d'inscription {label} du cabinet est vide : une "
                f"facture qui porte la {label} doit l'indiquer. Saisissez-le "
                f"dans {SETTINGS_TAX_NUMBERS_PATH}, puis recréez la facture. "
                "Rien n'a été créé.",
                reason,
            ))
    invoice_day = _calendar_day(merged.get("date"))
    due_day = _calendar_day(merged.get("due_date"))
    if invoice_day and due_day and due_day < invoice_day:
        out.append((
            f"La date d'échéance ({due_day.isoformat()}) précède la date de la "
            f"facture ({invoice_day.isoformat()}). Rien n'a été créé.",
            "echeance_anterieure",
        ))
    return out


def invoice_document_from(data: Optional[dict]) -> dict:
    """The invoice document a creation starts from — the ONE builder.

    Defaults, then from *data* only the keys of ``_CREATE_DATA_KEYS``,
    sanitized. :func:`plan_invoice` judges THIS document, so every caller
    that asks it a question — ``create_invoice``, and the connector's
    preview — must build its document here: a preview assembling its own
    (unfiltered, unsanitized) would judge another invoice than the one the
    write stores, the very drift ``plan_invoice`` exists to prevent. (A
    client name made only of markup, say, is emptied by ``sanitize`` and
    refused as blank — by both, or by neither.) Pure: reads nothing.
    """
    return {
        **_default_doc(),
        **_sanitize_data(
            {k: v for k, v in (data or {}).items() if k in _CREATE_DATA_KEYS}
        ),
    }


def _read_client_strict(client_id: str) -> Optional[dict]:
    """The contact *client_id* names, or ``None`` when none is on file.

    RAISES on a read failure — never the fail-open ``get_partie``, whose
    ``None`` on an outage would read as « this client does not exist » and
    refuse, or worse, as a contact whose address is blank. Through this
    module's own client, so the store an invoice is written to is the store
    its client is checked in.
    """
    from models import partie as partie_model

    snap = db.collection(partie_model.COLLECTION).document(client_id).get()
    return (snap.to_dict() or {}) if snap.exists else None


def plan_invoice(
    dossier_id: str,
    entry_ids: list[str],
    expense_ids: list[str],
    merged: dict,
    *,
    generated: bool,
    require_all_sources: bool,
    adjustment: Optional[dict] = None,
    expected_total: Optional[int] = None,
) -> InvoicePlan:
    """Everything ``create_invoice`` decides before its transaction — ONE
    implementation, so a preview and the write cannot drift.

    *merged* is the invoice document as :func:`invoice_document_from` builds
    it from the caller's data — never one a caller assembles itself, or the
    preview and the write would judge two different documents.
    *generated* — the invoice gets a number of our own sequence
    (``create_invoice`` without ``invoice_number``); ``False`` is the
    historical import. Reads the sources and the client; writes nothing,
    allocates nothing.

    In order, the first refusal wins (its error list is returned as a
    whole), exactly as ``create_invoice`` always refused:

    1. the invoice fields (``_validate``) and an empty selection; a date
       that is not a date (``validation``); then an invoice date AFTER the
       current UTC day (``date_future``, on both paths — see the module
       docstring). The date accepted, the plan settles the due date
       (``due_date``);
    2. each source is read; one that cannot become a line is SKIPPED with
       its reason — missing or unreadable, already billed (the reason names
       the invoice, so a stale selection points at what billed it),
       another dossier's, and, on the generated path, NON-BILLABLE: time the
       lawyer marked « non facturable » has no amount and must never print
       its hours on a client's invoice (a legacy entry that predates the
       flag carries no key and stays billable). A historical import may
       reproduce such a 0 $ line and keeps it;
    3. with *require_all_sources*, a duplicated id or ANY skipped source
       refuses, naming each offender by reason — without it the skip is
       silent (no caller of the application passes False any more);
    4. no line left; a malformed *adjustment*;
    5. *expected_total* — exact, never a tolerance;
    6. the provision: on the GENERATED path it is forced to 0 — a provision
       is applied later, by a « paiement d'honoraires » drawn on the trust
       account, and deducting it here too would count the same money twice
       (the provision trap of 2026-08-17); a non-zero value supplied there
       is ignored and said so in ``warnings``. On the import path it is kept
       and bounded to ``[0, total]``;
    7. the issuance checks (:func:`issuance_refusals`), the client being
       read STRICTLY — a read that fails refuses, it never passes.

    Warnings: the number's year versus the invoice date's
    (:func:`number_year_warning`, on the generated path — the number will
    be of the current Montréal year).
    """
    from models.time_entry import get_time_entry
    from models.expense import get_expense

    plan = InvoicePlan()
    entry_ids = list(entry_ids or [])
    expense_ids = list(expense_ids or [])

    errors = _validate(merged)
    if not entry_ids and not expense_ids:
        errors.append("Sélectionnez au moins une entrée de temps ou une dépense.")
    if errors:
        return plan.refuse(errors, "validation")

    # Never dated in the future — measured on the UTC day (module docstring).
    invoice_day = _calendar_day(merged.get("date"))
    if invoice_day is None:
        # _validate saw a value, but not a date: refused, never judged.
        return plan.refuse([
            "La date de la facture est illisible : elle doit être une date. "
            "Rien n'a été créé."
        ], "validation")
    if invoice_day > today_utc():
        return plan.refuse([
            f"La date de la facture ({invoice_day.isoformat()}) est "
            "postérieure à aujourd'hui : une facture ne se date jamais dans "
            "l'avenir. Rien n'a été créé."
        ], "date_future")
    plan.due_date = merged.get("due_date") or default_due_date(merged["date"])

    # Build line items from selected sources. A source that is missing,
    # already invoiced, owned by a different dossier or (generated path)
    # non-billable is SKIPPED — only the sources that actually become line
    # items get flipped to invoiced. Why each was dropped is kept even
    # when require_all_sources is off, so the reason is computed once, in
    # the one place that actually knows it.
    for eid in entry_ids:
        entry = get_time_entry(eid)
        if not entry:
            # get_time_entry swallows a read failure into None, so the model
            # genuinely cannot tell a deleted entry from an unreadable one.
            # Say both rather than assert the wrong one.
            plan.skipped[eid] = "introuvable ou illisible"
            continue
        if entry.get("invoiced"):
            billed_on = entry.get("invoice_id") or ""
            plan.skipped[eid] = (
                f"déjà facturée (facture {billed_on})" if billed_on
                else "déjà facturée"
            )
            continue
        if entry.get("dossier_id") != dossier_id:
            plan.skipped[eid] = "rattachée à un autre dossier"
            continue
        if generated and entry.get("billable") is False:
            plan.skipped[eid] = "non facturable"
            continue
        plan.valid_entry_ids.append(eid)
        plan.expected_etags[eid] = entry.get("etag", "")
        plan.line_items.append({
            **_default_line_item(),
            "id": str(uuid.uuid4()),
            "type": "fee",
            "source_id": eid,
            "date": entry.get("date"),
            "description": entry.get("description", ""),
            "hours": entry.get("hours", 0),
            "rate": entry.get("rate", 0),
            "amount": entry.get("amount", 0),
            "taxable": True,
        })

    for eid in expense_ids:
        expense = get_expense(eid)
        if not expense:
            plan.skipped[eid] = "introuvable ou illisible"
            continue
        if expense.get("invoiced"):
            billed_on = expense.get("invoice_id") or ""
            plan.skipped[eid] = (
                f"déjà facturé (facture {billed_on})" if billed_on
                else "déjà facturé"
            )
            continue
        if expense.get("dossier_id") != dossier_id:
            plan.skipped[eid] = "rattaché à un autre dossier"
            continue
        plan.valid_expense_ids.append(eid)
        plan.expected_etags[eid] = expense.get("etag", "")
        plan.line_items.append({
            **_default_line_item(),
            "id": str(uuid.uuid4()),
            "type": "expense",
            "source_id": eid,
            "date": expense.get("date"),
            "description": expense.get("description", ""),
            "hours": None,
            "rate": None,
            "amount": expense.get("amount", 0),
            "taxable": expense.get("taxable", True),
        })

    if require_all_sources:
        # A repeated id appends two identical line items and doubles the fee:
        # the build loops above do not dedupe, and the web form cannot
        # produce a duplicate.
        for label, ids in (
            ("time_entry_ids", entry_ids),
            ("expense_ids", expense_ids),
        ):
            seen: set[str] = set()
            dupes: set[str] = set()
            for source_id in ids:
                if source_id in seen:
                    dupes.add(source_id)
                seen.add(source_id)
            if dupes:
                return plan.refuse([
                    f"`{label}` contient des identifiants en double : "
                    + ", ".join(sorted(dupes))
                    + ". Chaque source ne peut être facturée qu'une fois."
                ], "ids_en_double")
        if plan.skipped:
            grouped: dict[str, list[str]] = {}
            for sid, reason in plan.skipped.items():
                grouped.setdefault(reason, []).append(sid)
            details = " ; ".join(
                f"{reason} : {', '.join(sorted(ids))}"
                for reason, ids in sorted(grouped.items())
            )
            return plan.refuse([
                f"Sources inutilisables (rien n'a été écrit) — {details}."
            ], "sources_inutilisables")

    if not plan.line_items:
        return plan.refuse(["Aucune entrée valide sélectionnée."], "aucune_ligne")

    if adjustment is not None:
        adjustment_item, adjustment_errors = _adjustment_line_item(adjustment)
        if adjustment_errors:
            return plan.refuse(adjustment_errors, "ajustement")
        plan.line_items.append(adjustment_item)

    totals = compute_totals(plan.line_items)
    plan.totals = totals

    if expected_total is not None:
        if not isinstance(expected_total, int) or isinstance(expected_total, bool):
            return plan.refuse(
                ["`expected_total` doit être un entier de cents."], "total_attendu"
            )
        if expected_total != totals["total"]:
            gap = totals["total"] - expected_total
            supplied = len(entry_ids) + len(expense_ids)
            return plan.refuse([
                f"Le total reconstitué ({totals['total']} ¢) ne correspond "
                f"pas au total attendu ({expected_total} ¢) — écart "
                f"{gap:+d} ¢. Calculé : honoraires {totals['subtotal_fees']} ¢, "
                f"déboursés {totals['subtotal_expenses']} ¢, "
                f"TPS {totals['gst_amount']} ¢, TVQ {totals['qst_amount']} ¢, "
                f"sur {len(plan.line_items)} ligne(s) retenue(s) pour {supplied} "
                "source(s) fournie(s). Aucune facture n'a été créée."
            ], "total_attendu")

    retainer_applied = merged.get("retainer_applied", 0)
    if generated:
        if retainer_applied not in (0, None, False):
            plan.warnings.append(
                "La provision indiquée n'a pas été déduite : sur une facture "
                "émise ici, une provision s'impute APRÈS l'envoi, par un "
                "« paiement d'honoraires » tiré du fidéicommis — la déduire "
                "aussi à la création compterait deux fois le même argent."
            )
        plan.retainer_applied = 0
    else:
        # Retainer must stay within [0, total] so amount_due can never go
        # negative.
        if (
            not isinstance(retainer_applied, int)
            or isinstance(retainer_applied, bool)
            or retainer_applied < 0
            or retainer_applied > totals["total"]
        ):
            return plan.refuse([
                "La provision appliquée doit être comprise entre 0 $ et le "
                "total de la facture."
            ], "provision_hors_bornes")
        plan.retainer_applied = retainer_applied

    client_id = str(merged.get("client_id") or "").strip()
    client = None
    if client_id:
        try:
            client = _read_client_strict(client_id)
        except Exception:
            log_unexpected("plan_invoice: client read failed")
            return plan.refuse([
                "Impossible de vérifier le client du dossier (lecture "
                "impossible). Rien n'a été créé : réessayez dans un instant."
            ], "client_illisible")
    refusals = issuance_refusals(merged, totals, generated=generated, client=client)
    if refusals:
        return plan.refuse([m for m, _ in refusals], refusals[0][1])

    if generated:
        warning = number_year_warning(
            merged.get("date"), f"{today_mtl().strftime('%Y')}-F"
        )
        if warning:
            plan.warnings.append(warning)
    return plan


# ── CRUD ─────────────────────────────────────────────────────────────────


def create_invoice(
    dossier_id: str,
    selected_entry_ids: list[str],
    selected_expense_ids: list[str],
    data: dict,
    *,
    invoice_number: Optional[str] = None,
    expected_total: Optional[int] = None,
    require_all_sources: bool = False,
    adjustment: Optional[dict] = None,
) -> tuple[Optional[dict], list[str]]:
    """Create an invoice with line items from selected time entries and expenses.

    Every decision taken before the write — the source selection, the
    refusals, the totals, the issuance checks — is :func:`plan_invoice`'s,
    called with this call's own flags; a refused plan is returned as the
    error list and NOTHING is read beyond it (no counter, no number). The
    invoice document, all line items, and the invoiced=True flips for the
    retained sources are then committed in a single Firestore transaction
    that re-reads each source, so a concurrent invoicing aborts the whole
    creation (no orphan invoices, no double-billing). Returns (invoice,
    errors).

    Without ``invoice_number`` this is the GENERATED path: the invoice this
    application issues today, which :func:`plan_invoice` holds to the
    issuance rules (a client that exists, its billing address, the firm's
    tax numbers, a due date not before the date, no NON-BILLABLE time, and
    NO provision — ``retainer_applied`` is forced to 0). Its number follows
    the Montréal year of today (:func:`number_year_warning` names the case
    where that is not the year of its date).

    The four keyword-only arguments serve the historical import:

    * *invoice_number* — a number carried over from the previous system. The
      year counter is then **never read and never advanced**; without it the
      counter allocates as always. It cannot arrive through *data* (which
      ``merged.update`` clobbers), so ``request.form`` can never forge one.
      It is also what stamps the invoice ``imported`` (True here, False on
      the generated path — IMP-07's marker, never a caller's value).
    * *expected_total* — the grand total printed on the paper invoice. On any
      difference the creation is refused, with the gap, the breakdown and the
      retained-versus-supplied source count. No tolerance: one cent of
      silent drift is how a book of account starts lying.
    * *require_all_sources* — turn the silent skip into a refusal naming every
      offender. This lives in the model and not only in a caller because the
      skip happens in the pre-read: a skipped id never enters
      ``source_refs``, so ``_SourceConflictError`` can never catch it and a
      caller's pre-flight is a TOCTOU snapshot the model may not honour. The
      web form passes it too since lot 3a: a stale selection (an entry
      billed or moved in another tab) is refused by name instead of
      producing a silently shorter invoice.
    * *adjustment* — a named, caller-justified extra line item (see
      ``_adjustment_line_item``) for a legacy total the lines cannot
      reconstruct.

    Ordering is the contract: a refused import consumes nothing and never
    even reads the counter; a refused GENERATED invoice consumes nothing
    either, since its number is allocated inside the same transaction that
    writes the invoice (every read — sources, counter — before any write).
    The ONE uncertain answer is :data:`CREATE_OUTCOME_UNCERTAIN`: the
    transaction raised, and a raise out of a commit does not prove that
    nothing landed.

    Only the keys of ``_CREATE_DATA_KEYS`` are taken from *data*; the
    payment state is forced (``brouillon``, nothing paid): an invoice is born
    unpaid, and only the accounting register may say otherwise.
    """
    from models.time_entry import COLLECTION as TE_COLLECTION
    from models.expense import COLLECTION as EXP_COLLECTION

    merged = invoice_document_from(data)

    plan = plan_invoice(
        dossier_id, selected_entry_ids, selected_expense_ids, merged,
        generated=invoice_number is None,
        require_all_sources=require_all_sources,
        adjustment=adjustment,
        expected_total=expected_total,
    )
    if plan.errors:
        log_invoice_event("invoice_refused", "", outcome="refused",
                          operation="create", reason=plan.refusal_reason,
                          dossier_id=dossier_id)
        return None, plan.errors

    now = datetime.now(timezone.utc)
    invoice_id = str(uuid.uuid4())
    line_items = plan.line_items
    valid_entry_ids = plan.valid_entry_ids
    valid_expense_ids = plan.valid_expense_ids
    expected_etags = plan.expected_etags
    totals = plan.totals
    retainer_applied = plan.retainer_applied

    # Resolve the number LAST, so every refusal above happens before the
    # counter is touched. On the imported branch it is never touched at all.
    # Note the name `invoice_number` is NOT rebound — the transaction closure
    # below reads the parameter to decide whether to run the uniqueness read.
    counter_ref = None
    number_prefix = ""
    seed = 0
    if invoice_number is None:
        # The generated number is allocated INSIDE the invoice transaction
        # below. Only the first-of-year seed is read here (see
        # _counter_seed). Any failure aborts the creation — never fall back
        # to a guessed number.
        year = today_mtl().strftime("%Y")
        number_prefix = f"{year}-F"
        counter_ref = _invoice_counter_ref(year)
        try:
            seed = _counter_seed(counter_ref, number_prefix)
        except Exception:
            log_unexpected("create_invoice: invoice number seeding failed")
            return None, [
                "Impossible de générer le numéro de facture. Veuillez réessayer."
            ]
        resolved_number = ""  # decided inside the transaction
    else:
        resolved_number, number_errors = _clean_imported_number(invoice_number)
        if number_errors:
            log_invoice_event("invoice_refused", "", outcome="refused",
                              operation="create", reason="numero_importe_refuse",
                              dossier_id=dossier_id)
            return None, number_errors

    merged.update({
        "id": invoice_id,
        "invoice_number": resolved_number,
        "dossier_id": dossier_id,
        **totals,
        "gst_rate": GST_RATE_BPS,
        "qst_rate": QST_RATE_BPS,
        "retainer_applied": retainer_applied,
        "amount_due": totals["total"] - retainer_applied,
        # Born unpaid, whatever the caller sent — see _CREATE_DATA_KEYS.
        "status": "brouillon",
        "amount_paid": 0,
        "paid_date": None,
        # The import marker (IMP-07): the model's own answer — a number
        # carried over is an import, a counter number never is.
        "imported": invoice_number is not None,
    })
    provenance.stamp_create(merged, now)

    # The plan's due date: the caller's, else one calendar month after the
    # invoice date — decided once, by plan_invoice.
    merged["due_date"] = plan.due_date

    invoice_ref = db.collection(COLLECTION).document(invoice_id)
    source_refs = [
        db.collection(TE_COLLECTION).document(eid) for eid in valid_entry_ids
    ] + [
        db.collection(EXP_COLLECTION).document(eid) for eid in valid_expense_ids
    ]

    transaction = db.transaction()

    @firestore.transactional
    def _txn_create(txn: firestore.Transaction) -> None:
        # All reads must precede all writes in a Firestore transaction.
        # Re-read every retained source so a concurrent invoicing (or
        # deletion / dossier reassignment) aborts this creation entirely.
        for ref in source_refs:
            snap = ref.get(transaction=txn)
            src = snap.to_dict() if snap.exists else None
            if src and src.get("invoice_id") == invoice_id:
                # THIS call's own id: an earlier attempt of this very
                # transaction landed (_OwnCommitLandedError) — never a
                # « source changed », which would deny the invoice.
                raise _OwnCommitLandedError(ref.id)
            if (
                not src
                or src.get("invoiced")
                or src.get("dossier_id") != dossier_id
                or src.get("etag", "") != expected_etags.get(ref.id, "")
            ):
                raise _SourceConflictError(ref.id)

        # Still a READ, so it must precede every write below. Import path
        # only: on the generated path the monotonic counter already
        # guarantees uniqueness, and paying a query on the web form's hot
        # path would buy nothing. Single-field equality — served by the
        # automatic index, no composite.
        if invoice_number is not None:
            clash = list(
                db.collection(COLLECTION)
                .where(filter=FieldFilter("invoice_number", "==", resolved_number))
                .limit(1)
                .stream(transaction=txn)
            )
            if clash:
                if clash[0].id == invoice_id:
                    raise _OwnCommitLandedError(invoice_id)
                raise _DuplicateNumberError(resolved_number)

        # The last READ: the year counter, generated path only. Read after
        # the source checks so a conflict aborts before it is even looked
        # at, and written below WITH the invoice — an abort of this
        # transaction, for any reason, leaves the counter where it was.
        next_seq = 0
        if counter_ref is not None:
            number, next_seq = _next_invoice_number(
                counter_ref.get(transaction=txn), seed, number_prefix
            )
            merged["invoice_number"] = number

        if counter_ref is not None:
            txn.set(counter_ref, {"seq": next_seq, "updated_at": now})
        txn.set(invoice_ref, merged)
        for item in line_items:
            txn.set(
                invoice_ref.collection(LINE_ITEMS_SUB).document(item["id"]),
                item,
            )
        for ref in source_refs:
            txn.update(ref, {
                "invoiced": True,
                "invoice_id": invoice_id,
                **provenance.update_fields(now),
            })

    try:
        _txn_create(transaction)
    except _OwnCommitLandedError:
        # Ahead of the conflict branches: not a refusal — an earlier attempt
        # of this call wrote the invoice (its number consumed).
        log_unexpected("create_invoice: an earlier attempt of the "
                       "transaction landed", exc_info=False,
                       dossier_id=dossier_id)
        return None, [CREATE_OUTCOME_UNCERTAIN]
    except _SourceConflictError:
        log_invoice_event("invoice_refused", "", outcome="refused",
                          operation="create", reason="source_modifiee",
                          dossier_id=dossier_id)
        return None, [
            "Certaines entrées sélectionnées ont été modifiées ou facturées entre-temps. Veuillez réessayer."
        ]
    except _DuplicateNumberError as exc:
        log_invoice_event("invoice_refused", "", outcome="refused",
                          operation="create", reason="numero_existant",
                          dossier_id=dossier_id)
        return None, [
            f"Le numéro de facture « {exc} » existe déjà dans Pallas Athéna. "
            "Une facture importée conserve son numéro d'origine : vérifiez "
            "s'il n'a pas déjà été repris."
        ]
    # The uniqueness read lives inside the transaction, so a Firestore blip
    # lands in the generic handler below and REFUSES — deliberately unlike
    # create_dossier's file_number check, which fails open. A duplicated
    # number in a legal accounting register is invisible to the lawyer and
    # unrepairable without renumbering an artifact already sent to a client;
    # a blocked web form on a transient error is merely annoying. And the
    # answer is UNCERTAIN, never « nothing was created »: the raise can
    # follow a commit the server applied (CREATE_OUTCOME_UNCERTAIN).
    except Exception:
        log_unexpected("create_invoice: transaction failed")
        return None, [CREATE_OUTCOME_UNCERTAIN]
    provenance.note_commit(COLLECTION, invoice_id)
    log_invoice_event(
        "invoice_created", invoice_id,
        dossier_id=dossier_id,
        source_count=len(valid_entry_ids) + len(valid_expense_ids),
        generated_number=invoice_number is None,
    )

    return merged, []


def get_invoice(invoice_id: str) -> Optional[dict]:
    """Fetch a single invoice by ID (without line items)."""
    try:
        doc = db.collection(COLLECTION).document(invoice_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception as exc:
        logger.warning("get_invoice failed for %s: %s", sanitize_log_value(invoice_id), exc)
    return None


def get_invoice_strict(invoice_id: str) -> Optional[dict]:
    """One invoice (without line items) — a read failure PROPAGATES.

    ``None`` means the store answered « no such invoice » (or the id cannot
    name one), never « the read failed ». For a caller that WRITES money on
    the answer (``services/comptabilite.resolve_fee_invoice``, lot 5a):
    :func:`get_invoice` swallows an error into ``None``, and « facture
    inconnue » on a read blip is a refusal the lawyer cannot act on. The
    document id is authoritative for ``id``."""
    if not isinstance(invoice_id, str) or not invoice_id or "/" in invoice_id:
        return None
    snap = db.collection(COLLECTION).document(invoice_id).get()
    if not snap.exists:
        return None
    return {**(snap.to_dict() or {}), "id": snap.id}


def get_invoice_with_items(invoice_id: str) -> tuple[Optional[dict], list[dict]]:
    """Fetch an invoice and all its line items. Returns (invoice, items)."""
    invoice = get_invoice(invoice_id)
    if not invoice:
        return None, []

    items: list[dict] = []
    try:
        docs = (
            db.collection(COLLECTION)
            .document(invoice_id)
            .collection(LINE_ITEMS_SUB)
            .stream()
        )
        items = [d.to_dict() for d in docs]
        # Sort by date
        items.sort(
            key=lambda i: i.get("date") or datetime.min.replace(tzinfo=timezone.utc)
        )
    except Exception as exc:
        logger.warning(
            "get_invoice_with_items: line items load failed for %s: %s",
            sanitize_log_value(invoice_id), exc,
        )

    return invoice, items


def get_invoice_with_items_strict(
    invoice_id: str,
) -> tuple[Optional[dict], list[dict]]:
    """The invoice and its line items — a read failure PROPAGATES.

    ``(None, [])`` means the store answered « no such invoice » (or the id
    cannot name one) — never « the read failed ». The reader of a caller
    that WRITES on the strength of the answer (``services.note_honoraires``,
    lot 3a): :func:`get_invoice_with_items` swallows a failed line-item
    read into ``[]``, and a note d'honoraires filled from that printed the
    stored totals over empty tables — a client-facing document that looked
    complete. Same sort as the fail-open reader.
    """
    if not isinstance(invoice_id, str) or not invoice_id or "/" in invoice_id:
        return None, []
    invoice_ref = db.collection(COLLECTION).document(invoice_id)
    snap = invoice_ref.get()
    if not snap.exists:
        return None, []
    items = [d.to_dict() or {} for d in invoice_ref.collection(LINE_ITEMS_SUB).stream()]
    items.sort(
        key=lambda i: i.get("date") or datetime.min.replace(tzinfo=timezone.utc)
    )
    return snap.to_dict() or {}, items


def line_items_missing(invoice: dict, items: list[dict]) -> bool:
    """True when *invoice* shows a non-zero subtotal yet no line item was
    read — the reads cannot say what the invoice bills. The one rule of the
    void (``void_invoice_report`` refuses on it) and of the note
    d'honoraires (which would print totals over empty tables)."""
    return not items and int(invoice.get("subtotal", 0) or 0) != 0


def list_line_items(invoice_id: str) -> list[dict]:
    """The line items of one invoice, without re-reading the invoice.

    :func:`get_invoice_with_items` costs an extra document read the journal
    export does not need — it already holds every invoice document. Fails
    open to ``[]``: a journal row must still print (its stored totals are
    authoritative) even when the detail is unreadable.
    """
    try:
        docs = (
            db.collection(COLLECTION)
            .document(invoice_id)
            .collection(LINE_ITEMS_SUB)
            .stream()
        )
        return [d.to_dict() for d in docs]
    except Exception as exc:
        logger.warning(
            "list_line_items failed for %s: %s",
            sanitize_log_value(invoice_id), exc,
        )
        return []


def expense_split(invoice: dict, line_items: list[dict]) -> tuple[int, int]:
    """Disbursements split as ``(taxable_cents, non_taxable_cents)``.

    The Barreau's « Journal des honoraires » wants that split, which the
    invoice document does not store — only the ``subtotal_expenses`` total.
    So the STORED total stays authoritative and the items are used solely to
    carve out the non-taxable part: the two columns then always add back to
    ``subtotal_expenses``, and a row whose items are missing or unreadable
    still ties (everything falls under taxable, which is the ``taxable:
    True`` default the tax was computed under) instead of silently
    under-reporting the sheet's own subtotal.

    Fees are excluded — ``create_invoice`` always writes them ``taxable:
    True``, so they are the journal's « Honoraires » column whole.
    """
    non_taxable = sum(
        int(i.get("amount") or 0)
        for i in line_items
        if i.get("type") != "fee" and not i.get("taxable", True)
    )
    total_expenses = int(invoice.get("subtotal_expenses") or 0)
    return total_expenses - non_taxable, non_taxable


def list_invoices(
    status_filter: Optional[str] = None,
    dossier_id: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> list[dict]:
    """Return invoices, optionally filtered."""
    try:
        query = db.collection(COLLECTION)

        if dossier_id:
            query = query.where(filter=FieldFilter("dossier_id", "==", dossier_id))

        results = [doc.to_dict() for doc in query.stream()]

        # Client-side filters
        if status_filter:
            results = [r for r in results if r.get("status") == status_filter]

        if date_from:
            results = [r for r in results if r.get("date") and r["date"] >= date_from]
        if date_to:
            results = [r for r in results if r.get("date") and r["date"] <= date_to]

        # Sort by date descending
        results.sort(
            key=lambda e: e.get("date") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

        return results
    except Exception:
        return []


def list_invoices_strict(transaction) -> list[dict]:
    """Every invoice of every status, read inside *transaction* — strict and
    capped (``models/full_reads``): the financial statements' reader, where
    a failed or truncated read must never pass for « no invoice »
    (``list_invoices`` above fails open, for display). Given ``None``, one
    plain query outside any transaction: the reader of
    :func:`get_outstanding_totals`, which must SEE a failure to log it."""
    return full_reads.stream_all(db.collection(COLLECTION), transaction=transaction)


def list_sent_invoices_strict() -> list[dict]:
    """Every « envoyée » invoice — STRICT and capped (``models/full_reads``),
    one plain query outside any transaction: the read of the daily
    lateness sweep (``routes/taches_factures``) and of its preview
    (``scripts/apercu_factures_echues``), where a failed or truncated read
    must never pass for « nothing is late ». One equality on ``status`` and
    no order: the automatic single-field index serves it, no composite
    (``firestore.indexes.json`` declares no override on ``invoices.status``).
    The document id is authoritative for ``id``."""
    return full_reads.stream_all(
        db.collection(COLLECTION).where(
            filter=FieldFilter("status", "==", "envoyée")),
        transaction=None,
    )


#: The most invoices one run of the daily sweep marks « en_retard »: each
#: mark is its own transaction, and the cron targets the default service
#: (gunicorn --timeout 60). The rest waits for the next run, counted
#: (``remaining``), never dropped — the ``_PLAFOND_MUTATIONS`` precedent of
#: ``routes/taches_outlook``. The preview names it too.
OVERDUE_SWEEP_CAP = 100


def overdue_among(
    invoices: list[dict], *, today: date,
) -> tuple[list[dict], list[str]]:
    """The invoices of *invoices* that are late on *today*
    (:func:`is_overdue`), the longest-late first (:func:`first_overdue_day`,
    then the id) — the order the daily sweep marks them in and its preview
    lists them in, so the two can never disagree — and the ids of those whose
    lateness could not be judged (a stored amount :func:`balance_of` cannot
    read), each logged by its id, the exception's class and the amount's
    field (:func:`unreadable_balance_field`) — never a traceback, whose last
    line quotes the stored value. PURE but for that log."""
    _montreal_day(today)
    late: list[dict] = []
    unjudged: list[str] = []
    for invoice in invoices:
        try:
            overdue = is_overdue(invoice, today=today)
        except Exception as exc:
            # The class and the FIELD, never the traceback: int()'s error
            # quotes the stored amount (CLAUDE.md « Do not log PII »).
            fields = {"invoice_id": str(invoice.get("id") or ""),
                      "error_type": type(exc).__name__}
            unreadable = unreadable_balance_field(invoice)
            if unreadable is not None:
                fields["field"] = unreadable
            log_unexpected("invoice lateness unreadable", exc_info=False,
                           **fields)
            unjudged.append(str(invoice.get("id") or ""))
            continue
        if overdue:
            late.append(invoice)
    late.sort(key=lambda inv: (first_overdue_day(inv), str(inv.get("id") or "")))
    return late, unjudged


def normalize_reference(reference: str) -> str:
    """The matching key of an invoice number cited from outside Athéna — a
    trust fee payment's ``invoice_external_ref``, an invoice's number or its
    ``legacy_ref`` (stored as ``facture:<number>``): the ``facture:`` prefix
    dropped, every character that is not an ASCII letter or digit dropped,
    upper case. The previous system's reprise lost the dashes on the way
    (« 251601-01 » cited, « 25160101 » stored): the same identifier, spelt
    two ways — the key the 2026-08 reprise matched on
    (``scripts/reprise_encaissements.normaliser``), with the prefix it strips
    too. A comparison of identifiers, never a fuzzy one: the caller still
    demands ONE invoice per key. Linear, no regex."""
    text = str(reference or "").strip()
    if text.lower().startswith("facture:"):
        text = text[len("facture:"):]
    return "".join(ch for ch in text if ch.isascii() and ch.isalnum()).upper()


def is_imported(invoice: dict) -> bool:
    """True when *invoice* was taken over from the previous system
    (``import_invoice``) — PURE. A copy of ``mcp.import_audit.is_imported``
    (which may import no model), pinned equal to it by a test: the
    ``imported`` marker decides when present; without it (invoices stored
    before 2026-09-28), a ``legacy_ref`` or a connector creation
    (``created_via`` « mcp » — import_invoice was then its only creator)."""
    invoice = invoice or {}
    marker = invoice.get("imported")
    if isinstance(marker, bool):
        return marker
    if str(invoice.get("legacy_ref") or "").strip():
        return True
    return str(invoice.get("created_via") or "") == "mcp"


def _page_query(
    status_filter: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> "firestore.Query":
    """The filtered, (date DESC, id ASC)-ordered invoice query.

    ONE builder shared by :func:`list_invoices_page` and
    :func:`count_invoices_page`, so the page read and its total can never
    ride different indexes. The date range targets the PRIMARY sort field,
    so it consumes no extra index dimension.
    """
    query = db.collection(COLLECTION)
    if status_filter and status_filter in VALID_STATUSES:
        query = query.where(filter=FieldFilter("status", "==", status_filter))
    if date_from:
        query = query.where(filter=FieldFilter("date", ">=", date_from))
    if date_to:
        query = query.where(filter=FieldFilter("date", "<=", date_to))
    return query.order_by(
        "date", direction=firestore.Query.DESCENDING
    ).order_by("id")


def count_invoices_page(
    status_filter: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> Optional[int]:
    """Rows in the filtered set, or None when the count could not be read.

    Runs on the SAME query object as the page read - order_by INCLUDED - so
    the SAME composite index serves both. An aggregation forwards its nested
    query verbatim, and a COUNT adds no aggregated field to trail the index.
    Dropping the order_by makes the backend apply its own implicit ordering,
    a THIRD ordering that FAILS (measured 2026-09-07 against production).

    That shared ordering also keeps the count HONEST: an order_by excludes
    documents missing the key, from the count and the page read alike, so
    the two can never disagree (cross-checked against a bare collection
    COUNT: gap of 0).

    Returns None, NEVER 0, on failure - a rendered "/ 0" is a confident lie.
    """
    try:
        values = _aggregation_values(
            _page_query(status_filter, date_from, date_to).count(alias="n").get()
        )
        n = values.get("n")
        return int(n) if n is not None else None
    except Exception as exc:
        logger.warning("count_invoices_page: aggregation failed: %s", exc)
        return None


def list_invoices_page(
    status_filter: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: int = PAGE_SIZE,
    cursor: Optional[str] = None,
    offset: int = 0,
) -> tuple[list[dict], Optional[str]]:
    """Return one page of invoices plus an opaque cursor for the next page.

    Cursor-mode counterpart of :func:`list_invoices` for the list view:
    reads ~``limit`` documents per page (server-side filters + ``order_by``
    + ``start_after``) instead of streaming the whole collection. The
    legacy :func:`list_invoices` remains the path for exports, summaries
    and dossier-scoped views.

    The status filter is an equality and the optional date range targets
    ``date`` — the same field as the primary sort — so both composite
    indexes serve every filter combination here:
    ``(date DESC, id ASC)`` and ``(status ASC, date DESC, id ASC)``.

    Sort order: ``date DESC, id ASC``. The ``id`` field mirrors the
    document ID and is always set — a stable tiebreaker when several
    invoices share the same date.
    """
    if offset and cursor:
        raise ValueError(
            "cursor et offset s'excluent \u2014 chacun nomme une position"
        )
    try:
        query = _page_query(status_filter, date_from, date_to)
        if offset:
            # An ABSOLUTE page read, for a leap control. Same ordering, so
            # the same index - Firestore emits ONE query
            # (order_by -> offset -> limit). It bills the skipped documents,
            # which is why `page` is clamped before it ever reaches here.
            query = query.offset(offset)

        # decode_cursor yields values in encode order: [date, id].
        # Anything malformed (None or wrong arity) degrades to page 1.
        values = decode_cursor(cursor)
        if values and len(values) == 2:
            query = query.start_after({"date": values[0], "id": values[1]})

        # Fetch one extra row to know whether a next page exists.
        docs = [doc.to_dict() for doc in query.limit(limit + 1).stream()]

        next_cursor = None
        if len(docs) > limit:
            docs = docs[:limit]
            last = docs[-1]
            next_cursor = encode_cursor([last.get("date"), last.get("id")])

        return docs, next_cursor
    except Exception as exc:
        # PII-free: log only the exception type, never document contents.
        logger.warning("list_invoices_page failed: %s", type(exc).__name__)
        return [], None


# ── Write-offs (« radiation », lot 3a — module docstring) ────────────────

#: The kinds of a write-off event: a ``radiation`` writes off an overdue
#: invoice's whole remainder; a ``contre_passation`` cancels ONE radiation,
#: whole, repeating its figures. Stored keys: a rename is a migration.
WRITE_OFF_KINDS = ("radiation", "contre_passation")
_RADIATION, _CONTRE_PASSATION = WRITE_OFF_KINDS

#: The figures a contre_passation repeats from the radiation it cancels.
_WRITE_OFF_FIGURES = ("amount", "gst_amount", "qst_amount", "tax_claimed")

#: The ceiling of a write-off's stated reason — refused beyond, never
#: truncated (the void's own).
WRITE_OFF_REASON_MAX_LENGTH = 500

#: The form of a credit note's number (lot 3b, module docstring): « AAAA-NC »
#: — the Montréal year of its radiation — then a sequence of three digits or
#: more (:func:`_next_credit_note_number`). ASCII digits only, matched whole;
#: linear (CWE-1333).
CREDIT_NOTE_NUMBER_RE = re.compile(r"[0-9]{4}-NC[0-9]{3,}")


class WriteOffsUnreadable(ValueError):
    """The stored ``write_offs`` is not the shape its two doors write.

    Its message names the defect in French, by the event's position — never
    a stored value: an amount is no log field (CLAUDE.md « Do not log
    PII »), and a traceback carrying this message quotes nothing.
    ``scripts/verify_admin_integrity`` (check 13) prints it to the lawyer."""


def _is_cents(value: object, *, positive: bool = False) -> bool:
    """*value* is integer cents — never a bool, a float or a text — ``> 0``
    when *positive*, ``>= 0`` otherwise."""
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    return value > 0 if positive else value >= 0


def _utc_midnight_day(value: object) -> Optional[date]:
    """The calendar day a date-only value names — a tz-aware instant at
    midnight UTC, as every date-only field is stored (Rule 5) — or ``None``
    for anything else (a naive instant, a time of day, a text)."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    at = value.astimezone(timezone.utc)
    if (at.hour, at.minute, at.second, at.microsecond) != (0, 0, 0, 0):
        return None
    return at.date()


def write_off_events(invoice: dict) -> list[dict]:
    """The write-off events of *invoice*, in stored (append) order —
    VALIDATED: ``[]`` when the key is absent, :class:`WriteOffsUnreadable`
    when the array is not the shape the two doors write. PURE.

    The one judge of a well-formed array — :func:`balance_of`, the doors and
    check 13 of ``scripts/verify_admin_integrity`` read through it, and so
    must every other reader of the events (the financial statements, part 2
    of the lot) — so a malformed event is never counted as 0. Each event
    is a record with a unique, non-blank ``id``; a ``kind`` of
    :data:`WRITE_OFF_KINDS`; a ``date`` at midnight UTC; an ``amount`` of
    cents > 0; a ``gst_amount`` and a ``qst_amount`` of cents ≥ 0 that
    together do not exceed it; a boolean ``tax_claimed`` (both parts 0 when
    False); and a text ``reverses_id`` — ``''`` on a radiation, and on a
    contre_passation the id of a radiation EARLIER in the array, dated no
    later, whose four figures (:data:`_WRITE_OFF_FIGURES`) it repeats — no
    radiation cancelled twice. Since lot 3b, a radiation carries its credit
    note's ``credit_note_number`` (:data:`CREDIT_NOTE_NUMBER_RE`, the year of
    its own date, no other radiation of the invoice bearing it), and a
    contre_passation carries none — no write-off existed before, so no
    legacy shape is accepted. The reason and the stamps move no figure and
    are not judged here."""
    events = invoice.get("write_offs", [])
    if not isinstance(events, list):
        raise WriteOffsUnreadable("les radiations ne sont pas une liste")
    seen: set[str] = set()
    numbers: set[str] = set()
    radiations: dict[str, tuple[date, dict]] = {}
    cancelled: set[str] = set()
    for position, event in enumerate(events, start=1):
        where = f"événement n° {position}"
        if not isinstance(event, dict):
            raise WriteOffsUnreadable(f"{where} : ce n'est pas un enregistrement")
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise WriteOffsUnreadable(f"{where} : identifiant absent")
        if event_id in seen:
            raise WriteOffsUnreadable(f"{where} : identifiant en double")
        seen.add(event_id)
        kind = event.get("kind")
        if kind not in WRITE_OFF_KINDS:
            raise WriteOffsUnreadable(f"{where} : nature inconnue")
        day = _utc_midnight_day(event.get("date"))
        if day is None:
            raise WriteOffsUnreadable(
                f"{where} : date illisible (un jour, à minuit UTC)")
        if not _is_cents(event.get("amount"), positive=True):
            raise WriteOffsUnreadable(
                f"{where} : montant illisible (des cents entiers, positifs)")
        if not (_is_cents(event.get("gst_amount"))
                and _is_cents(event.get("qst_amount"))):
            raise WriteOffsUnreadable(
                f"{where} : TPS ou TVQ illisible (des cents entiers)")
        if event["gst_amount"] + event["qst_amount"] > event["amount"]:
            raise WriteOffsUnreadable(f"{where} : la TPS et la TVQ dépassent le montant")
        if not isinstance(event.get("tax_claimed"), bool):
            raise WriteOffsUnreadable(f"{where} : réclamation des taxes illisible")
        if not event["tax_claimed"] and (event["gst_amount"] or event["qst_amount"]):
            raise WriteOffsUnreadable(
                f"{where} : taxes non réclamées, mais une part de TPS ou de "
                "TVQ inscrite")
        reverses = event.get("reverses_id")
        if not isinstance(reverses, str):
            raise WriteOffsUnreadable(f"{where} : lien de contre-passation illisible")
        if kind == _RADIATION:
            if reverses:
                raise WriteOffsUnreadable(f"{where} : une radiation ne contre-passe rien")
            number = event.get("credit_note_number")
            if not (isinstance(number, str)
                    and CREDIT_NOTE_NUMBER_RE.fullmatch(number)):
                raise WriteOffsUnreadable(
                    f"{where} : numéro de note de crédit illisible (AAAA-NC###)")
            if number[:4] != f"{day.year:04d}":
                raise WriteOffsUnreadable(
                    f"{where} : numéro de note de crédit d'une autre année que "
                    "la radiation")
            if number in numbers:
                raise WriteOffsUnreadable(
                    f"{where} : numéro de note de crédit en double")
            numbers.add(number)
            radiations[event_id] = (day, event)
            continue
        if "credit_note_number" in event:
            raise WriteOffsUnreadable(
                f"{where} : une contre-passation ne porte aucun numéro de note "
                "de crédit")
        original = radiations.get(reverses)
        if original is None:
            raise WriteOffsUnreadable(
                f"{where} : contre-passation d'aucune radiation antérieure de "
                "la facture")
        if reverses in cancelled:
            raise WriteOffsUnreadable(f"{where} : radiation déjà contre-passée")
        radiation_day, radiation = original
        if any(event[name] != radiation[name] for name in _WRITE_OFF_FIGURES):
            raise WriteOffsUnreadable(
                f"{where} : contre-passation dont les montants diffèrent de la "
                "radiation")
        if day < radiation_day:
            raise WriteOffsUnreadable(
                f"{where} : contre-passation antérieure à sa radiation")
        cancelled.add(reverses)
    return list(events)


def standing_write_offs(invoice: dict) -> list[dict]:
    """The radiations of *invoice* that STAND — no contre_passation names
    them — in stored order. PURE; raises :class:`WriteOffsUnreadable` on a
    malformed array (:func:`write_off_events`)."""
    events = write_off_events(invoice)
    cancelled = {e["reverses_id"] for e in events if e["kind"] == _CONTRE_PASSATION}
    return [e for e in events
            if e["kind"] == _RADIATION and e["id"] not in cancelled]


def written_off_of(invoice: dict) -> int:
    """What stands written off *invoice*, in cents, taxes included: the sum
    of its standing radiations (:func:`standing_write_offs`). DERIVED, never
    stored. PURE; RAISES on a malformed array — a bad event never counts
    as 0."""
    return sum(e["amount"] for e in standing_write_offs(invoice))


class UnreadableAmount(ValueError):
    """A stored amount that cannot be read as integer cents. Its message
    is the FIELD NAME alone (the lawyer's decision of 2026-11-12):
    ``int()``'s own error quotes the stored value, and every traceback that
    carried it reached Cloud Logging (CLAUDE.md « Do not log PII »). Raised
    OUTSIDE the failed conversion, so it chains no context that would quote
    it either. A ValueError, as the conversion's own error was.

    Raised by :func:`balance_of`, so by every reader of the balance — the
    note d'honoraires, the journal PDF, an administration entry's « Solde
    dû actuel », the registers' pickers and caps — and by the connector's
    money reader (``mcp.handlers._cents``, which names it too)."""


def _stored_cents(invoice: dict, field: str) -> int:
    """``int()`` of *invoice*'s stored *field* — an absent one as 0, as
    :func:`balance_of` always read it — or :class:`UnreadableAmount` naming
    *field*, never the value."""
    value = invoice.get(field, 0)
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        pass
    raise UnreadableAmount(field)


def balance_of(invoice: dict) -> int:
    """The live balance in cents: what was due at issuance, less what came
    in, less what stands written off (lot 3a, :func:`written_off_of`).

    DERIVED, never stored — a stored balance drifts the moment either side
    of the subtraction is written without it. Note the trap it replaces:
    ``amount_due`` is frozen at issuance and stays non-zero on a fully paid
    invoice, so it has never been a balance despite reading like one. A
    stored amount it cannot read raises :class:`UnreadableAmount`, naming
    the field, never the value; a malformed ``write_offs`` raises
    :class:`WriteOffsUnreadable`, naming the event by its position
    (:func:`unreadable_balance_field` names which, beforehand).
    """
    return (_stored_cents(invoice, "amount_due")
            - _stored_cents(invoice, "amount_paid")
            - written_off_of(invoice))


#: What :func:`balance_of` reads, in the order it reads it: the two stored
#: amounts, then the write-off events (lot 3a).
_BALANCE_FIELDS = ("amount_due", "amount_paid", "write_offs")


def _write_offs_unreadable(invoice: dict) -> bool:
    """True when :func:`written_off_of` cannot read *invoice*'s array."""
    try:
        written_off_of(invoice)
    except WriteOffsUnreadable:
        return True
    return False


def unreadable_balance_field(invoice: dict) -> Optional[str]:
    """The first thing :func:`balance_of` cannot read — its FIELD NAME, in
    balance_of's order (``amount_due``, then ``amount_paid``, then, since
    lot 3a, ``write_offs``) — or ``None`` when it reads them all. PURE;
    ``None`` exactly when balance_of answers, on every type a Firestore
    field can hold and every shape of the array (``tests/test_invoice_lateness``
    pins the two together).

    The one way to say WHICH amount is unreadable: ``int()``'s own error
    quotes the stored value, and no log line — its traceback included —
    carries an amount (CLAUDE.md « Do not log PII »; review of lot 2)."""
    for name in _BALANCE_FIELDS:
        if name == "write_offs":
            if _write_offs_unreadable(invoice):
                return name
            continue
        try:
            int(invoice.get(name, 0))
        except (TypeError, ValueError, OverflowError):
            return name
    return None


#: How a refusal names each field :func:`unreadable_balance_field` can
#: return — the opening of its French sentence, never the stored value.
_UNREADABLE_OPENINGS = {
    "amount_due": "Le montant dû enregistré sur {where} est illisible",
    "amount_paid": "Le montant payé enregistré sur {where} est illisible",
    "write_offs": "Les radiations enregistrées sur {where} sont illisibles",
}


def unreadable_transition_field(invoice: dict) -> Optional[str]:
    """The first stored amount :func:`available_transitions` cannot read on
    *invoice* — its FIELD NAME — or ``None`` when it reads every amount it
    needs. PURE; ``None`` exactly when available_transitions answers, on
    every status and every type a Firestore field can hold
    (``tests/test_invoice_lateness`` pins the two together).

    An issued invoice's lateness reads its balance, strictly
    (:func:`unreadable_balance_field`: ``amount_due``, then ``amount_paid`` —
    a None or empty one is unreadable there); every invoice's transitions
    read whether a payment is recorded (:func:`_payment_recorded`:
    ``amount_paid``, an absent, None or empty one read as none — a None or
    empty one, which on an issued invoice the strict balance read has
    already refused). No transition reads the ``amount_due`` of a
    brouillon, a payée or an annulée — though, since 2026-11-12,
    :func:`update_status_report` refuses to SEND a brouillon, or REOPEN a
    payée, whose balance it cannot read, by its own rule: it would become
    an issued invoice whose balance is never judged. Since lot 3a, a
    payée's transitions also read whether a radiation stands
    (``write_offs``, after its ``amount_paid``) — an issued invoice's
    balance reads it already; a brouillon's and an annulée's never. What
    :func:`update_status_report` and :func:`void_invoice_report` refuse by
    name (``montant_illisible``) rather than fail generically, its
    traceback quoting the value (second review of lot 2; the void since
    2026-11-12)."""
    status = invoice.get("status", "")
    if status in _ISSUED_STATUSES:
        return unreadable_balance_field(invoice)
    try:
        _payment_recorded(invoice)
    except (TypeError, ValueError, OverflowError):
        return "amount_paid"
    if status == "payée" and _write_offs_unreadable(invoice):
        return "write_offs"
    return None


def payments_print_defect(invoice: dict, standing_receipts: list[dict]) -> Optional[str]:
    """Why the invoice's payments cannot be PRINTED as they stand, or ``None``.

    The note d'honoraires lists the standing encaissements imputed on the
    invoice (``models.admin_ledger.receipt_stands``) and, since lot 3a, its
    standing write-offs (:func:`standing_write_offs`, a « Note de crédit
    <n°> » row each, « Radiation » before lot 3b) above its live balance
    (:func:`balance_of`, which subtracts
    them): on paper, TOTAL APRÈS TAXES less the rows must give that
    balance. Two states break the sum, and the note refuses rather than
    print figures that do not close (``services/note_honoraires``):

    * ``payments_mismatch`` — the encaissement rows do not sum to
      ``amount_paid``: the register and the invoice drifted
      (``scripts/verify_admin_integrity``, check 8, reports the same). The
      write-off rows are not among them: they are the very events
      :func:`balance_of` subtracts, never a payment;
    * ``paid_by_hand`` — « payée » with a balance left: a status the lawyer
      set by hand, with nothing (or not everything) recorded, since
      :func:`payment_updates` and the write-off doors settle « payée » only
      at a zero balance. The note would print a balance owing under the
      lawyer's own « payée ». A « payée » a standing write-off settles has
      no balance left: its note prints, the credit note's row bringing it
      to zero (the lawyer's decision D2 — a forgiveness shown to the
      client, never refused).

    PURE: the caller reads the rows (strictly) and keeps the standing ones.
    ``amount_paid`` reads as :func:`_payment_recorded` reads it — absent,
    None or empty as 0 — and an amount ``int()`` cannot read raises
    :class:`UnreadableAmount`, naming the field, never the value (the
    lawyer's decision of 2026-11-12, which :func:`balance_of` follows).
    """
    paid = _stored_cents(invoice, "amount_paid") if invoice.get("amount_paid") else 0
    if sum(int(r.get("amount", 0) or 0) for r in standing_receipts) != paid:
        return "payments_mismatch"
    if invoice.get("status") == "payée" and balance_of(invoice) > 0:
        return "paid_by_hand"
    return None


def _settled_status(
    invoice: dict, *, paid: int, written_off: int, carries_write_off: bool,
) -> Optional[str]:
    """The status a settlement change leaves on *invoice* — ``None`` to
    leave it as stored. PURE, and no clock: the ONE rule of
    :func:`payment_updates` and of the two write-off doors (lot 3a).

    *invoice* is the stored document, BEFORE the change; *paid* and
    *written_off* are its settlement AFTER it — ``amount_paid`` and what
    stands written off (:func:`written_off_of`); *carries_write_off* —
    whether the invoice carries any write-off event after the change, a
    cancelled one included.

    * « payée » once ``paid + written_off`` reaches ``amount_due``, from an
      issued status (envoyée, en_retard);
    * a « payée » this module settled — a payment recorded, or a radiation
      standing, before the change — whose settlement now falls short is
      reopened: « en_retard » when the invoice carries a write-off event (a
      write-off happens only past the due date, and an issued invoice's due
      date never moves), « envoyée » otherwise (the daily sweep marks it
      again once due);
    * a « payée » set by hand, with nothing recorded, is the lawyer's
      statement and is never touched.

    The stored ``amount_paid`` is read strictly, on every status, as
    :func:`payment_updates` always read it: an unreadable one raises.
    """
    status = invoice.get("status", "")
    recorded_before = (int(invoice.get("amount_paid", 0)) > 0
                       or written_off_of(invoice) > 0)
    if paid + written_off >= int(invoice.get("amount_due", 0)):
        return "payée" if status in _ISSUED_STATUSES else None
    if status == "payée" and recorded_before:
        return "en_retard" if carries_write_off else "envoyée"
    return None


class PaymentRefused(Exception):
    """A payment the invoice cannot take — raised by :func:`payment_updates`.

    Inside :func:`record_payment`'s transaction it aborts it (nothing is
    written); inside a caller's own ledger transaction it must abort THAT
    transaction too — never be caught and the ledger entry committed without
    its payment. The message is French and user-facing.
    """


def payment_updates(
    invoice: dict,
    new_amount_paid: int,
    paid_date,
    *,
    now: datetime,
    reducing: bool = False,
) -> dict:
    """The fields a payment writes on *invoice* — the ONE computation of
    ``amount_paid``, ``paid_date`` and the status flip. Returns the update
    dict for a partial ``update()``; raises :class:`PaymentRefused`.

    PURE: no read, no write, no clock. The caller has read *invoice* (inside
    its transaction) and passes ``now``; the provenance stamp comes from
    ``provenance.update_fields`` like every model write (``updated_via`` from
    the request's ContextVar — ``mcp`` when the connector writes — and a
    fresh etag). It exists so a LEDGER transaction can stage the invoice's
    payment in the same commit as the entry that records the money, instead
    of projecting it afterwards with a second, non-transactional write (lot
    5, D2). :func:`record_payment` runs on it, byte for byte.

    The semantics — each one a decision that already cost something:

    * ``brouillon`` refuses: an invoice not yet sent owes nothing;
    * ``annulée`` refuses — unless ``reducing``: a REDUCTION (a reversed
      encaissement) must still be able to take back what was recorded on an
      invoice voided since, and leaves its status alone. ``reducing`` never
      raises the amount: a caller that passes more than what is recorded is
      refused, not obeyed;
    * the cap is ``amount_due`` less what stands written off (lot 3a,
      :func:`written_off_of`), NEVER ``total``: with a retainer applied,
      ``amount_due < total``, and capping on the total let a payment land
      between the two — a negative balance with nothing to explain it; and
      a written-off remainder is no longer owed;
    * the status follows :func:`_settled_status`, the one settlement rule
      it shares with the two write-off doors: the balance reaching zero
      flips ``envoyée``/``en_retard`` to ``payée``; a correction that
      reopens a balance undoes that flip — and ONLY that flip: a ``payée``
      a payment or a radiation settled goes back to ``en_retard`` when the
      invoice carries a write-off event, to ``envoyée`` otherwise; a
      ``payée`` set by hand, with nothing recorded, is the lawyer's
      statement and is never touched;
    * ``paid_date`` is ``None`` once nothing is paid (a stale date would
      read as « paid on that day »).

    A new payment on an invoice where a radiation stands is refused BEFORE
    this function, by the registers' own guard (``facture_radiee``): the
    computation itself only caps and settles. A malformed ``write_offs``
    raises :class:`WriteOffsUnreadable` (never a cap that ignores it).
    """
    if not isinstance(new_amount_paid, int) or isinstance(new_amount_paid, bool):
        raise PaymentRefused("Le montant encaissé doit être un nombre entier de cents.")
    amount = new_amount_paid
    if amount < 0:
        raise PaymentRefused("Le montant encaissé ne peut pas être négatif.")

    status = invoice.get("status", "")
    if status == "annulée" and not reducing:
        raise PaymentRefused(
            "Cette facture est annulée : aucun encaissement ne peut y "
            "être porté."
        )
    if status == "brouillon":
        raise PaymentRefused(
            "Cette facture est encore un brouillon : envoyez-la avant "
            "d'y porter un encaissement."
        )
    if reducing and amount > int(invoice.get("amount_paid", 0)):
        raise PaymentRefused(
            "Une réduction d'encaissement ne peut pas dépasser le montant "
            "déjà encaissé sur la facture."
        )

    # Cap on what is OWED, not on the invoice total. With a retainer
    # applied, amount_due < total, and capping on `total` let a payment
    # land between the two — producing a NEGATIVE balance with nothing
    # to explain it. An overpayment is a real event, but recording it
    # here would silently corrupt the balance rather than report it. What
    # stands written off is no longer owed either (lot 3a).
    written_off = written_off_of(invoice)
    cap = int(invoice.get("amount_due", 0)) - written_off
    if amount > cap:
        raise PaymentRefused(
            "Le montant encaissé ne peut pas dépasser le solde dû "
            f"({cap / 100:.2f} $). Pour un trop-perçu, portez-le au "
            "fidéicommis plutôt qu'à la facture."
        )

    updates: dict = {
        "amount_paid": amount,
        # A cleared payment has no date; keeping a stale one would read
        # as « paid on that day » for an invoice carrying no payment.
        "paid_date": paid_date if amount > 0 else None,
        **provenance.update_fields(now),
    }

    # The flip and its narrow undo — see the docstring and _settled_status.
    settled = _settled_status(
        invoice, paid=amount, written_off=written_off,
        carries_write_off=bool(write_off_events(invoice)))
    if settled is not None:
        updates["status"] = settled
    return updates


def get_invoices_by_number(number: str) -> list[dict]:
    """Every invoice bearing *number* — STRICT: a read failure RAISES.

    For resolving a fee invoice by the number the lawyer typed or Claude
    cites. The resolution it replaces (``routes/trust._resolve_invoice_number``)
    went through :func:`list_invoices`, which fails OPEN to ``[]`` — a read
    blip then answered « Aucune facture » for an invoice that exists. Here a
    swallowed error is impossible: the caller decides what an unreadable
    store means.

    Returns EVERY match, so the caller can refuse an ambiguity rather than
    pick one: ``create_invoice`` checks uniqueness inside its transaction,
    but a lookup cannot vouch for every number the base has ever held. A
    blank number is ``[]`` without a read. One equality on
    ``invoice_number`` — the automatic single-field index, the shape
    :func:`invoice_number_exists` already reads. The document id is
    authoritative for ``id`` (a caller writes through it).
    """
    wanted = (number or "").strip()
    if not wanted:
        return []
    return [
        {**(snap.to_dict() or {}), "id": snap.id}
        for snap in db.collection(COLLECTION)
        .where(filter=FieldFilter("invoice_number", "==", wanted))
        .stream()
    ]


def record_payment(
    invoice_id: str,
    amount_paid: int,
    paid_date: Optional[datetime] = None,
) -> tuple[Optional[dict], list[str]]:
    """Record (or correct) the amount received on an invoice.

    Owns BOTH payment fields and the status flip in one transaction:

    * balance reaches zero → the invoice flips to ``payée``;
    * a CORRECTION that reopens a balance undoes that flip, back to
      ``envoyée`` — ``en_retard`` when the invoice carries a write-off
      event (lot 3a, :func:`_settled_status`).

    That second half is not a nicety. ``payée`` does now have a manual exit
    (``payée → envoyée``), but ``available_transitions`` closes it the moment
    a payment is RECORDED — a ledger-backed ``payée`` corrects by
    contre-passation, never by hand. So for the ONE status this function can
    set, the undo below remains the only way back, and an erroneous amount
    would still strand the invoice without it. The ledger no longer calls
    this function: since lot 5a ``models/admin_ledger`` stages
    :func:`payment_updates` — the same computation, undo included — in the
    encaissement's (or the reversal's) own transaction. Its remaining
    callers are the two hand-run maintenance scripts that clear a payment
    (``record_payment(id, 0)``). The invoice's own payment form, the second,
    ledger-blind writer of ``amount_paid``, was removed on 2026-08-17. The undo is deliberately NARROW: it only
    reverses a flip this function could have made (status ``payée`` with a
    recorded payment that no longer covers the invoice). A ``payée`` set by
    hand, with no payment recorded, is never touched — that status is the
    lawyer's statement, not an inference of ours.

    ``amount_paid = 0`` clears the payment entirely (the full correction).
    Returns ``(updated_invoice, errors)``; fails CLOSED on any read error.

    The computation — refusals, cap, flip and its narrow undo — is
    :func:`payment_updates`, the pure function a ledger transaction stages
    in its own commit; this function reads the invoice in its transaction,
    applies it and writes. A refusal aborts before any write.
    """
    errors: list[str] = []
    try:
        amount = int(amount_paid)
    except (TypeError, ValueError):
        return None, ["Le montant encaissé doit être un nombre entier de cents."]
    if amount < 0:
        return None, ["Le montant encaissé ne peut pas être négatif."]

    ref = db.collection(COLLECTION).document(invoice_id)

    @firestore.transactional
    def _apply(transaction) -> dict:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise PaymentRefused("Facture introuvable.")
        invoice = snap.to_dict() or {}
        updates = payment_updates(
            invoice, amount, paid_date, now=datetime.now(timezone.utc)
        )
        transaction.update(ref, updates)
        return {**invoice, **updates}

    try:
        return _apply(db.transaction()), errors
    except PaymentRefused as refusal:
        return None, [str(refusal)]
    except Exception:
        log_unexpected("invoice operation failed")
        return None, ["Erreur. Veuillez réessayer."]


#: Why « annulée » is refused by update_status — shown to the web user AND,
#: later, to the connector. It names the one path that releases the sources.
VOID_BY_STATUS_REFUSED = (
    "Une facture ne s'annule pas par un changement de statut : utilisez "
    "« Annuler » sur la fiche de la facture — c'est la seule voie qui libère "
    "ses entrées de temps et ses déboursés."
)


def update_status(
    invoice_id: str,
    new_status: str,
    *,
    expected_etag: Optional[str] = None,
) -> tuple[bool, str]:
    """Transition an invoice to a new status. Returns (success, error).

    :func:`update_status_report` for a caller that needs only the verdict —
    the historical signature, kept for the web route and the scripts. Always
    a change made by hand (cause ``manuel``).
    """
    _doc, errors = update_status_report(
        invoice_id, new_status, expected_etag=expected_etag)
    if errors:
        return False, errors[0]
    return True, ""


#: What caused a status change, as ``invoice_status_changed`` records it:
#: ``manuel`` — someone asked for it (the sheet, the connector, a script);
#: ``echeance`` — the due date, through the daily lateness sweep
#: (``routes/taches_factures``), the only caller that names it.
STATUS_CAUSES = ("manuel", "echeance")

#: :func:`update_status_report`'s answer when its transaction RAISED — the
#: one error that is not a refusal of the change itself (the daily sweep
#: counts it apart). Nothing is known to be written: a commit whose answer
#: was lost may have landed, which a re-read settles. Its ERROR line names
#: the invoice (``invoice_id``): one request of the sweep marks up to
#: :data:`OVERDUE_SWEEP_CAP` of them (review of lot 2).
STATUS_UPDATE_FAILED = "Erreur. Veuillez réessayer."


def _lateness_refusal(invoice: dict, new_status: str) -> Optional[_StatusRefused]:
    """The refusal of a move between « envoyée » and « en_retard » that
    lateness forbids — naming the day lateness begins — or ``None`` when
    lateness is not why *new_status* is refused. Reached only once
    :func:`available_transitions` has refused the move."""
    current = invoice.get("status", "")
    first = first_overdue_day(invoice)
    if current == "envoyée" and new_status == "en_retard":
        if first is None:
            return _StatusRefused(
                "Cette facture n'a pas de date d'échéance : elle ne peut pas "
                "être « en retard ».", "pas_en_retard")
        if balance_of(invoice) <= 0:
            return _StatusRefused(
                "Cette facture n'a plus de solde dû : elle ne peut pas être "
                "« en retard ».", "pas_en_retard")
        return _StatusRefused(
            "L'échéance de cette facture n'est pas passée : elle sera en "
            f"retard à compter du {first.isoformat()} (une échéance qui tombe "
            "un samedi, un dimanche ou un jour férié est reportée au jour "
            "ouvrable suivant).", "pas_en_retard")
    if current == "en_retard" and new_status == "envoyée" and first is not None:
        return _StatusRefused(
            f"Cette facture est en retard depuis le {first.isoformat()} : le "
            "retard suit l'échéance et ne se retire pas. « Retirer le retard » "
            "ne corrige qu'un retard marqué avant l'échéance.", "retard_avere")
    return None


#: The French name of each stored amount :func:`unreadable_balance_field`
#: can name — what a ``montant_illisible`` refusal says, never the value,
#: which ``int()``'s own error quotes. One per field of ``_BALANCE_FIELDS``
#: (``tests/test_invoice_unreadable_amounts`` pins the two together); read
#: by the refusals below, the invoice sheet and list, and the connector
#: (2026-11-12).
BALANCE_FIELD_LABELS = {"amount_due": "montant dû", "amount_paid": "montant payé",
                        "write_offs": "radiations"}


def unreadable_amount_message(field: str, consequence: str, *,
                              number: str = "") -> str:
    """The ``montant_illisible`` sentence, in French: the stored amount
    *field* named — never its value —, what cannot be judged
    (*consequence*), and the repair it calls for. *number* names the invoice
    when given (the connector, whose reads list many), « cette facture »
    otherwise. One sentence family for every surface (2026-11-12)."""
    where = f"la facture {number}" if number else "cette facture"
    opening = _UNREADABLE_OPENINGS.get(
        field, f"Le {field} enregistré sur {{where}} est illisible")
    return (f"{opening.format(where=where)} : {consequence}. La facture "
            "doit d'abord être réparée.")


def update_status_report(
    invoice_id: str,
    new_status: str,
    *,
    expected_etag: Optional[str] = None,
    cause: str = "manuel",
) -> tuple[Optional[dict], list[str]]:
    """Transition an invoice to a new status — ``(invoice, errors)``.

    The invoice returned is the one WRITTEN (its new etag included), so a
    caller that hands the version back — the connector's
    ``update_invoice`` (lot 3b) — never re-reads it after the commit.

    « annulée » is refused FIRST, before anything is read: a bare status
    write left every time entry and disbursement flagged invoiced for good
    (``void_invoice`` then refused « déjà annulée » and ``delete_invoice``
    refused the references still hanging on). Voiding goes through
    :func:`void_invoice`, and only there.

    The read, the checks and the write run in ONE transaction. They used to
    be a read then an unconditional ``update()``, so a concurrent automatic
    flip to « payée » by ``record_payment`` could be overwritten by
    « en retard ». *expected_etag* — the version the caller's page was
    rendered from — is compared INSIDE that transaction
    (``models.concurrency``); ``None`` asserts nothing (a page rendered
    before its form carried one, a script).

    Lateness is judged there too, on ``today_mtl()`` read inside the
    transaction (lot 2, 2026-11-11): « en_retard » only while the invoice is
    overdue, back to « envoyée » only while it is not
    (:func:`available_transitions`), each refusal naming the day lateness
    begins (:func:`first_overdue_day`). *cause* — ``manuel`` or, from the
    daily sweep alone, ``echeance`` (:data:`STATUS_CAUSES`) — is what the
    ``invoice_status_changed`` line records beside the writer's path; a
    cause outside it, or ``echeance`` for any status but « en_retard », is a
    caller's bug and raises before anything is read.

    Every transition reads stored amounts through
    :func:`available_transitions`: whether a payment is recorded
    (``amount_paid``, on every status — :func:`_payment_recorded`, which
    reads an absent, None or empty one as none) and, for an issued
    invoice's lateness, its balance (:func:`balance_of`, which reads both
    amounts strictly). An amount it cannot read
    (:func:`unreadable_transition_field`) is refused by NAME (reason
    ``montant_illisible``, the field in French), never judged — and never
    the generic failure, whose traceback quoted the value (reviews of
    lot 2; the second found every status's ``amount_paid`` still read
    there, a read older than the lot). No transition reads a brouillon's
    ``amount_due``, and the absent, None or empty ``amount_paid`` of a
    brouillon, a payée or an annulée reads as no payment, as it always did.
    The SEND and the REOPEN are judged on more (2026-11-12: the send
    reversing lot 2's « an unreadable ``amount_due`` locks none », the
    reopen by the lawyer's decision): « brouillon → envoyée » and « payée
    → envoyée » are refused by name (``montant_illisible``) when
    :func:`balance_of` cannot read the invoice
    (:func:`unreadable_balance_field` — its ``amount_due``, or a None or
    empty ``amount_paid``: a « payée » set by hand reopens only when its
    balance can be read), since sent or reopened it would be an issued
    invoice whose balance is never judged; it used to commit the send, and
    the connector's answer then failed after the commit, its ERROR
    traceback quoting the value. An ISSUED invoice's None or empty
    ``amount_paid`` is refused, by contrast (an absent one reads as 0):
    its balance cannot be read, so
    neither « envoyée → en_retard » nor « en_retard → envoyée », which went
    through on it before lot 2, can be judged (third review of lot 2: this
    docstring said every None or empty one read as no payment).

    Since lot 3a the balance also reads the write-off events, and a payée's
    transitions read whether a radiation stands: a malformed ``write_offs``
    is refused by name the same way (« Les radiations enregistrées sur
    cette facture sont illisibles »). « Rouvrir » a payée that a standing
    radiation settles is refused naming its way back, « Annuler la
    radiation » (reason ``radiation_inscrite``).
    """
    if cause not in STATUS_CAUSES:
        raise ValueError(f"cause inconnue : {cause!r}")
    if cause == "echeance" and new_status != "en_retard":
        raise ValueError("l'échéance ne cause que le passage à « en_retard »")
    if new_status == "annulée":
        log_invoice_event("invoice_refused", invoice_id, outcome="refused",
                          operation="status", reason="annulation_par_statut")
        return None, [VOID_BY_STATUS_REFUSED]

    ref = db.collection(COLLECTION).document(invoice_id)

    @firestore.transactional
    def _apply(transaction) -> tuple[str, dict]:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise _StatusRefused("Facture introuvable.", "introuvable")
        invoice = snap.to_dict() or {}
        if not concurrency.matches(invoice, expected_etag):
            raise concurrency.StaleWrite()

        current = invoice.get("status", "")
        # available_transitions reads THIS invoice's stored amounts: whether
        # a payment is recorded, on every status, and an issued invoice's
        # balance for its lateness (is_overdue → balance_of). An amount it
        # cannot read is refused by name, never judged: int()'s error quotes
        # the stored value, and the catch-all below would log it in its
        # traceback.
        unreadable = unreadable_transition_field(invoice)
        if unreadable is not None:
            judged = ("son retard" if current in _ISSUED_STATUSES
                      else "son changement de statut")
            raise _StatusRefused(unreadable_amount_message(
                unreadable,
                f"{judged} ne peut pas être jugé, et rien n'a été écrit"),
                "montant_illisible")
        # The send and the reopen (2026-11-12: the send reversing lot 2,
        # the reopen by the lawyer's decision) both make an issued invoice,
        # whose lateness reads its balance, strictly — one whose balance
        # cannot be read would be an issued invoice whose balance is never
        # judged. Neither a brouillon's nor a payée's transitions read its
        # amount_due or a None or empty amount_paid (« no payment »), so
        # every move INTO an issued status judges them here, by name,
        # before any write.
        if current not in _ISSUED_STATUSES and new_status in _ISSUED_STATUSES:
            unissuable = unreadable_balance_field(invoice)
            if unissuable is not None:
                once = ("une fois envoyée" if current == "brouillon"
                        else "une fois rouverte")
                raise _StatusRefused(unreadable_amount_message(
                    unissuable,
                    f"{once}, son solde ne pourrait jamais être "
                    "jugé, et rien n'a été écrit"),
                    "montant_illisible")
        if new_status not in available_transitions(invoice, today=today_mtl()):
            # Nommer la voie plutôt que de refuser sèchement : la seule raison
            # qu'une facture payée ait de rester fermée est qu'un encaissement
            # l'adosse, et cet encaissement a son propre chemin de correction
            # — ou, depuis le lot 3a, qu'une radiation la solde, et c'est
            # « Annuler la radiation » qui la rouvre.
            if (current == "payée" and new_status == "envoyée"
                    and standing_write_offs(invoice)):
                raise _StatusRefused(
                    "Cette facture est soldée par une radiation : « Annuler "
                    "la radiation », sur sa fiche, la rouvrira d'elle-même.",
                    "radiation_inscrite")
            if current == "payée" and new_status == "envoyée":
                raise _StatusRefused(
                    "Cette facture porte un encaissement inscrit au grand livre. "
                    "Contre-passez l'écriture dans « Administration » — la facture "
                    "rouvrira d'elle-même."
                )
            if new_status == "payée":
                raise _StatusRefused(
                    "Une facture se marque payée par un encaissement inscrit au "
                    "registre d'administration, jamais à la main."
                )
            lateness = _lateness_refusal(invoice, new_status)
            if lateness is not None:
                raise lateness
            raise _StatusRefused(
                f"Transition de « {STATUS_LABELS.get(current, current)} » vers "
                f"« {STATUS_LABELS.get(new_status, new_status)} » non permise."
            )

        updates = {
            "status": new_status,
            **provenance.update_fields(datetime.now(timezone.utc)),
        }
        transaction.update(ref, updates)
        return current, {**invoice, **updates}

    try:
        previous, written = _apply(db.transaction())
    except concurrency.StaleWrite:
        log_invoice_event("invoice_refused", invoice_id, outcome="refused",
                          operation="status", reason="stale_etag")
        return None, [concurrency.STALE_ETAG_ERROR]
    except _StatusRefused as refusal:
        log_invoice_event("invoice_refused", invoice_id, outcome="refused",
                          operation="status", reason=refusal.reason)
        return None, [str(refusal)]
    except Exception:
        log_unexpected("invoice status update failed", invoice_id=invoice_id)
        return None, [STATUS_UPDATE_FAILED]
    provenance.note_commit(COLLECTION, invoice_id)
    log_invoice_event("invoice_status_changed", invoice_id,
                      from_status=previous, to_status=new_status,
                      cause=cause, via=written.get("updated_via", ""))
    return written, []


#: The ceiling of a void's stated reason — refused beyond, never truncated.
VOID_REASON_MAX_LENGTH = 500


def invalid_void_reason(reason: object) -> list[str]:
    """Why a void's *reason* is refused rather than altered — ``[]`` if fine.

    It is stored on the invoice and shown on its sheet; ``sanitize`` would
    truncate it and delete a « < … > » run in silence, so both are refused
    first (the draft texts' rule). ``''`` is fine: the web form states none.
    """
    if not isinstance(reason, str):
        return ["Le motif de l'annulation doit être un texte."]
    if len(reason) > VOID_REASON_MAX_LENGTH:
        return [
            f"Le motif de l'annulation dépasse {VOID_REASON_MAX_LENGTH} "
            f"caractères ({len(reason)}) : il n'est jamais tronqué — "
            "raccourcissez-le."
        ]
    if sanitize(reason, max_length=VOID_REASON_MAX_LENGTH) != reason:
        return [
            "Le motif de l'annulation contient un passage entre chevrons "
            "(< … >) qui serait supprimé à l'enregistrement : retirez les "
            "chevrons."
        ]
    return []


def void_invoice_report(
    invoice_id: str,
    *,
    expected_etag: Optional[str] = None,
    reason: str = "",
) -> tuple[Optional[dict], list[str]]:
    """Void an invoice — status « annulée » — and release its sources.

    *reason* — why it is voided — is stored as ``void_reason`` beside
    ``voided_at``, the instant of the void (lot 3b: the connector's void
    demands one; the web form states none, ``''``). It is judged before
    anything is read (:func:`invalid_void_reason`): over-long or carrying a
    « < … > » run, it is REFUSED, never truncated.

    Returns ``(report, errors)``. The report::

        {"invoice": <the invoice as written>,
         "released_time_entry_ids": [...], "released_expense_ids": [...],
         "foreign_source_ids": [...], "missing_source_ids": [...]}

    ONE Firestore transaction, every read before any write:

    * the invoice (its status, its etag, its ``amount_paid``);
    * its line items, and the ``timeentries``/``expenses`` whose
      ``invoice_id`` still names it — the UNION is released, so a void can
      never leave behind a source ``delete_invoice`` would later find
      stranded;
    * the administration entries and the trust payments imputed on it.

    Refused — nothing written — when the invoice is missing, already
    annulée, settled by a radiation that still stands (lot 3a,
    ``radiation_inscrite``: « Annuler la radiation » first — judged before
    « payée », whose sentence names an encaissement), payée, not the version
    *expected_etag* names, or when money
    still stands on it: a recorded ``amount_paid``, a standing
    ``encaissement_facture`` in « Administration », a standing
    ``virement_honoraires`` in « Fidéicommis ». It used to check the status
    only: voiding a partly paid invoice released its hours for re-billing
    while the payment stayed on the voided invoice — the client billed
    twice for the same work. The refusal names WHERE to reverse the
    payment first: the trust register when a trust fee payment is involved
    (it is the source of truth of that pair, and reverses the linked admin
    receipt itself), « Administration » otherwise.

    The stored amounts are judged through their authority (2026-11-12): one
    the void cannot read (:func:`unreadable_transition_field` — an issued
    invoice's balance, any other's ``amount_paid``) is refused by name
    (``montant_illisible``, the field in French), and a recorded payment is
    :func:`_payment_recorded`. The void read ``amount_paid`` itself: a value
    ``int()`` could not read fell into the generic failure, its traceback
    quoting it, and an issued invoice's None or empty one read as no payment
    — the void committed, and the connector's answer, which reads the
    balance, then failed after the commit.

    It NEVER refuses because of a source. A source whose ``invoice_id`` is
    empty is released — rewritten only if it is still flagged invoiced, so
    an already-released row keeps its etag; one that names ANOTHER invoice
    has been billed again since — it is left untouched and reported in
    ``foreign_source_ids``; a source document that no longer exists has
    nothing to release and is reported in ``missing_source_ids``. Refusing
    on either would strand the invoice for good — the victims of the
    read-then-set race on the time-entry form are exactly such invoices,
    and the duplicate invoice the race produced is the one the lawyer needs
    to void. What IS refused is a line-item subcollection that comes back
    EMPTY under a non-zero subtotal: the reads could not tell which sources
    to release, and guessing would be worse than stopping (fail closed —
    as is any read that raises).
    """
    from models.time_entry import COLLECTION as TE_COLLECTION
    from models.expense import COLLECTION as EXP_COLLECTION

    reason_errors = invalid_void_reason(reason)
    if reason_errors:
        log_invoice_event("invoice_refused", invoice_id, outcome="refused",
                          operation="void", reason="motif_invalide")
        return None, reason_errors
    reason = reason.strip()

    invoice_ref = db.collection(COLLECTION).document(invoice_id)

    @firestore.transactional
    def _apply(transaction) -> dict:
        snap = invoice_ref.get(transaction=transaction)
        if not snap.exists:
            raise _VoidRefused("Facture introuvable.", "introuvable")
        invoice = snap.to_dict() or {}

        status = invoice.get("status", "")
        if status == "annulée":
            raise _VoidRefused("Cette facture est déjà annulée.", "deja_annulee")
        # Lot 3a — a standing radiation settles the invoice as money does: it
        # is cancelled first, by its own door. Before « payée », whose
        # sentence (an encaissement, or a status set by hand) would send the
        # lawyer the wrong way on an invoice a radiation settles. The void
        # reads no balance, so an unreadable array is refused under a code
        # of its own — never the balance doors' unreadable-amount code,
        # which the void has never answered (DEPLOYMENT.md §15, lot 2,
        # item 1).
        try:
            radiated = bool(standing_write_offs(invoice))
        except WriteOffsUnreadable:
            raise _VoidRefused(
                unreadable_amount_message(
                    "write_offs",
                    "l'annulation ne peut pas être jugée, et rien n'a été "
                    "annulé"),
                "radiations_illisibles",
            ) from None
        if radiated:
            raise _VoidRefused(
                "Une radiation est inscrite sur cette facture : annulez-la "
                "d'abord (« Annuler la radiation », sur sa fiche), puis "
                "annulez la facture. Rien n'a été annulé.",
                "radiation_inscrite",
            )
        if status == "payée":
            raise _VoidRefused(
                "Impossible d'annuler une facture déjà payée : contre-passez "
                "d'abord l'encaissement qui l'a soldée, ou rouvrez-la si le "
                "statut a été posé à la main.",
                "payee",
            )
        if not concurrency.matches(invoice, expected_etag):
            raise concurrency.StaleWrite()

        items = [
            d.to_dict() or {}
            for d in invoice_ref.collection(LINE_ITEMS_SUB).stream(
                transaction=transaction
            )
        ]
        admin_rows = [
            d.to_dict() or {}
            for d in db.collection(_ADMIN_TRANSACTIONS)
            .where(filter=FieldFilter("invoice_id", "==", invoice_id))
            .stream(transaction=transaction)
        ]
        trust_rows = [
            d.to_dict() or {}
            for d in db.collection(_TRUST_TRANSACTIONS)
            .where(filter=FieldFilter("invoice_id", "==", invoice_id))
            .stream(transaction=transaction)
        ]

        # Money first, the trust register first of all: it is the source of
        # truth of a fee payment and of the admin receipt it minted.
        standing_admin = [r for r in admin_rows if _standing_admin_encaissement(r)]
        trust_linked = any(_standing_trust_fee_payment(r) for r in trust_rows) or any(
            r.get("trust_transaction_id") for r in standing_admin
        )
        if trust_linked:
            raise _VoidRefused(
                "Cette facture est acquittée, en tout ou en partie, par un "
                "paiement d'honoraires tiré du fidéicommis : contre-passez ce "
                "paiement dans « Fidéicommis » (la recette d'administration "
                "liée suit d'elle-même), puis annulez la facture. Rien n'a été "
                "annulé.",
                "paiement_fideicommis",
            )
        # The amounts through their authority (2026-11-12): one the void's
        # judgment cannot read is refused by name, never judged — int()'s
        # error quoted the stored value, and the catch-all below logged it
        # in its traceback.
        unreadable = unreadable_transition_field(invoice)
        if unreadable is not None:
            raise _VoidRefused(unreadable_amount_message(
                unreadable,
                "son annulation ne peut pas être jugée, et rien n'a été "
                "annulé"),
                "montant_illisible")
        if _payment_recorded(invoice):
            raise _VoidRefused(
                f"Cette facture porte un encaissement de "
                f"{format_cents_fr(invoice.get('amount_paid', 0))} : "
                "contre-passez l'écriture "
                "dans « Administration », puis annulez la facture — sinon ses "
                "heures redeviendraient facturables alors que le paiement "
                "resterait sur une facture annulée. Rien n'a été annulé.",
                "paiement_inscrit",
            )
        if standing_admin:
            raise _VoidRefused(
                "Une écriture d'encaissement du registre d'administration est "
                "encore imputée sur cette facture : contre-passez-la dans "
                "« Administration », puis annulez la facture. Rien n'a été "
                "annulé.",
                "encaissement_administration",
            )

        if line_items_missing(invoice, items):
            raise _VoidRefused(
                "Les lignes de cette facture sont introuvables alors que son "
                "sous-total n'est pas nul : impossible de savoir quelles "
                "entrées libérer. Rien n'a été annulé.",
                "lignes_illisibles",
            )

        # The sources: every one a line item names, plus every one that
        # still names this invoice. {(collection, id): data | None}.
        sources: dict[tuple[str, str], Optional[dict]] = {}
        for col in (TE_COLLECTION, EXP_COLLECTION):
            for d in (
                db.collection(col)
                .where(filter=FieldFilter("invoice_id", "==", invoice_id))
                .stream(transaction=transaction)
            ):
                sources[(col, d.id)] = d.to_dict() or {}
        for item in items:
            source_id = item.get("source_id", "")
            if not source_id:
                continue  # the named adjustment line — nothing to release
            col = TE_COLLECTION if item.get("type") == "fee" else EXP_COLLECTION
            if (col, source_id) in sources:
                continue
            source_snap = (
                db.collection(col).document(source_id).get(transaction=transaction)
            )
            sources[(col, source_id)] = (
                (source_snap.to_dict() or {}) if source_snap.exists else None
            )

        now = datetime.now(timezone.utc)
        report: dict = {
            "released_time_entry_ids": [],
            "released_expense_ids": [],
            "foreign_source_ids": [],
            "missing_source_ids": [],
        }
        releases = []
        for (col, source_id), data in sources.items():
            if data is None:
                report["missing_source_ids"].append(source_id)
                continue
            points_at = data.get("invoice_id") or ""
            if points_at and points_at != invoice_id:
                report["foreign_source_ids"].append(source_id)
                continue
            # Already released (the race victim: invoiced False, no
            # invoice_id): reported as released, but NOT rewritten. A
            # « harmless » write would still regenerate its etag and stamp
            # updated_via on a row nothing changed — and an edit form open on
            # that entry would then refuse its next save as stale.
            if data.get("invoiced") or points_at:
                releases.append((col, source_id))
            key = (
                "released_time_entry_ids" if col == TE_COLLECTION
                else "released_expense_ids"
            )
            report[key].append(source_id)

        # ── writes ──
        for col, source_id in releases:
            transaction.update(db.collection(col).document(source_id), {
                "invoiced": False,
                "invoice_id": None,
                **provenance.update_fields(now),
            })
        stamp = {
            "status": "annulée",
            "void_reason": reason,
            "voided_at": now,
            **provenance.update_fields(now),
        }
        transaction.update(invoice_ref, stamp)
        for key in report:
            report[key].sort()
        report["invoice"] = {**invoice, **stamp}
        return report

    try:
        report = _apply(db.transaction())
    except concurrency.StaleWrite:
        log_invoice_event("invoice_refused", invoice_id, outcome="refused",
                          operation="void", reason="stale_etag")
        return None, [concurrency.STALE_ETAG_ERROR]
    except _VoidRefused as refusal:
        log_invoice_event("invoice_refused", invoice_id, outcome="refused",
                          operation="void", reason=refusal.reason)
        return None, [str(refusal)]
    except Exception:
        log_unexpected("invoice void failed")
        return None, ["Erreur lors de l'annulation. Veuillez réessayer."]
    provenance.note_commit(COLLECTION, invoice_id)
    log_invoice_event(
        "invoice_voided", invoice_id,
        dossier_id=report["invoice"].get("dossier_id", ""),
        released_count=(
            len(report["released_time_entry_ids"])
            + len(report["released_expense_ids"])
        ),
        foreign_count=len(report["foreign_source_ids"]),
        missing_count=len(report["missing_source_ids"]),
    )
    return report, []


def void_invoice(
    invoice_id: str, *, expected_etag: Optional[str] = None,
) -> tuple[bool, str]:
    """:func:`void_invoice_report` for a caller that needs only the verdict.

    Returns ``(success, error)`` — the historical signature, kept for its
    callers.
    """
    report, errors = void_invoice_report(invoice_id, expected_etag=expected_etag)
    if errors:
        return False, errors[0]
    return True, ""


# ── Write-offs: the derived readers and the two doors (lot 3a) ───────────

#: How a « payée » reads once a radiation settles it — no new status (the
#: default the lawyer adopted on 2026-11-11): :func:`settlement_label`.
SETTLED_BY_WRITE_OFF_LABEL = "Soldée par radiation"
PARTLY_PAID_WRITTEN_OFF_LABEL = "Payée en partie — solde radié"


def settlement_label(invoice: dict) -> str:
    """How *invoice*'s status reads, a « payée » saying HOW it was settled:
    « Payée »; « Soldée par radiation » when a radiation stands and nothing
    is paid; « Payée en partie — solde radié » when both. Any other status
    reads as its own label (:data:`STATUS_LABELS`). PURE; raises on a
    malformed ``write_offs`` (:class:`WriteOffsUnreadable`) or an unreadable
    ``amount_paid``."""
    status = invoice.get("status", "")
    if status != "payée":
        return STATUS_LABELS.get(status, status)
    if not standing_write_offs(invoice):
        return STATUS_LABELS["payée"]
    if _payment_recorded(invoice):
        return PARTLY_PAID_WRITTEN_OFF_LABEL
    return SETTLED_BY_WRITE_OFF_LABEL


def write_off_refusal(invoice: dict, *, today: date) -> str:
    """``''`` EXACTLY when *invoice*'s remainder may be written off on
    *today* — when it is overdue (:func:`is_overdue`, the one authority of
    lateness) — otherwise why not, in French. PURE; *today* is the caller's
    Montréal day (``utils.deadlines.today_mtl``), required. The door judges
    it inside its transaction; a page offers « Radier le solde » only when it
    answers ``''``. A stored amount or a ``write_offs`` that cannot be read
    raises, as everywhere they are read."""
    if is_overdue(invoice, today=today):
        return ""
    status = invoice.get("status", "")
    if status == "brouillon":
        return ("Cette facture est encore un brouillon : seul le solde d'une "
                "facture en retard se radie.")
    if status == "annulée":
        return "Cette facture est annulée : il n'y a rien à radier."
    if status == "payée":
        return (f"Cette facture est « {settlement_label(invoice)} » : il n'y "
                "a plus de solde à radier.")
    if status not in _ISSUED_STATUSES:
        return "Seul le solde d'une facture en retard se radie."
    if balance_of(invoice) <= 0:
        return "Cette facture n'a plus de solde dû : il n'y a rien à radier."
    first = first_overdue_day(invoice)
    if first is None:
        return ("Cette facture n'a pas de date d'échéance : elle n'est jamais "
                "en retard, et son solde ne se radie pas.")
    return ("L'échéance de cette facture n'est pas passée : son solde ne se "
            f"radie qu'une fois la facture en retard, à compter du "
            f"{first.isoformat()} (une échéance qui tombe un samedi, un "
            "dimanche ou un jour férié est reportée au jour ouvrable "
            "suivant).")


def write_off_tax_parts(invoice: dict, amount: int) -> tuple[int, int]:
    """``(gst, qst)`` — the TPS and the TVQ inside *amount*, a write-off of
    *invoice* (D3, the lawyer's decision of 2026-11-11): each the invoice's
    own tax (``gst_amount``, ``qst_amount``) in proportion of its ``total``,
    taxes included — ``round_half_up(amount × tax ÷ total)``, once for each,
    in ``Decimal``; ``(0, 0)`` when the total is 0. The ``total``, NEVER
    ``amount_due``: an applied retainer makes ``amount_due < total``, and
    the tax in the remainder is that of what was billed. PURE; raises
    ``ValueError`` on a figure that is not integer cents ≥ 0, or on taxes
    beyond the total — never a guess. (The plan's « For your accountant »,
    point 1: the fraction's fairness on an invoice that mixes taxed fees and
    untaxed disbursements is his to confirm.)"""
    if not _is_cents(amount):
        raise ValueError("amount : des cents entiers, positifs ou nuls")
    figures = {name: invoice.get(name)
               for name in ("total", "gst_amount", "qst_amount")}
    for name, value in figures.items():
        if not _is_cents(value):
            raise ValueError(f"{name} : des cents entiers, positifs ou nuls")
    total = figures["total"]
    if figures["gst_amount"] + figures["qst_amount"] > total:
        raise ValueError("gst_amount et qst_amount dépassent total")
    if total == 0:
        return 0, 0

    def _part(tax: int) -> int:
        return int((Decimal(amount) * Decimal(tax) / Decimal(total))
                   .quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    return _part(figures["gst_amount"]), _part(figures["qst_amount"])


#: What a one-shot repair script never rewrites the payments of: an invoice
#: carrying write-off events (lot 3a) or interest charges (« avis
#: d'intérêts », lot 4a). Such a script writes around the model's doors —
#: each says so in its docstring —, and on such an invoice it would break
#: what those doors keep: written off ≤ due − paid, « payée » ⇔ settled.
REPAIR_REFUSED_KEYS = ("write_offs", "interest_charges")
_REPAIR_REFUSED_WHAT = {
    "write_offs": "une radiation",
    "interest_charges": "un avis d'intérêts",
}


def repair_script_refusal(invoice: dict) -> str:
    """``''`` when a one-shot repair script may rewrite *invoice*'s payment
    fields; otherwise why not, as a French clause the script completes
    (« porte une radiation : … »). PURE. An absent key or an empty list
    carries nothing; any other value — a malformed one included — refuses.
    A CANCELLED radiation counts too: its pair is history the scripts never
    rewrite around."""
    carried = [_REPAIR_REFUSED_WHAT[key] for key in REPAIR_REFUSED_KEYS
               if key in invoice and invoice[key] != []]
    if not carried:
        return ""
    return (f"porte {' et '.join(carried)} : un script de réparation ne "
            "réécrit jamais ses paiements")


class _WriteOffRefused(Exception):
    """Refusal raised inside a write-off door's transaction (aborts, never
    writes). ``reason`` is the machine-stable code the observability event
    carries; the French sentence is ``str(exc)``."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class _WriteOffLanded(Exception):
    """Raised inside a RE-RUN of a write-off door's transaction that finds
    THIS call's event already stored: an earlier attempt landed and its
    answer was lost (the doctrine of :class:`_OwnCommitLandedError` — the
    ``transactional`` decorator re-runs its body after an ``Aborted`` retry
    of a commit that landed). The event id is minted once per call and known
    to no other writer. Judged as a refusal — the invoice already settled,
    its etag moved — the re-run would report « nothing written » over a
    write that stands; the door answers it as written instead, from the
    stored invoice this carries."""

    def __init__(self, invoice: dict) -> None:
        super().__init__("an earlier attempt of the write-off landed")
        self.invoice = invoice


#: The doors' answer when the caller names no version: a write-off is judged
#: on the invoice the lawyer saw, so ``expected_etag`` is REQUIRED — there is
#: no legacy path (reason ``version_requise``).
WRITE_OFF_VERSION_REQUIRED = (
    "La version de la facture est requise : rechargez sa fiche, puis "
    "recommencez. Rien n'a été écrit."
)

#: Each door's answer when its transaction raised and nothing was written —
#: no commit was attempted, or the read-back finds none of this call's
#: event. A commit whose outcome cannot be settled is
#: ``concurrency.WRITE_OUTCOME_UNCERTAIN_ERROR`` instead, never this.
WRITE_OFF_NOT_WRITTEN = "La radiation a échoué : rien n'a été écrit. Veuillez réessayer."
WRITE_OFF_REVERSAL_NOT_WRITTEN = (
    "L'annulation de la radiation a échoué : rien n'a été écrit. Veuillez "
    "réessayer."
)

_DRIFT_ENCAISSEMENTS = (
    "Les encaissements inscrits au registre d'administration pour cette "
    "facture ne totalisent pas le montant payé qu'elle affiche : le registre "
    "et la facture ne concordent plus (contrôle d'intégrité n° 8). Rien n'a "
    "été radié — faites vérifier le compte d'administration avant de radier "
    "le solde."
)
_DRIFT_FIDEICOMMIS = (
    "Un paiement d'honoraires tiré du fidéicommis vise cette facture sans "
    "encaissement qui lui soit lié au registre d'administration : le "
    "fidéicommis, le registre et la facture ne concordent plus. Rien n'a été "
    "radié — faites vérifier les deux registres avant de radier le solde."
)


def _write_off_reason_text(reason: object, *, label: str) -> tuple[str, list[str]]:
    """The reason as it is STORED — its line breaks read as LF (a browser
    posts a textarea's as CRLF: :func:`_draft_text`), stripped — and why it
    is refused rather than altered, ``[]`` if fine. The void's rule, on the
    exact string stored: over :data:`WRITE_OFF_REASON_MAX_LENGTH` characters,
    or carrying a « < … > » run that ``sanitize`` would delete, it is
    REFUSED — never truncated, never stripped. ``''`` is fine."""
    if not isinstance(reason, str):
        return "", [f"{label} doit être un texte."]
    text = reason.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > WRITE_OFF_REASON_MAX_LENGTH:
        return text, [
            f"{label} dépasse {WRITE_OFF_REASON_MAX_LENGTH} caractères "
            f"({len(text)}) : il n'est jamais tronqué — raccourcissez-le."
        ]
    if sanitize(text, max_length=WRITE_OFF_REASON_MAX_LENGTH) != text:
        return text, [
            f"{label} contient un passage entre chevrons (< … >) qui serait "
            "supprimé à l'enregistrement : retirez les chevrons."
        ]
    return text, []


def _log_write_off_refusal(invoice_id: object, operation: str, reason: str) -> None:
    """The ``invoice_refused`` line of a write-off door (ids and the code)."""
    log_invoice_event("invoice_refused", str(invoice_id or ""), outcome="refused",
                      operation=operation, reason=reason)


def _carries_event(invoice: dict, event_id: str) -> bool:
    """Whether *invoice*'s stored array holds an event of id *event_id* —
    read leniently: it only tells this call's own landed write apart."""
    events = invoice.get("write_offs")
    return isinstance(events, list) and any(
        isinstance(e, dict) and e.get("id") == event_id for e in events)


def _read_for_write_off(ref, transaction, event_id: str, expected_etag: str) -> dict:
    """The invoice a write-off door reads FIRST in its transaction: absent,
    refused; carrying this call's own event, an earlier attempt landed
    (:class:`_WriteOffLanded`) — before any guard judges its new state; not
    the version the caller saw, stale. The document id is authoritative for
    ``id``."""
    snap = ref.get(transaction=transaction)
    if not snap.exists:
        raise _WriteOffRefused("Facture introuvable.", "introuvable")
    invoice = {**(snap.to_dict() or {}), "id": snap.id}
    if _carries_event(invoice, event_id):
        raise _WriteOffLanded(invoice)
    if not concurrency.matches(invoice, expected_etag):
        raise concurrency.StaleWrite()
    return invoice


def _refuse_unreadable(invoice: dict) -> None:
    """Refuse by NAME (``montant_illisible``) what :func:`balance_of` cannot
    read — never judge it, never let ``int()`` quote it in a traceback."""
    unreadable = unreadable_balance_field(invoice)
    if unreadable is not None:
        raise _WriteOffRefused(
            unreadable_amount_message(
                unreadable, "le solde ne peut pas être jugé, et rien n'a "
                "été écrit"),
            "montant_illisible")


def _register_rows(invoice_id: str, transaction) -> tuple[list[dict], list[dict]]:
    """The administration and the trust rows that name *invoice_id*, read
    inside *transaction* — :func:`void_invoice_report`'s two reads, single-
    field equalities on the automatic index. The document id is
    authoritative for ``id``."""
    def _rows(collection: str) -> list[dict]:
        return [{**(d.to_dict() or {}), "id": d.id}
                for d in db.collection(collection)
                .where(filter=FieldFilter("invoice_id", "==", invoice_id))
                .stream(transaction=transaction)]
    return _rows(_ADMIN_TRANSACTIONS), _rows(_TRUST_TRANSACTIONS)


def _register_drift(
    invoice: dict, admin_rows: list[dict], trust_rows: list[dict],
) -> Optional[_WriteOffRefused]:
    """Why the registers disagree with what *invoice* records as paid — the
    two drifts a write-off refuses (the judge's point 13), so that the
    remainder written off is exactly what the registers say is owed — or
    ``None``:

    * the standing encaissements (:func:`_standing_admin_encaissement`) do
      not add up to ``amount_paid`` — check 8's drift
      (``encaissements_discordants``; an amount that is not integer cents
      counts as a drift too);
    * a standing trust fee payment (:func:`_standing_trust_fee_payment`)
      names the invoice with no standing encaissement linked to it by
      ``trust_transaction_id`` (``fideicommis_sans_encaissement``)."""
    standing = [r for r in admin_rows if _standing_admin_encaissement(r)]
    amounts = [r.get("amount") for r in standing]
    if (not all(_is_cents(a) for a in amounts)
            or sum(amounts) != int(invoice.get("amount_paid", 0))):
        return _WriteOffRefused(_DRIFT_ENCAISSEMENTS, "encaissements_discordants")
    linked = {str(r.get("trust_transaction_id") or "") for r in standing}
    if any(_standing_trust_fee_payment(r) and r.get("id") not in linked
           for r in trust_rows):
        return _WriteOffRefused(_DRIFT_FIDEICOMMIS, "fideicommis_sans_encaissement")
    return None


def _write_off_day(today: date) -> datetime:
    """*today* — the Montréal day — as a date-only field is stored: midnight
    UTC (Rule 5)."""
    return datetime(today.year, today.month, today.day, tzinfo=timezone.utc)


# Credit-note numbers (lot 3b, module docstring) — « AAAA-NC### », the
# Montréal year of the radiation's date, from the monotonic transactional
# counter ``counters/notes-credit-{year}``: read and written INSIDE
# write_off_remainder's own transaction, as an invoice's number is inside
# create_invoice's (CLAUDE.md « Sequence numbers come from counters/{scope}
# inside the writing transaction, never reused »). A write-off that aborts,
# for any reason, leaves the counter where it was; a cancelled radiation
# keeps its number for ever. No seed, unlike the invoices': no credit note
# was ever numbered before the counter existed.


def _credit_note_counter_ref(year: int):
    """The transactional counter of *year*'s « AAAA-NC » series."""
    return db.collection(COUNTERS_COLLECTION).document(f"notes-credit-{year:04d}")


def _next_credit_note_number(counter_snapshot, year: int) -> tuple[str, int]:
    """``(number, next_seq)`` from *year*'s counter as read INSIDE the
    write-off's transaction — :func:`_next_invoice_number`'s rule, unseeded:
    « 2026-NC001 », « 2026-NC002 », … « 2026-NC1000 » past 999."""
    return _next_invoice_number(counter_snapshot, 0, f"{year:04d}-NC")


def _landed_report(invoice: dict, event_id: str) -> dict:
    """The report of a write whose commit landed — the STORED invoice and
    this call's event in it."""
    event = next(e for e in invoice.get("write_offs") or []
                 if isinstance(e, dict) and e.get("id") == event_id)
    return {"invoice": invoice, "write_off": event}


def _settle_failed_write_off(
    ref, event_id: str, *, attempted: bool, not_written: str,
) -> tuple[Optional[dict], list[str]]:
    """A write-off door's transaction RAISED — settle what happened rather
    than guess (« Honest outcomes »). No commit attempted: nothing was
    written. Otherwise the invoice is read back: THIS call's event found,
    the commit landed and its answer was lost — answered as written;
    absent, nothing was; the read-back failing too, the outcome is
    uncertain, never « nothing written »."""
    if not attempted:
        return None, [not_written]
    try:
        snap = ref.get()
    except Exception:
        return None, [concurrency.WRITE_OUTCOME_UNCERTAIN_ERROR]
    stored = {**(snap.to_dict() or {}), "id": snap.id} if snap.exists else None
    if stored is not None and _carries_event(stored, event_id):
        return _landed_report(stored, event_id), []
    return None, [not_written]


def _log_write_off_committed(event: str, invoice_id: str, report: dict) -> None:
    """The ONE line of a committed write-off door, after its commit: ids,
    the tax flag, the writer's path (the event's own ``created_via``) and the
    invoice's status after — never an amount, never the reason."""
    written, entry = report["invoice"], report["write_off"]
    log_invoice_write_off(
        event, invoice_id,
        write_off_id=str(entry.get("id") or ""),
        reverses_id=str(entry.get("reverses_id") or "") or None,
        tax_claimed=bool(entry.get("tax_claimed")),
        via=str(entry.get("created_via") or ""),
        status=str(written.get("status") or ""),
        dossier_id=str(written.get("dossier_id") or ""),
    )


def write_off_remainder(
    invoice_id: str,
    *,
    expected_etag: str,
    reason: str = "",
    claim_tax: bool = True,
) -> tuple[Optional[dict], list[str]]:
    """Write off the WHOLE remainder of an overdue invoice — « Radier le
    solde » (lot 3a; the lawyer's decisions D2 and D3 and the defaults
    adopted with them: module docstring). Returns ``(report, errors)``; the
    report::

        {"invoice": <the invoice as WRITTEN, its new etag included>,
         "write_off": <the radiation appended, its credit_note_number
                       included>}

    Its one entry point is the invoice sheet's « Radier le solde »
    (``routes/invoices``, lot 3b). The connector never may —
    ``mcp/disclosure``'s « invoice_paid » promise forbids the name.

    Judged before anything is read: *claim_tax* must be a bool (a caller's
    bug raises ``TypeError``); *expected_etag* — the version the lawyer saw —
    is REQUIRED (``version_requise``: no legacy path); *reason* is stored as
    :func:`_write_off_reason_text` reads it, refused rather than altered
    (``motif_invalide``); an id that cannot name an invoice is
    ``introuvable``.

    ONE Firestore transaction, every read before the write:

    * the invoice — this call's own landed event first
      (:class:`_WriteOffLanded`), then its etag (``stale_etag``); a stored
      amount or a ``write_offs`` :func:`balance_of` cannot read is refused
      by name (``montant_illisible``); it must be overdue on ``today_mtl()``,
      read inside the transaction (:func:`write_off_refusal`,
      ``pas_en_retard``);
    * the administration and the trust rows that name it, read as
      :func:`void_invoice_report` reads them, and the two register drifts of
      :func:`_register_drift` refused (``encaissements_discordants``,
      ``fideicommis_sans_encaissement``);
    * LAST, once nothing is left to refuse, the counter of the year's credit
      notes (lot 3b): ``counters/notes-credit-{year}``, the Montréal year of
      today (:func:`_next_credit_note_number`).

    Then ``W = balance_of`` — computed here, never typed —, its TPS and TVQ
    parts when *claim_tax* (:func:`write_off_tax_parts`, on the invoice's
    ``total``; a tax figure it cannot read refuses, ``taxes_illisibles``),
    the radiation dated today (Montréal, at midnight UTC), numbered
    (``credit_note_number``), appended to ``write_offs``, the counter
    written with it, the invoice settled through :func:`_settled_status`
    (« payée »), a fresh etag and the writer's stamps (Rule 7).
    ``amount_paid`` is never touched.

    After the commit, ONE ``invoice_written_off`` line. A transaction that
    raised is settled by :func:`_settle_failed_write_off` — never « nothing
    written » over a write that may stand.
    """
    if not isinstance(claim_tax, bool):
        raise TypeError("claim_tax : un booléen, jamais une autre valeur")
    operation = "write_off"
    text, reason_errors = _write_off_reason_text(
        reason, label="Le motif de la radiation")
    if not isinstance(expected_etag, str):
        _log_write_off_refusal(invoice_id, operation, "version_requise")
        return None, [WRITE_OFF_VERSION_REQUIRED]
    if reason_errors:
        _log_write_off_refusal(invoice_id, operation, "motif_invalide")
        return None, reason_errors
    if not is_addressable_id(invoice_id):
        _log_write_off_refusal(invoice_id, operation, "introuvable")
        return None, ["Facture introuvable."]

    ref = db.collection(COLLECTION).document(invoice_id)
    event_id = str(uuid.uuid4())
    attempted = {"commit": False}

    @firestore.transactional
    def _apply(transaction) -> dict:
        invoice = _read_for_write_off(ref, transaction, event_id, expected_etag)
        admin_rows, trust_rows = _register_rows(invoice_id, transaction)
        # ── every read is done: judge ──
        today = today_mtl()
        _refuse_unreadable(invoice)
        refusal = write_off_refusal(invoice, today=today)
        if refusal:
            raise _WriteOffRefused(refusal, "pas_en_retard")
        drift = _register_drift(invoice, admin_rows, trust_rows)
        if drift is not None:
            raise drift
        remainder = balance_of(invoice)
        gst = qst = 0
        if claim_tax:
            try:
                gst, qst = write_off_tax_parts(invoice, remainder)
            except ValueError:
                raise _WriteOffRefused(
                    "Le total, la TPS ou la TVQ enregistrés sur cette facture "
                    "sont illisibles : la part de taxes de la radiation ne "
                    "peut pas être calculée, et rien n'a été écrit. La "
                    "facture doit d'abord être réparée.",
                    "taxes_illisibles") from None
        # The last READ (lot 3b): the year's credit-note counter, once
        # nothing is left to refuse, and written below WITH the invoice.
        counter_ref = _credit_note_counter_ref(today.year)
        number, next_seq = _next_credit_note_number(
            counter_ref.get(transaction=transaction), today.year)
        now = datetime.now(timezone.utc)
        event = {
            "id": event_id,
            "kind": _RADIATION,
            "date": _write_off_day(today),
            "amount": remainder,
            "gst_amount": gst,
            "qst_amount": qst,
            "tax_claimed": claim_tax,
            "reason": text,
            "reverses_id": "",
            "credit_note_number": number,
            "created_at": now,
            "created_via": provenance.current_via(),
        }
        events = list(invoice.get("write_offs") or []) + [event]
        write_off_events({"write_offs": events})     # well-formed, proved
        settled = _settled_status(
            invoice, paid=int(invoice.get("amount_paid", 0)),
            written_off=written_off_of(invoice) + remainder,
            carries_write_off=True)
        updates: dict = {"write_offs": events, **provenance.update_fields(now)}
        if settled is not None:
            updates["status"] = settled
        transaction.set(counter_ref, {"seq": next_seq, "updated_at": now})
        transaction.update(ref, updates)
        attempted["commit"] = True
        return {"invoice": {**invoice, **updates}, "write_off": event}

    try:
        report = _apply(db.transaction())
    except concurrency.StaleWrite:
        _log_write_off_refusal(invoice_id, operation, "stale_etag")
        return None, [concurrency.STALE_ETAG_ERROR]
    except _WriteOffRefused as refusal:
        _log_write_off_refusal(invoice_id, operation, refusal.reason)
        return None, [str(refusal)]
    except _WriteOffLanded as landed:
        report = _landed_report(landed.invoice, event_id)
    except Exception as exc:
        # The class only, no traceback: nothing raised here may quote a
        # stored value into the log (CLAUDE.md « Do not log PII »).
        log_unexpected("invoice write-off failed", exc_info=False,
                       invoice_id=invoice_id, error_type=type(exc).__name__,
                       commit_attempted=attempted["commit"])
        report, errors = _settle_failed_write_off(
            ref, event_id, attempted=attempted["commit"],
            not_written=WRITE_OFF_NOT_WRITTEN)
        if errors:
            return None, errors
    provenance.note_commit(COLLECTION, invoice_id)
    _log_write_off_committed("invoice_written_off", invoice_id, report)
    return report, []


def _standing_radiation(invoice: dict, event_id: object) -> dict:
    """The radiation *event_id* names on *invoice*, if it STANDS — refused
    otherwise: already cancelled (``radiation_deja_annulee``, so a second
    cancellation is refused), or no radiation of this invoice
    (``radiation_introuvable`` — a contre_passation is never cancelled)."""
    for event in standing_write_offs(invoice):
        if event["id"] == event_id:
            return event
    if any(e["kind"] == _RADIATION and e["id"] == event_id
           for e in write_off_events(invoice)):
        raise _WriteOffRefused(
            "Cette radiation est déjà annulée : une radiation ne s'annule "
            "qu'une fois. Rien n'a été écrit.", "radiation_deja_annulee")
    raise _WriteOffRefused(
        "Cette radiation est introuvable sur la facture (seule une radiation "
        "s'annule, jamais une annulation). Rien n'a été écrit.",
        "radiation_introuvable")


def reverse_write_off(
    invoice_id: str,
    event_id: str,
    *,
    expected_etag: str,
    reason: str = "",
) -> tuple[Optional[dict], list[str]]:
    """Cancel one standing radiation — « Annuler la radiation » (lot 3a) —
    by a ``contre_passation`` DATED TODAY that repeats its four figures
    (:data:`_WRITE_OFF_FIGURES`); the radiation itself is never edited.
    The contre_passation takes no credit-note number, and the radiation's
    credit note keeps its own for ever — « annulée », derived (lot 3b).
    The way back when the client pays after all: cancel the radiation,
    record the payment, then write off any new remainder. Returns
    ``(report, errors)`` — the report of :func:`write_off_remainder`, its
    ``write_off`` the contre_passation appended.

    The same pre-checks as :func:`write_off_remainder` (``version_requise``,
    ``motif_invalide``, ``introuvable``) and the same ONE transaction: the
    invoice read first (this call's own landed event, then ``stale_etag``,
    then ``montant_illisible``); the radiation must STAND
    (:func:`_standing_radiation`: ``radiation_deja_annulee``,
    ``radiation_introuvable``). The invoice is settled again through
    :func:`_settled_status` — a « payée » that falls short reopens
    « en_retard », the due date having passed when it was written off —,
    with a fresh etag and the writer's stamps.

    After the commit, ONE ``invoice_write_off_reversed`` line (ids, the
    radiation it cancels, the tax flag, the writer's path, the status after
    — never an amount, never the reason). A transaction that raised is
    settled as :func:`write_off_remainder`'s is.
    """
    operation = "write_off_reversal"
    text, reason_errors = _write_off_reason_text(
        reason, label="Le motif de l'annulation de la radiation")
    if not isinstance(expected_etag, str):
        _log_write_off_refusal(invoice_id, operation, "version_requise")
        return None, [WRITE_OFF_VERSION_REQUIRED]
    if reason_errors:
        _log_write_off_refusal(invoice_id, operation, "motif_invalide")
        return None, reason_errors
    if not is_addressable_id(invoice_id):
        _log_write_off_refusal(invoice_id, operation, "introuvable")
        return None, ["Facture introuvable."]

    ref = db.collection(COLLECTION).document(invoice_id)
    reversal_id = str(uuid.uuid4())
    attempted = {"commit": False}

    @firestore.transactional
    def _apply(transaction) -> dict:
        invoice = _read_for_write_off(ref, transaction, reversal_id, expected_etag)
        _refuse_unreadable(invoice)
        radiation = _standing_radiation(invoice, event_id)
        today = today_mtl()
        now = datetime.now(timezone.utc)
        event = {
            "id": reversal_id,
            "kind": _CONTRE_PASSATION,
            "date": _write_off_day(today),
            **{name: radiation[name] for name in _WRITE_OFF_FIGURES},
            "reason": text,
            "reverses_id": radiation["id"],
            "created_at": now,
            "created_via": provenance.current_via(),
        }
        events = list(invoice.get("write_offs") or []) + [event]
        write_off_events({"write_offs": events})     # well-formed, proved
        settled = _settled_status(
            invoice, paid=int(invoice.get("amount_paid", 0)),
            written_off=written_off_of(invoice) - radiation["amount"],
            carries_write_off=True)
        updates: dict = {"write_offs": events, **provenance.update_fields(now)}
        if settled is not None:
            updates["status"] = settled
        transaction.update(ref, updates)
        attempted["commit"] = True
        return {"invoice": {**invoice, **updates}, "write_off": event}

    try:
        report = _apply(db.transaction())
    except concurrency.StaleWrite:
        _log_write_off_refusal(invoice_id, operation, "stale_etag")
        return None, [concurrency.STALE_ETAG_ERROR]
    except _WriteOffRefused as refusal:
        _log_write_off_refusal(invoice_id, operation, refusal.reason)
        return None, [str(refusal)]
    except _WriteOffLanded as landed:
        report = _landed_report(landed.invoice, reversal_id)
    except Exception as exc:
        log_unexpected("invoice write-off reversal failed", exc_info=False,
                       invoice_id=invoice_id, error_type=type(exc).__name__,
                       commit_attempted=attempted["commit"])
        report, errors = _settle_failed_write_off(
            ref, reversal_id, attempted=attempted["commit"],
            not_written=WRITE_OFF_REVERSAL_NOT_WRITTEN)
        if errors:
            return None, errors
    provenance.note_commit(COLLECTION, invoice_id)
    _log_write_off_committed("invoice_write_off_reversed", invoice_id, report)
    return report, []


# ── Draft corrections ────────────────────────────────────────────────────

#: The fields a brouillon may correct — and nothing else. No money figure,
#: no line item, no client, no number: those are what the invoice IS, and
#: changing one means voiding it and issuing a new one. (The retired
#: ``payment_terms`` left on 2026-11-11 — module docstring.)
DRAFT_FIELDS = ("notes", "due_date")
#: The connector's import cap (``mcp/handlers._import_invoice_impl``), so a
#: brouillon correctable on one surface is correctable on the other.
DRAFT_NOTES_MAX_LENGTH = 1500


def draft_edit_refusal(invoice: dict) -> str:
    """'' when *invoice* is a brouillon, else why it cannot be corrected.

    The one sentence, shown by the web form before it renders and by
    :func:`update_invoice_draft` inside its transaction.
    """
    status = invoice.get("status", "")
    if status == "brouillon":
        return ""
    if status == "annulée":
        return "Cette facture est annulée : elle ne se modifie plus."
    label = STATUS_LABELS.get(status, status)
    if status == "payée":
        return (
            f"Seul un brouillon se modifie : cette facture est « {label} »."
        )
    return (
        f"Seul un brouillon se modifie : cette facture est « {label} ». Une "
        "facture émise ne se corrige qu'en l'annulant — possible tant "
        "qu'aucun paiement n'y est inscrit —, puis en en émettant une "
        "nouvelle."
    )


class _DraftInvalid(Exception):
    """A changed value the draft refuses (raised in the transaction)."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _draft_text_errors(text: str, *, label: str, cap: int) -> list[str]:
    """Why a CHANGED draft text is refused rather than altered — [] if fine.

    ``sanitize`` truncates and deletes angle-bracket runs in silence; on a
    document the client receives, a note that loses « < b et b > » with a
    success message is worse than a refusal. So the length is checked first,
    and the value must come out of ``sanitize`` unchanged. An empty text is
    never refused: ``''`` clears the notes.
    """
    if len(text) > cap:
        return [
            f"{label} dépassent {cap} caractères ({len(text)}) : elles ne "
            "sont jamais tronquées — raccourcissez-les."
        ]
    if sanitize(text, max_length=cap) != text:
        return [
            f"{label} contiennent un passage entre chevrons (< … >) qui serait "
            "supprimé à l'enregistrement : retirez les chevrons."
        ]
    return []


def _parse_draft_due_date(value: object) -> tuple[Optional[datetime], list[str]]:
    """A due date as the date-only convention stores it: midnight UTC."""
    if isinstance(value, datetime):
        day = _calendar_day(value)
    elif isinstance(value, date):
        day = value
    elif isinstance(value, str) and value.strip():
        try:
            day = datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None, ["La date d'échéance est invalide (AAAA-MM-JJ attendu)."]
    else:
        return None, ["La date d'échéance est requise."]
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc), []


# {field: (French label, cap)} — the one draft text.
_DRAFT_TEXT_RULES = {
    "notes": ("Les notes", DRAFT_NOTES_MAX_LENGTH),
}


def _draft_text(value: object) -> str:
    """A draft text as it is COMPARED and written: LF line breaks, stripped.

    A browser submits every line break of a ``<textarea>`` as CRLF (HTML
    form submission normalizes them so), while the connector's import and
    every script store LF. Compared raw, a multi-line note read back
    unchanged from the page differed from the stored one on EVERY save:
    it was rewritten, reported among the changed fields — and, being
    « changed », re-judged against the cap with each break counted twice,
    so a 1 499-character imported note made an unrelated due-date
    correction REFUSED. The same text under two line-ending conventions is
    one text. ``None`` (a legacy key never set) reads as empty.
    """
    text = "" if value is None else str(value)
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def update_invoice_draft(
    invoice_id: str,
    changes: dict,
    *,
    expected_etag: Optional[str],
    refresh_billing_address: bool = False,
) -> tuple[Optional[dict], list[str], list[str]]:
    """Correct a BROUILLON — ``(invoice, errors, changed_fields)``.

    There was no way to fix a draft at all: a typo in its notes, a wrong due
    date, a client who moved before the invoice left — each could only be
    « fixed » by voiding the draft, which retires its number for ever.

    *changes* holds any of :data:`DRAFT_FIELDS`, by PRESENCE (an absent key
    leaves the field alone):

    * ``notes`` — at most :data:`DRAFT_NOTES_MAX_LENGTH` characters; ``''``
      clears them;
    * ``due_date`` — a ``datetime``/``date`` or ``'YYYY-MM-DD'``, never
      before the invoice date.

    Text is stripped and its line breaks read as LF (a browser posts a
    textarea's as CRLF — :func:`_draft_text`); a CHANGED value that is
    over-long, or that ``sanitize`` would alter, is REFUSED — never
    truncated or stripped. A value EQUAL to the stored one (under that one
    line-ending convention) is not a change and is not re-judged: the web
    form resubmits every field, and a brouillon written before these caps
    (web notes went to 2 000 characters) must stay correctable in its other
    fields. Any other key is refused by name — the retired ``payment_terms``
    among them (module docstring). With
    *refresh_billing_address*, the frozen billing address is re-snapshotted
    from the invoice's client as it is on file NOW (``billing_address_from``,
    the one builder) — read inside the transaction; a client that does not
    resolve, or a read that fails, refuses: an address is never blanked.

    ONE Firestore transaction, every read before the write:

    1. the invoice (missing → refused); it must still be a brouillon;
    2. the client, when refreshing;
    3. the fields that actually CHANGE are computed, and only they are
       judged (the notes; the due date against the invoice date). When
       none changes, nothing is written and the stored invoice is returned
       with ``changed_fields == []``. This comes BEFORE the etag check on
       purpose: a replay of a save that already succeeded (a double
       submit, a retried call) is a no-op, not a false conflict;
    4. *expected_etag* — the version the caller read — must be the stored
       one (``None`` asserts nothing: a page rendered before its form
       carried an etag). Required as a KEYWORD: every caller states what it
       read, even when that is « nothing »;
    5. a PARTIAL ``update()`` of the changed keys plus the provenance stamp.
       Never the merged full-document ``set()``: no money figure, no line
       item, no status is even reachable from here.

    The third member of the return deviates from the house ``(doc,
    errors)`` convention on purpose (the ``set_*_phase`` precedent): a
    caller must tell « applied » from « already so » without re-deriving it.
    """
    changes = dict(changes or {})
    unknown = sorted(k for k in changes if k not in DRAFT_FIELDS)
    if unknown:
        return None, [
            "Champ non modifiable sur un brouillon : " + ", ".join(unknown)
            + ". Seules les notes, la date d'échéance et l'adresse de "
            "facturation se corrigent."
        ], []
    if not changes and not refresh_billing_address:
        return None, ["Aucune modification demandée."], []

    wanted: dict = {}
    errors: list[str] = []
    for key, (label, _cap) in _DRAFT_TEXT_RULES.items():
        if key not in changes:
            continue
        if not isinstance(changes[key], str):
            errors.append(f"{label} : une chaîne de caractères est attendue.")
        else:
            wanted[key] = _draft_text(changes[key])
    if "due_date" in changes:
        due, errs = _parse_draft_due_date(changes["due_date"])
        errors += errs
        wanted["due_date"] = due
    if errors:
        log_invoice_event("invoice_refused", invoice_id, outcome="refused",
                          operation="draft", reason="validation")
        return None, errors, []

    from models import partie as partie_model

    ref = db.collection(COLLECTION).document(invoice_id)

    @firestore.transactional
    def _apply(transaction) -> tuple[dict, list[str]]:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise _DraftRefused("Facture introuvable.", "introuvable")
        invoice = snap.to_dict() or {}
        refusal = draft_edit_refusal(invoice)
        if refusal:
            raise _DraftRefused(refusal, "pas_un_brouillon")

        target = dict(wanted)
        if refresh_billing_address:
            client_id = str(invoice.get("client_id") or "").strip()
            if not client_id:
                raise _DraftRefused(
                    "Cette facture n'a pas de client : aucune adresse de "
                    "facturation à rafraîchir.",
                    "aucun_client",
                )
            client_snap = (
                db.collection(partie_model.COLLECTION).document(client_id)
                .get(transaction=transaction)
            )
            if not client_snap.exists:
                raise _DraftRefused(
                    "Le client de la facture est introuvable : l'adresse de "
                    "facturation ne peut pas être rafraîchie. Rien n'a été "
                    "enregistré.",
                    "client_introuvable",
                )
            target["billing_address"] = billing_address_from(
                client_snap.to_dict() or {}
            )

        # A text is compared under ONE line-ending convention (_draft_text):
        # the page's CRLF and the store's LF are the same text, never a
        # change — otherwise every multi-line note would be rewritten and
        # re-judged on a save that did not touch it.
        changed = sorted(
            k for k, v in target.items()
            if (_draft_text(invoice.get(k)) if k in _DRAFT_TEXT_RULES
                else invoice.get(k)) != v
        )
        invalid: list[str] = []
        for key in changed:
            if key in _DRAFT_TEXT_RULES:
                label, cap = _DRAFT_TEXT_RULES[key]
                invalid += _draft_text_errors(target[key], label=label, cap=cap)
        if "due_date" in changed:
            invoice_day = _calendar_day(invoice.get("date"))
            due_day = _calendar_day(target["due_date"])
            if invoice_day and due_day < invoice_day:
                invalid.append(
                    f"La date d'échéance ({due_day.isoformat()}) précède la "
                    f"date de la facture ({invoice_day.isoformat()})."
                )
        if invalid:
            raise _DraftInvalid(invalid)
        if not changed:
            return invoice, []
        if not concurrency.matches(invoice, expected_etag):
            raise concurrency.StaleWrite()
        updates = {k: target[k] for k in changed}
        updates.update(provenance.update_fields(datetime.now(timezone.utc)))
        transaction.update(ref, updates)
        return {**invoice, **updates}, changed

    try:
        doc, changed = _apply(db.transaction())
    except concurrency.StaleWrite:
        log_invoice_event("invoice_refused", invoice_id, outcome="refused",
                          operation="draft", reason="stale_etag")
        return None, [concurrency.STALE_ETAG_ERROR], []
    except _DraftInvalid as invalid:
        log_invoice_event("invoice_refused", invoice_id, outcome="refused",
                          operation="draft", reason="validation")
        return None, list(invalid.errors), []
    except _DraftRefused as refusal:
        log_invoice_event("invoice_refused", invoice_id, outcome="refused",
                          operation="draft", reason=refusal.reason)
        return None, [str(refusal)], []
    except Exception:
        log_unexpected("invoice draft update failed")
        return None, [
            "Erreur lors de l'enregistrement du brouillon. Rien n'a été "
            "enregistré : réessayez."
        ], []
    if changed:
        provenance.note_commit(COLLECTION, invoice_id)
        log_invoice_event(
            "invoice_draft_updated", invoice_id,
            fields_changed=changed,
            billing_refreshed="billing_address" in changed,
        )
    return doc, [], changed


def delete_invoice(invoice_id: str) -> tuple[bool, str]:
    """Delete a cancelled invoice and its line items. Returns (success, error)."""
    from models.time_entry import COLLECTION as TE_COLLECTION
    from models.expense import COLLECTION as EXP_COLLECTION

    invoice = get_invoice(invoice_id)
    if not invoice:
        return False, "Facture introuvable."

    if invoice.get("status") != "annulée":
        return False, "Seule une facture annulée peut être supprimée."

    # Guard: refuse deletion while any source still references this invoice.
    # void_invoice should have released them; this protects the stranded case.
    # Invoice number reuse is prevented by the monotonic counter
    # (counters/invoices-{year}), so hard deletion never frees a number.
    try:
        stranded = list(
            db.collection(TE_COLLECTION)
            .where(filter=FieldFilter("invoice_id", "==", invoice_id))
            .limit(1)
            .stream()
        ) or list(
            db.collection(EXP_COLLECTION)
            .where(filter=FieldFilter("invoice_id", "==", invoice_id))
            .limit(1)
            .stream()
        )
        if stranded:
            return False, (
                "Des entrées de temps ou des dépenses référencent encore cette "
                "facture. Elles doivent être libérées avant la suppression."
            )
    except Exception as exc:
        logger.error(
            "delete_invoice: reference check failed for %s: %s",
            sanitize_log_value(invoice_id), exc,
        )
        return False, "Erreur lors de la vérification des références. Veuillez réessayer."

    try:
        # Delete all line items in the subcollection
        items_ref = (
            db.collection(COLLECTION)
            .document(invoice_id)
            .collection(LINE_ITEMS_SUB)
            .stream()
        )
        for item_doc in items_ref:
            item_doc.reference.delete()

        # Delete the invoice document
        db.collection(COLLECTION).document(invoice_id).delete()
        return True, ""
    except Exception:
        log_unexpected("invoice delete failed")
        return False, "Erreur lors de la suppression. Veuillez réessayer."


# Shared implementation lives in models/__init__.py; aliased so this module's
# helpers (and their tests) keep a stable local name.
_aggregation_values = aggregation_values


def get_outstanding_totals_strict() -> dict[str, int]:
    """The live balance (cents) still owed on the issued invoices, PER issued
    status: ``{status: Σ balance_of}`` for each status of
    ``_ISSUED_STATUSES``, in that order — the one authority of the
    dashboard's « Factures impayées » tile (its total, and each status' own
    figure on its link to that status' list) and of the connector's
    ``get_agenda`` total, through :func:`get_outstanding_total`, their sum.
    The connector's ``get_billing_snapshot`` does NOT call it: it sums
    ``balance_of`` over its own read of the same pair, so that its total
    matches the rows it lists (``mcp/handlers.get_billing_snapshot``), and
    ``tests/test_dashboard_invoice_tile.py`` pins the two figures equal.

    A PYTHON sum over ONE strict read of the invoices
    (:func:`list_invoices_strict`, outside any transaction), never a
    Firestore aggregation, by design:

    * ``amount_due`` is FROZEN at issuance and stays whole on a settled
      invoice: summing it overstated the receivables by everything already
      received — invisible as long as no payment was recorded;
    * an aggregation cannot subtract two fields. It would take two, hence an
      index ``(status, amount_due, amount_paid)`` that does not exist, and
      whose absence fails SILENTLY: the exception swallowed, 0 answered,
      which reads as « nothing is owed » (the incident of June 2026);
    * one STRICT read for every status: one read per status through
      :func:`list_invoices`, which swallows a failure into ``[]``, could fail
      apart and show one status' real figure beside a 0 that only meant
      « unread », with nothing logged.

    The repository already sums in Python on this collection —
    ``routes/admin_ledger._factures_impayees``,
    ``routes/trust._factures_emises``, ``routes/invoices._journal_rows`` —
    and ``models/trust`` made it its doctrine. The volume counts in dozens;
    the read's cap (``models/full_reads``) sits far above it.

    STRICT: a failed or truncated read, or a corrupt stored amount, RAISES —
    never a partial figure, and never a 0 that would read « nothing is owed »
    (« A failed read never passes for absence »). The dashboard's tile reads
    this door and says « indisponible » (the lawyer's choice of 2026-11-11,
    the financial statements' posture); :func:`get_outstanding_totals` is
    its fail-open twin.
    """
    invoices = list_invoices_strict(None)
    return {
        status: sum(balance_of(inv) for inv in invoices if inv.get("status") == status)
        for status in _ISSUED_STATUSES
    }


def get_outstanding_totals() -> dict[str, int]:
    """:func:`get_outstanding_totals_strict`, failing OPEN — every status at
    0, the failure logged with its exception's type: the connector's
    ``get_agenda`` total, through :func:`get_outstanding_total` (display
    only, never a decision). The dashboard's tile reads the strict door
    instead and says « indisponible »."""
    try:
        return get_outstanding_totals_strict()
    except Exception as exc:
        # The exception's type only: its text may quote a stored value.
        logger.warning("get_outstanding_totals: failed: %s", type(exc).__name__)
        return dict.fromkeys(_ISSUED_STATUSES, 0)


def get_outstanding_total() -> int:
    """The live balance (cents) still owed on the issued invoices: the sum of
    :func:`get_outstanding_totals` — the figure of the connector's
    ``get_agenda``, and the dashboard tile's total. Fails open with it, to
    0."""
    return sum(get_outstanding_totals().values())


def get_invoice_summary(dossier_id: str, invoices: Optional[list[dict]] = None) -> dict:
    """Return invoice summary for a dossier — the « Facturation » tab's
    figures and the connector's ``get_dossier`` / ``get_billing_snapshot``
    dossier summaries.

    Every invoice but the annulée ones counts (brouillons included), on its
    ``total``; a « payée » one counts as paid (the hand-set ones too: that
    status is the lawyer's statement). Since lot 3a, what stands written off
    (:func:`written_off_of`) is ``total_written_off``, and a « payée »
    invoice counts ``total − written_off`` as paid: a remainder written off
    was never received, and is no longer outstanding either —
    ``total_outstanding`` is what was invoiced less what was paid and
    written off. A malformed ``write_offs`` raises, as the balance does."""
    if invoices is None:
        invoices = list_invoices(dossier_id=dossier_id)
    total_invoiced = 0
    total_paid = 0
    total_written_off = 0
    count = 0

    for inv in invoices:
        if inv.get("status") == "annulée":
            continue
        count += 1
        written_off = written_off_of(inv)
        total_invoiced += inv.get("total", 0)
        total_written_off += written_off
        if inv.get("status") == "payée":
            total_paid += inv.get("total", 0) - written_off

    return {
        "count": count,
        "total_invoiced": total_invoiced,
        "total_paid": total_paid,
        "total_written_off": total_written_off,
        "total_outstanding": total_invoiced - total_paid - total_written_off,
    }
