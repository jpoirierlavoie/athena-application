"""The entry forms read the declared tables (lot 5 of the suppliers request,
2026-11-11).

What the two registers' forms and pages take from ``models/admin_ledger
.ENTRY_TYPES`` and ``models/trust.PURPOSE_TYPES``:

1. **The menus.** The administration « Type » (create and edit) and the
   journal's « Type » filter, the trust « Objet », grouped by family
   (``<optgroup>``), never an empty group; the create form's order derived
   (``FORM_KINDS``), « Dépense » first.
2. **The counterparty field.** Its label and example follow the type (the
   administration) or the direction (the trust register); a NEW entry's
   field starts with its type's pre-fill — the lawyer's name as the firm
   profile spells it (never the firm), « Revenu Québec », the chosen
   invoice's client, the chosen client, the account's bank — resolved by
   the route, never on an edit, a refused POST or a name the page arrives
   with (decision D6). The pre-fill's BEHAVIOUR in the page is Alpine's:
   it was proved in headless Chrome against a control; these tests pin the
   data the page acts on.
3. **The field order (D7).** The pickers that decide the suggestions come
   before the counterparty: the invoice and the dossier (administration),
   the dossier and the client (trust).
4. **The supplier's invoice number.** Shown and posted only for the kinds
   that carry one (``SUPPLIER_REF_KINDS``, « Autre recette » included), and
   only when posted: a create of another kind stores none, an edit keeps
   the stored number whatever the kind becomes.
5. **The pages.** An entry's counterparty under its type's label (a
   correction, its original's), and the TPS/TVQ split of a dépense, a
   refund and their corrections, whatever the direction.
6. **The trust form's small fixes.** The dossier's hint follows the
   purpose; the client a refused POST kept stays selected; a new dossier
   search drops the old client.

« F » tests fail on the code before lot 5 (checked); the others are
witnesses. Every name, number and amount is invented; the bench is the
shared fake Firestore under the real routes, models and templates, with the
REAL ``jsattr``.
"""

import html
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import admin_ledger as al
    from models import fee_payment
    from models import partie as pm
    from models import trust
    import routes.admin_ledger as ra
    import routes.dossiers as dossiers_routes
    import routes.invoices as invoices_routes
    import routes.trust as rt

from flask import Flask, render_template  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils import contreparties as cp  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.html_attr import jsattr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc
LAWYER_STORED = "  Me  Aube   Fictive "
LAWYER = "Me Aube Fictive"
FIRM = "Cabinet Fictif inc."
CLIENT = "Client Fictif inc."
BANK = "Banque Fictive"


