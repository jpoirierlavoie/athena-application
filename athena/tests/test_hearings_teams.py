"""Génération et synchronisation Teams des audiences extrajudiciaires (lot 1)."""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from markupsafe import escape

_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ATHENA)

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from flask import Flask  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync
    import models.dossier as dossier_model
    import models.hearing as hearing_model
    import models.partie as partie_model
    import models.rendez_vous_reglages as rv_model
    import routes.hearings as rh

from config import Config  # noqa: E402
from routes._helpers import theme_context  # noqa: E402
from tests._fake_firestore import install  # noqa: E402
from tz import mtl_to_utc, to_mtl  # noqa: E402
from utils.graph import GraphError, GraphNotConfigured  # noqa: E402
from utils.html_attr import jsattr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.rendez_vous_defaults import default_reglages  # noqa: E402

UTC = timezone.utc


@pytest.fixture()
def real_store(monkeypatch):
    modules = [
        m
        for name, m in sorted(sys.modules.items())
        if (name.startswith("models.") or name == "dav.sync")
        and getattr(m, "db", None) is not None
    ]
    store = install(monkeypatch, *modules)
    store.seed(
        "parties/p1",
        {
            "id": "p1",
            "type": "individual",
            "first_name": "Jean",
            "last_name": "Tremblay",
            "email": "client@exemple.ca",
            "contact_role": "client",
            "etag": "pe-1",
            "vcard_uid": "vc-p1",
        },
    )
    store.seed(
        "dossiers/d1",
        {
            "id": "d1",
            "file_number": "2026-001",
            "title": "Tremblay c. Lavoie",
            "status": "actif",
            "client_ids": ["p1"],
            "opposing_party_ids": [],
            "avocat_ids": [],
            "etag": "de-1",
        },
    )
    return store


@pytest.fixture()
def client(real_store):
    app = Flask(__name__, template_folder=os.path.join(_ATHENA, "templates"))
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.jinja_env.globals.update(
        csrf_token=lambda: "tok", ms=ms, csp_nonce=lambda: "n"
    )
    app.jinja_env.filters["to_mtl"] = to_mtl
    app.jinja_env.filters["jsattr"] = jsattr
    app.register_blueprint(rh.hearings_bp)
    app.add_url_rule(
        "/dossiers/<dossier_id>",
        endpoint="dossiers.dossier_detail",
        view_func=lambda dossier_id: "",
    )
    app.context_processor(theme_context)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(UTC) + timedelta(hours=1)
    return c


@pytest.fixture()
def teams_on(monkeypatch):
    for key, val in (
        ("GRAPH_TENANT_ID", "tenant-1"),
        ("GRAPH_CLIENT_ID", "client-1"),
        ("GRAPH_CLIENT_SECRET", "secret-1"),
        ("GRAPH_SENDER_UPN", "reception@example.com"),
        ("BOOKINGS_JURISTE_UPN", "juriste@example.com"),
    ):
        monkeypatch.setattr(Config, key, val)
    monkeypatch.setattr(Config, "TEAMS_REUNIONS_ACTIF", True)


def _form(**over) -> dict[str, str]:
    base = {
        "title": "Consultation Teams",
        "start_date": "2026-11-24",
        "start_time": "10:00",
        "end_time": "11:00",
        "hearing_type": "consultation",
        "status": "confirmée",
        "modalite": "visioconférence",
        "dossier_id": "d1",
        "dossier_display": "2026-001",
        "generer_teams": "on",
        "teams_attendee_emails": "client@exemple.ca",
        "teams_agenda": "1. Accueil (5 min)\n2. Fond (45 min)",
        "reminder_minutes": "1440",
    }
    base.update(over)
    return base


def _seed_teams_hearing(store, **over) -> dict:
    start = mtl_to_utc(datetime(2026, 11, 24, 10, 0))
    doc = {
        "id": "h-teams-1",
        "title": "Consultation Teams",
        "start_datetime": start,
        "end_datetime": start + timedelta(hours=1),
        "all_day": False,
        "hearing_type": "consultation",
        "status": "confirmée",
        "modalite": "visioconférence",
        "conference_uri": "https://teams.microsoft.com/l/meetup-join/orig",
        "dossier_id": "d1",
        "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. Lavoie",
        "location": "",
        "description": "",
        "reminder_minutes": 1440,
        "confirmation": "confirmée",
        "source": "athena_teams",
        "graph_event_id": "EVT-ORIG",
        "graph_ical_uid": "ICAL-ORIG",
        "online_meeting_id": "OM-ORIG",
        "teams_agenda": "1. Accueil (5 min)\n2. Fond (45 min)",
        "transcript_status": "",
        "attendee_emails": ["client@exemple.ca"],
        "vevent_uid": "vev-h-teams-1",
        "etag": "etag-orig",
    }
    doc.update(over)
    store.seed(f"hearings/{doc['id']}", doc)
    return doc


