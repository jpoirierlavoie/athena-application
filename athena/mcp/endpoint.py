"""POST /mcp — stateless, JSON-response-mode MCP Streamable HTTP endpoint.

One JSON-RPC 2.0 message per POST; every response is a single
``application/json`` body (no SSE streams, no ``Mcp-Session-Id``, no
server-initiated messages). Notifications are acknowledged with an empty
202. ``GET``/``DELETE`` fall through to Flask's automatic 405.
"""

import json
import time
from typing import Any, Optional

from flask import Response, current_app, jsonify, request

from config import Config
from mcp import (
    DEFAULT_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    mcp_bp,
)
from mcp import disclosure, jsonrpc, protocol, tools
from mcp.bearer import (
    ScopeRequired,
    granted_scopes,
    insufficient_scope_response,
    mcp_auth_required,
    revalidate_for_write,
)
from mcp.tools import CommittedWriteError, ToolArgumentError
from security import limiter
from utils.logging_setup import log_mcp_event, log_unexpected, sanitize_log_value
from utils.tracing_setup import span

# Instructions surfaced to the client model at initialize — ASSEMBLED from
# the disclosure registry (mcp/disclosure.py): the counts, the families and
# the « never » statements are derived, so a lot that adds a family or lifts
# a promise changes the registry and this text follows. Recopied by hand,
# the counts went stale twice and the undo path stated a falsehood (« frees
# the number »). ONE text for every token since 2026-10-05: the per-token
# accounting variant left with the athena:comptabilite scope.
INSTRUCTIONS = disclosure.build_instructions(tools.TOOLS)


SERVER_INFO = {
    "name": "pallas-athena",
    "title": "Pallas Athéna",
    "version": "2.0.0",
}


def _resolve_protocol_version(
    method: str, params: dict[str, Any], request_id: jsonrpc.RequestId
) -> tuple[Optional[str], Optional[Response]]:
    """Negotiate the MCP-Protocol-Version header (absent → 2025-03-26)."""
    header = request.headers.get("MCP-Protocol-Version")
    if header is None:
        if not protocol.absent_version_allowed(
            method, serves_pre_2025_06_18=True
        ):
            resp = jsonify(
                jsonrpc.error_response(
                    request_id,
                    protocol.HEADER_MISMATCH,
                    "En-tête « MCP-Protocol-Version » manquant.",
                )
            )
            resp.status_code = 400
            return None, resp
        req_ver = params.get("protocolVersion")
        if isinstance(req_ver, str) and req_ver in SUPPORTED_PROTOCOL_VERSIONS:
            return req_ver, None
        return DEFAULT_PROTOCOL_VERSION, None
    version, ver_err = protocol.negotiate_version(
        header, SUPPORTED_PROTOCOL_VERSIONS
    )
    if ver_err is not None:
        resp = jsonify({"jsonrpc": "2.0", "id": request_id, "error": ver_err})
        resp.status_code = 400
        return None, resp
    return version, None


@mcp_bp.route("/mcp/server-card", methods=["GET", "HEAD", "OPTIONS"])
@limiter.limit("60 per minute")
def mcp_server_card() -> Response:
    """Public SEP-2127 discovery document (zero secrets or client PII)."""
    if request.method == "OPTIONS":
        return Response(
            "",
            status=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                "Access-Control-Allow-Headers": "Accept, Content-Type",
                "Access-Control-Max-Age": "86400",
            },
        )
    origin = current_app.config.get(
        "MCP_CANONICAL_ORIGIN", Config.MCP_CANONICAL_ORIGIN
    )
    card = protocol.build_server_card(
        origin=origin,
        supported_versions=SUPPORTED_PROTOCOL_VERSIONS,
        server_info=SERVER_INFO,
    )
    body = "" if request.method == "HEAD" else json.dumps(card, ensure_ascii=False)
    return Response(
        body,
        status=200,
        headers={
            "Content-Type": protocol.SERVER_CARD_CONTENT_TYPE,
            "Cache-Control": "public, max-age=600",
            "X-Content-Type-Options": "nosniff",
            "Access-Control-Allow-Origin": "*",
        },
    )


@mcp_bp.route("/mcp/server-card", methods=["POST", "PUT", "DELETE", "PATCH"])
def mcp_server_card_method_not_allowed() -> Response:
    """Restrict /mcp/server-card to read-only discovery verbs."""
    resp = jsonify({"error": "method_not_allowed"})
    resp.status_code = 405
    resp.headers["Allow"] = "GET, HEAD, OPTIONS"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@mcp_bp.route("/mcp", methods=["GET", "DELETE"])
