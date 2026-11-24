"""Tests for Courrier Phase 1: Graph Courrier client and get_courrier_connexion."""

import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest import mock

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from config import Config
    import mcp.handlers as handlers
    from utils import graph_courrier


def _make_jwt(claims: dict) -> str:
    """Build an unsigned test JWT carrying the given payload claims."""
    def _b64(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{_b64({'alg': 'RS256', 'typ': 'JWT'})}.{_b64(claims)}.sig"


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, *, text: str = "", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_courrier_state(monkeypatch):
    graph_courrier._reset_token_cache_for_tests()
    graph_courrier.detect_execution_region.cache_clear()
    monkeypatch.setattr(Config, "GRAPH_TENANT_ID", "tenant-id")
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_ID", "courrier-client-id")
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_SECRET", "courrier-secret")
    monkeypatch.setattr(Config, "COURRIER_UPN", "juriste@cabinet.example")
    monkeypatch.setattr(Config, "COURRIER_ONEDRIVE_ROOT", "Dossiers")
    monkeypatch.setattr(Config, "COURRIER_ACTIF", False)
    yield
    graph_courrier._reset_token_cache_for_tests()
    graph_courrier.detect_execution_region.cache_clear()


# ── Token caching, JWT parsing & expiry ───────────────────────────────────


def test_jeton_courrier_caches_and_refreshes_after_margin(monkeypatch):
    exp_ts = int(datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc).timestamp())
    tok1 = _make_jwt({"roles": ["Files.ReadWrite.All", "Mail.ReadWrite"], "exp": exp_ts})
    tok2 = _make_jwt({"roles": ["Mail.ReadWrite"], "exp": exp_ts + 3600})

    calls = []
    clock = [1000.0]
    monkeypatch.setattr(graph_courrier.time, "monotonic", lambda: clock[0])

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        token = tok1 if len(calls) == 1 else tok2
        return _FakeResponse(200, {"access_token": token, "expires_in": 3600})

    monkeypatch.setattr(graph_courrier._session, "post", fake_post)

    info1 = graph_courrier.jeton_courrier()
    assert info1.access_token == tok1
    assert info1.roles == ("Files.ReadWrite.All", "Mail.ReadWrite")
    assert info1.expires_at_utc == datetime.fromtimestamp(exp_ts, tz=timezone.utc)
    assert len(calls) == 1

    # Within the 300 s safety margin (1000 + 3200 < 1000 + 3300): cached.
    clock[0] = 4200.0
    info_cached = graph_courrier.jeton_courrier()
    assert info_cached.access_token == tok1
    assert len(calls) == 1

    # Inside the 300 s safety margin (1000 + 3300): refreshes.
    clock[0] = 4300.0
    info2 = graph_courrier.jeton_courrier()
    assert info2.access_token == tok2
    assert info2.roles == ("Mail.ReadWrite",)
    assert len(calls) == 2


def test_jeton_courrier_opaque_token_falls_back_to_expires_in(monkeypatch):
    monkeypatch.setattr(
        graph_courrier._session,
        "post",
        lambda url, **kw: _FakeResponse(200, {"access_token": "opaque-tok", "expires_in": 1800}),
    )
    before = datetime.now(timezone.utc)
    info = graph_courrier.jeton_courrier()
    after = datetime.now(timezone.utc)

    assert info.access_token == "opaque-tok"
    assert info.roles == ()
    assert before + timedelta(seconds=1795) <= info.expires_at_utc <= after + timedelta(seconds=1805)


# ── Prefer headers, pagination & SSRF guard ───────────────────────────────