# ── GET /audiences/new : prefill & switch state ───────────────────────────


def test_new_form_prefills_client_emails_and_agendas_when_teams_enabled(
    client, teams_on
):
    r = client.get("/audiences/new?dossier_id=d1")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "Générer une réunion Microsoft Teams" in body
    assert "client@exemple.ca" in body
    assert "Vérification préliminaire" in body
    assert "TEAMS_REUNIONS_ACTIF" not in body


def test_new_form_hides_toggle_or_shows_switch_off_notice_when_disabled(
    client, monkeypatch
):
    monkeypatch.setattr(Config, "TEAMS_REUNIONS_ACTIF", False)
    r = client.get("/audiences/new?dossier_id=d1")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert 'name="generer_teams"' not in body
    assert "TEAMS_REUNIONS_ACTIF" in body


# ── POST /audiences/ : Teams creation, warning, and refusals ──────────────


def test_create_teams_hearing_persists_graph_metadata_and_logs(
    client, real_store, teams_on, monkeypatch, caplog
):
    called = {}

    def fake_create(**kw):
        called.update(kw)
        return {
            "graph_event_id": "EVT-NEW",
            "graph_ical_uid": "ICAL-NEW",
            "join_url": "https://teams.microsoft.com/l/meetup-join/new-call",
            "online_meeting_id": "OM-NEW",
            "options_appliquees": True,
        }

    monkeypatch.setattr(rh.graph_teams, "creer_reunion_teams", fake_create)

    with caplog.at_level(logging.INFO, logger="pallas.bookings"):
        r = client.post("/audiences/", data=_form())

    assert r.status_code == 302
    assert "message=" not in r.headers["Location"]

    assert called["sujet"] == "Consultation Teams"
    assert called["courriels_invites"] == ("client@exemple.ca",)
    assert called["ordre_du_jour"] == "1. Accueil (5 min)\n2. Fond (45 min)"

    hearings = list(real_store.collection("hearings").stream())
    assert len(hearings) == 1
    stored = hearings[0].to_dict()
    assert stored["source"] == "athena_teams"
    assert stored["graph_event_id"] == "EVT-NEW"
    assert stored["graph_ical_uid"] == "ICAL-NEW"
    assert stored["online_meeting_id"] == "OM-NEW"
    assert stored["conference_uri"] == "https://teams.microsoft.com/l/meetup-join/new-call"
    assert stored["attendee_emails"] == ["client@exemple.ca"]
    assert stored["teams_agenda"] == "1. Accueil (5 min)\n2. Fond (45 min)"

    assert any(
        rec.getMessage() == "teams_reunion_creee"
        for rec in caplog.records
    )


def test_create_teams_hearing_warns_honestly_when_options_fail(
    client, real_store, teams_on, monkeypatch
):
    monkeypatch.setattr(
        rh.graph_teams,
        "creer_reunion_teams",
        lambda **kw: {
            "graph_event_id": "EVT-PARTIAL",
            "graph_ical_uid": "ICAL-PARTIAL",
            "join_url": "https://teams.microsoft.com/l/meetup-join/partial",
            "online_meeting_id": "",
            "options_appliquees": False,
        },
    )

    r = client.post("/audiences/", data=_form())
    assert r.status_code == 302
    detail_r = client.get(r.headers["Location"])
    assert detail_r.status_code == 200
    body = detail_r.data.decode("utf-8")
    assert "transcription" in body.lower() or "options" in body.lower()


def test_create_teams_graph_error_refuses_200_saves_nothing_and_preserves_inputs(
    client, real_store, teams_on, monkeypatch
):
    monkeypatch.setattr(
        rh.graph_teams,
        "creer_reunion_teams",
        lambda **kw: (_ for _ in ()).throw(GraphError("Échec POST Graph (HTTP 503).")),
    )

    r = client.post(
        "/audiences/",
        data=_form(
            title="Mon titre saisi",
            teams_agenda="1. Agenda conservé (30 min)",
        ),
    )
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "Teams" in body
    assert "Mon titre saisi" in body
    assert "1. Agenda conservé (30 min)" in body
    assert list(real_store.collection("hearings").stream()) == []


