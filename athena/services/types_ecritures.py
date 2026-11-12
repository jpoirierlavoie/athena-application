"""The « Types d'écritures » page's cards (lot 7 of 2026-11-11).

What each entry type of the two registers does, derived from the model —
the declared tables (``admin_ledger.ENTRY_TYPES``, ``trust.PURPOSE_TYPES``),
the rule sets they restate, the cash-flow lines and the statements'
sentences (``models/financial_statements``) — never written by hand: a
type changing its rules changes its card.

PURE: no Firestore read, no Flask. The route renders what this returns.
"""

from models import admin_ledger as al
from models import financial_statements as fs
from models import trust
from utils import contreparties as cp

#: How the page names each suggestion source and pre-fill.
SOURCE_LABELS = {
    cp.SOURCE_INVOICE_CLIENT: "le client de la facture",
    cp.SOURCE_DOSSIER_CLIENTS: "les clients du dossier",
    cp.SOURCE_DOSSIER_PARTIES: "les parties du dossier",
    cp.SOURCE_CLIENTS: "les clients",
    cp.SOURCE_SUPPLIERS: "les fournisseurs",
    cp.SOURCE_PROFESSIONALS: ("les professionnels (avocats, notaires, huissiers, "
                              "experts, sténographes)"),
    cp.SOURCE_TRUST_ACCOUNT: f"« {cp.TRUST_ACCOUNT_NAME} »",
    cp.SOURCE_TAX_AUTHORITIES: ", ".join(cp.TAX_AUTHORITIES),
}
PREFILL_LABELS = {
    cp.PREFILL_INVOICE_CLIENT: "le client de la facture choisie",
    cp.PREFILL_CHOSEN_CLIENT: "le client choisi",
    cp.PREFILL_LAWYER: "le nom de l'avocat (profil du cabinet)",
    cp.PREFILL_TAX_AUTHORITY: f"« {cp.TAX_AUTHORITIES[0]} »",
    cp.PREFILL_BANK: "la banque du compte",
}
_DIRECTION = {"recette": "Recette", "déboursé": "Déboursé"}
_REVERSED = "L'inverse de l'écriture contre-passée"
_LIKE_ORIGINAL = "Ceux de l'écriture contre-passée"


def _suggestions(sources) -> str:
    return " ; ".join(SOURCE_LABELS[s] for s in sources) or "Aucune"


def _prefill(code: str) -> str:
    return PREFILL_LABELS.get(code, "") or "Aucun"


def _admin_card(kind: str, entry_type) -> dict:
    if kind == al.REVERSAL_KIND:
        sens, dossier, split = _REVERSED, "Celui de l'écriture contre-passée", _LIKE_ORIGINAL
        lines = ["Celle de l'écriture contre-passée"]
        where = "« Contre-passer », depuis l'écriture"
    else:
        if kind == "paiement_carte":
            sens = "Deux volets : un déboursé au compte d'opérations, une recette à la carte"
            dossier = "Jamais"
            where = "« Paiement de carte »"
        else:
            sens = _DIRECTION[al.implied_direction(kind)]
            dossier = ("Celui de la facture" if kind == "encaissement_facture"
                       else "Jamais" if kind in al.NO_DOSSIER_KINDS else "Facultatif")
            where = "« Nouvelle écriture d'administration »"
        split = ("Catégorie et ventilation de la TPS/TVQ" if kind in al.CATEGORY_KINDS
                 else "Aucune catégorie ; aucune TPS/TVQ réclamée")
        lines = [fs.CASH_FLOW_LINE_LABELS[al.CASH_FLOW_LINES[kind]]]
        if kind == al.TRANSFER_PURPOSE_KIND:
            lines.append(fs.CASH_FLOW_LINE_LABELS[al.ADVANCE_TRANSFER_LINE]
                         + " (objet « Avances à verser au fidéicommis »)")
    return {
        "code": kind, "label": entry_type.label, "sens": sens, "dossier": dossier,
        "split": split, "cash_flow": lines,
        "payee_label": entry_type.payee_label or "Celui de l'écriture contre-passée",
        "suggestions": _suggestions(entry_type.sources),
        "prefill": _prefill(entry_type.prefill),
        "where": where,
        "statement": fs.KIND_STATEMENT_SENTENCES[kind],
        "aliases": [],
    }


def _trust_card(purpose: str, purpose_type) -> dict:
    optional = [p for p in trust.NO_DOSSIER_PURPOSES if p != trust.REVERSAL_PURPOSE]
    if purpose == trust.REVERSAL_PURPOSE:
        sens, dossier = _REVERSED, "Celui de l'écriture contre-passée"
        payee = "Celui de l'écriture contre-passée"
        where = "« Contre-passer », depuis l'écriture"
    elif purpose == trust.TRANSFER_PURPOSE:
        sens = "Deux volets : un déboursé au dossier d'origine, une recette au dossier destinataire"
        dossier = "Les deux dossiers, chacun avec son client"
        payee = "Chaque volet nomme l'autre"
        where = "« Virement inter-dossiers »"
    else:
        direction = ("déboursé" if purpose == trust.FEE_PAYMENT_PURPOSE
                     else trust.PURPOSE_DIRECTIONS.get(purpose))
        sens = _DIRECTION[direction] if direction else "Recette ou déboursé"
        dossier = "Facultatif" if purpose in optional else "Obligatoire, avec son client"
        payee = (" ou ".join(f"« {trust.PAYEE_LABELS[d]} »" for d in trust.VALID_DIRECTIONS)
                 if not direction else f"« {trust.PAYEE_LABELS[direction]} »")
        where = "« Nouvelle écriture en fidéicommis »"
    suggestions = _suggestions(purpose_type.sources)
    if purpose == trust.FEE_PAYMENT_PURPOSE:
        payee = "L'avocat ou son cabinet, tels que les nomme le profil (art. 58)"
        suggestions = "Un choix entre ces deux noms"
    return {
        "code": purpose, "label": purpose_type.label, "sens": sens, "dossier": dossier,
        "payee_label": payee, "suggestions": suggestions,
        "prefill": _prefill(purpose_type.prefill), "where": where,
    }


def administration() -> list[tuple[str, list[dict]]]:
    """``[(family label, [card])]``: every administration type, family by
    family (``admin_ledger.KIND_FAMILIES``), each once."""
    groups = [(label, [_admin_card(kind, al.ENTRY_TYPES[kind]) for kind in kinds])
              for label, kinds in al.kinds_by_family(al.VALID_KINDS)]
    # The form's « Virement interne » is no stored kind: its link opens the
    # first of the two virement cards.
    seen: set = set()
    for _label, cards in groups:
        for card in cards:
            value = al.form_kind(card["code"])[0]
            if value != card["code"] and value not in seen:
                seen.add(value)
                card["aliases"].append(value)
    return groups


def fideicommis() -> list[tuple[str, list[dict]]]:
    """``[(family label, [card])]``: every trust purpose, family by family
    (``trust.PURPOSE_FAMILIES``), each once."""
    return [(label, [_trust_card(p, trust.PURPOSE_TYPES[p]) for p in purposes])
            for label, purposes in trust.purposes_by_family(trust.VALID_PURPOSES)]