def test_courrier_get_merges_prefer_headers_and_paginates(monkeypatch):
    tok = _make_jwt({"roles": ["Mail.ReadWrite"]})
    monkeypatch.setattr(
        graph_courrier._session,
        "post",
        lambda url, **kw: _FakeResponse(200, {"access_token": tok, "expires_in": 3600}),
    )

    seen_calls = []

    def fake_get(url, **kw):
        seen_calls.append((url, kw))
        if len(seen_calls) == 1:
            return _FakeResponse(
                200,
                {
                    "value": [{"id": "m1"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/users/x/messages?$skip=1",
                },
            )
        return _FakeResponse(200, {"value": [{"id": "m2"}]})

    monkeypatch.setattr(graph_courrier._session, "get", fake_get)

    res = graph_courrier.courrier_get(
        "/users/juriste%40cabinet.example/messages",
        params={"$top": 1},
        immutable_id=True,
        prefer=('outlook.body-content-type="text"',),
    )
    assert [m["id"] for m in res["value"]] == ["m1", "m2"]
    assert "@odata.nextLink" not in res
    assert seen_calls[0][1]["headers"]["Prefer"] == (
        'IdType="ImmutableId", outlook.body-content-type="text"'
    )
    assert seen_calls[0][1]["params"] == {"$top": 1}
    assert "params" not in seen_calls[1][1]

    seen_calls.clear()
    graph_courrier.courrier_get("/users/x/drive/root", immutable_id=False)
    assert "Prefer" not in seen_calls[0][1]["headers"]


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://evil.example/v1.0/me",
        "http://graph.microsoft.com/v1.0/me",
        "https://graph.microsoft.com.evil.example/v1.0/me",
    ],
)
def test_courrier_get_rejects_non_graph_urls(bad_url):
    with pytest.raises(graph_courrier.CourrierGraphError, match="refusée"):
        graph_courrier.courrier_get(bad_url)


def test_courrier_get_rejects_poisoned_nextlink(monkeypatch):
    tok = _make_jwt({"roles": ["Mail.ReadWrite"]})
    monkeypatch.setattr(
        graph_courrier._session,
        "post",
        lambda url, **kw: _FakeResponse(200, {"access_token": tok, "expires_in": 3600}),
    )
    monkeypatch.setattr(
        graph_courrier._session,
        "get",
        lambda url, **kw: _FakeResponse(
            200,
            {
                "value": [{"id": "m1"}],
                "@odata.nextLink": "https://evil.example/steal-bearer",
            },
        ),
    )
    with pytest.raises(graph_courrier.CourrierGraphError, match="refusée"):
        graph_courrier.courrier_get("/users/x/messages")


# ── Retries, Retry-After clamp & zero-PII errors ──────────────────────────


@pytest.mark.parametrize("status_code", [429, 503, 504])
def test_retries_on_transient_status_honouring_clamped_retry_after(monkeypatch, status_code):
    tok = _make_jwt({"roles": ["Mail.ReadWrite"]})
    monkeypatch.setattr(
        graph_courrier._session,
        "post",
        lambda url, **kw: _FakeResponse(200, {"access_token": tok, "expires_in": 3600}),
    )
    sleeps = []
    monkeypatch.setattr(graph_courrier, "_sleep", sleeps.append)

    http_date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60))
    responses = [
        _FakeResponse(status_code, headers={"Retry-After": "120"}),
        _FakeResponse(status_code, headers={"Retry-After": http_date}),
        _FakeResponse(200, {"id": "inbox-id"}),
    ]
    monkeypatch.setattr(graph_courrier._session, "get", lambda url, **kw: responses.pop(0))

    res = graph_courrier.courrier_get("/users/x/mailFolders/Inbox")
    assert res == {"id": "inbox-id"}
    assert len(sleeps) == 2
    assert sleeps[0] == 15.0
    assert 13.0 <= sleeps[1] <= 15.0


@pytest.mark.parametrize("status_code", [401, 403, 404])
def test_no_retry_on_client_or_auth_errors_and_no_pii_leak(monkeypatch, status_code):
    tok = _make_jwt({"roles": ["Mail.ReadWrite"]})
    monkeypatch.setattr(
        graph_courrier._session,
        "post",
        lambda url, **kw: _FakeResponse(200, {"access_token": tok, "expires_in": 3600}),
    )
    sleeps = []
    monkeypatch.setattr(graph_courrier, "_sleep", sleeps.append)

    attempts = []

    def fake_get(url, **kw):
        attempts.append(url)
        return _FakeResponse(
            status_code,
            text="Sensitive body juriste@cabinet.example /Dossiers/Client SecretBody",
        )

    monkeypatch.setattr(graph_courrier._session, "get", fake_get)

    with pytest.raises(graph_courrier.CourrierGraphError) as exc_info:
        graph_courrier.courrier_get("/users/juriste%40cabinet.example/drive/root:/Dossiers")

    assert len(attempts) == 1
    assert sleeps == []
    assert exc_info.value.status_code == status_code
    msg = str(exc_info.value)
    assert f"HTTP {status_code}" in msg
    assert "juriste" not in msg
    assert "Dossiers" not in msg
    assert "SecretBody" not in msg
    assert tok not in msg


