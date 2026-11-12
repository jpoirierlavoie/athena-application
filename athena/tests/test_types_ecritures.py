"""La page « Types d'écritures » (lot 7 du 2026-11-11).

Ce que fait chaque type des deux registres, TIRÉ DU MODÈLE
(``services/types_ecritures``) : ce qu'on épingle ici —

- chaque type de chaque registre a sa carte, une seule fois, et chaque valeur
  des formulaires (« Virement interne » compris) a son ancre : le lien « Que
  fait chaque type ? » des formulaires y mène ;
- chaque ligne d'une carte se lit dans les règles du modèle (dossier,
  catégorie, flux de trésorerie, contrepartie, phrase des états) — jamais une
  copie écrite à la main ;
- une phrase « Aux états financiers » par type, ni plus ni moins ;
- la page ne lit RIEN dans Firestore : elle répond quel que soit l'état du
  magasin ;
- le gabarit n'écrit aucun code de type.
"""

import os
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone
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
    from models import financial_statements as fs
    from models import trust
    import routes.admin_ledger as ra
    import routes.comptabilite as rc
    import routes.journal_radiations as rj
    import routes.trust as rt
    from services import types_ecritures as te

from flask import Flask  # noqa: E402
from markupsafe import escape  # noqa: E402

TEMPLATE = _ATHENA / "templates" / "comptabilite" / "types_ecritures.html"
URL = "/comptabilite/types-ecritures"


