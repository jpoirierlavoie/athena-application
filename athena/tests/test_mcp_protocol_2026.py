"""Tests for MCP-Socle (#v0.7.0) transport parity & SEP-2127 server card.

Covers ``mcp.protocol`` unit contracts and full Flask ``/mcp`` +
``/mcp/server-card`` integration through ``security.init_security``.
"""

import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import mcp as mcp_pkg
    import mcp.bearer as bearer
    import mcp.endpoint as endpoint
    import mcp.protocol as protocol
    import mcp.store as store
    import security


UTC = timezone.utc
AUTH = {"Authorization": "Bearer test-token"}


def _make_secure_app(**config) -> Flask:
    app = Flask(
        __name__,
        template_folder=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "templates",
        ),
    )
    app.config["SECRET_KEY"] = "test-secret"
    app.config["MCP_CANONICAL_ORIGIN"] = "https://athena.poirierlavoie.ca"
    app.config["ENV"] = "development"
    app.config["RATELIMIT_ENABLED"] = False
    app.config.update(config)
    security.init_security(app)
    mcp_pkg.register_mcp(app)
    security.csrf.exempt(mcp_pkg.mcp_bp)
    return app


def _token(scope: str = "athena:read") -> dict:
    return {
        "token_type": "access",
        "client_id": "client-1",
        "scope": scope,
        "resource": None,
        "family_id": "fam-1",
        "revoked": False,
        "expire_at": datetime.now(UTC) + timedelta(hours=1),
    }


def _b64_sentinel(text: str) -> str:
    return f"=?base64?{base64.b64encode(text.encode('utf-8')).decode('ascii')}?="


# ── Version negotiation & boundary ───────────────────────────────────────

def test_modern_version_boundary_and_error_copy():
    assert protocol.is_modern("2026-07-28") is True
    assert protocol.is_modern("2026-11-01") is True
    assert protocol.is_modern("2025-11-25") is False
    assert protocol.is_modern("2025-03-26") is False

    served = ["2026-07-28", "2025-11-25", "2025-06-18"]
    ver, err = protocol.negotiate_version("2025-11-25", served)
    assert ver == "2025-11-25" and err is None

    ver2, err2 = protocol.negotiate_version("1900-01-01", served)
    assert ver2 is None
    assert err2["code"] == protocol.UNSUPPORTED_PROTOCOL_VERSION == -32022
    assert err2["data"] == {"supported": served, "requested": "1900-01-01"}

    # Mutating error data must not mutate caller's supported list.
    err2["data"]["supported"].append("bogus")
    assert served == ["2026-07-28", "2025-11-25", "2025-06-18"]


def test_absent_version_header_and_initialize_negotiation():
    assert protocol.absent_version_allowed("initialize", serves_pre_2025_06_18=False) is True
    assert protocol.absent_version_allowed("server/discover", serves_pre_2025_06_18=False) is True
    assert protocol.absent_version_allowed("tools/list", serves_pre_2025_06_18=False) is False
    assert protocol.absent_version_allowed("tools/list", serves_pre_2025_06_18=True) is True

    supported = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26")
    assert protocol.negotiate_initialize("2026-07-28", supported) == "2026-07-28"
    assert protocol.negotiate_initialize("2025-06-18", supported) == "2025-06-18"
    assert protocol.negotiate_initialize("1999-01-01", supported) == "2025-11-25"
    assert protocol.negotiate_initialize(None, supported) == "2025-11-25"
    assert protocol.negotiate_initialize(123, supported) == "2025-11-25"
    assert protocol.negotiate_initialize(None, ("2026-07-28",)) == "2026-07-28"


# ── Base64 sentinel & anti-smuggling header concordance ──────────────────

def test_decode_header_sentinel_strict_utf8():
    assert protocol.decode_header_sentinel("get_dossier") == "get_dossier"
    assert protocol.decode_header_sentinel("=?base64?sans fin") == "=?base64?sans fin"
    assert protocol.decode_header_sentinel(_b64_sentinel("café à Montréal")) == "café à Montréal"
    assert protocol.decode_header_sentinel("=?base64?!!!pas-base64!!!?=") is None
    invalid_utf8 = f"=?base64?{base64.b64encode(b'\xff\xfe\xfd').decode('ascii')}?="
    assert protocol.decode_header_sentinel(invalid_utf8) is None


