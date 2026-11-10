"""Tests for the embedded OAuth 2.1 authorization server and bearer auth."""

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from flask import Flask

with mock.patch("google.cloud.firestore.Client"):
    import mcp as mcp_pkg
    import mcp.bearer as bearer
    import mcp.store as store
    import mcp.tools as tools
    import mcp.disclosure as disclosure

UTC = timezone.utc
ATHENA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
ORIGIN = "https://athena.poirierlavoie.ca"


def _make_app(**config) -> Flask:
    app = Flask(__name__, template_folder=os.path.join(ATHENA_DIR, "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.config["MCP_CANONICAL_ORIGIN"] = ORIGIN
    app.config["ENV"] = "development"
    app.config["RATELIMIT_ENABLED"] = False
    app.config.update(config)

    from routes.auth_routes import auth_bp
    from security import csrf, limiter

    from utils.icons import ms as _ms
    app.jinja_env.globals["ms"] = _ms  # icônes Material (global posé par create_app en prod)
    csrf.init_app(app)
    limiter.init_app(app)
    app.register_blueprint(auth_bp)
    mcp_pkg.register_mcp(app)
    csrf.exempt(mcp_pkg.mcp_bp)
    return app


# ── In-memory fake of the store boundary (protocol tests) ───────────────

class FakeStore:
    """Dict-backed mirror of mcp.store semantics — no Firestore in CI."""

    def __init__(self):
        self.clients: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.tokens: dict[str, dict] = {}

    # clients
    def create_client(self, client_name, redirect_uris):
        client_id = secrets.token_urlsafe(24)
        doc = {
            "client_id": client_id,
            "client_name": client_name,
            "redirect_uris": list(redirect_uris),
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "last_used_at": None,
        }
        self.clients[client_id] = doc
        return doc

    def get_client(self, client_id):
        return self.clients.get(client_id)

    def touch_client(self, client_id):
        if client_id in self.clients:
            self.clients[client_id]["last_used_at"] = datetime.now(UTC)

    # codes
    def create_auth_code(self, client_id, redirect_uri, scope, code_challenge, resource):
        code = secrets.token_urlsafe(32)
        self.codes[store.sha256_hex(code)] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "resource": resource,
            "used": False,
            "family_id": None,
            "expire_at": datetime.now(UTC) + timedelta(seconds=300),
        }
        return code

    def get_auth_code(self, code_hash):
        doc = self.codes.get(code_hash)
        return dict(doc) if doc else None

    def consume_auth_code(self, code_hash, family_id):
        doc = self.codes.get(code_hash)
        if doc is None:
            return None, False
        if doc["used"]:
            return dict(doc), True
        doc["used"] = True
        doc["family_id"] = family_id
        return dict(doc), False

    # tokens
    def create_token_pair(self, client_id, scope, resource, family_id=None):
        family = family_id or uuid.uuid4().hex
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        base = {
            "client_id": client_id,
            "scope": scope,
            "resource": resource,
            "family_id": family,
            "revoked": False,
            "rotated_to": None,
            "last_used_at": None,
        }
        self.tokens[store.sha256_hex(access)] = {
            **base,
            "token_type": "access",
            "expire_at": now + timedelta(seconds=3600),
        }
        self.tokens[store.sha256_hex(refresh)] = {
            **base,
            "token_type": "refresh",
            "expire_at": now + timedelta(days=30),
        }
        return {
            "access_token": access,
            "refresh_token": refresh,
            "access_token_hash": store.sha256_hex(access),
            "refresh_token_hash": store.sha256_hex(refresh),
            "family_id": family,
            "scope": scope,
            "expires_in": 3600,
        }

    def get_token(self, token_hash):
        doc = self.tokens.get(token_hash)
        return dict(doc) if doc else None

    def rotate_refresh_token(self, token_hash):
        doc = self.tokens.get(token_hash)
        if doc is None or doc["token_type"] != "refresh":
            return None, "not_found"
        if doc["revoked"]:
            return None, "replayed"
        doc["revoked"] = True
        pair = self.create_token_pair(
            doc["client_id"], doc["scope"], doc["resource"],
            family_id=doc["family_id"],
        )
        doc["rotated_to"] = pair["refresh_token_hash"]
        return pair, ""

    def revoke_token_hash(self, token_hash):
        doc = self.tokens.get(token_hash)
        if doc is None or doc["revoked"]:
            return False
        doc["revoked"] = True
        return True

    def revoke_family(self, family_id):
        count = 0
        for doc in self.tokens.values():
            if doc["family_id"] == family_id and not doc["revoked"]:
                doc["revoked"] = True
                count += 1
        return count

    def stamp_token_last_used(self, token_hash):
        pass


_PATCHED_FUNCS = (
    "create_client",
    "get_client",
    "touch_client",
    "create_auth_code",
    "get_auth_code",
    "consume_auth_code",
    "create_token_pair",
    "get_token",
    "rotate_refresh_token",
    "revoke_token_hash",
    "revoke_family",
    "stamp_token_last_used",
)


@pytest.fixture()
def fake(monkeypatch):
    bearer.reset_brake_state()
    fake_store = FakeStore()
    for name in _PATCHED_FUNCS:
        monkeypatch.setattr(store, name, getattr(fake_store, name))
    yield fake_store
    bearer.reset_brake_state()


@pytest.fixture()
def app(fake):
    return _make_app()


@pytest.fixture()
def client(app):
    return app.test_client()


def _login(client):
    with client.session_transaction() as sess:
        sess["user_id"] = "test-user"
        sess["expires_at"] = datetime.now(UTC) + timedelta(hours=1)


def _register_client(fake) -> dict:
    return fake.create_client("Claude", [CLAUDE_CALLBACK])


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)[:64]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _authorize_params(client_doc, challenge, **overrides) -> dict:
    params = {
        "response_type": "code",
        "client_id": client_doc["client_id"],
        "redirect_uri": CLAUDE_CALLBACK,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "athena:read",
        "state": "xyz123",
    }
    params.update(overrides)
    return params


