"""Invoice → note-d'honoraires context builder (Phase H.2).

Pure function — no Firestore, no Flask (the caller loads the invoice and
line items). Maps a stored ``invoices/{id}`` document + its line items to:

* ``.values``     — scalar fields: ``facture.*`` (§6.2) plus the Phase H
                    header namespaces (``destinataire.*``/``dossier.*``/
                    ``cabinet.*``/``date.*``) resolved through the field
                    catalog, so the note reuses the same header fields as
                    letters and procedures;
* ``.rows``       — ``region -> list[row dict]`` for the three tables (§6.3);
* ``.conditions`` — ``si_*`` flags driving conditional-region removal (§5.4).

Every money figure is READ from the invoice and only *formatted* — the
builder performs no tax arithmetic (§7.2). Its only arithmetic is integer
addition of line-item cents for the two derived disbursement subtotals
(§6.4), which is exact.

The payments (2026-11-08) join only when the caller passes them
(:class:`InvoicePayments`; ``services/note_honoraires`` does so only for a
template that PRINTS them, :func:`prints_payments`): the region
``ligne_paiement`` — the provision of an imported invoice, then each
standing encaissement imputed on the invoice, then each standing write-off
of its remainder —, its flag ``si_paiements`` and ``facture.solde_restant``.
Without them the context holds exactly the keys every earlier template was
filled with, so its fingerprint
(``services.note_honoraires.fill_fingerprint``) does not move. The balance
arrives computed (``models.invoice.balance_of``, its one authority); the
payment rows are read and formatted, never summed here.

A write-off (« radiation », lot 3a of « Factures : retard, radiation,
intérêts ») is a forgiveness SHOWN TO THE CLIENT — the lawyer's decision D2
of 2026-11-11, refined by his decision 8 of 2026-11-12: a price reduction
documented by a credit note. Each one that stands prints as a row of its
own, source « Note de crédit <its number> » (lot 3b — « Radiation » until
then), its amount taxes included, and brings the printed balance to zero. A
cancelled one prints neither row, its nor its cancellation's (the caller
passes ``models.invoice.standing_write_offs``). The row is the template's
own ``ligne_paiement`` row, filled once more: no new part, no new field.

So does the client's trust balance, ``facture.solde_fideicommis``
(2026-11-08): given only for a template that prints it
(:func:`prints_trust_balance`), computed by the trust model
(``models.trust.client_book_balance``), formatted here.

Declared exception to « one source per list »: :data:`PAYMENT_SOURCE_LABELS`
words ``models.admin_ledger.VALID_METHODS`` for a CLIENT document (the
register's « Autre » reads « Paiement » there). Its keys are that
vocabulary exactly, pinned by ``tests/test_invoice_lifecycle.py`` — this
pure module may not import ``models``.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Optional

from utils.format_fr import (
    format_cents_fr,
    format_cents_fr_parens,
    format_date_fr,
    format_hours_fr,
    format_rate_fr,
)
from utils.template_fields import CATALOG, FLAT_ALIASES, resolve_values

# The invoice stores GST ×100 and QST ×1000 (models.invoice) — see
# utils.format_fr.format_rate_fr.
_GST_SCALE = 100
_QST_SCALE = 1000

# ── Payments (2026-11-08) ──────────────────────────────────────────────────
PAYMENT_REGION = "ligne_paiement"
PAYMENT_FLAG = "si_paiements"
#: The row-scoped fields of ``ligne_paiement`` — exactly the keys of every
#: row :func:`_payment_rows` emits (pinned by tests/test_invoice_docx.py).
PAYMENT_ROW_FIELDS = ("p.date", "p.source", "p.montant")
#: The fields whose presence in a template's placeholder inventory makes
#: the note read and print the payments (:func:`prints_payments`). The
#: markers ``{{#ligne_paiement}}`` and ``{{?si_paiements}}`` never enter the
#: inventory (``docx_fill.PLACEHOLDER_RE`` excludes them): the fields do.
PAYMENT_FIELDS = PAYMENT_ROW_FIELDS + ("facture.solde_restant",)
#: ``p.source`` of a payment drawn from trust — a fee payment's encaissement
#: (``trust_transaction_id`` set) — and of an imported invoice's provision.
PAYMENT_SOURCE_TRUST = "Fidéicommis"
#: ``p.source`` of a standing write-off of the invoice's remainder
#: (« radiation », lot 3a): a forgiveness shown to the client (D2) as a price
#: reduction documented by a credit note (decision 8, lot 3b) — this
#: wording, a space, then the credit note's number (« Note de crédit
#: 2026-NC001 »: :func:`_write_off_source`).
PAYMENT_SOURCE_CREDIT_NOTE = "Note de crédit"
#: ``p.source`` of every other encaissement, by its ``method`` (the module
#: docstring declares this vocabulary). A method outside it prints the
#: « autre » wording: the row is a payment either way.
PAYMENT_SOURCE_LABELS = {
    "chèque": "Chèque",
    "virement": "Virement",
    "prélèvement": "Prélèvement",
    "dépôt_direct": "Dépôt direct",
    "carte": "Carte",
    "comptant": "Comptant",
    "autre": "Paiement",
}
#: The client's trust balance in the invoice's dossier, at generation —
#: given, hence printable, only when a template names it
#: (:func:`prints_trust_balance`).
TRUST_BALANCE_FIELD = "facture.solde_fideicommis"


@dataclass
class InvoiceContext:
    values: dict[str, str] = field(default_factory=dict)
    rows: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    conditions: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class InvoicePayments:
    """What the note prints of the invoice's payments.

    ``receipts`` — the STANDING encaissements imputed on the invoice
    (``models.admin_ledger.list_invoice_receipts_strict``, kept by
    ``receipt_stands``), oldest first; ``balance_cents`` — its live balance
    (``models.invoice.balance_of``). The caller has checked that the two
    agree (``models.invoice.payments_print_defect``). ``write_offs`` — its
    STANDING write-offs (lot 3a, ``models.invoice.standing_write_offs``),
    in stored order: never a cancelled one, never a cancellation.
    """

    receipts: list[dict]
    balance_cents: int
    write_offs: list[dict] = field(default_factory=list)


def prints_payments(placeholders) -> bool:
    """True when a template prints the payments: its placeholder inventory
    names one of :data:`PAYMENT_FIELDS`. Exact names, as the fill matches
    them (row fields case-sensitively, ``facture.*`` values by key)."""
    return any(name in PAYMENT_FIELDS for name in (placeholders or ()))


def prints_trust_balance(placeholders) -> bool:
    """True when a template prints :data:`TRUST_BALANCE_FIELD` (its exact
    name in the placeholder inventory)."""
    return TRUST_BALANCE_FIELD in (placeholders or ())


def _as_date(value) -> Optional[date]:
    """A stored date-only field (midnight-UTC datetime) → its UTC calendar date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _date_str(value) -> str:
    d = _as_date(value)
    return format_date_fr(d) if d else ""