@pytest.mark.parametrize(
    ("over", "expected_substr"),
    [
        ({"hearing_type": "audience"}, "extrajudiciaires"),
        ({"all_day": "on", "end_date": ""}, "journées entières"),
        (
            {"frequence": "hebdomadaire", "fin_mode": "count", "fin_count": "3"},
            "aux séries",
        ),
        ({"teams_attendee_emails": "pas-un-courriel"}, "courriel"),
        ({"teams_agenda": "1. Test <script>"}, "chevrons"),
        ({"status": "annulée"}, "annulée"),
    ],
)
def test_create_teams_refuses_invalid_combinations_with_zero_graph_calls(
    client, real_store, teams_on, monkeypatch, over, expected_substr
):
    def _forbid(**kw):
        raise AssertionError("creer_reunion_teams ne doit pas être appelé")

    monkeypatch.setattr(rh.graph_teams, "creer_reunion_teams", _forbid)

    r = client.post("/audiences/", data=_form(**over))
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert expected_substr in body.lower()
    assert list(real_store.collection("hearings").stream()) == []


# ── POST /audiences/<id> (edit & cancel) & DELETE ─────────────────────────


def test_update_teams_hearing_slot_patches_graph_and_warns_on_graph_error(
    client, real_store, teams_on, monkeypatch, caplog
):
    _seed_teams_hearing(real_store)
    patched = []
    monkeypatch.setattr(
        rh.graph_teams,
        "modifier_reunion_teams",
        lambda gid, **kw: patched.append((gid, kw)),
    )

    r = client.post(
        "/audiences/h-teams-1",
        data=_form(
            title="Consultation modifiée",
            start_time="11:00",
            end_time="12:00",
            etag="etag-orig",
        ),
    )
    assert r.status_code == 302
    assert len(patched) == 1
    assert patched[0][0] == "EVT-ORIG"
    assert patched[0][1]["sujet"] == "Consultation modifiée"

    # Now simulate Graph error on a second edit: Athena persists, user gets warning.
    stored = real_store.peek("hearings/h-teams-1")
    monkeypatch.setattr(
        rh.graph_teams,
        "modifier_reunion_teams",
        lambda *a, **k: (_ for _ in ()).throw(GraphError("HTTP 502")),
    )
    with caplog.at_level(logging.INFO, logger="pallas.bookings"):
        r2 = client.post(
            "/audiences/h-teams-1",
            data=_form(
                title="Consultation resaisie",
                start_time="13:00",
                end_time="14:00",
                etag=stored["etag"],
            ),
        )
    assert r2.status_code == 302
    assert "erreur=" in r2.headers["Location"]
    assert any(
        rec.getMessage() == "teams_reunion_erreur_graph"
        for rec in caplog.records
    )


def test_update_teams_hearing_to_annulee_cancels_in_graph(
    client, real_store, teams_on, monkeypatch, caplog
):
    _seed_teams_hearing(real_store)
    cancelled = []
    monkeypatch.setattr(
        rh.graph_teams,
        "annuler_reunion_teams",
        lambda gid, motif="": cancelled.append((gid, motif)),
    )

    with caplog.at_level(logging.INFO, logger="pallas.bookings"):
        r = client.post(
            "/audiences/h-teams-1",
            data=_form(status="annulée", etag="etag-orig"),
        )
    assert r.status_code == 302
    assert len(cancelled) == 1 and cancelled[0][0] == "EVT-ORIG"
    assert any(
        rec.getMessage() == "teams_reunion_annulee"
        for rec in caplog.records
    )


def test_delete_teams_hearing_deletes_graph_event_unless_already_cancelled(
    client, real_store, teams_on, monkeypatch
):
    _seed_teams_hearing(real_store, id="h-active", status="confirmée", graph_event_id="EVT-ACT")
    _seed_teams_hearing(real_store, id="h-canc", status="annulée", graph_event_id="EVT-CANC")

    deleted = []
    monkeypatch.setattr(
        rh.graph_teams, "supprimer_reunion_teams", lambda gid: deleted.append(gid)
    )

    r1 = client.post("/audiences/h-active/delete")
    assert r1.status_code == 302
    assert deleted == ["EVT-ACT"]

    r2 = client.post("/audiences/h-canc/delete")
    assert r2.status_code == 302
    assert deleted == ["EVT-ACT"], "already-annulée event must not hit Graph delete"


def test_detail_renders_teams_agenda_and_invitee_emails(client, real_store):
    _seed_teams_hearing(real_store)
    r = client.get("/audiences/h-teams-1")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "Ordre du jour minuté (Teams)" in body
    assert "1. Accueil (5 min)" in body
    assert "client@exemple.ca" in body