def _consent_allow(client, fake, client_doc, challenge) -> str:
    """Run the consent flow (GET → POST allow) and return the auth code."""
    _login(client)
    page = client.get("/oauth/authorize", query_string=_authorize_params(client_doc, challenge))
    assert page.status_code == 200
    token_match = re.search(
        rb'name="csrf_token" value="([^"]+)"', page.data
    )
    assert token_match, "consent page must embed a CSRF token"
    form = _authorize_params(client_doc, challenge)
    form["csrf_token"] = token_match.group(1).decode()
    form["decision"] = "allow"
    resp = client.post("/oauth/authorize", data=form)
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert location.startswith(CLAUDE_CALLBACK)
    code_match = re.search(r"[?&]code=([^&]+)", location)
    assert code_match and "state=xyz123" in location
    return code_match.group(1)


def _consent_form(client, client_doc, challenge, **extra):
    """Return a ready-to-POST consent form (CSRF harvested from the page)."""
    _login(client)
    page = client.get(
        "/oauth/authorize",
        query_string=_authorize_params(client_doc, challenge, **extra),
    )
    assert page.status_code == 200
    token_match = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
    assert token_match
    form = _authorize_params(client_doc, challenge, **extra)
    form["csrf_token"] = token_match.group(1).decode()
    form["decision"] = "allow"
    return form, page


def _code_from(resp):
    assert resp.status_code == 302
    match = re.search(r"[?&]code=([^&]+)", resp.headers["Location"])
    assert match
    return match.group(1)


def _exchange(client, client_doc, code, verifier, **overrides):
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CLAUDE_CALLBACK,
        "client_id": client_doc["client_id"],
        "code_verifier": verifier,
    }
    form.update(overrides)
    return client.post("/oauth/token", data=form)


# ── Metadata documents ──────────────────────────────────────────────────

def test_authorization_server_metadata(client):
    doc = client.get("/.well-known/oauth-authorization-server").get_json()
    assert doc["issuer"] == ORIGIN
    assert doc["authorization_endpoint"] == f"{ORIGIN}/oauth/authorize"
    assert doc["token_endpoint"] == f"{ORIGIN}/oauth/token"
    assert doc["registration_endpoint"] == f"{ORIGIN}/oauth/register"
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert doc["token_endpoint_auth_methods_supported"] == ["none"]
    # Two scopes since 2026-10-05: athena:comptabilite, advertised from plan
    # lot 0a, left with its consent box — the accounting tools are write
    # tools like the others, granted by the write box alone.
    assert doc["scopes_supported"] == ["athena:read", "athena:write"]


def test_protected_resource_metadata_both_paths(client):
    for path in (
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-protected-resource",
    ):
        doc = client.get(path).get_json()
        assert doc["resource"] == f"{ORIGIN}/mcp"
        assert doc["authorization_servers"] == [ORIGIN]
        assert doc["bearer_methods_supported"] == ["header"]
        assert doc["scopes_supported"] == ["athena:read", "athena:write"]


# ── Dynamic Client Registration ─────────────────────────────────────────