def _partie_from_billing_address(billing: dict) -> dict:
    """Synthetic partie from the invoice's ``billing_address`` snapshot (§6.6).

    Used when the client partie was deleted — the invoice is the record of
    what was billed, so its snapshot must still render. Typed as an
    organization so ``destinataire.nom_complet`` resolves to the snapshot name
    and the address fields feed ``destinataire.adresse_complete``.
    """
    billing = billing or {}
    return {
        "type": "organization",
        "organization_name": billing.get("name", ""),
        "address_street": billing.get("street", ""),
        "address_unit": billing.get("unit", ""),
        "address_city": billing.get("city", ""),
        "address_province": billing.get("province", ""),
        "address_postal_code": billing.get("postal_code", ""),
        "address_country": "Canada",
        "contact_role": "client",
    }


def _facture_values(invoice: dict, line_items: list[dict]) -> dict[str, str]:
    """The ``facture.*`` scalar fields (§6.2). All figures read, not computed."""
    fees = [li for li in line_items if li.get("type") == "fee"]
    expenses = [li for li in line_items if li.get("type") != "fee"]
    taxable = [e for e in expenses if e.get("taxable", True)]
    non_taxable = [e for e in expenses if not e.get("taxable", True)]

    # Derived subtotals — exact integer-cent addition (§6.4).
    st_tx = sum(int(e.get("amount", 0) or 0) for e in taxable)
    st_ntx = sum(int(e.get("amount", 0) or 0) for e in non_taxable)

    total_hours = sum(float(f.get("hours") or 0) for f in fees)
    rates = {r for f in fees if (r := int(f.get("rate") or 0)) > 0}
    taux_horaire = format_cents_fr(next(iter(rates))) if len(rates) == 1 else ""

    return {
        "facture.numero": invoice.get("invoice_number", ""),
        "facture.date": _date_str(invoice.get("date")),
        "facture.date_echeance": _date_str(invoice.get("due_date")),
        "facture.sous_total_honoraires": format_cents_fr(invoice.get("subtotal_fees", 0)),
        "facture.sous_total_debours_tx": format_cents_fr(st_tx),
        "facture.sous_total_debours_ntx": format_cents_fr(st_ntx),
        "facture.total_honoraires": format_cents_fr(invoice.get("subtotal_fees", 0)),
        "facture.total_debours_tx": format_cents_fr(st_tx),
        "facture.total_debours_ntx": format_cents_fr(st_ntx),
        "facture.total_avant_taxes": format_cents_fr(invoice.get("subtotal", 0)),
        "facture.tps_taux": format_rate_fr(invoice.get("gst_rate", 500), _GST_SCALE),
        "facture.tps_numero": invoice.get("gst_number", ""),
        "facture.tps_montant": format_cents_fr(invoice.get("gst_amount", 0)),
        "facture.tvq_taux": format_rate_fr(invoice.get("qst_rate", 9975), _QST_SCALE),
        "facture.tvq_numero": invoice.get("qst_number", ""),
        "facture.tvq_montant": format_cents_fr(invoice.get("qst_amount", 0)),
        "facture.total_apres_taxes": format_cents_fr(invoice.get("total", 0)),
        "facture.avances_fideicommis": format_cents_fr_parens(
            invoice.get("retainer_applied", 0)
        ),
        "facture.solde": format_cents_fr(invoice.get("amount_due", 0)),
        "facture.nombre_heures": format_hours_fr(total_hours),
        "facture.taux_horaire": taux_horaire,
    }


