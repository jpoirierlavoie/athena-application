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
