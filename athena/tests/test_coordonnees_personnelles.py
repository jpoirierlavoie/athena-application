"""Coordonnées personnelles d'abord pour une personne physique (2026-10-30).

Décision de l'avocat : le bloc professionnel d'un client porte désormais les
coordonnées de son EMPLOYEUR (« Occupation »). La facture, le téléphone des
lettres, les boutons de la fiche, la ligne de la liste et l'accusé du portail
allaient au travail en premier ; ils suivent maintenant le bloc que
``utils/template_fields`` retient — personnel d'abord, sauf un rôle
professionnel (avocat, expert, huissier, notaire), dont le cabinet est
l'adresse de correspondance. Une personne morale ne change pas.
"""

import os
import pathlib
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
    import dav.sync as dav_sync  # its db is patched below
    from models import dossier as dossier_model
    from models import partie as pm
    import routes.doc_templates as doc_templates_routes
    import routes.dossiers as dossiers_routes
    import routes.parties as parties_routes
    import routes.reception as reception_routes
    import routes.taches_portail as taches_portail

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils import template_fields  # noqa: E402
from utils.html_attr import jsattr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

_ = (dav_sync, dossier_model)

UTC = timezone.utc
AVANT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

# Un client salarié : son domicile, et le bloc professionnel de l'employeur.
SALARIE = {
    "type": "individual", "contact_role": "client", "prefix": "M.",
    "first_name": "Jean", "last_name": "Tremblay", "occupation": "salarie",
    "organization": "Hydro-Exemple", "job_title": "Technicien",
    "email": "jean@maison.ca", "email_work": "jtremblay@hydro-exemple.ca",
    "phone_cell": "+15145550002", "phone_work": "+15145550001",
    "address_street": "10 rue Principale", "address_city": "Laval",
    "address_province": "Québec", "address_postal_code": "H7A 1A1",
    "address_country": "Canada",
    "work_address_street": "75 boulevard René-Lévesque",
    "work_address_city": "Montréal", "work_address_province": "Québec",
    "work_address_postal_code": "H2Z 1A4", "work_address_country": "Canada",
}


def test_le_telephone_retenu_suit_le_bloc():
    assert template_fields.selected_phone(SALARIE) == "+15145550002"
    assert not template_fields.uses_work_block(SALARIE)
    avocat = {**SALARIE, "contact_role": "avocat_adverse"}
    assert template_fields.selected_phone(avocat) == "+15145550001"
    assert template_fields.uses_work_block(avocat)
    morale = {**SALARIE, "type": "organization", "organization_name": "Béton Nord inc."}
    assert template_fields.selected_phone(morale) == "+15145550001"
    assert template_fields.selected_phone({**SALARIE, "phone_cell": ""}) == "+15145550001"
    assert template_fields.selected_phone({"type": "individual"}) == ""


def test_l_accuse_du_portail_ne_nomme_plus_l_employeur_sous_le_client():
    bloc = taches_portail._client_expediteur({"email": "jean@maison.ca"}, dict(SALARIE))
    assert bloc["organisation"] == ""
    assert "514" in bloc["telephone"] and "555-0002" in bloc["telephone"]
    assert bloc["adresse_lignes"][0] == "10 rue Principale"
    avocat = {**SALARIE, "contact_role": "avocat_adverse"}
    bloc = taches_portail._client_expediteur({"email": "x@y.ca"}, avocat)
    assert bloc["organisation"] == "Hydro-Exemple"


# ── La fiche et la liste, par les vraies routes ───────────────────────────


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(parties_routes, "_compliance_signer", lambda: "Me Test")
    return install(monkeypatch, *_fake_modules())


@pytest.fixture
def client(db):
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    app.jinja_env.filters.update(to_mtl=to_mtl, phone=format_phone_display,
                                 jsattr=jsattr)
    for bp in (parties_routes.parties_bp, dossiers_routes.dossiers_bp,
               doc_templates_routes.doc_templates_bp,
               reception_routes.reception_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _seed(db, pid="p1", **over) -> str:
    doc = {**pm._default_doc(), **SALARIE, "id": pid, "vcard_uid": f"u-{pid}",
           "etag": f"e-{pid}", "created_at": AVANT, "updated_at": AVANT}
    doc.update(over)
    db.seed(f"parties/{pid}", doc)
    return pid


def test_appeler_et_courriel_joignent_la_personne_chez_elle(client, db):
    pid = _seed(db)
    html = client.get(f"/parties/{pid}").get_data(as_text=True)
    assert 'href="tel:+15145550002"' in html
    assert 'href="mailto:jean@maison.ca"' in html
    # Le bloc professionnel reste lisible sur la fiche.
    assert "jtremblay@hydro-exemple.ca" in html


def test_la_liste_montre_les_coordonnees_retenues(client, db):
    _seed(db)
    html = client.get("/parties/").get_data(as_text=True)
    assert "jean@maison.ca" in html
    assert "jtremblay@hydro-exemple.ca" not in html
