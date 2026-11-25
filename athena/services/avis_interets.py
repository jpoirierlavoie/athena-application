"""Generate the Word document (.docx) of a standing overdue interest notice.

Mirrors ``services/note_credit.py`` strictly:

* fills the active template of kind ``avis_interets`` (never a draft/latest);
* refuses before any write on unreadable invoice, cancelled invoice, missing or
  already-cancelled notice, unreadable balance, missing active template, or
  unreadable dossier;
* computes a deterministic fingerprint over the notice + template + resolved
  values and reuses an already-filed identical document in ``Mandat › Factures``
  unless ``regenerate=True``;
* writes bytes through ``services.gabarits.save_generated`` under ``resolve_uid``
  and logs PII-free ``document_generated`` / ``generation_failed`` events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

from models.doc_template import (
    ACTIVE_KIND_NAMES,
    TemplateReadError,
    get_active_template,
    has_en,
    template_file_bytes,
)
from models.document import INTEREST_NOTICE_TAG, find_generated_for_invoice
from models.dossier import get_dossier_strict
from models.invoice import (
    InterestChargesUnreadable,
    balance_of,
    get_invoice_strict,
    interest_charge_events,
    standing_interest_charges,
    unreadable_amount_message,
    unreadable_balance_field,
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
    NoteRefusee,
    assemble_note_values,
    fill_fingerprint,
)
from utils import storage_identity
from utils.cabinet import cabinet_dict
from utils.deadlines import today_mtl
from utils.docx_fill import DocxFillError, fill_docx
from utils.invoice_docx import build_interest_notice_context
from utils.logging_setup import log_template_event, log_unexpected
from utils.template_fields import VALID_LANGUAGES
from utils.tracing_setup import add_attributes, span


NOTE_KIND = "avis_interets"
SOURCE = "avis_interets"
TAG = INTEREST_NOTICE_TAG

INVOICE_UNREADABLE = (
    "La facture n'a pas pu être lue — lecture impossible. Rien n'a été "
    "généré : réessayez dans un instant."
)
INVOICE_VOIDED = (
    "Impossible de générer un avis d'intérêts pour une facture annulée."
)
INTEREST_UNREADABLE = (
    "Les avis d'intérêts enregistrés sur cette facture sont illisibles : la "
    "facture doit d'abord être réparée. Rien n'a été généré."
)
INTEREST_NOT_FOUND = (
    "Cet avis d'intérêts est introuvable sur la facture : seul un avis actif "
    "peut être généré en Word. Rien n'a été généré."
)
INTEREST_CANCELLED = (
    "Cet avis d'intérêts a été annulé : le numéro {number} est annulé, et "
    "aucun document ne se génère plus pour lui. Rien n'a été généré."
)
BALANCE_CONSEQUENCE = (
    "le solde de la facture après l'avis d'intérêts ne peut pas être établi"
)
NO_ACTIVE_INTEREST_TEMPLATE = (
    f"Aucun gabarit « {ACTIVE_KIND_NAMES[NOTE_KIND]} » n'est désigné comme "
    "actif : désignez-en un dans Gabarits."
)
INTEREST_TEMPLATE_UNREADABLE = (
    "Le gabarit actif des avis d'intérêts n'a pas pu être lu — lecture "
    "impossible. Rien n'a été généré : réessayez dans un instant."
)
GENERATED_LOOKUP_FAILED = (
    "Les avis d'intérêts déjà versés pour cette facture n'ont pas pu être "
    "lus. Rien n'a été généré, pour ne pas en verser un en double : "
    "réessayez dans un instant."
)
SAVE_UNCERTAIN = (
    "L'enregistrement de l'avis d'intérêts n'a pas pu être confirmé : il a "
    f"peut-être déjà été versé dans « {_FACTURES_PATH} ». Vérifiez le "
    "dossier ou réessayez : le document existant sera repris."
)


class AvisInteretsRefuse(NoteRefusee):
    """Refusal raised before or on an uncertain save when generating an avis d'intérêts."""


