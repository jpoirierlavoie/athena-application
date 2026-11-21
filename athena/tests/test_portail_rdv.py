"""Tests Lot 2 end-to-end pour le module RDV portail, tâches et Réception."""

import ast
import importlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ATHENA)

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")
os.environ.setdefault("PORTAIL_SECRET_KEY", "test-portail-secret")

from flask import Flask  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import routes.taches_portail as tp
    from models import portail_disponibilites, portail_invitation
    from routes._helpers import theme_context
    from routes.admin_ledger import admin_bp
    from routes.auth_routes import auth_bp
    from routes.comptabilite import comptabilite_bp
    from routes.dashboard import dashboard_bp
    from routes.doc_templates import doc_templates_bp, modeles_bp
    from routes.documents import documents_bp
    from routes.dossiers import dossiers_bp
    from routes.financial_statements import etats_bp
    from routes.hearings import hearings_bp
    from routes.invoices import invoices_bp
    from routes.notes import notes_bp
    from routes.parties import parties_bp
    from routes.reception import reception_bp
    from routes.settings import settings_bp
    from routes.tasks import tasks_bp
    from routes.time_expenses import time_expenses_bp
    from routes.trust import trust_bp
    from services import portail_emission

import client.app as client_app  # noqa: E402
from client import limiter  # noqa: E402
from client.config import PORTAIL_QUEUE  # noqa: E402
from client.services import invitations as client_invitations  # noqa: E402
from client.services import stockage, taches  # noqa: E402
from config import Config  # noqa: E402
from security import init_security  # noqa: E402
from tests._fake_firestore import FakeFirestore, install  # noqa: E402
from tz import mtl_to_utc, to_mtl  # noqa: E402
from utils import courriel, graph_teams  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.html_attr import jsattr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.markdown_docx import markdown_to_safe_html  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

UTC = timezone.utc


class _FakeBlob:
    def __init__(self, name: str, data: bytes | None = None):
        self.name = name
        self._data = data
        self.size = len(data) if data is not None else 0

    def exists(self) -> bool:
        return self._data is not None

    def download_as_bytes(self) -> bytes:
        assert self._data is not None
        return self._data

    def upload_from_string(self, data, content_type: str = "", if_generation_match=None) -> None:
        self._data = data.encode("utf-8") if isinstance(data, str) else data
        self.size = len(self._data)


class _FakeBucket:
    def __init__(self):
        self._blobs: dict[str, _FakeBlob] = {}

    def blob(self, name: str) -> _FakeBlob:
        if name not in self._blobs:
            self._blobs[name] = _FakeBlob(name, None)
        return self._blobs[name]

    def put_json(self, name: str, payload: dict) -> None:
        self.blob(name).upload_from_string(json.dumps(payload, ensure_ascii=False, default=str))

    def get_json(self, name: str) -> dict | None:
        blob = self._blobs.get(name)
        if not blob or not blob.exists():
            return None
        return json.loads(blob.download_as_bytes())


@pytest.fixture(scope="module")
def portal_app():
    with mock.patch("utils.tracing_setup.init_app"):
        with mock.patch.object(client_app, "_init_firebase"):
            app = client_app.create_portail_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    limiter.enabled = False
    yield app
    limiter.enabled = True