# ── Region detection ──────────────────────────────────────────────────────


def test_detect_execution_region_local_gae_and_error(monkeypatch):
    monkeypatch.delenv("GAE_ENV", raising=False)
    graph_courrier.detect_execution_region.cache_clear()
    assert graph_courrier.detect_execution_region() == "local"

    monkeypatch.setenv("GAE_ENV", "standard")
    graph_courrier.detect_execution_region.cache_clear()
    monkeypatch.setattr(
        graph_courrier._session,
        "get",
        lambda url, **kw: _FakeResponse(200, text="projects/12345/regions/northamerica-northeast1"),
    )
    assert graph_courrier.detect_execution_region() == "northamerica-northeast1"

    graph_courrier.detect_execution_region.cache_clear()

    def raise_timeout(url, **kw):
        raise requests.Timeout("timed out")

    monkeypatch.setattr(graph_courrier._session, "get", raise_timeout)
    assert graph_courrier.detect_execution_region() == "unknown"


# ── verifier_connexion & MCP handler ──────────────────────────────────────


def test_verifier_connexion_unconfigured(monkeypatch):
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_ID", "")
    res = handlers.get_courrier_connexion({})
    assert res["configure"] is False
    assert res["interrupteur_actif"] is False
    assert res["jeton_valide"] is False
    assert res["expiration"] is None
    assert res["reconsentement_requis"] is False
    assert res["boite_accessible"] is False
    assert res["onedrive_accessible"] is False
    assert res["erreur"] == "Le module Courrier n'est pas configuré."
    assert res["pret"] is False
    assert "Module Courrier non configuré." in res["motifs"]
    assert "Interrupteur COURRIER_ACTIF désactivé." in res["motifs"]


def test_verifier_connexion_healthy_montreal_expiry_and_switch(monkeypatch):
    exp_utc = datetime(2026, 11, 20, 19, 30, 0, tzinfo=timezone.utc)
    tok = _make_jwt(
        {"roles": ["Files.ReadWrite.All", "Mail.ReadWrite"], "exp": int(exp_utc.timestamp())}
    )
    monkeypatch.setattr(Config, "COURRIER_ACTIF", True)
    monkeypatch.setattr(
        graph_courrier._session,
        "post",
        lambda url, **kw: _FakeResponse(200, {"access_token": tok, "expires_in": 3600}),
    )

    def fake_get(url, **kw):
        if "/mailFolders/Inbox" in url:
            return _FakeResponse(200, {"id": "inbox-id", "totalItemCount": 3})
        if "/drive/root:/" in url:
            return _FakeResponse(200, {"id": "root-id", "folder": {"childCount": 2}})
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(graph_courrier._session, "get", fake_get)

    res = handlers.get_courrier_connexion({})
    assert res["interrupteur_actif"] is True
    assert res["configure"] is True
    assert res["mode_auth"] == "application"
    assert res["jeton_valide"] is True
    assert res["expiration"] == "2026-11-20T14:30:00-05:00"
    assert res["reconsentement_requis"] is False
    assert res["portees"] == ["Files.ReadWrite.All", "Mail.ReadWrite"]
    assert res["envoi_absent"] is True
    assert res["boite_accessible"] is True
    assert res["onedrive_accessible"] is True
    assert res["erreur"] is None
    assert res["pret"] is True
    assert res["motifs"] == []

    # Flip COURRIER_ACTIF off: probes succeed, yet pret is False with clear motif.
    monkeypatch.setattr(Config, "COURRIER_ACTIF", False)
    res_off = handlers.get_courrier_connexion({})
    assert res_off["pret"] is False
    assert res_off["motifs"] == ["Interrupteur COURRIER_ACTIF désactivé."]


