"""The firm's financial statements — lot B (the lawyer's decisions of 2026-11-07).

PURE: no Firestore, no Flask. ``services/financial_statements`` reads one
consistent snapshot (one read-only transaction) and hands it here; this
module is the ONE authority for every figure, label and identity of the
statements page and of its PDF (``present`` builds the labelled sections
both render).

The rules (the plan of 2026-11-07, « The rules (Lot B) »):

* **Billed basis.** Revenue is the invoices whose status is in
  ``invoice.REVENUE_STATUSES`` (envoyée, en_retard, payée), in the period of
  their invoice date, before tax: ``subtotal_fees`` (honoraires) and
  ``subtotal_expenses`` (débours refacturés). A brouillon is not billed; an
  annulée invoice counts in NO period, whenever it was voided — the
  statements are « selon l'état actuel des inscriptions ».
* **Costs** are the administration dépenses, net of TPS/TVQ, classed by
  ``admin_ledger.cost_class`` (dossier ⇒ direct; ``VARIABLE_COST_CATEGORIES``
  ⇒ variable; the rest fixed); a supplier's refund
  (``admin_ledger.REFUND_KINDS``, 2026-11-08) lowers the class and category
  of the cost it refunds, and gives its CTI/RTI back. Each class prints its
  categories in the ledger's order, the members of a group
  (``admin_ledger.EXPENSE_CATEGORY_GROUPS``) beneath their heading, and no
  category line that sums to zero in the period — a heading prints whenever
  one of its members does, even at zero (2026-11-08). « Autres produits »
  are the « autre recette » entries carrying no trust link, printed as ONE
  total (the lawyer's decision of 2026-11-08); one that may stand for a fee
  payment drawn from trust (``fee_payment.manual_recette_candidates``) is
  listed among the points, never moved.
* **Clients' advances received here** (``admin_ledger.ADVANCE_KINDS``,
  2026-11-08) are no revenue: until a virement interne marked « avances »
  moves them to trust they are owed to the clients — a liability, «
  Avances de clients à verser au fidéicommis » — and neither they nor that
  transfer move the net assets.
* **TPS/TVQ is never revenue nor expense**: the tax of billed invoices is a
  liability, the CTI/RTI of the dépenses reduce it, a remittance pays it, a
  refund comes back on it.
* **Corrections** count against their original's kind and links, sign
  inverted, on their own day (``admin_ledger.resolve_corrections`` over the
  whole ledger, before any period slicing).
* **Trust-linked « autre recette »** — a fee payment drawn from trust before
  the encaissement path existed, or citing an invoice outside Athéna — is
  ATTRIBUTED to an Athéna invoice when its trust entry names one: by
  ``invoice_id``, or by an ``invoice_external_ref`` whose key
  (``invoice.normalize_reference``) matches exactly ONE invoice of the same
  dossier, by number or ``legacy_ref`` (« rattachement présumé »); it covers
  that invoice's provision (``retainer_applied``) first. Otherwise it is no
  revenue and is listed (« facture absente d'Athéna »); a unique match in
  ANOTHER dossier is listed apart, never attributed.
* **Hand-set « payée »**: an invoice whose status is payée while its
  register payments fall short counts the shortfall as settled, and is
  listed.
* **Days.** A stored date-only value counts on its UTC calendar day; an
  instant (``voided_at``, ``created_at``, now) on its Montréal day
  (Architecture Rule 5). A period never starts before ``BOOKS_START``;
  nothing is carried in: every balance is computed from the whole history.

The equity rollforward closes BY CONSTRUCTION: every row's effect on the
net assets is either named (net income, the lawyer's apports and
prélèvements, internal transfers, receipts for invoices absent from Athéna,
balances presumed settled, provisions applied and received) or measured as
a defect (a direction that contradicts the kind, an inexact tax split, a
card payment whose legs fall in different periods, an invoice whose amount
due contradicts its totals) — so « écart non expliqué » is zero, and a
non-zero one is a bug of this module, which a seeded property test hunts.

Unbilled work and the trust funds are memos, never in a total. The
« points à vérifier » count every data condition a figure rests on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from models import admin_ledger as al
from models import fee_payment
from models import invoice as invoice_model
from models import trust
from tz import to_mtl
from utils import periods
from utils.format_fr import format_date_fr

#: The day the books start (the lawyer's decision, 2026-11-07): no period
#: may start earlier, and no opening balance is carried in.
BOOKS_START = date(2025, 1, 1)

#: The collections a snapshot holds, in reading order, with their French
#: names for an « indisponible » sentence.
COLLECTIONS = (
    "admin_accounts", "admin_transactions", "admin_reconciliations",
    "invoices", "trust_accounts", "trust_transactions",
    "time_entries", "expenses",
)
COLLECTION_LABELS = {
    "admin_accounts": "comptes d'administration",
    "admin_transactions": "registre d'administration",
    "admin_reconciliations": "conciliations d'administration",
    "invoices": "factures",
    "trust_accounts": "comptes en fidéicommis",
    "trust_transactions": "registre du fidéicommis",
    "time_entries": "heures",
    "expenses": "débours",
}
#: The sections, in display order, with their titles.
SECTION_TITLES = {
    "resultats": "État des résultats",
    "flux": "Flux de trésorerie — comptes d'opérations",
    "bilan": "Bilan",
    "avoir": "Rapprochement de l'avoir net",
    "taxes": "TPS/TVQ de la période",
    "redressements": "Redressements de périodes antérieures",
    "fideicommis": "Note — fonds en fidéicommis (hors bilan)",
    "travaux": "Note — travaux en cours (hors bilan)",
}
#: The three statements the PDF may not go without.
REQUIRED_FOR_PDF = ("resultats", "flux", "bilan")

#: The cash-flow lines, in display order, with their labels — every value
#: of ``admin_ledger.CASH_FLOW_LINES`` plus the trust-linked receipts' own
#: line (pinned by a test).
CASH_FLOW_LINE_ORDER = (
    "encaissements_factures", "encaissements_factures_absentes",
    "autres_recettes", "depenses", "remboursements_depenses", "paiements_carte",
    "remises_tps_tvq", "remboursements_tps_tvq",
    "apports", "prelevements", "virements_entrants", "virements_sortants",
    "avances_recues", "avances_versees",
)
CASH_FLOW_LINE_LABELS = {
    "encaissements_factures": "Encaissements de factures",
    "encaissements_factures_absentes": "Virements du fidéicommis non rattachés à une facture",
    "autres_recettes": "Autres recettes",
    "depenses": "Dépenses payées (taxes comprises)",
    "remboursements_depenses": "Remboursements de dépenses reçus (taxes comprises)",
    "paiements_carte": "Paiements de la carte de crédit",
    "remises_tps_tvq": "Remises de TPS/TVQ",
    "remboursements_tps_tvq": "Remboursements de TPS/TVQ",
    "apports": "Apports de l'avocat",
    "prelevements": "Prélèvements de l'avocat",
    "virements_entrants": "Virements internes reçus",
    "virements_sortants": "Virements internes versés",
    "avances_recues": "Avances d'honoraires de clients reçues",
    "avances_versees": "Avances versées au fidéicommis",
}

#: Categories that are normally a dossier's cost: one filed without its
#: dossier lands in the fixed costs, and is listed.
_DOSSIER_CATEGORIES = ("huissier", "sténographe", "timbres_judiciaires")
#: Words of a payee or description that name the taxes or the authorities.
_TAX_WORDS = ("tps", "tvq", "gst", "qst", "arc")
_TAX_PHRASES = ("revenu québec", "revenu quebec", "agence du revenu")
#: The natures a TPS/TVQ remittance or refund is mistaken for.
_FISCAL_SCAN_KINDS = ("dépense", "recette_autre", "prélèvement",
                      "virement_interne_sortant", "virement_interne_entrant")
#: The heading of the reconciliation lines, on the page and in the PDF.
FLOORS_HEADING = "Conciliations — une période non conciliée peut encore changer"


class Unavailable(Exception):
    """A section cannot be computed; ``args[0]`` is the French sentence."""


@dataclass
class Snapshot:
    """One consistent read: each collection's rows, or ``None`` when its read
    failed (``failures[name]`` then says why: ``"read"`` or ``"truncated"``)."""

    admin_accounts: Optional[list] = None
    admin_transactions: Optional[list] = None
    admin_reconciliations: Optional[list] = None
    invoices: Optional[list] = None
    trust_accounts: Optional[list] = None
    trust_transactions: Optional[list] = None
    time_entries: Optional[list] = None
    expenses: Optional[list] = None
    failures: dict = field(default_factory=dict)

    def rows(self, name: str) -> list:
        """The rows of *name*, or :class:`Unavailable` naming the failure."""
        value = getattr(self, name)
        if value is None:
            if self.failures.get(name) == "truncated":
                raise Unavailable(
                    f"Lecture tronquée ({COLLECTION_LABELS[name]}) : trop "
                    "d'inscriptions pour un calcul complet.")
            raise Unavailable(
                f"Lecture impossible ({COLLECTION_LABELS[name]}) — réessayez "
                "dans un instant.")
        return value


# ── Values and days ─────────────────────────────────────────────────────────


def _cents(doc: dict, key: str) -> int:
    """An integer amount in cents: absent or None reads 0 (legacy documents
    lack later keys); anything else but an int — a bool, a float, a string —
    is unreadable: never rounded, never guessed."""
    value = doc.get(key)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise Unavailable(
            f"Montant illisible ({key}) sur l'inscription {doc.get('id') or '?'} "
            "— aucun total n'est calculé sur un montant douteux.")
    return value


def _day(value) -> Optional[date]:
    """The UTC calendar day of a stored date-only value (midnight UTC)."""
    if isinstance(value, datetime):
        return (value.astimezone(timezone.utc) if value.tzinfo else value).date()
    if isinstance(value, date):
        return value
    return None


def _mtl_day(value) -> Optional[date]:
    """The Montréal calendar day of a stored instant."""
    if isinstance(value, datetime):
        return to_mtl(value).date()
    return None


def _within(day: Optional[date], start: date, end: date) -> bool:
    return day is not None and start <= day <= end


# ── The administration ledger ───────────────────────────────────────────────


@dataclass
class _Admin:
    accounts: dict           # id -> account
    rows: list               # resolved rows, with "_day", "_amount", "_delta"
    ops_ids: list            # operations accounts, display order
    card_ids: list           # card accounts, display order
    by_id: dict = field(default_factory=dict)   # resolved row id -> row


def _admin(snapshot: Snapshot) -> _Admin:
    accounts = {a["id"]: a for a in snapshot.rows("admin_accounts")}
    raw = snapshot.rows("admin_transactions")
    for r in raw:
        if not r.get("account_id"):
            raise Unavailable(f"L'écriture {r.get('id')} du registre d'administration "
                              "ne nomme aucun compte.")
        if (r.get("kind") or "") not in al.VALID_KINDS:
            raise Unavailable(f"L'écriture {r.get('id')} du registre d'administration "
                              "porte une nature inconnue du registre.")
    try:
        resolved = al.resolve_corrections(raw)
    except al.UnresolvedCorrection as exc:
        raise Unavailable(
            f"La correction {exc.args[0] or '?'} du registre d'administration "
            "ne retrouve pas l'écriture qu'elle contre-passe : aucun total "
            "n'est calculé sur une inconnue.") from None
    except ValueError:
        raise Unavailable(
            "Une écriture du registre d'administration est illisible.") from None
    rows = []
    for r in resolved:
        if r["account_id"] not in accounts:
            raise Unavailable(
                f"L'écriture {r.get('id')} nomme un compte d'administration "
                "introuvable.")
        day = _day(r.get("date"))
        if day is None:
            raise Unavailable(f"L'écriture {r.get('id')} n'a pas de date.")
        amount = _cents(r, "amount")
        for key in ("net_amount", "gst_amount", "qst_amount"):
            _cents(r, key)
        rows.append({**r, "_day": day, "_amount": amount,
                     "_delta": al.admin_delta(r.get("direction", ""), amount)})
    ops_ids, card_ids = [], []
    for aid, account in sorted(accounts.items(),
                               key=lambda kv: (kv[1].get("name") or "", kv[0])):
        if account.get("account_type") == "opérations":
            ops_ids.append(aid)
        elif account.get("account_type") == "carte_crédit":
            card_ids.append(aid)
        else:
            raise Unavailable(f"Le compte d'administration {aid} a un type inconnu.")
    return _Admin(accounts, rows, ops_ids, card_ids, {r["id"]: r for r in rows})


def _signed(row: dict) -> int:
    """The amount a resolved row counts for on its (original's) line."""
    return row["sign"] * row["_amount"]


def _factor(row: dict) -> int:
    """+1 for a dépense, -1 for its refund (``admin_ledger.REFUND_KINDS``),
    which counts against the same category and tax credits."""
    return -1 if row.get("kind") in al.REFUND_KINDS else 1


def _cost(row: dict) -> int:
    """A resolved dépense's cost, net of TPS/TVQ — a refund's, negative."""
    return _factor(row) * row["sign"] * _cents(row, "net_amount")


def _tax_credits(row: dict) -> tuple:
    """``(CTI, RTI)`` a resolved dépense claims — a refund gives them back
    (negative)."""
    factor = _factor(row) * row["sign"]
    return factor * _cents(row, "gst_amount"), factor * _cents(row, "qst_amount")


# ── Invoices ────────────────────────────────────────────────────────────────


@dataclass
class _Invoices:
    by_id: dict
    index: dict                    # normalized number -> [invoice ids]


_INVOICE_MONEY = ("subtotal_fees", "subtotal_expenses", "gst_amount",
                  "qst_amount", "total", "retainer_applied", "amount_paid",
                  "amount_due")


def _invoices(snapshot: Snapshot) -> _Invoices:
    by_id = {}
    index: dict = {}
    for inv in snapshot.rows("invoices"):
        for key in _INVOICE_MONEY:
            _cents(inv, key)                      # unreadable ⇒ Unavailable
        iid = inv["id"]
        by_id[iid] = inv
        keys = {invoice_model.normalize_reference(inv.get("invoice_number")),
                invoice_model.normalize_reference(inv.get("legacy_ref"))}
        for key in keys:
            if key and iid not in index.setdefault(key, []):
                index[key].append(iid)
    return _Invoices(by_id, index)


def _counted(inv: Optional[dict]) -> bool:
    return bool(inv) and inv.get("status") in invoice_model.REVENUE_STATUSES


def _inv_day(inv: dict) -> Optional[date]:
    return _day(inv.get("date"))


def _amount_due(inv: dict) -> int:
    """The stored amount due; a legacy invoice without one owes its total
    less its provision (and is listed as an invoice to verify)."""
    if inv.get("amount_due") is None:
        return _cents(inv, "total") - _cents(inv, "retainer_applied")
    return _cents(inv, "amount_due")


def _invoice_defect(inv: dict) -> int:
    """How far the amount due departs from what the totals make it — the
    part of an issuance's effect on the net assets no named term explains."""
    expected = (_cents(inv, "subtotal_fees") + _cents(inv, "subtotal_expenses")
                + _cents(inv, "gst_amount") + _cents(inv, "qst_amount")
                - _cents(inv, "retainer_applied"))
    return _amount_due(inv) - expected


def _number(inv: dict) -> str:
    return str(inv.get("invoice_number") or inv.get("id") or "?")


# ── Attribution of trust-linked « autre recette » ───────────────────────────

ATTRIBUTED = "rattachee"
ABSENT = "absente"
OTHER_DOSSIER = "autre_dossier"
AMBIGUOUS = "ambigue"
NO_REFERENCE = "sans_reference"
MISSING_TRUST_ENTRY = "lien_introuvable"


def _attribution(admin: _Admin, invoices: _Invoices, trust_rows: list) -> dict:
    """``{admin row id: (status, invoice id or None)}`` for every
    trust-linked « autre recette » — a correction included: it carries its
    original's link, so it follows its original."""
    trust_by_id = {t["id"]: t for t in trust_rows}
    out = {}
    for r in admin.rows:
        if r.get("kind") != "recette_autre" or not r.get("trust_transaction_id"):
            continue
        t = trust_by_id.get(r["trust_transaction_id"])
        if t is None:
            out[r["id"]] = (MISSING_TRUST_ENTRY, None)
            continue
        if t.get("invoice_id"):
            iid = t["invoice_id"]
            out[r["id"]] = ((ATTRIBUTED, iid) if iid in invoices.by_id
                            else (ABSENT, None))
            continue
        key = invoice_model.normalize_reference(t.get("invoice_external_ref"))
        if not key:
            out[r["id"]] = (NO_REFERENCE, None)
            continue
        candidates = invoices.index.get(key, [])
        # A voided duplicate (an invoice imported twice under two spellings
        # of one number, one of them voided) never blocks the live match.
        live = [iid for iid in candidates
                if invoices.by_id[iid].get("status") != "annulée"]
        pool = live or candidates
        same = [iid for iid in pool
                if invoices.by_id[iid].get("dossier_id") == t.get("dossier_id")]
        if len(same) == 1:
            out[r["id"]] = (ATTRIBUTED, same[0])
        elif len(same) > 1 or len(pool) > 1:
            out[r["id"]] = (AMBIGUOUS, None)
        elif len(pool) == 1:
            out[r["id"]] = (OTHER_DOSSIER, pool[0])
        else:
            out[r["id"]] = (ABSENT, None)
    return out


def _is_attributed(attribution: dict, row: dict) -> bool:
    return attribution.get(row["id"], (ABSENT, None))[0] == ATTRIBUTED


# ── Positions at a day ──────────────────────────────────────────────────────


@dataclass
class _Payment:
    invoice_id: Optional[str]
    day: date
    amount: int               # signed (a correction negative)
    source: str               # "encaissement" | ATTRIBUTED
    row_id: str


@dataclass
class _World:
    """Everything a position at a day is computed from."""

    admin: _Admin
    invoices: _Invoices
    attribution: dict
    payments: list
    settled: dict = field(default_factory=dict)   # invoice id -> shortfall

    def receipts(self, iid: str, until: Optional[date], source: str) -> int:
        return sum(p.amount for p in self.payments
                   if p.invoice_id == iid and p.source == source
                   and (until is None or p.day <= until))

    def provision_part(self, iid: str, until: Optional[date]) -> int:
        """The part of the attributed transfers that covers the invoice's
        provision — first, in aggregate up to *until*."""
        attributed = self.receipts(iid, until, ATTRIBUTED)
        retainer = _cents(self.invoices.by_id[iid], "retainer_applied")
        return min(max(attributed, 0), retainer)

    def paid(self, iid: str, until: Optional[date]) -> int:
        """A counted invoice's register payments up to *until* (None: all
        time), less what covered its provision."""
        return (self.receipts(iid, until, "encaissement")
                + self.receipts(iid, until, ATTRIBUTED)
                - self.provision_part(iid, until))

    def balance(self, iid: str, until: date) -> int:
        inv = self.invoices.by_id[iid]
        return _amount_due(inv) - self.paid(iid, until) - self.settled.get(iid, 0)

    def taken(self, iid: Optional[str], until: date) -> bool:
        """Whether a payment to *iid* is one the receivables take at *until*:
        a billed invoice dated on or before that day."""
        inv = self.invoices.by_id.get(iid) if iid else None
        day = _inv_day(inv) if inv else None
        return _counted(inv) and day is not None and day <= until


def _world(admin: _Admin, invoices: _Invoices, trust_rows: Optional[list]) -> _World:
    attribution = _attribution(admin, invoices, trust_rows or []) if trust_rows is not None else {}
    payments = []
    for r in admin.rows:
        if r.get("kind") == "encaissement_facture":
            payments.append(_Payment(r.get("invoice_id"), r["_day"], _signed(r),
                                     "encaissement", r["id"]))
        elif _is_attributed(attribution, r):
            payments.append(_Payment(attribution[r["id"]][1], r["_day"], _signed(r),
                                     ATTRIBUTED, r["id"]))
    world = _World(admin, invoices, attribution, payments)
    for iid, inv in invoices.by_id.items():
        if inv.get("status") == "payée":
            shortfall = _amount_due(inv) - world.paid(iid, None)
            if shortfall > 0:
                world.settled[iid] = shortfall
    return world


@dataclass
class Position:
    """The balance sheet at the end of one day."""

    day: date
    cash: dict                # operations account id -> cents
    card: dict                # card account id -> cents owed
    receivables: int
    tax_payable: int
    unattached: int
    provisions: int           # provision coverage received (never in a total)
    advances: int = 0         # clients' advances received here, not yet moved to trust

    @property
    def assets(self) -> int:
        return sum(self.cash.values()) + self.receivables

    @property
    def liabilities(self) -> int:
        return (sum(self.card.values()) + self.tax_payable + self.unattached
                + self.advances)

    @property
    def net(self) -> int:
        return self.assets - self.liabilities


def _position(world: _World, day: date) -> Position:
    admin = world.admin
    cash = {aid: 0 for aid in admin.ops_ids}
    card = {aid: 0 for aid in admin.card_ids}
    tax = advances = 0
    for r in admin.rows:
        if r["_day"] > day:
            continue
        if r["account_id"] in cash:
            cash[r["account_id"]] += r["_delta"]
        else:
            card[r["account_id"]] -= r["_delta"]
        kind = r.get("kind")
        if kind in al.CATEGORY_KINDS:
            tax -= sum(_tax_credits(r))
        elif kind == "remise_tps_tvq":
            tax -= _signed(r)
        elif kind == "remboursement_tps_tvq":
            tax += _signed(r)
        elif kind in al.ADVANCE_KINDS:
            advances += _signed(r)
        elif al.is_advance_transfer(r):
            advances -= _signed(r)
    receivables = provisions = 0
    for iid, inv in world.invoices.by_id.items():
        if world.taken(iid, day):
            tax += _cents(inv, "gst_amount") + _cents(inv, "qst_amount")
            receivables += world.balance(iid, day)
            provisions += world.provision_part(iid, day)
    unattached = sum(p.amount for p in world.payments
                     if p.day <= day and not world.taken(p.invoice_id, day))
    return Position(day, cash, card, receivables, tax, unattached, provisions,
                    advances)


# ── The statements ──────────────────────────────────────────────────────────


@dataclass
class Statements:
    period: periods.Period
    as_of: date
    opening_day: date
    in_progress: bool
    now: datetime
    sections: dict = field(default_factory=dict)      # key -> data
    unavailable: dict = field(default_factory=dict)   # key -> French sentence
    points: list = field(default_factory=list)
    accounts: dict = field(default_factory=dict)      # admin account id -> account
    floors: Optional[dict] = None                     # admin account id -> day or None
    floors_error: str = ""                            # why the floors are unknown
    points_unchecked: list = field(default_factory=list)  # French reasons
    books_start: date = BOOKS_START


def _manual_marks(admin: _Admin, trust_rows: Optional[list]) -> dict:
    """``{admin row id: fee payment day}`` — the « autre recette » entries
    that may be the recette the lawyer entered by hand for a fee payment
    drawn from trust with no linked recette (``fee_payment
    .manual_recette_candidates``): marked, never moved."""
    if trust_rows is None:
        return {}
    linked = {r.get("trust_transaction_id") for r in admin.rows
              if r.get("trust_transaction_id")}
    unlinked = [r for r in admin.rows
                if r["sign"] == 1 and not r.get("trust_transaction_id")
                and r.get("direction") == "recette"
                and r.get("kind") in al.REVENUE_KINDS
                and r.get("status") != "annulée" and not r.get("reversed_by_id")]
    marks = {}
    for fee in trust_rows:
        if (fee.get("purpose") != trust.FEE_PAYMENT_PURPOSE
                or fee.get("status") == "annulée" or fee.get("reversed_by_id")
                or fee.get("id") in linked):
            continue
        fee = {**fee, "amount": _cents(fee, "amount")}
        for row in fee_payment.manual_recette_candidates(fee, unlinked):
            if row.get("kind") == "recette_autre":
                day = _day(fee.get("date"))
                if day is not None and (row["id"] not in marks or day < marks[row["id"]]):
                    marks[row["id"]] = day
    return marks


def _income(admin: _Admin, invoices: _Invoices, start: date, end: date,
            marks: dict) -> dict:
    fees = billed_expenses = 0
    for inv in invoices.by_id.values():
        if _counted(inv) and _within(_inv_day(inv), start, end):
            fees += _cents(inv, "subtotal_fees")
            billed_expenses += _cents(inv, "subtotal_expenses")
    other = []
    costs = {cls: {} for cls in al.COST_CLASSES}
    for r in admin.rows:
        if not _within(r["_day"], start, end):
            continue
        kind = r.get("kind")
        if kind == "recette_autre" and not r.get("trust_transaction_id"):
            other.append(r)
        elif kind in al.CATEGORY_KINDS:
            # A refund lowers the class and category of the cost it refunds.
            bucket = costs[al.cost_class(r)]
            category = r.get("category") or ""
            bucket[category] = bucket.get(category, 0) + _cost(r)
    other.sort(key=lambda r: (r["_day"], r.get("id") or ""))
    other_total = sum(_signed(r) for r in other)
    totals = {cls: sum(v.values()) for cls, v in costs.items()}
    revenue = fees + billed_expenses + other_total
    gross = revenue - totals["direct"]
    net = gross - totals["variable"] - totals["fixe"]
    return {"fees": fees, "billed_expenses": billed_expenses,
            "other": other, "other_total": other_total,
            "marks": {r["id"]: marks[r["id"]] for r in other if r["id"] in marks},
            "costs": costs, "cost_totals": totals,
            "revenue": revenue, "gross_margin": gross, "net_income": net}


def _cash_flow(world: _World, start: date, end: date,
               opening: Position, closing: Position) -> dict:
    admin = world.admin
    lines = {key: 0 for key in CASH_FLOW_LINE_ORDER}
    by_class = {cls: 0 for cls in al.COST_CLASSES}
    per_account = {aid: 0 for aid in admin.ops_ids}
    moved = False
    for r in admin.rows:
        if r["account_id"] not in per_account or not _within(r["_day"], start, end):
            continue
        moved = True
        kind = r.get("kind")
        line = al.cash_flow_line(r)
        if kind == "recette_autre" and r.get("trust_transaction_id"):
            line = ("encaissements_factures" if _is_attributed(world.attribution, r)
                    else "encaissements_factures_absentes")
        lines[line] += r["_delta"]
        if kind == "dépense":
            by_class[al.cost_class(r)] += r["_delta"]
        per_account[r["account_id"]] += r["_delta"]
    for aid in admin.ops_ids:
        if opening.cash[aid] + per_account[aid] != closing.cash[aid]:
            raise AssertionError(f"cash flow does not close on account {aid}")
    return {"lines": lines, "depenses_by_class": by_class, "moved": moved,
            "opening": sum(opening.cash.values()),
            "closing": sum(closing.cash.values())}


def _rollforward(world: _World, start: date, end: date, income: dict,
                 opening: Position, closing: Position) -> dict:
    terms = {"apports": 0, "prelevements": 0, "virements": 0, "factures_absentes": 0}
    defects = {"direction": 0, "split": 0, "card": 0, "invoice": 0}
    defect_ids = {key: [] for key in defects}
    for r in world.admin.rows:
        if not _within(r["_day"], start, end):
            continue
        kind = r.get("kind")
        signed = _signed(r)
        if kind == "paiement_carte":
            defects["card"] += r["_delta"]
            continue
        expected_delta = signed if al.implied_direction(kind) == "recette" else -signed
        if r["_delta"] != expected_delta:
            defects["direction"] += r["_delta"] - expected_delta
            defect_ids["direction"].append(r["id"])
        if kind in al.CATEGORY_KINDS:
            # A refund's inexact split moves the net assets the other way.
            gap = _factor(r) * r["sign"] * (
                _cents(r, "net_amount") + _cents(r, "gst_amount")
                + _cents(r, "qst_amount") - r["_amount"])
            if gap:
                defects["split"] += gap
                defect_ids["split"].append(r["id"])
        elif kind == "apport":
            terms["apports"] += signed
        elif kind == "prélèvement":
            terms["prelevements"] += signed
        elif kind == "virement_interne_entrant":
            terms["virements"] += signed
        elif kind == "virement_interne_sortant" and not al.is_advance_transfer(r):
            # A transfer of advances settles the advances owed (a liability):
            # like the advance it moves, it leaves the net assets alone.
            terms["virements"] -= signed
        elif (kind == "recette_autre" and r.get("trust_transaction_id")
              and not _is_attributed(world.attribution, r)):
            terms["factures_absentes"] += signed
    defect_ids["card"] = [r["id"] for r in world.admin.rows
                          if r.get("kind") == "paiement_carte"
                          and _within(r["_day"], start, end)
                          and not _within(_card_partner_day(world.admin, r), start, end)]
    settled = applied = 0
    for iid, inv in world.invoices.by_id.items():
        if _counted(inv) and _within(_inv_day(inv), start, end):
            settled += world.settled.get(iid, 0)
            applied += _cents(inv, "retainer_applied")
            gap = _invoice_defect(inv)
            if gap:
                defects["invoice"] += gap
                defect_ids["invoice"].append(iid)
    received = closing.provisions - opening.provisions
    total = sum(defects.values())
    expected = (opening.net + income["net_income"] + terms["apports"]
                - terms["prelevements"] + terms["virements"]
                + terms["factures_absentes"] - settled - applied + received + total)
    return {"opening": opening.net, "net_income": income["net_income"], **terms,
            "settled": settled, "provisions_applied": applied,
            "provisions_received": received, "defects": defects,
            "defect_ids": defect_ids, "defects_total": total,
            "expected": expected, "closing": closing.net,
            "unexplained": closing.net - expected}


def _card_partner_day(admin: _Admin, row: dict) -> Optional[date]:
    """The day of a card-payment leg's other leg — for a correction, the
    correction of the other leg — or None when it cannot be found."""
    if row["sign"] > 0:
        partner = admin.by_id.get(row.get("related_transaction_id"))
    else:
        original = admin.by_id.get(row.get("corrects_id")) or {}
        other = admin.by_id.get(original.get("related_transaction_id")) or {}
        partner = admin.by_id.get(other.get("reversed_by_id"))
    return partner["_day"] if partner else None


def _taxes(admin: _Admin, invoices: _Invoices, start: date, end: date,
           opening_tax: int, closing_tax: int) -> dict:
    gst = qst = cti = rti = remitted = refunded = 0
    for inv in invoices.by_id.values():
        if _counted(inv) and _within(_inv_day(inv), start, end):
            gst += _cents(inv, "gst_amount")
            qst += _cents(inv, "qst_amount")
    for r in admin.rows:
        if not _within(r["_day"], start, end):
            continue
        kind = r.get("kind")
        if kind in al.CATEGORY_KINDS:
            gst_credit, qst_credit = _tax_credits(r)
            cti += gst_credit
            rti += qst_credit
        elif kind == "remise_tps_tvq":
            remitted += _signed(r)
        elif kind == "remboursement_tps_tvq":
            refunded += _signed(r)
    net = gst + qst - cti - rti
    if opening_tax + net - remitted + refunded != closing_tax:
        raise AssertionError("TPS/TVQ table does not close")
    return {"gst_collected": gst, "qst_collected": qst, "cti": cti, "rti": rti,
            "net": net, "remitted": remitted, "refunded": refunded,
            "opening": opening_tax, "closing": closing_tax}


def _adjustments(invoices: _Invoices, start: date, end: date) -> dict:
    voided, late, late_imported = [], [], []
    undated_voids = 0
    for inv in invoices.by_id.values():
        day = _inv_day(inv)
        if inv.get("status") == "annulée":
            when = _mtl_day(inv.get("voided_at"))
            if when is None:
                undated_voids += 1
            elif _within(when, start, end) and day is not None and day < start:
                voided.append(inv)
        elif (_counted(inv) and day is not None and day < start
              and _within(_mtl_day(inv.get("created_at")), start, end)):
            (late_imported if invoice_model.is_imported(inv) else late).append(inv)

    def order(inv):
        return (_inv_day(inv) or date.min, _number(inv))

    return {"voided": sorted(voided, key=order), "late": sorted(late, key=order),
            "late_imported": sorted(late_imported, key=order),
            "undated_voids": undated_voids}


def _trust_memo(snapshot: Snapshot, opening_day: date, as_of: date) -> dict:
    accounts = sorted(snapshot.rows("trust_accounts"),
                      key=lambda a: (a.get("name") or "", a["id"]))
    balances = {a["id"]: [0, 0] for a in accounts}
    for t in snapshot.rows("trust_transactions"):
        aid = t.get("account_id")
        if aid not in balances:
            raise Unavailable(
                f"L'écriture {t.get('id')} du fidéicommis nomme un compte introuvable.")
        day = _day(t.get("date"))
        if day is None:
            raise Unavailable(f"L'écriture {t.get('id')} du fidéicommis n'a pas de date.")
        book = trust.compute_deltas(t.get("direction", ""), _cents(t, "amount"),
                                    t.get("status", ""))["book"]
        if day <= opening_day:
            balances[aid][0] += book
        if day <= as_of:
            balances[aid][1] += book
    return {"accounts": [(a, balances[a["id"]][0], balances[a["id"]][1])
                         for a in accounts]}


def _wip_at(entries: list, invoices: _Invoices, day: date, *, time: bool) -> tuple:
    """``(unbilled, still on a voided invoice, marked billed but its invoice
    unknown)`` at *day*, in cents — the last two never in a total."""
    unbilled = voided = frozen = 0
    for e in entries:
        when = _day(e.get("date"))
        if when is None or when > day or (time and e.get("billable") is False):
            continue
        amount = _cents(e, "amount")
        inv = invoices.by_id.get(e.get("invoice_id")) if e.get("invoice_id") else None
        if inv is not None and inv.get("status") == "annulée":
            voided += amount
        elif inv is None and e.get("invoiced"):
            frozen += amount
        elif not (_counted(inv) and _inv_day(inv) is not None and _inv_day(inv) <= day):
            unbilled += amount
    return unbilled, voided, frozen


def _wip(snapshot: Snapshot, invoices: _Invoices, opening_day: date, as_of: date) -> dict:
    time_rows = snapshot.rows("time_entries")
    expense_rows = snapshot.rows("expenses")
    out = {}
    for label, day in (("opening", opening_day), ("closing", as_of)):
        t_unbilled, t_voided, t_frozen = _wip_at(time_rows, invoices, day, time=True)
        e_unbilled, e_voided, e_frozen = _wip_at(expense_rows, invoices, day, time=False)
        out[label] = {"time": t_unbilled, "expenses": e_unbilled,
                      "voided": t_voided + e_voided, "frozen": t_frozen + e_frozen}
    return out


def _lock_floors(snapshot: Snapshot) -> dict:
    """``{admin account id: last completed reconciliation day or None}``."""
    floors = {a["id"]: None for a in snapshot.rows("admin_accounts")}
    for rec in snapshot.rows("admin_reconciliations"):
        if rec.get("status") != "complétée" or rec.get("account_id") not in floors:
            continue
        day = _day(rec.get("period_end"))
        current = floors[rec["account_id"]]
        if day is not None and (current is None or day > current):
            floors[rec["account_id"]] = day
    return floors


# ── Points à vérifier ───────────────────────────────────────────────────────


def _point(points: list, key: str, label: str, ids: list, *,
           amount: Optional[int] = None, contribution: Optional[int] = None,
           refs: Optional[list] = None) -> None:
    """A point, when *ids* is not empty; *refs* are what the lawyer reads
    to find each record (an invoice number, an entry's day and payee)."""
    if ids:
        points.append({"key": key, "label": label, "count": len(ids),
                       "amount": amount, "contribution": contribution,
                       "ids": [str(x) for x in ids],
                       "refs": [str(x) for x in (refs if refs is not None else ids)]})


def _ref(row: dict) -> str:
    """How an administration entry is found on its journal: day and payee."""
    return f"{format_date_fr(row['_day'])} — {row.get('counterparty') or row.get('id')}"


def _names_the_taxes(row: dict) -> bool:
    """A payee or description that names the taxes or the authorities
    (whole words, lower case)."""
    text = " ".join([str(row.get("counterparty") or ""),
                     str(row.get("description") or "")]).lower()
    words = set("".join(ch if ch.isalnum() else " " for ch in text).split())
    return bool(words & set(_TAX_WORDS)) or any(p in text for p in _TAX_PHRASES)


def _points(st: Statements, world: Optional[_World], admin: Optional[_Admin],
            invoices: Optional[_Invoices]) -> list:
    points: list = []
    start, end = st.period.start, st.as_of
    if invoices is not None:
        rows = list(invoices.by_id.values())
        drafts = [i for i in rows if i.get("status") == "brouillon"
                  and _within(_inv_day(i), start, end)]
        _point(points, "brouillons", "Factures en brouillon datées dans la "
               "période — aucun revenu tant qu'elles ne sont pas envoyées",
               [_number(i) for i in drafts],
               amount=sum(_cents(i, "total") for i in drafts))
        _point(points, "factures_sans_date", "Factures émises sans date — dans "
               "aucune période", [_number(i) for i in rows
                                  if _counted(i) and _inv_day(i) is None])
        _point(points, "statut_inconnu", "Factures d'un statut inconnu",
               [_number(i) for i in rows
                if i.get("status") not in invoice_model.VALID_STATUSES])
        _point(points, "datees_apres_calcul", "Factures datées après le jour du "
               "calcul (date en UTC, créées le soir) — elles compteront demain",
               [_number(i) for i in rows if _counted(i) and _inv_day(i) is not None
                and end < _inv_day(i) <= st.period.end])
        _point(points, "factures_incoherentes", "Factures dont le montant dû "
               "manque ou contredit les totaux",
               [_number(i) for i in rows if _counted(i)
                and (i.get("amount_due") is None or _invoice_defect(i) != 0)])
    if world is not None:
        by_id = world.admin.by_id
        groups = (
            ("rattachements_presumes", (ATTRIBUTED,), "Virements du fidéicommis "
             "rattachés à une facture par l'identifiant ou le numéro qu'ils citent "
             "(rattachement présumé)"),
            ("factures_absentes", (ABSENT,), "Virements du fidéicommis citant une "
             "facture absente d'Athéna — hors revenus ; leur TPS/TVQ manque au passif"),
            ("numero_ambigu", (AMBIGUOUS,), "Virements du fidéicommis dont le numéro "
             "cité désigne plusieurs factures — doublon à vérifier"),
            ("sans_reference", (NO_REFERENCE,), "Virements du fidéicommis qui ne "
             "citent aucune facture"),
            ("lien_introuvable", (MISSING_TRUST_ENTRY,), "Recettes liées à une "
             "écriture du fidéicommis introuvable"),
            ("autre_dossier", (OTHER_DOSSIER,), "Virements du fidéicommis citant la "
             "facture d'un autre dossier (non rattachés)"),
        )
        for key, statuses, label in groups:
            ids = [rid for rid, (s, _i) in world.attribution.items() if s in statuses]
            _point(points, key, label, ids, amount=sum(_signed(by_id[x]) for x in ids),
                   refs=[_ref(by_id[x]) for x in ids])
        settled = [iid for iid in world.settled if world.taken(iid, end)]
        _point(points, "reglees_hors_registres", "Factures « payée » dont les "
               "paiements inscrits n'atteignent pas le montant dû (solde présumé "
               "réglé)", [_number(world.invoices.by_id[i]) for i in settled],
               amount=sum(world.settled[i] for i in settled))
        loose = [p for p in world.payments if p.day <= end
                 and not world.taken(p.invoice_id, end)]
        _point(points, "a_rattacher", "Sommes reçues à rattacher — facture "
               "annulée, en brouillon, introuvable ou datée plus tard",
               [p.row_id for p in loose], amount=sum(p.amount for p in loose),
               refs=[_ref(by_id[p.row_id]) for p in loose])
        before = [i for iid, i in world.invoices.by_id.items()
                  if world.taken(iid, end) and _inv_day(i) < st.books_start
                  and world.balance(iid, end) != 0]
        _point(points, "avant_les_livres", "Factures antérieures au début des "
               "livres qui portent encore un solde", [_number(i) for i in before])
        _point(points, "montant_paye", "Factures dont le montant payé inscrit "
               "diffère des encaissements du registre",
               [_number(i) for iid, i in world.invoices.by_id.items()
                if _cents(i, "amount_paid") != world.receipts(iid, None, "encaissement")])
        _point(points, "creances_negatives", "Factures payées au-delà du montant dû",
               [_number(i) for iid, i in world.invoices.by_id.items()
                if world.taken(iid, end) and world.balance(iid, end) < 0])
    if admin is not None:
        period_rows = [r for r in admin.rows if _within(r["_day"], start, end)]
        depenses = [r for r in period_rows if r.get("kind") == "dépense"]
        no_dossier = [r for r in depenses if not r.get("dossier_id")
                      and (r.get("category") or "") in _DOSSIER_CATEGORIES]
        _point(points, "huissier_sans_dossier", "Frais d'huissier, de "
               "sténographe ou de greffe sans dossier — comptés en frais fixes",
               [r["id"] for r in no_dossier], amount=sum(_cost(r) for r in no_dossier),
               refs=[_ref(r) for r in no_dossier])
        fiscal = [r for r in period_rows if r.get("kind") in _FISCAL_SCAN_KINDS
                  and ((r.get("kind") == "dépense"
                        and (r.get("category") or "") == "taxes_permis")
                       or _names_the_taxes(r))]
        _point(points, "remise_possible", "Écritures qui nomment le fisc ou la "
               "TPS/TVQ (ou une dépense « Taxes et permis ») — une remise ou un "
               "remboursement de TPS/TVQ inscrit sous une autre nature ?",
               [r["id"] for r in fiscal], amount=sum(r["_delta"] for r in fiscal),
               refs=[_ref(r) for r in fiscal])
        untaxed = [r for r in depenses if r["sign"] == 1
                   and not _cents(r, "gst_amount") and not _cents(r, "qst_amount")]
        _point(points, "sans_ventilation", "Dépenses sans TPS ni TVQ ventilées — "
               "aucun crédit de taxe réclamé", [r["id"] for r in untaxed],
               amount=sum(r["_amount"] for r in untaxed), refs=[_ref(r) for r in untaxed])
        card_refunds = [r for r in period_rows if r.get("kind") == "recette_autre"
                        and r["account_id"] in admin.card_ids]
        _point(points, "recette_sur_carte", "Autres recettes sur une carte de "
               "crédit — remboursement d'achat ? son crédit de taxe n'est pas repris",
               [r["id"] for r in card_refunds],
               amount=sum(_signed(r) for r in card_refunds),
               refs=[_ref(r) for r in card_refunds])
        # An « autre recette » the same account sends on within 14 days, for
        # the same amount, by a virement interne not marked « avances »: the
        # shape of a client's advance that reached the wrong account.
        window = timedelta(days=14)
        sent = [r for r in admin.rows if r.get("kind") == "virement_interne_sortant"
                and not al.is_advance_transfer(r) and r["sign"] == 1
                and not r.get("reversed_by_id")]
        maybe = [r for r in period_rows if r.get("kind") == "recette_autre"
                 and r["sign"] == 1 and not r.get("trust_transaction_id")
                 and not r.get("reversed_by_id")
                 and any(s["account_id"] == r["account_id"] and s["_amount"] == r["_amount"]
                         and r["_day"] <= s["_day"] <= r["_day"] + window for s in sent)]
        _point(points, "avance_possible", "Autres recettes suivies, dans les 14 "
               "jours, d'un virement interne sortant du même montant — l'avance "
               "d'un client, à inscrire en « Avance d'honoraires » ?",
               [r["id"] for r in maybe], amount=sum(r["_amount"] for r in maybe),
               refs=[_ref(r) for r in maybe])
        # « Autres produits » print as one total (2026-11-08): the ones that
        # may be the recette of a fee payment drawn from trust are named here.
        income = st.sections.get("resultats")
        if income is not None:
            marks = income["marks"]
            marked = [r for r in income["other"] if r["id"] in marks]
            _point(points, "autres_produits_lies", "Autres produits qui pourraient "
                   "être la recette d'un paiement d'honoraires tiré du fidéicommis "
                   "— à vérifier", [r["id"] for r in marked],
                   amount=sum(_signed(r) for r in marked),
                   refs=[f"{_ref(r)} (paiement du fidéicommis du "
                         f"{format_date_fr(marks[r['id']])})" for r in marked])
    bilan = st.sections.get("bilan")
    if admin is not None and bilan is not None:
        owed = bilan["closing"].advances

        def newest(rows: list, total: int) -> list:
            """The latest of *rows* whose amounts reach *total* — under a
            first-in, first-out settlement, the ones still open."""
            picked, reached = [], 0
            for r in sorted(rows, key=lambda r: (r["_day"], r.get("id") or ""),
                            reverse=True):
                if reached >= total:
                    break
                picked.append(r)
                reached += r["_amount"]
            return picked

        standing = [r for r in admin.rows if r["sign"] == 1 and r["_day"] <= end
                    and not r.get("reversed_by_id")]
        if owed > 0:
            open_rows = newest([r for r in standing
                                if r.get("kind") in al.ADVANCE_KINDS], owed)
            _point(points, "avances_a_verser", "Avances d'honoraires reçues au "
                   "compte d'opérations, pas encore versées au fidéicommis",
                   [r["id"] for r in open_rows], amount=owed,
                   refs=[_ref(r) for r in open_rows])
        elif owed < 0:
            extra = newest([r for r in standing if al.is_advance_transfer(r)], -owed)
            _point(points, "avances_non_inscrites", "Virements marqués « avances » "
                   "au-delà des avances inscrites — une avance inscrite en autre "
                   "recette ?", [r["id"] for r in extra], amount=-owed,
                   refs=[_ref(r) for r in extra])
    roll = st.sections.get("avoir")
    if roll:
        for key, label in (("split", "Dépenses ou remboursements dont la ventilation "
                                     "n'égale pas le montant"),
                           ("card", "Paiements de carte dont les jambes tombent dans "
                                    "des périodes différentes"),
                           ("direction", "Écritures dont le sens contredit la nature"),
                           ("invoice", "Factures de la période dont le montant dû "
                                       "contredit les totaux")):
            ids = roll["defect_ids"][key]
            refs = ([_number(world.invoices.by_id[i]) for i in ids] if key == "invoice"
                    else [_ref(world.admin.by_id[i]) for i in ids])
            _point(points, f"ecart_{key}", label, ids,
                   contribution=roll["defects"][key], refs=refs)
    return points


# ── Assembly ────────────────────────────────────────────────────────────────


def compute(snapshot: Snapshot, period: periods.Period, *, today: date,
            now: datetime, books_start: date = BOOKS_START) -> Statements:
    """The statements of *period* over *snapshot*, computed up to *today*
    (Montréal's) while the period is in progress. Each section is computed
    or « indisponible » with its reason; nothing reads as zero for an
    unknown."""
    as_of = periods.as_of(period, today)
    opening_day = period.start - timedelta(days=1)
    st = Statements(period=period, as_of=as_of, opening_day=opening_day,
                    in_progress=periods.in_progress(period, today), now=now,
                    books_start=books_start)

    def attempt(builder):
        try:
            return builder(), ""
        except Unavailable as exc:
            return None, exc.args[0]

    admin, admin_error = attempt(lambda: _admin(snapshot))
    invoices, invoices_error = attempt(lambda: _invoices(snapshot))
    trust_rows, trust_error = attempt(lambda: snapshot.rows("trust_transactions"))
    if admin is not None:
        st.accounts = admin.accounts
    core_error = admin_error or invoices_error

    def unavailable(key: str, reason: str) -> None:
        st.unavailable[key] = reason

    world = None
    marks_error = ""
    if core_error:
        for key in ("resultats", "flux", "bilan", "avoir", "taxes"):
            unavailable(key, core_error)
    else:
        # A trust row the marks cannot read (an unreadable amount) leaves the
        # figures alone — the marks only name — but the point
        # « autres_produits_lies » is then unjudged, and the list says so.
        marks, marks_error = attempt(lambda: _manual_marks(admin, trust_rows))
        marks = marks or {}
        st.sections["resultats"] = _income(admin, invoices, period.start, as_of, marks)
        if trust_rows is None:
            for key in ("flux", "bilan", "avoir"):
                unavailable(key, trust_error)
            tax_world = _world(admin, invoices, None)
        else:
            world = tax_world = _world(admin, invoices, trust_rows)
        opening = _position(tax_world, opening_day)
        closing = _position(tax_world, as_of)
        if world is not None:
            st.sections["bilan"] = {"opening": opening, "closing": closing}
            st.sections["flux"] = _cash_flow(world, period.start, as_of, opening, closing)
            st.sections["avoir"] = _rollforward(world, period.start, as_of,
                                                st.sections["resultats"], opening, closing)
        st.sections["taxes"] = _taxes(admin, invoices, period.start, as_of,
                                      opening.tax_payable, closing.tax_payable)
    if invoices is not None:
        st.sections["redressements"] = _adjustments(invoices, period.start, as_of)
        wip, error = attempt(lambda: _wip(snapshot, invoices, opening_day, as_of))
        if wip is None:
            unavailable("travaux", error)
        else:
            st.sections["travaux"] = wip
    else:
        unavailable("redressements", invoices_error)
        unavailable("travaux", invoices_error)
    memo, error = attempt(lambda: _trust_memo(snapshot, opening_day, as_of))
    if memo is None:
        unavailable("fideicommis", error)
    else:
        st.sections["fideicommis"] = memo
    floors, floors_error = attempt(lambda: _lock_floors(snapshot))
    st.floors, st.floors_error = floors, floors_error
    st.points = _points(st, world, admin, invoices)
    # What the points could not judge — never « aucun point » over an unread
    # collection, nor over the marks of a trust row they could not read.
    for reason in (invoices_error, admin_error, "" if core_error else trust_error,
                   marks_error):
        if reason and reason not in st.points_unchecked:
            st.points_unchecked.append(reason)
    return st


def pdf_refusal(st: Statements) -> str:
    """The French reason the PDF cannot be built — one of the three
    statements is unavailable — or ""."""
    for key in REQUIRED_FOR_PDF:
        if key not in st.sections:
            return st.unavailable.get(key) or "Section indisponible."
    return ""


# ── Presentation: the labelled sections the page and the PDF render ─────────


def _row(label: str, *values, key: str, level: int = 0, style: str = "line",
         note: str = "") -> dict:
    """One labelled row. *key* is its stable identity within its section —
    the same line carries the same key in every period, so the connector's
    reader lines periods up by it (``get_financial_statements``); the page
    and the PDF ignore it."""
    return {"key": key, "label": label, "values": list(values), "level": level,
            "style": style, "note": note}


def _category_label(code: str) -> str:
    return al.ADMIN_CATEGORY_LABELS.get(code, code or "Sans catégorie")


def _account_name(account: dict) -> str:
    return str(account.get("name") or account.get("id") or "?")


def _cost_rows(cls: str, costs: dict) -> list:
    """The category lines of one cost class: the ledger's order, a code the
    vocabulary no longer knows after it; a group's members beneath its
    heading, which carries their total (``admin_ledger
    .EXPENSE_CATEGORY_GROUPS``). No category line sums to zero (a reversed
    pair); a heading prints whenever one of its members does — even at zero,
    when a refund cancels another member. Keys name the class: a category
    can fall in two classes (a dossier's cost filed without its dossier)."""
    codes = [c for c in al.ADMIN_EXPENSE_CATEGORIES if costs.get(c)]
    codes += sorted(c for c in set(costs) - set(al.ADMIN_EXPENSE_CATEGORIES)
                    if costs[c])
    rows, headed = [], set()
    for code in codes:
        group = al.category_group(code)
        if group is None:
            rows.append(_row(_category_label(code), costs[code],
                             key=f"categorie:{cls}:{code}", level=1))
            continue
        if group in headed:
            continue
        headed.add(group)
        members = [c for c in al.EXPENSE_CATEGORY_GROUPS[group] if costs.get(c)]
        rows.append(_row(al.EXPENSE_CATEGORY_GROUP_LABELS[group],
                         sum(costs[c] for c in members),
                         key=f"groupe:{cls}:{group}", level=1))
        rows.extend(_row(_category_label(c), costs[c],
                         key=f"categorie:{cls}:{c}", level=2) for c in members)
    return rows


def _present_income(data: dict) -> dict:
    # « Autres produits » print as one total (the lawyer's decision of
    # 2026-11-08); the ones that may be a fee payment's recette are a point.
    rows = [_row("Produits", None, key="produits", style="heading"),
            _row("Honoraires facturés", data["fees"], key="honoraires", level=1),
            _row("Débours refacturés", data["billed_expenses"],
                 key="debours_refactures", level=1),
            _row("Autres produits", data["other_total"], key="autres_produits", level=1),
            _row("Total des produits", data["revenue"], key="total_produits",
                 style="subtotal")]
    for cls in al.COST_CLASSES:
        heading = al.COST_CLASS_LABELS[cls]
        if cls == "direct":
            heading += " (dépenses liées à un dossier)"
        rows.append(_row(heading, None, key=f"classe:{cls}", style="heading"))
        rows.extend(_cost_rows(cls, data["costs"][cls]))
        rows.append(_row(f"Total — {al.COST_CLASS_LABELS[cls].lower()}",
                         data["cost_totals"][cls], key=f"total:{cls}", style="subtotal"))
        if cls == "direct":
            rows.append(_row("Marge brute", data["gross_margin"], key="marge_brute",
                             style="subtotal"))
    rows.append(_row("Bénéfice net", data["net_income"], key="benefice_net",
                     style="total"))
    return {"columns": ["Période"], "rows": rows, "notes": [
        "Base facturée : factures envoyées, en retard ou payées, à la date de "
        "la facture, avant taxes. Coûts nets de TPS/TVQ."]}


def _present_cash_flow(data: dict) -> dict:
    rows = [_row("Encaisse au début", data["opening"], key="encaisse_debut")]
    moved = False
    for key in CASH_FLOW_LINE_ORDER:
        if data["lines"][key]:
            moved = True
            rows.append(_row(CASH_FLOW_LINE_LABELS[key], data["lines"][key], key=key,
                             level=1))
            if key == "depenses":
                for cls in al.COST_CLASSES:
                    if data["depenses_by_class"][cls]:
                        rows.append(_row(f"dont {al.COST_CLASS_LABELS[cls].lower()}",
                                         data["depenses_by_class"][cls],
                                         key=f"depenses:{cls}", level=2))
    if not data["moved"]:
        rows.append(_row("Aucun mouvement dans la période", None, key="aucun_mouvement",
                         level=1, style="note"))
    elif not moved:
        rows.append(_row("Des mouvements qui s'annulent dans la période", None,
                         key="mouvements_annules", level=1, style="note"))
    rows.append(_row("Variation de l'encaisse", data["closing"] - data["opening"],
                     key="variation_encaisse", style="subtotal"))
    rows.append(_row("Encaisse à la fin", data["closing"], key="encaisse_fin",
                     style="total"))
    return {"columns": ["Période"], "rows": rows, "notes": [
        "Comptes d'opérations seulement : un achat à la carte de crédit sort "
        "de l'encaisse quand la carte est payée."]}


def _present_balance(st: Statements, data: dict) -> dict:
    opening, closing = data["opening"], data["closing"]
    accounts = st.accounts
    rows = [_row("Actif", None, None, key="actif", style="heading")]
    for aid in opening.cash:
        rows.append(_row(f"Encaisse — {_account_name(accounts[aid])}",
                         opening.cash[aid], closing.cash[aid], key=f"encaisse:{aid}",
                         level=1))
    rows.append(_row("Comptes clients", opening.receivables, closing.receivables,
                     key="comptes_clients", level=1))
    rows.append(_row("Total de l'actif", opening.assets, closing.assets,
                     key="total_actif", style="subtotal"))
    rows.append(_row("Passif", None, None, key="passif", style="heading"))
    for aid in opening.card:
        rows.append(_row(f"Carte de crédit — {_account_name(accounts[aid])} (solde dû)",
                         opening.card[aid], closing.card[aid], key=f"carte:{aid}",
                         level=1))
    rows.append(_row("TPS/TVQ à remettre", opening.tax_payable, closing.tax_payable,
                     key="tps_tvq_a_remettre", level=1))
    rows.append(_row("Sommes reçues à rattacher", opening.unattached, closing.unattached,
                     key="sommes_a_rattacher", level=1))
    rows.append(_row("Avances de clients à verser au fidéicommis", opening.advances,
                     closing.advances, key="avances_clients", level=1))
    rows.append(_row("Total du passif", opening.liabilities, closing.liabilities,
                     key="total_passif", style="subtotal"))
    rows.append(_row("Avoir net (actif − passif)", opening.net, closing.net,
                     key="avoir_net", style="total"))
    return {"columns": [f"Au {format_date_fr(st.opening_day)}", f"Au {format_date_fr(st.as_of)}"],
            "rows": rows, "notes": [
                "Un montant négatif au passif est une somme à recevoir (un crédit "
                "de taxe, un solde créditeur de carte). Les fonds en fidéicommis "
                "et les travaux en cours sont présentés en note, hors bilan."]}


def _present_rollforward(data: dict) -> dict:
    rows = [_row("Avoir net au début", data["opening"], key="avoir_debut"),
            _row("Bénéfice net", data["net_income"], key="benefice_net", level=1),
            _row("Apports de l'avocat", data["apports"], key="apports", level=1),
            _row("Prélèvements de l'avocat", -data["prelevements"], key="prelevements",
                 level=1),
            _row("Virements internes (nets, hors avances versées au fidéicommis)",
                 data["virements"], key="virements", level=1),
            _row("Virements du fidéicommis non rattachés à une facture",
                 data["factures_absentes"], key="factures_absentes", level=1),
            _row("Soldes de factures présumés réglés", -data["settled"],
                 key="soldes_presumes_regles", level=1),
            _row("Provisions appliquées aux factures", -data["provisions_applied"],
                 key="provisions_appliquees", level=1),
            _row("Provisions reçues du fidéicommis", data["provisions_received"],
                 key="provisions_recues", level=1),
            _row("Écarts mesurés (voir les points à vérifier)", data["defects_total"],
                 key="ecarts_mesures", level=1)]
    labels = {"direction": "sens contraire à la nature", "split": "ventilations inexactes",
              "card": "jambes de carte décalées", "invoice": "factures incohérentes"}
    for key, label in labels.items():
        if data["defects"][key]:
            rows.append(_row(f"dont {label}", data["defects"][key], key=f"ecart:{key}",
                             level=2))
    rows += [_row("Avoir net attendu", data["expected"], key="avoir_attendu",
                  style="subtotal"),
             _row("Avoir net à la fin", data["closing"], key="avoir_fin"),
             _row("Écart non expliqué", data["unexplained"], key="ecart_non_explique",
                  style="total")]
    return {"columns": ["Période"], "rows": rows, "notes": []}


def _present_taxes(data: dict) -> dict:
    rows = [_row("Solde au début", data["opening"], key="solde_debut"),
            _row("TPS perçue (factures de la période)", data["gst_collected"],
                 key="tps_percue", level=1),
            _row("TVQ perçue (factures de la période)", data["qst_collected"],
                 key="tvq_percue", level=1),
            _row("CTI — TPS payée sur les dépenses", -data["cti"], key="cti", level=1),
            _row("RTI — TVQ payée sur les dépenses", -data["rti"], key="rti", level=1),
            _row("Taxe nette de la période", data["net"], key="taxe_nette",
                 style="subtotal"),
            _row("Remises versées", -data["remitted"], key="remises", level=1),
            _row("Remboursements reçus", data["refunded"], key="remboursements", level=1),
            _row("Solde à la fin (à remettre)", data["closing"], key="solde_fin",
                 style="total")]
    return {"columns": ["Période"], "rows": rows, "notes": [
        "Selon l'état actuel des inscriptions : une facture annulée après son "
        "envoi, ou inscrite après coup, peut modifier une période déjà déclarée — "
        "voir les redressements."]}


def _present_adjustments(data: dict) -> dict:
    rows = []
    groups = (("voided", "Factures annulées pendant la période, datées avant elle "
                         "— un brouillon annulé n'avait jamais compté : à vérifier"),
              ("late", "Factures inscrites pendant la période, datées avant elle"),
              ("late_imported", "Factures reprises de l'ancien système pendant la "
                                "période, datées avant elle"))
    for key, heading in groups:
        if data[key]:
            rows.append(_row(heading, None, None, key=f"groupe:{key}", style="heading"))
            for inv in data[key]:
                # An invoice falls in one group at most (voided, or counted
                # and created late — imported or not).
                rows.append(_row(f"{_number(inv)} ({format_date_fr(_inv_day(inv))})",
                                 _cents(inv, "gst_amount"), _cents(inv, "qst_amount"),
                                 key=f"facture:{inv.get('id')}", level=1))
    notes = []
    if not rows:
        notes.append("Aucun redressement.")
    notes.append("Une facture datée d'une période antérieure mais envoyée pendant "
                 "celle-ci (un brouillon envoyé plus tard) n'est pas détectée ici.")
    if data["undated_voids"]:
        notes.append(f"{data['undated_voids']} facture(s) annulée(s) sans date "
                     "d'annulation (annulations antérieures au 28 septembre 2026).")
    return {"columns": ["TPS", "TVQ"], "rows": rows, "notes": notes}


def _present_trust(data: dict) -> dict:
    rows = []
    opening_total = closing_total = 0
    for account, opening, closing in data["accounts"]:
        kind = trust.ACCOUNT_TYPE_LABELS.get(account.get("account_type"),
                                            account.get("account_type") or "")
        rows.append(_row(f"{_account_name(account)} ({kind.lower()})", opening, closing,
                         key=f"compte:{account['id']}", level=1))
        opening_total += opening
        closing_total += closing
    if not rows:
        rows.append(_row("Aucun compte en fidéicommis", None, None, key="aucun_compte",
                         style="note"))
    rows.append(_row("Total des fonds en fidéicommis", opening_total, closing_total,
                     key="total_fideicommis", style="subtotal"))
    return {"columns": ["Début", "Fin"], "rows": rows, "notes": [
        "Les sommes détenues pour les clients ne sont pas des actifs du cabinet "
        "(solde aux livres)."]}


def _present_wip(data: dict) -> dict:
    o, c = data["opening"], data["closing"]
    rows = [_row("Heures non facturées", o["time"], c["time"], key="heures_non_facturees",
                 level=1),
            _row("Débours non facturés", o["expenses"], c["expenses"],
                 key="debours_non_factures", level=1),
            _row("Total des travaux en cours", o["time"] + o["expenses"],
                 c["time"] + c["expenses"], key="total_travaux", style="subtotal")]
    if o["voided"] or c["voided"]:
        rows.append(_row("Entrées encore rattachées à une facture annulée (hors total)",
                         o["voided"], c["voided"], key="sur_facture_annulee", level=1))
    if o["frozen"] or c["frozen"]:
        rows.append(_row("Entrées marquées facturées dont la facture est introuvable "
                         "(hors total)", o["frozen"], c["frozen"],
                         key="facture_introuvable", level=1))
    return {"columns": ["Début", "Fin"], "rows": rows, "notes": [
        "Valeur au taux inscrit sur chaque entrée, selon l'état actuel des "
        "inscriptions. Un dossier à forfait, à contingence, mixte, pro bono ou "
        "d'aide juridique peut ne pas facturer ces heures à ce taux."]}


_REFS_SHOWN = 5


def _present_points(points: list, unchecked: list) -> dict:
    rows = []
    if unchecked:
        rows.append(_row("Liste incomplète — " + " ".join(unchecked), None,
                         key="liste_incomplete", style="note"))
    for p in points:
        shown = ", ".join(p["refs"][:_REFS_SHOWN])
        more = len(p["refs"]) - _REFS_SHOWN
        refs = f" : {shown}" + (f" et {more} autre(s)" if more > 0 else "")
        if p["contribution"] is not None:
            value, note = p["contribution"], f"{p['count']} inscription(s){refs} — écart mesuré"
        else:
            value, note = p["amount"], f"{p['count']} inscription(s){refs}"
        rows.append(_row(p["label"], value, key=p["key"], note=note))
    if not points and not unchecked:
        rows.append(_row("Aucun point à vérifier.", None, key="aucun_point", style="note"))
    return {"key": "points", "title": "Points à vérifier", "columns": ["Montant"],
            "rows": rows, "notes": [], "unavailable": ""}


def _title_case(text: str) -> str:
    return text[:1].upper() + text[1:]


def present(st: Statements) -> dict:
    """Everything the page and the PDF show, labelled: the header, the KPI
    cards, the sections in order (each computed or carrying its
    « indisponible » sentence), the points to verify and the lock floors."""
    builders = {
        "resultats": _present_income,
        "flux": _present_cash_flow,
        "bilan": lambda d: _present_balance(st, d),
        "avoir": _present_rollforward,
        "taxes": _present_taxes,
        "redressements": _present_adjustments,
        "fideicommis": _present_trust,
        "travaux": _present_wip,
    }
    sections = []
    for key, title in SECTION_TITLES.items():
        if key in st.sections:
            body = builders[key](st.sections[key])
            sections.append({"key": key, "title": title, "unavailable": "", **body})
        else:
            sections.append({"key": key, "title": title, "columns": ["Période"],
                             "rows": [], "notes": [],
                             "unavailable": st.unavailable.get(key, "Indisponible.")})
    kpis = []
    income = st.sections.get("resultats")
    balance = st.sections.get("bilan")
    kpis.append({"key": "produits", "label": "Produits",
                 "value": income["revenue"] if income else None})
    kpis.append({"key": "benefice_net", "label": "Bénéfice net",
                 "value": income["net_income"] if income else None})
    kpis.append({"key": "encaisse", "label": "Encaisse", "value":
                 sum(balance["closing"].cash.values()) if balance else None})
    kpis.append({"key": "comptes_clients", "label": "Comptes clients", "value":
                 balance["closing"].receivables if balance else None})
    floors = []
    if st.floors is None:
        floors.append(f"Conciliations indisponibles — {st.floors_error}")
    else:
        for aid, account in sorted(st.accounts.items(),
                                   key=lambda kv: (kv[1].get("name") or "", kv[0])):
            day = st.floors.get(aid)
            floors.append(f"{_account_name(account)} : concilié jusqu'au "
                          f"{format_date_fr(day)}" if day else
                          f"{_account_name(account)} : jamais concilié")
    if st.in_progress:
        as_of_line = (f"Période en cours : calculée au {format_date_fr(st.as_of)} "
                      "inclusivement.")
    else:
        as_of_line = f"Arrêtée au {format_date_fr(st.as_of)}."
    now_mtl = to_mtl(st.now)
    return {
        "period_label": _title_case(periods.label_fr(st.period)),
        "span": _title_case(periods.span_fr(st.period)),
        "as_of_line": as_of_line,
        "computed_line": (f"Calculé le {format_date_fr(now_mtl.date())} à "
                          f"{now_mtl.strftime('%H:%M')} (heure de Montréal), selon "
                          "l'état actuel des inscriptions."),
        "kpis": kpis,
        "sections": sections,
        "points": _present_points(st.points, st.points_unchecked),
        "floors_heading": FLOORS_HEADING,
        "floors": floors,
    }
