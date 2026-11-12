"""The credit note — the Word document of one write-off (lot 3d).

Lot 3d of « Factures : retard, radiation, intérêts », on the lawyer's
decision 8 of 2026-11-12: toward the client a write-off (« radiation ») is a
PRICE REDUCTION documented by a credit note, numbered « AAAA-NC### » by the
write-off door itself (``models.invoice.write_off_remainder``, lot 3b). This
service prints that credit note, on demand, from the gabarit of kind
« note_credit » the lawyer DESIGNATED « actif » — modelled on, and built
from, ``services/note_honoraires``: the same fill engine (string
substitution only, every other zip entry byte-identical, no new part), the
same fingerprint (``fill_fingerprint``, given the credit note's subject),
the same save (``services.gabarits.save_generated``) into the same « Mandat
› Factures » system folder, the document linked to its invoice
(``source_invoice_id`` + ``generation_fingerprint``) exactly as a note
d'honoraires is — never a second filing logic. Its one entry point is the
invoice sheet's « Générer la note de crédit », on each STANDING radiation
(``routes/invoices``); the connector has no tool for it.

The pipeline, in order — every refusal raises ``NoteRefusee`` BEFORE
anything is written, and logs its ``generation_failed`` line here
(``source="note_credit"``):

1. the invoice through the STRICT reader (``get_invoice_strict``): a read
   failure refuses (``invoice_unreadable``), an absent one
   (``invoice_not_found``); a voided one (``invoice_voided``);
2. the radiation, through the one judge of the events
   (``models.invoice.write_off_events``): a malformed array refuses
   (``write_offs_unreadable``), an id that names no radiation of THIS
   invoice — a contre_passation included — too (``write_off_not_found``),
   and a radiation that no longer STANDS (``standing_write_offs``) gets no
   new credit note (``write_off_cancelled``): its credit note keeps its
   number, « annulée »;
3. the invoice's balance after the credit (``models.invoice.balance_of``,
   its one authority — zero while the radiation stands): a stored amount
   it cannot read refuses by name (``montant_illisible``);
4. the template the lawyer DESIGNATED (``get_active_template
   ("note_credit")`` — never « the most recent »): none designated
   (``no_credit_note_template``) and an unreadable store
   (``template_read_failed``) are two different answers;
5. the dossier through the STRICT reader (``dossier_unreadable``), the
   client as the note d'honoraires reads it (a client gone falls back to
   the invoice's frozen billing snapshot), the context
   (``utils/invoice_docx.build_credit_note_context``), the values
   (``assemble_note_values``) and their fingerprint;
6. unless *regenerate*: a credit note already filed for THIS radiation, in
   THIS dossier, with the SAME fingerprint is returned instead of a
   duplicate (``reused=True``, nothing written, no log line); the lookup is
   strict (``generated_lookup_failed``);
7. the bytes of the file THIS template record names, then the fill
   (``template.fill`` span);
8. the Storage uid — obtained only now, through *resolve_uid* (the web
   passes ``request_uid``) — and the save into « Mandat › Factures », by its
   ROLE (``factures_unavailable`` when it cannot be obtained: never a save
   at the dossier root).

Unlike the note d'honoraires' button, the web reuses: a second click files
no second copy of an unchanged credit note, and a click after an
uncertain save finds the one that may have landed. Declared exception to
« every refusal … BEFORE anything is written », as there: ``save_uncertain``
— the note MAY be filed. Never reads ``flask.session`` — a sweep pins it.
"""

from __future__ import annotations

from datetime import date
from typing import Callable, Optional

from models.doc_template import (
    ACTIVE_KIND_NAMES,
    TemplateReadError,
    get_active_template,
    template_file_bytes,
)
from models.document import CREDIT_NOTE_TAG, find_generated_for_invoice
from models.dossier import get_dossier_strict
from models.invoice import (
    WriteOffsUnreadable,
    balance_of,
    get_invoice_strict,
    standing_write_offs,
    unreadable_amount_message,
    unreadable_balance_field,
    write_off_events,
)
from models.partie import get_partie
from services.gabarits import (
    FACTURES,
    GenerationRefused,
    projet_names,
    save_generated,
)
from services.note_honoraires import (
    _FACTURES_PATH,
    DOSSIER_UNREADABLE,
    FILL_ERROR,
    INVOICE_NOT_FOUND,
    SAVE_FAILED,
    TEMPLATE_FILE_UNAVAILABLE,
    TEMPLATE_INVALID,
    NoteGeneree,
    NoteRefusee,
    assemble_note_values,
    fill_fingerprint,
)
from utils import storage_identity
from utils.cabinet import cabinet_dict
from utils.deadlines import today_mtl
from utils.docx_fill import DocxFillError, fill_docx
from utils.invoice_docx import build_credit_note_context
from utils.logging_setup import log_template_event, log_unexpected
from utils.tracing_setup import add_attributes, span

NOTE_KIND = "note_credit"
#: The ``source`` of every line this service logs (OBSERVABILITY.md).
SOURCE = "note_credit"
#: The tag the filed document carries — what sets it apart from a note
#: d'honoraires, which shares its invoice link (``source_invoice_id``).
TAG = CREDIT_NOTE_TAG