def _build_rows(line_items: list[dict]) -> dict[str, list[dict[str, str]]]:
    """Split line items into the three region row-lists, in line-item order."""
    honoraire: list[dict] = []
    debours_tx: list[dict] = []
    debours_ntx: list[dict] = []
    for li in line_items:
        date_str = _date_str(li.get("date"))
        description = li.get("description", "") or ""
        if li.get("type") == "fee":
            honoraire.append({
                "h.date": date_str,
                "h.description": description,
                "h.temps": format_hours_fr(li.get("hours") or 0),
            })
        else:
            row = {
                "d.date": date_str,
                "d.description": description,
                "d.cout": format_cents_fr(li.get("amount", 0)),
            }
            (debours_tx if li.get("taxable", True) else debours_ntx).append(row)
    return {
        "ligne_honoraire": honoraire,
        "ligne_debours_tx": debours_tx,
        "ligne_debours_ntx": debours_ntx,
    }


def _payment_source(receipt: dict) -> str:
    """« Fidéicommis » for a fee payment's encaissement, else its method's
    client-facing wording."""
    if receipt.get("trust_transaction_id"):
        return PAYMENT_SOURCE_TRUST
    return PAYMENT_SOURCE_LABELS.get(
        receipt.get("method") or "", PAYMENT_SOURCE_LABELS["autre"])


def _write_off_source(write_off: dict) -> str:
    """« Note de crédit 2026-NC001 » — a standing radiation's row source
    (lot 3b). The caller passes the events the model validated
    (``models.invoice.standing_write_offs``), each numbered: a radiation
    without its number never reaches this row — the model raises first."""
    return f"{PAYMENT_SOURCE_CREDIT_NOTE} {write_off['credit_note_number']}"


