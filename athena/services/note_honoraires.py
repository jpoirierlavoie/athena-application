"""The note d'honoraires — ONE generation, for the web and the connector.

Hoisted out of ``routes/invoices.invoice_note_docx`` (lot 3a, step 2) so
that the « Note d'honoraires (Word) » button and the connector's future
generation tool (lot 3b) cannot print an invoice two ways. The route became
a request adapter: it calls :func:`generer_note_honoraires` and turns a
:class:`NoteRefusee` into its fragment. The pure pieces stay where they
were — ``utils/invoice_docx.build_invoice_context`` (the invoice → fill
context), the fill engine — and the save is ``services.gabarits.
save_generated``, the ONE uploader of every generated .docx.

The pipeline, in order — every refusal raises :class:`NoteRefusee` BEFORE
anything is written, and logs its ``generation_failed`` line here, so both
surfaces log identically:

1. the invoice and its line items through the STRICT reader
   (``get_invoice_with_items_strict``): a read failure refuses
   (``invoice_unreadable``) — the route used the fail-open reader, whose
   swallowed line-item failure printed the stored totals over EMPTY tables,
   a client-facing document that looked complete;
2. an ``annulée`` invoice is refused (``invoice_voided``);
3. line items absent under a non-zero subtotal refuse
   (``line_items_unreadable`` — ``models.invoice.line_items_missing``, the
   void's own rule: one rule for the web and the connector);
4. the template the lawyer DESIGNATED (``get_active_template
   ("note_honoraires")`` — never « the most recent », D11): none designated
   (``no_note_template``) and an unreadable store (``template_read_failed``)
   are two different answers;
5. the fill context — the dossier through the STRICT reader (a read
   failure refuses, ``dossier_unreadable``: the fail-open ``get_dossier``
   turned a blip into ``None``, and the note was filed with « [CHAMP
   MANQUANT : …] » in every dossier field while the call reported success
   — review of lot 3a, step 2), the client as before (a client gone falls
   back to the invoice's frozen billing snapshot), the payments when the
   template PRINTS them (``utils/invoice_docx.prints_payments``, 2026-11-08:
   the encaissements imputed on the invoice through the STRICT reader
   ``models/admin_ledger.list_invoice_receipts_strict`` — a read failure
   refuses, ``payments_unreadable``: « no payment » on a client document
   must never be a blip —, and the standing ones refused when they do not
   explain its payment state, ``models.invoice.payments_print_defect``:
   ``payments_mismatch``, ``paid_by_hand``; since lot 3a, its STANDING
   write-offs too, ``models.invoice.standing_write_offs`` — one « Note de
   crédit <n°> » row each since lot 3b, a forgiveness shown to the client
   (the lawyer's decision D2 of 2026-11-11, refined by his decision 8:
   a price reduction its credit note documents; never a refusal): the
   printed balance reaches
   zero, and a cancelled write-off prints neither row; a template without a
   payment field reads nothing, and keeps the context and fingerprint it
   always had), the client's trust balance when the template prints it
   (``utils/invoice_docx.prints_trust_balance``: the book balance the
   dossier just read strictly carries for the client whose funds stand
   against the invoice, ``models.trust.invoice_trust_client`` — refused,
   ``trust_balance_unavailable``, when none can be named) —, the values,
   and their FINGERPRINT (:func:`fill_fingerprint` — the template's id and
   version plus a SHA-256 of everything the fill prints);
6. unless *regenerate*: a note already generated from THIS invoice with the
   SAME fingerprint is returned instead of a duplicate (``reused=True``,
   nothing written, no log line — the connector's own ``mcp_write`` says
   what its call did). The lookup is strict (``find_generated_for_invoice``
   raises): « none found » on a transient error would file a duplicate, so
   it refuses (``generated_lookup_failed``);
7. the bytes of the file THIS template record names
   (``template_file_bytes`` — never a re-read that could print version N+1
   under the name of N), then the fill (``template.fill`` span);
8. the Storage uid — obtained only now, so nothing is written for a
   request that could not file the note — through *resolve_uid*
   (``storage_identity.owner_uid`` by default: a caller with no browser
   session; the web passes ``request_uid``), and the save into the
   dossier's « Factures » system folder (« Mandat › Factures », role
   ``factures`` — created with « Mandat » on first use), by its ROLE, the
   document carrying ``source_invoice_id`` + ``generation_fingerprint``. A
   failure to obtain it REFUSES (``factures_unavailable``), never a save at
   the dossier root. The note keeps its « … Projet … » name and its
   « correspondance » category: only its folder moved, with the default
   folder tree (until then it went to « Projets », beside the gabarits).
   A REUSED note (step 6) stays wherever it is filed — the lookup never
   looks at folders, so a note filed in « Projets » before the tree is
   still found, and returned, not refiled.

The web passes ``regenerate=True``: its button has always filed a new note
on every click, and keeps doing so (the note it files is fingerprinted, so
the connector finds it). Never reads ``flask.session`` — a sweep pins it.

Declared exception to « every refusal … BEFORE anything is written » (the
« incertain » lot, 2026-11-06): ``save_uncertain`` — the save of step 8
RAISED and its record could not be read back, so the note MAY be filed.
Its sentence (:data:`SAVE_UNCERTAIN`) sends the lawyer to « Mandat ›
Factures » before another click, the web keeping its every-click rule; the
connector keeps its idempotency claim and words its own.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

from models.admin_ledger import list_invoice_receipts_strict, receipt_stands
from models.doc_template import (
    TemplateReadError,
    get_active_template,
    template_file_bytes,
)
from models.document import find_generated_for_invoice
from models.dossier import get_dossier_strict
from models.folder import (
    SYSTEM_FOLDER_NAMES,
    SYSTEM_FOLDER_PARENTS,
    SYSTEM_ROLE_FACTURES,
)
from models.invoice import (
    balance_of,
    get_invoice_with_items_strict,
    line_items_missing,
    payments_print_defect,
    standing_write_offs,
)
from models.partie import get_partie
from models.trust import client_book_balance, invoice_trust_client
from services.gabarits import (
    FACTURES,
    GenerationRefused,
    projet_names,
    save_generated,
)
from utils import storage_identity
from utils.cabinet import cabinet_dict
from utils.deadlines import today_mtl
from utils.docx_fill import DocxFillError, fill_docx
from utils.invoice_docx import (
    PAYMENT_REGION,
    InvoicePayments,
    build_invoice_context,
    prints_payments,
    prints_trust_balance,
)
from utils.logging_setup import log_template_event, log_unexpected
from utils.template_fields import classify_placeholders, fallback_value, manual_value
from utils.tracing_setup import add_attributes, span

NOTE_KIND = "note_honoraires"

# French messages — the route's own wording, kept VERBATIM where it existed.
INVOICE_NOT_FOUND = "Facture introuvable."
INVOICE_UNREADABLE = (
    "La facture ou ses lignes n'ont pas pu être lues — lecture impossible. "
    "Rien n'a été généré : réessayez dans un instant."
)
INVOICE_VOIDED = (
    "Impossible de générer une note d'honoraires pour une facture annulée."
)
DOSSIER_UNREADABLE = (
    "Le dossier de cette facture n'a pas pu être lu — lecture impossible. "
    "Rien n'a été généré : réessayez dans un instant."
)
LINE_ITEMS_UNREADABLE = (
    "Les lignes de cette facture sont introuvables alors que son sous-total "
    "n'est pas nul : la note imprimerait des totaux au-dessus de tableaux "
    "vides. Rien n'a été généré."
)
NO_ACTIVE_NOTE_HONORAIRES = (
    "Aucun gabarit « Note d'honoraires » n'est désigné comme actif : "
    "désignez-en un dans Gabarits."
)
NOTE_TEMPLATE_UNREADABLE = (
    "Le gabarit actif des notes d'honoraires n'a pas pu être lu — lecture "
    "impossible. Rien n'a été généré : réessayez dans un instant."
)
GENERATED_LOOKUP_FAILED = (
    "Les notes d'honoraires déjà générées pour cette facture n'ont pas pu "
    "être lues. Rien n'a été généré, pour ne pas en créer une en double : "
    "réessayez dans un instant."
)
TEMPLATE_FILE_UNAVAILABLE = (
    "Le fichier du gabarit est introuvable. Téléversez-le à nouveau."
)
PAYMENTS_UNREADABLE = (
    "Les paiements inscrits sur cette facture n'ont pas pu être lus — "
    "lecture impossible. Rien n'a été généré : réessayez dans un instant."
)
# models.invoice.payments_print_defect's codes → their sentence.
PAYMENT_DEFECT_MESSAGES = {
    "payments_mismatch": (
        "Les encaissements inscrits au registre d'administration pour cette "
        "facture ne concordent pas avec son montant payé : la note "
        "imprimerait des paiements qui ne mènent pas à son solde. Rien n'a "
        "été généré : vérifiez les encaissements de la facture."
    ),
    "paid_by_hand": (
        "Cette facture est marquée « payée » sans que ses paiements soient "
        "tous inscrits au registre d'administration : la note imprimerait un "
        "solde restant dû. Rien n'a été généré : inscrivez les encaissements "
        "manquants."
    ),
}
TRUST_BALANCE_UNAVAILABLE = (
    "Le solde en fidéicommis ne peut pas être établi pour cette facture : "
    "elle ne nomme pas de client et son dossier n'en compte pas un seul. "
    "Rien n'a été généré."
)
TEMPLATE_INVALID = "Le gabarit est invalide et n'a pas pu être rempli."
FILL_ERROR = "Erreur lors de la génération. Veuillez réessayer."
SAVE_FAILED = "Erreur lors de l'enregistrement."
# Where the note is filed, as the browser shows it (« Mandat › Factures »),
# from the folder registry — never a name written here.
_FACTURES_PATH = " › ".join(
    SYSTEM_FOLDER_NAMES[role]
    for role in (SYSTEM_FOLDER_PARENTS[SYSTEM_ROLE_FACTURES],
                 SYSTEM_ROLE_FACTURES)
    if role
)
# ``save_uncertain`` — the one refusal that does NOT say nothing was
# written: the note MAY be filed (module docstring). The web button files a
# new note on every click, so the sentence sends the lawyer to look first.
SAVE_UNCERTAIN = (
    "L'enregistrement de la note d'honoraires n'a pas pu être confirmé : "
    "elle a peut-être été versée au dossier. Vérifiez « "
    f"{_FACTURES_PATH} » avant de la générer de nouveau — chaque clic en "
    "verse une nouvelle."
)


class NoteRefusee(Exception):
    """A note d'honoraires that must not be generated — nothing was written,
    except under ``save_uncertain``, whose note MAY be filed (module
    docstring: its sentence never invites a blind retry).

    ``reason`` is machine-stable (the ``generation_failed`` vocabulary of
    OBSERVABILITY.md); ``message`` is French and never quotes a value.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass
