"""The declared tables of entry types (lot 4 of the suppliers request,
2026-11-11): ``models/admin_ledger.ENTRY_TYPES`` and
``models/trust.PURPOSE_TYPES``.

Each register declares its types ONCE: the label, the family the forms
group it under, the counterparty field's label and example (the
administration), the sources its suggestions come from and the pre-fill a
new entry starts with (``utils/contreparties``). The vocabulary and the
labels derive from that table. These tests pin:

* the derived vocabularies against TODAY's literals, order included, and
  the labels' content — written before the table, against the old code:
  nothing a page, an export or the connector shows changes. The trust
  labels keep their order too; the administration labels now follow the
  vocabulary's order, a dict nothing iterates;
* each table against its vocabulary, and each family against the rule set
  it restates — moving « apport » out of the lawyer's money fails;
* the suggestions and pre-fills against the lawyer's decisions of
  2026-11-11;
* the fixed names against the string the scripts match, the trust payee
  labels against the register's column, and the kinds that carry a
  supplier's invoice number against the connector's whitelist.

Pure: no Firestore read.
"""

import os
import pathlib
import sys
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import mcp.handlers as handlers
    from models import admin_ledger as al
    from models import trust
    from scripts import reclassify_admin_ledger
    from services import comptabilite as svc

# ── Today's literals (2026-11-11), copied by hand from the old code ─────────
ADMIN_KINDS_TODAY = (
    "encaissement_facture", "recette_autre", "dépense", "remboursement_depense",
    "prélèvement", "apport", "virement_interne_sortant",
    "virement_interne_entrant", "avance_honoraires", "remise_tps_tvq",
    "remboursement_tps_tvq", "paiement_carte", "correction",
)
ADMIN_LABELS_TODAY = {
    "encaissement_facture": "Encaissement de facture",
    "recette_autre": "Autre recette",
    "dépense": "Dépense",
    "prélèvement": "Prélèvement de l'avocat",
    "apport": "Apport de l'avocat",
    "remboursement_depense": "Remboursement de dépense",
    "virement_interne_sortant": "Virement interne (sortant)",
    "virement_interne_entrant": "Virement interne (entrant)",
    "avance_honoraires": "Avance d'honoraires",
    "remise_tps_tvq": "Remise de TPS/TVQ",
    "remboursement_tps_tvq": "Remboursement de TPS/TVQ",
    "paiement_carte": "Paiement de carte",
    "correction": "Correction",
}
TRUST_LABELS_TODAY = [
    ("avance_honoraires", "Avance d'honoraires"),
    ("dépôt_client", "Dépôt du client"),
    ("règlement", "Règlement"),
    ("virement_honoraires", "Paiement d'honoraires"),
    ("remise_client", "Remise au client"),
    ("déboursé_tiers", "Déboursé à un tiers"),
    ("virement_inter_dossiers", "Virement inter-dossiers"),
    ("intérêts", "Intérêts"),
    ("frais_bancaires", "Frais bancaires"),
    ("correction", "Correction"),
    ("autre", "Autre"),
]
ENTRY_FORM_PURPOSES_TODAY = (
    "avance_honoraires", "dépôt_client", "règlement", "virement_honoraires",
    "remise_client", "déboursé_tiers", "intérêts", "frais_bancaires", "autre",
)


# ── 1. Nothing shown changes ────────────────────────────────────────────────

def test_the_administration_kinds_and_labels_are_today_s():
    assert al.VALID_KINDS == ADMIN_KINDS_TODAY
    assert al.KIND_LABELS == ADMIN_LABELS_TODAY
    # What the connector's refusals read (services/comptabilite's copy).
    assert svc.ADMIN_KIND_LABELS == ADMIN_LABELS_TODAY


def test_the_trust_purposes_and_labels_are_today_s_order_included():
    assert trust.VALID_PURPOSES == tuple(p for p, _label in TRUST_LABELS_TODAY)
    assert list(trust.PURPOSE_LABELS.items()) == TRUST_LABELS_TODAY
    assert list(svc.TRUST_PURPOSE_LABELS.items()) == TRUST_LABELS_TODAY


def test_the_trust_entry_form_offers_today_s_purposes():
    from routes import trust as trust_routes
    assert trust_routes.ENTRY_FORM_PURPOSES == ENTRY_FORM_PURPOSES_TODAY


# ── 2. One table per register, one family per rule set ─────────────────────