def test_validate_modern_headers_concordance():
    assert protocol.HEADER_MISMATCH == -32020
    base_headers = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list"}
    assert protocol.validate_modern_headers(base_headers, "tools/list") is None
    assert (
        protocol.validate_modern_headers(
            {"Mcp-Method": "tools/list"}, "tools/list"
        )
        is not None
    )
    assert (
        protocol.validate_modern_headers(
            {"MCP-Protocol-Version": "2026-07-28"}, "tools/list"
        )
        is not None
    )
    assert (
        protocol.validate_modern_headers(
            {**base_headers, "Mcp-Method": "tools/call"}, "tools/list"
        )
        is not None
    )
    assert (
        protocol.validate_modern_headers(
            base_headers,
            "tools/list",
            {"_meta": {protocol.META_PROTOCOL_VERSION: "2025-11-25"}},
        )
        is not None
    )
    assert (
        protocol.validate_modern_headers(
            base_headers, "tools/list", {"_meta": ["not-dict"]}
        )
        is not None
    )
    assert (
        protocol.validate_modern_headers(
            base_headers, "tools/list", require_version_meta=True
        )
        is not None
    )
    assert (
        protocol.validate_modern_headers(
            base_headers,
            "tools/list",
            {"_meta": {protocol.META_PROTOCOL_VERSION: "2026-07-28"}},
            require_version_meta=True,
        )
        is None
    )

    call_headers = {
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/call",
        "Mcp-Name": _b64_sentinel("outil_café"),
    }
    assert (
        protocol.validate_modern_headers(
            call_headers, "tools/call", {"name": "outil_café"}
        )
        is None
    )
    assert (
        protocol.validate_modern_headers(
            {**call_headers, "Mcp-Name": "autre"},
            "tools/call",
            {"name": "outil_café"},
        )
        is not None
    )
    assert (
        protocol.validate_modern_headers(
            {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call"},
            "tools/call",
            {"name": "outil_café"},
        )
        is not None
    )
    assert (
        protocol.validate_modern_headers(
            {
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "resources/read",
                "Mcp-Name": "https://x/1",
            },
            "resources/read",
            {"uri": "https://x/1"},
        )
        is None
    )


def test_read_request_meta_and_readable_client():
    assert protocol.read_request_meta(None) == {}
    assert protocol.read_request_meta({"_meta": ["bad"]}) == {}
    meta = protocol.read_request_meta(
        {
            "_meta": {
                protocol.META_PROTOCOL_VERSION: "2026-07-28",
                protocol.META_CLIENT_INFO: {"name": "Claude", "version": "2.1", "extra": "ignored"},
                protocol.META_CLIENT_CAPABILITIES: {},
            }
        }
    )
    assert meta == {
        "protocolVersion": "2026-07-28",
        "clientInfo": {"name": "Claude", "version": "2.1"},
        "clientCapabilities": {},
    }
    assert protocol.readable_client(meta["clientInfo"]) == "Claude/2.1"
    assert protocol.readable_client({"name": "Claude"}) == "Claude"
    assert protocol.readable_client({"name": 123}) is None


# ── Endpoint integration (2026-07-28 vs 2025-11-25) ──────────────────────

def _post_mcp(client, body, headers=None):
    return client.post(
        "/mcp",
        data=json.dumps(body),
        content_type="application/json",
        headers={**AUTH, **(headers or {})},
    )


def test_server_discover_over_mcp_endpoint(monkeypatch):
    bearer.reset_brake_state()
    monkeypatch.setattr(store, "get_token", lambda h: _token("athena:read"))
    monkeypatch.setattr(store, "stamp_token_last_used", lambda h: None)
    try:
        cl = _make_secure_app().test_client()
        # Works header-less and under 2026-07-28 with Mcp-Method.
        for headers in (
            None,
            {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "server/discover"},
        ):
            resp = _post_mcp(
                cl,
                {"jsonrpc": "2.0", "id": 1, "method": "server/discover"},
                headers=headers,
            )
            assert resp.status_code == 200
            res = resp.get_json()["result"]
            assert res["supportedVersions"] == list(mcp_pkg.SUPPORTED_PROTOCOL_VERSIONS)
            assert res["capabilities"] == {"tools": {"listChanged": False}}
            assert res["instructions"] == endpoint.INSTRUCTIONS
            assert res["resultType"] == "complete"
            assert res["ttlMs"] == 3_600_000
            # Critical: Athena tools/list varies by token scope -> private only.
            assert res["cacheScope"] == "private"
            assert "serverInfo" not in res
            assert res["_meta"][protocol.META_SERVER_INFO] == endpoint.SERVER_INFO
    finally:
        bearer.reset_brake_state()


def test_modern_headers_and_wrapping_on_2026_07_28_vs_2025_11_25(monkeypatch):
    bearer.reset_brake_state()
    monkeypatch.setattr(store, "get_token", lambda h: _token("athena:read"))
    monkeypatch.setattr(store, "stamp_token_last_used", lambda h: None)
    try:
        cl = _make_secure_app(RECAPTCHA_ENTERPRISE_SITE_KEY="test-site-key").test_client()

        # Legacy 2025-11-25: no Mcp-Method required, no modern wrapper fields.
        legacy_list = _post_mcp(
            cl,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"MCP-Protocol-Version": "2025-11-25"},
        )
        assert legacy_list.status_code == 200
        legacy_res = legacy_list.get_json()["result"]
        assert "resultType" not in legacy_res
        assert "cacheScope" not in legacy_res
        assert "_meta" not in legacy_res

        # Modern 2026-07-28 missing Mcp-Method -> 400 + -32020.
        bad_hdr = _post_mcp(
            cl,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers={"MCP-Protocol-Version": "2026-07-28"},
        )
        assert bad_hdr.status_code == 400
        assert bad_hdr.get_json()["error"]["code"] == protocol.HEADER_MISMATCH

        # Modern 2026-07-28 tools/list -> wrapped with private cacheScope.
        mod_list = _post_mcp(
            cl,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            headers={
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/list",
                "HX-Request": "true",
            },
        )
        assert mod_list.status_code == 200
        mod_res = mod_list.get_json()["result"]
        assert mod_res["resultType"] == "complete"
        assert mod_res["ttlMs"] == 3_600_000
        assert mod_res["cacheScope"] == "private"
        assert mod_res["_meta"][protocol.META_SERVER_INFO] == endpoint.SERVER_INFO

        # Modern 2026-07-28 tools/call with base64 sentinel & refusal -> isError + wrapped.
        mod_call = _post_mcp(
            cl,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "parse_court_file_number",
                    "arguments": {"court_file_number": "500-05-123456-241"},
                },
            },
            headers={
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/call",
                "Mcp-Name": _b64_sentinel("parse_court_file_number"),
            },
        )
        assert mod_call.status_code == 200
        call_res = mod_call.get_json()["result"]
        assert call_res["isError"] is False
        assert call_res["resultType"] == "complete"
        assert call_res["_meta"][protocol.META_SERVER_INFO] == endpoint.SERVER_INFO
        assert "structuredContent" not in call_res
    finally:
        bearer.reset_brake_state()