class NoteGeneree:
    """A note d'honoraires the call produced — or found already filed."""

    document: dict
    template: dict
    fingerprint: str
    reused: bool = False
    counts: dict = field(default_factory=dict)
    #: The invoice printed, as read (lot 3b: the connector names it).
    invoice: dict = field(default_factory=dict)
    #: The folder the note landed in — « Mandat › Factures » (role
    #: ``factures``) — or ``None`` when it was REUSED (the caller reads the
    #: filed document's own ``folder_id``: a note filed before the default
    #: tree stays in « Projets »).
    folder: Optional[dict] = None


def assemble_note_values(template: dict, ctx) -> dict[str, str]:
    """One value per template placeholder: ctx (facture.* + resolved header
    fields) → auto fallback → manual default. Row-scoped (h./d.) and
    passthrough names are omitted (rows fill the former; the latter stay
    literal). Moved verbatim from ``routes/invoices._assemble_note_values``.
    """
    placeholders = template.get("placeholders", [])
    classification = classify_placeholders(placeholders)
    values: dict[str, str] = {}
    for name in placeholders:
        if name in ctx.values:
            values[name] = ctx.values[name]
        elif name in classification.auto:
            values[name] = fallback_value(name, is_auto=True)
        elif name in classification.manual:
            # manual_value(), never MANUAL_FIELDS[name]: manual names match
            # case-insensitively since Sept. 2026, so a {{PRIVILÈGE}} in a note
            # template would KeyError on the bare index — on a path that never
            # prompts, so nothing would hint at the cause.
            values[name] = manual_value(name)
        # else: passthrough / row-scoped → omit
    return values


