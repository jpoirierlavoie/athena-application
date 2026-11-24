"""Graph Teams online-meeting client & diagnostic probe (utils/graph_teams)."""

import base64
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from config import Config  # noqa: E402
from utils import graph, graph_teams as gt  # noqa: E402
from utils.graph import GraphError, GraphNotConfigured  # noqa: E402
from utils.graph_calendrier import MIROIR_CATEGORIE  # noqa: E402

UTC = timezone.utc
UPN = "juriste@example.com"
UID = "entra-uid-42"


def _jwt_with_roles(roles: list[str]) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode("ascii").rstrip("=")
    body_bytes = json.dumps({"roles": roles}).encode("utf-8")
    payload = base64.urlsafe_b64encode(body_bytes).decode("ascii").rstrip("=")
    return f"{header}.{payload}.sig"


@pytest.fixture()
def configured(monkeypatch):
    for key, val in (
        ("GRAPH_TENANT_ID", "tenant-1"),
        ("GRAPH_CLIENT_ID", "client-99"),
        ("GRAPH_CLIENT_SECRET", "secret-val"),
        ("BOOKINGS_JURISTE_UPN", UPN),
        ("BOOKINGS_JURISTE_OBJECT_ID", UID),
        ("GRAPH_SENDER_UPN", UPN),
    ):
        monkeypatch.setattr(Config, key, val)


# ── Helpers & PowerShell snippet ──────────────────────────────────────────


def test_powershell_setup_commands_use_placeholders_or_supplied_values():
    default_cmds = gt.powershell_setup_commands()
    assert default_cmds[0] == "Connect-MicrosoftTeams"
    assert "Set-CsTeamsMeetingConfiguration -EnableGraphTranscriptAccess $true" in default_cmds
    assert (
        "Set-CsTeamsMeetingPolicy -Identity Global "
        "-AllowCloudRecording $true "
        "-AllowTranscription $true "
        "-AutoRecording Enabled "
        "-NewMeetingRecordingExpirationDays 1 "
        "-Copilot EnabledWithTranscriptDefaultOn"
    ) in default_cmds
    assert "<GRAPH_CLIENT_ID>" in default_cmds[2]
    assert "<BOOKINGS_JURISTE_UPN>" in default_cmds[3]

    real_cmds = gt.powershell_setup_commands(
        client_id="cid-123", juriste_upn="me@cabinet.ca"
    )
    assert '-AppIds "cid-123"' in real_cmds[2]
    assert '-Identity "me@cabinet.ca"' in real_cmds[3]


def test_construire_corps_html_reunion_escapes_html_and_adds_ai_notice():
    html_body = gt.construire_corps_html_reunion(
        "1. Intro <script>alert(1)</script>\r\n2. Suite",
        points_discussion="Verifier <dossier>",
    )
    assert "<script>" not in html_body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;<br>2. Suite" in html_body
    assert "<strong>Points à aborder</strong>" in html_body
    assert "Verifier &lt;dossier&gt;" in html_body
    assert "enregistrement" in html_body
    assert "transcription et la prise de notes assistée" in html_body

    no_points = gt.construire_corps_html_reunion("1. Point unique")
    assert "Points à aborder" not in no_points


# ── Unconfigured guard ────────────────────────────────────────────────────


def test_write_helpers_raise_when_unconfigured(monkeypatch):
    monkeypatch.setattr(Config, "GRAPH_TENANT_ID", "")
    monkeypatch.setattr(Config, "BOOKINGS_JURISTE_UPN", "")
    debut = datetime(2026, 11, 24, 14, 0, tzinfo=UTC)
    fin = debut + timedelta(minutes=30)

    with pytest.raises(GraphNotConfigured):
        gt.creer_reunion_teams(
            sujet="Consultation", debut_utc=debut, fin_utc=fin, ordre_du_jour="1. Test"
        )
    with pytest.raises(GraphNotConfigured):
        gt.modifier_reunion_teams("evt-1", sujet="Mod", debut_utc=debut, fin_utc=fin)
    with pytest.raises(GraphNotConfigured):
        gt.annuler_reunion_teams("evt-1")
    with pytest.raises(GraphNotConfigured):
        gt.supprimer_reunion_teams("evt-1")


# ── Create, update, cancel, delete ────────────────────────────────────────