# ── SEP-2127 /mcp/server-card ────────────────────────────────────────────

def test_sep_2127_server_card_methods_headers_and_appcheck_exemption():
    app = _make_secure_app(RECAPTCHA_ENTERPRISE_SITE_KEY="test-site-key")
    cl = app.test_client()

    # Probe with HX-Request: true (no App Check token) to confirm public exemption.
    get_resp = cl.get("/mcp/server-card", headers={"HX-Request": "true"})
    assert get_resp.status_code == 200
    assert get_resp.headers["Content-Type"] == protocol.SERVER_CARD_CONTENT_TYPE
    assert get_resp.headers["Cache-Control"] == "public, max-age=600"
    assert get_resp.headers["X-Content-Type-Options"] == "nosniff"
    assert get_resp.headers["Access-Control-Allow-Origin"] == "*"
    assert "Link" not in get_resp.headers

    card = json.loads(get_resp.data.decode("utf-8"))
    assert card["$schema"] == protocol.SERVER_CARD_SCHEMA
    assert card["name"] == "ca.poirierlavoie.athena/pallas-athena"
    assert card["title"] == "Pallas Athéna"
    assert card["remotes"][0]["url"] == "https://athena.poirierlavoie.ca/mcp"
    assert card["remotes"][0]["supportedProtocolVersions"] == list(
        mcp_pkg.SUPPORTED_PROTOCOL_VERSIONS
    )
    assert card["remotes"][0]["headers"][0]["isSecret"] is True

    head_resp = cl.head("/mcp/server-card")
    assert head_resp.status_code == 200
    assert head_resp.data == b""
    assert head_resp.headers["Cache-Control"] == "public, max-age=600"

    opt_resp = cl.options("/mcp/server-card")
    assert opt_resp.status_code == 204
    assert opt_resp.headers["Access-Control-Allow-Origin"] == "*"
    assert opt_resp.headers["Access-Control-Allow-Methods"] == "GET, HEAD, OPTIONS"

    for method in ("POST", "DELETE"):
        bad = cl.open("/mcp/server-card", method=method)
        assert bad.status_code == 405
        assert bad.headers["Allow"] == "GET, HEAD, OPTIONS"
        assert "no-store" in bad.headers["Cache-Control"]