@dataclass
class AvisInteretsGenere:
    document: dict
    template: dict
    fingerprint: str
    reused: bool
    invoice: dict
    interest_charge: dict = field(default_factory=dict)
    folder: Optional[dict] = None


def _refuse(reason: str, message: str, **fields) -> AvisInteretsRefuse:
    log_template_event("generation_failed", reason=reason, source=SOURCE, **fields)
    return AvisInteretsRefuse(reason, message)


def _standing_notice(invoice: dict, interest_id: str, invoice_id: str) -> dict:
    try:
        events = interest_charge_events(invoice)
        standing = {event["id"] for event in standing_interest_charges(invoice)}
    except InterestChargesUnreadable:
        raise _refuse("interest_unreadable", INTEREST_UNREADABLE, invoice_id=invoice_id)
    for event in events:
        if event["kind"] == "avis_interets" and event["id"] == interest_id:
            if interest_id in standing:
                return event
            number = str(event.get("interest_notice_number") or "")
            raise _refuse(
                "interest_cancelled",
                INTEREST_CANCELLED.format(number=number),
                invoice_id=invoice_id,
                interest_id=interest_id,
            )
    raise _refuse(
        "interest_not_found",
        INTEREST_NOT_FOUND,
        invoice_id=invoice_id,
        interest_id=interest_id,
    )