def _app() -> Flask:
    from utils.format_fr import format_cents_fr
    from utils.icons import ms

    app = Flask(__name__, template_folder=str(_ATHENA / "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.jinja_env.globals["ms"] = ms
    app.jinja_env.globals["csrf_token"] = lambda: "jeton-test"
    app.jinja_env.filters["cents_fr"] = format_cents_fr
    # The hub's header names the write-off journal too (lot 3c).
    for bp in (rc.comptabilite_bp, rj.journal_radiations_bp, rt.trust_bp, ra.admin_bp):
        app.register_blueprint(bp)
    return app


@pytest.fixture()
def web():
    client = _app().test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


class _NoFirestore:
    """Every attribute read records itself and raises: a page that reads
    nothing never touches it."""

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name):
        self.touched.append(name)
        raise RuntimeError("firestore indisponible")


@pytest.fixture()
def sans_firestore(monkeypatch):
    boom = _NoFirestore()
    for name, module in list(sys.modules.items()):
        if (name.split(".")[0] in {"models", "services", "routes", "utils", "auth"}
                and module is not None and "db" in vars(module)):
            monkeypatch.setattr(module, "db", boom)
    return boom


def _page(web) -> str:
    resp = web.get(URL)
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def _cards(groups) -> list[dict]:
    return [card for _label, cards in groups for card in cards]


# ── La page ────────────────────────────────────────────────────────────────


def test_chaque_type_a_sa_carte_une_fois(web):
    html = _page(web)
    for kind in al.VALID_KINDS:
        assert html.count(f'id="type-{kind}"') == 1, kind
    for purpose in trust.VALID_PURPOSES:
        assert html.count(f'id="type-fideicommis-{purpose}"') == 1, purpose
    # Every value the forms post has its anchor — « Virement interne »
    # included, which no stored kind carries.
    for value in al.FORM_KINDS:
        assert html.count(f'id="type-{value}"') == 1, value
    for purpose in rt.ENTRY_FORM_PURPOSES:
        assert f'id="type-fideicommis-{purpose}"' in html, purpose
    for kind in al.VALID_KINDS:
        assert str(escape(al.ENTRY_TYPES[kind].label)) in html, kind
    for purpose in trust.VALID_PURPOSES:
        assert str(escape(trust.PURPOSE_TYPES[purpose].label)) in html, purpose
    assert str(escape(fs.TRUST_STATEMENT_SENTENCE)) in html


def test_la_page_ne_lit_rien(web, sans_firestore):
    """Régression du principe : la page est tirée du modèle, sans lecture —
    elle répond même quand Firestore est en panne."""
    html = _page(web)
    assert sans_firestore.touched == []
    assert 'id="type-dépense"' in html


def test_connexion_requise():
    from flask import Blueprint

    app = _app()
    stub = Blueprint("auth", __name__)
    stub.add_url_rule("/connexion", "login", lambda: "connexion")
    app.register_blueprint(stub)
    resp = app.test_client().get(URL)
    assert resp.status_code == 302 and "/connexion" in resp.headers["Location"]


def test_le_hub_mene_a_la_page(web, monkeypatch):
    monkeypatch.setattr(rc.trust, "get_firm_trust_snapshot",
                        lambda: {"accounts": [], "total_held_cents": 0})
    monkeypatch.setattr(rc.al, "get_firm_admin_snapshot", lambda: {"accounts": []})
    assert f'href="{URL}"' in web.get("/comptabilite/").get_data(as_text=True)


def test_le_gabarit_n_ecrit_aucun_code():
    text = TEMPLATE.read_text(encoding="utf-8")
    codes = set(al.VALID_KINDS) | set(al.FORM_KINDS) | set(trust.VALID_PURPOSES)
    for code in codes:
        assert not re.search(rf"(?<![\w-]){re.escape(code)}(?![\w-])", text), code


# ── Les cartes, lues dans les règles du modèle ─────────────────────────────


def test_une_phrase_des_etats_par_type():
    assert set(fs.KIND_STATEMENT_SENTENCES) == set(al.VALID_KINDS)
    for kind, sentence in fs.KIND_STATEMENT_SENTENCES.items():
        assert sentence.strip() and sentence.endswith("."), kind


def test_les_cartes_d_administration_suivent_les_regles():
    cards = _cards(te.administration())
    assert [c["code"] for c in cards] == [
        k for _l, kinds in al.kinds_by_family(al.VALID_KINDS) for k in kinds]
    assert sorted(c["code"] for c in cards) == sorted(al.VALID_KINDS)
    for c in cards:
        kind = c["code"]
        entry_type = al.ENTRY_TYPES[kind]
        assert c["label"] == entry_type.label
        assert c["statement"] == fs.KIND_STATEMENT_SENTENCES[kind]
        if kind == al.REVERSAL_KIND:
            continue
        assert c["payee_label"] == entry_type.payee_label
        assert (c["dossier"] == "Jamais") == (
            kind in al.NO_DOSSIER_KINDS or kind == "paiement_carte"), kind
        assert (c["dossier"] == "Celui de la facture") == (
            kind == "encaissement_facture"), kind
        assert c["split"].startswith("Catégorie") == (kind in al.CATEGORY_KINDS), kind
        assert c["cash_flow"][0] == fs.CASH_FLOW_LINE_LABELS[al.CASH_FLOW_LINES[kind]]
        assert (len(c["cash_flow"]) == 2) == (kind == al.TRANSFER_PURPOSE_KIND), kind
        if kind != "paiement_carte":
            assert c["sens"] == {"recette": "Recette", "déboursé": "Déboursé"}[
                al.implied_direction(kind)], kind
    # The form's « Virement interne » opens the first virement card only.
    first_virement = next(c["code"] for c in cards
                          if c["code"] in al.INTERNAL_TRANSFER_KINDS)
    assert [(c["code"], a) for c in cards for a in c["aliases"]] == [
        (first_virement, al.INTERNAL_TRANSFER_FORM_KIND)]


def test_les_cartes_du_fideicommis_suivent_les_regles():
    cards = _cards(te.fideicommis())
    assert sorted(c["code"] for c in cards) == sorted(trust.VALID_PURPOSES)
    optional = set(trust.NO_DOSSIER_PURPOSES) - {trust.REVERSAL_PURPOSE}
    for c in cards:
        purpose = c["code"]
        assert c["label"] == trust.PURPOSE_TYPES[purpose].label
        if purpose in (trust.REVERSAL_PURPOSE, trust.TRANSFER_PURPOSE):
            continue
        assert (c["dossier"] == "Facultatif") == (purpose in optional), purpose
        direction = ("déboursé" if purpose == trust.FEE_PAYMENT_PURPOSE
                     else trust.PURPOSE_DIRECTIONS.get(purpose))
        if direction:
            assert c["sens"] == {"recette": "Recette", "déboursé": "Déboursé"}[direction]
        else:
            assert c["sens"] == "Recette ou déboursé"
