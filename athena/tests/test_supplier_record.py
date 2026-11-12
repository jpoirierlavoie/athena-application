"""The supplier record (2026-11-11): the TPS and TVQ numbers, the usual
expense category and the firm's account number of a supplier or a
professional (``utils/contact_roles.PAYEE_ROLES``).

* The model normalises the tax numbers (spaces dropped, letters
  upper-cased) and refuses a wrong shape. It refuses a category outside the
  expense vocabulary, and an account number too long or that the sanitizer
  would alter — never trimmed.
* A filled field is refused on any other role. A record kept from a former
  role stays dormant, so it never locks the contact.
* The form shows the section for the payee roles and disables it — on the
  server too — for any other. The fiche shows the record with the numbers
  spaced.
* A professional's phone edit keeps the record, which is never on the
  vCard, and the connector serves none of it (the lawyer's decision D3).

The bench: the real routes, models, templates and CardDAV handlers on
``tests/_fake_firestore.py``. Every name and number here is invented.
"""

import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.carddav as carddav
    import dav.sync as dav_sync
    import mcp.handlers as handlers
    import mcp.write_support as write_support
    from models import admin_ledger
    from models import partie as pm
    import routes.doc_templates as doc_templates_routes
    import routes.dossiers as dossiers_routes
    import routes.parties as parties_routes
    import routes.reception as reception_routes

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils import contact_roles  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.html_attr import jsattr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.markdown_docx import markdown_to_safe_html  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

_ = (dav_sync, write_support)

UTC = timezone.utc
AUTH = {"Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTpwdw=="}
TPS, TVQ = "123456789RT0001", "1234567890TQ0001"
CATEGORY = admin_ledger.ADMIN_EXPENSE_CATEGORIES[0]


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n in ("dav.sync", "mcp.write_support"))
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, *_fake_modules())


def _contact(pid: str, role: str, **over) -> dict:
    doc = pm._default_doc()
    doc.update({
        "id": pid, "type": "organization", "contact_role": role,
        "organization_name": f"Entreprise fictive {pid}",
        "etag": f"etag-{pid}", "vcard_uid": f"uid-{pid}",
    })
    doc.update(over)
    return doc


def _create(**fields):
    return pm.create_partie({
        "type": "organization", "contact_role": "fournisseur",
        "organization_name": "Fournitures Exemple inc.", **fields})


