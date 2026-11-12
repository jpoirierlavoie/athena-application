"""Counterparty suggestions: where they come from, and the fixed names.

The two registers' declared tables of entry types
(``models/admin_ledger.ENTRY_TYPES``, ``models/trust.PURPOSE_TYPES``) name,
for each type, the SOURCES its counterparty field suggests from, in the
order the suggestions list them, and the PRE-FILL a new entry of that type
starts with. The lawyer's decisions of 2026-11-11:

* a payment to a third party, or a supplier's refund of an expense: the
  suppliers and the professionals (``utils/contact_roles.PAYEE_ROLES``);
* a client's money: the clients, the dossier's first;
* the lawyer's own money (a drawing, a contribution): pre-filled with the
  lawyer's name;
* the TPS/TVQ: the tax authorities, pre-filled with the first.

A suggestion is only a suggestion: the field stays free text, and a typed
name is stored as typed.

Pure (stdlib only, pinned by tests/test_entry_types.py): both models import
it, and the routes and the connector may.
"""

#: The trust account, as a virement interne names it — the exact string
#: ``scripts/reclassify_admin_ledger.TRUST_COUNTERPARTY`` matches (pinned).
TRUST_ACCOUNT_NAME = "Compte en fidéicommis"
#: The tax authorities a TPS/TVQ remittance or refund names. The first is
#: the pre-fill: Revenu Québec administers the TPS as well as the TVQ for
#: most Québec businesses.
TAX_AUTHORITIES = ("Revenu Québec", "Agence du revenu du Canada")

# ── Sources ────────────────────────────────────────────────────────────────
#: The chosen invoice's client (an encaissement).
SOURCE_INVOICE_CLIENT = "client_facture"
#: The chosen dossier's clients — an encaissement's dossier is its invoice's.
SOURCE_DOSSIER_CLIENTS = "clients_dossier"
#: The chosen dossier's parties: its clients and opposing parties (the trust
#: form's original list, kept for « Règlement » and « Autre »).
SOURCE_DOSSIER_PARTIES = "parties_dossier"
#: Every contact whose role is « client ».
SOURCE_CLIENTS = "clients"
#: The suppliers (``contact_roles.SUPPLIER_ROLES``).
SOURCE_SUPPLIERS = "fournisseurs"
#: The professionals (``contact_roles.PROFESSIONAL_ROLES``).
SOURCE_PROFESSIONALS = "professionnels"
#: ``TRUST_ACCOUNT_NAME``.
SOURCE_TRUST_ACCOUNT = "compte_fideicommis"
#: ``TAX_AUTHORITIES``.
SOURCE_TAX_AUTHORITIES = "administrations_fiscales"
SOURCES = (
    SOURCE_INVOICE_CLIENT, SOURCE_DOSSIER_CLIENTS, SOURCE_DOSSIER_PARTIES,
    SOURCE_CLIENTS, SOURCE_SUPPLIERS, SOURCE_PROFESSIONALS,
    SOURCE_TRUST_ACCOUNT, SOURCE_TAX_AUTHORITIES,
)

# ── Pre-fills ──────────────────────────────────────────────────────────────
# A pre-fill applies to a new entry only, and never replaces a name typed or
# one the page arrived with.
#: The chosen invoice's client.
PREFILL_INVOICE_CLIENT = "client_facture"
#: The client chosen on the trust form.
PREFILL_CHOSEN_CLIENT = "client_choisi"
#: The LAWYER's name as the firm profile holds it (``nom``, never the firm's
#: ``organisation``), normalised as ``models/fee_payment._payees_from``
#: normalises the fee payment's payees — never ``default_fee_payee``, which
#: names the firm on a transfer.
PREFILL_LAWYER = "avocat"
#: ``TAX_AUTHORITIES[0]``.
PREFILL_TAX_AUTHORITY = "administration_fiscale"
#: The trust account's financial institution.
PREFILL_BANK = "banque"
PREFILLS = (
    PREFILL_INVOICE_CLIENT, PREFILL_CHOSEN_CLIENT, PREFILL_LAWYER,
    PREFILL_TAX_AUTHORITY, PREFILL_BANK,
)
