"""Tests for utils/invoice_docx.py — the note-d'honoraires context builder."""

import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.invoice_docx import build_invoice_context

NBSP = " "
TODAY = date(2026, 1, 15)


def _dt(y, m, d):
    return datetime(y, m, d, 0, 0, tzinfo=timezone.utc)


def _invoice(**overrides):
    base = {
        "invoice_number": "2025-F007",
        "date": _dt(2025, 12, 11),
        "due_date": _dt(2026, 1, 10),
        "client_id": "c1",
        "billing_address": {
            "name": "Jean Tremblay",
            "street": "12 rue Principale",
            "unit": "",
            "city": "Montréal",
            "province": "QC",
            "postal_code": "H2X 1Y6",
        },
        "subtotal_fees": 50000,
        "subtotal_expenses": 15000,
        "subtotal": 65000,
        "gst_rate": 500,
        "gst_amount": 3250,
        "qst_rate": 9975,
        "qst_amount": 6484,
        "total": 74734,
        "retainer_applied": 0,
        "amount_due": 74734,
        "gst_number": "123456789 RT0001",
        "qst_number": "1234567890 TQ0001",
    }
    base.update(overrides)
    return base


def _fee(desc, hours, rate, amount, d=_dt(2025, 12, 1)):
    return {"type": "fee", "date": d, "description": desc, "hours": hours,
            "rate": rate, "amount": amount, "taxable": True}


def _exp(desc, amount, taxable, d=_dt(2025, 12, 2)):
    return {"type": "expense", "date": d, "description": desc, "hours": None,
            "rate": None, "amount": amount, "taxable": taxable}


def _build(invoice, items, **kw):
    defaults = dict(firm={}, destinataire=None, dossier=None, today=TODAY)
    defaults.update(kw)
    return build_invoice_context(invoice, items, **defaults)


# ── Region splitting ────────────────────────────────────────────────────

def test_splits_into_three_regions_in_order():
    items = [
        _fee("Recherche", 2.5, 25000, 62500),
        _exp("Signification", 5000, True),
        _exp("Timbre judiciaire", 3000, False),
    ]
    ctx = _build(_invoice(), items)
    assert [r["h.description"] for r in ctx.rows["ligne_honoraire"]] == ["Recherche"]
    assert [r["d.description"] for r in ctx.rows["ligne_debours_tx"]] == ["Signification"]
    assert [r["d.description"] for r in ctx.rows["ligne_debours_ntx"]] == ["Timbre judiciaire"]


def test_conditions_reflect_empty_and_nonempty_regions():
    items = [_fee("R", 1, 25000, 25000), _exp("S", 5000, True)]  # no non-taxable
    ctx = _build(_invoice(), items)
    assert ctx.conditions == {
        "si_honoraires": True, "si_debours_tx": True, "si_debours_ntx": False,
    }
    empty = _build(_invoice(), [])
    assert empty.conditions == {
        "si_honoraires": False, "si_debours_tx": False, "si_debours_ntx": False,
    }


# ── Subtotal invariant (§6.4) ───────────────────────────────────────────

def test_subtotal_invariant_tx_plus_ntx_equals_expenses():
    items = [_exp("A", 5000, True), _exp("B", 3000, False), _exp("C", 7000, True)]
    inv = _invoice(subtotal_expenses=15000)
    ctx = _build(inv, items)
    tx = sum(e["amount"] for e in items if e["taxable"])
    ntx = sum(e["amount"] for e in items if not e["taxable"])
    assert tx + ntx == inv["subtotal_expenses"]  # exact cents
    assert ctx.values["facture.sous_total_debours_tx"] == f"120,00{NBSP}$"
    assert ctx.values["facture.sous_total_debours_ntx"] == f"30,00{NBSP}$"


# ── Formatting (§7) ─────────────────────────────────────────────────────

def test_scalar_money_date_hours_rate_strings():
    items = [_fee("Recherche", 0.5, 25000, 12500)]
    inv = _invoice(subtotal_fees=115000, subtotal=115000,
                   retainer_applied=115000, amount_due=0)
    ctx = _build(inv, items)
    v = ctx.values
    assert v["facture.numero"] == "2025-F007"
    assert v["facture.date"] == "11 décembre 2025"
    assert v["facture.sous_total_honoraires"] == f"1{NBSP}150,00{NBSP}$"
    assert v["facture.avances_fideicommis"] == f"(1{NBSP}150,00){NBSP}$"
    assert v["facture.solde"] == f"0,00{NBSP}$"
    assert v["facture.nombre_heures"] == "0,50"
    assert v["facture.tps_taux"] == f"5{NBSP}%"
    assert v["facture.tvq_taux"] == f"9,975{NBSP}%"
    # Row-scoped formatting.
    row = ctx.rows["ligne_honoraire"][0]
    assert row["h.date"] == "1er décembre 2025"
    assert row["h.temps"] == "0,50"


