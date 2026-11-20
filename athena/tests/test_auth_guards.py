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


def _login_app_with_csrf() -> Flask:
    from flask import Blueprint

    from routes.auth_routes import auth_bp
    from security import init_security
    from utils.icons import ms as _ms

    athena_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = Flask(__name__, template_folder=os.path.join(athena_dir, "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["AUTHORIZED_USER_EMAIL"] = "test@example.com"
    app.config["REQUIRE_MFA"] = False
    app.config["RATELIMIT_ENABLED"] = False
    app.config["WTF_CSRF_ENABLED"] = True
    app.config["RECAPTCHA_ENTERPRISE_SITE_KEY"] = ""
    app.config["CF_ORIGIN_SECRET"] = ""
    init_security(app)
    app.jinja_env.globals["ms"] = _ms
    app.jinja_env.globals["firebase_api_key"] = "clef-test"
    app.jinja_env.globals["firebase_project_id"] = "test-project"
    app.jinja_env.globals["firebase_app_id"] = "1:2:web:3"

    dash_bp = Blueprint("dashboard", __name__)

    @dash_bp.route("/")
    @auth_module.login_required
    def index():
        return "dashboard"

    app.register_blueprint(auth_bp)
    app.register_blueprint(dash_bp)
    return app


def _extract_csrf_meta(html: str) -> str:
    import re

    m = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
    assert m is not None, "csrf-token meta missing from login page"
    return m.group(1)


def _extract_js_function(html: str, signature: str) -> str:
    """Return the brace-delimited body of *signature* with line comments stripped."""
    rest = html[html.index(signature):]
    rest = "\n".join(line.split("//")[0] for line in rest.split("\n"))
    start = rest.index("{")
    depth = 0
    for idx in range(start, len(rest)):
        if rest[idx] == "{":
            depth += 1
        elif rest[idx] == "}":
            depth -= 1
            if depth == 0:
                return rest[start:idx + 1]
    raise AssertionError(f"Unclosed brace after {signature}")


def test_second_tab_login_redirect_preserves_first_tab_csrf_token():
    app = _login_app_with_csrf()
    client = app.test_client()

    # Tab 1 hits protected index -> redirected to /auth/login, receives CSRF token.
    r1 = client.get("/")
    assert r1.status_code == 302
    assert "/auth/login" in r1.headers["Location"]
    page1 = client.get("/auth/login?next=/")
    assert page1.status_code == 200
    token_tab1 = _extract_csrf_meta(page1.get_data(as_text=True))

    # Tab 2 hits protected index -> redirected to /auth/login while still unauthenticated.
    r2 = client.get("/")
    assert r2.status_code == 302
    assert "/auth/login" in r2.headers["Location"]
    page2 = client.get("/auth/login?next=/")
    assert page2.status_code == 200
    _extract_csrf_meta(page2.get_data(as_text=True))

    # Tab 1 submits its original CSRF token and must still succeed.
    with mock.patch.object(
        auth_module.firebase_auth, "verify_id_token", return_value=_decoded()
    ):
        resp = client.post(
            "/auth/verify-token",
            headers={"X-CSRFToken": token_tab1},
            data={"id_token": "valid-token"},
        )
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "redirect": "/"}


def test_expired_authenticated_session_still_clears_auth_keys_on_redirect():
    app = _login_app_with_csrf()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u1"
        sess["sid"] = "old-sid"
        sess["expires_at"] = datetime(2020, 1, 1, tzinfo=timezone.utc)

    resp = client.get("/")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]
    with client.session_transaction() as sess:
        assert "user_id" not in sess
        assert "sid" not in sess
        assert "expires_at" not in sess


def test_real_app_verify_token_csrf_failure_returns_json_and_logs():
    import textwrap

    from tests._real_app_probe import run_probe

    probe = textwrap.dedent("""
        import json, logging
        import main

        captured = []

        class _Capture(logging.Handler):
            def emit(self, record):
                captured.append({
                    "level": record.levelname,
                    "fields": dict(getattr(record, "json_fields", {}) or {}),
                })

        logger = logging.getLogger("pallas.security")
        logger.addHandler(_Capture(level=logging.DEBUG))
        logger.setLevel(logging.DEBUG)

        client = main.app.test_client()
        captured.clear()
        vt = client.post("/auth/verify-token", data={"id_token": "x"})
        vt_logs = list(captured)

        captured.clear()
        lo = client.post("/auth/logout")
        lo_logs = list(captured)

        out = {
            "verify_token": {
                "status": vt.status_code,
                "is_json": vt.is_json,
                "json": vt.get_json(silent=True),
                "logs": vt_logs,
            },
            "logout": {
                "status": lo.status_code,
                "is_json": lo.is_json,
                "content_type": lo.content_type,
                "logs": lo_logs,
            },
        }
        print("@@CSRFJSON@@" + json.dumps(out))
    """)
    env = {**os.environ}
    for name in ("ENV", "CF_ORIGIN_SECRET", "RECAPTCHA_ENTERPRISE_SITE_KEY"):
        env.pop(name, None)
    env.update({
        "SECRET_KEY": "test-secret",
        "FIREBASE_PROJECT_ID": "test-project",
        "FIREBASE_STORAGE_BUCKET": "test-bucket",
        "AUTHORIZED_USER_EMAIL": "test@example.com",
    })
    res = run_probe(probe, "@@CSRFJSON@@", env=env, timeout=180)

    vt = res["verify_token"]
    assert vt["status"] == 400
    assert vt["is_json"] is True
    assert vt["json"] == {
        "ok": False,
        "csrf_expired": True,
        "error": "La page de connexion a expiré. Actualisation…",
    }
    assert len(vt["logs"]) == 1
    assert vt["logs"][0]["level"] == "WARNING"
    assert vt["logs"][0]["fields"] == {
        "event": "csrf_failure",
        "path": "/auth/verify-token",
    }

    lo = res["logout"]
    assert lo["status"] == 400
    assert lo["is_json"] is False
    assert lo["content_type"].startswith("text/html")
    assert len(lo["logs"]) == 1
    assert lo["logs"][0]["level"] == "WARNING"
    assert lo["logs"][0]["fields"] == {
        "event": "csrf_failure",
        "path": "/auth/logout",
    }


def test_login_template_isolates_post_id_token_from_mfa_catch():
    app = _login_app_with_csrf()
    resp = app.test_client().get("/auth/login")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    verify_body = _extract_js_function(html, "async verifyMfaCode()")
    assert verify_body.index("this.postIdToken") > verify_body.index(
        "Erreur de vérification. Réessayez."
    )

    submit_body = _extract_js_function(html, "async submit()")
    assert submit_body.index("this.postIdToken") > submit_body.index(
        "Erreur de connexion. Réessayez."
    )

    post_body = _extract_js_function(html, "async postIdToken(user)")
    assert "csrf_expired" in post_body
    assert "window.location.reload()" in post_body