def test_creer_reunion_teams_happy_path_sets_options_and_escapes_odata_quote(
    configured, monkeypatch
):
    join_url = "https://teams.microsoft.com/l/meetup-join/19%3ameeting/O'Brien"
    posts = []
    gets = []
    patches = []

    def fake_post(path, payload):
        posts.append((path, payload))
        return {
            "id": "EVT-100",
            "iCalUId": "ICAL-100",
            "onlineMeeting": {"joinUrl": join_url},
        }

    def fake_get(path, params=None, **kw):
        gets.append((path, params))
        assert not path.endswith("/juriste%40example.com"), "User.Read.All lookup forbidden"
        return {"value": [{"id": "OM-900"}]}

    def fake_patch(path, payload):
        patches.append((path, payload))
        return {}

    monkeypatch.setattr(graph, "graph_post", fake_post)
    monkeypatch.setattr(graph, "graph_get", fake_get)
    monkeypatch.setattr(graph, "graph_patch", fake_patch)

    debut = datetime(2026, 11, 24, 14, 0, tzinfo=UTC)
    fin = debut + timedelta(minutes=30)
    res = gt.creer_reunion_teams(
        sujet="Rencontre préliminaire",
        debut_utc=debut,
        fin_utc=fin,
        ordre_du_jour="1. Salutations (5 min)",
        courriels_invites=("client@exemple.ca", " "),
        transaction_id="tx-1",
    )

    assert res == {
        "graph_event_id": "EVT-100",
        "graph_ical_uid": "ICAL-100",
        "join_url": join_url,
        "online_meeting_id": "OM-900",
        "options_appliquees": True,
    }

    post_path, ev_payload = posts[0]
    assert post_path == "/users/juriste%40example.com/events"
    assert ev_payload["isOnlineMeeting"] is True
    assert ev_payload["onlineMeetingProvider"] == "teamsForBusiness"
    assert ev_payload["categories"] == [MIROIR_CATEGORIE]
    assert "singleValueExtendedProperties" not in ev_payload
    assert ev_payload["transactionId"] == "tx-1"
    assert ev_payload["attendees"] == [
        {"emailAddress": {"address": "client@exemple.ca"}, "type": "required"}
    ]
    assert ev_payload["start"] == {"dateTime": "2026-11-24T14:00:00", "timeZone": "UTC"}

    assert len(gets) == 1
    assert gets[0][0] == f"/users/{UID}/onlineMeetings"
    assert gets[0][1]["$filter"] == (
        "JoinWebUrl eq 'https://teams.microsoft.com/l/meetup-join/19%3ameeting/O''Brien'"
    )

    patch_path, om_payload = patches[0]
    assert patch_path == f"/users/{UID}/onlineMeetings/OM-900"
    assert om_payload == {
        "allowRecording": True,
        "allowTranscription": True,
        "recordAutomatically": True,
        "meetingSpokenLanguageTag": "fr-ca",
        "lobbyBypassSettings": {
            "scope": "organizer",
            "isDialInBypassEnabled": False,
        },
    }


def test_creer_reunion_teams_degrades_honestly_when_step2_fails(configured, monkeypatch):
    monkeypatch.setattr(
        graph,
        "graph_post",
        lambda path, payload: {
            "id": "EVT-101",
            "iCalUId": "ICAL-101",
            "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/x"},
        },
    )
    monkeypatch.setattr(
        graph,
        "graph_get",
        lambda path, params=None, **kw: (_ for _ in ()).throw(
            GraphError("Échec d'un GET Graph (HTTP 403).")
        ),
    )

    debut = datetime(2026, 11, 24, 14, 0, tzinfo=UTC)
    res = gt.creer_reunion_teams(
        sujet="Test",
        debut_utc=debut,
        fin_utc=debut + timedelta(minutes=30),
        ordre_du_jour="1. Point",
    )
    assert res["graph_event_id"] == "EVT-101"
    assert res["join_url"].startswith("https://teams.microsoft.com/")
    assert res["online_meeting_id"] == ""
    assert res["options_appliquees"] is False


def test_creer_reunion_teams_raises_when_event_lacks_id_or_join_url(
    configured, monkeypatch
):
    monkeypatch.setattr(
        graph, "graph_post", lambda path, payload: {"id": "EVT-102", "onlineMeeting": {}}
    )
    debut = datetime(2026, 11, 24, 14, 0, tzinfo=UTC)
    with pytest.raises(GraphError):
        gt.creer_reunion_teams(
            sujet="Test",
            debut_utc=debut,
            fin_utc=debut + timedelta(minutes=30),
            ordre_du_jour="1. Point",
        )