def test_taux_horaire_single_vs_mixed_rates():
    same = _build(_invoice(), [_fee("A", 1, 25000, 25000), _fee("B", 2, 25000, 50000)])
    assert same.values["facture.taux_horaire"] == f"250,00{NBSP}$"
    # Mixed rates, no dossier → blank (itemized per row).
    mixed = _build(_invoice(), [_fee("A", 1, 25000, 25000), _fee("B", 1, 30000, 30000)])
    assert mixed.values["facture.taux_horaire"] == ""


def test_taux_horaire_falls_back_to_dossier_rate():
    dossier = {"hourly_rate": 27500, "clients": [], "opposing_parties": []}
    # Mixed line-item rates → fall back to the dossier's configured rate.
    mixed = _build(_invoice(),
                   [_fee("A", 1, 25000, 25000), _fee("B", 1, 30000, 30000)],
                   dossier=dossier)
    assert mixed.values["facture.taux_horaire"] == f"275,00{NBSP}$"
    # A uniform billed rate still wins over the dossier rate.
    uniform = _build(_invoice(),
                     [_fee("A", 1, 25000, 25000), _fee("B", 2, 25000, 50000)],
                     dossier=dossier)
    assert uniform.values["facture.taux_horaire"] == f"250,00{NBSP}$"


# ── Taxes read, never recomputed (§7.2) ─────────────────────────────────

def test_taxes_are_read_not_recomputed():
    # Stored amounts deliberately unrelated to any % of the subtotal.
    inv = _invoice(gst_amount=9999, qst_amount=8888)
    ctx = _build(inv, [_fee("R", 1, 25000, 25000)])
    assert ctx.values["facture.tps_montant"] == f"99,99{NBSP}$"
    assert ctx.values["facture.tvq_montant"] == f"88,88{NBSP}$"


# ── Client fallback to billing_address (§6.6) ───────────────────────────

def test_client_fallback_to_billing_address_when_partie_missing():
    ctx = _build(_invoice(), [_fee("R", 1, 25000, 25000)], destinataire=None)
    assert ctx.values.get("destinataire.nom_complet") == "Jean Tremblay"
    assert "12 rue Principale" in ctx.values.get("destinataire.adresse_complete", "")


def test_flat_alias_header_fields_resolve_like_procedures():
    # A note template may use the same flat placeholders as the
    # procedures/letters gabarits — canonical AND alias both resolve.
    dossier = {
        "title": "Tremblay c. Lavoie",
        "court_file_number": "500-05-123456-241",
        "file_number": "2026-042",
        "clients": [], "opposing_parties": [],
    }
    ctx = _build(_invoice(), [_fee("R", 1, 25000, 25000)],
                 dossier=dossier, destinataire=None)
    # Flat aliases (as the existing gabarits type them).
    assert ctx.values.get("intitulé_dossier") == "Tremblay c. Lavoie"
    assert ctx.values.get("numero_dossier") == "500-05-123456-241"
    assert ctx.values.get("référence_interne") == "2026-042"
    assert ctx.values.get("ville_récipient") == "Montréal"
    assert ctx.values.get("adresse_civique_récipient") == "12 rue Principale"
    # Canonical still resolves too.
    assert ctx.values.get("dossier.titre") == "Tremblay c. Lavoie"
    assert ctx.values.get("destinataire.ville") == "Montréal"


def test_live_partie_used_when_provided():
    partie = {
        "type": "individual", "prefix": "M.", "first_name": "Luc",
        "last_name": "Gagnon", "contact_role": "client",
        "address_street": "9 av. du Parc", "address_city": "Laval",
        "address_province": "Québec", "address_postal_code": "H7A 1B2",
    }
    ctx = _build(_invoice(), [_fee("R", 1, 25000, 25000)], destinataire=partie)
    assert ctx.values["destinataire.nom_complet"] == "Luc Gagnon"  # bare
    assert ctx.values["destinataire.nom_complet_avec_civilite"] == "M. Luc Gagnon"
    assert ctx.values["destinataire.adresse_complete"].startswith("9 av. du Parc")