def test_register_happy_path(client, fake):
    resp = client.post(
        "/oauth/register",
        json={"client_name": "Claude", "redirect_uris": [CLAUDE_CALLBACK]},
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["client_id"] in fake.clients
    assert body["token_endpoint_auth_method"] == "none"
    assert body["redirect_uris"] == [CLAUDE_CALLBACK]


def test_register_rejects_non_allowlisted_redirect(client):
    resp = client.post(
        "/oauth/register", json={"redirect_uris": ["https://evil.example/cb"]}
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_redirect_uri"


def test_register_localhost_only_outside_production(fake):
    dev = _make_app(ENV="development").test_client()
    assert (
        dev.post(
            "/oauth/register", json={"redirect_uris": ["http://localhost:6274/cb"]}
        ).status_code
        == 201
    )
    prod = _make_app(ENV="production").test_client()
    assert (
        prod.post(
            "/oauth/register", json={"redirect_uris": ["http://localhost:6274/cb"]}
        ).status_code
        == 400
    )


def test_register_sanitizes_client_name(client, fake):
    resp = client.post(
        "/oauth/register",
        json={
            "client_name": "<script>alert(1)</script>Claude",
            "redirect_uris": [CLAUDE_CALLBACK],
        },
    )
    assert resp.get_json()["client_name"] == "alert(1)Claude"


# ── Authorize (consent) ─────────────────────────────────────────────────

def test_authorize_unauthenticated_redirects_to_login_with_full_query(client, fake):
    client_doc = _register_client(fake)
    _, challenge = _pkce_pair()
    resp = client.get(
        "/oauth/authorize", query_string=_authorize_params(client_doc, challenge)
    )
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]
    # The OAuth query string must survive the login round-trip.
    assert "code_challenge" in resp.headers["Location"]


def test_authorize_unknown_client_renders_error_page(client, fake):
    _login(client)
    _, challenge = _pkce_pair()
    resp = client.get(
        "/oauth/authorize",
        query_string={
            "response_type": "code",
            "client_id": "nope",
            "redirect_uri": CLAUDE_CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert resp.status_code == 400
    assert "Client OAuth inconnu".encode() in resp.data


def test_authorize_mismatched_redirect_renders_error_page(client, fake):
    client_doc = _register_client(fake)
    _login(client)
    _, challenge = _pkce_pair()
    resp = client.get(
        "/oauth/authorize",
        query_string=_authorize_params(
            client_doc, challenge, redirect_uri="https://evil.example/cb"
        ),
    )
    assert resp.status_code == 400
    assert "redirection".encode() in resp.data.lower()


@pytest.mark.parametrize(
    ("override", "expected_error"),
    [
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"code_challenge": ""}, "invalid_request"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        # athena:write is a SUPPORTED scope now, so it is no longer invalid to
        # request — it is simply never granted without the consent checkbox
        # (see the grant-rule tests below). A wholly unknown scope still is.
        ({"scope": "athena:admin"}, "invalid_scope"),
        # So is the accounting scope since it was retired on 2026-10-05,
        # requested alone (beside athena:read it is dropped — see
        # test_a_forged_accounting_tick_is_simply_ignored).
        ({"scope": "athena:comptabilite"}, "invalid_scope"),
        ({"resource": "https://evil.example/mcp"}, "invalid_target"),
    ],
)
def test_authorize_post_validation_errors_redirect_with_state(
    client, fake, override, expected_error
):
    client_doc = _register_client(fake)
    _login(client)
    _, challenge = _pkce_pair()
    resp = client.get(
        "/oauth/authorize",
        query_string=_authorize_params(client_doc, challenge, **override),
    )
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert location.startswith(CLAUDE_CALLBACK)
    assert f"error={expected_error}" in location
    assert "state=xyz123" in location


def test_authorize_decision_requires_csrf(client, fake):
    client_doc = _register_client(fake)
    _login(client)
    _, challenge = _pkce_pair()
    form = _authorize_params(client_doc, challenge)
    form["decision"] = "allow"
    resp = client.post("/oauth/authorize", data=form)  # no csrf_token
    assert resp.status_code == 400
    assert not fake.codes  # no code was issued


def test_authorize_deny_redirects_access_denied(client, fake):
    client_doc = _register_client(fake)
    _login(client)
    _, challenge = _pkce_pair()
    page = client.get(
        "/oauth/authorize", query_string=_authorize_params(client_doc, challenge)
    )
    token_match = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
    form = _authorize_params(client_doc, challenge)
    form["csrf_token"] = token_match.group(1).decode()
    form["decision"] = "deny"
    resp = client.post("/oauth/authorize", data=form)
    assert resp.status_code == 302
    assert "error=access_denied" in resp.headers["Location"]
    assert "state=xyz123" in resp.headers["Location"]


def test_consent_page_shows_client_name(client, fake):
    client_doc = _register_client(fake)
    _login(client)
    _, challenge = _pkce_pair()
    page = client.get(
        "/oauth/authorize", query_string=_authorize_params(client_doc, challenge)
    )
    assert "Claude".encode() in page.data
    assert "Autoriser".encode() in page.data
    assert "Refuser".encode() in page.data


# ── Write grant: the consent checkbox is the ONLY path ──────────────────

def test_consent_page_discloses_write_and_no_longer_claims_read_only(client, fake):
    """The screen is the only human-readable description of what the token
    can do. Cut short on 2026-11-10 (the lawyer: « far too long and dense —
    cut to the chase »): what it must still say is pinned here in its new
    words, and every sentence an earlier screen got WRONG stays absent."""
    client_doc = _register_client(fake)
    _login(client)
    _, challenge = _pkce_pair()
    page = client.get(
        "/oauth/authorize", query_string=_authorize_params(client_doc, challenge)
    )
    body = page.data.decode("utf-8")
    flat = " ".join(body.split())
    # The old copies' misrepresentations, each false once the write tools
    # existed (read-only, notes only, create-only, « jamais » bullets the
    # lots 3b, 4b and 5 rewrote) — absent, whatever the screen's length.
    for gone in (
        "Aucune modification de vos données n'est possible",
        "demande un accès <strong>en lecture seule</strong>",
        "Autoriser l'écriture des notes",
        "création seulement",
        "et <strong>uniquement</strong>, créer",
        "une seule modification",
        "écrire au <strong>fidéicommis</strong>",
        "toucher au <strong>fidéicommis</strong>",
        "sauf avec la case",
        "émettre un nouveau numéro de facture",
        "seul un encaissement inscrit dans l'application",
        "le budget des deux dossiers en tient compte",
        "il ne sera jamais réattribué, même si",
        "faute d'historique",
        "le numéro se libère",
        "signée",
        "Chaque écriture est horodatée",
        "fermer un dossier doit vider sa collection DavX5",
        "à la vérification d'identité ou à la vérification des conflits",
        "aucune des cases",
    ):
        assert gone not in flat, gone
    # READ — the hand-written list: what is read, the privilege it carries
    # and where it goes, and what is never shown.
    for must in (
        "demande à <strong>lire</strong> Pallas Athéna",
        "facturation et budgets",
        # Since 2026-11-10 the pièces justificatives too: a receipt's text
        # — or a photo, as the image itself — reaches claude.ai.
        "le contenu des documents et des pièces justificatives, souvent "
        "couvert par le <strong>secret professionnel</strong>&nbsp;: il "
        "<strong>transite par claude.ai</strong> quand vous l'y faites lire",
        "le fidéicommis, le registre d'administration et les états "
        "financiers, sans numéro de compte ni de transit",
        "les demandes Bookings en attente, avec le nom et le courriel du "
        "demandeur",
    ):
        assert must in flat, must
    # WRITE — the box, what it does, and what the lines must still say: the
    # one effect that reaches a client, the number an invoice consumes for
    # ever, what stays presumed, what the phone receives, what never happens.
    assert 'name="grant_write"' in body
    for must in (
        "Autoriser aussi les écritures",
        "Sans cette case, Claude ne peut rien créer ni modifier.",
        "un refus annule la réunion Outlook et <strong>en avise le "
        "client</strong>",
        "<strong>consommé pour toujours</strong>",
        "marquer envoyée (rien n'est envoyé) ou annuler une facture",
        "à titre <strong>présumé</strong> jusqu'à votre confirmation",
        "<strong>présumées</strong> jusqu'à votre confirmation",
        "<strong>transformé en gabarit</strong>",
        "(le contact reste)",
        "(votre téléphone suit)",
        # 2026-11-10: Claude may replace a receipt — and deletes nothing.
        "comme pièce justificative (une pièce remplacée reste conservée)",
        "<strong>supprimer</strong> quoi que ce soit",
        "inscrire un <strong>paiement</strong> hors des registres comptables",
        "marquer une facture <strong>payée</strong> sans paiement inscrit",
        "<strong>envoyer</strong> une facture",
        "<strong>confirmer</strong> une vérification d'identité ou de "
        "conflits",
        "<strong>virer des fonds</strong> du fidéicommis",
        "retirer du fidéicommis <strong>en espèces</strong>",
        "journalisé et marqué comme provenant de Claude",
        "vont aussi à votre téléphone",
        "Vous pouvez révoquer cet accès en tout temps.",
    ):
        assert must in flat, must
    # Default state is unchecked — least privilege — and it is the ONE box.
    checkbox = re.search(r'<input type="checkbox" name="grant_write"[^>]*>', body)
    assert checkbox and "checked" not in checkbox.group(0)
    consent_form = body[body.index('<form method="post" action="/oauth/authorize"'):]
    assert re.findall(r'<input type="checkbox" name="([^"]+)"', consent_form) == [
        "grant_write"]


def test_the_consent_screen_stays_short(client, fake):
    """The lawyer, 2026-11-10: the screen was « far too long and dense » —
    about 4 600 words: a partial per write family, a checkbox summary that
    restated them, twenty long « jamais » bullets. It now says each thing
    once, in a few words, and this budget keeps it so: a line that grows
    back fails here first. Measured with the client asking for write (the
    longest screen)."""
    budget = 450
    client_doc = _register_client(fake)
    _, challenge = _pkce_pair()
    _, page = _consent_form(client, client_doc, challenge,
                            scope="athena:read athena:write")
    body = page.data.decode("utf-8")
    assert "Le client a demandé cet accès." in _flat(body)
    assert _visible_words(body) <= budget, _visible_words(body)
    # The budget bites: one family line grown back to a paragraph fails it.
    grown = body.replace("</li>", " " + "mot " * (budget // 2) + "</li>", 1)
    assert _visible_words(grown) > budget


def _flat(text: str) -> str:
    return " ".join(text.split())


def _visible_words(body: str) -> int:
    """The words a reader sees on the consent card: from its title to the
    end of ITS form (the page's frame has forms of its own before it),
    scripts, tags and entities out."""
    start = body.index("<h1")
    card = body[start:body.index("</form>", start)]
    card = re.sub(r"<script\b.*?</script>", " ", card, flags=re.S)
    card = re.sub(r"<[^>]+>", " ", card)
    card = re.sub(r"&[a-z]+;", " ", card)
    return len([w for w in card.split() if any(c.isalnum() for c in w)])


def test_the_write_block_is_assembled_from_the_disclosure_registry(client, fake):
    """DERIVED, both ways: every write family's line and every promise's
    clause reach the page, in the registry's order — so a family added to
    mcp/disclosure is described to the lawyer by construction, and a promise
    deleted there leaves the screen with it."""
    client_doc = _register_client(fake)
    _, challenge = _pkce_pair()
    _, page = _consent_form(client, client_doc, challenge)
    body = page.data.decode("utf-8")
    context = disclosure.consent_context()
    assert set(context) == {"write_families", "nevers"}
    # Every family is a write family since 2026-10-05 — ACCOUNTING too.
    assert [f.key for f in context["write_families"]] == [
        f.key for f in disclosure.FAMILIES if f.tools]
    assert "accounting" in [f.key for f in context["write_families"]]
    assert len(context["nevers"]) == len(disclosure.NEVERS)
    block = _flat(_write_block(body))
    jamais = block.index("Jamais&nbsp;:")
    lines = [block.index(f"<li><strong>{f.title_fr}</strong>&nbsp;: "
                         f"{_flat(str(f.consent_fr))}</li>")
             for f in context["write_families"]]
    assert lines == sorted(lines) and lines[-1] < jamais
    clauses = [block.index(f"<li>{_flat(str(n))}</li>", jamais)
               for n in context["nevers"]]
    assert clauses == sorted(clauses)
    # The known false claims are absent from the rendered screen too — the
    # list test_mcp_disclosure sweeps the connector's strings with.
    from tests.test_mcp_disclosure import _false_claims_in
    assert _false_claims_in(_flat(body)) == []


def test_the_write_block_uses_only_compiled_classes(client, fake):
    """A class absent from the compiled artifact silently does not apply
    (CLAUDE.md item 6): every class of the consent card, its write block
    included, is in the stylesheet."""
    client_doc = _register_client(fake)
    _, challenge = _pkce_pair()
    _, page = _consent_form(client, client_doc, challenge)
    body = page.data.decode("utf-8")
    start = body.index("<h1")
    card = body[start:body.index("</form>", start)]
    assert "<strong>Comptabilité</strong>" in _write_block(body)
    classes = {c for block in re.findall(r'class="([^"]+)"', card)
               for c in block.split()}
    assert {"list-disc", "mt-3", "font-medium", "bg-amber-50",
            "text-indigo-600", "focus:ring-indigo-500"} <= classes
    css_path = next(iter(sorted(
        (p for p in os.listdir(os.path.join(ATHENA_DIR, "static", "vendor"))
         if re.fullmatch(r"app\.[0-9a-f]{8}\.css", p))
    )))
    with open(os.path.join(ATHENA_DIR, "static", "vendor", css_path), encoding="utf-8") as fh:
        css = fh.read()
    absent = []
    for cls in sorted(classes):
        needle = "." + re.sub(r"([:./])", r"\\\1", cls)
        hits = [m.end() for m in re.finditer(re.escape(needle), css)]
        if not any(i >= len(css) or not (css[i].isalnum() or css[i] in "-_\\")
                   for i in hits):
            absent.append(cls)
    assert not absent, absent


def test_unticked_checkbox_grants_read_only(client, fake):
    """Even when the CLIENT requests write. The hidden scope field is
    attacker-modifiable and must never be able to escalate on its own."""
    client_doc = _register_client(fake)
    _, challenge = _pkce_pair()
    form, page = _consent_form(
        client, client_doc, challenge, scope="athena:read athena:write"
    )
    # The request only informs the page — it says the client asked, and
    # pre-ticks nothing; the hidden field stays the read baseline.
    body = page.data.decode("utf-8")
    assert "Le client a demandé cet accès." in _flat(body)
    assert 'name="scope" value="athena:read"' in body
    assert "grant_write" not in form
    code = _code_from(client.post("/oauth/authorize", data=form))
    stored = fake.codes[store.sha256_hex(code)]
    assert stored["scope"] == "athena:read"


def test_ticked_checkbox_grants_read_and_write(client, fake):
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    form, page = _consent_form(client, client_doc, challenge)
    # Not requested: the box says nothing of a request.
    assert "Le client a demandé cet accès" not in _flat(page.data.decode("utf-8"))
    form["grant_write"] = "on"
    code = _code_from(client.post("/oauth/authorize", data=form))
    assert fake.codes[store.sha256_hex(code)]["scope"] == "athena:read athena:write"
    # And the granted scope must reach the token response (RFC 6749 §3.3:
    # the AS may grant a scope other than requested, but MUST echo it).
    body = _exchange(client, client_doc, code, verifier).get_json()
    assert body["scope"] == "athena:read athena:write"


def test_write_only_request_still_yields_a_usable_read_scope(client, fake):
    """bearer.py demands athena:read on EVERY /mcp call — a write-only grant
    would be a permanently dead connector that 403s even on initialize."""
    client_doc = _register_client(fake)
    _, challenge = _pkce_pair()
    form, _ = _consent_form(client, client_doc, challenge, scope="athena:write")
    form["grant_write"] = "on"
    code = _code_from(client.post("/oauth/authorize", data=form))
    assert fake.codes[store.sha256_hex(code)]["scope"].split() == [
        "athena:read", "athena:write"
    ]


def test_refresh_rotation_never_widens_the_scope(client, fake):
    """A read-only family must never rotate itself into write."""
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    form, _ = _consent_form(client, client_doc, challenge)
    code = _code_from(client.post("/oauth/authorize", data=form))
    first = _exchange(client, client_doc, code, verifier).get_json()
    assert first["scope"] == "athena:read"
    rotated = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": client_doc["client_id"],
        },
    ).get_json()
    assert rotated["scope"] == "athena:read"
    # Assert the STORED doc too: bearer.py reads that, not the echo.
    assert fake.tokens[store.sha256_hex(rotated["access_token"])]["scope"] == (
        "athena:read"
    )


def test_refresh_rotation_preserves_the_write_grant(client, fake):
    """The direction that silently breaks the feature: if rotation narrowed
    the scope, note writing would work for 60 minutes and then the tools
    would vanish from tools/list mid-conversation, with no error."""
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    form, _ = _consent_form(client, client_doc, challenge)
    form["grant_write"] = "on"
    code = _code_from(client.post("/oauth/authorize", data=form))
    first = _exchange(client, client_doc, code, verifier).get_json()
    assert first["scope"] == "athena:read athena:write"
    rotated = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": client_doc["client_id"],
        },
    ).get_json()
    assert rotated["scope"] == "athena:read athena:write"
    assert fake.tokens[store.sha256_hex(rotated["access_token"])]["scope"] == (
        "athena:read athena:write"
    )