@pytest.fixture
def web(db):
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms,
                                 csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl, phone=format_phone_display,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=jsattr, markdown=markdown_to_safe_html,
    )
    for bp in (parties_routes.parties_bp, dossiers_routes.dossiers_bp,
               doc_templates_routes.doc_templates_bp,
               reception_routes.reception_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["user_email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


# ── The model ──────────────────────────────────────────────────────────────


def test_the_record_is_one_list_for_the_payee_roles():
    assert pm.SUPPLIER_RECORD_FIELDS == (
        "numero_tps", "numero_tvq", "categorie_depense_defaut",
        "numero_compte_fournisseur")
    defaults = pm._default_doc()
    assert all(defaults[f] == "" for f in pm.SUPPLIER_RECORD_FIELDS)
    assert set(contact_roles.PAYEE_ROLES) == (
        set(contact_roles.SUPPLIER_ROLES) | set(contact_roles.PROFESSIONAL_ROLES))


def test_a_supplier_keeps_its_numbers_without_spaces(db):
    doc, errors = _create(numero_tps=" 123456789 rt 0001 ",
                          numero_tvq="1234567890 tq 0001",
                          categorie_depense_defaut=CATEGORY,
                          numero_compte_fournisseur="C-445 566")
    assert errors == []
    stored = db.peek(f"parties/{doc['id']}")
    assert (stored["numero_tps"], stored["numero_tvq"]) == (TPS, TVQ)
    assert stored["categorie_depense_defaut"] == CATEGORY
    assert stored["numero_compte_fournisseur"] == "C-445 566"
    assert pm.format_tax_number(TPS) == "123456789 RT 0001"
    assert pm.format_tax_number(TVQ) == "1234567890 TQ 0001"
    assert pm.format_tax_number("n'importe") == "n'importe"


@pytest.mark.parametrize("field, value, message", [
    ("numero_tps", "12345678RT0001", pm.TPS_INVALID),
    ("numero_tps", "123456789RX0001", pm.TPS_INVALID),
    ("numero_tps", TVQ, pm.TPS_INVALID),
    # Python's \d would take full-width digits: ASCII 0-9 only.
    ("numero_tps", "１２３４５６７８９RT０００１", pm.TPS_INVALID),
    ("numero_tvq", TPS, pm.TVQ_INVALID),
    ("numero_tvq", "1234567890TQ001", pm.TVQ_INVALID),
    ("categorie_depense_defaut", "plomberie", pm.CATEGORY_INVALID),
    ("numero_compte_fournisseur", "x" * 101, pm.ACCOUNT_INVALID),
    ("numero_compte_fournisseur", "<b>C-1</b>", pm.ACCOUNT_INVALID),
])
def test_a_malformed_field_is_refused_and_nothing_written(db, field, value,
                                                          message):
    doc, errors = _create(**{field: value})
    assert doc is None and errors == [message]
    assert db.peek_collection("parties") == {}


def test_a_record_on_a_client_is_refused_but_an_empty_one_is_fine(db):
    doc, errors = pm.create_partie({
        "type": "organization", "contact_role": "client",
        "organization_name": "Client Exemple inc.", "numero_tps": TPS})
    assert doc is None and errors == [pm.SUPPLIER_RECORD_ROLE]
    doc, errors = pm.create_partie({
        "type": "organization", "contact_role": "client",
        "organization_name": "Client Exemple inc.", "numero_tps": "",
        "numero_compte_fournisseur": ""})
    assert errors == [] and doc["contact_role"] == "client"


def test_a_professional_holds_a_record_too(db):
    doc, errors = pm.create_partie({
        "type": "individual", "contact_role": "huissier",
        "first_name": "Prénom", "last_name": "Fictif", "numero_tps": TPS})
    assert errors == [] and doc["numero_tps"] == TPS


def test_a_dormant_record_never_locks_the_contact(db):
    db.seed("parties/fou", _contact("fou", "fournisseur", numero_tps=TPS))
    # Becomes a client: the record, not sent, stays dormant.
    doc, errors = pm.update_partie("fou", {"contact_role": "client"})
    assert errors == [] and db.peek("parties/fou")["numero_tps"] == TPS
    # An unrelated edit of that client does not trip on it.
    doc, errors = pm.update_partie("fou", {"notes": "Note fictive."})
    assert errors == []
    # Back to supplier, the record is there again.
    doc, errors = pm.update_partie("fou", {"contact_role": "fournisseur"})
    assert errors == [] and doc["numero_tps"] == TPS


# ── The web ────────────────────────────────────────────────────────────────


def _fieldset(body: str) -> str:
    start = body.index("Informations de fournisseur</h2>")
    return body[start:body.index("</fieldset>", start)]


def _fieldset_tag(body: str) -> str:
    """The record fieldset's OPENING tag, attributes included."""
    section = _fieldset(body)
    start = section.index("<fieldset")
    return section[start:section.index(">", start) + 1]


_SERVER_DISABLED = re.compile(r'<fieldset class="min-w-0"\s+disabled\s')


def test_the_form_offers_the_section_to_a_payee_and_disables_it_otherwise(
        db, web):
    db.seed("parties/fou", _contact("fou", "fournisseur", numero_tps=TPS))
    db.seed("parties/cli", _contact("cli", "client"))
    supplier = web.get("/parties/fou/edit").get_data(as_text=True)
    tag = _fieldset_tag(supplier)
    assert not _SERVER_DISABLED.search(tag)
    assert ':disabled="!payeeRoles.includes(contactRole)"' in tag
    assert 'value="123456789 RT 0001"' in _fieldset(supplier)
    client = web.get("/parties/cli/edit").get_data(as_text=True)
    assert _SERVER_DISABLED.search(_fieldset_tag(client))


def test_the_alpine_side_reads_the_one_list_of_payee_roles(db, web):
    db.seed("parties/fou", _contact("fou", "fournisseur"))
    body = web.get("/parties/fou/edit").get_data(as_text=True)
    assert 'x-show="payeeRoles.includes(contactRole)"' in body
    payee = ",".join(contact_roles.PAYEE_ROLES)
    assert f"payeeRoles: {jsattr(payee)}.split(',')" in body


def test_a_supplier_saved_on_the_web_stores_its_record(db, web):
    db.seed("parties/fou", _contact("fou", "fournisseur"))
    resp = web.post("/parties/fou", data={
        "type": "organization", "contact_role": "fournisseur",
        "organization_name": "Entreprise fictive fou", "expected_etag": "etag-fou",
        "numero_tps": "123456789 RT 0001", "numero_tvq": "1234567890 TQ 0001",
        "categorie_depense_defaut": CATEGORY, "numero_compte_fournisseur": "A-9",
    })
    assert resp.status_code in (302, 303)
    stored = db.peek("parties/fou")
    assert (stored["numero_tps"], stored["numero_tvq"]) == (TPS, TVQ)
    assert stored["numero_compte_fournisseur"] == "A-9"


def test_a_client_save_never_touches_a_dormant_record(db, web):
    db.seed("parties/cli", _contact("cli", "client", numero_tps=TPS))
    # The browser posts no record field: the fieldset is disabled.
    resp = web.post("/parties/cli", data={
        "type": "organization", "contact_role": "client",
        "organization_name": "Entreprise fictive cli", "expected_etag": "etag-cli",
    })
    assert resp.status_code in (302, 303)
    assert db.peek("parties/cli")["numero_tps"] == TPS


def test_an_unchanged_record_reposted_under_another_role_is_accepted(db, web):
    """A page where Alpine never ran posts the stored record (its fieldset
    enabled on the server for a payee): only a CHANGED field is refused."""
    db.seed("parties/fou", _contact("fou", "fournisseur", numero_tps=TPS,
                                    numero_compte_fournisseur="A-9"))
    unchanged = {
        "type": "organization", "contact_role": "client",
        "organization_name": "Entreprise fictive fou", "expected_etag": "etag-fou",
        "numero_tps": "123456789 RT 0001", "numero_tvq": "",
        "categorie_depense_defaut": "", "numero_compte_fournisseur": "A-9",
    }
    assert web.post("/parties/fou", data=unchanged).status_code in (302, 303)
    stored = db.peek("parties/fou")
    assert stored["contact_role"] == "client" and stored["numero_tps"] == TPS
    changed = {**unchanged, "expected_etag": stored["etag"],
               "numero_tps": "987654321 RT 0001"}
    resp = web.post("/parties/fou", data=changed)
    assert resp.status_code == 200 and pm.SUPPLIER_RECORD_ROLE in resp.get_data(
        as_text=True).replace("&#39;", "'")
    assert db.peek("parties/fou")["numero_tps"] == TPS


def test_a_refused_save_redisplays_the_stored_record_and_the_switch_keeps_it(
        db, web):
    db.seed("parties/fou", _contact("fou", "fournisseur", numero_tps=TPS,
                                    categorie_depense_defaut=CATEGORY,
                                    numero_compte_fournisseur="A-9"))
    # Role set to « Client » (the fieldset disabled: nothing posted) and a
    # field refused.
    resp = web.post("/parties/fou", data={
        "type": "organization", "contact_role": "client",
        "organization_name": "Entreprise fictive fou", "expected_etag": "etag-fou",
        "email_work": "pas-un-courriel",
    })
    assert resp.status_code == 200
    section = _fieldset(resp.get_data(as_text=True))
    assert 'value="123456789 RT 0001"' in section and 'value="A-9"' in section
    assert f'<option value="{CATEGORY}" selected>' in section
    # Back to « Fournisseur »: the browser posts what the page shows.
    resp = web.post("/parties/fou", data={
        "type": "organization", "contact_role": "fournisseur",
        "organization_name": "Entreprise fictive fou", "expected_etag": "etag-fou",
        "numero_tps": "123456789 RT 0001", "numero_tvq": "",
        "categorie_depense_defaut": CATEGORY, "numero_compte_fournisseur": "A-9",
    })
    assert resp.status_code in (302, 303)
    stored = db.peek("parties/fou")
    assert (stored["numero_tps"], stored["numero_compte_fournisseur"],
            stored["categorie_depense_defaut"]) == (TPS, "A-9", CATEGORY)


def test_a_refused_number_redisplays_with_its_reason(db, web):
    db.seed("parties/fou", _contact("fou", "fournisseur"))
    resp = web.post("/parties/fou", data={
        "type": "organization", "contact_role": "fournisseur",
        "organization_name": "Entreprise fictive fou", "expected_etag": "etag-fou",
        "numero_tps": "12345",
    })
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Numéro de TPS invalide" in body
    assert 'value="12345"' in body
    assert db.peek("parties/fou")["numero_tps"] == ""


def test_the_fiche_shows_the_record_of_a_payee_only(db, web):
    db.seed("parties/fou", _contact("fou", "fournisseur", numero_tps=TPS,
                                    categorie_depense_defaut=CATEGORY))
    db.seed("parties/cli", _contact("cli", "client", numero_tps=TPS))
    supplier = web.get("/parties/fou").get_data(as_text=True)
    assert "Informations de fournisseur" in supplier
    assert "123456789 RT 0001" in supplier
    assert admin_ledger.ADMIN_CATEGORY_LABELS[CATEGORY] in supplier
    # An empty row reads « — », never « Non inscrit » (« not a registrant »).
    assert "Non inscrit" not in supplier
    assert re.search(r'text-gray-400">—</dd>', supplier)
    client = web.get("/parties/cli").get_data(as_text=True)
    assert "Informations de fournisseur" not in client
    assert "123456789 RT 0001" not in client


# ── The phone and the connector ────────────────────────────────────────────


def test_a_professionals_phone_edit_keeps_the_record(db, monkeypatch):
    monkeypatch.setattr("dav.dav_auth._check_credentials", lambda u, p: True)
    monkeypatch.setattr("dav.dav_auth._check_success_cache", lambda u, p: True)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(carddav.carddav_bp)
    db.seed("parties/hui", _contact("hui", "huissier", numero_tps=TPS,
                                    numero_compte_fournisseur="H-1"))
    card = pm.partie_to_vcard(db.peek("parties/hui"))
    assert TPS not in card and "H-1" not in card
    resp = app.test_client().put(
        "/dav/addressbook/hui.vcf", data=card.encode("utf-8"),
        headers={**AUTH, "Content-Type": "text/vcard", "If-Match": '"etag-hui"'})
    assert resp.status_code == 204
    stored = db.peek("parties/hui")
    assert stored["numero_tps"] == TPS and stored["numero_compte_fournisseur"] == "H-1"


def test_the_connector_serves_none_of_the_record(db):
    db.seed("parties/fou", _contact("fou", "fournisseur", numero_tps=TPS,
                                    numero_tvq=TVQ, numero_compte_fournisseur="A-9",
                                    categorie_depense_defaut=CATEGORY))
    payload = json.dumps(handlers.get_partie({"partie_id": "fou"}), default=str)
    assert all(field not in payload for field in pm.SUPPLIER_RECORD_FIELDS)
    assert TPS not in payload and "A-9" not in payload
