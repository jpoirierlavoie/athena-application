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

THE CREDIT NOTE (lot 3d — the lawyer's decision 8 of 2026-11-12): the Word
document of one standing radiation, filled from the « actif » gabarit of
kind « note_credit » (``services/note_credit``).
:func:`build_credit_note_context` gives it the SAME header fields as the
note d'honoraires (``dossier.*``, ``destinataire.*`` — the invoice's
client —, ``cabinet.*``, ``date.*`` and their flat aliases: one resolution,
:func:`_header_values`), its own ``nc.*`` (:data:`CREDIT_NOTE_FIELDS`) read
from the event, and a few of the invoice's fields
(``utils.template_fields.CREDIT_NOTE_INVOICE_FIELDS``): those the note
d'honoraires reads from the invoice alone (:func:`_facture_values`), and
the balance after the credit, which arrives computed
(``models.invoice.balance_of``). No row, no condition. Its one arithmetic,
the amount before taxes, is the exact integer subtraction W − TPS − TVQ of
the event's own stored figures — no tax is computed here either.

Declared exception to « one source per list »: :data:`PAYMENT_SOURCE_LABELS`
words ``models.admin_ledger.VALID_METHODS`` for a CLIENT document (the
register's « Autre » reads « Paiement » there). Its keys are that
vocabulary exactly, pinned by ``tests/test_invoice_lifecycle.py`` — this
pure module may not import ``models``.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Optional

from utils.format_en import (
    format_cents_en,
    format_cents_en_parens,
    format_date_en,
    format_hours_en,
    format_rate_en,
)
from utils.format_fr import (
    format_cents_fr,
    format_cents_fr_parens,
    format_date_fr,
    format_hours_fr,
    format_rate_fr,
)
from utils.template_fields import (
    CATALOG,
    CREDIT_NOTE_INVOICE_FIELDS,
    FLAT_ALIASES,
    INTEREST_NOTICE_INVOICE_FIELDS,
    normalize_lang,
    resolve_values,
)

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
#: The invoice's live balance, as printed — on the note d'honoraires after
#: its payment rows, on the credit note (lot 3d) after the credit.
BALANCE_FIELD = "facture.solde_restant"
#: The fields whose presence in a template's placeholder inventory makes
#: the note read and print the payments (:func:`prints_payments`). The
#: markers ``{{#ligne_paiement}}`` and ``{{?si_paiements}}`` never enter the
#: inventory (``docx_fill.PLACEHOLDER_RE`` excludes them): the fields do.
PAYMENT_FIELDS = PAYMENT_ROW_FIELDS + (BALANCE_FIELD,)
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
PAYMENT_SOURCE_TRUST_EN = "Trust"
PAYMENT_SOURCE_CREDIT_NOTE_EN = "Credit note"
PAYMENT_SOURCE_LABELS_EN = {
    "chèque": "Cheque",
    "virement": "Wire transfer",
    "prélèvement": "Direct debit",
    "dépôt_direct": "Direct deposit",
    "carte": "Card",
    "comptant": "Cash",
    "autre": "Payment",
}
#: The client's trust balance in the invoice's dossier, at generation —
#: given, hence printable, only when a template names it
#: (:func:`prints_trust_balance`).
TRUST_BALANCE_FIELD = "facture.solde_fideicommis"

# ── The credit note (lot 3d) ───────────────────────────────────────────────
#: The credit note's own fields — exactly the nc.* keys
#: :func:`build_credit_note_context` emits (pinned against the guide by
#: tests/test_gabarits_placeholders_doc.py).
CREDIT_NOTE_FIELDS = (
    "nc.numero",
    "nc.date",
    "nc.motif",
    "nc.montant_avant_taxes",
    "nc.tps",
    "nc.tvq",
    "nc.total",
)

INTEREST_NOTICE_FIELDS = (
    "ai.numero",
    "ai.date",
    "ai.periode_debut",
    "ai.periode_fin",
    "ai.jours",
    "ai.capital",
    "ai.taux",
    "ai.montant",
    "ai.motif",
)


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


def _date_str(value, lang: str = "fr") -> str:
    d = _as_date(value)
    if not d:
        return ""
    return format_date_en(d) if normalize_lang(lang) == "en" else format_date_fr(d)


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


def _facture_values(
    invoice: dict, line_items: list[dict], *, lang: str = "fr"
) -> dict[str, str]:
    """The ``facture.*`` scalar fields (§6.2). All figures read, not computed."""
    is_en = normalize_lang(lang) == "en"
    fmt_cents = format_cents_en if is_en else format_cents_fr
    fmt_cents_parens = format_cents_en_parens if is_en else format_cents_fr_parens
    fmt_hours = format_hours_en if is_en else format_hours_fr
    fmt_rate = format_rate_en if is_en else format_rate_fr

    fees = [li for li in line_items if li.get("type") == "fee"]
    expenses = [li for li in line_items if li.get("type") != "fee"]
    taxable = [e for e in expenses if e.get("taxable", True)]
    non_taxable = [e for e in expenses if not e.get("taxable", True)]

    # Derived subtotals — exact integer-cent addition (§6.4).
    st_tx = sum(int(e.get("amount", 0) or 0) for e in taxable)
    st_ntx = sum(int(e.get("amount", 0) or 0) for e in non_taxable)

    total_hours = sum(float(f.get("hours") or 0) for f in fees)
    rates = {r for f in fees if (r := int(f.get("rate") or 0)) > 0}
    taux_horaire = fmt_cents(next(iter(rates))) if len(rates) == 1 else ""

    return {
        "facture.numero": invoice.get("invoice_number", ""),
        "facture.date": _date_str(invoice.get("date"), lang=lang),
        "facture.date_echeance": _date_str(invoice.get("due_date"), lang=lang),
        "facture.sous_total_honoraires": fmt_cents(invoice.get("subtotal_fees", 0)),
        "facture.sous_total_debours_tx": fmt_cents(st_tx),
        "facture.sous_total_debours_ntx": fmt_cents(st_ntx),
        "facture.total_honoraires": fmt_cents(invoice.get("subtotal_fees", 0)),
        "facture.total_debours_tx": fmt_cents(st_tx),
        "facture.total_debours_ntx": fmt_cents(st_ntx),
        "facture.total_avant_taxes": fmt_cents(invoice.get("subtotal", 0)),
        "facture.tps_taux": fmt_rate(invoice.get("gst_rate", 500), _GST_SCALE),
        "facture.tps_numero": invoice.get("gst_number", ""),
        "facture.tps_montant": fmt_cents(invoice.get("gst_amount", 0)),
        "facture.tvq_taux": fmt_rate(invoice.get("qst_rate", 9975), _QST_SCALE),
        "facture.tvq_numero": invoice.get("qst_number", ""),
        "facture.tvq_montant": fmt_cents(invoice.get("qst_amount", 0)),
        "facture.total_apres_taxes": fmt_cents(invoice.get("total", 0)),
        "facture.avances_fideicommis": fmt_cents_parens(
            invoice.get("retainer_applied", 0)
        ),
        "facture.solde": fmt_cents(invoice.get("amount_due", 0)),
        "facture.nombre_heures": fmt_hours(total_hours),
        "facture.taux_horaire": taux_horaire,
    }


def _build_rows(
    line_items: list[dict], *, lang: str = "fr"
) -> dict[str, list[dict[str, str]]]:
    """Split line items into the three region row-lists, in line-item order."""
    is_en = normalize_lang(lang) == "en"
    fmt_cents = format_cents_en if is_en else format_cents_fr
    fmt_hours = format_hours_en if is_en else format_hours_fr
    honoraire: list[dict] = []
    debours_tx: list[dict] = []
    debours_ntx: list[dict] = []
    for li in line_items:
        date_str = _date_str(li.get("date"), lang=lang)
        description = li.get("description", "") or ""
        if li.get("type") == "fee":
            honoraire.append({
                "h.date": date_str,
                "h.description": description,
                "h.temps": fmt_hours(li.get("hours") or 0),
            })
        else:
            row = {
                "d.date": date_str,
                "d.description": description,
                "d.cout": fmt_cents(li.get("amount", 0)),
            }
            (debours_tx if li.get("taxable", True) else debours_ntx).append(row)
    return {
        "ligne_honoraire": honoraire,
        "ligne_debours_tx": debours_tx,
        "ligne_debours_ntx": debours_ntx,
    }


def _payment_source(receipt: dict, *, lang: str = "fr") -> str:
    """« Fidéicommis » / « Trust » for a trust receipt, else its method wording."""
    is_en = normalize_lang(lang) == "en"
    if receipt.get("trust_transaction_id"):
        return PAYMENT_SOURCE_TRUST_EN if is_en else PAYMENT_SOURCE_TRUST
    labels = PAYMENT_SOURCE_LABELS_EN if is_en else PAYMENT_SOURCE_LABELS
    return labels.get(receipt.get("method") or "", labels["autre"])


def _write_off_source(write_off: dict, *, lang: str = "fr") -> str:
    """« Note de crédit 2026-NC001 » / « Credit note 2026-NC001 »."""
    prefix = (
        PAYMENT_SOURCE_CREDIT_NOTE_EN
        if normalize_lang(lang) == "en"
        else PAYMENT_SOURCE_CREDIT_NOTE
    )
    return f"{prefix} {write_off['credit_note_number']}"


def _payment_rows(
    invoice: dict,
    receipts: list[dict],
    write_offs: Iterable[dict] = (),
    *,
    lang: str = "fr",
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
    is_en = normalize_lang(lang) == "en"
    fmt_date = format_date_en if is_en else format_date_fr
    fmt_cents_parens = format_cents_en_parens if is_en else format_cents_fr_parens
    trust_label = PAYMENT_SOURCE_TRUST_EN if is_en else PAYMENT_SOURCE_TRUST
    entries = []
    retainer = int(invoice.get("retainer_applied", 0) or 0)
    if retainer > 0:
        entries.append((_as_date(invoice.get("date")), trust_label, retainer))
    for receipt in receipts:
        entries.append((
            _as_date(receipt.get("date")),
            _payment_source(receipt, lang=lang),
            int(receipt.get("amount", 0) or 0),
        ))
    for write_off in write_offs:
        entries.append((
            _as_date(write_off.get("date")),
            _write_off_source(write_off, lang=lang),
            int(write_off.get("amount", 0) or 0),
        ))
    entries.sort(key=lambda entry: entry[0] or date.min)
    return [
        {
            "p.date": fmt_date(day) if day else "",
            "p.source": source,
            "p.montant": fmt_cents_parens(amount),
        }
        for day, source, amount in entries
    ]


def _header_values(
    invoice: dict,
    *,
    firm: dict,
    destinataire: Optional[dict],
    dossier: Optional[dict],
    today: date,
    lang: str = "fr",
) -> dict[str, str]:
    """The header fields of a document an invoice fills — the note
    d'honoraires and, since lot 3d, the credit note: ONE resolution.

    ``destinataire`` is the client partie; when ``None`` (partie deleted) the
    billing_address snapshot is used so generation never fails (§6.6). Header
    namespaces via the Phase H catalog — both CANONICAL names and the flat
    ALIASES the procedures/letters gabarits use, so a note template can use
    the identical placeholders ({{numero_dossier}} as well as
    {{dossier.numero_cour}}). Resolving the whole set is cheap and pure;
    unused fields are ignored by the fill engine.
    """
    dest = destinataire or _partie_from_billing_address(invoice.get("billing_address") or {})
    return resolve_values(
        list(CATALOG) + list(FLAT_ALIASES),
        dossier=dossier,
        client=None,
        adverse=None,
        destinataire=dest,
        firm=firm or {},
        today=today,
        lang=lang,
    )


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
    lang: str = "fr",
) -> InvoiceContext:
    """Build the note-d'honoraires context from a stored invoice (§6).

    ``destinataire`` is the client partie (:func:`_header_values`). Header
    fields are resolved through the Phase H catalog by canonical name.
    ``payments`` adds the payment region, its flag and
    ``facture.solde_restant``; ``trust_balance_cents`` adds
    ``facture.solde_fideicommis`` (module docstring). Left ``None``, each
    leaves the context exactly as it was before it.
    """
    is_en = normalize_lang(lang) == "en"
    fmt_cents = format_cents_en if is_en else format_cents_fr
    values = _header_values(invoice, firm=firm, destinataire=destinataire,
                            dossier=dossier, today=today, lang=lang)
    values.update(_facture_values(invoice, line_items, lang=lang))

    # Hourly rate: prefer the uniform billed rate; when the line items are
    # mixed or rateless, fall back to the dossier's configured rate so the
    # note still shows a rate (§6.2).
    if not values.get("facture.taux_horaire") and dossier and dossier.get("hourly_rate"):
        values["facture.taux_horaire"] = fmt_cents(int(dossier["hourly_rate"]))

    rows = _build_rows(line_items, lang=lang)
    conditions = {
        "si_honoraires": bool(rows["ligne_honoraire"]),
        "si_debours_tx": bool(rows["ligne_debours_tx"]),
        "si_debours_ntx": bool(rows["ligne_debours_ntx"]),
    }
    if payments is not None:
        values[BALANCE_FIELD] = fmt_cents(payments.balance_cents)
        rows[PAYMENT_REGION] = _payment_rows(
            invoice, payments.receipts, payments.write_offs, lang=lang
        )
        conditions[PAYMENT_FLAG] = bool(rows[PAYMENT_REGION])
    if trust_balance_cents is not None:
        values[TRUST_BALANCE_FIELD] = fmt_cents(trust_balance_cents)
    return InvoiceContext(values=values, rows=rows, conditions=conditions)


def build_credit_note_context(
    invoice: dict,
    write_off: dict,
    *,
    firm: dict,
    destinataire: Optional[dict],
    dossier: Optional[dict],
    today: date,
    balance_cents: int,
    lang: str = "fr",
) -> InvoiceContext:
    """Build the credit note's context (lot 3d, module docstring) for one
    radiation *write_off* of *invoice* — an event the model validated
    (``models.invoice.write_off_events``), standing.

    The header fields of the note d'honoraires (:func:`_header_values`);
    the credit note's own :data:`CREDIT_NOTE_FIELDS` — its number, its date
    (the radiation's, a date-only value: its UTC calendar day, never
    ``to_mtl``), its reason as recorded ('' prints nothing), the amount
    before taxes (W − TPS − TVQ), the TPS and the TVQ it carries (0 when the
    radiation claims none) and its total W, taxes included —; and the
    invoice's ``CREDIT_NOTE_INVOICE_FIELDS``: those read from the invoice
    alone by :func:`_facture_values`, never restated, and *balance_cents*,
    its balance after the credit (``models.invoice.balance_of``). No row and
    no condition: the note d'honoraires' tables are not the credit note's.
    """
    is_en = normalize_lang(lang) == "en"
    fmt_cents = format_cents_en if is_en else format_cents_fr
    values = _header_values(invoice, firm=firm, destinataire=destinataire,
                            dossier=dossier, today=today, lang=lang)
    from_invoice = _facture_values(invoice, [], lang=lang)
    values.update({name: from_invoice[name]
                   for name in CREDIT_NOTE_INVOICE_FIELDS if name in from_invoice})
    values[BALANCE_FIELD] = fmt_cents(balance_cents)
    amount = int(write_off["amount"])
    gst = int(write_off["gst_amount"])
    qst = int(write_off["qst_amount"])
    values.update({
        "nc.numero": str(write_off["credit_note_number"]),
        "nc.date": _date_str(write_off.get("date"), lang=lang),
        "nc.motif": str(write_off.get("reason") or ""),
        "nc.montant_avant_taxes": fmt_cents(amount - gst - qst),
        "nc.tps": fmt_cents(gst),
        "nc.tvq": fmt_cents(qst),
        "nc.total": fmt_cents(amount),
    })
    return InvoiceContext(values=values, rows={}, conditions={})


def build_interest_notice_context(
    invoice: dict,
    interest_charge: dict,
    *,
    firm: dict,
    destinataire: Optional[dict],
    dossier: Optional[dict],
    today: date,
    balance_cents: int,
    lang: str = "fr",
) -> InvoiceContext:
    """Build the overdue interest notice's context (lot 4a) for one standing
    ``avis_interets`` event of *invoice*.
    """
    is_en = normalize_lang(lang) == "en"
    fmt_cents = format_cents_en if is_en else format_cents_fr
    fmt_rate = format_rate_en if is_en else format_rate_fr
    values = _header_values(invoice, firm=firm, destinataire=destinataire,
                            dossier=dossier, today=today, lang=lang)
    from_invoice = _facture_values(invoice, [], lang=lang)
    values.update({name: from_invoice[name]
                   for name in INTEREST_NOTICE_INVOICE_FIELDS if name in from_invoice})
    values[BALANCE_FIELD] = fmt_cents(balance_cents)
    values.update({
        "ai.numero": str(interest_charge["interest_notice_number"]),
        "ai.date": _date_str(interest_charge.get("date"), lang=lang),
        "ai.periode_debut": _date_str(interest_charge.get("period_start"), lang=lang),
        "ai.periode_fin": _date_str(interest_charge.get("period_end"), lang=lang),
        "ai.jours": str(int(interest_charge["days"])),
        "ai.capital": fmt_cents(int(interest_charge["principal"])),
        "ai.taux": fmt_rate(int(interest_charge["rate_bps"]), _GST_SCALE),
        "ai.montant": fmt_cents(int(interest_charge["amount"])),
        "ai.motif": str(interest_charge.get("reason") or ""),
    })
    return InvoiceContext(values=values, rows={}, conditions={})
