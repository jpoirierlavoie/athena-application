"""Session-creation guards (portail client, spec L1 §1.2).

Pins the defense-in-depth refusal: a Firebase token carrying the custom claim
``portail: True`` (minted for a portal client account) must never establish a
session on the main service, independently of the email allowlist.
"""

import os
import sys
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

import auth as auth_module  # noqa: E402


def _app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["AUTHORIZED_USER_EMAIL"] = "test@example.com"
    app.config["REQUIRE_MFA"] = False
    return app


def _decoded(**over) -> dict:
    base = {
        "uid": "u1",
        "email": "test@example.com",
        "email_verified": True,
        "auth_time": datetime.now(timezone.utc).timestamp(),
        "firebase": {},
    }
    base.update(over)
    return base


def _attempt(decoded: dict) -> tuple[bool, str]:
    app = _app()
    with app.test_request_context("/auth/verify-token"):
        with mock.patch.object(
            auth_module.firebase_auth, "verify_id_token", return_value=decoded
        ):
            return auth_module.verify_and_create_session("token")


def test_portail_claim_is_refused():
    ok, message = _attempt(_decoded(portail=True))
    assert ok is False
    assert message == "Accès non autorisé."


def test_portail_claim_refused_even_with_authorized_email():
    # The claim guard must not depend on the allowlist: the authorized email
    # WITH the claim is still refused (a poisoned-claim juriste account must
    # fail closed, per §1.3's rationale for the self-invitation ban).
    ok, _ = _attempt(_decoded(email="test@example.com", portail=True))
    assert ok is False


def test_normal_token_still_creates_session():
    ok, message = _attempt(_decoded())
    assert ok is True
    assert message == ""


def test_absent_or_false_claim_passes():
    ok, _ = _attempt(_decoded(portail=False))
    assert ok is True


def test_replayed_session_cookie_is_refused_after_logout():
    from flask import Blueprint, redirect, session

    from routes.auth_routes import auth_bp

    app = _app()
    app.config["WTF_CSRF_ENABLED"] = False
    dash_bp = Blueprint("dashboard", __name__)

    @dash_bp.route("/")
    def index():
        return "dashboard"

    app.register_blueprint(auth_bp)
    app.register_blueprint(dash_bp)

    @app.route("/establish")
    def establish():
        with mock.patch.object(
            auth_module.firebase_auth, "verify_id_token", return_value=_decoded(uid="replay-u1")
        ):
            ok, msg = auth_module.verify_and_create_session("token")
            assert ok, msg
        return "established"

    @app.route("/protected")
    @auth_module.login_required
    def protected():
        return "secret-data"

    client = app.test_client()
    assert client.get("/establish").status_code == 200
    assert client.get("/protected").status_code == 200

    # Capture the signed Flask session cookie before logging out.
    captured_cookie = client.get_cookie("session")
    assert captured_cookie is not None

    with mock.patch("utils.logging_setup.log_auth_event"):
        resp = client.post("/auth/logout")
        assert resp.status_code == 302

    # Replay the captured cookie on a brand-new client: @login_required must
    # reject it instead of honouring the unexpired stateless cookie.
    attacker = app.test_client()
    attacker.set_cookie("session", captured_cookie.value)
    replayed = attacker.get("/protected")
    assert replayed.status_code == 302
    assert "/auth/login" in replayed.headers["Location"]

    # Signing in again mints a fresh login_time above the logout cutoff.
    assert client.get("/establish").status_code == 200
    assert client.get("/protected").status_code == 200


def test_unverified_email_is_refused():
    ok, message = _attempt(_decoded(email_verified=False))
    assert ok is False
    assert message == "Accès non autorisé."


def test_missing_email_verified_claim_is_refused():
    d = _decoded()
    del d["email_verified"]
    ok, message = _attempt(d)
    assert ok is False
    assert message == "Accès non autorisé."


def test_mfa_none_claim_is_refused():
    app = _app()
    app.config["REQUIRE_MFA"] = True
    with app.test_request_context("/auth/verify-token"):
        with mock.patch.object(
            auth_module.firebase_auth,
            "verify_id_token",
            return_value=_decoded(firebase=None),
        ):
            ok, msg = auth_module.verify_and_create_session("token")
            assert ok is False
            assert msg == "Vérification en deux étapes requise."


def test_mfa_missing_second_factor_is_refused():
    app = _app()
    app.config["REQUIRE_MFA"] = True
    with app.test_request_context("/auth/verify-token"):
        with mock.patch.object(
            auth_module.firebase_auth,
            "verify_id_token",
            return_value=_decoded(firebase={"sign_in_second_factor": None}),
        ):
            ok, msg = auth_module.verify_and_create_session("token")
            assert ok is False
            assert msg == "Vérification en deux étapes requise."


def test_mfa_valid_second_factor_passes():
    app = _app()
    app.config["REQUIRE_MFA"] = True
    with app.test_request_context("/auth/verify-token"):
        with mock.patch.object(
            auth_module.firebase_auth,
            "verify_id_token",
            return_value=_decoded(firebase={"sign_in_second_factor": "phone"}),
        ):
            ok, msg = auth_module.verify_and_create_session("token")
            assert ok is True
            assert msg == ""


def test_naive_expires_at_in_session_does_not_raise():
    app = _app()
    with app.test_request_context("/"):
        from flask import session
        session["user_id"] = "u1"
        session["expires_at"] = datetime(2030, 1, 1, 0, 0, 0)  # naive future
        assert auth_module.is_session_active() is True

        session["expires_at"] = datetime(2020, 1, 1, 0, 0, 0)  # naive past
        assert auth_module.is_session_active() is False

        session["expires_at"] = "invalid-type"
        assert auth_module.is_session_active() is False


@pytest.mark.parametrize("bad_auth_time", [
    float("nan"),
    float("inf"),
    "bad",
    True,
    None,
    0,
])
def test_malformed_or_non_finite_auth_time_is_refused(bad_auth_time):
    claims = _decoded()
    if bad_auth_time is None:
        claims.pop("auth_time", None)
    else:
        claims["auth_time"] = bad_auth_time
    ok, msg = _attempt(claims)
    assert ok is False
    assert msg == "Session expirée. Veuillez vous reconnecter."


def test_revoke_current_session_with_naive_expires_at_does_not_raise():
    app = _app()
    with app.test_request_context("/"):
        from flask import session
        session["user_id"] = "naive-revoke-u1"
        session["sid"] = "naive-sid"
        session["login_time"] = datetime(2026, 1, 1, 0, 0, 0)
        session["expires_at"] = datetime(2030, 1, 1, 0, 0, 0)
        auth_module.revoke_current_session()
        assert auth_module.is_session_active() is False

    # A fresh session lookup must also succeed without TypeError during pruning.
    with app.test_request_context("/"):
        from flask import session
        session["user_id"] = "other-u2"
        session["expires_at"] = datetime(2030, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert auth_module.is_session_active() is True


def test_is_appengine_internal_request_scoped_to_internal_paths():
    from security import is_appengine_internal_request
    app = _app()
    with app.test_request_context("/auth/login", headers={"X-AppEngine-QueueName": "default"}):
        assert is_appengine_internal_request() is False
    with app.test_request_context("/dossiers", headers={"X-Appengine-Cron": "true"}):
        assert is_appengine_internal_request() is False
    with app.test_request_context("/taches/portail/evenement", headers={"X-AppEngine-QueueName": "portail"}):
        assert is_appengine_internal_request() is True
    with app.test_request_context("/_ah/warmup", headers={"X-AppEngine-QueueName": "default"}):
        assert is_appengine_internal_request() is True