def generer_avis_interets(
    invoice_id: str,
    interest_id: str,
    *,
    regenerate: bool = False,
    resolve_uid: Optional[Callable[[], str]] = None,
    today: Optional[date] = None,
    lang: str = "fr",
) -> AvisInteretsGenere:
    """Generate the Word notice for standing interest event *interest_id*."""
    lang_clean = str(lang or "fr").strip().lower()
    if lang_clean not in VALID_LANGUAGES:
        raise _refuse("invalid_lang", "Langue de document non valide.")
    try:
        invoice = get_invoice_strict(invoice_id)
    except Exception:
        log_unexpected("avis d'intérêts: invoice read failed", invoice_id=invoice_id)
        raise _refuse("invoice_unreadable", INVOICE_UNREADABLE, invoice_id=invoice_id)
    if not invoice:
        raise _refuse("invoice_not_found", INVOICE_NOT_FOUND)
    if invoice.get("status") == "annulée":
        raise _refuse("invoice_voided", INVOICE_VOIDED, invoice_id=invoice_id)

    notice = _standing_notice(invoice, interest_id, invoice_id)
    ids = {"invoice_id": invoice_id, "interest_id": notice["id"]}

    unreadable = unreadable_balance_field(invoice)
    if unreadable is not None:
        raise _refuse(
            "montant_illisible",
            f"{unreadable_amount_message(unreadable, BALANCE_CONSEQUENCE)} "
            "Rien n'a été généré.",
            **ids,
        )
    balance = balance_of(invoice)

    try:
        template = get_active_template(NOTE_KIND)
    except TemplateReadError:
        raise _refuse("template_read_failed", INTEREST_TEMPLATE_UNREADABLE, **ids)
    if not template:
        raise _refuse("no_interest_notice_template", NO_ACTIVE_INTEREST_TEMPLATE, **ids)
    template_id = template["id"]
    if lang_clean == "en" and not has_en(template):
        raise _refuse(
            "no_en_version",
            "Le gabarit actif « Avis d'intérêts » n'a pas de version anglaise.",
            template_id=template_id,
            **ids,
        )

    dossier_id = invoice.get("dossier_id", "")
    try:
        dossier = get_dossier_strict(dossier_id) if dossier_id else None
    except Exception:
        log_unexpected("avis d'intérêts: dossier read failed", invoice_id=invoice_id)
        raise _refuse(
            "dossier_unreadable", DOSSIER_UNREADABLE, template_id=template_id, **ids
        )
    client_id = invoice.get("client_id", "")
    client = get_partie(client_id) if client_id else None
    day = today or today_mtl()

    lang_kw = {"lang": "en"} if lang_clean == "en" else {}
    ctx = build_interest_notice_context(
        invoice,
        notice,
        firm=cabinet_dict(),
        destinataire=client,
        dossier=dossier,
        today=day,
        balance_cents=balance,
        **lang_kw,
    )
    values = assemble_note_values(template, ctx, **lang_kw)
    add_attributes(template_id=template_id, invoice_id=invoice_id)
    fingerprint = fill_fingerprint(
        template,
        values,
        ctx,
        subject={"kind": NOTE_KIND, "interest_id": notice["id"]},
        **lang_kw,
    )

    if not regenerate:
        try:
            filed = find_generated_for_invoice(invoice_id)
        except Exception:
            log_unexpected(
                "avis d'intérêts: generated notes lookup failed", invoice_id=invoice_id
            )
            raise _refuse(
                "generated_lookup_failed",
                GENERATED_LOOKUP_FAILED,
                template_id=template_id,
                **ids,
            )
        for existing in filed:
            if (
                existing.get("generation_fingerprint") == fingerprint
                and existing.get("dossier_id") == dossier_id
            ):
                return AvisInteretsGenere(
                    document=existing,
                    template=template,
                    fingerprint=fingerprint,
                    reused=True,
                    invoice=invoice,
                    interest_charge=notice,
                )

    docx_bytes = template_file_bytes(template, **lang_kw)
    if docx_bytes is None:
        raise _refuse(
            "template_file_unavailable",
            TEMPLATE_FILE_UNAVAILABLE,
            template_id=template_id,
            **ids,
        )

    try:
        with span("template.fill", template_id=template_id, invoice_id=invoice_id):
            filled = fill_docx(docx_bytes, values)
    except DocxFillError as exc:
        reason = "unbalanced_condition" if "conditionnelle" in str(exc) else "fill_error"
        raise _refuse(reason, TEMPLATE_INVALID, template_id=template_id, **ids)
    except Exception:
        log_unexpected("avis d'intérêts fill failed", template_id=template_id)
        raise _refuse("fill_error", FILL_ERROR, template_id=template_id, **ids)

    save_fields = {"template_id": template_id, "dossier_id": dossier_id, **ids}
    try:
        uid = (resolve_uid or storage_identity.owner_uid)()
    except storage_identity.StorageIdentityUnavailable as exc:
        raise _refuse("save_failed", storage_identity.public_message(exc), **save_fields)

    number = notice["interest_notice_number"]
    invoice_number = invoice.get("invoice_number", "")
    tmpl_base = template.get("name") or ACTIVE_KIND_NAMES[NOTE_KIND]
    lang_suffix = " (EN)" if lang_clean == "en" else ""
    display, out_name = projet_names(dossier, f"{tmpl_base} {number}{lang_suffix}".strip(), day)
    try:
        doc, landed = save_generated(
            dossier={
                "id": dossier_id,
                "file_number": invoice.get("dossier_file_number", ""),
            },
            filled=filled,
            uid=uid,
            display_name=display,
            filename=out_name,
            category="correspondance",
            genere_depuis=(
                f"Générée depuis l'avis d'intérêts {number} de la facture "
                f"{invoice_number}{lang_suffix}"
            ),
            tags=(TAG,),
            folder=FACTURES,
            generated_from_invoice={
                "invoice_id": invoice_id,
                "fingerprint": fingerprint,
            },
        )
    except GenerationRefused as refusal:
        if refusal.reason == "save_uncertain":
            raise _refuse("save_uncertain", SAVE_UNCERTAIN, **save_fields)
        raise _refuse(refusal.reason, refusal.message or SAVE_FAILED, **save_fields)

    log_template_event(
        "document_generated",
        template_id=template_id,
        dossier_id=dossier_id,
        saved_document_id=doc["id"],
        source=SOURCE,
        **ids,
    )
    return AvisInteretsGenere(
        document=doc,
        template=template,
        fingerprint=fingerprint,
        reused=False,
        invoice=invoice,
        interest_charge=notice,
        folder=landed,
    )