def test_live_organization_address_resolves_from_the_work_block():
    """Le bogue signalé (2026-08-14), épinglé bout en bout : une personne
    morale VIVANTE — le formulaire de contact masque son bloc personnel, donc
    son adresse ne peut être que professionnelle — sortait avec « [CHAMP
    MANQUANT : destinataire.adresse_complete] » sur la note d'honoraires.
    Ironie du chemin : la même entreprise SUPPRIMÉE s'imprimait correctement,
    l'instantané billing_address faisant déjà « travail sinon personnelle »."""
    org = {
        "type": "organization",
        "organization_name": "Constructions Beaubien inc.",
        "contact_role": "client",
        "email_work": "info@beaubien.ca",
        "work_address_street": "500 rue Beaubien Est",
        "work_address_unit": "bureau 300",
        "work_address_city": "Montréal",
        "work_address_province": "Québec",
        "work_address_postal_code": "H2S 1S5",
        "work_address_country": "Canada",
    }
    ctx = _build(_invoice(), [_fee("R", 1, 25000, 25000)], destinataire=org)
    assert ctx.values["destinataire.nom_complet"] == "Constructions Beaubien inc."
    assert ctx.values["destinataire.adresse_complete"] == (
        "500 rue Beaubien Est, bureau 300, Montréal (Québec) H2S 1S5"
    )
    assert ctx.values["destinataire.ville"] == "Montréal"
    assert ctx.values["destinataire.courriel"] == "info@beaubien.ca"
    # Les alias plats que les gabarits existants emploient suivent.
    assert ctx.values["ville_récipient"] == "Montréal"
    assert ctx.values["adresse_civique_récipient"] == "500 rue Beaubien Est, bureau 300"


# ── End-to-end: build context → fill a full note template (§13.1–4) ─────

import io
import zipfile
from defusedxml import ElementTree as ET

from utils.docx_fill import extract_placeholders, fill_docx
from utils.template_fields import (
    MANUAL_FIELDS, classify_placeholders, fallback_value,
)

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _p(text):
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _tc(text):
    return f"<w:tc><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>"


def _tr(*cells):
    return f"<w:tr>{''.join(cells)}</w:tr>"


def _tbl(*rows):
    return f"<w:tbl><w:tblPr/>{''.join(rows)}</w:tbl>"


def _make_docx(body):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml",
                   f'<?xml version="1.0"?><w:document {_W}><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def _document_xml(docx):
    with zipfile.ZipFile(io.BytesIO(docx)) as z:
        return z.read("word/document.xml").decode("utf-8")


def _assemble(placeholders, ctx):
    """Mirror services.note_honoraires.assemble_note_values (pure; the
    route's until lot 3a)."""
    classification = classify_placeholders(placeholders)
    values = {}
    for name in placeholders:
        if name in ctx.values:
            values[name] = ctx.values[name]
        elif name in classification.auto:
            values[name] = fallback_value(name, is_auto=True)
        elif name in classification.manual:
            values[name] = MANUAL_FIELDS[name]["default"] or fallback_value(name, is_auto=False)
    return values


def _note_template():
    honoraires = (
        _p("{{?si_honoraires}}")
        + _tbl(
            _tr(_tc("Date"), _tc("Description"), _tc("Temps")),
            _tr(_tc("{{#ligne_honoraire}}{{h.date}}"),
                _tc("{{h.description}}"), _tc("{{h.temps}}")),
        )
        + _p("{{/si_honoraires}}")
    )
    debours_tx = (
        _p("{{?si_debours_tx}}")
        + _tbl(_tr(_tc("{{#ligne_debours_tx}}{{d.date}}"),
                   _tc("{{d.description}}"), _tc("{{d.cout}}")))
        + _p("{{/si_debours_tx}}")
    )
    debours_ntx = (
        _p("{{?si_debours_ntx}}")
        + _tbl(_tr(_tc("Débours non assujettis {{#ligne_debours_ntx}}{{d.date}}"),
                   _tc("{{d.description}}"), _tc("{{d.cout}}")))
        + _p("{{/si_debours_ntx}}")
    )
    totals = _p(
        "Total avant taxes {{facture.total_avant_taxes}} "
        "TPS {{facture.tps_montant}} TVQ {{facture.tvq_montant}} "
        "Total {{facture.total_apres_taxes}} Solde {{facture.solde}}"
    )
    header = _p("Facture {{facture.numero}} — {{destinataire.nom_complet}}")
    return _make_docx(header + honoraires + debours_tx + debours_ntx + totals)