# ── The accounting tools: in the write block, under the write box ───────
#
# Until 2026-10-05 the accounting tools had their OWN block, box, scope
# (athena:comptabilite) and switch. The lawyer removed all four: they are
# write tools like the others, described by the ACCOUNTING family's partial
# inside the write block and granted by « Autoriser les écritures » alone.
# What survives of the separate grant is that nothing can bring it back — a
# forged tick, a hidden field or a request naming the retired scope.


def _granted(client, fake, client_doc, challenge, *, scope="athena:read", **ticks):
    """Consent with the given boxes ticked; return the STORED code scope."""
    form, _ = _consent_form(client, client_doc, challenge, scope=scope)
    for box in ticks:
        form[box] = "on"
    code = _code_from(client.post("/oauth/authorize", data=form))
    return fake.codes[store.sha256_hex(code)]["scope"]


def _write_block(body: str) -> str:
    """The write block as rendered: from the write box's label to the
    revocation line — the box, the family lines, the « jamais » list and
    the provenance line."""
    start = body.rindex("<label", 0, body.index('name="grant_write"'))
    return body[start:body.index("Vous pouvez révoquer", start)]


def test_the_write_block_says_what_the_accounting_tools_do_and_never_do(client, fake):
    """The ACCOUNTING family's line sits in the write block, before the
    « jamais » list, and the promises the accounting tools keep are clauses
    of that list: told to everyone who reads « Autoriser aussi les
    écritures », since that box is what grants them.

    REWRITTEN on 2026-11-10, when the lawyer had the screen cut short: the
    D14 rules the family's partial spelled out (art. 58, the same-operation
    fee payment, the clearing ceiling, the reconciled period) live in the
    register tools' descriptions, which Claude reads, and
    tests/test_mcp_accounting.py pins their behaviour."""
    client_doc = _register_client(fake)
    _, challenge = _pkce_pair()
    _, page = _consent_form(client, client_doc, challenge)
    body = page.data.decode("utf-8")
    flat = _flat(body)
    block = _flat(_write_block(body))
    (accounting,) = [f for f in disclosure.FAMILIES if f.key == "accounting"]
    jamais = block.index("Jamais&nbsp;:")
    line = (f"<li><strong>{accounting.title_fr}</strong>&nbsp;: "
            f"{_flat(accounting.consent_fr)}</li>")
    assert 0 <= block.index(line) < jamais
    assert "compenser et contre-passer" in line
    for key in ("register_delete", "register_setup", "register_transfer",
                "trust_withdrawal", "fee_invoice", "fee_payee",
                "account_number"):
        (never,) = [n for n in disclosure.NEVERS if n.key == key]
        assert f"<li>{_flat(never.fr)}</li>" in block[jamais:], key
    # Nothing of the separate grant survives on the page.
    assert 'name="grant_comptabilite"' not in body
    for gone in ("Autoriser la comptabilité", "Comptabilité (facultatif)",
                 "autorisation distincte"):
        assert gone not in flat, gone
    from tests.test_mcp_disclosure import _false_claims_in
    assert _false_claims_in(flat) == []


