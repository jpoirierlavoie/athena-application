"""MCP Streamable HTTP 2026-07-28 protocol helpers & SEP-2127 server card.

Python counterpart of ``@poirierlavoie/mcp-socle`` (#v0.7.0) for Pallas
Athena's stateless JSON-response-mode ``/mcp`` endpoint:

* Generic protocol version negotiation (``2026-07-28`` boundary, structured
  ``-32022`` error carrying ``data.supported``);
* Modern anti-smuggling header concordance (``MCP-Protocol-Version``,
  ``Mcp-Method``, ``Mcp-Name`` with strict UTF-8 ``=?base64?…?=`` sentinel,
  and ``params._meta`` version check) yielding ``-32020``;
* Per-request ``_meta`` extraction, ``resultType: "complete"`` + cache hints
  (``cacheScope: "private"`` on scope-filtered ``tools/list`` and
  ``server/discover``), and ``server/discover`` payload assembly;
* Public SEP-2127 discovery card builder (``/mcp/server-card``) carrying zero
  secrets or client PII.

Pure module: imports only stdlib and ``mcp.FIRST_MODERN_PROTOCOL_VERSION``.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from mcp import FIRST_MODERN_PROTOCOL_VERSION

#: JSON-RPC error code for unsupported MCP protocol versions.
UNSUPPORTED_PROTOCOL_VERSION = -32022
#: JSON-RPC error code for HTTP header vs JSON-RPC body mismatches.
HEADER_MISMATCH = -32020

_META_PREFIX = "io.modelcontextprotocol/"
META_PROTOCOL_VERSION = f"{_META_PREFIX}protocolVersion"
META_CLIENT_INFO = f"{_META_PREFIX}clientInfo"
META_CLIENT_CAPABILITIES = f"{_META_PREFIX}clientCapabilities"
META_SERVER_INFO = f"{_META_PREFIX}serverInfo"

SERVER_CARD_SCHEMA = (
    "https://static.modelcontextprotocol.io/schemas/v1/server-card.schema.json"
)
SERVER_CARD_CONTENT_TYPE = "application/mcp-server-card+json; charset=utf-8"
PUBLIC_SERVER_CARD_DESCRIPTION = (
    "Connecteur MCP privé du logiciel de gestion de cabinet juridique Pallas "
    "Athéna (dossiers, contacts, agenda, documents, gabarits, facturation et "
    "comptabilité en fidéicommis et d'administration)."
)

CACHEABLE_METHODS: tuple[str, ...] = (
    "server/discover",
    "tools/list",
    "prompts/list",
    "resources/list",
    "resources/templates/list",
    "resources/read",
)
METHODS_WITHOUT_REQUIRED_META: frozenset[str] = frozenset(
    {"initialize", "ping", "server/discover"}
)
EXPECTED_NAME_FIELD: dict[str, str] = {
    "tools/call": "name",
    "resources/read": "uri",
    "prompts/get": "name",
}


def is_modern(version: str) -> bool:
    """True when *version* uses sessionless 2026-07-28+ semantics."""
    # ISO YYYY-MM-DD revisions compare lexicographically.
    return version >= FIRST_MODERN_PROTOCOL_VERSION


def unsupported_version_error(
    requested: str,
    supported: Sequence[str],
) -> dict[str, Any]:
    """Structured ``-32022`` error payload advertising supported revisions."""
    return {
        "code": UNSUPPORTED_PROTOCOL_VERSION,
        "message": "Unsupported protocol version",
        "data": {"supported": list(supported), "requested": requested},
    }


def negotiate_version(
    requested: str,
    supported: Sequence[str],
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """Return ``(version, None)`` if served, else ``(None, error_dict)``."""
    if requested in supported:
        return requested, None
    return None, unsupported_version_error(requested, supported)


def absent_version_allowed(
    method: str,
    *,
    serves_pre_2025_06_18: bool = True,
) -> bool:
    """Whether a request lacking ``MCP-Protocol-Version`` may proceed."""
    if method in ("initialize", "server/discover"):
        return True
    return serves_pre_2025_06_18


def negotiate_initialize(
    requested: Any,
    supported: Sequence[str],
) -> str:
    """Negotiate the revision for a legacy ``initialize`` handshake."""
    if not supported:
        raise ValueError("At least one MCP protocol version must be declared.")
    if isinstance(requested, str) and requested in supported:
        return requested
    legacy = next((v for v in supported if not is_modern(v)), None)
    if legacy is not None:
        return legacy
    return supported[-1]


def _get_header(headers: Mapping[str, str], target: str) -> Optional[str]:
    """Read *target* from Flask Headers or a plain dict case-insensitively."""
    direct = headers.get(target)
    if direct is not None:
        return direct
    folded = target.casefold()
    for key, val in headers.items():
        if isinstance(key, str) and key.casefold() == folded:
            return val
    return None


def decode_header_sentinel(value: str) -> Optional[str]:
    """Decode strict UTF-8 ``=?base64?…?=`` HTTP header sentinels.

    Returns plain values unchanged, decoded text on a valid sentinel, or
    ``None`` when the sentinel holds malformed base64 or invalid UTF-8 bytes.
    """
    if not (value.startswith("=?base64?") and value.endswith("?=")):
        return value
    payload = value[len("=?base64?") : -len("?=")]
    try:
        raw_ascii = payload.encode("ascii")
        octets = base64.b64decode(raw_ascii, validate=True)
        return octets.decode("utf-8", errors="strict")
    except (ValueError, UnicodeDecodeError):
        return None


def validate_modern_headers(
    headers: Mapping[str, str],
    method: str,
    params: Optional[dict[str, Any]] = None,
    *,
    require_version_meta: bool = False,
    version_meta: Any = None,
) -> Optional[str]:
    """Verify HTTP routing headers match the JSON-RPC body on modern calls.

    Returns ``None`` when concordant, or a French refusal string for ``400`` +
    ``-32020`` (:data:`HEADER_MISMATCH`). Must only run on modern revisions
    (>= ``2026-07-28``).
    """
    version = _get_header(headers, "MCP-Protocol-Version")
    if version is None:
        return "En-tête « MCP-Protocol-Version » manquant."

    version_meta_present = False
    if version_meta is not None:
        if not isinstance(version_meta, str):
            return "La version annoncée dans « _meta » doit être une chaîne de caractères."
        if version_meta != version:
            return (
                f"« MCP-Protocol-Version » ({version}) ne correspond pas à la "
                f"version du corps ({version_meta})."
            )
        version_meta_present = True

    if params is not None and "_meta" in params and params["_meta"] is not None:
        meta = params["_meta"]
        if not isinstance(meta, dict):
            return "« params._meta » doit être un objet."
        if META_PROTOCOL_VERSION in meta and meta[META_PROTOCOL_VERSION] is not None:
            meta_ver = meta[META_PROTOCOL_VERSION]
            if not isinstance(meta_ver, str):
                return (
                    f"« params._meta[\"{META_PROTOCOL_VERSION}\"] » doit être "
                    "une chaîne de caractères."
                )
            if meta_ver != version:
                return (
                    f"« MCP-Protocol-Version » ({version}) ne correspond pas à "
                    f"la version du corps ({meta_ver})."
                )
            version_meta_present = True

    if require_version_meta and not version_meta_present:
        return f"« _meta[\"{META_PROTOCOL_VERSION}\"] » manquant pour {method}."

    mcp_method = _get_header(headers, "Mcp-Method")
    if mcp_method is None:
        return "En-tête « Mcp-Method » manquant."
    if mcp_method != method:
        return (
            f"« Mcp-Method » ({mcp_method}) ne correspond pas à la méthode du "
            f"corps ({method})."
        )

    field_name = EXPECTED_NAME_FIELD.get(method)
    if field_name is None:
        return None

    raw_name = _get_header(headers, "Mcp-Name")
    if raw_name is None:
        return f"En-tête « Mcp-Name » manquant pour {method}."
    decoded = decode_header_sentinel(raw_name)
    if decoded is None:
        return "« Mcp-Name » porte une sentinelle base64 illisible."

    expected = params.get(field_name) if isinstance(params, dict) else None
    if not isinstance(expected, str):
        return (
            f"« params.{field_name} » est absent du corps, alors que "
            "« Mcp-Name » est présent."
        )
    if decoded != expected:
        return (
            f"« Mcp-Name » ({decoded}) ne correspond pas à "
            f"« params.{field_name} » ({expected})."
        )
    return None


def read_request_meta(params: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Extract sanitized ``_meta`` fields from a request's ``params``."""
    if not isinstance(params, dict):
        return {}
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return {}

    out: dict[str, Any] = {}
    if isinstance(meta.get(META_PROTOCOL_VERSION), str):
        out["protocolVersion"] = meta[META_PROTOCOL_VERSION]

    client_raw = meta.get(META_CLIENT_INFO)
    if isinstance(client_raw, dict):
        info: dict[str, str] = {}
        if isinstance(client_raw.get("name"), str):
            info["name"] = client_raw["name"]
        if isinstance(client_raw.get("version"), str):
            info["version"] = client_raw["version"]
        out["clientInfo"] = info

    caps_raw = meta.get(META_CLIENT_CAPABILITIES)
    if isinstance(caps_raw, dict):
        out["clientCapabilities"] = caps_raw
    return out