INVOICE_UNREADABLE = (
    "La facture n'a pas pu être lue — lecture impossible. Rien n'a été "
    "généré : réessayez dans un instant."
)
INVOICE_VOIDED = (
    "Impossible de générer une note de crédit pour une facture annulée."
)
WRITE_OFFS_UNREADABLE = (
    "Les radiations enregistrées sur cette facture sont illisibles : la "
    "facture doit d'abord être réparée. Rien n'a été généré."
)
WRITE_OFF_NOT_FOUND = (
    "Cette radiation est introuvable sur la facture : seule une radiation a "
    "sa note de crédit, jamais son annulation. Rien n'a été généré."
)
WRITE_OFF_CANCELLED = (
    "Cette radiation a été annulée : sa note de crédit {number} est annulée, "
    "et aucune note de crédit ne se génère plus pour elle. Rien n'a été "
    "généré."
)
BALANCE_CONSEQUENCE = (
    "le solde de la facture après la note de crédit ne peut pas être établi"
)
NO_ACTIVE_NOTE_CREDIT = (
    f"Aucun gabarit « {ACTIVE_KIND_NAMES[NOTE_KIND]} » n'est désigné comme "
    "actif : désignez-en un dans Gabarits."
)
NOTE_CREDIT_TEMPLATE_UNREADABLE = (
    "Le gabarit actif des notes de crédit n'a pas pu être lu — lecture "
    "impossible. Rien n'a été généré : réessayez dans un instant."
)
GENERATED_LOOKUP_FAILED = (
    "Les notes de crédit déjà versées pour cette facture n'ont pas pu être "
    "lues. Rien n'a été généré, pour ne pas en verser une en double : "
    "réessayez dans un instant."
)
# ``save_uncertain`` — the one refusal that does NOT say nothing was
# written. The web reuses an identical credit note, so another click finds
# the one that may have landed instead of filing a second.
SAVE_UNCERTAIN = (
    "L'enregistrement de la note de crédit n'a pas pu être confirmé : elle a "
    f"peut-être été versée au dossier, sous « {_FACTURES_PATH} ». Un nouveau "
    "clic la retrouve si elle l'a été : il n'en verse pas une seconde."
)


def _refuse(reason: str, message: str, **fields) -> NoteRefusee:
    log_template_event("generation_failed", reason=reason, source=SOURCE,
                       **fields)
    return NoteRefusee(reason, message)


def _radiation(invoice: dict, write_off_id: str, invoice_id: str) -> dict:
    """The radiation *write_off_id* names on *invoice*, STANDING — or the
    refusal that says why not (module docstring, step 2)."""
    try:
        events = write_off_events(invoice)
        standing = {event["id"] for event in standing_write_offs(invoice)}
    except WriteOffsUnreadable:
        raise _refuse("write_offs_unreadable", WRITE_OFFS_UNREADABLE,
                      invoice_id=invoice_id)
    radiation = next((event for event in events
                      if event["kind"] == "radiation"
                      and event["id"] == write_off_id), None)
    if radiation is None:
        raise _refuse("write_off_not_found", WRITE_OFF_NOT_FOUND,
                      invoice_id=invoice_id)
    if radiation["id"] not in standing:
        raise _refuse(
            "write_off_cancelled",
            WRITE_OFF_CANCELLED.format(number=radiation["credit_note_number"]),
            invoice_id=invoice_id, write_off_id=radiation["id"])
    return radiation


