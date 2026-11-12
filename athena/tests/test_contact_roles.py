"""Contact roles: one vocabulary, with « Fournisseur » and « Sténographe »
(2026-11-11).

``utils/contact_roles.py`` is the one list of contact roles and their
labels. The contact model, the connector's three enums and the generated
documents' work-address rule read it, and these tests pin every copy to
it. Also covered:

* the vCard CATEGORIES label — the role's wire format to DavX5 — maps back
  to its role, the pre-rename labels included;
* the contact list's ``?role=`` filter is checked against the vocabulary;
* the form marks the stored role ``selected`` on the server, and an edit
  posted without a role keeps it (it used to become « client »);
* a supplier gets none of a client's identity checks;
* the new roles are written to at their work address;
* the connector creates and lists suppliers within its output contract.

The bench: the real routes, models and templates on
``tests/_fake_firestore.py``. Every name here is invented.
"""

import os
import pathlib
import re
import sys
import unicodedata
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
    import dav.sync as dav_sync  # its db is patched below
    import mcp.handlers as handlers
    import mcp.tools as tools
    import mcp.write_support as write_support
    from models import partie as pm
    import routes.doc_templates as doc_templates_routes
    import routes.dossiers as dossiers_routes
    import routes.parties as parties_routes
    import routes.reception as reception_routes

from flask import Flask  # noqa: E402

from mcp.output_schemas import OUTPUT_SCHEMAS  # noqa: E402
from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils import contact_roles, kyc, template_fields  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.html_attr import jsattr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.markdown_docx import markdown_to_safe_html  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

# Loaded for their side effect: the fake store is installed on every
# LOADED module holding a `db`.
_ = (dav_sync, write_support)

UTC = timezone.utc

# The labels of the eight roles that predate 2026-11-11 — the vCard wire
# format phones already hold, so they never change.
_SHIPPED_LABELS = {
    "client": "Client",
    "partie_adverse": "Partie",
    "avocat_adverse": "Avocat(e)",
    "témoin": "Témoin",
    "expert": "Expert(e)",
    "huissier": "Huissier(ère)",
    "notaire": "Notaire",
    "autre": "Autre",
}


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
    # The fiche links to the dossiers, the gabarits and Réception.
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


# ── One vocabulary ─────────────────────────────────────────────────────────


def test_every_copy_of_the_roles_is_the_one_vocabulary():
    assert pm.VALID_CONTACT_ROLES == contact_roles.ROLES
    assert pm.ROLE_LABELS == contact_roles.ROLE_LABELS
    assert tuple(contact_roles.ROLE_LABELS) == contact_roles.ROLES
    assert tools._CONTACT_ROLES == list(contact_roles.ROLES)
    for tool in ("list_parties", "create_partie", "update_partie"):
        prop = tools.TOOLS[tool]["input_schema"]["properties"]["contact_role"]
        assert prop["enum"] == list(contact_roles.ROLES), tool


def test_the_new_roles_come_last_before_autre_and_the_shipped_labels_hold():
    assert contact_roles.ROLES[-3:] == ("sténographe", "fournisseur", "autre")
    assert contact_roles.ROLE_LABELS["sténographe"] == "Sténographe"
    assert contact_roles.ROLE_LABELS["fournisseur"] == "Fournisseur"
    # A shipped label is the vCard wire format: never renamed.
    assert {k: contact_roles.ROLE_LABELS[k] for k in _SHIPPED_LABELS} == _SHIPPED_LABELS
    # The first seven keep their order (the forms' menu order).
    assert contact_roles.ROLES[:7] == tuple(_SHIPPED_LABELS)[:7]


def test_the_role_groups_are_roles_and_the_work_rule_is_derived():
    groups = (contact_roles.PROFESSIONAL_ROLES, contact_roles.SUPPLIER_ROLES,
              tuple(contact_roles.PHONE_EXCLUDED_ROLES))
    for group in groups:
        assert set(group) <= set(contact_roles.ROLES), group
    assert contact_roles.WORK_PREFERRED_ROLES == (
        set(contact_roles.PROFESSIONAL_ROLES) | set(contact_roles.SUPPLIER_ROLES))
    assert template_fields._WORK_PREFERRED_ROLES == contact_roles.WORK_PREFERRED_ROLES
    assert set(contact_roles.PROFESSIONAL_ROLES) == {
        "avocat_adverse", "notaire", "huissier", "expert", "sténographe"}
    assert contact_roles.PHONE_EXCLUDED_ROLES == {"fournisseur"}


