"""Le formulaire et la fiche d'un contact — titres, occupation (2026-10-30).

Par les vraies routes, les vrais gabarits et le VRAI filtre ``jsattr``
(``utils/html_attr``) : c'est lui qui rend l'apostrophe de « L'hon. »
inoffensive dans ``x-data``. Le défaut d'origine : la valeur, citée à la main
entre apostrophes, fermait la chaîne JavaScript et brisait tout le composant
Alpine — dont le champ caché des mandataires.
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

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.html_attr import jsattr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

_ = (dav_sync, dossier_model)

UTC = timezone.utc
AVANT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


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
    # The fiche links to the dossier, gabarit and Réception pages.
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
    doc = {**pm._default_doc(), "id": pid, "type": "individual",
           "contact_role": "client", "first_name": "Marie",
           "last_name": "Côté", "vcard_uid": f"u-{pid}", "etag": f"e-{pid}",
           "created_at": AVANT, "updated_at": AVANT}
    doc.update(over)
    db.seed(f"parties/{pid}", doc)
    return pid


def _post(db, pid, **over) -> dict:
    stored = db.peek(f"parties/{pid}")
    data = {"type": "individual", "contact_role": "client",
            "first_name": "Marie", "last_name": "Côté",
            "prefix": stored.get("prefix", ""),
            "identity_verified": stored["identity_verified"],
            "conflict_check": stored["conflict_check"],
            "expected_etag": stored["etag"]}
    data.update(over)
    return data


def test_x_data_echappe_le_titre(client, db):
    pid = _seed(db, prefix="L'hon.")
    html = client.get(f"/parties/{pid}/edit").get_data(as_text=True)
    assert "prefix: &#34;L&#39;hon.&#34;" in html
    assert "prefix: 'L'hon.'" not in html


def test_le_titre_offre_la_liste_et_garde_un_titre_inconnu(client, db):
    pid = _seed(db, prefix="Capitaine")
    html = client.get(f"/parties/{pid}/edit").get_data(as_text=True)
    assert '<option value="Dre" >Dre — Docteure</option>' in html
    assert '<option value="Capitaine" selected>Capitaine (non reconnu)</option>' in html


def test_un_titre_inconnu_se_reenregistre_mais_ne_s_invente_pas(client, db):
    pid = _seed(db, prefix="Capitaine")
    resp = client.post(f"/parties/{pid}", data=_post(db, pid, first_name="Mariette"))
    assert resp.status_code == 302
    assert db.peek(f"parties/{pid}")["prefix"] == "Capitaine"
    resp = client.post(f"/parties/{pid}", data=_post(db, pid, prefix="Colonel"))
    assert resp.status_code == 200
    assert "Titre invalide" in resp.get_data(as_text=True)
    assert db.peek(f"parties/{pid}")["prefix"] == "Capitaine"


def test_le_bloc_professionnel_suit_l_occupation(client, db):
    pid = _seed(db, job_title="Avocate", job_role="Associée")
    html = client.get(f"/parties/{pid}/edit").get_data(as_text=True)
    assert ">Fonction<" not in html
    assert "Poste ou profession" in html
    # La « Fonction » non migrée paraît déjà repliée dans le poste.
    assert 'value="Avocate — Associée"' in html
    assert '<span x-show="occupation === &#34;salarie&#34;" x-cloak>Employeur</span>' in html
    assert "Établissement d&#39;enseignement" in html
    resp = client.post(f"/parties/{pid}", data=_post(
        db, pid, occupation="salarie", job_title="Avocate — Associée"))
    assert resp.status_code == 302
    stored = db.peek(f"parties/{pid}")
    assert stored["occupation"] == "salarie"
    assert stored["job_title"] == "Avocate — Associée"
    assert "job_role" not in stored


def test_une_occupation_hors_liste_revient_au_formulaire(client, db):
    pid = _seed(db)
    resp = client.post(f"/parties/{pid}", data=_post(db, pid, occupation="astronaute"))
    assert resp.status_code == 200
    assert "Occupation invalide" in resp.get_data(as_text=True)


def test_la_fiche_porte_le_suffixe_les_autres_prenoms_et_l_occupation(client, db):
    pid = _seed(db, prefix="L'hon.", suffix="j.c.s.", middle_name="Anne",
                occupation="etudiant", organization="Université Laval",
                job_title="Doctorante")
    html = client.get(f"/parties/{pid}").get_data(as_text=True)
    assert "L&#39;hon. Marie Côté, j.c.s.</h1>" in html
    assert "Autres prénoms : Anne" in html
    assert "Étudiant(e)" in html
    assert "Établissement d&#39;enseignement" in html
    assert ">Fonction<" not in html