def _payment_rows(
    invoice: dict, receipts: list[dict], write_offs: Iterable[dict] = (),
) -> list[dict[str, str]]:
    """The ``ligne_paiement`` rows, oldest first.

    First the provision of an imported invoice (``retainer_applied``, the
    « Avances en fidéicommis » deducted when it was issued — dated the
    invoice's date, drawn from trust), so that the region REPLACES the
    Avances line and TOTAL APRÈS TAXES less the rows gives the balance;
    then each standing encaissement, in the caller's ``(date, sequence)``
    order; then each standing write-off (lot 3a), « Note de crédit <its
    number> » (lot 3b), its amount
    taxes included, dated the day it was recorded — it wrote off what the
    encaissements left. The sort is stable on the calendar day: the
    provision stays first on its day, the register's order holds within a
    day, and a write-off follows the encaissements of its day. Dates are the
    UTC calendar day (date-only values at UTC midnight, never ``to_mtl``).
    """
    entries = []
    retainer = int(invoice.get("retainer_applied", 0) or 0)
    if retainer > 0:
        entries.append((_as_date(invoice.get("date")), PAYMENT_SOURCE_TRUST, retainer))
    for receipt in receipts:
        entries.append((_as_date(receipt.get("date")), _payment_source(receipt),
                        int(receipt.get("amount", 0) or 0)))
    for write_off in write_offs:
        entries.append((_as_date(write_off.get("date")), _write_off_source(write_off),
                        int(write_off.get("amount", 0) or 0)))
    entries.sort(key=lambda entry: entry[0] or date.min)
    return [
        {
            "p.date": format_date_fr(day) if day else "",
            "p.source": source,
            "p.montant": format_cents_fr_parens(amount),
        }
        for day, source, amount in entries
    ]


def build_invoice_context(
    invoice: dict,
    line_items: list[dict],
    *,
    firm: dict,
    destinataire: Optional[dict],
    dossier: Optional[dict],
    today: date,
    payments: Optional[InvoicePayments] = None,
    trust_balance_cents: Optional[int] = None,
) -> InvoiceContext:
    """Build the note-d'honoraires context from a stored invoice (§6).

    ``destinataire`` is the client partie; when ``None`` (partie deleted) the
    billing_address snapshot is used so generation never fails (§6.6). Header
    fields are resolved through the Phase H catalog by canonical name.
    ``payments`` adds the payment region, its flag and
    ``facture.solde_restant``; ``trust_balance_cents`` adds
    ``facture.solde_fideicommis`` (module docstring). Left ``None``, each
    leaves the context exactly as it was before it.
    """
    dest = destinataire or _partie_from_billing_address(invoice.get("billing_address") or {})

    # Header namespaces via the Phase H catalog — both CANONICAL names and the
    # flat ALIASES the procedures/letters gabarits use, so a note template can
    # use the identical placeholders ({{numero_dossier}} as well as
    # {{dossier.numero_cour}}). Resolving the whole set is cheap and pure;
    # unused fields are ignored by the fill engine.
    values = resolve_values(
        list(CATALOG) + list(FLAT_ALIASES),
        dossier=dossier,
        client=None,
        adverse=None,
        destinataire=dest,
        firm=firm or {},
        today=today,
    )
    values.update(_facture_values(invoice, line_items))

    # Hourly rate: prefer the uniform billed rate; when the line items are
    # mixed or rateless, fall back to the dossier's configured rate so the
    # note still shows a rate (§6.2).
    if not values.get("facture.taux_horaire") and dossier and dossier.get("hourly_rate"):
        values["facture.taux_horaire"] = format_cents_fr(int(dossier["hourly_rate"]))

    rows = _build_rows(line_items)
    conditions = {
        "si_honoraires": bool(rows["ligne_honoraire"]),
        "si_debours_tx": bool(rows["ligne_debours_tx"]),
        "si_debours_ntx": bool(rows["ligne_debours_ntx"]),
    }
    if payments is not None:
        values["facture.solde_restant"] = format_cents_fr(payments.balance_cents)
        rows[PAYMENT_REGION] = _payment_rows(invoice, payments.receipts,
                                             payments.write_offs)
        conditions[PAYMENT_FLAG] = bool(rows[PAYMENT_REGION])
    if trust_balance_cents is not None:
        values[TRUST_BALANCE_FIELD] = format_cents_fr(trust_balance_cents)
    return InvoiceContext(values=values, rows=rows, conditions=conditions)