def test_the_administration_table_is_the_vocabulary():
    assert tuple(al.ENTRY_TYPES) == al.VALID_KINDS
    # The labels follow the table's order now (a dict nothing iterates).
    assert tuple(al.KIND_LABELS) == al.VALID_KINDS
    assert all(isinstance(t, al.EntryType) for t in al.ENTRY_TYPES.values())


def test_the_trust_table_is_the_vocabulary():
    assert tuple(trust.PURPOSE_TYPES) == trust.VALID_PURPOSES
    assert tuple(trust.PURPOSE_LABELS) == trust.VALID_PURPOSES
    assert all(isinstance(t, trust.PurposeType)
               for t in trust.PURPOSE_TYPES.values())


def _members(table: dict, family: str) -> set:
    return {code for code, t in table.items() if t.family == family}


# Each administration family is the rule set the model already keeps.
ADMIN_FAMILY_RULES = {
    "dépenses": al.CATEGORY_KINDS,
    "recettes": al.REVENUE_KINDS,
    "argent_clients": al.ADVANCE_KINDS,
    "argent_avocat": al.OWNER_KINDS,
    "virements": al.INTERNAL_TRANSFER_KINDS,
    "tps_tvq": al.SALES_TAX_KINDS,
    # The two structural kinds, minted only by their own write paths.
    "particulières": tuple(k for k in ADMIN_KINDS_TODAY
                           if k not in al._SIMPLE_KINDS),
}


def test_each_administration_family_is_its_rule_set():
    assert tuple(al.KIND_FAMILIES) == tuple(ADMIN_FAMILY_RULES)
    for family, rule in ADMIN_FAMILY_RULES.items():
        assert _members(al.ENTRY_TYPES, family) == set(rule), family
    assert ADMIN_FAMILY_RULES["particulières"] == ("paiement_carte", "correction")


# Each trust family restates the direction a purpose implies
# (PURPOSE_DIRECTIONS), the fee payment, the purposes without a dossier and
# the two the model reserves.
TRUST_FAMILY_RULES = {
    "reçues": tuple(p for p, d in trust.PURPOSE_DIRECTIONS.items()
                    if d == "recette"),
    "versées": tuple(p for p, d in trust.PURPOSE_DIRECTIONS.items()
                     if d == "déboursé") + (trust.FEE_PAYMENT_PURPOSE,),
    # « Règlement » and « Autre » go either way (PURPOSE_DIRECTIONS's note).
    "selon_le_cas": ("règlement", "autre"),
    "compte_bancaire": tuple(p for p in trust.NO_DOSSIER_PURPOSES
                             if p != trust.REVERSAL_PURPOSE),
    "particulières": (trust.TRANSFER_PURPOSE, trust.REVERSAL_PURPOSE),
}


def test_each_trust_family_is_its_rule_set():
    assert tuple(trust.PURPOSE_FAMILIES) == tuple(TRUST_FAMILY_RULES)
    for family, rule in TRUST_FAMILY_RULES.items():
        assert _members(trust.PURPOSE_TYPES, family) == set(rule), family
    assert not set(TRUST_FAMILY_RULES["selon_le_cas"]) & set(trust.PURPOSE_DIRECTIONS)
    # The entry form offers every purpose but the reserved family.
    assert set(ENTRY_FORM_PURPOSES_TODAY) == (
        set(trust.VALID_PURPOSES) - _members(trust.PURPOSE_TYPES, "particulières"))


def test_every_family_has_its_own_french_label_and_a_member():
    for families, table in ((al.KIND_FAMILIES, al.ENTRY_TYPES),
                            (trust.PURPOSE_FAMILIES, trust.PURPOSE_TYPES)):
        for family, label in families.items():
            assert label and label[0].isupper(), family
            assert _members(table, family), family
        assert {t.family for t in table.values()} <= set(families)
        # One heading per group of the menu.
        assert len(set(families.values())) == len(families)
    assert list(al.KIND_FAMILIES.values()) == [
        "Dépenses", "Recettes du cabinet", "Argent des clients",
        "Argent de l'avocat", "Virements", "TPS/TVQ", "Écritures particulières"]
    assert list(trust.PURPOSE_FAMILIES.values()) == [
        "Sommes reçues", "Sommes versées", "Selon le cas", "Compte bancaire",
        "Écritures particulières"]


# ── 3. The lawyer's decisions of 2026-11-11 ────────────────────────────────