def fill_fingerprint(template: dict, values: dict, ctx) -> str:
    """The SHA-256 of what a note prints: the template's id and version,
    and every value, row and condition the fill receives.

    Two generations with the same fingerprint produce the same document:
    the same bytes of the same template (a replaced file is a new version),
    filled with the same values. What the invoice's ``updated_at`` would
    miss (a replaced letterhead, an edited client address) and what it
    would over-report (a status change the note does not print) are both
    decided by the CONTENT here.
    """
    payload = {
        "template_id": str(template.get("id") or ""),
        "template_version": template.get("version", 1),
        "values": values,
        "rows": ctx.rows,
        "conditions": ctx.conditions,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _refuse(reason: str, message: str, **fields) -> NoteRefusee:
    log_template_event("generation_failed", reason=reason, **fields)
    return NoteRefusee(reason, message)


def generer_note_honoraires(
    invoice_id: str,
    *,
    regenerate: bool = False,
    resolve_uid: Optional[Callable[[], str]] = None,
    today: Optional[date] = None,
    generated_by: str = "",
) -> NoteGeneree:
    """Generate *invoice_id*'s note d'honoraires into « Mandat › Factures ».

    Raises :class:`NoteRefusee` (nothing written). See the module
    docstring for the order of the checks. *regenerate* files a new note
    even when an identical one exists; *resolve_uid* — how the Storage uid
    is obtained, called once the note is filled (step 8) — defaults to
    ``storage_identity.owner_uid``; *today* defaults to Montréal's.
    *generated_by* — who asked, appended to the document's provenance
    (``genere_depuis``): the connector passes « par Claude (connecteur) »,
    the phrase its INSTRUCTIONS promise every document it generates
    carries; the web passes nothing (lot 3b).
    """
    try:
        invoice, items = get_invoice_with_items_strict(invoice_id)
    except Exception:
        log_unexpected("note d'honoraires: invoice read failed",
                       invoice_id=invoice_id)
        raise _refuse("invoice_unreadable", INVOICE_UNREADABLE,
                      invoice_id=invoice_id)
    if not invoice:
        raise _refuse("invoice_not_found", INVOICE_NOT_FOUND)
    if invoice.get("status") == "annulée":
        raise _refuse("invoice_voided", INVOICE_VOIDED)
    if line_items_missing(invoice, items):
        raise _refuse("line_items_unreadable", LINE_ITEMS_UNREADABLE,
                      invoice_id=invoice_id)

    # The DESIGNATED template (D11, lot 2A T3) — never « the most recent »:
    # an edit of another template can no longer switch the letterhead of
    # every client's note. None designated and an unreadable store are two
    # different answers: the first names the fix, the second says retry.
    try:
        template = get_active_template(NOTE_KIND)
    except TemplateReadError:
        raise _refuse("template_read_failed", NOTE_TEMPLATE_UNREADABLE)
    if not template:
        raise _refuse("no_note_template", NO_ACTIVE_NOTE_HONORAIRES)
    template_id = template["id"]

    dossier_id = invoice.get("dossier_id", "")
    # STRICT: this read decides what a client-facing document prints, and
    # the note is filed on its answer. « None » stays « no such dossier »
    # (the save then refuses: « Factures » has no dossier to live in).
    try:
        dossier = get_dossier_strict(dossier_id) if dossier_id else None
    except Exception:
        log_unexpected("note d'honoraires: dossier read failed",
                       invoice_id=invoice_id)
        raise _refuse("dossier_unreadable", DOSSIER_UNREADABLE,
                      template_id=template_id, invoice_id=invoice_id)
    client_id = invoice.get("client_id", "")
    client = get_partie(client_id) if client_id else None
    day = today or today_mtl()

    # The payments — only for a template that PRINTS them. STRICT: the note
    # is filed on this answer, and « no payment » on a read failure would
    # print a blip as an unpaid invoice. The standing rows must explain the
    # invoice's payment state, or the printed rows would not lead to the
    # printed balance (models.invoice.payments_print_defect). The standing
    # write-offs print beside them (lot 3a, D2): balance_of subtracts the
    # very events these rows print.
    payments = None
    if prints_payments(template.get("placeholders")):
        try:
            standing = [row for row in list_invoice_receipts_strict(invoice_id)
                        if receipt_stands(row)]
        except Exception:
            log_unexpected("note d'honoraires: payments read failed",
                           invoice_id=invoice_id)
            raise _refuse("payments_unreadable", PAYMENTS_UNREADABLE,
                          template_id=template_id, invoice_id=invoice_id)
        defect = payments_print_defect(invoice, standing)
        if defect:
            raise _refuse(defect, PAYMENT_DEFECT_MESSAGES[defect],
                          template_id=template_id, invoice_id=invoice_id)
        payments = InvoicePayments(receipts=standing,
                                   balance_cents=balance_of(invoice),
                                   write_offs=standing_write_offs(invoice))

    # The client's trust balance — only for a template that PRINTS it: the
    # book balance the dossier read above carries (no second read), for the
    # client whose funds a fee payment on this invoice draws. With no
    # dossier the save below refuses anyway (« Factures » has none to live
    # in).
    trust_balance = None
    if dossier is not None and prints_trust_balance(template.get("placeholders")):
        trust_client = invoice_trust_client(invoice, dossier)
        if not trust_client:
            raise _refuse("trust_balance_unavailable", TRUST_BALANCE_UNAVAILABLE,
                          template_id=template_id, invoice_id=invoice_id)
        trust_balance = client_book_balance(dossier, trust_client)

    ctx = build_invoice_context(
        invoice, items,
        firm=cabinet_dict(), destinataire=client, dossier=dossier, today=day,
        payments=payments, trust_balance_cents=trust_balance,
    )
    values = assemble_note_values(template, ctx)
    counts = {
        "rows_honoraire": len(ctx.rows["ligne_honoraire"]),
        "rows_debours_tx": len(ctx.rows["ligne_debours_tx"]),
        "rows_debours_ntx": len(ctx.rows["ligne_debours_ntx"]),
    }
    if PAYMENT_REGION in ctx.rows:
        counts["rows_paiement"] = len(ctx.rows[PAYMENT_REGION])
    add_attributes(template_id=template_id, invoice_id=invoice_id, **counts)
    fingerprint = fill_fingerprint(template, values, ctx)

    if not regenerate:
        try:
            filed = find_generated_for_invoice(invoice_id)
        except Exception:
            log_unexpected("note d'honoraires: generated notes lookup failed",
                           invoice_id=invoice_id)
            raise _refuse("generated_lookup_failed", GENERATED_LOOKUP_FAILED,
                          template_id=template_id, invoice_id=invoice_id)
        for existing in filed:
            if (existing.get("generation_fingerprint") == fingerprint
                    and existing.get("dossier_id") == dossier_id):
                return NoteGeneree(document=existing, template=template,
                                   fingerprint=fingerprint, reused=True,
                                   counts=counts, invoice=invoice)

    docx_bytes = template_file_bytes(template)
    if docx_bytes is None:
        raise _refuse("template_file_unavailable", TEMPLATE_FILE_UNAVAILABLE,
                      template_id=template_id)

    try:
        with span("template.fill", template_id=template_id, invoice_id=invoice_id):
            filled = fill_docx(
                docx_bytes, values,
                rows_by_region=ctx.rows, conditions=ctx.conditions,
            )
    except DocxFillError as exc:
        reason = "unbalanced_condition" if "conditionnelle" in str(exc) else "fill_error"
        raise _refuse(reason, TEMPLATE_INVALID, template_id=template_id)
    except Exception:
        log_unexpected("note-honoraires fill failed", template_id=template_id)
        raise _refuse("fill_error", FILL_ERROR, template_id=template_id)

    save_fields = {"template_id": template_id, "dossier_id": dossier_id,
                   "invoice_id": invoice_id}
    # The uid first, then « Factures »: nothing is written — not even the
    # folder — for a request that could not file the note anyway.
    try:
        uid = (resolve_uid or storage_identity.owner_uid)()
    except storage_identity.StorageIdentityUnavailable as exc:
        raise _refuse("save_failed", storage_identity.public_message(exc), **save_fields)

    invoice_number = invoice.get("invoice_number", "")
    tmpl_base = template.get("name") or "Note d'honoraires"
    display, out_name = projet_names(
        dossier, f"{tmpl_base} {invoice_number}".strip(), day)
    try:
        # « Mandat › Factures », found by its ROLE at its deterministic id
        # (the default folder tree): a failure REFUSES
        # (``factures_unavailable``), never a save at the dossier root.
        doc, landed = save_generated(
            dossier={"id": dossier_id,
                     "file_number": invoice.get("dossier_file_number", "")},
            filled=filled,
            uid=uid,
            display_name=display,
            filename=out_name,
            category="correspondance",
            genere_depuis=" ".join(
                part for part in (
                    f"Générée depuis la facture {invoice_number}".strip(),
                    generated_by.strip(),
                ) if part),
            tags=("note_honoraires",),
            folder=FACTURES,
            generated_from_invoice={"invoice_id": invoice_id,
                                    "fingerprint": fingerprint},
        )
    except GenerationRefused as refusal:
        if refusal.reason == "save_uncertain":
            # The note MAY be filed: its own sentence, never « réessayez ».
            raise _refuse("save_uncertain", SAVE_UNCERTAIN, **save_fields)
        raise _refuse(refusal.reason, refusal.message or SAVE_FAILED,
                      **save_fields)

    log_template_event("document_generated", template_id=template_id,
                       dossier_id=dossier_id, saved_document_id=doc["id"],
                       invoice_id=invoice_id, source="facture", **counts)
    return NoteGeneree(document=doc, template=template,
                       fingerprint=fingerprint, reused=False, counts=counts,
                       invoice=invoice, folder=landed)