def test_modifier_reunion_teams_patches_subject_and_utc_times(configured, monkeypatch):
    calls = []
    monkeypatch.setattr(
        graph, "graph_patch", lambda path, payload: calls.append((path, payload)) or {}
    )
    debut = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
    fin = debut + timedelta(hours=1)
    gt.modifier_reunion_teams("EVT/9", sujet="Nouveau titre", debut_utc=debut, fin_utc=fin)

    assert len(calls) == 1
    assert calls[0][0] == "/users/juriste%40example.com/events/EVT%2F9"
    assert calls[0][1]["subject"] == "Nouveau titre"
    assert calls[0][1]["start"]["dateTime"] == "2026-11-24T15:00:00"
    assert calls[0][1]["end"]["dateTime"] == "2026-11-24T16:00:00"


def test_annuler_and_supprimer_treat_404_as_noop_and_reraise_other_errors(
    configured, monkeypatch
):
    monkeypatch.setattr(
        graph,
        "graph_post",
        lambda path, payload: (_ for _ in ()).throw(GraphError("HTTP 404")),
    )
    monkeypatch.setattr(
        graph,
        "graph_delete",
        lambda path: (_ for _ in ()).throw(GraphError("HTTP 404")),
    )
    gt.annuler_reunion_teams("EVT-gone", motif="Reporté")
    gt.supprimer_reunion_teams("EVT-gone")

    monkeypatch.setattr(
        graph,
        "graph_post",
        lambda path, payload: (_ for _ in ()).throw(GraphError("HTTP 500")),
    )
    monkeypatch.setattr(
        graph,
        "graph_delete",
        lambda path: (_ for _ in ()).throw(GraphError("HTTP 500")),
    )
    with pytest.raises(GraphError, match="500"):
        gt.annuler_reunion_teams("EVT-err")
    with pytest.raises(GraphError, match="500"):
        gt.supprimer_reunion_teams("EVT-err")


# ── Read-only diagnostic & pagination guard ───────────────────────────────


def test_verifier_connexion_teams_when_unconfigured(configured, monkeypatch):
    monkeypatch.setattr(Config, "BOOKINGS_JURISTE_OBJECT_ID", "")
    res = gt.verifier_connexion_teams()
    assert res["configure"] is False
    assert res["jeton_valide"] is False
    assert res["erreurs"]

    monkeypatch.setattr(Config, "GRAPH_TENANT_ID", "")
    res2 = gt.verifier_connexion_teams()
    assert res2["configure"] is False


def test_verifier_connexion_teams_reports_missing_roles_and_403_policy(
    configured, monkeypatch
):
    monkeypatch.setattr(
        graph,
        "jeton_application",
        lambda: _jwt_with_roles(["Calendars.ReadWrite", "Mail.Send"]),
    )

    def fake_get(path, params=None, **kw):
        if path == "/users/juriste%40example.com/calendar":
            return {"id": "cal-1"}
        if path == f"/users/{UID}/onlineMeetings":
            raise GraphError("Échec d'un GET Graph (HTTP 403).")
        return {"value": []}

    monkeypatch.setattr(graph, "graph_get", fake_get)
    monkeypatch.setattr(
        Config,
        "courrier_configured",
        classmethod(lambda cls: True),
        raising=False,
    )
    fake_courrier = types.SimpleNamespace(
        verifier_connexion=lambda: {"onedrive_accessible": True}
    )
    monkeypatch.setitem(sys.modules, "utils.graph_courrier", fake_courrier)
    monkeypatch.setattr(
        sys.modules["utils"], "graph_courrier", fake_courrier, raising=False
    )

    res = gt.verifier_connexion_teams()
    assert res["jeton_valide"] is True
    assert "OnlineMeetings.ReadWrite.All" in res["roles_manquants"]
    assert res["utilisateur_accessible"] is True
    assert res["online_meetings_accessible"] is False
    assert res["politique_teams_requise"] is True
    assert res["transcriptions_accessibles"] is True
    assert res["onedrive_configure"] is True
    assert res["onedrive_accessible"] is True
    assert any("CsApplicationAccessPolicy" in e for e in res["erreurs"])