def mcp_method_not_allowed() -> Response:
    """No SSE stream (GET), no sessions to delete (DELETE) — §9.1.

    Registered explicitly rather than relying on Flask's automatic 405, so
    the answer carries ``Allow: POST`` and the JSON error body.
    """
    resp = jsonify({"error": "method_not_allowed"})
    resp.status_code = 405
    resp.headers["Allow"] = "POST"
    return resp


@mcp_bp.route("/mcp", methods=["POST"])
@limiter.limit("240 per minute")
@mcp_auth_required
def mcp_endpoint() -> Any:
    try:
        message = jsonrpc.parse_message(request.get_data())
    except jsonrpc.JsonRpcError as exc:
        return jsonify(jsonrpc.error_response(exc.request_id, exc.code, exc.message))

    if jsonrpc.is_notification(message):
        # notifications/initialized, notifications/cancelled, …
        return "", 202

    request_id = message["id"]
    method = message["method"]
    params = message.get("params") or {}

    protocol_version, version_error = _resolve_protocol_version(
        method, params, request_id
    )
    if version_error is not None:
        return version_error
    assert protocol_version is not None

    if protocol.is_modern(protocol_version) and method != "initialize":
        header_err = protocol.validate_modern_headers(
            request.headers, method, params
        )
        if header_err is not None:
            resp = jsonify(
                jsonrpc.error_response(
                    request_id, protocol.HEADER_MISMATCH, header_err
                )
            )
            resp.status_code = 400
            return resp

    with span("mcp.request", method=method):
        try:
            result = _dispatch(method, params, request_id, protocol_version)
        except jsonrpc.JsonRpcError as exc:
            return jsonify(
                jsonrpc.error_response(request_id, exc.code, exc.message)
            )
        except ScopeRequired as exc:
            # MUST precede `except Exception` below: caught there, an
            # authorization refusal would become a 200 "internal error" with
            # no 403 and no WWW-Authenticate step-up signal — and would be
            # indistinguishable from a Firestore outage in the logs.
            log_mcp_event(
                "mcp_write_refused",
                "refused",
                tool=exc.tool or None,
                reason="insufficient_scope",
            )
            return insufficient_scope_response(exc.scope)
        except Exception:
            log_unexpected("mcp request dispatch failed")
            return jsonify(
                jsonrpc.error_response(
                    request_id, jsonrpc.INTERNAL_ERROR, "Internal error"
                )
            )
    return jsonify(jsonrpc.result_response(request_id, result))


def _dispatch(
    method: str,
    params: dict,
    request_id: jsonrpc.RequestId,
    protocol_version: str,
) -> dict:
    modern = protocol.is_modern(protocol_version)
    if method == "server/discover":
        return protocol.build_discovery_result(
            supported_versions=SUPPORTED_PROTOCOL_VERSIONS,
            capabilities={"tools": {"listChanged": False}},
            server_info=SERVER_INFO,
            instructions=INSTRUCTIONS,
            ttl_ms=3_600_000,
            cache_scope="private",
        )
    if method == "initialize":
        return _initialize(params)
    if method == "ping":
        return protocol.wrap_modern_result(
            {}, modern=modern, server_info=SERVER_INFO
        )
    if method == "tools/list":
        # Private cache scope only: advertised tools depend on token scopes.
        return protocol.wrap_modern_result(
            {"tools": tools.list_tool_descriptors(granted_scopes())},
            modern=modern,
            ttl_ms=3_600_000,
            cache_scope="private",
            server_info=SERVER_INFO,
        )
    if method == "tools/call":
        return protocol.wrap_modern_result(
            _tools_call(params, protocol_version),
            modern=modern,
            server_info=SERVER_INFO,
        )
    raise jsonrpc.JsonRpcError(
        jsonrpc.METHOD_NOT_FOUND, f"Method not found: {method}", request_id
    )


def _initialize(params: dict) -> dict:
    negotiated = protocol.negotiate_initialize(
        params.get("protocolVersion"), SUPPORTED_PROTOCOL_VERSIONS
    )
    client_info = params.get("clientInfo") or {}
    log_mcp_event(
        "mcp_initialize",
        "success",
        client_name=sanitize_log_value(str(client_info.get("name", ""))[:80]),
        client_version=sanitize_log_value(str(client_info.get("version", ""))[:40]),
        protocol_version=negotiated,
    )
    return {
        "protocolVersion": negotiated,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": dict(SERVER_INFO),
        "instructions": INSTRUCTIONS,
    }