# ── The vCard label ────────────────────────────────────────────────────────


def test_every_label_maps_back_to_its_role():
    for role, label in contact_roles.ROLE_LABELS.items():
        assert contact_roles.role_for_vcard_label(label) == role
    assert contact_roles.role_for_vcard_label("Partie adverse") == "partie_adverse"
    assert contact_roles.role_for_vcard_label("Avocat(e) adverse") == "avocat_adverse"
    assert contact_roles.role_for_vcard_label("Plombier") == "autre"
    assert contact_roles.role_for_vcard_label("") == "autre"
    # A phone may send decomposed accents, or surrounding spaces.
    decomposed = unicodedata.normalize("NFD", "Sténographe")
    assert decomposed != "Sténographe"
    assert contact_roles.role_for_vcard_label(f" {decomposed} ") == "sténographe"


@pytest.mark.parametrize("labels, legacy, message", [
    ({"client": "Client", "autre": "Client"}, {}, "names two roles"),
    ({"client": "Client, cabinet", "autre": "Autre"}, {}, "holds a comma"),
    ({"client": "Client", "autre": "Autre"}, {"Ancien": "inconnu"},
     "names no role"),
    ({"client": "Client", "autre": "Autre"}, {"Autre": "client"},
     "names two roles"),
])
def test_a_label_collision_or_a_comma_is_refused(monkeypatch, labels, legacy,
                                                 message):
    monkeypatch.setattr(contact_roles, "ROLE_LABELS", labels)
    monkeypatch.setattr(contact_roles, "VCARD_LEGACY_LABELS", legacy)
    with pytest.raises(ValueError, match=message):
        contact_roles._vcard_reverse_map()


@pytest.mark.parametrize("role", contact_roles.ROLES)
def test_every_role_round_trips_through_the_vcard(role):
    card = pm.partie_to_vcard(_contact("r1", role))
    assert f"CATEGORIES:{contact_roles.ROLE_LABELS[role]}" in card
    assert pm.vcard_to_partie(card)["contact_role"] == role


# ── The contact list and form ──────────────────────────────────────────────


def test_an_unknown_role_filter_shows_the_clients_not_every_contact(db, web):
    db.seed("parties/cli", _contact("cli", "client"))
    db.seed("parties/fou", _contact("fou", "fournisseur"))
    body = web.get("/parties/?role=nimporte").get_data(as_text=True)
    assert "Entreprise fictive cli" in body
    assert "Entreprise fictive fou" not in body
    suppliers = web.get("/parties/?role=fournisseur").get_data(as_text=True)
    assert "Entreprise fictive fou" in suppliers
    assert "Entreprise fictive cli" not in suppliers
    every = web.get("/parties/?role=tous").get_data(as_text=True)
    assert "Entreprise fictive cli" in every and "Entreprise fictive fou" in every


def test_the_edit_form_marks_the_stored_role_selected(db, web):
    db.seed("parties/fou", _contact("fou", "fournisseur"))
    body = web.get("/parties/fou/edit").get_data(as_text=True)
    selected = re.findall(r'<option value="([^"]+)" selected>', body)
    assert "fournisseur" in selected
    assert "client" not in selected


def test_an_edit_posted_without_a_role_keeps_the_stored_one(db, web):
    db.seed("parties/fou", _contact("fou", "fournisseur"))
    resp = web.post("/parties/fou", data={
        "type": "organization", "organization_name": "Entreprise fictive fou",
        "expected_etag": "etag-fou",
    })
    assert resp.status_code in (302, 303)
    assert db.peek("parties/fou")["contact_role"] == "fournisseur"


