"""Flask application factory and WSGI entrypoint for Pallas Athena."""

import os
import sys
import logging
from datetime import timedelta

# Ensure the athena/ directory is on sys.path so imports work both locally
# (where Python may resolve the parent package) and on App Engine (where
# athena/ is the root at /srv/).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Bootstrap minimal logging so anything emitted before ``init_logging``
# attaches the proper handler chain still surfaces somewhere.  The real
# configuration (Cloud Logging in production, stderr locally, plus the
# context + redaction filters) is set up by ``utils.logging_setup.init_app``
# inside ``create_app``.
logging.basicConfig(level=logging.WARNING)

import firebase_admin
from firebase_admin import credentials
from flask import Flask, abort, render_template as _render_template, request

from config import Config
from security import init_security
from utils.logging_setup import init_app as init_logging, log_unexpected
from utils.tracing_setup import init_app as init_tracing


def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__)

    # ── Configuration ────────────────────────────────────────────────
    app.config.from_object(Config)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = Config.ENV == "production"
    # Jetons CSRF valides pour la SESSION, pas 1 h (défaut flask-wtf) : la
    # finalisation d'un lot de téléversements directs-à-GCS (≤ 200 Mo par
    # fichier, séquentiels) peut arriver plus d'une heure après le rendu du
    # formulaire — même décision que le portail (client/app.py, 2026-08-12).
    app.config["WTF_CSRF_TIME_LIMIT"] = None
    app.permanent_session_lifetime = timedelta(
        hours=Config.SESSION_LIFETIME_HOURS,
    )

    # ── Tracing (Cloud Trace + auto-instrumentation) ────────────────
    # Tracing must run before logging so the OTel Flask middleware wraps
    # the WSGI app first; this guarantees an active span exists by the
    # time logging's ``before_request`` reads its trace context.
    init_tracing(app)

    # ── Logging (Cloud Logging + context/redaction filters) ─────────
    init_logging(app)

    # ── Firebase Admin SDK ───────────────────────────────────────────
    if not firebase_admin._apps:
        # Uses GOOGLE_APPLICATION_CREDENTIALS env var or ADC on App Engine
        firebase_admin.initialize_app(
            credentials.ApplicationDefault(),
            {"storageBucket": Config.FIREBASE_STORAGE_BUCKET},
        )

    # ── Security (headers, CSRF, rate limiter) ───────────────────────
    init_security(app)

    # ── Jinja2 custom filters ───────────────────────────────────────────
    from tz import to_mtl
    app.jinja_env.filters["to_mtl"] = to_mtl

    from utils.validators import format_phone_display
    app.jinja_env.filters["phone"] = format_phone_display

    from utils.format_fr import format_cents_fr

    def _cents_fr(cents) -> str:
        """Integer cents → fr-CA amount ("85 000,00 $"); "" when unset."""
        return format_cents_fr(cents) if cents is not None else ""

    app.jinja_env.filters["cents_fr"] = _cents_fr

    # A value as a JS string literal inside a double-quoted HTML attribute
    # (the Alpine x-data blocks) — utils/html_attr.py, pure, so a test can
    # register the real filter rather than a copy.
    from utils.html_attr import jsattr
    app.jinja_env.filters["jsattr"] = jsattr

    # The whole pipeline lives in utils/markdown_docx.py — the SAME
    # extensions/allowlist (incl. use_align_attribute=True, which restores
    # table alignment on screen) drive the .docx conversion of a note's
    # body, so screen and paper can never drift.
    from utils.markdown_docx import markdown_to_safe_html

    app.jinja_env.filters["markdown"] = markdown_to_safe_html

    # Material Symbols (icônes en ligatures) — global plutôt que macro Jinja :
    # ~65 gabarits appelants, et utils/icons.py est aussi le lieu de la liste
    # canonique du sous-ensemble vendu (validation + gouvernance + émission
    # au même endroit ; tests/test_icons.py épingle l'accord).
    from utils.icons import ms
    app.jinja_env.globals["ms"] = ms

    # ── Blueprints ───────────────────────────────────────────────────
    from routes.auth_routes import auth_bp
    from routes.dashboard import dashboard_bp
    from routes.parties import parties_bp
    from routes.dossiers import dossiers_bp
    from routes.time_expenses import time_expenses_bp
    from routes.invoices import invoices_bp
    from routes.hearings import hearings_bp
    from routes.tasks import tasks_bp
    from routes.protocols import protocols_bp
    from routes.documents import documents_bp
    from routes.notes import notes_bp
    from routes.doc_templates import doc_templates_bp, modeles_bp
    from routes.trust import trust_bp
    from routes.budgets import budgets_bp
    from routes.admin_ledger import admin_bp
    from routes.comptabilite import comptabilite_bp
    from routes.journal_radiations import journal_radiations_bp
    from routes.financial_statements import etats_anciens_bp, etats_bp
    from routes.settings import settings_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(parties_bp)
    app.register_blueprint(dossiers_bp)
    app.register_blueprint(time_expenses_bp)
    app.register_blueprint(invoices_bp)
    app.register_blueprint(hearings_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(protocols_bp)
    app.register_blueprint(documents_bp)
    app.register_blueprint(notes_bp)
    app.register_blueprint(doc_templates_bp)
    # Bibliothèque → Modèles (2026-11-11): the list of the templates of
    # kind « modele », GET only, under @login_required — a browser
    # blueprint, never CSRF-exempt, under no special-cased prefix.
    app.register_blueprint(modeles_bp)
    app.register_blueprint(trust_bp)
    app.register_blueprint(budgets_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(comptabilite_bp)
    # Journal des radiations et intérêts (lot 3c) : GET seulement, en lecture
    # seule, sous @login_required — un blueprint de navigateur, jamais
    # exempté de CSRF, sous /comptabilite/radiations/ (aucun préfixe exempté).
    app.register_blueprint(journal_radiations_bp)
    # États financiers (lot B) : GET seulement, en lecture seule, sous
    # @login_required — un blueprint de navigateur, jamais exempté de CSRF.
    # Au menu « Finances » (/finances/) depuis le 2026-11-10 ; l'ancienne
    # adresse /comptabilite/etats/ ne fait plus que rediriger.
    app.register_blueprint(etats_bp)
    app.register_blueprint(etats_anciens_bp)
    # « Paramètres » : profil du cabinet + sécurité du compte. Blueprint
    # à LUI, jamais exempté de CSRF (ses POST viennent du navigateur), et
    # sous aucun préfixe exempté d'App Check — contrairement à /auth/.
    app.register_blueprint(settings_bp)

    # ── DAV blueprints (CardDAV, CalDAV, RFC-5545, per-dossier) ─────────
    from dav import dav_bp
    from dav.carddav import carddav_bp
    from dav.dossier_collections import dossier_dav_bp

    app.register_blueprint(dav_bp)
    app.register_blueprint(carddav_bp)
    app.register_blueprint(dossier_dav_bp)

    # Exempt all DAV endpoints from CSRF protection (they use HTTP Basic Auth)
    from security import csrf
    csrf.exempt(dav_bp)
    csrf.exempt(carddav_bp)
    csrf.exempt(dossier_dav_bp)

    # ── Portail client (spec L1): Réception + gestionnaire de tâches ────
    from routes.reception import compteur_reception, reception_bp
    app.register_blueprint(reception_bp)

    # Nav badge: invitations awaiting review. Cached 60 s in-process and
    # fail-open (None → no badge) so a missing « portail » database can
    # never break a page render.
    @app.context_processor
    def inject_reception_badge() -> dict:
        try:
            return {"reception_count": compteur_reception()}
        except Exception:
            return {"reception_count": None}

    from routes.taches_portail import taches_portail_bp
    app.register_blueprint(taches_portail_bp)
    # Machine blueprint: reached only by App Engine-internal dispatches
    # (the X-AppEngine-* header guards inside the views are the proof of
    # origin). Its OWN blueprint — never widen a browser blueprint's
    # exemption (Security Rules).
    csrf.exempt(taches_portail_bp)

    # ── Bookings sync (spec L2): cron-driven « Bookings with me » import ──
    from routes.taches_bookings import taches_bookings_bp
    app.register_blueprint(taches_bookings_bp)
    # Machine blueprint (GET /taches/bookings/sync), guarded by
    # X-Appengine-Cron. CSRF does not apply to GET, but exempting keeps it in
    # its own machine blueprint like taches_portail (never a browser one).
    csrf.exempt(taches_bookings_bp)

    # ── Miroir Outlook : cron-driven push of confirmed hearings to the
    # juriste's Outlook calendar (Graph). Same machine-blueprint discipline.
    from routes.taches_outlook import taches_outlook_bp
    app.register_blueprint(taches_outlook_bp)
    csrf.exempt(taches_outlook_bp)

    # ── Factures échues (2026-11-11): the daily lateness sweep, GET
    # /taches/factures/retards, guarded by X-Appengine-Cron and switched by
    # FACTURES_RETARD_ACTIF. Same machine-blueprint discipline: its OWN
    # blueprint, never a browser one widened.
    from routes.taches_factures import taches_factures_bp
    app.register_blueprint(taches_factures_bp)
    csrf.exempt(taches_factures_bp)

    # ── MCP connector (Phase I): /mcp endpoint + embedded OAuth 2.1 AS ──
    from mcp import mcp_bp, register_mcp
    register_mcp(app)
    # /mcp uses Bearer tokens, never a browser session. The machine-facing
    # OAuth views (/oauth/register, /oauth/token, /oauth/revoke) are
    # exempted individually in mcp/oauth.py; the /oauth/authorize POST —
    # the one browser-origin form in the flow — keeps CSRF enforcement.
    csrf.exempt(mcp_bp)

    # ── Context processor (Firebase config for all templates) ──────────
    @app.context_processor
    def inject_firebase_config() -> dict[str, str]:
        return {
            "firebase_project_id": app.config["FIREBASE_PROJECT_ID"],
            "firebase_api_key": app.config.get("FIREBASE_API_KEY", ""),
            "firebase_app_id": app.config.get("FIREBASE_APP_ID", ""),
        }

    # ── Context processor (display theme: <html data-theme>, Rule 10) ──
    # Reads the per-device theme cookie only — never Firestore, never raises.
    from routes._helpers import theme_context
    app.context_processor(theme_context)

    # ── Block direct appspot.com access (traffic must come via Cloudflare) ──
    @app.before_request
    def block_appspot() -> None:
        # App Engine internal requests (warmup, cron) arrive on the appspot
        # host without Cloudflare headers — they must not be blocked.
        if request.path.startswith("/_ah/"):
            return None
        # Cloud Tasks / cron dispatches (portail L1) also hit the appspot
        # host; their X-AppEngine-* headers are stripped from all external
        # traffic, so presence proves internal origin (security.py).
        from security import is_appengine_internal_request
        if is_appengine_internal_request():
            return None
        host = request.host.split(":", 1)[0].lower().rstrip(".")
        if host == "appspot.com" or host.endswith(".appspot.com"):
            # Refused, and SAID, since 2026-11-08 (Change Impact 4): the
            # event was registered with the registry and never emitted. The
            # event's own fields are ids only — never the host, the path or a
            # header a client chose; like every line logged inside a request,
            # the entry still carries `route` and, in production,
            # httpRequest.requestUrl and httpRequest.userAgent
            # (OBSERVABILITY.md).
            from utils.logging_setup import (
                log_security_event,
                sanitize_log_value,
            )
            log_security_event(
                "appspot_blocked",
                "warning",
                service="default",
                endpoint=sanitize_log_value(request.endpoint or ""),
            )
            abort(403)

    # ── App Engine warmup (inbound_services: warmup in app.yaml) ──────
    # Fired when App Engine starts an instance AHEAD of traffic (a deploy, a
    # second instance) — never when the service wakes from zero: then the
    # first request itself waits for the start. It reaches one gunicorn
    # worker. Each step primes what that worker's first page would
    # otherwise pay for (2026-11-12): the two Firestore channels (the
    # default database, and « portail », which every page's Réception badge
    # reads), the two Firebase modules imported on first use, and the two
    # templates every first page renders. Each step is optional: a warmup
    # never fails loudly.
    @app.route("/_ah/warmup")
    def warmup() -> tuple[str, int]:
        def _prime(step) -> None:
            try:
                step()
            except Exception:  # pragma: no cover — warmup must never fail loudly
                pass

        def _firestore() -> None:
            from models import db
            db.collection("dav_sync").document("parties").get()

        def _portail() -> None:
            # A document that never exists: the channel opens, no
            # invitation is read.
            from models import portail_invitation
            portail_invitation._col().document("_warmup").get()

        def _firebase_modules() -> None:
            from firebase_admin import app_check, auth  # noqa: F401

        def _templates() -> None:
            for name in ("base.html", "dashboard/index.html"):
                app.jinja_env.get_template(name)

        for step in (_firestore, _portail, _firebase_modules, _templates):
            _prime(step)
        return "", 200

    # ── Offline fallback (PWA) ─────────────────────────────────────────
    @app.route("/offline")
    def offline():
        return _render_template("offline.html")

    # ── Digital Asset Links (Android TWA) ─────────────────────────────
    @app.route("/.well-known/assetlinks.json")
    def asset_links():
        import json
        data = [{
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": "ca.poirierlavoie.athena",
                "sha256_cert_fingerprints": ["47:3B:05:FB:50:D9:23:0E:51:FC:12:C7:AB:6A:DB:AD:02:FA:85:CB:3A:C9:BE:8E:80:1F:06:5E:7A:8B:D7:11"]
            }
        }]
        return json.dumps(data), 200, {"Content-Type": "application/json"}

    # ── CSP violation reporting (referenced by report-uri in security.py) ──
    from security import csrf as _csrf

    @app.route("/csp-report", methods=["POST"])
    @_csrf.exempt
    def csp_report():  # type: ignore[no-untyped-def]
        """Collect browser CSP violation reports (report-only policy)."""
        try:
            payload = request.get_json(force=True, silent=True) or {}
            report = payload.get("csp-report", payload) or {}
            from utils.logging_setup import log_security_event
            log_security_event(
                "csp_violation",
                "warning",
                directive=str(report.get("effective-directive")
                              or report.get("violated-directive") or "")[:120],
                blocked=str(report.get("blocked-uri") or "")[:200],
            )
        except Exception:  # pragma: no cover — reporting must never error
            pass
        return "", 204

    # ── Error handlers ─────────────────────────────────────────────────
    @app.errorhandler(404)
    def page_not_found(e):  # type: ignore[no-untyped-def]
        return _render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def internal_server_error(e):  # type: ignore[no-untyped-def]
        return _render_template("errors/500.html"), 500

    @app.errorhandler(413)
    def request_entity_too_large(e):  # type: ignore[no-untyped-def]
        return _render_template("errors/404.html"), 413

    from flask_wtf.csrf import CSRFError
    from werkzeug.exceptions import HTTPException

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):  # type: ignore[no-untyped-def]
        # Expected security event — WARNING via the typed helper, not an
        # ERROR-level unhandled exception.
        from utils.logging_setup import log_security_event
        log_security_event("csrf_failure", "warning", path=request.path)
        return _render_template("errors/404.html"), 400

    @app.errorhandler(Exception)
    def handle_unexpected(e):  # type: ignore[no-untyped-def]
        # Intended 4xx/redirect responses (abort(403), 401 from App Check,
        # 405s…) must keep their status and must NOT be logged as ERROR —
        # only genuine unhandled exceptions are.
        if isinstance(e, HTTPException):
            return e
        log_unexpected("unhandled exception")
        return _render_template("errors/500.html"), 500

    return app


# WSGI / gunicorn entrypoint
app = create_app()