@pytest.mark.parametrize("requested", [
    "athena:read", "athena:read athena:comptabilite",
], ids=["read", "retired_scope_requested"])
@pytest.mark.parametrize("ticks, minted", [
    (("grant_comptabilite",), "athena:read"),
    (("grant_write", "grant_comptabilite"), "athena:read athena:write"),
], ids=["accounting_tick_alone", "with_the_write_box"])
def test_a_forged_accounting_tick_is_simply_ignored(
    client, fake, requested, ticks, minted
):
    """The accounting box and its scope left on 2026-10-05: a
    `grant_comptabilite` field — forged, or posted by a page rendered before
    the removal —, a hidden `comptabilite_offered`, and a request naming the
    retired scope beside athena:read are all ignored. The hidden `scope`
    field stays the read baseline, and the minted scope is exactly it, plus
    athena:write when — and only when — the write box is ticked."""
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    form, page = _consent_form(client, client_doc, challenge, scope=requested)
    assert 'name="scope" value="athena:read"' in page.data.decode("utf-8")
    for box in ticks:
        form[box] = "on"
    form["comptabilite_offered"] = "True"      # a hidden field changes nothing
    code = _code_from(client.post("/oauth/authorize", data=form))
    assert fake.codes[store.sha256_hex(code)]["scope"] == minted
    # And the token the code buys, echoed and STORED (bearer.py reads the
    # stored scope, not the echo).
    body = _exchange(client, client_doc, code, verifier).get_json()
    assert body["scope"] == minted
    assert fake.tokens[store.sha256_hex(body["access_token"])]["scope"] == minted