def generer_note_credit(
    invoice_id: str,
    write_off_id: str,
    *,
    regenerate: bool = False,
    resolve_uid: Optional[Callable[[], str]] = None,
    today: Optional[date] = None,
) -> NoteGeneree:
    """Generate the credit note of the radiation *write_off_id* of
    *invoice_id* into « Mandat › Factures ».

    Raises ``NoteRefusee`` (nothing written, but under ``save_uncertain``).
    See the module docstring for the order of the checks. *regenerate*
    files a new credit note even when an identical one is filed;
    *resolve_uid* — how the Storage uid is obtained, called once the note
    is filled — defaults to ``storage_identity.owner_uid``; *today* (the
    header's ``date.*`` and the document's name) defaults to Montréal's.
    """
    try:
        invoice = get_invoice_strict(invoice_id)
    except Exception:
        log_unexpected("note de crédit: invoice read failed",
                       invoice_id=invoice_id)
        raise _refuse("invoice_unreadable", INVOICE_UNREADABLE,
                      invoice_id=invoice_id)
    if not invoice:
        raise _refuse("invoice_not_found", INVOICE_NOT_FOUND)
    if invoice.get("status") == "annulée":
        raise _refuse("invoice_voided", INVOICE_VOIDED, invoice_id=invoice_id)
    radiation = _radiation(invoice, write_off_id, invoice_id)
    ids = {"invoice_id": invoice_id, "write_off_id": radiation["id"]}

    # The balance after the credit — balance_of, the one authority. A
    # stored amount it cannot read is named, never quoted, never guessed.
    unreadable = unreadable_balance_field(invoice)
    if unreadable is not None:
        raise _refuse(
            "montant_illisible",
            f"{unreadable_amount_message(unreadable, BALANCE_CONSEQUENCE)} "
            "Rien n'a été généré.", **ids)
    balance = balance_of(invoice)

    # The DESIGNATED template (D11) — never « the most recent ». None
    # designated names the fix; an unreadable store says retry.
    try:
        template = get_active_template(NOTE_KIND)
    except TemplateReadError:
        raise _refuse("template_read_failed", NOTE_CREDIT_TEMPLATE_UNREADABLE,
                      **ids)
    if not template:
        raise _refuse("no_credit_note_template", NO_ACTIVE_NOTE_CREDIT, **ids)
    template_id = template["id"]

    dossier_id = invoice.get("dossier_id", "")
    # STRICT: a client-facing document is filed on this answer (the note
    # d'honoraires' own rule). « None » stays « no such dossier »: the save
    # then refuses, « Factures » having no dossier to live in.
    try:
        dossier = get_dossier_strict(dossier_id) if dossier_id else None
    except Exception:
        log_unexpected("note de crédit: dossier read failed",
                       invoice_id=invoice_id)
        raise _refuse("dossier_unreadable", DOSSIER_UNREADABLE,
                      template_id=template_id, **ids)
    client_id = invoice.get("client_id", "")
    client = get_partie(client_id) if client_id else None
    day = today or today_mtl()

    ctx = build_credit_note_context(
        invoice, radiation, firm=cabinet_dict(), destinataire=client,
        dossier=dossier, today=day, balance_cents=balance)
    values = assemble_note_values(template, ctx)
    add_attributes(template_id=template_id, invoice_id=invoice_id)
    fingerprint = fill_fingerprint(
        template, values, ctx,
        subject={"kind": NOTE_KIND, "write_off_id": radiation["id"]})

    if not regenerate:
        try:
            filed = find_generated_for_invoice(invoice_id)
        except Exception:
            log_unexpected("note de crédit: generated notes lookup failed",
                           invoice_id=invoice_id)
            raise _refuse("generated_lookup_failed", GENERATED_LOOKUP_FAILED,
                          template_id=template_id, **ids)
        for existing in filed:
            if (existing.get("generation_fingerprint") == fingerprint
                    and existing.get("dossier_id") == dossier_id):
                return NoteGeneree(document=existing, template=template,
                                   fingerprint=fingerprint, reused=True,
                                   invoice=invoice, write_off=radiation)

    docx_bytes = template_file_bytes(template)
    if docx_bytes is None:
        raise _refuse("template_file_unavailable", TEMPLATE_FILE_UNAVAILABLE,
                      template_id=template_id, **ids)

    try:
        with span("template.fill", template_id=template_id, invoice_id=invoice_id):
            filled = fill_docx(docx_bytes, values)
    except DocxFillError as exc:
        reason = "unbalanced_condition" if "conditionnelle" in str(exc) else "fill_error"
        raise _refuse(reason, TEMPLATE_INVALID, template_id=template_id, **ids)
    except Exception:
        log_unexpected("note de crédit fill failed", template_id=template_id)
        raise _refuse("fill_error", FILL_ERROR, template_id=template_id, **ids)

    save_fields = {"template_id": template_id, "dossier_id": dossier_id, **ids}
    # The uid first, then « Factures »: nothing is written — not even the
    # folder — for a request that could not file the note anyway.
    try:
        uid = (resolve_uid or storage_identity.owner_uid)()
    except storage_identity.StorageIdentityUnavailable as exc:
        raise _refuse("save_failed", storage_identity.public_message(exc),
                      **save_fields)

    number = radiation["credit_note_number"]
    invoice_number = invoice.get("invoice_number", "")
    tmpl_base = template.get("name") or ACTIVE_KIND_NAMES[NOTE_KIND]
    display, out_name = projet_names(dossier, f"{tmpl_base} {number}".strip(), day)
    try:
        doc, landed = save_generated(
            dossier={"id": dossier_id,
                     "file_number": invoice.get("dossier_file_number", "")},
            filled=filled,
            uid=uid,
            display_name=display,
            filename=out_name,
            category="correspondance",
            genere_depuis=(f"Générée depuis la radiation de la facture "
                           f"{invoice_number} (note de crédit {number})"),
            tags=(TAG,),
            folder=FACTURES,
            generated_from_invoice={"invoice_id": invoice_id,
                                    "fingerprint": fingerprint},
        )
    except GenerationRefused as refusal:
        if refusal.reason == "save_uncertain":
            raise _refuse("save_uncertain", SAVE_UNCERTAIN, **save_fields)
        raise _refuse(refusal.reason, refusal.message or SAVE_FAILED,
                      **save_fields)

    log_template_event("document_generated", template_id=template_id,
                       dossier_id=dossier_id, saved_document_id=doc["id"],
                       source=SOURCE, **ids)
    return NoteGeneree(document=doc, template=template, fingerprint=fingerprint,
                       reused=False, invoice=invoice, folder=landed,
                       write_off=radiation)
