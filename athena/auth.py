"""Firebase Authentication verification and session management."""

from datetime import datetime, timedelta, timezone
from functools import wraps
import math
import secrets
import threading
import time
from typing import Any, Callable, Optional

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


def _normalize_utc_dt(value: Any) -> Optional[datetime]:
    """Return a timezone-aware UTC datetime, or None when *value* is invalid."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _is_fresh_auth_time(auth_time: Any, now_ts: float, max_age_seconds: int) -> bool:
    """Return True when *auth_time* is a finite epoch within the freshness window."""
    if isinstance(auth_time, bool) or not isinstance(auth_time, (int, float)):
        return False
    ts = float(auth_time)
    if not math.isfinite(ts) or ts <= 0:
        return False
    return (now_ts - ts <= max_age_seconds) and (ts - now_ts <= 60)


def is_session_active() -> bool:
    """Return True if the current Flask session exists, is unexpired, and wasn't revoked."""
    user_id = session.get("user_id")
    expires_at = _normalize_utc_dt(session.get("expires_at"))
    login_time = _normalize_utc_dt(session.get("login_time"))
    sid = session.get("sid")
    iat_ns = session.get("iat_ns")

    if not user_id or expires_at is None:
        return False

    now = datetime.now(timezone.utc)
    if now >= expires_at:
        return False

    has_valid_iat_ns = isinstance(iat_ns, int) and not isinstance(iat_ns, bool)
    with _session_lock:
        _prune_revocations_locked(now)
        if sid and (sid, expires_at) in _revoked_sids:
            return False
        cutoff_ns = _min_session_iat_ns.get(user_id)
        if cutoff_ns is not None and has_valid_iat_ns and iat_ns <= cutoff_ns:
            return False
        if (
            not sid
            and not has_valid_iat_ns
            and login_time is not None
            and (user_id, login_time, expires_at) in _revoked_sessions
        ):
            return False

    return True


def revoke_current_session() -> None:
    """Invalidate the current session server-side so cookie replay fails after logout."""
    user_id = session.get("user_id")
    login_time = _normalize_utc_dt(session.get("login_time"))
    expires_at = _normalize_utc_dt(session.get("expires_at"))
    sid = session.get("sid")
    iat_ns = session.get("iat_ns")

    if not user_id or expires_at is None:
        return

    has_valid_iat_ns = isinstance(iat_ns, int) and not isinstance(iat_ns, bool)
    now = datetime.now(timezone.utc)
    now_ns = time.time_ns()
    with _session_lock:
        _prune_revocations_locked(now)
        if sid:
            _revoked_sids.add((sid, expires_at))
        if sid or has_valid_iat_ns:
            floor_ns = max(now_ns, iat_ns if has_valid_iat_ns else 0)
            existing_ns = _min_session_iat_ns.get(user_id, 0)
            if floor_ns > existing_ns:
                _min_session_iat_ns[user_id] = floor_ns
        elif login_time is not None:
            # Legacy pre-deploy session carrying login_time without sid/iat_ns.
            _revoked_sessions.add((user_id, login_time, expires_at))

    if current_app.config.get("ENV") == "production":
        try:
            firebase_auth.revoke_refresh_tokens(user_id)
        except Exception:
            # Best-effort server-side revocation; local session revocation above already stands.
            pass


_AUTH_SESSION_KEYS = (
    "user_id",
    "email",
    "sid",
    "iat_ns",
    "login_time",
    "expires_at",
)


def clear_inactive_auth_session() -> None:
    """Clear stale authentication fields while preserving pre-login CSRF state.

    When an authenticated session expires or is revoked, ``session.clear()``
    wipes all session state. When no authentication keys are present (an
    unauthenticated browser holding only ``session["csrf_token"]``), leaving
    the session untouched prevents a second tab hitting ``@login_required``
    or ``/auth/login`` from invalidating an already-open login tab's CSRF
    token.
    """
    if any(k in session for k in _AUTH_SESSION_KEYS):
        session.clear()


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
            clear_inactive_auth_session()
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

    if decoded.get("email_verified") is not True:
        log_auth_event("auth_failure", "failure", reason="email_unverified")
        return False, "Accès non autorisé."

    # Replay guard: only tokens from a recent interactive authentication may
    # establish a session (a captured ID token is otherwise valid ~1 h).
    now_ts = datetime.now(timezone.utc).timestamp()
    if not _is_fresh_auth_time(decoded.get("auth_time"), now_ts, _MAX_TOKEN_AGE_SECONDS):
        log_auth_event("auth_failure", "failure", reason="token_stale")
        return False, "Session expirée. Veuillez vous reconnecter."

    # MFA strictness check: if REQUIRE_MFA is enabled, ensure the token
    # was issued after MFA verification (contains a valid sign_in_second_factor).
    if current_app.config.get("REQUIRE_MFA", False):
        firebase_claim = decoded.get("firebase") or {}
        if not firebase_claim.get("sign_in_second_factor"):
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