# A REFUSED call's `dossier_id` is still the caller's string: the schema
# bounds it to 64 characters and nothing more, and the commonest handler
# refusal is precisely « dossier introuvable ». A title or a party name
# pasted into the argument must not reach the refusal log, where the
# RedactionFilter does not scrub names. The shape check lives in mcp.tools
# since the write protocol needs the same one for its stored records.
_loggable_id = tools.loggable_id


def _log_write_refused(name: str, reason: str, **fields: Any) -> None:
    """Log a refused WRITE call: the tool, a reason CODE, ids and counts.

    Never the refusal's text — schema errors quote argument names the
    caller chose, and handler refusals describe user-supplied content (a
    note's text, a party's name) that the RedactionFilter does not scrub.
    A read tool's refusal is not logged here: `mcp_write_refused` is the
    write audit's counterpart, and a bad read argument changes nothing.
    """
    if name in tools.WRITE_TOOLS:
        log_mcp_event(
            "mcp_write_refused", "refused", tool=name, reason=reason, **fields
        )


def _tools_call(params: dict, protocol_version: str) -> dict:
    name = params.get("name")
    if not isinstance(name, str) or name not in tools.TOOLS:
        return tools.error_result(
            f"Unknown tool: {sanitize_log_value(str(name))[:80]}"
        )

    # Authorization BEFORE argument validation and before any handler runs,
    # so a refused write never touches the model layer.
    needed = tools.required_scope(name)
    if needed not in granted_scopes():
        raise ScopeRequired(needed, name)
    if name in tools.WRITE_TOOLS:
        # Re-read the live token: the bearer success cache is a read-path
        # optimization and must not let a revoked token mutate the file.
        # `needed` is the tool's own scope (athena:write for every write),
        # so the live token must still carry it.
        revalidate_for_write(needed, name)

    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        _log_write_refused(name, "schema_invalid", error_count=1)
        return tools.error_result("arguments must be an object")

    schema = tools.TOOLS[name]["input_schema"]
    validation_errors = tools.validate_args(schema, arguments)
    if validation_errors:
        # The COUNT only: the messages quote argument names the caller
        # chose, and an unvalidated `dossier_id` is not an id yet.
        _log_write_refused(
            name, "schema_invalid", error_count=len(validation_errors)
        )
        return tools.error_result("; ".join(validation_errors))

    # The span attribute and every `mcp_tool_call` line carry the dossier
    # only when it is SHAPED like an id. On any path — success included (a
    # read tool answers an unknown dossier with an empty list, not a
    # refusal) — the argument is still the caller's string, bounded to 64
    # characters by the schema and nothing more: a title or a party name
    # pasted into it would reach Cloud Trace and the tool-call log, which
    # the RedactionFilter does not scrub for names (OBSERVABILITY.md:
    # `dossier_id` span attributes are « UUIDs only »).
    dossier_id = _loggable_id(arguments.get("dossier_id"))
    span_attrs: dict[str, Any] = {}
    if dossier_id:
        span_attrs["dossier_id"] = dossier_id

    handler = tools.get_handler(name)
    started = time.perf_counter()
    argument_error: Optional[str] = None
    argument_reason = tools.DEFAULT_REFUSAL_REASON
    committed_error: Optional[CommittedWriteError] = None
    image: Optional[tools.ImageAttachment] = None
    try:
        with span(f"mcp.tool.{name}", **span_attrs):
            # ToolArgumentError is caught INSIDE the span. `span()` calls
            # record_exception + set_status(str(exc)) on anything crossing
            # its boundary, and these messages describe user-supplied
            # content — letting one through would ship a fragment of a
            # privileged note to Cloud Trace, which the exporter's
            # attribute scrubbing does not cover (it sanitizes attributes,
            # not exception events).
            try:
                payload = handler(arguments)
                # An image leaves the payload here, before it is serialized
                # — only for a tool allowed one (tools.IMAGE_RESULT_TOOLS);
                # any other raises, inside the span: « internal error ».
                payload, image = tools.split_image(name, payload)
            except ToolArgumentError as exc:
                argument_error = exc.client_message
                argument_reason = exc.reason
                payload = None
            except CommittedWriteError as exc:
                # A write that COMMITTED, then failed. Caught here and not
                # by the blanket `except` below, which would answer
                # « internal error » — a retryable failure, so the caller
                # would retry and write twice. Inside the span for the same
                # reason as above: its own fields are ids, but its
                # `__cause__` is the original exception, whose text may
                # describe content — and record_exception would export the
                # chain. run_write has already logged the traceback
                # (`unexpected`) and recorded the entry `partial`.
                committed_error = exc
                payload = None
    except Exception:
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        log_unexpected("mcp tool execution failed", tool=name)
        log_mcp_event(
            "mcp_tool_call",
            "failure",
            tool=name,
            duration_ms=duration_ms,
            **({"dossier_id": dossier_id} if span_attrs else {}),
        )
        # Execution errors are tool RESULTS, not protocol errors (MCP spec).
        return tools.error_result(
            "Tool execution failed due to an internal error."
        )

    if committed_error is not None:
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        # Ids, a collection name and counts only — never content. No
        # `mcp_write` success line: the call did not succeed, and the audit
        # line that says « written » would mislead a reader counting writes.
        partial_fields = {
            "entity_id": _loggable_id(committed_error.entity_id),
            "dossier_id": _loggable_id(committed_error.dossier_id),
            "collection": committed_error.collection or None,
        }
        log_mcp_event(
            "mcp_write_partial",
            "failure",
            tool=name,
            rows=committed_error.rows,
            idempotent_replay=committed_error.replay,
            **{k: v for k, v in partial_fields.items() if v is not None},
        )
        log_mcp_event(
            "mcp_tool_call",
            "failure",
            tool=name,
            duration_ms=duration_ms,
            **({"dossier_id": dossier_id} if span_attrs else {}),
        )
        # A tool RESULT with isError — not a -32602, which promises that
        # nothing was written. The message says the opposite, loudly — and
        # it is the server-authored sentence, never the exception rendered.
        return tools.error_result(committed_error.client_message)

    if argument_error is not None:
        # The refusal is LOGGED by its reason code — the text below reaches
        # the client only. OBSERVABILITY.md promised an `mcp_write_refused`
        # burst as the stop-the-import signal long before either of these
        # refusal paths emitted one. The dossier id only when id-shaped
        # (`dossier_id` above is already the shape-checked value): a
        # refused call's argument has been checked for length, not for
        # being an id.
        _log_write_refused(
            name,
            argument_reason,
            **({"dossier_id": dossier_id} if dossier_id else {}),
        )
        # Returned as an isError tool result (outside the span, so user-derived
        # text never reaches Cloud Trace), allowing the caller model to read
        # the refusal and recover.
        return tools.error_result(argument_error)

    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    if name in tools.WRITE_TOOLS:
        # Generalized write audit (WP15): entity ids and counts only, never
        # a title or content. `entity` is the WP16+ shape; the two note
        # tools keep their historical `note` key and ALSO keep emitting the
        # original mcp_note_written event for log-metric continuity.
        entity = (payload or {}).get("entity") or (payload or {}).get("note") or {}
        replay = bool((payload or {}).get("idempotent_replay"))
        common = {
            "tool": name,
            "dossier_id": entity.get("dossier_id") or None,
            "entity_id": entity.get("id") or None,
            # A replay means « nothing new was written » — the audit line
            # must say so. (`dry_run` sat beside it until 2026-08-27, when
            # the preview left the write protocol.)
            "idempotent_replay": replay,
            # The bump itself, NOT dav_synced — a closed dossier bumps
            # correctly but is never advertised to DavX5, and conflating the
            # two would make a healthy write look like a sync failure.
            # A replay hands back the FIRST call's stored payload, `true`
            # included, while bumping nothing: logged as-is, every replay
            # claimed a CTag bump that never happened.
            "ctag_bumped": bool((payload or {}).get("ctag_bumped")) and not replay,
            "dav_synced": bool((payload or {}).get("dav_synced")),
        }
        log_mcp_event("mcp_write", "success", **common)
        if (payload or {}).get("note"):
            note = (payload or {}).get("note") or {}
            log_mcp_event(
                "mcp_note_written",
                "success",
                tool=name,
                dossier_id=note.get("dossier_id") or None,
                note_id=note.get("id") or None,
                content_chars=note.get("content_length"),
                ctag_bumped=common["ctag_bumped"],
                dav_synced=common["dav_synced"],
            )
    log_mcp_event(
        "mcp_tool_call",
        "success",
        tool=name,
        duration_ms=duration_ms,
        **({"dossier_id": dossier_id} if span_attrs else {}),
    )
    return tools.tool_result(payload, protocol_version, image=image)