def _d(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def clock(monkeypatch):
    from utils import deadlines as dl
    frozen = datetime(2031, 9, 20, 16, 0, tzinfo=UTC)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(dl, "datetime", _Clock)
    return frozen


@pytest.fixture
def fake(monkeypatch, clock):
    f = install(monkeypatch, *_fake_modules())
    f.seed("settings/cabinet", {"nom": LAWYER_STORED, "organisation": FIRM})
    f.seed("admin_accounts/ops1", {
        "id": "ops1", "name": "Opérations", "status": "actif",
        "account_type": "opérations", "ledger_balance": 0, "etag": "e0",
    })
    f.seed("trust_accounts/acc1", {
        "id": "acc1", "name": "Fidéicommis général", "institution": BANK,
        "status": "actif", "account_type": "général", "book_balance": 0,
        "bank_balance": 0, "etag": "t0",
    })
    f.seed("dossiers/dos1", {
        "id": "dos1", "file_number": "2031-001", "title": "Dossier fictif",
        "status": "actif", "client_ids": ["c1"],
        "clients": [{"id": "c1", "name": CLIENT}],
        "trust_balance": 0, "trust_balance_by_client": {},
        "trust_cleared_by_client": {},
    })
    f.seed("invoices/inv1", {
        "id": "inv1", "invoice_number": "2031-F001", "dossier_id": "dos1",
        "dossier_file_number": "2031-001", "client_id": "c1",
        "client_name": CLIENT, "status": "envoyée", "amount_due": 60000,
        "amount_paid": 0, "etag": "i0",
    })
    return f


@pytest.fixture
def client(fake):
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=jsattr,
    )
    for bp in (rt.trust_bp, invoices_routes.invoices_bp, ra.admin_bp,
               dossiers_routes.dossiers_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


class _Menus(HTMLParser):
    """Every ``<select>`` by name: its options in document order, each with
    the label of the ``<optgroup>`` holding it (``None`` outside one), and
    every group opened."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.selects: dict = {}
        self._select = self._group = self._option = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "select":
            self._select = {"attrs": a, "options": [], "groups": []}
            self.selects[a.get("name", "")] = self._select
        elif tag == "optgroup" and self._select is not None:
            self._group = a.get("label", "")
            self._select["groups"].append(self._group)
        elif tag == "option" and self._select is not None:
            self._option = {"value": a.get("value", ""), "label": "",
                            "group": self._group, "attrs": a}
            self._select["options"].append(self._option)

    def handle_data(self, data):
        if self._option is not None:
            self._option["label"] += data

    def handle_endtag(self, tag):
        if tag == "option":
            self._option = None
        elif tag == "optgroup":
            self._group = None
        elif tag == "select":
            self._select = self._group = None


def _menus(page: str) -> dict:
    parser = _Menus()
    parser.feed(page)
    parser.close()
    return parser.selects


def _grouped(select: dict) -> list:
    """``[(group label, [values])]``, in document order."""
    out: list = []
    for option in select["options"]:
        if out and out[-1][0] == option["group"]:
            out[-1][1].append(option["value"])
        else:
            out.append((option["group"], [option["value"]]))
    return out


def _json_block(page: str, block_id: str) -> dict:
    match = re.search(r'<script type="application/json" id="%s">(.*?)</script>'
                      % re.escape(block_id), page, re.S)
    assert match, block_id
    return json.loads(match.group(1))


def _admin_data(kind: str, **over) -> dict:
    d = {"account_id": "ops1", "kind": kind, "amount": 10000,
         "method": "virement", "counterparty": "Contrepartie fictive",
         "date": _d(2031, 9, 1), "description": "", "reference": "",
         "supplier_invoice_ref": ""}
    if kind in al.CATEGORY_KINDS:
        d.update(category="fournitures", net_amount=8698, gst_amount=435,
                 qst_amount=867)
    d.update(over)
    return d


def _admin(kind: str, **over) -> dict:
    entry, errs = al.create_transaction(_admin_data(kind, **over))
    assert errs == [], errs
    return entry


def _admin_form(kind: str, **over) -> dict:
    f = {"account_id": "ops1", "kind": kind, "amount": "100,00",
         "method": "virement", "counterparty": "Contrepartie fictive",
         "date": "2031-09-01", "description": "", "reference": ""}
    f.update(over)
    return f


def _correction_of(fake, original_id: str) -> dict:
    (row,) = [r for r in fake.peek_collection("admin_transactions").values()
              if r.get("reverses_id") == original_id]
    return row


# ══════════════════════════════════════════════════════════════════════
# 1. The administration menus
# ══════════════════════════════════════════════════════════════════════

EXPECTED_FORM_KINDS = (
    "dépense", "remboursement_depense",
    "encaissement_facture", "recette_autre",
    "avance_honoraires",
    "prélèvement", "apport",
    "virement_interne",
    "remise_tps_tvq", "remboursement_tps_tvq",
)


def test_the_form_kinds_are_derived_family_by_family():
    """F — the create menu's order was a hand-written literal."""
    assert al.FORM_KINDS == EXPECTED_FORM_KINDS
    assert al.FORM_KINDS[0] == "dépense"
    assert set(al.FORM_KINDS) == {al.form_kind(k)[0] for k in al._SIMPLE_KINDS}
    # « Virement interne » is the two virement codes, one profile.
    sortant, entrant = (al.ENTRY_TYPES[k] for k in al.INTERNAL_TRANSFER_KINDS)
    assert (sortant.payee_label, sortant.placeholder, sortant.sources,
            sortant.prefill, sortant.family) == (
        entrant.payee_label, entrant.placeholder, entrant.sources,
        entrant.prefill, entrant.family)
    assert al.form_entry_type(al.INTERNAL_TRANSFER_FORM_KIND) is sortant
    assert al.form_entry_type("inconnu") is None


def test_grouping_refuses_a_kind_the_table_ignores():
    with pytest.raises(ValueError):
        al.kinds_by_family(["dépense", "inconnu"])
    with pytest.raises(ValueError):
        trust.purposes_by_family(["dépôt_client", "inconnu"])
    assert al.kinds_by_family([]) == []


def test_the_create_menu_is_grouped_by_family(client, fake):
    """F — the « Type » menu was one flat list mixing five families."""
    page = client.get("/administration/nouvelle").get_data(as_text=True)
    kind = _menus(page)["kind"]
    assert _grouped(kind) == [
        ("Dépenses", ["dépense", "remboursement_depense"]),
        ("Recettes du cabinet", ["encaissement_facture", "recette_autre"]),
        ("Argent des clients", ["avance_honoraires"]),
        ("Argent de l'avocat", ["prélèvement", "apport"]),
        ("Virements", ["virement_interne"]),
        ("TPS/TVQ", ["remise_tps_tvq", "remboursement_tps_tvq"]),
    ]
    assert kind["groups"] == [g for g, _ in _grouped(kind)]   # no empty group
    selected = [o["value"] for o in kind["options"] if "selected" in o["attrs"]]
    assert selected == ["dépense"]


@pytest.mark.parametrize("stored", sorted(al.KIND_MOVES))
def test_each_edit_menu_groups_exactly_its_passages(client, fake, stored):
    """F — the edit menus were flat; every group holds a passage, in the
    families' order, under its own family's label."""
    entry = _admin(stored)
    page = client.get(f"/administration/{entry['id']}/modifier").get_data(as_text=True)
    kind = _menus(page)["kind"]
    groups = _grouped(kind)
    assert kind["groups"] == [g for g, _ in groups]
    offered = [v for _, values in groups for v in values]
    assert offered == [v for v in al.FORM_KINDS if v in offered]
    for label, values in groups:
        for value in values:
            assert al.KIND_FAMILIES[al.form_entry_type(value).family] == label


def test_the_journal_filter_groups_every_stored_kind(client, fake):
    """F — the filter listed the 13 kinds flat."""
    page = client.get("/administration/").get_data(as_text=True)
    kind = _menus(page)["kind"]
    groups = _grouped(kind)
    assert groups[0] == (None, [""])                    # « Tous », outside
    assert [label for label, _ in groups[1:]] == list(al.KIND_FAMILIES.values())
    values = [v for _, vs in groups[1:] for v in vs]
    assert sorted(values) == sorted(al.VALID_KINDS)     # each code once
    assert groups[-1] == ("Écritures particulières", ["paiement_carte", "correction"])
    assert ("Virements", list(al.INTERNAL_TRANSFER_KINDS)) in groups
    page = client.get("/administration/?kind=correction").get_data(as_text=True)
    chosen = [o["value"] for o in _menus(page)["kind"]["options"]
              if "selected" in o["attrs"]]
    assert chosen == ["correction"]
    # The form's « Virement interne » is no stored code: the filter ignores
    # it and lists every entry, where a stored code filters.
    spent, drawn = _admin("dépense"), _admin("prélèvement")
    page = client.get("/administration/", query_string={"kind": "virement_interne"}
                      ).get_data(as_text=True)
    assert not [o for o in _menus(page)["kind"]["options"] if "selected" in o["attrs"]]
    assert spent["id"] in page and drawn["id"] in page
    page = client.get("/administration/", query_string={"kind": "dépense"}
                      ).get_data(as_text=True)
    assert spent["id"] in page and drawn["id"] not in page


# ══════════════════════════════════════════════════════════════════════
# 2. The administration's counterparty field
# ══════════════════════════════════════════════════════════════════════


def test_the_profiles_follow_the_table_and_resolve_the_prefills(client, fake):
    """F — one label (« Payeur / Fournisseur ») and two examples for every
    type, and no pre-fill."""
    page = client.get("/administration/nouvelle").get_data(as_text=True)
    block = _json_block(page, "admin-entry-types")
    assert block["prefill_on"] is True
    assert block["payee_label_fallback"] == al.PAYEE_LABEL_FALLBACK
    kinds = block["kinds"]
    assert list(kinds) and set(kinds) == set(al.FORM_KINDS)
    for value, profile in kinds.items():
        t = al.form_entry_type(value)
        assert (profile["payee_label"], profile["placeholder"]) == (
            t.payee_label, t.placeholder), value
    # The lawyer's own money: the LAWYER's name as the profile spells it,
    # its spaces folded — never the firm.
    assert kinds["prélèvement"]["prefill"] == kinds["apport"]["prefill"] == LAWYER
    assert FIRM not in json.dumps(block, ensure_ascii=False)
    for kind in al.SALES_TAX_KINDS:
        assert kinds[kind]["prefill"] == "Revenu Québec"
    # An encaissement's is read in the page, from the chosen invoice.
    assert kinds["encaissement_facture"] == {
        "payee_label": "Reçu de", "placeholder": "Ex. : le client de la facture",
        "prefill": "", "from_invoice": True}
    assert not [v for v, p in kinds.items()
                if p["from_invoice"] and v != "encaissement_facture"]
    for kind in ("dépense", "recette_autre", al.INTERNAL_TRANSFER_FORM_KIND):
        assert kinds[kind]["prefill"] == "", kind


def test_every_prefill_code_of_the_menu_is_resolved():
    """W (guard) — a code the route does not resolve would pre-fill
    nothing, silently. Proved on a planted code."""
    resolved = {"", cp.PREFILL_LAWYER, cp.PREFILL_TAX_AUTHORITY,
                cp.PREFILL_INVOICE_CLIENT}

    def unresolved(types):
        return [t.prefill for t in types if t.prefill not in resolved]

    assert unresolved(al.form_entry_type(v) for v in al.FORM_KINDS) == []
    planted = al.EntryType("Fictif", "dépenses", "Fournisseur", "Ex. : x",
                           prefill=cp.PREFILL_BANK)
    assert unresolved([planted]) == [cp.PREFILL_BANK]


def test_the_lawyer_s_name_is_never_the_firm(fake):
    assert fee_payment.lawyer_name() == LAWYER
    fake.seed("settings/cabinet", {"nom": "", "organisation": FIRM})
    assert fee_payment.lawyer_name() == ""          # never the firm
    assert fee_payment.default_fee_payee("chèque") == FIRM   # unlike this one
    fake.seed("settings/cabinet", {"nom": "cabinet fictif INC.", "organisation": FIRM})
    assert fee_payment.lawyer_name() == "cabinet fictif INC."
    # The fee payment's own list is unchanged by the shared normaliser.
    fake.seed("settings/cabinet", {"nom": LAWYER_STORED, "organisation": " " + FIRM})
    assert fee_payment.fee_payees() == [FIRM, LAWYER]


@pytest.mark.parametrize("case", ["edit", "edit_blank", "refused", "sens_requis"])
def test_no_prefill_on_an_edit_or_a_refused_post(client, fake, monkeypatch, case):
    """F — D6: a pre-fill is for a NEW entry only. The profile is not even
    read then. « edit_blank »: an entry stored WITHOUT a counterparty (a
    legacy row, written around the model) — the mode alone keeps the
    pre-fill off its edit."""
    calls = []
    monkeypatch.setattr(ra.comptabilite, "nom_avocat",
                        lambda: calls.append(1) or LAWYER)
    if case in ("edit", "edit_blank"):
        entry = _admin("prélèvement", counterparty="Nom stocké fictif")
        if case == "edit_blank":
            fake.seed(f"admin_transactions/{entry['id']}",
                      {**fake.peek(f"admin_transactions/{entry['id']}"),
                       "counterparty": ""})
        resp = client.get(f"/administration/{entry['id']}/modifier")
        assert resp.status_code == 200
    elif case == "refused":
        resp = client.post("/administration/", data=_admin_form("prélèvement", counterparty=""))
        assert resp.status_code == 400
    else:
        resp = client.post("/administration/", data=_admin_form("virement_interne"))
        assert resp.status_code == 400
    block = _json_block(resp.get_data(as_text=True), "admin-entry-types")
    assert block["prefill_on"] is False
    assert all(p["prefill"] == "" and p["from_invoice"] is False
               for p in block["kinds"].values())
    assert calls == []
    # A new entry reads it once.
    client.get("/administration/nouvelle")
    assert calls == [1]


def test_a_page_arriving_with_a_name_keeps_it_and_every_field(client, fake):
    """F — the invoices lot's pre-filled link (its lot 4b): a create render
    given an entry with a counterparty pre-fills nothing, and every key of
    the entry reaches the template."""
    arrived = {"kind": "recette_autre", "counterparty": "Client fictif",
               "supplier_invoice_ref": "AV-1", "amount": 1234,
               "dossier_id": "dos1", "interest_charge_id": "x-1"}
    with client.application.test_request_context():
        ctx = ra._form_context(arrived, [])
        assert ctx["entry"] is arrived
        assert ctx["entry_types"]["prefill_on"] is False
        page = render_template("administration/form.html", **ctx)
    assert 'value="Client fictif"' in page and 'value="AV-1"' in page
    assert "kind: &#34;recette_autre&#34;," in page
    # A blank name arriving still lets the type's pre-fill apply.
    with client.application.test_request_context():
        assert ra._form_context({"kind": "prélèvement", "counterparty": "  "},
                                [])["entry_types"]["prefill_on"] is True


@pytest.mark.parametrize("kind, label, placeholder", [
    ("prélèvement", "Versé à", "Ex. : le nom de l'avocat"),
    ("remise_tps_tvq", "Administration fiscale", "Ex. : Revenu Québec"),
    ("virement_interne_sortant", "Compte de contrepartie", "Ex. : Compte en fidéicommis"),
    ("recette_autre", "Reçu de", "Ex. : un client, la banque"),
])
def test_the_server_renders_the_type_s_label_and_example(client, fake, kind,
                                                         label, placeholder):
    """F — the server rendered « Payeur / Fournisseur » for every type but a
    virement."""
    entry = _admin(kind)
    page = html.unescape(client.get(
        f"/administration/{entry['id']}/modifier").get_data(as_text=True))
    assert f">{label}</label>" in page
    assert f'placeholder="{placeholder}"' in page
    page = html.unescape(client.get("/administration/nouvelle").get_data(as_text=True))
    assert ">Fournisseur</label>" in page
    assert 'placeholder="Ex. : Bell Canada, Immeubles X"' in page


def test_each_invoice_option_carries_its_client(client, fake):
    """F — an encaissement's pre-fill reads the chosen invoice's client."""
    fake.seed("invoices/inv2", {
        "id": "inv2", "invoice_number": "2031-F002", "dossier_id": "dos1",
        "dossier_file_number": "2031-001", "client_name": 'O\'Brien & "Fils" <x>',
        "status": "en_retard", "amount_due": 5000, "amount_paid": 0})
    fake.seed("invoices/inv3", {
        "id": "inv3", "invoice_number": "2031-F003", "dossier_id": "dos1",
        "dossier_file_number": "2031-001", "status": "envoyée",
        "amount_due": 7000, "amount_paid": 0})
    page = client.get("/administration/nouvelle").get_data(as_text=True)
    options = {o["value"]: o["attrs"] for o in _menus(page)["invoice_id"]["options"]}
    assert options["inv1"]["data-client"] == CLIENT
    assert options["inv2"]["data-client"] == 'O\'Brien & "Fils" <x>'
    assert options["inv3"]["data-client"] == ""
    assert '<x>"' not in page and "&lt;x&gt;" in page   # escaped in the markup


def test_the_encaissement_help_promises_the_client_only_when_it_fills(client, fake):
    """Review of lot 5 — after a refused submission the chosen invoice's
    client no longer fills the field (D6 keeps the name submitted), so the
    help line under the invoices no longer promises it: an encaissement is
    locked from its creation, and a wrong name would stay in the register."""
    clause = "et la contrepartie avec le client de la facture"
    page = html.unescape(client.get("/administration/nouvelle").get_data(as_text=True))
    assert clause in page
    resp = client.post("/administration/", data=_admin_form(
        "encaissement_facture", invoice_id="inv1", amount="999,00", counterparty=CLIENT))
    assert resp.status_code == 400
    page = html.unescape(resp.get_data(as_text=True))
    assert "Le montant se préremplit avec le solde dû" in page
    assert clause not in page


def test_the_pickers_come_before_the_counterparty(client, fake):
    """F (D7) — the counterparty came before the invoice and the dossier."""
    page = client.get("/administration/nouvelle").get_data(as_text=True)
    at = [page.index(s) for s in ('name="invoice_id"', 'name="q"',
                                  'name="counterparty"', 'name="supplier_invoice_ref"',
                                  'name="category"', "<fieldset")]
    assert at == sorted(at)
    # The data block comes before the form's x-data that reads it (the
    # page's first form is the sidebar's sign-out).
    assert page.index('id="admin-entry-types"') < page.index("kind: &#34;")


# The administration form's pre-fill, as the page runs it (Alpine). pytest
# runs no JavaScript: the behaviour was proved in headless Chrome against a
# control; these lines pin the code that behaviour rests on, so a changed
# line fails here and is re-proved in the browser (review of lot 5).
ADMIN_ALPINE_LINES = (
    "entryTypes: JSON.parse((document.getElementById('admin-entry-types') || {}).textContent || '{}'),",
    "lastPrefill: null,",
    "profile: function (k) { return (this.entryTypes.kinds || {})[k] || {}; },",
    "invoiceClient: function () { let s = this.$refs.invoice; let o = s ? s.selectedOptions[0] : null; return (o &amp;&amp; o.dataset.client) || ''; },",
    "prefillFor: function (k) { let p = this.profile(k); return p.from_invoice ? this.invoiceClient() : (p.prefill || ''); },",
    "applyPrefill: function (value) { let f = this.$refs.counterparty; if (!this.entryTypes.prefill_on || !f) { return; } if (f.value.trim() === '' || f.value === this.lastPrefill) { f.value = value; this.lastPrefill = value; } },",
    # Lot 6: the suggestions' state and the pick (a supplier's usual
    # category fills an EMPTY category only, on a dépense or its refund).
    "cpOpen: false, cpSkip: false, cpEmpty: true, cpStatus: '',",
    "pickCounterparty: function (b) { let f = this.$refs.counterparty; f.value = b.dataset.counterparty; this.lastPrefill = null; let c = this.$refs.category; if (b.dataset.category &amp;&amp; c &amp;&amp; !c.value &amp;&amp; this.categoryKinds.includes(this.kind)) { c.value = b.dataset.category; } this.cpOpen = false; this.cpSkip = true; f.focus(); }",
)
ADMIN_X_INIT = (
    "let sel = document.getElementById('compte-select'); if (sel && sel.selectedOptions.length) "
    "{ accountType = sel.selectedOptions[0].dataset.accountType || 'opérations'; } "
    "$watch('kind', function (k) { applyPrefill(prefillFor(k)); }); applyPrefill(prefillFor(kind));")


def test_the_page_s_alpine_reads_the_profiles(client, fake):
    """The template restates no type: it reads the profiles, never compares a
    type code, and the pre-fill follows the kind through $watch (the card
    account changes the kind without a change event). Each line the
    pre-fill rests on is pinned, and so is its contract with the route: the
    keys it reads exist in the data block, the attribute it reads is on the
    invoice options, the refs it reads are on the right elements."""
    src = (_ATHENA / "templates" / "administration" / "form.html").read_text(encoding="utf-8")
    lines = [line.strip() for line in src.splitlines()]
    for pinned in ADMIN_ALPINE_LINES:
        assert pinned in lines, pinned
    assert re.search(r'x-init="([^"]*)"', src).group(1) == ADMIN_X_INIT
    assert "if (profile(kind).from_invoice) { applyPrefill(invoiceClient()); }" in src
    # The field itself lives in components/_champ_contrepartie.html (lot 6):
    # its bindings are checked on the rendered page.
    page = client.get("/administration/nouvelle").get_data(as_text=True)
    assert 'x-text="profile(kind).payee_label || entryTypes.payee_label_fallback || \'\'"' in page
    assert ':placeholder="profile(kind).placeholder || \'\'"' in page
    # The refs: the counterparty input, the invoice and category selects.
    counterparty = re.search(r'<input type="text" name="counterparty"[^>]*>', page).group(0)
    assert 'x-ref="counterparty"' in counterparty and "x-model" not in counterparty
    invoice = re.search(r'<select name="invoice_id"[^>]*', src).group(0)
    assert 'x-ref="invoice"' in invoice
    assert 'x-ref="category"' in re.search(r'<select name="category"[^>]*', src).group(0)
    assert page.count('x-ref="counterparty"') == page.count('x-ref="invoice"') == 1
    # The contract: every key the code reads is in the route's data block.
    block = _json_block(page, "admin-entry-types")
    assert {"kinds", "prefill_on", "payee_label_fallback"} <= set(block)
    for profile in block["kinds"].values():
        assert set(profile) == {"payee_label", "placeholder", "prefill", "from_invoice"}
    assert 'data-client="' in page


# ══════════════════════════════════════════════════════════════════════
# 3. The supplier's invoice number
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("kind, sens", [
    ("prélèvement", ""), ("apport", ""), ("virement_interne", "entrant"),
    ("remise_tps_tvq", ""), ("avance_honoraires", ""),
])
def test_a_create_of_a_kind_without_one_stores_none(client, fake, kind, sens):
    """F — a stale number the hidden field still held was stored on any
    kind."""
    form = _admin_form(kind, supplier_invoice_ref="F-9")
    if sens:
        form["sens_virement"] = sens
    resp = client.post("/administration/", data=form)
    assert resp.status_code == 302, resp.get_data(as_text=True)[:400]
    (stored,) = fake.peek_collection("admin_transactions").values()
    assert stored["supplier_invoice_ref"] == ""


@pytest.mark.parametrize("posted", ["", "F-2", None])
def test_an_edit_to_another_kind_keeps_the_stored_number(client, fake, posted):
    """F — the field, hidden but posted, rewrote or erased the number of an
    entry whose nature only changed; absent, the route turned it into an
    empty value, which erased it too."""
    entry = _admin("dépense", supplier_invoice_ref="F-1")
    before = fake.peek(f"admin_transactions/{entry['id']}")
    form = {**_admin_form("prélèvement", category=""), "expected_etag": before["etag"]}
    if posted is not None:
        form["supplier_invoice_ref"] = posted
    resp = client.post(f"/administration/{entry['id']}/modifier", data=form)
    assert resp.status_code == 302, resp.get_data(as_text=True)[:400]
    after = fake.peek(f"admin_transactions/{entry['id']}")
    assert after["kind"] == "prélèvement"
    assert after["supplier_invoice_ref"] == "F-1"
    assert "supplier_invoice_ref" not in after["revisions"][-1]["changes"]


def test_the_kinds_that_carry_one_still_write_it(client, fake):
    entry = _admin("dépense", supplier_invoice_ref="F-1")

    def edit(**over):
        etag = fake.peek(f"admin_transactions/{entry['id']}")["etag"]
        form = {**_admin_form("dépense", category="fournitures"),
                "expected_etag": etag, **over}
        resp = client.post(f"/administration/{entry['id']}/modifier", data=form)
        assert resp.status_code == 302, resp.get_data(as_text=True)[:400]
        return fake.peek(f"admin_transactions/{entry['id']}")["supplier_invoice_ref"]

    assert edit(supplier_invoice_ref="F-2") == "F-2"
    # F — absent (no Alpine posts it disabled) keeps it; it was erased.
    assert edit(description="sans le champ") == "F-2"
    assert edit(supplier_invoice_ref="") == ""
    # An « Autre recette » takes one (the connector's create whitelist).
    resp = client.post("/administration/", data=_admin_form(
        "recette_autre", supplier_invoice_ref="R-1"))
    assert resp.status_code == 302
    (other,) = [r for r in fake.peek_collection("admin_transactions").values()
                if r["kind"] == "recette_autre"]
    assert other["supplier_invoice_ref"] == "R-1"


def test_the_number_s_field_follows_the_kinds_that_carry_one(client, fake):
    """F — the field sat in the dépense-only block, never disabled."""
    page = client.get("/administration/nouvelle").get_data(as_text=True)
    assert ("supplierRefKinds: &#34;dépense,remboursement_depense,"
            "recette_autre&#34;.split(','),") in page
    src = (_ATHENA / "templates" / "administration" / "form.html").read_text(encoding="utf-8")
    block = src.split('x-show="supplierRefKinds.includes(kind)"', 1)[1].split("</div>", 1)[0]
    assert 'name="supplier_invoice_ref" :disabled="!supplierRefKinds.includes(kind)"' in block
    category = src.split('x-show="categoryKinds.includes(kind)"', 1)[1]
    assert 'name="supplier_invoice_ref"' not in category
    assert src.count('name="supplier_invoice_ref"') == 1


# ══════════════════════════════════════════════════════════════════════
# 4. The administration's entry page
# ══════════════════════════════════════════════════════════════════════

_SPLIT = '<p class="text-xs text-gray-500">TPS</p>'


def _detail(client, entry_id: str) -> str:
    resp = client.get(f"/administration/{entry_id}")
    assert resp.status_code == 200
    return html.unescape(resp.get_data(as_text=True))


def _label(page: str) -> str:
    return re.search(r'<div class="col-span-2"><p class="text-xs text-gray-500">'
                     r'([^<]*)</p>', page).group(1)


@pytest.mark.parametrize("kind, label", [
    ("dépense", "Fournisseur"), ("remboursement_depense", "Fournisseur"),
    ("prélèvement", "Versé à"), ("apport", "Reçu de"),
    ("virement_interne_sortant", "Compte de contrepartie"),
    ("remise_tps_tvq", "Administration fiscale"),
    ("avance_honoraires", "Reçu de"),
])
def test_an_entry_page_names_its_counterparty_by_its_type(client, fake, kind, label):
    """F — every page read « Payeur / Fournisseur »."""
    page = _detail(client, _admin(kind)["id"])
    assert _label(page) == label
    assert "Payeur / Fournisseur" not in page


def test_a_correction_takes_its_original_s_label_and_split(client, fake):
    """F — a correction's page read « Payeur / Fournisseur »; a dépense's
    correction (a recette) hid its split; an apport's showed 0,00 three
    times."""
    drawing = _admin("prélèvement")
    _, errs = al.reverse_transaction(drawing["id"], "Saisie en double")
    assert errs == []
    page = _detail(client, _correction_of(fake, drawing["id"])["id"])
    assert _label(page) == "Versé à" and _SPLIT not in page

    expense = _admin("dépense")
    al.reverse_transaction(expense["id"], "Saisie en double")
    fix = _correction_of(fake, expense["id"])
    assert fix["direction"] == "recette"
    page = _detail(client, fix["id"])
    assert _label(page) == "Fournisseur" and _SPLIT in page

    contribution = _admin("apport")
    al.reverse_transaction(contribution["id"], "Saisie en double")
    page = _detail(client, _correction_of(fake, contribution["id"])["id"])
    assert _label(page) == "Reçu de" and _SPLIT not in page


def test_a_refund_shows_its_split(client, fake):
    """F — a refund (a recette) hid its split."""
    page = _detail(client, _admin("remboursement_depense")["id"])
    assert _SPLIT in page and "86,98" in page.replace("\xa0", " ")


def test_a_legacy_correction_reads_its_original_or_falls_back(client, fake):
    """A correction written before ``reverses_kind`` (2026-10-07) takes its
    original's label through the page's read; with the original gone, the
    journal's own header and no split — never a blank, never a 500."""
    drawing = _admin("prélèvement")
    al.reverse_transaction(drawing["id"], "Saisie en double")
    legacy = dict(_correction_of(fake, drawing["id"]))
    legacy.pop("reverses_kind", None)
    legacy["id"] = "legacy-1"
    fake.seed("admin_transactions/legacy-1", legacy)
    assert _label(_detail(client, "legacy-1")) == "Versé à"
    orphan = dict(legacy, id="legacy-2", reverses_id="absent")
    fake.seed("admin_transactions/legacy-2", orphan)
    page = _detail(client, "legacy-2")
    assert _label(page) == al.PAYEE_LABEL_FALLBACK == "Fournisseur / Source"
    assert _SPLIT not in page
    # A legacy correction of a dépense: its split, through the same read.
    expense = _admin("dépense")
    al.reverse_transaction(expense["id"], "Saisie en double")
    legacy_split = dict(_correction_of(fake, expense["id"]))
    legacy_split.pop("reverses_kind", None)
    legacy_split["id"] = "legacy-3"
    fake.seed("admin_transactions/legacy-3", legacy_split)
    page = _detail(client, "legacy-3")
    assert _label(page) == "Fournisseur" and _SPLIT in page


def test_the_helpers_follow_the_counted_kind():
    assert al.counted_kind({"kind": "dépense"}) == "dépense"
    assert al.counted_kind({"kind": "correction", "reverses_kind": "apport"},
                           {"kind": "dépense"}) == "apport"
    assert al.counted_kind({"kind": "correction"}, {"kind": "dépense"}) == "dépense"
    assert al.counted_kind({"kind": "correction"}) == ""
    assert al.payee_label_of({"kind": "inconnu"}) == al.PAYEE_LABEL_FALLBACK
    assert al.payee_label_of({}) == al.PAYEE_LABEL_FALLBACK
    assert al.shows_split({"kind": "correction", "reverses_kind": "remboursement_depense"})
    assert not al.shows_split({"kind": "paiement_carte"})


# ══════════════════════════════════════════════════════════════════════
# 5. The trust form
# ══════════════════════════════════════════════════════════════════════


def _trust_form(**over) -> dict:
    f = {"account_id": "acc1", "direction": "recette", "amount": "",
         "purpose": "avance_honoraires", "method": "chèque",
         "counterparty": CLIENT, "dossier_id": "dos1", "client_id": "c1",
         "date": "2031-09-10", "reference": "", "description": ""}
    f.update(over)
    return f


def test_the_objet_menu_is_grouped_by_family(client, fake):
    """F — the « Objet » menu was one flat list."""
    page = client.get("/fideicommis/nouvelle").get_data(as_text=True)
    purpose = _menus(page)["purpose"]
    assert _grouped(purpose) == [
        ("Sommes reçues", ["avance_honoraires", "dépôt_client"]),
        ("Sommes versées", ["virement_honoraires", "remise_client", "déboursé_tiers"]),
        ("Selon le cas", ["règlement", "autre"]),
        ("Compte bancaire", ["intérêts", "frais_bancaires"]),
    ]
    assert purpose["groups"] == [g for g, _ in _grouped(purpose)]
    assert rt.ENTRY_FORM_PURPOSES[0] == "avance_honoraires"
    # The labels now come with the route's groups: each the model's.
    for option in purpose["options"]:
        assert option["label"].strip() == trust.PURPOSE_LABELS[option["value"]]


def test_the_trust_pickers_come_before_the_counterparty(client, fake):
    """F (D7) — the counterparty came before the dossier and the client; it
    still comes before the fee payment's payee select (D23)."""
    page = client.get("/fideicommis/nouvelle").get_data(as_text=True)
    at = [page.index(s) for s in ('name="q"', '<select name="client_id"',
                                  'name="counterparty" required',
                                  "Bénéficiaire du paiement d'honoraires")]
    assert at == sorted(at)
    assert page.index('id="trust-entry-types"') < page.index("purpose: &#34;")


def test_the_trust_label_follows_the_direction(client, fake):
    """F — one label, « Somme reçue de / Bénéficiaire du débours »."""
    page = html.unescape(client.get("/fideicommis/nouvelle").get_data(as_text=True))
    assert ">Somme reçue de</label>" in page
    resp = client.post("/fideicommis/", data=_trust_form(
        direction="déboursé", purpose="déboursé_tiers", counterparty="Huissier fictif"))
    assert resp.status_code == 400
    page = resp.get_data(as_text=True)
    assert ">Bénéficiaire du débours</label>" in html.unescape(page)
    block = _json_block(page, "trust-entry-types")
    assert block["payee_labels"] == trust.PAYEE_LABELS
    assert block["payee_label_fallback"] == trust.PAYEE_LABEL_FALLBACK


def _hints(page: str) -> dict:
    """Each dossier hint span: is it cloaked (hidden before Alpine)?"""
    out = {}
    for m in re.finditer(r'<span class="text-gray-400 font-normal" x-show="[^"]*"'
                         r'( x-cloak)?>\(([^)]*)\)</span>', page):
        out[m.group(2)] = bool(m.group(1))
    return out


def test_the_dossier_hint_follows_the_purpose(client, fake):
    """F — « laisser vide pour intérêts / frais bancaires » showed for every
    purpose, where all but those two need a dossier."""
    page = html.unescape(client.get("/fideicommis/nouvelle").get_data(as_text=True))
    assert _hints(page) == {"laisser vide pour intérêts / frais bancaires": True,
                            "obligatoire, avec son client": False}
    resp = client.post("/fideicommis/", data=_trust_form(
        purpose="intérêts", dossier_id="", client_id="", counterparty=BANK))
    assert resp.status_code == 400
    page = resp.get_data(as_text=True)
    assert _hints(html.unescape(page)) == {
        "laisser vide pour intérêts / frais bancaires": False,
        "obligatoire, avec son client": True}
    assert _json_block(page, "trust-entry-types")["optional_dossier"] == [
        "intérêts", "frais_bancaires"]
    # The locked view (from a dossier's tab) has no choice to hint at.
    page = html.unescape(client.get(
        "/fideicommis/nouvelle?dossier_id=dos1&locked=1").get_data(as_text=True))
    assert _hints(page) == {}
    # Once Alpine runs, each span follows the purpose: the route's list.
    src = (_ATHENA / "templates" / "trust" / "form.html").read_text(encoding="utf-8")
    assert ('x-show="(entryTypes.optional_dossier || []).includes(purpose)"'
            '{% if not dossier_optional %} x-cloak{% endif %}>(laisser vide pour '
            'intérêts / frais bancaires)</span>') in src
    assert ('x-show="!(entryTypes.optional_dossier || []).includes(purpose)"'
            '{% if dossier_optional %} x-cloak{% endif %}>(obligatoire, avec son '
            'client)</span>') in src


def test_each_account_option_carries_its_bank(client, fake):
    """F — interest and bank fees pre-fill with the account's bank."""
    fake.seed("trust_accounts/acc2", {
        "id": "acc2", "name": "Compte ancien", "status": "actif",
        "account_type": "général", "etag": "t0"})
    fake.seed("trust_accounts/acc3", {
        "id": "acc3", "name": "Compte spécial", "institution": 'Banque "X" & <Y>',
        "status": "actif", "account_type": "spécial", "etag": "t0"})
    page = client.get("/fideicommis/nouvelle").get_data(as_text=True)
    options = {o["value"]: o["attrs"] for o in _menus(page)["account_id"]["options"]}
    assert options["acc1"]["data-institution"] == BANK
    assert options["acc2"]["data-institution"] == ""
    assert options["acc3"]["data-institution"] == 'Banque "X" & <Y>'
    assert "&lt;Y&gt;" in page


TRUST_ALPINE_LINES = (
    "entryTypes: JSON.parse((document.getElementById('trust-entry-types') || {}).textContent || '{}'),",
    "institution: '', lastPrefill: null,",
    "prefillFor: function (p) { let t = this.entryTypes; if ((t.client_prefill || []).includes(p)) { let id = this.clientId; let c = this.clients.find(function (x) { return x.id === id; }); return c ? (c.name || '') : ''; } if ((t.bank_prefill || []).includes(p)) { return this.institution || ''; } return ''; },",
    "applyPrefill: function () { let f = this.$refs.counterparty; if (!this.entryTypes.prefill_on || !f) { return; } let v = this.prefillFor(this.purpose); if (f.value.trim() === '' || f.value === this.lastPrefill) { f.value = v; this.lastPrefill = v; } },",
)
TRUST_X_INIT = (
    "$watch('purpose', function (p) { if (implied[p]) { direction = implied[p]; } applyPrefill(); }); "
    "$watch('clientId', function () { applyPrefill(); }); "
    "institution = ($refs.account && $refs.account.selectedOptions.length) ? "
    "($refs.account.selectedOptions[0].dataset.institution || '') : ''; applyPrefill();")


def test_the_trust_prefill_data_and_its_switch(client, fake):
    """F — D6 on the trust form: the chosen client for a client's money,
    the account's bank for interest and fees; never on a refused POST or
    over a name arriving with the page."""
    page = client.get("/fideicommis/nouvelle").get_data(as_text=True)
    block = _json_block(page, "trust-entry-types")
    assert block["prefill_on"] is True
    assert block["client_prefill"] == ["avance_honoraires", "dépôt_client", "remise_client"]
    assert block["bank_prefill"] == ["intérêts", "frais_bancaires"]
    resp = client.post("/fideicommis/", data=_trust_form(counterparty=""))
    assert resp.status_code == 400
    assert _json_block(resp.get_data(as_text=True), "trust-entry-types")["prefill_on"] is False
    with client.application.test_request_context():
        assert rt._entry_form_extras(entry={"counterparty": "X"})["entry_types"][
            "prefill_on"] is False
        assert rt._entry_form_extras(entry={"counterparty": " "})["entry_types"][
            "prefill_on"] is True
    src = (_ATHENA / "templates" / "trust" / "form.html").read_text(encoding="utf-8")
    lines = [line.strip() for line in src.splitlines()]
    # Each line the pre-fill rests on (proved in headless Chrome against a
    # control; pytest runs no JavaScript — review of lot 5).
    for pinned in TRUST_ALPINE_LINES:
        assert pinned in lines, pinned
    assert re.search(r'x-init="([^"]*)"', src).group(1) == TRUST_X_INIT
    # The label follows the direction; the bank follows the account. (The
    # field lives in components/_champ_contrepartie.html since lot 6: its
    # bindings are checked on the rendered page.)
    assert ('x-text="(entryTypes.payee_labels || {})[direction] '
            '|| entryTypes.payee_label_fallback') in page
    assert ("@change=\"institution = $event.target.selectedOptions.length ? "
            "($event.target.selectedOptions[0].dataset.institution || '') : ''; "
            "applyPrefill()\"") in src
    # The refs: the FREE counterparty input (never the fee payment's select)
    # and the account select.
    free = re.search(r'<input type="text" name="counterparty"[^>]*>', page).group(0)
    assert 'x-ref="counterparty"' in free and "x-model" not in free
    assert 'x-ref="account"' in re.search(r'<select name="account_id"[^>]*', src).group(0)
    assert page.count('x-ref="counterparty"') == 1
    assert ("pickCounterparty: function (b) { let f = this.$refs.counterparty; "
            "f.value = b.dataset.counterparty; this.lastPrefill = null; "
            "this.cpOpen = false; this.cpSkip = true; f.focus(); },") in lines
    # The contract: the keys the code reads, and the clients' « name ».
    assert {"client_prefill", "bank_prefill", "prefill_on", "payee_labels",
            "payee_label_fallback", "optional_dossier"} <= set(block)
    rows = client.get("/fideicommis/dossier-search?q=2031").get_data(as_text=True)
    (clients,) = re.findall(r'data-clients="([^"]*)"', rows)
    assert json.loads(html.unescape(clients)) == [{"id": "c1", "name": CLIENT}]


def test_the_refused_post_keeps_its_client_and_a_new_search_drops_it():
    """F — a refused POST's client showed « Choisir le client… » (the x-for
    options had no selection), and a resubmit was refused; a new dossier
    search kept the old client, posted with no dossier. Proved in headless
    Chrome; the source carries both fixes."""
    src = (_ATHENA / "templates" / "trust" / "form.html").read_text(encoding="utf-8")
    assert '<option :value="c.id" x-text="c.name" :selected="c.id === clientId"></option>' in src
    assert "@input=\"dossierOpen = true; dossierId=''; clientId = '';" in src


def test_the_fee_payment_still_posts_one_counterparty(client, fake):
    """W (D23) — on a fee payment the free field is disabled and the payee
    select posts: exactly one « counterparty », the select, after the free
    field in the page."""
    page = client.get("/fideicommis/nouvelle").get_data(as_text=True)
    controls = re.findall(r'<(input|select)\b[^>]*name="counterparty"[^>]*>', page)
    assert controls == ["input", "select"]
    tags = re.findall(r'<(?:input|select)\b[^>]*name="counterparty"[^>]*>', page)
    enabled = [t for t in tags if ':disabled="purpose === \'virement_honoraires\'"' not in t
               and ':disabled="purpose !== \'virement_honoraires\'"' in t]
    assert len(enabled) == 1 and enabled[0].startswith("<select")
    assert ':disabled="purpose === \'virement_honoraires\'"' in tags[0]


# ══════════════════════════════════════════════════════════════════════
# 6. The trust entry page
# ══════════════════════════════════════════════════════════════════════


def _trust(**over) -> dict:
    d = {"account_id": "acc1", "direction": "recette", "amount": 50000,
         "purpose": "dépôt_client", "method": "chèque", "counterparty": CLIENT,
         "dossier_id": "dos1", "client_id": "c1", "date": _d(2031, 9, 2),
         "description": "", "reference": ""}
    d.update(over)
    entry, errs = trust.create_transaction(d)
    assert errs == [], errs
    return entry


def _trust_label(client, entry_id: str) -> str:
    resp = client.get(f"/fideicommis/{entry_id}")
    assert resp.status_code == 200
    return _label(html.unescape(resp.get_data(as_text=True)))


def test_a_trust_page_names_its_counterparty_by_direction(client, fake):
    """F — every page read « Somme reçue de / Bénéficiaire »; a correction
    takes its original's: the opposite of its own direction."""
    deposit = _trust()
    assert _trust_label(client, deposit["id"]) == "Somme reçue de"
    _, errs = trust.clear_transaction(deposit["id"], _d(2031, 9, 2))
    assert errs == []
    payment = _trust(direction="déboursé", purpose="déboursé_tiers",
                     counterparty="Huissier fictif", amount=10000,
                     date=_d(2031, 9, 3))
    assert _trust_label(client, payment["id"]) == "Bénéficiaire du débours"
    rev, errs = trust.reverse_transaction(payment["id"], "Saisie en double")
    assert errs == [] and rev["direction"] == "recette"
    assert _trust_label(client, rev["id"]) == "Bénéficiaire du débours"
    rev, errs = trust.reverse_transaction(deposit["id"], "Saisie en double")
    assert errs == [] and rev["direction"] == "déboursé"
    assert _trust_label(client, rev["id"]) == "Somme reçue de"


def test_the_trust_label_helper_never_blanks():
    assert trust.payee_label_of({}) == trust.PAYEE_LABEL_FALLBACK
    assert trust.payee_label_of({"direction": "?"}) == trust.PAYEE_LABEL_FALLBACK
    assert trust.payee_label_of({"direction": "recette", "reverses_id": "x"}) == (
        "Bénéficiaire du débours")
    assert trust.PAYEE_LABEL_FALLBACK == dict(trust.BARREAU_COLUMNS)["counterparty"]



# ══════════════════════════════════════════════════════════════════════
# 7. The suggestions (lot 6)
# ══════════════════════════════════════════════════════════════════════


def _contact(pid: str, role: str, **over) -> dict:
    doc = pm._default_doc()
    doc.update({"id": pid, "contact_role": role, **over})
    return doc


@pytest.fixture
def people(fake):
    for doc in (
        _contact("s1", "fournisseur", type="organization",
                 organization_name="Lévesque inc.", categorie_depense_defaut="fournitures"),
        _contact("h1", "huissier", type="individual", first_name="Jean",
                 last_name="Lévesque"),
        _contact("a1", "avocat_adverse", type="individual", prefix="Me",
                 first_name="Marie", last_name="Lévesque"),
        _contact("c9", "client", type="individual", first_name="Luc",
                 last_name="Lévesque"),
        _contact("t1", "fournisseur", type="organization",
                 organization_name="Société Légale inc.", trade_name="Papeterie Levant"),
    ):
        fake.seed(f"parties/{doc['id']}", doc)
    return fake


def _suggest(client, url: str):
    resp = client.get(url)
    assert resp.status_code == 200
    assert resp.headers.get("X-Athena-Fragment") == "contreparties"
    page = resp.get_data(as_text=True)
    names = [html.unescape(n) for n in re.findall(
        r'<button type="button"[^>]*data-counterparty="([^"]*)"', page)]
    return page, names


def test_the_fold_is_the_report_s():
    from utils.rapprochement import plier_contrepartie
    for sample in ("Lévesque inc.", "O'Brien & « Fils »", "  ÉCOLE  ", "—", ""):
        assert cp.fold(sample) == plier_contrepartie(sample)


def test_the_ranking():
    pinned = [{"name": "Client Fictif inc.", "detail": "ce dossier"}]
    fixed = [{"name": "Revenu Québec"}]
    contacts = [{"name": f"Nom {i}", "alternates": [f"Lévesque{i}"]} for i in range(10)]
    contacts.append({"name": "Paul Valévesque", "alternates": []})   # contains
    rows, more = cp.rank(pinned, fixed, contacts, "")
    assert [r["name"] for r in rows] == ["Client Fictif inc.", "Revenu Québec"] and not more
    # One letter: no contact searched yet.
    assert [r["name"] for r in cp.rank(pinned, fixed, contacts, "l")[0]] == [
        "Client Fictif inc."]
    rows, more = cp.rank(pinned, fixed, contacts, "lev")
    assert len(rows) == cp.SUGGESTION_LIMIT and more
    assert rows[0]["name"] == "Nom 0"               # starts-with before contains
    rows, _ = cp.rank([], [], contacts[-1:] + contacts[:1], "lev")
    assert [r["name"] for r in rows] == ["Nom 0", "Paul Valévesque"]
    # A name already listed (folded) is skipped.
    rows, _ = cp.rank([{"name": "Lévesque inc."}], [], [{"name": "LEVESQUE INC"}], "lev")
    assert [r["name"] for r in rows] == ["Lévesque inc."]


def test_a_pick_writes_the_trade_name_else_the_legal_name_never_a_title():
    from utils import civilite
    assert civilite.payee_name({"type": "organization", "organization_name": "Société Légale inc.",
                                "trade_name": "Papeterie Levant"}) == "Papeterie Levant"
    assert civilite.payee_name({"type": "organization",
                                "organization_name": "Lévesque inc."}) == "Lévesque inc."
    assert civilite.payee_name({"type": "individual", "prefix": "Me",
                                "first_name": "Marie", "last_name": "Lévesque"}) == "Marie Lévesque"


def test_the_reader_by_role_is_strict_and_capped(people, monkeypatch):
    for bad in ((), ("",), ("inconnu",), ("fournisseur", "x")):
        with pytest.raises(ValueError):
            pm.list_by_roles_strict(bad)
    rows, truncated = pm.list_by_roles_strict(("fournisseur",))
    assert sorted(r["id"] for r in rows) == ["s1", "t1"] and not truncated
    rows, truncated = pm.list_by_roles_strict(("fournisseur",), limit=1)
    assert len(rows) == 1 and truncated


def test_a_depense_suggests_suppliers_and_professionals_never_a_client(client, people):
    """F — the administration form suggested nothing."""
    page, names = _suggest(client, "/administration/counterparty-suggest?kind=d%C3%A9pense&counterparty=lev")
    assert set(names) == {"Lévesque inc.", "Jean Lévesque", "Marie Lévesque",
                          "Papeterie Levant"}
    assert "Luc Lévesque" not in names and "Me " not in page
    assert 'data-category="fournitures"' in page          # the supplier's usual one
    _page, names = _suggest(client, "/administration/counterparty-suggest?kind=d%C3%A9pense&counterparty=xyz")
    assert names == []


def test_an_encaissement_lists_the_invoice_s_client_first(client, people):
    page, names = _suggest(client, "/administration/counterparty-suggest"
                                   "?kind=encaissement_facture&invoice_id=inv1&counterparty=")
    assert names[0] == CLIENT and "client de la facture" in page


def test_a_drawing_or_an_unknown_type_reads_no_contact(client, people, monkeypatch):
    calls = []
    real = pm.list_by_roles_strict
    monkeypatch.setattr(pm, "list_by_roles_strict",
                        lambda *a, **k: calls.append(a) or real(*a, **k))
    for kind in ("pr%C3%A9l%C3%A8vement", "inconnu", ""):
        page, names = _suggest(client, f"/administration/counterparty-suggest?kind={kind}&counterparty=lev")
        assert names == [] and page == ""
    assert calls == []


def test_a_failed_read_says_unavailable_and_logs_no_name(client, people, monkeypatch, caplog):
    def broken(*_a, **_k):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(pm, "list_by_roles_strict", broken)
    canary = "Zéphyrin Canari"
    page, names = _suggest(client, f"/administration/counterparty-suggest?kind=d%C3%A9pense&counterparty={canary}")
    assert "Suggestions indisponibles — saisissez le nom." in page and names == []
    assert "Aucun" not in page
    logged = " ".join(r.getMessage() + str(getattr(r, "json_fields", "")) for r in caplog.records)
    assert "counterparty suggestions failed" in logged and canary not in logged


def test_names_are_escaped_and_eight_rows_say_precisez(client, fake):
    fake.seed("parties/x1", _contact("x1", "fournisseur", type="organization",
                                     organization_name='O\'Brien & "Fils" <x>'))
    for i in range(10):
        fake.seed(f"parties/n{i}", _contact(f"n{i}", "notaire", type="individual",
                                            first_name="Ophélie", last_name=f"Obrien{i}"))
    page, names = _suggest(client, "/administration/counterparty-suggest?kind=d%C3%A9pense&counterparty=ob")
    assert len(names) == cp.SUGGESTION_LIMIT and "précisez" in page
    page, names = _suggest(client, "/administration/counterparty-suggest?kind=d%C3%A9pense&counterparty=o%27b")
    assert '<x>' not in page and "&lt;x&gt;" in page
    assert 'O\'Brien & "Fils" <x>' in names


def test_a_trust_payment_to_a_third_party_never_offers_a_client(client, people):
    """F — the trust form offered the dossier's clients and opposing parties
    for every purpose, and ignored what was typed."""
    _page, names = _suggest(client, "/fideicommis/counterparty-suggest"
                                    "?purpose=d%C3%A9bours%C3%A9_tiers&dossier_id=dos1&counterparty=lev")
    assert set(names) == {"Lévesque inc.", "Jean Lévesque", "Marie Lévesque", "Papeterie Levant"}
    _page, names = _suggest(client, "/fideicommis/counterparty-suggest"
                                    "?purpose=avance_honoraires&dossier_id=dos1&counterparty=")
    assert names == [CLIENT]
    _page, names = _suggest(client, "/fideicommis/counterparty-suggest"
                                    "?purpose=r%C3%A8glement&dossier_id=dos1&counterparty=zzz")
    assert names == []
    # A page rendered before lot 6 (no purpose): the dossier's parties.
    _page, names = _suggest(client, "/fideicommis/counterparty-suggest?dossier_id=dos1&counterparty=zzz")
    assert names == [CLIENT]


def test_an_unknown_name_still_saves(client, fake):
    resp = client.post("/administration/", data=_admin_form("dépense", category="fournitures",
                                                             counterparty="Nom jamais vu"))
    assert resp.status_code == 302
    (stored,) = fake.peek_collection("admin_transactions").values()
    assert stored["counterparty"] == "Nom jamais vu"


# ══════════════════════════════════════════════════════════════════════
# Lot 7 (2026-11-12) — « Que fait chaque type ? » mène à la carte du type
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def client_hub(client):
    with mock.patch("google.cloud.firestore.Client"):
        import routes.comptabilite as rc
    client.application.register_blueprint(rc.comptabilite_bp)
    return client


def test_lot7_les_formulaires_menent_a_la_carte_du_type(client_hub):
    page = client_hub.get("/administration/nouvelle").get_data(as_text=True)
    assert """:href="'/comptabilite/types-ecritures#type-' + kind" """.strip() in page
    page = client_hub.get("/fideicommis/nouvelle?dossier_id=dos1").get_data(as_text=True)
    assert (""":href="'/comptabilite/types-ecritures#type-fideicommis-' + purpose" """
            .strip() in page)


def test_lot7_sans_le_hub_les_formulaires_repondent_sans_lien(client):
    """Une application sans le hub (ce banc) : le formulaire répond, sans
    l'aide — jamais un BuildError pour un lien d'aide."""
    for url in ("/administration/nouvelle", "/fideicommis/nouvelle?dossier_id=dos1"):
        resp = client.get(url)
        assert resp.status_code == 200, url
        assert "Que fait chaque" not in resp.get_data(as_text=True), url
