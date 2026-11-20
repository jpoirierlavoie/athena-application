"""Routes « Paramètres → Rendez-vous » — auth, CSRF, POST-only, DB & Graph."""

import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ATHENA)

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from flask import Flask  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import models.rendez_vous_reglages as m_rv
    import routes.auth_routes as ra
    import routes.settings as rs

from config import Config  # noqa: E402
from routes._helpers import theme_context  # noqa: E402
from security import init_security  # noqa: E402
from tests._fake_firestore import install  # noqa: E402
from utils import graph_teams  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.rendez_vous_defaults import WEEKDAYS, default_reglages  # noqa: E402


def _app(*, csrf_on: bool = False) -> Flask:
    app = Flask(__name__, template_folder=os.path.join(_ATHENA, "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["REQUIRE_MFA"] = True
    app.config["AUTHORIZED_USER_EMAIL"] = "test@example.com"
    app.config["RATELIMIT_ENABLED"] = False
    app.config["WTF_CSRF_ENABLED"] = csrf_on
    app.config["RECAPTCHA_ENTERPRISE_SITE_KEY"] = ""
    init_security(app)
    app.jinja_env.globals["ms"] = ms
    app.jinja_env.globals["firebase_api_key"] = "clef-test"
    app.jinja_env.globals["firebase_project_id"] = "test-project"
    app.jinja_env.globals["firebase_app_id"] = "1:2:web:3"
    app.register_blueprint(rs.settings_bp)
    app.register_blueprint(ra.auth_bp)
    app.context_processor(theme_context)
    return app


def _login(client):
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


@pytest.fixture()
def store(monkeypatch):
    return install(monkeypatch, m_rv)


@pytest.fixture()
def web(store):
    return _login(_app().test_client())


@pytest.fixture()
def anon(store):
    return _app().test_client()


def _valid_form(**over) -> dict[str, str]:
    form: dict[str, str] = {
        "battement_minutes": "15",
        "delai_minimum_heures": "24",
        "horizon_jours": "60",
        "preavis_modification_heures": "24",
    }
    for day in WEEKDAYS:
        form[f"plage_{day}_actif"] = "on"
        form[f"plage_{day}_matin_debut"] = "09:30"
        form[f"plage_{day}_matin_fin"] = "12:00"
        form[f"plage_{day}_apres_midi_debut"] = "13:30"
        form[f"plage_{day}_apres_midi_fin"] = "16:30"
    form.update({
        "type_0_cle": "consultation",
        "type_0_libelle": "Consultation initiale",
        "type_0_hearing_type": "consultation",
        "type_0_duree_minutes": "30",
        "type_0_public": "on",
        "type_0_ordre_du_jour": "1. Accueil (5 min)\n2. Situation (25 min)",
    })
    for idx in range(1, 5):
        form[f"type_{idx}_cle"] = ""
        form[f"type_{idx}_libelle"] = ""
        form[f"type_{idx}_hearing_type"] = "rencontre"
        form[f"type_{idx}_duree_minutes"] = ""
        form[f"type_{idx}_ordre_du_jour"] = ""
    form.update(over)
    return form


# ── Auth & CSRF ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/parametres/rendez-vous"),
        ("POST", "/parametres/rendez-vous/enregistrer"),
        ("POST", "/parametres/rendez-vous/tester"),
    ],
)
def test_rendez_vous_routes_require_authenticated_session(anon, method, path):
    r = anon.open(path, method=method)
    assert r.status_code == 302
    assert "/auth/login" in r.headers["Location"]


@pytest.mark.parametrize(
    "path",
    [
        "/parametres/rendez-vous/enregistrer",
        "/parametres/rendez-vous/tester",
    ],
)
def test_rendez_vous_posts_require_csrf(store, path):
    c = _login(_app(csrf_on=True).test_client())
    r = c.post(path, data=_valid_form())
    assert r.status_code == 400


# ── GET rendering, no Graph, no htmx/inline scripts, fail-closed UI ───────


def test_get_never_calls_graph_and_has_no_htmx_or_template_scripts(web, monkeypatch):
    def _forbid(*a, **k):
        raise AssertionError("GET /parametres/rendez-vous ne doit jamais appeler Graph")

    monkeypatch.setattr(graph_teams, "verifier_connexion_teams", _forbid)

    r = web.get("/parametres/rendez-vous")
    assert r.status_code == 200
    body = r.data.decode("utf-8")

    assert "Rendez-vous" in body
    assert "Connect-MicrosoftTeams" in body
    for verb in (
        "hx-post", "hx-get", "hx-put", "hx-delete", "hx-patch",
        "hx-trigger", "hx-target", "hx-include", "hx-swap",
        "hx-vals", "hx-boost",
    ):
        assert verb not in body, verb

    tpl_path = os.path.join(_ATHENA, "templates", "settings", "rendez_vous.html")
    with open(tpl_path, encoding="utf-8") as fh:
        tpl_src = fh.read()
    tpl_code = re.sub(r"\{#.*?#\}", "", tpl_src, flags=re.DOTALL)
    assert "<script" not in tpl_code.lower()
    assert "|safe" not in tpl_code