@pytest.fixture(scope="module")
def main_app():
    app = Flask(__name__, template_folder=os.path.join(_ATHENA, "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["REQUIRE_MFA"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["RATELIMIT_ENABLED"] = False
    app.config["AUTHORIZED_USER_EMAIL"] = "test@example.com"
    app.config["RECAPTCHA_ENTERPRISE_SITE_KEY"] = ""
    init_security(app)
    app.jinja_env.filters["to_mtl"] = to_mtl
    app.jinja_env.filters["phone"] = format_phone_display
    app.jinja_env.filters["cents_fr"] = lambda c: format_cents_fr(c) if c is not None else ""
    app.jinja_env.filters["jsattr"] = jsattr
    app.jinja_env.filters["markdown"] = markdown_to_safe_html
    app.jinja_env.globals["ms"] = ms
    app.jinja_env.globals["firebase_api_key"] = "clef-test"
    app.jinja_env.globals["firebase_project_id"] = "test-project"
    app.jinja_env.globals["firebase_app_id"] = "1:2:web:3"
    app.context_processor(theme_context)
    app.context_processor(lambda: {"reception_badge": 0})
    for bp in (
        auth_bp, dashboard_bp, parties_bp, dossiers_bp, time_expenses_bp,
        invoices_bp, hearings_bp, tasks_bp, documents_bp, notes_bp,
        doc_templates_bp, modeles_bp, trust_bp, admin_bp, comptabilite_bp,
        etats_bp, settings_bp, reception_bp, tp.taches_portail_bp,
    ):
        app.register_blueprint(bp)
    return app


@pytest.fixture()
def env(monkeypatch):
    for modname in (
        "dav.sync",
        "models.dossier",
        "models.hearing",
        "models.partie",
        "models.rendez_vous_reglages",
        "models.portail_invitation",
        "models.portail_disponibilites",
    ):
        importlib.import_module(modname)
    modules = [
        m
        for name, m in sorted(sys.modules.items())
        if (name.startswith("models.") or name == "dav.sync")
        and getattr(m, "db", None) is not None
    ]
    main_db = install(monkeypatch, *modules)
    portail_db = FakeFirestore()
    monkeypatch.setattr(portail_invitation, "_pdb", lambda: portail_db)
    monkeypatch.setattr(client_invitations, "_pdb", lambda: portail_db)

    bucket = _FakeBucket()
    monkeypatch.setattr(tp, "_bucket", lambda: bucket)
    monkeypatch.setattr(stockage, "_bucket", lambda: bucket)
    monkeypatch.setattr(
        portail_emission.storage,
        "bucket",
        lambda *_args, **_kwargs: bucket,
    )

    monkeypatch.setattr(Config, "PORTAIL_RDV_ACTIF", True)
    monkeypatch.setattr(Config, "TEAMS_REUNIONS_ACTIF", True)
    monkeypatch.setattr(Config, "teams_configured", lambda: True)
    monkeypatch.setattr(Config, "graph_configured", lambda: True)
    monkeypatch.setattr(
        graph_teams,
        "lire_plages_occupees",
        lambda _s, _e: [],
    )

    emails: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        courriel,
        "envoyer",
        lambda to, subject, body, **_kw: emails.append((to, subject, body)),
    )
    monkeypatch.setattr(portail_emission, "preparer_utilisateur_portail", lambda _email: [])
    monkeypatch.setattr(
        portail_emission,
        "_generer_lien",
        lambda _email, inv_id: f"https://portail.example/entree?i={inv_id}&oob=fake",
    )

    return main_db, portail_db, bucket, emails


def _future_weekday_slot(days_ahead: int = 5, hour: int = 10, minute: int = 0) -> datetime:
    d = (datetime.now(UTC) + timedelta(days=days_ahead)).date()
    while d.weekday() >= 5 or (d.month, d.day) in ((12, 25), (12, 26), (1, 1), (1, 2)):
        d += timedelta(days=1)
    return mtl_to_utc(datetime(d.year, d.month, d.day, hour, minute))


def _seed_dispo(portail_db: FakeFirestore, *slots_utc: datetime) -> None:
    isos = [s.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") for s in slots_utc]
    portail_db.seed(
        f"{portail_disponibilites.COLLECTION}/{portail_disponibilites.DOC_ID}",
        {
            "updated_at": datetime.now(UTC),
            "horizon_jours": 60,
            "delai_minimum_heures": 24,
            "preavis_modification_heures": 24,
            "types_rdv": [
                {
                    "cle": "consultation",
                    "libelle": "Consultation initiale",
                    "duree_minutes": 30,
                    "public": True,
                },
                {
                    "cle": "rencontre",
                    "libelle": "Rencontre de suivi",
                    "duree_minutes": 45,
                    "public": False,
                },
            ],
            "creneaux_par_duree": {"30": isos, "45": isos, "60": isos},
        },
    )


def _portal_session(web, inv_id: str, email: str = "client@exemple.com"):
    with web.session_transaction() as s:
        s["inv_id"] = inv_id
        s["uid"] = "fb-u1"
        s["email"] = email


def _login_main(client):
    with client.session_transaction() as s:
        s["user_id"] = "avocat-1"
        s["expires_at"] = datetime.now(UTC) + timedelta(hours=1)
    return client


def _post_task(main_client, payload: dict):
    return main_client.post(
        "/taches/portail/evenement",
        json=payload,
        headers={"X-AppEngine-QueueName": PORTAIL_QUEUE},
    )


# ── Import isolation & quarantine paths ───────────────────────────────────


def test_portail_rdv_modules_import_zero_models_config_or_security():
    root = Path(_ATHENA)
    forbidden = ("models", "config", "security")
    for rel in (
        "client/services/disponibilites.py",
        "client/services/rdv_validation.py",
    ):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden, (rel, alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden, (rel, node.module)


def test_ecrire_objet_json_enforces_quarantine_depth_and_no_envelope(env):
    _main_db, _portail_db, bucket, _emails = env
    stockage.ecrire_objet_json("submissions/_rdv_demande_r1.json", {"ok": 1})
    stockage.ecrire_objet_json("submissions/inv1/rdv_b1.json", {"ok": 2})
    assert bucket.get_json("submissions/_rdv_demande_r1.json") == {"ok": 1}

    for bad in (
        "other/r1.json",
        "submissions/../secret.json",
        "submissions/inv1/batch/envelope.json",
        "submissions/inv1/batch/deep/file.json",
    ):
        with pytest.raises(ValueError):
            stockage.ecrire_objet_json(bad, {"x": 1})


# ── Public /rdv & /api/rdv/demander ───────────────────────────────────────


def test_public_rdv_page_and_demander_validation(portal_app, env, monkeypatch):
    _main_db, portail_db, bucket, _emails = env
    slot = _future_weekday_slot(5, 10, 0)
    web = portal_app.test_client()

    portal_app.config["PORTAIL_RDV_ACTIF"] = False
    assert "momentanément désactivée" in web.get("/rdv").get_data(as_text=True)
    assert web.post("/api/rdv/demander", json={}).status_code == 503

    portal_app.config["PORTAIL_RDV_ACTIF"] = True
    assert web.post("/api/rdv/demander", json={}).status_code == 503

    _seed_dispo(portail_db, slot)
    html = web.get("/rdv").get_data(as_text=True)
    assert "Consultation initiale" in html
    assert "Rencontre de suivi" not in html  # type invite-seulement exclu

    base_payload = {
        "type_rdv_cle": "consultation",
        "slot_debut_utc": slot.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nom_complet": "Marie Tremblay",
        "courriel": "marie@exemple.ca",
        "telephone": "5145551234",
        "aucune_partie_adverse": False,
        "parties_adverses": "Gestion ABC inc.",
        "sujet": "Vice caché résidentiel",
    }

    bad_chevrons = {**base_payload, "sujet": "Litige <script>alert(1)</script>"}
    assert web.post("/api/rdv/demander", json=bad_chevrons).status_code == 422

    bad_no_adverse = {**base_payload, "parties_adverses": ""}
    assert web.post("/api/rdv/demander", json=bad_no_adverse).status_code == 422

    bad_slot = {**base_payload, "slot_debut_utc": "2026-12-01T15:00:00Z"}
    assert web.post("/api/rdv/demander", json=bad_slot).status_code == 422

    signaux = []
    monkeypatch.setattr(
        taches,
        "signaler",
        lambda event, inv_id, batch=None: signaux.append((event, inv_id, batch)),
    )
    resp = web.post("/api/rdv/demander", json=base_payload)
    assert resp.status_code == 202
    assert len(signaux) == 1
    ev, req_id, batch = signaux[0]
    assert ev == "rdv_demande" and req_id == batch
    assert bucket.get_json(f"submissions/_rdv_demande_{req_id}.json")["sujet"] == "Vice caché résidentiel"


# ── Tâche rdv_demande, rdv_retenir & reprise creneau_pris ─────────────────


def test_tache_rdv_demande_guards_and_zero_pii_portail_db(main_app, env):
    _main_db, portail_db, bucket, emails = env
    slot = _future_weekday_slot(5, 10, 0)
    mc = main_app.test_client()

    # Courriel de l'avocat titulaire → ignoré silencieusement
    bucket.put_json("submissions/_rdv_demande_own.json", {
        "type_rdv_cle": "consultation",
        "slot_debut_utc": slot.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nom_complet": "Moi",
        "courriel": "test@example.com",
        "telephone": "5145550000",
        "parties_adverses": ["Adverse X"],
        "sujet": "Sujet interne",
    })
    assert _post_task(mc, {"event": "rdv_demande", "invitation_id": "own", "batch": "own"}).status_code == 200
    assert portail_db.peek("invitations/own") is None
    assert emails == []

    # Plafond 3 / 24h par courriel
    now = datetime.now(UTC)
    for idx in range(3):
        portail_db.seed(f"invitations/old{idx}", {
            "id": f"old{idx}",
            "type": "rdv",
            "rdv_mode": "public",
            "email": "spam@exemple.ca",
            "created_at": now - timedelta(hours=1),
        })
    bucket.put_json("submissions/_rdv_demande_cap.json", {
        "type_rdv_cle": "consultation",
        "slot_debut_utc": slot.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nom_complet": "Spam",
        "courriel": "spam@exemple.ca",
        "telephone": "5145550000",
        "parties_adverses": [],
        "sujet": "Test",
    })
    assert _post_task(mc, {"event": "rdv_demande", "invitation_id": "cap", "batch": "cap"}).status_code == 200
    assert portail_db.peek("invitations/cap") is None

    # Cas légitime : crée demande.json en quarantaine, invitation sans PII sensible, 1 courriel idempotent
    bucket.put_json("submissions/_rdv_demande_req1.json", {
        "type_rdv_cle": "consultation",
        "type_rdv_libelle": "Consultation initiale",
        "duree_minutes": 30,
        "slot_debut_utc": slot.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nom_complet": "Marie Tremblay",
        "courriel": "marie@exemple.ca",
        "telephone": "5145551234",
        "parties_adverses": ["Construction Nord inc."],
        "sujet": "Infiltration eau",
    })
    assert _post_task(mc, {"event": "rdv_demande", "invitation_id": "req1", "batch": "req1"}).status_code == 200
    inv = portail_db.peek("invitations/req1")
    assert inv is not None
    assert inv["etat_rdv"] == "en_attente_courriel"
    assert "parties_adverses" not in inv
    assert "sujet" not in inv
    assert "telephone" not in inv
    assert bucket.get_json("submissions/req1/demande.json")["parties_adverses"] == ["Construction Nord inc."]
    assert len(emails) == 1

    # Rejeu idempotent → aucun second courriel
    assert _post_task(mc, {"event": "rdv_demande", "invitation_id": "req1", "batch": "req1"}).status_code == 200
    assert len(emails) == 1


def test_rdv_retenir_holds_slot_and_recovers_after_creneau_pris(portal_app, main_app, env, monkeypatch):
    main_db, portail_db, bucket, emails = env
    portal_app.config["PORTAIL_RDV_ACTIF"] = True
    slot_a = _future_weekday_slot(6, 10, 0)
    slot_b = _future_weekday_slot(6, 11, 0)
    _seed_dispo(portail_db, slot_a, slot_b)

    # Scénario A : créneau libre → retenu + audience à_confirmer
    portail_db.seed("invitations/req-ok", {
        "id": "req-ok",
        "type": "rdv",
        "rdv_mode": "public",
        "email": "marie@exemple.ca",
        "client_name": "Marie Tremblay",
        "display_label": "Consultation initiale",
        "type_rdv_cle": "consultation",
        "type_rdv_libelle": "Consultation initiale",
        "duree_minutes": 30,
        "etat_rdv": "en_attente_courriel",
        "statut": "envoyée",
        "slot_debut_utc": slot_a,
        "slot_fin_utc": slot_a + timedelta(minutes=30),
        "preavis_modification_heures": 24,
        "expires_at": datetime.now(UTC) + timedelta(days=2),
        "accuses": {},
    })
    bucket.put_json("submissions/req-ok/demande.json", {
        "nom_complet": "Marie Tremblay",
        "telephone": "5145551234",
        "parties_adverses": ["Syndic XYZ"],
        "sujet": "Contrat de service",
    })
    mc = main_app.test_client()
    assert _post_task(mc, {"event": "rdv_retenir", "invitation_id": "req-ok"}).status_code == 200

    inv_ok = portail_db.peek("invitations/req-ok")
    assert inv_ok["etat_rdv"] == "a_confirmer"
    assert inv_ok["statut"] == "ouverte"
    h = main_db.peek(f"hearings/{inv_ok['hearing_id']}")
    assert h["source"] == "portail"
    assert h["confirmation"] == "à_confirmer"
    assert h["status"] == "à_confirmer"
    assert h["modalite"] == "visioconférence"
    assert h["parties_adverses"] == ["Syndic XYZ"]
    assert len(emails) == 1

    # Scénario B : créneau déjà pris → bascule creneau_pris, puis reprise via /api/rdv/reserver
    portail_db.seed("invitations/req-race", {
        "id": "req-race",
        "type": "rdv",
        "rdv_mode": "public",
        "email": "paul@exemple.ca",
        "client_name": "Paul Roy",
        "display_label": "Consultation initiale",
        "type_rdv_cle": "consultation",
        "type_rdv_libelle": "Consultation initiale",
        "duree_minutes": 30,
        "etat_rdv": "en_attente_courriel",
        "statut": "envoyée",
        "slot_debut_utc": slot_a,
        "slot_fin_utc": slot_a + timedelta(minutes=30),
        "preavis_modification_heures": 24,
        "expires_at": datetime.now(UTC) + timedelta(days=2),
        "accuses": {},
    })
    bucket.put_json("submissions/req-race/demande.json", {
        "nom_complet": "Paul Roy",
        "telephone": "5145559999",
        "parties_adverses": [],
        "sujet": "Conseil préventif",
    })
    # slot_a est déjà bloqué par l'audience retenue de req-ok !
    assert _post_task(mc, {"event": "rdv_retenir", "invitation_id": "req-race"}).status_code == 200
    inv_race = portail_db.peek("invitations/req-race")
    assert inv_race["etat_rdv"] == "creneau_pris"
    assert not inv_race.get("hearing_id")

    web = portal_app.test_client()
    _portal_session(web, "req-race", "paul@exemple.ca")
    assert "précédemment demandé" in web.get("/rendez-vous").get_data(as_text=True)

    enqueued = []
    monkeypatch.setattr(taches, "signaler", lambda ev, inv_id, batch=None: enqueued.append((ev, inv_id, batch)))
    r_res = web.post("/api/rdv/reserver", json={"slot_debut_utc": slot_b.strftime("%Y-%m-%dT%H:%M:%SZ")})
    assert r_res.status_code == 202
    assert enqueued[0][0] == "rdv_reserver"
    batch_b = enqueued[0][2]

    assert _post_task(mc, {"event": "rdv_reserver", "invitation_id": "req-race", "batch": batch_b}).status_code == 200
    inv_race2 = portail_db.peek("invitations/req-race")
    assert inv_race2["etat_rdv"] == "a_confirmer"
    assert main_db.peek(f"hearings/{inv_race2['hearing_id']}")["start_datetime"] == slot_b


# ── Mode invité (type="rdv") & création Teams + DavX5 immédiate ───────────


def test_invite_rdv_emission_agenda_and_immediate_teams_confirmation(portal_app, main_app, env, monkeypatch):
    main_db, portail_db, bucket, emails = env
    portal_app.config["PORTAIL_RDV_ACTIF"] = True
    slot = _future_weekday_slot(7, 14, 0)
    _seed_dispo(portail_db, slot)

    main_db.seed("dossiers/d1", {"id": "d1", "file_number": "2026-010", "title": "Tremblay c. Gagnon"})
    main_db.seed("parties/p1", {"id": "p1", "type": "individual", "first_name": "Luc", "last_name": "Tremblay"})

    with main_app.test_request_context():
        inv, errs, _manual = portail_emission.emettre_invitation(
            "rdv",
            "luc@exemple.ca",
            dossier_id="d1",
            partie_id="p1",
            client_name="Luc Tremblay",
            type_rdv_cle="rencontre",
            duree_minutes=45,
            ordre_du_jour="00:00 Ouverture\n00:10 Preuve",
        )
    assert errs == []
    assert inv is not None
    inv_id = inv["id"]
    assert inv["rdv_mode"] == "invite"
    assert inv["etat_rdv"] == "a_choisir"
    stored_inv = portail_db.peek(f"invitations/{inv_id}")
    assert "ordre_du_jour" not in stored_inv
    assert bucket.get_json(f"submissions/{inv_id}/agenda.json")["ordre_du_jour"] == "00:00 Ouverture\n00:10 Preuve"

    web = portal_app.test_client()
    _portal_session(web, inv_id, "luc@exemple.ca")
    enqueued = []
    monkeypatch.setattr(taches, "signaler", lambda ev, iid, batch=None: enqueued.append((ev, iid, batch)))
    r = web.post("/api/rdv/reserver", json={
        "slot_debut_utc": slot.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "points_discussion": "Documents médicaux supplémentaires",
    })
    assert r.status_code == 202
    batch = enqueued[0][2]

    teams_calls = []

    def fake_create(**kw):
        teams_calls.append(kw)
        return {
            "graph_event_id": "EVT-INV-1",
            "graph_ical_uid": "UID-INV-1",
            "join_url": "https://teams.microsoft.com/l/meetup-join/19%3atest",
            "online_meeting_id": "OM-INV-1",
            "options_appliquees": True,
        }

    monkeypatch.setattr(graph_teams, "creer_reunion_teams", fake_create)
    mc = main_app.test_client()
    assert _post_task(mc, {"event": "rdv_reserver", "invitation_id": inv_id, "batch": batch}).status_code == 200

    assert len(teams_calls) == 1
    assert teams_calls[0]["ordre_du_jour"] == "00:00 Ouverture\n00:10 Preuve"
    assert teams_calls[0]["points_discussion"] == "Documents médicaux supplémentaires"

    inv_after = portail_db.peek(f"invitations/{inv_id}")
    assert inv_after["etat_rdv"] == "confirme"
    h = main_db.peek(f"hearings/{inv_after['hearing_id']}")
    assert h["confirmation"] == ""
    assert h["status"] == "confirmée"
    assert h["dossier_id"] == "d1"
    assert h["graph_event_id"] == "EVT-INV-1"
    assert h["conference_uri"] == "https://teams.microsoft.com/l/meetup-join/19%3atest"
    assert main_db.peek("dav_sync/dossier:d1") is not None
    assert len(emails) == 2  # invitation + confirmation


# ── Auto-service reporter / annuler + préavis 24 h ───────────────────────


def test_self_serve_reschedule_cancel_and_cutoff_enforcement(portal_app, main_app, env, monkeypatch):
    main_db, portail_db, _bucket, emails = env
    portal_app.config["PORTAIL_RDV_ACTIF"] = True
    slot_far_1 = _future_weekday_slot(8, 9, 30)
    slot_far_2 = _future_weekday_slot(9, 15, 0)
    _seed_dispo(portail_db, slot_far_1, slot_far_2)

    # 1) À l'intérieur du préavis (< 24 h) → UI et API + tâche refusent
    soon = datetime.now(UTC) + timedelta(hours=12)
    portail_db.seed("invitations/inv-late", {
        "id": "inv-late",
        "type": "rdv",
        "rdv_mode": "invite",
        "email": "client@exemple.com",
        "display_label": "Suivi",
        "duree_minutes": 30,
        "etat_rdv": "confirme",
        "statut": "ouverte",
        "hearing_id": "h-late",
        "slot_debut_utc": soon,
        "slot_fin_utc": soon + timedelta(minutes=30),
        "preavis_modification_heures": 24,
        "expires_at": soon + timedelta(days=3),
        "accuses": {},
    })
    main_db.seed("hearings/h-late", {
        "id": "h-late",
        "title": "Suivi",
        "start_datetime": soon,
        "end_datetime": soon + timedelta(minutes=30),
        "source": "portail",
        "confirmation": "",
        "status": "confirmée",
        "etag": "e1",
    })
    web = portal_app.test_client()
    _portal_session(web, "inv-late")
    html_late = web.get("/rendez-vous").get_data(as_text=True)
    assert "Déplacer ce rendez-vous" not in html_late
    assert web.post("/api/rdv/reporter", json={"slot_debut_utc": slot_far_2.strftime("%Y-%m-%dT%H:%M:%SZ")}).status_code == 422
    assert web.post("/api/rdv/annuler", json={}).status_code == 422

    mc = main_app.test_client()
    assert _post_task(mc, {"event": "rdv_annuler", "invitation_id": "inv-late", "batch": "b1"}).status_code == 200
    assert main_db.peek("hearings/h-late")["status"] == "confirmée"
    assert portail_db.peek("invitations/inv-late")["etat_rdv"] == "confirme"

    # 2) Hors préavis (>= 24 h) → report modifie Teams/audience/CTag, annulation ferme
    portail_db.seed("invitations/inv-ok", {
        "id": "inv-ok",
        "type": "rdv",
        "rdv_mode": "invite",
        "email": "client@exemple.com",
        "display_label": "Suivi",
        "duree_minutes": 30,
        "etat_rdv": "confirme",
        "statut": "ouverte",
        "hearing_id": "h-ok",
        "slot_debut_utc": slot_far_1,
        "slot_fin_utc": slot_far_1 + timedelta(minutes=30),
        "preavis_modification_heures": 24,
        "expires_at": slot_far_1 + timedelta(days=3),
        "accuses": {},
    })
    main_db.seed("hearings/h-ok", {
        "id": "h-ok",
        "title": "Suivi — Client",
        "dossier_id": "",
        "start_datetime": slot_far_1,
        "end_datetime": slot_far_1 + timedelta(minutes=30),
        "source": "portail",
        "confirmation": "",
        "status": "confirmée",
        "graph_event_id": "EVT-OK",
        "etag": "e-ok",
    })
    _portal_session(web, "inv-ok")
    enq = []
    monkeypatch.setattr(taches, "signaler", lambda ev, iid, batch=None: enq.append((ev, iid, batch)))
    assert web.post("/api/rdv/reporter", json={"slot_debut_utc": slot_far_2.strftime("%Y-%m-%dT%H:%M:%SZ")}).status_code == 202
    b_rep = enq[0][2]

    mod_calls = []
    cancel_calls = []
    monkeypatch.setattr(graph_teams, "modifier_reunion_teams", lambda gid, **kw: mod_calls.append((gid, kw)))
    monkeypatch.setattr(graph_teams, "annuler_reunion_teams", lambda gid, motif="": cancel_calls.append((gid, motif)))

    assert _post_task(mc, {"event": "rdv_reporter", "invitation_id": "inv-ok", "batch": b_rep}).status_code == 200
    assert len(mod_calls) == 1 and mod_calls[0][0] == "EVT-OK"
    assert main_db.peek("hearings/h-ok")["start_datetime"] == slot_far_2
    assert main_db.peek("dav_sync/general") is not None

    assert web.post("/api/rdv/annuler", json={}).status_code == 202
    b_ann = enq[1][2]
    assert _post_task(mc, {"event": "rdv_annuler", "invitation_id": "inv-ok", "batch": b_ann}).status_code == 200
    assert len(cancel_calls) == 1 and cancel_calls[0][0] == "EVT-OK"
    assert main_db.peek("hearings/h-ok")["status"] == "annulée"
    inv_closed = portail_db.peek("invitations/inv-ok")
    assert inv_closed["etat_rdv"] == "annule"
    assert inv_closed["statut"] == "traitée"
    # Page état reste accessible en lecture mais tout POST est refusé (401)
    assert "annulé" in web.get("/rendez-vous").get_data(as_text=True)
    assert web.post("/api/rdv/reporter", json={"slot_debut_utc": slot_far_1.strftime("%Y-%m-%dT%H:%M:%SZ")}).status_code == 401
    assert len(emails) == 2

    # 3) Audience publique non encore confirmée annulée par client → annulée_client
    portail_db.seed("invitations/inv-pend", {
        "id": "inv-pend",
        "type": "rdv",
        "rdv_mode": "public",
        "email": "pend@exemple.com",
        "display_label": "Consultation",
        "duree_minutes": 30,
        "etat_rdv": "a_confirmer",
        "statut": "ouverte",
        "hearing_id": "h-pend",
        "slot_debut_utc": slot_far_1,
        "slot_fin_utc": slot_far_1 + timedelta(minutes=30),
        "preavis_modification_heures": 24,
        "expires_at": slot_far_1 + timedelta(days=3),
        "accuses": {},
    })
    main_db.seed("hearings/h-pend", {
        "id": "h-pend",
        "title": "Consultation",
        "start_datetime": slot_far_1,
        "end_datetime": slot_far_1 + timedelta(minutes=30),
        "source": "portail",
        "confirmation": "à_confirmer",
        "status": "à_confirmer",
        "etag": "e-p",
    })
    assert _post_task(mc, {"event": "rdv_annuler", "invitation_id": "inv-pend", "batch": "b-p"}).status_code == 200
    assert main_db.peek("hearings/h-pend")["confirmation"] == "annulée_client"
    assert main_db.peek("hearings/h-pend")["status"] == "annulée"


# ── Réception « Rendez-vous » (conflits, confirmation Teams, refus) ───────


def test_reception_portail_rdv_conflicts_overlap_confirm_and_refuse(main_app, env, monkeypatch):
    main_db, portail_db, _bucket, emails = env
    slot = _future_weekday_slot(6, 10, 0)

    main_db.seed("parties/p-adv", {
        "id": "p-adv",
        "type": "organization",
        "organization_name": "Gestion Nord inc.",
        "contact_role": "adverse",
    })
    main_db.seed("hearings/h-autre", {
        "id": "h-autre",
        "title": "Conférence de gestion Cour",
        "start_datetime": slot + timedelta(minutes=15),
        "end_datetime": slot + timedelta(minutes=45),
        "source": "manuelle",
        "confirmation": "",
        "status": "confirmée",
    })
    portail_db.seed("invitations/inv-c1", {
        "id": "inv-c1",
        "type": "rdv",
        "rdv_mode": "public",
        "email": "dem@exemple.ca",
        "etat_rdv": "a_confirmer",
        "statut": "ouverte",
        "hearing_id": "h-p1",
        "expires_at": slot + timedelta(days=3),
    })
    main_db.seed("hearings/h-p1", {
        "id": "h-p1",
        "title": "Consultation initiale — Jean Dem",
        "hearing_type": "consultation",
        "start_datetime": slot,
        "end_datetime": slot + timedelta(minutes=30),
        "source": "portail",
        "confirmation": "à_confirmer",
        "status": "à_confirmer",
        "client_nom": "Jean Dem",
        "client_email": "dem@exemple.ca",
        "client_telephone": "(514) 555-1111",
        "parties_adverses": ["Gestion Nord inc."],
        "sujet_client": "Litige bail",
        "portail_invitation_id": "inv-c1",
        "teams_agenda": "00:00 Accueil\n00:15 Analyse",
        "etag": "e-p1",
    })

    portail_db.seed("invitations/inv-r1", {
        "id": "inv-r1",
        "type": "rdv",
        "rdv_mode": "public",
        "email": "refus@exemple.ca",
        "etat_rdv": "a_confirmer",
        "statut": "ouverte",
        "hearing_id": "h-p2",
        "expires_at": slot + timedelta(days=3),
    })
    main_db.seed("hearings/h-p2", {
        "id": "h-p2",
        "title": "Consultation initiale — Refusé",
        "hearing_type": "consultation",
        "start_datetime": slot + timedelta(hours=2),
        "end_datetime": slot + timedelta(hours=2, minutes=30),
        "source": "portail",
        "confirmation": "à_confirmer",
        "status": "à_confirmer",
        "client_nom": "Autre",
        "client_email": "refus@exemple.ca",
        "portail_invitation_id": "inv-r1",
        "etag": "e-p2",
    })

    mc = _login_main(main_app.test_client())
    html = mc.get("/reception/?onglet=rdv").get_data(as_text=True)
    assert "Gestion Nord inc." in html
    assert "Rapprochement visuel (vérification de conflit)" in html
    assert "Chevauchement détecté avec un autre événement du calendrier." in html
    assert "00:00 Accueil" in html

    teams_created = []
    monkeypatch.setattr(
        graph_teams,
        "creer_reunion_teams",
        lambda **kw: teams_created.append(kw) or {
            "graph_event_id": "EVT-CONF-1",
            "graph_ical_uid": "UID-CONF-1",
            "join_url": "https://teams.microsoft.com/l/meetup-join/conf1",
            "online_meeting_id": "OM-CONF-1",
            "options_appliquees": True,
        },
    )
    r_conf = mc.post("/reception/rdv/h-p1/confirmer", data={
        "expected_etag": "e-p1",
        "teams_agenda": "00:00 Ordre personnalisé\n00:20 Conclusion",
    })
    assert r_conf.status_code == 302
    assert len(teams_created) == 1
    assert teams_created[0]["ordre_du_jour"] == "00:00 Ordre personnalisé\n00:20 Conclusion"
    assert main_db.peek("hearings/h-p1")["confirmation"] == ""
    assert main_db.peek("hearings/h-p1")["conference_uri"] == "https://teams.microsoft.com/l/meetup-join/conf1"
    assert portail_db.peek("invitations/inv-c1")["etat_rdv"] == "confirme"
    assert main_db.peek("dav_sync/general") is not None

    r_ref = mc.post("/reception/rdv/h-p2/refuser", data={"expected_etag": "e-p2"})
    assert r_ref.status_code == 302
    assert main_db.peek("hearings/h-p2")["confirmation"] == "refusée"
    inv_r1 = portail_db.peek("invitations/inv-r1")
    assert inv_r1["etat_rdv"] == "refuse"
    assert inv_r1["statut"] == "refusée"
    assert len(emails) == 1 and emails[0][0] == "refus@exemple.ca"
    assert "aucun mandat" in emails[0][2].lower()