def test_verifier_connexion_teams_treats_404_on_valid_probe_url_as_accessible(
    configured, monkeypatch
):
    """HTTP 404 on the syntactically valid Teams meetup-join URL proves
    CsApplicationAccessPolicy succeeded (the synthetic meeting id simply does
    not exist), and unconfigured OneDrive does not fail the diagnostic."""
    monkeypatch.setattr(
        graph,
        "jeton_application",
        lambda: _jwt_with_roles(list(gt.REQUIRED_GRAPH_ROLES)),
    )
    monkeypatch.setattr(
        Config,
        "courrier_configured",
        classmethod(lambda cls: False),
        raising=False,
    )
    seen_paths = []

    def fake_get(path, params=None, **kw):
        seen_paths.append((path, params))
        if path == "/users/juriste%40example.com/calendar":
            return {"id": "cal-1"}
        if path == f"/users/{UID}/onlineMeetings":
            flt = (params or {}).get("$filter", "")
            assert "19%3ameeting_00000000-0000-0000-0000-000000000000%40thread.v2" in flt
            assert "tenant-1" in flt and UID in flt
            raise GraphError("Échec d'un GET Graph (HTTP 404).")
        return {"value": []}

    monkeypatch.setattr(graph, "graph_get", fake_get)

    res = gt.verifier_connexion_teams()
    assert res["utilisateur_accessible"] is True
    assert res["online_meetings_accessible"] is True
    assert res["politique_teams_requise"] is False
    assert res["transcriptions_accessibles"] is True
    assert res["onedrive_configure"] is False
    assert res["onedrive_accessible"] is False
    assert res["erreurs"] == []
    assert not any(p.endswith("/juriste%40example.com") for p, _ in seen_paths)


def test_verifier_connexion_teams_transcript_probe_stops_after_one_page(
    configured, monkeypatch
):
    """Pins Step 6b: ``getAllTranscripts`` probe passes ``follow_next=False``
    so a ``$top=1`` collection response carrying ``@odata.nextLink`` never loops
    through every tenant transcript."""
    monkeypatch.setattr(
        graph,
        "jeton_application",
        lambda: _jwt_with_roles(list(gt.REQUIRED_GRAPH_ROLES)),
    )
    monkeypatch.setattr(
        Config,
        "courrier_configured",
        classmethod(lambda cls: False),
        raising=False,
    )

    session_urls: list[str] = []

    class _FakeResp:
        def __init__(self, payload: dict, status_code: int = 200):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def fake_session_get(url, params=None, headers=None, timeout=None):
        session_urls.append(url)
        if "/calendar" in url:
            return _FakeResp({"id": "cal-1"})
        if url.endswith(f"/users/{UID}/onlineMeetings"):
            return _FakeResp({"error": {"code": "NotFound"}}, status_code=404)
        if "/getAllTranscripts(" in url:
            return _FakeResp(
                {
                    "value": [{"id": "t-1"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/next-page",
                }
            )
        return _FakeResp({"value": []})

    monkeypatch.setattr(graph, "_auth_headers", lambda: {"Authorization": "Bearer tok"})
    monkeypatch.setattr(graph._session, "get", fake_session_get)

    res = gt.verifier_connexion_teams()
    assert res["utilisateur_accessible"] is True
    assert res["online_meetings_accessible"] is True
    assert res["transcriptions_accessibles"] is True
    assert res["roles_manquants"] == []
    assert res["onedrive_configure"] is False
    assert res["erreurs"] == []
    transcript_calls = [u for u in session_urls if "getAllTranscripts" in u]
    assert len(transcript_calls) == 1
    assert not any("next-page" in u for u in session_urls)


def test_graph_get_default_still_follows_next_link(monkeypatch):
    pages = []

    class _FakeResp:
        def __init__(self, payload: dict):
            self.status_code = 200
            self._payload = payload

        def json(self):
            return self._payload

    def fake_session_get(url, params=None, headers=None, timeout=None):
        pages.append(url)
        if url.endswith("/items"):
            return _FakeResp(
                {
                    "value": [{"id": 1}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/items?page=2",
                }
            )
        return _FakeResp({"value": [{"id": 2}]})

    monkeypatch.setattr(graph, "_auth_headers", lambda: {"Authorization": "Bearer tok"})
    monkeypatch.setattr(graph._session, "get", fake_session_get)

    merged = graph.graph_get("/items")
    assert merged == {"value": [{"id": 1}, {"id": 2}]}
    assert len(pages) == 2