def test_the_consent_line_says_whether_write_was_granted(client, fake, caplog):
    """The `mcp_consent` line says what the screen granted — athena:write or
    not — and the scope it minted. Since 2026-10-05 it carries no
    `comptabilite_granted`, and a forged accounting tick moves neither."""
    import logging

    client_doc = _register_client(fake)
    _, challenge = _pkce_pair()
    seen = []
    for ticks in ({}, {"grant_write": True}, {"grant_comptabilite": True},
                  {"grant_write": True, "grant_comptabilite": True}):
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="pallas.mcp"):
            stored = _granted(client, fake, client_doc, challenge, **ticks)
        (line,) = [
            r.json_fields for r in caplog.records
            if getattr(r, "json_fields", {}).get("event") == "mcp_consent"
        ]
        assert "comptabilite_granted" not in line
        assert line["scope"] == stored
        seen.append((line["write_granted"], stored))
    assert seen == [
        (False, "athena:read"), (True, "athena:read athena:write"),
        (False, "athena:read"), (True, "athena:read athena:write"),
    ]


# ── Token endpoint ──────────────────────────────────────────────────────

def test_full_pkce_round_trip(client, fake):
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    code = _consent_allow(client, fake, client_doc, challenge)

    resp = _exchange(client, client_doc, code, verifier)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    assert body["scope"] == "athena:read"
    assert store.sha256_hex(body["access_token"]) in fake.tokens
    assert store.sha256_hex(body["refresh_token"]) in fake.tokens
    # The code is burned.
    code_doc = fake.codes[store.sha256_hex(code)]
    assert code_doc["used"] is True