def test_end_to_end_fill_note_template():
    docx = _note_template()
    items = [
        _fee("Recherche", 2.5, 25000, 62500),
        _fee("Rédaction", 1.0, 25000, 25000),
        _fee("Appel", 0.5, 25000, 12500),
        _exp("Signification", 5000, True),
        # No non-taxable disbursement → that table must be removed entirely.
    ]
    inv = _invoice(subtotal_fees=100000, subtotal_expenses=5000, subtotal=105000,
                   gst_amount=5250, qst_amount=10474, total=120724, amount_due=120724)
    ctx = _build(inv, items)
    placeholders = extract_placeholders(docx)
    values = _assemble(placeholders, ctx)
    out = _document_xml(fill_docx(docx, values,
                                  rows_by_region=ctx.rows, conditions=ctx.conditions))

    ET.fromstring(out)  # acceptance §13.1 — opens (well-formed) without repair

    # §13.2 — N fee rows, M taxable rows, K=0 non-taxable table removed.
    assert out.count("<w:tr>") == 5  # honoraires header + 3 data + 1 débours-tx
    assert "Recherche" in out and "Rédaction" in out and "Appel" in out
    assert "Signification" in out
    # §13.3 — empty table gone (heading, marker and all).
    assert "Débours non assujettis" not in out
    assert "{{#ligne_debours_ntx}}" not in out and "{{?si_debours_ntx}}" not in out
    # Header + totals filled from stored figures (§13.4/5).
    assert "2025-F007" in out and "Jean Tremblay" in out
    assert "52,50" in out and "104,74" in out  # stored gst / qst
    # Nothing left unfilled among the app-filled fields / row fields.
    assert "{{facture." not in out
    assert "{{h." not in out and "{{d." not in out
    assert "{{destinataire.nom_complet}}" not in out


# ── Payments (2026-11-08) ────────────────────────────────────────────────
# A fictional year and invented amounts (house rule for this public repo).

from utils.invoice_docx import (  # noqa: E402
    PAYMENT_FIELDS,
    PAYMENT_FLAG,
    PAYMENT_REGION,
    PAYMENT_ROW_FIELDS,
    TRUST_BALANCE_FIELD,
    InvoicePayments,
    _payment_rows,
    prints_payments,
    prints_trust_balance,
)


def _receipt(amount, d, *, method="virement", trust=False, sequence=1):
    row = {"kind": "encaissement_facture", "status": "compensée",
           "amount": amount, "date": d, "method": method, "sequence": sequence}
    if trust:
        row["trust_transaction_id"] = "tx-fidei"
    return row


def _paid_invoice(**overrides):
    fields = dict(invoice_number="2031-F012", date=_dt(2031, 3, 3),
                  due_date=_dt(2031, 4, 3), total=86231, amount_due=86231)
    fields.update(overrides)
    return _invoice(**fields)


def test_without_payments_the_context_keeps_its_former_keys():
    """The gate's promise: a template that prints no payment is filled from
    the very context it always was — the same keys, hence the same
    fingerprint (services.note_honoraires.fill_fingerprint)."""
    ctx = _build(_paid_invoice(), [_fee("R", 1, 25000, 25000)])
    assert set(ctx.rows) == {"ligne_honoraire", "ligne_debours_tx", "ligne_debours_ntx"}
    assert set(ctx.conditions) == {"si_honoraires", "si_debours_tx", "si_debours_ntx"}
    assert "facture.solde_restant" not in ctx.values
    assert TRUST_BALANCE_FIELD not in ctx.values


def test_the_trust_balance_prints_as_a_plain_amount_only_when_given():
    ctx = _build(_paid_invoice(), [], trust_balance_cents=115000)
    assert ctx.values["facture.solde_fideicommis"] == f"1{NBSP}150,00{NBSP}$"
    # A scalar only: no region, no flag.
    assert set(ctx.rows) == {"ligne_honoraire", "ligne_debours_tx", "ligne_debours_ntx"}
    assert set(ctx.conditions) == {"si_honoraires", "si_debours_tx", "si_debours_ntx"}
    empty = _build(_paid_invoice(), [], trust_balance_cents=0)
    assert empty.values[TRUST_BALANCE_FIELD] == f"0,00{NBSP}$"


