"""Firebase Authentication verification and session management."""

from datetime import datetime, timedelta, timezone
from functools import wraps
import secrets
import threading
import time
from typing import Any, Callable

from flask import (
    current_app,
    redirect,
    request,
    session,
    url_for,
)
from firebase_admin import auth as firebase_auth

# Server-side logout revocation (bounded in-process memory). Flask's default
# cookie session is stateless, so without server-side tracking a captured cookie
# would replay until expires_at even after POST /auth/logout.
_session_lock = threading.Lock()
_min_session_iat_ns: dict[str, int] = {}
_revoked_sids: set[tuple[str, datetime]] = set()
_revoked_sessions: set[tuple[str, datetime, datetime]] = set()


def _prune_revocations_locked(now: datetime) -> None:
    for entry in [e for e in _revoked_sids if e[1] <= now]:
        _revoked_sids.discard(entry)
    for entry in [e for e in _revoked_sessions if e[2] <= now]:
        _revoked_sessions.discard(entry)


def is_session_active() -> bool:
    """Return True if the current Flask session exists, is unexpired, and wasn't revoked."""
    user_id = session.get("user_id")
    expires_at = session.get("expires_at")
    login_time = session.get("login_time")
    sid = session.get("sid")
    iat_ns = session.get("iat_ns")

    if not user_id or not expires_at:
        return False

    now = datetime.now(timezone.utc)
    if now >= expires_at:
        return False

    with _session_lock:
        _prune_revocations_locked(now)
        if sid and (sid, expires_at) in _revoked_sids:
            return False
        cutoff_ns = _min_session_iat_ns.get(user_id)
        if cutoff_ns is not None and isinstance(iat_ns, int) and iat_ns <= cutoff_ns:
            return False
        if (
            not sid
            and not isinstance(iat_ns, int)
            and isinstance(login_time, datetime)
            and (user_id, login_time, expires_at) in _revoked_sessions
        ):
            return False

    return True


def revoke_current_session() -> None:
    """Invalidate the current session server-side so cookie replay fails after logout."""
    user_id = session.get("user_id")
    login_time = session.get("login_time")
    expires_at = session.get("expires_at")
    sid = session.get("sid")
    iat_ns = session.get("iat_ns")

    if not user_id or not expires_at:
        return

    now = datetime.now(timezone.utc)
    now_ns = time.time_ns()
    with _session_lock:
        _prune_revocations_locked(now)
        if sid:
            _revoked_sids.add((sid, expires_at))
        if sid or isinstance(iat_ns, int):
            floor_ns = max(now_ns, iat_ns if isinstance(iat_ns, int) else 0)
            existing_ns = _min_session_iat_ns.get(user_id, 0)
            if floor_ns > existing_ns:
                _min_session_iat_ns[user_id] = floor_ns
        elif isinstance(login_time, datetime):
            # Legacy pre-deploy session carrying login_time without sid/iat_ns.
            _revoked_sessions.add((user_id, login_time, expires_at))

    if current_app.config.get("ENV") == "production":
        try:
            firebase_auth.revoke_refresh_tokens(user_id)
        except Exception:
            pass


def login_required(f: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that ensures the request has a valid, non-expired session.

    Redirects to login with a ``next`` query parameter so the user is
    returned to the originally-requested page after authenticating.
    """

    @wraps(f)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        # Preserve the query string so parameterized deep links (filtered
        # lists, /oauth/authorize?client_id=...) survive the login
        # round-trip; security.safe_internal_redirect re-validates the value
        # on the way back (routes/auth_routes.verify_token).
        return_to = request.full_path if request.query_string else request.path

        if not is_session_active():
            session.clear()
            return redirect(url_for("auth.login", next=return_to))

        return f(*args, **kwargs)

    return decorated


# A token must have been minted by a *fresh* interactive sign-in to create a
# session; older (but still unexpired) ID tokens are refused to limit replay.
_MAX_TOKEN_AGE_SECONDS = 10 * 60


def verify_and_create_session(id_token: str) -> tuple[bool, str]:
    """Verify a Firebase ID token and establish a server-side session.

    Returns a tuple of (success: bool, error_message: str).
    On success error_message is empty.
    """
    from utils.logging_setup import log_auth_event

    try:
        # check_revoked: reject tokens minted before a revocation/disable
        # event so a console-side compromise response takes effect.
        decoded = firebase_auth.verify_id_token(
            id_token, check_revoked=True, clock_skew_seconds=10
        )
    except Exception as exc:
        # Never log the token or any claim — only the failure class.
        log_auth_event(
            "auth_failure", "failure",
            reason="token_invalid", error_type=type(exc).__name__,
        )
        return False, "Jeton invalide."

    # Defense in depth (portail client, spec L1 §1.2): accounts minted by the
    # client portal carry the custom claim ``portail: True`` and must never
    # open a session on the main service, even if the allowlist below were
    # ever misconfigured. Refused before the email check so the two guards
    # stay independent.
    if decoded.get("portail") is True:
        log_auth_event("auth_failure", "failure", reason="portail_claim")
        return False, "Accès non autorisé."

    email = decoded.get("email", "")
    authorized_email = current_app.config["AUTHORIZED_USER_EMAIL"]

    if email.lower() != authorized_email.lower():
        # The attempted email is attacker-controlled PII — do not log it.
        log_auth_event("auth_failure", "failure", reason="unauthorized_email")
        return False, "Accès non autorisé."

    # Replay guard: only tokens from a recent interactive authentication may
    # establish a session (a captured ID token is otherwise valid ~1 h).
    auth_time = decoded.get("auth_time", 0)
    now_ts = datetime.now(timezone.utc).timestamp()
    if not auth_time or now_ts - float(auth_time) > _MAX_TOKEN_AGE_SECONDS:
        log_auth_event("auth_failure", "failure", reason="token_stale")
        return False, "Session expirée. Veuillez vous reconnecter."

    # MFA strictness check: if REQUIRE_MFA is enabled, ensure the token
    # was issued after MFA verification (contains sign_in_second_factor).
    if current_app.config.get("REQUIRE_MFA", False):
        firebase_claim = decoded.get("firebase", {})
        if "sign_in_second_factor" not in firebase_claim:
            log_auth_event("auth_failure", "failure", reason="mfa_missing")
            return False, "Vérification en deux étapes requise."

    lifetime = current_app.config.get("SESSION_LIFETIME_HOURS", 12)
    now = datetime.now(timezone.utc)
    uid = decoded["uid"]
    iat_ns = time.time_ns()
    with _session_lock:
        cutoff_ns = _min_session_iat_ns.get(uid)
        if cutoff_ns is not None and iat_ns <= cutoff_ns:
            iat_ns = cutoff_ns + 1

    session.permanent = True
    session["user_id"] = uid
    session["email"] = email
    session["sid"] = secrets.token_urlsafe(16)
    session["iat_ns"] = iat_ns
    session["login_time"] = now
    session["expires_at"] = now + timedelta(hours=lifetime)

    log_auth_event("login", "success")
    return True, ""