def test_wrong_verifier_rejected(client, fake):
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    code = _consent_allow(client, fake, client_doc, challenge)
    other_verifier, _ = _pkce_pair()
    resp = _exchange(client, client_doc, code, other_verifier)
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_grant"


def test_expired_code_rejected(client, fake):
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    code = _consent_allow(client, fake, client_doc, challenge)
    fake.codes[store.sha256_hex(code)]["expire_at"] = datetime.now(UTC) - timedelta(
        seconds=1
    )
    resp = _exchange(client, client_doc, code, verifier)
    assert resp.get_json()["error"] == "invalid_grant"


def test_redirect_uri_mismatch_rejected(client, fake):
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    code = _consent_allow(client, fake, client_doc, challenge)
    resp = _exchange(
        client, client_doc, code, verifier,
        redirect_uri="https://claude.com/api/mcp/auth_callback",
    )
    assert resp.get_json()["error"] == "invalid_grant"


def test_reused_code_revokes_family(client, fake):
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    code = _consent_allow(client, fake, client_doc, challenge)

    first = _exchange(client, client_doc, code, verifier)
    assert first.status_code == 200
    tokens = first.get_json()

    replay = _exchange(client, client_doc, code, verifier)
    assert replay.get_json()["error"] == "invalid_grant"
    # Every token minted from the replayed code is dead.
    assert fake.tokens[store.sha256_hex(tokens["access_token"])]["revoked"]
    assert fake.tokens[store.sha256_hex(tokens["refresh_token"])]["revoked"]


def test_refresh_rotation_issues_new_pair_and_revokes_old(client, fake):
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    code = _consent_allow(client, fake, client_doc, challenge)
    tokens = _exchange(client, client_doc, code, verifier).get_json()

    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client_doc["client_id"],
        },
    )
    assert resp.status_code == 200
    new_tokens = resp.get_json()
    assert new_tokens["access_token"] != tokens["access_token"]
    assert new_tokens["scope"] == "athena:read"

    old_hash = store.sha256_hex(tokens["refresh_token"])
    assert fake.tokens[old_hash]["revoked"] is True
    assert fake.tokens[old_hash]["rotated_to"] == store.sha256_hex(
        new_tokens["refresh_token"]
    )
    # Same family across the rotation.
    assert (
        fake.tokens[store.sha256_hex(new_tokens["refresh_token"])]["family_id"]
        == fake.tokens[old_hash]["family_id"]
    )


def test_revoked_refresh_replay_kills_family(client, fake):
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    code = _consent_allow(client, fake, client_doc, challenge)
    tokens = _exchange(client, client_doc, code, verifier).get_json()

    def refresh(token):
        return client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": token,
                "client_id": client_doc["client_id"],
            },
        )

    rotated = refresh(tokens["refresh_token"]).get_json()
    replay = refresh(tokens["refresh_token"])  # old token again
    assert replay.get_json()["error"] == "invalid_grant"
    # The rotated successor pair is dead too.
    assert fake.tokens[store.sha256_hex(rotated["access_token"])]["revoked"]
    assert fake.tokens[store.sha256_hex(rotated["refresh_token"])]["revoked"]