def test_the_trust_balance_gate_reads_its_exact_name():
    assert prints_trust_balance(["facture.numero", "facture.solde_fideicommis"])
    assert not prints_trust_balance(
        ["facture.avances_fideicommis", "facture.solde", "facture.solde_restant", "p.montant"])
    assert not prints_trust_balance(None)
    assert TRUST_BALANCE_FIELD == "facture.solde_fideicommis"


def test_payment_rows_name_trust_or_the_method_and_print_a_deduction():
    receipts = [
        # A fee payment's encaissement carries the trust withdrawal's method;
        # its source is the trust account all the same.
        _receipt(40000, _dt(2031, 3, 10), trust=True, sequence=4),
        _receipt(12500, _dt(2031, 3, 17), method="chèque", sequence=7),
        _receipt(3105, _dt(2031, 3, 24), method="autre", sequence=9),
        _receipt(626, _dt(2031, 4, 1), method="carte", sequence=11),
    ]
    ctx = _build(_paid_invoice(amount_paid=56231), [],
                 payments=InvoicePayments(receipts=receipts, balance_cents=30000))
    assert ctx.rows[PAYMENT_REGION] == [
        {"p.date": "10 mars 2031", "p.source": "Fidéicommis",
         "p.montant": f"(400,00){NBSP}$"},
        {"p.date": "17 mars 2031", "p.source": "Chèque",
         "p.montant": f"(125,00){NBSP}$"},
        {"p.date": "24 mars 2031", "p.source": "Paiement",
         "p.montant": f"(31,05){NBSP}$"},
        {"p.date": "1er avril 2031", "p.source": "Carte",
         "p.montant": f"(6,26){NBSP}$"},
    ]
    assert ctx.conditions[PAYMENT_FLAG] is True
    assert ctx.values["facture.solde_restant"] == f"300,00{NBSP}$"
    # The former fields keep their meaning: solde is the amount due at issuance.
    assert ctx.values["facture.solde"] == f"862,31{NBSP}$"


def test_a_method_outside_the_vocabulary_still_prints_a_payment():
    rows = _payment_rows({}, [_receipt(100, _dt(2031, 5, 5), method="interac"),
                              {"amount": 200, "date": _dt(2031, 5, 6)}])
    assert [r["p.source"] for r in rows] == ["Paiement", "Paiement"]


def test_the_provision_of_an_imported_invoice_leads_the_rows():
    """retainer_applied rides in the series — the region REPLACES the
    Avances line, so on paper TOTAL less the rows gives the balance."""
    inv = _paid_invoice(retainer_applied=20000, amount_due=66231, amount_paid=15000)
    receipts = [_receipt(15000, _dt(2031, 3, 3), method="comptant", sequence=2)]
    ctx = _build(inv, [], payments=InvoicePayments(receipts=receipts,
                                                   balance_cents=51231))
    assert [(r["p.date"], r["p.source"], r["p.montant"])
            for r in ctx.rows[PAYMENT_REGION]] == [
        ("3 mars 2031", "Fidéicommis", f"(200,00){NBSP}$"),
        ("3 mars 2031", "Comptant", f"(150,00){NBSP}$"),
    ]
    assert ctx.values["facture.solde_restant"] == f"512,31{NBSP}$"


def test_rows_follow_the_calendar_day_then_the_register_order():
    receipts = [  # the strict reader's order: (date, sequence)
        _receipt(100, _dt(2031, 3, 5), method="chèque", sequence=3),
        _receipt(200, _dt(2031, 3, 5), method="virement", sequence=8),
    ]
    rows = _payment_rows(_paid_invoice(retainer_applied=300, date=_dt(2031, 3, 9)),
                         receipts)
    assert [r["p.source"] for r in rows] == ["Chèque", "Virement", "Fidéicommis"]