def test_get_unreadable_store_shows_banner_and_disables_save_button(web, monkeypatch):
    monkeypatch.setattr(
        rs,
        "get_rendez_vous_reglages_state",
        lambda: (default_reglages(), "unreadable"),
    )
    r = web.get("/parametres/rendez-vous")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "magasin de réglages est illisible" in body
    assert re.search(r'<button\s+type="submit"\s+disabled', body)


# ── POST save & refusals ──────────────────────────────────────────────────


def test_post_enregistrer_saves_skips_blank_slots_and_logs_without_values(
    web, store, caplog
):
    form = _valid_form()
    form.pop("plage_vendredi_actif")  # unchecked checkbox

    with caplog.at_level(logging.INFO, logger="pallas.settings"):
        r = web.post("/parametres/rendez-vous/enregistrer", data=form)

    assert r.status_code == 302
    assert "/parametres/rendez-vous" in r.headers["Location"]
    assert "ok=1" in r.headers["Location"]

    stored = store.peek(f"{m_rv.COLLECTION}/{m_rv.DOC_ID}")
    assert stored is not None
    assert len(stored["types_rdv"]) == 1
    assert stored["types_rdv"][0]["cle"] == "consultation"
    assert stored["plages"]["vendredi"]["actif"] is False

    follow = web.get(r.headers["Location"])
    assert follow.status_code == 200
    assert "Réglages de rendez-vous et Teams enregistrés." in follow.data.decode("utf-8")

    records = [rec for rec in caplog.records if rec.name == "pallas.settings"]
    assert any(
        rec.getMessage() == "rendez_vous_reglages_updated"
        for rec in records
    )
    joined_logs = " ".join(rec.getMessage() for rec in records)
    assert "Consultation initiale" not in joined_logs
    assert "Accueil" not in joined_logs


def test_post_partial_slot_refuses_200_and_preserves_submitted_values(
    web, store, caplog
):
    form = _valid_form(
        battement_minutes="42",
        type_1_cle="partiel",
        type_1_libelle="",
        type_1_duree_minutes="45",
        type_1_ordre_du_jour="1. Point",
    )
    form.pop("plage_vendredi_actif")  # unchecked Friday must stay unchecked

    with caplog.at_level(logging.WARNING, logger="pallas.settings"):
        r = web.post("/parametres/rendez-vous/enregistrer", data=form)

    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "le libellé est requis" in body
    assert 'name="battement_minutes"' in body
    assert 'value="42"' in body
    assert 'name="type_1_cle"' in body
    assert 'value="partiel"' in body

    friday_tag = re.search(
        r'<input\s+type="checkbox"\s+name="plage_vendredi_actif"[^>]*>', body
    )
    assert friday_tag is not None
    assert "checked" not in friday_tag.group(0)
    assert store.peek(f"{m_rv.COLLECTION}/{m_rv.DOC_ID}") is None

    assert any(
        rec.getMessage() == "rendez_vous_reglages_refused"
        and rec.levelno == logging.WARNING
        for rec in caplog.records
    )


def test_post_tester_renders_diagnostic_and_logs_bookings_event(
    web, monkeypatch, caplog
):
    monkeypatch.setattr(Config, "TEAMS_REUNIONS_ACTIF", True)
    monkeypatch.setattr(
        graph_teams,
        "verifier_connexion_teams",
        lambda: {
            "interrupteur_actif": True,
            "configure": True,
            "jeton_valide": True,
            "roles_presents": ["Calendars.ReadWrite"],
            "roles_manquants": [],
            "utilisateur_accessible": True,
            "online_meetings_accessible": True,
            "politique_teams_requise": False,
            "transcriptions_accessibles": True,
            "onedrive_accessible": True,
            "erreurs": [],
        },
    )

    with caplog.at_level(logging.INFO, logger="pallas.bookings"):
        r = web.post("/parametres/rendez-vous/tester")

    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "TEAMS_REUNIONS_ACTIF — actif" in body
    assert "Jeton Entra : valide" in body
    assert "API onlineMeetings (Teams) : accessible" in body
    assert "API transcriptions Graph : accessible" in body
    assert "OneDrive (module Courrier) : accessible" in body
    assert any(
        rec.getMessage() == "teams_connexion_verifiee"
        for rec in caplog.records
    )