def test_unsupported_grant_type(client):
    resp = client.post("/oauth/token", data={"grant_type": "password"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "unsupported_grant_type"


def test_token_missing_fields_invalid_request(client):
    resp = client.post("/oauth/token", data={"grant_type": "authorization_code"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_request"


# ── Revocation ──────────────────────────────────────────────────────────

def test_revoke_store_failure_returns_503_not_200(client, fake, monkeypatch):
    def boom(token_hash):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(store, "get_token", boom)
    resp = client.post("/oauth/revoke", data={"token": "whatever"})
    assert resp.status_code == 503


def test_revoke_refresh_revokes_family_and_unknown_token_is_200(client, fake):
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    code = _consent_allow(client, fake, client_doc, challenge)
    tokens = _exchange(client, client_doc, code, verifier).get_json()

    resp = client.post("/oauth/revoke", data={"token": tokens["refresh_token"]})
    assert resp.status_code == 200
    assert fake.tokens[store.sha256_hex(tokens["access_token"])]["revoked"]

    assert client.post("/oauth/revoke", data={"token": "unknown"}).status_code == 200


# ── Bearer validation on /mcp ───────────────────────────────────────────

def _mcp_ping(client, token, headers=None):
    return client.post(
        "/mcp",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        content_type="application/json",
        headers={"Authorization": f"Bearer {token}", **(headers or {})},
    )


def _issue_tokens(client, fake):
    client_doc = _register_client(fake)
    verifier, challenge = _pkce_pair()
    code = _consent_allow(client, fake, client_doc, challenge)
    return _exchange(client, client_doc, code, verifier).get_json()


def test_bearer_valid_token_passes(client, fake):
    tokens = _issue_tokens(client, fake)
    assert _mcp_ping(client, tokens["access_token"]).status_code == 200


def test_bearer_missing_token_401_with_resource_metadata(client, fake):
    resp = client.post(
        "/mcp",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        content_type="application/json",
    )
    assert resp.status_code == 401
    challenge = resp.headers["WWW-Authenticate"]
    assert "Bearer" in challenge
    assert f'{ORIGIN}/.well-known/oauth-protected-resource/mcp' in challenge


def test_bearer_invalid_expired_revoked_and_refresh_tokens_rejected(client, fake):
    tokens = _issue_tokens(client, fake)
    access_hash = store.sha256_hex(tokens["access_token"])

    bearer.reset_brake_state()
    assert _mcp_ping(client, "no-such-token").status_code == 401

    fake.tokens[access_hash]["expire_at"] = datetime.now(UTC) - timedelta(seconds=1)
    bearer.reset_brake_state()
    assert _mcp_ping(client, tokens["access_token"]).status_code == 401

    fake.tokens[access_hash]["expire_at"] = datetime.now(UTC) + timedelta(hours=1)
    fake.tokens[access_hash]["revoked"] = True
    bearer.reset_brake_state()
    assert _mcp_ping(client, tokens["access_token"]).status_code == 401

    # A refresh token is never accepted as a bearer credential.
    bearer.reset_brake_state()
    resp = _mcp_ping(client, tokens["refresh_token"])
    assert resp.status_code == 401
    assert 'error="invalid_token"' in resp.headers["WWW-Authenticate"]


def test_bearer_insufficient_scope_403(client, fake):
    tokens = _issue_tokens(client, fake)
    fake.tokens[store.sha256_hex(tokens["access_token"])]["scope"] = "other:scope"
    bearer.reset_brake_state()
    resp = _mcp_ping(client, tokens["access_token"])
    assert resp.status_code == 403
    assert 'error="insufficient_scope"' in resp.headers["WWW-Authenticate"]


def test_bearer_brake_trips_after_threshold(client, fake):
    bearer.reset_brake_state()
    for _ in range(20):
        assert _mcp_ping(client, "bad-token").status_code == 401
    resp = _mcp_ping(client, "bad-token")
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


def test_bearer_origin_allowlist(client, fake):
    tokens = _issue_tokens(client, fake)
    assert (
        _mcp_ping(
            client, tokens["access_token"], headers={"Origin": "https://claude.ai"}
        ).status_code
        == 200
    )
    resp = _mcp_ping(
        client, tokens["access_token"], headers={"Origin": "https://evil.example"}
    )
    assert resp.status_code == 403


# ── Store invariants ────────────────────────────────────────────────────

def test_sha256_hex_is_the_document_key():
    assert store.sha256_hex("abc") == hashlib.sha256(b"abc").hexdigest()


def test_expiry_enforced_in_code_despite_ttl_lag():
    # A doc that still exists in Firestore (TTL deletion lags) but whose
    # expire_at is past must be treated as dead.
    stale = {"expire_at": datetime.now(UTC) - timedelta(days=2)}
    assert store.is_expired(stale) is True
    live = {"expire_at": datetime.now(UTC) + timedelta(minutes=5)}
    assert store.is_expired(live) is False
    assert store.is_expired({}) is True  # missing expire_at fails closed