def test_a_refused_edit_without_a_role_redisplays_the_stored_one(db, web):
    db.seed("parties/fou", _contact("fou", "fournisseur"))
    resp = web.post("/parties/fou", data={
        "type": "organization", "organization_name": "",
        "expected_etag": "etag-fou",
    })
    assert resp.status_code == 200
    selected = re.findall(r'<option value="([^"]+)" selected>',
                          resp.get_data(as_text=True))
    assert "fournisseur" in selected and "client" not in selected


def test_a_supplier_gets_no_identity_check(db, web):
    db.seed("parties/cli", _contact("cli", "client"))
    db.seed("parties/fou", _contact("fou", "fournisseur"))
    rows = web.get("/parties/?role=tous").get_data(as_text=True)
    # The client's dot only (one span, its text in aria-label and title).
    assert rows.count('aria-label="Identité à faire"') == 1
    with pytest.raises(tools.ToolArgumentError):
        check = next(iter(handlers._KYC_CHECK_FIELDS))
        field = handlers._KYC_CHECK_FIELDS[check]
        handlers._record_kyc_status_impl({
            "partie_id": "fou", "check": check,
            "status": kyc.STATUSES[field][-1],
        })
    assert db.peek("parties/fou")[
        handlers._KYC_CHECK_FIELDS[next(iter(handlers._KYC_CHECK_FIELDS))]
    ] == kyc.NON_VERIFIE


def test_the_fiche_shows_the_conformite_card_to_a_client_only(db, web):
    db.seed("parties/cli", _contact("cli", "client"))
    db.seed("parties/fou", _contact("fou", "fournisseur"))
    card = ">Conformité</h2>"
    assert card in web.get("/parties/cli").get_data(as_text=True)
    supplier = web.get("/parties/fou").get_data(as_text=True)
    assert card not in supplier
    assert "Fournisseur" in supplier and "bg-teal-100 text-teal-700" in supplier


# ── Documents ──────────────────────────────────────────────────────────────


def _with_both_addresses(role: str, kind: str = "individual") -> dict:
    return {
        "type": kind, "contact_role": role, "first_name": "Prénom",
        "last_name": "Fictif", "organization_name": "Fictive inc.",
        "address_street": "1 rue Domicile", "address_city": "Laval",
        "address_province": "Québec", "address_postal_code": "H7A 1A1",
        "address_country": "Canada",
        "work_address_street": "2 rue Bureau", "work_address_city": "Montréal",
        "work_address_province": "Québec", "work_address_postal_code": "H2X 2X2",
        "work_address_country": "Canada",
    }


@pytest.mark.parametrize("role, work", [
    ("fournisseur", True), ("sténographe", True), ("huissier", True),
    ("client", False), ("autre", False),
])
def test_the_new_roles_are_written_to_at_their_work_address(role, work):
    assert template_fields.uses_work_block(_with_both_addresses(role)) is work


# ── The connector ──────────────────────────────────────────────────────────


def _conforms(tool: str, payload) -> None:
    errors = tools.validate_args(OUTPUT_SCHEMAS[tool], tools._jsonable(payload))
    assert errors == [], f"{tool}: {errors}"


def test_the_connector_creates_and_lists_a_supplier_within_its_contract(db):
    created = handlers.create_partie({
        "type": "organization", "contact_role": "fournisseur",
        "organization_name": "Fournitures Exemple inc.",
    })
    _conforms("create_partie", created)
    # The bump fired, but a supplier never reaches the phone.
    assert created["ctag_bumped"] is True and created["dav_synced"] is False
    client = handlers.create_partie({
        "type": "organization", "contact_role": "client",
        "organization_name": "Client Exemple inc.",
    })
    assert client["dav_synced"] is True
    pid = created["entity"]["id"]
    assert db.peek(f"parties/{pid}")["contact_role"] == "fournisseur"
    listed = handlers.list_parties({"contact_role": "fournisseur"})
    _conforms("list_parties", listed)
    assert [row["id"] for row in listed["items"]] == [pid]
    assert tools.validate_args(
        tools.TOOLS["create_partie"]["input_schema"],
        {"type": "organization", "contact_role": "vendeur",
         "organization_name": "X"}) != []