def readable_client(client_info: Optional[dict[str, Any]]) -> Optional[str]:
    """Reduce ``clientInfo`` to ``name[/version]`` for safe structured logs."""
    if not isinstance(client_info, dict):
        return None
    name = client_info.get("name")
    if not isinstance(name, str) or not name:
        return None
    version = client_raw_ver = client_info.get("version")
    if isinstance(client_raw_ver, str) and version:
        return f"{name}/{version}"
    return name


def is_cacheable(method: str) -> bool:
    """True when *method* may carry cache hints in modern revisions."""
    return method in CACHEABLE_METHODS


def wrap_modern_result(
    raw: dict[str, Any],
    *,
    modern: bool,
    ttl_ms: Optional[int] = None,
    cache_scope: str = "private",
    server_info: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Attach 2026-07-28 envelope fields when *modern* is True; else unchanged."""
    if not modern:
        return raw
    out = {**raw, "resultType": "complete"}
    if ttl_ms is not None:
        if isinstance(ttl_ms, bool) or not isinstance(ttl_ms, int) or ttl_ms < 0:
            raise ValueError(f"ttl_ms must be a non-negative integer (got {ttl_ms!r})")
        if cache_scope not in ("public", "private"):
            raise ValueError(f"cache_scope must be 'public' or 'private' (got {cache_scope!r})")
        out["ttlMs"] = ttl_ms
        out["cacheScope"] = cache_scope
    if server_info is not None:
        existing_meta = out.get("_meta")
        meta_dict = dict(existing_meta) if isinstance(existing_meta, dict) else {}
        meta_dict[META_SERVER_INFO] = dict(server_info)
        out["_meta"] = meta_dict
    return out


def build_discovery_result(
    *,
    supported_versions: Sequence[str],
    capabilities: dict[str, Any],
    server_info: dict[str, Any],
    instructions: Optional[str] = None,
    ttl_ms: int = 3_600_000,
    cache_scope: str = "private",
) -> dict[str, Any]:
    """Build the ``server/discover`` payload (``serverInfo`` lives in ``_meta``)."""
    base: dict[str, Any] = {
        "supportedVersions": list(supported_versions),
        "capabilities": capabilities,
    }
    if instructions is not None:
        base["instructions"] = instructions
    return wrap_modern_result(
        base,
        modern=True,
        ttl_ms=ttl_ms,
        cache_scope=cache_scope,
        server_info=server_info,
    )


def build_server_card(
    *,
    origin: str,
    supported_versions: Sequence[str],
    server_info: dict[str, Any],
    description: str = PUBLIC_SERVER_CARD_DESCRIPTION,
) -> dict[str, Any]:
    """Build the public SEP-2127 discovery document (zero secrets or PII)."""
    clean_origin = origin.rstrip("/")
    return {
        "$schema": SERVER_CARD_SCHEMA,
        "name": "ca.poirierlavoie.athena/pallas-athena",
        "title": server_info["title"],
        "version": server_info["version"],
        "description": description,
        "websiteUrl": f"{clean_origin}/",
        "repository": {
            "url": "https://github.com/jpoirierlavoie/Pallas-Athena",
            "source": "github",
        },
        "remotes": [
            {
                "type": "streamable-http",
                "url": f"{clean_origin}/mcp",
                "supportedProtocolVersions": list(supported_versions),
                "headers": [
                    {
                        "name": "Authorization",
                        "description": "Jeton d'accès OAuth 2.1 PKCE (« Bearer <jeton> »).",
                        "isRequired": True,
                        "isSecret": True,
                    }
                ],
            }
        ],
    }