def test_sources_and_prefills_are_closed_vocabularies():
    from utils import contreparties as cp
    for table in (al.ENTRY_TYPES, trust.PURPOSE_TYPES):
        for code, t in table.items():
            assert isinstance(t.sources, tuple), code
            assert set(t.sources) <= set(cp.SOURCES), code
            assert len(set(t.sources)) == len(t.sources), code
            assert t.prefill in cp.PREFILLS + ("",), code


def _profile(table: dict, code: str) -> tuple:
    return table[code].sources, table[code].prefill


def test_payments_to_third_parties_suggest_suppliers_and_professionals():
    from utils import contreparties as cp
    third_parties = ((cp.SOURCE_SUPPLIERS, cp.SOURCE_PROFESSIONALS), "")
    for kind in al.CATEGORY_KINDS:  # a dépense and a supplier's refund
        assert _profile(al.ENTRY_TYPES, kind) == third_parties, kind
    assert _profile(trust.PURPOSE_TYPES, "déboursé_tiers") == third_parties


def test_client_receipts_suggest_the_clients_the_dossier_s_first():
    from utils import contreparties as cp
    assert _profile(al.ENTRY_TYPES, "encaissement_facture") == (
        (cp.SOURCE_INVOICE_CLIENT, cp.SOURCE_DOSSIER_CLIENTS, cp.SOURCE_CLIENTS),
        cp.PREFILL_INVOICE_CLIENT)
    for kind in al.ADVANCE_KINDS:
        assert _profile(al.ENTRY_TYPES, kind) == (
            (cp.SOURCE_DOSSIER_CLIENTS, cp.SOURCE_CLIENTS), ""), kind
    # The trust form names the client: a client's money takes that client.
    for purpose in ("avance_honoraires", "dépôt_client", "remise_client"):
        assert _profile(trust.PURPOSE_TYPES, purpose) == (
            (cp.SOURCE_DOSSIER_CLIENTS, cp.SOURCE_CLIENTS),
            cp.PREFILL_CHOSEN_CLIENT), purpose


def test_an_other_receipt_and_the_either_way_purposes():
    from utils import contreparties as cp
    # D5: « Autre recette » — the dossier's clients, then every client,
    # supplier and professional.
    assert _profile(al.ENTRY_TYPES, "recette_autre") == (
        (cp.SOURCE_DOSSIER_CLIENTS, cp.SOURCE_CLIENTS, cp.SOURCE_SUPPLIERS,
         cp.SOURCE_PROFESSIONALS), "")
    # D5: trust « Règlement » and « Autre » keep the dossier's parties.
    for purpose in TRUST_FAMILY_RULES["selon_le_cas"]:
        assert _profile(trust.PURPOSE_TYPES, purpose) == (
            (cp.SOURCE_DOSSIER_PARTIES,), ""), purpose


def test_the_lawyer_s_own_money_is_prefilled_with_the_lawyer_s_name():
    from utils import contreparties as cp
    for kind in al.OWNER_KINDS:
        assert _profile(al.ENTRY_TYPES, kind) == ((), cp.PREFILL_LAWYER), kind


def test_tps_tvq_goes_to_the_tax_authority_revenu_quebec_first():
    from utils import contreparties as cp
    for kind in al.SALES_TAX_KINDS:
        assert _profile(al.ENTRY_TYPES, kind) == (
            (cp.SOURCE_TAX_AUTHORITIES,), cp.PREFILL_TAX_AUTHORITY), kind
    assert cp.TAX_AUTHORITIES == ("Revenu Québec", "Agence du revenu du Canada")


def test_a_virement_suggests_the_trust_account_and_the_bank_entries_take_the_bank():
    from utils import contreparties as cp
    for kind in al.INTERNAL_TRANSFER_KINDS:
        assert _profile(al.ENTRY_TYPES, kind) == ((cp.SOURCE_TRUST_ACCOUNT,), ""), kind
    for purpose in TRUST_FAMILY_RULES["compte_bancaire"]:
        assert _profile(trust.PURPOSE_TYPES, purpose) == ((), cp.PREFILL_BANK), purpose


def test_the_structural_entries_and_the_fee_payment_take_nothing():
    for kind in ADMIN_FAMILY_RULES["particulières"]:
        assert _profile(al.ENTRY_TYPES, kind) == ((), ""), kind
    # The fee payment names its payee from its own list (art. 58, D23).
    for purpose in TRUST_FAMILY_RULES["particulières"] + (trust.FEE_PAYMENT_PURPOSE,):
        assert _profile(trust.PURPOSE_TYPES, purpose) == ((), ""), purpose


# ── 4. Labels of the counterparty field ────────────────────────────────────