def test_a_standing_write_off_prints_after_the_payments_of_its_day():
    """Lot 3a (D2, a forgiveness shown to the client): each standing
    write-off is a row of its own, its amount taxes included, after the
    encaissements of its own day — it wrote off what they left —, the rows
    oldest first; the balance printed under them is zero. Since lot 3b (the
    lawyer's decision 8: a price reduction its credit note documents) its
    source names the credit note — « Note de crédit 2031-NC001 », no longer
    « Radiation »."""
    receipts = [_receipt(10000, _dt(2031, 3, 1), sequence=2),
                _receipt(5000, _dt(2031, 3, 10), method="chèque", sequence=6)]
    write_offs = [{"id": "rad-1", "kind": "radiation", "date": _dt(2031, 3, 10),
                   "amount": 71231, "gst_amount": 3098, "qst_amount": 6180,
                   "tax_claimed": True, "reason": "", "reverses_id": "",
                   "credit_note_number": "2031-NC001"}]
    ctx = _build(_paid_invoice(status="payée", amount_paid=15000), [],
                 payments=InvoicePayments(receipts=receipts, balance_cents=0,
                                          write_offs=write_offs))
    assert [(r["p.date"], r["p.source"], r["p.montant"])
            for r in ctx.rows[PAYMENT_REGION]] == [
        ("1er mars 2031", "Virement", f"(100,00){NBSP}$"),
        ("10 mars 2031", "Chèque", f"(50,00){NBSP}$"),
        ("10 mars 2031", "Note de crédit 2031-NC001", f"(712,31){NBSP}$"),
    ]
    assert ctx.conditions[PAYMENT_FLAG] is True
    assert ctx.values["facture.solde_restant"] == f"0,00{NBSP}$"


def test_no_payment_yet_empties_the_region_and_lowers_its_flag():
    ctx = _build(_paid_invoice(), [],
                 payments=InvoicePayments(receipts=[], balance_cents=86231))
    assert ctx.rows[PAYMENT_REGION] == []
    assert ctx.conditions[PAYMENT_FLAG] is False
    assert ctx.values["facture.solde_restant"] == f"862,31{NBSP}$"


def test_the_gate_reads_the_template_inventory_by_exact_name():
    assert prints_payments(["facture.total_apres_taxes", "p.montant"])
    assert prints_payments(["facture.solde_restant"])
    assert not prints_payments(["facture.solde", "h.date", "facture.avances_fideicommis"])
    assert not prints_payments(["P.MONTANT"])      # row fields match case-sensitively
    assert not prints_payments(None)
    assert set(PAYMENT_FIELDS) == set(PAYMENT_ROW_FIELDS) | {"facture.solde_restant"}


def test_the_row_fields_are_the_keys_every_payment_row_carries():
    rows = _payment_rows({"retainer_applied": 1, "date": _dt(2031, 1, 2)},
                         [_receipt(1, _dt(2031, 1, 3), trust=True),
                          _receipt(1, _dt(2031, 1, 4))],
                         [{"date": _dt(2031, 1, 5), "amount": 1,
                           "credit_note_number": "2031-NC001"}])
    assert len(rows) == 4 and all(tuple(r) == PAYMENT_ROW_FIELDS for r in rows)


def test_end_to_end_fill_lists_the_payments_in_place_of_the_avances_line():
    totals = _tbl(
        _tr(_tc("TOTAL APRÈS TAXES"), _tc("{{facture.total_apres_taxes}}")),
        _tr(_tc("{{#ligne_paiement}}{{p.date}} — {{p.source}}"), _tc("{{p.montant}}")),
        _tr(_tc("SOLDE"), _tc("{{facture.solde_restant}}")),
    )
    docx = _make_docx(_p("Facture {{facture.numero}}") + totals)
    receipts = [_receipt(40000, _dt(2031, 3, 10), trust=True),
                _receipt(16231, _dt(2031, 3, 17), method="chèque", sequence=2)]
    ctx = _build(_paid_invoice(amount_paid=56231), [],
                 payments=InvoicePayments(receipts=receipts, balance_cents=30000))
    values = _assemble(extract_placeholders(docx), ctx)
    out = _document_xml(fill_docx(docx, values,
                                  rows_by_region=ctx.rows, conditions=ctx.conditions))

    ET.fromstring(out)
    assert out.count("<w:tr>") == 4         # total + 2 payments + solde
    assert "10 mars 2031 — Fidéicommis" in out
    assert "17 mars 2031 — Chèque" in out
    assert f"(400,00){NBSP}$" in out and f"(162,31){NBSP}$" in out
    assert f"300,00{NBSP}$" in out and f"862,31{NBSP}$" in out
    assert "{{" not in out