def test_verifier_connexion_token_401_flags_reconsent(monkeypatch):
    monkeypatch.setattr(Config, "COURRIER_ACTIF", True)
    monkeypatch.setattr(
        graph_courrier._session,
        "post",
        lambda url, **kw: _FakeResponse(401, text="invalid_client"),
    )
    res = handlers.get_courrier_connexion({})
    assert res["jeton_valide"] is False
    assert res["reconsentement_requis"] is True
    assert res["expiration"] is None
    assert "HTTP 401" in res["erreur"]
    assert res["pret"] is False
    assert res["motifs"] == ["Reconsentement ou rotation de secret Microsoft requis."]


def test_verifier_connexion_refuses_mail_send_and_skips_probes(monkeypatch):
    tok = _make_jwt({"roles": ["Files.ReadWrite.All", "Mail.ReadWrite", "Mail.Send"]})
    monkeypatch.setattr(Config, "COURRIER_ACTIF", True)
    monkeypatch.setattr(
        graph_courrier._session,
        "post",
        lambda url, **kw: _FakeResponse(200, {"access_token": tok, "expires_in": 3600}),
    )

    def forbid_get(url, **kw):
        raise AssertionError("probes must not run when Mail.Send is present")

    monkeypatch.setattr(graph_courrier._session, "get", forbid_get)

    res = handlers.get_courrier_connexion({})
    assert res["jeton_valide"] is True
    assert res["envoi_absent"] is False
    assert res["boite_accessible"] is False
    assert res["onedrive_accessible"] is False
    assert "Mail.Send" in res["erreur"]
    assert res["pret"] is False
    assert res["motifs"] == ["Permission interdite Mail.Send présente sur le jeton."]


def test_verifier_connexion_inbox_403_and_onedrive_ok(monkeypatch):
    tok = _make_jwt({"roles": ["Files.ReadWrite.All", "Mail.ReadWrite"]})
    monkeypatch.setattr(Config, "COURRIER_ACTIF", True)
    monkeypatch.setattr(
        graph_courrier._session,
        "post",
        lambda url, **kw: _FakeResponse(200, {"access_token": tok, "expires_in": 3600}),
    )

    def fake_get(url, **kw):
        if "/mailFolders/Inbox" in url:
            return _FakeResponse(403)
        return _FakeResponse(200, {"id": "root-id", "folder": {"childCount": 0}})

    monkeypatch.setattr(graph_courrier._session, "get", fake_get)

    res = handlers.get_courrier_connexion({})
    assert res["jeton_valide"] is True
    assert res["boite_accessible"] is False
    assert res["onedrive_accessible"] is True
    assert res["reconsentement_requis"] is True
    assert "Boîte de réception" in res["erreur"]
    assert res["pret"] is False
    assert "Reconsentement ou rotation de secret Microsoft requis." in res["motifs"]
    assert "Boîte de réception Outlook inaccessible." in res["motifs"]


def test_verifier_connexion_onedrive_404_and_non_folder(monkeypatch):
    tok = _make_jwt({"roles": ["Files.ReadWrite.All", "Mail.ReadWrite"]})
    monkeypatch.setattr(Config, "COURRIER_ACTIF", True)
    monkeypatch.setattr(
        graph_courrier._session,
        "post",
        lambda url, **kw: _FakeResponse(200, {"access_token": tok, "expires_in": 3600}),
    )

    def fake_get_404(url, **kw):
        if "/mailFolders/Inbox" in url:
            return _FakeResponse(200, {"id": "inbox-id"})
        return _FakeResponse(404)

    monkeypatch.setattr(graph_courrier._session, "get", fake_get_404)

    res = handlers.get_courrier_connexion({})
    assert res["boite_accessible"] is True
    assert res["onedrive_accessible"] is False
    assert res["reconsentement_requis"] is False
    assert "OneDrive" in res["erreur"]
    assert res["pret"] is False
    assert res["motifs"] == ["Dossier racine OneDrive inaccessible."]

    def fake_get_file(url, **kw):
        if "/mailFolders/Inbox" in url:
            return _FakeResponse(200, {"id": "inbox-id"})
        return _FakeResponse(200, {"id": "file-id", "file": {}})

    monkeypatch.setattr(graph_courrier._session, "get", fake_get_file)
    res2 = handlers.get_courrier_connexion({})
    assert res2["boite_accessible"] is True
    assert res2["onedrive_accessible"] is False
    assert "pas un dossier" in res2["erreur"]
    assert res2["pret"] is False
    assert res2["motifs"] == ["Dossier racine OneDrive inaccessible."]