def test_every_form_kind_names_its_counterparty_field():
    for kind, t in al.ENTRY_TYPES.items():
        on_the_form = kind in al._SIMPLE_KINDS
        assert bool(t.placeholder) == on_the_form, kind
        assert t.placeholder.startswith("Ex. : ") or not on_the_form, kind
        # A correction takes its original's label; every other kind has one.
        assert bool(t.payee_label) == (kind != al.REVERSAL_KIND), kind


# The counterparty field's label per kind: the approved plan's table
# (2026-11-11) for every kind it names — « Compte de contrepartie » is
# today's label of a virement, and a correction takes its original's. The
# plan gives a card payment no label (« automatic »: each leg names the
# other account); its « Compte de contrepartie » is this lot's choice, to be
# put to the lawyer when lot 5 shows it.
PAYEE_LABELS_PLANNED = {
    "encaissement_facture": "Reçu de",
    "recette_autre": "Reçu de",
    "dépense": "Fournisseur",
    "remboursement_depense": "Fournisseur",
    "prélèvement": "Versé à",
    "apport": "Reçu de",
    "virement_interne_sortant": "Compte de contrepartie",
    "virement_interne_entrant": "Compte de contrepartie",
    "avance_honoraires": "Reçu de",
    "remise_tps_tvq": "Administration fiscale",
    "remboursement_tps_tvq": "Administration fiscale",
    "paiement_carte": "Compte de contrepartie",
    "correction": "",
}


def test_the_payee_labels_are_pinned_and_read_with_the_direction():
    assert {kind: t.payee_label for kind, t in al.ENTRY_TYPES.items()} == (
        PAYEE_LABELS_PLANNED)
    # « Reçu de » only on money in, « Versé à » only on money out.
    for kind, label in PAYEE_LABELS_PLANNED.items():
        if label == "Reçu de":
            assert al._KIND_DIRECTION[kind] == "recette", kind
        elif label == "Versé à":
            assert al._KIND_DIRECTION[kind] == "déboursé", kind


def test_the_examples_name_the_fixed_names():
    from utils import contreparties as cp
    for kind in al.INTERNAL_TRANSFER_KINDS:
        assert al.ENTRY_TYPES[kind].placeholder == f"Ex. : {cp.TRUST_ACCOUNT_NAME}"
    for kind in al.SALES_TAX_KINDS:
        assert al.ENTRY_TYPES[kind].placeholder == f"Ex. : {cp.TAX_AUTHORITIES[0]}"


def test_the_trust_payee_labels_are_the_register_column_s_two_halves():
    assert tuple(trust.PAYEE_LABELS) == trust.VALID_DIRECTIONS
    assert " / ".join(trust.PAYEE_LABELS.values()) == dict(
        trust.BARREAU_COLUMNS)["counterparty"]


# ── 5. The fixed names and the supplier's invoice number ──────────────────

def test_the_trust_account_is_named_as_the_scripts_match_it():
    from utils import contreparties as cp
    assert cp.TRUST_ACCOUNT_NAME == reclassify_admin_ledger.TRUST_COUNTERPARTY


def test_the_kinds_with_a_supplier_invoice_number_are_the_connector_s():
    accepted = {kind for kind, keys in handlers._ADMIN_KIND_ARGS.items()
                if "supplier_invoice_ref" in keys}
    assert set(al.SUPPLIER_REF_KINDS) == accepted
    assert al.SUPPLIER_REF_KINDS == al.CATEGORY_KINDS + ("recette_autre",)


def test_the_vocabulary_module_is_pure():
    """Both models import it, and the connector may: stdlib only — the
    allow-list of tests/test_civilite.py (an app module such as ``security``
    or ``config`` is refused too)."""
    import ast
    path = _ATHENA / "utils" / "contreparties.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        for module in modules:
            assert module.split(".")[0] in sys.stdlib_module_names, module


def test_the_keys_are_distinct_and_all_listed():
    """A source or a pre-fill is looked up by its key: two constants holding
    one string would merge two decisions (« suppliers » suggesting the
    dossier's parties, the bank pre-filled with the lawyer's name)."""
    from utils import contreparties as cp
    for prefix, listed in (("SOURCE_", cp.SOURCES), ("PREFILL_", cp.PREFILLS)):
        declared = [value for name, value in vars(cp).items()
                    if name.startswith(prefix) and isinstance(value, str)]
        assert len(set(listed)) == len(listed), prefix
        assert sorted(declared) == sorted(listed), prefix
