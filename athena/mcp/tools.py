"""MCP tool registry, subset JSON-Schema validator, and output helpers.

The registry maps tool names to their metadata and handler name (resolved
lazily against :mod:`mcp.handlers` to avoid a circular import). Every tool
is read-only (``readOnlyHint``) **except the members of** :data:`WRITE_TOOLS`,
which require the ``athena:write`` scope — the accounting writes
(:data:`ACCOUNTING_WRITE_TOOLS`) included since 2026-10-05, when the lawyer
removed their separate ``athena:comptabilite`` scope and every MCP kill
switch. Every schema sets ``additionalProperties: false``.
"""

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from mcp import SCOPE_READ, SCOPE_WRITE
from mcp import coverage, disclosure
from mcp.output_schemas import OUTPUT_SCHEMAS
from tz import to_mtl
# Pure module (no Firestore at import) — safe to derive enums from, unlike
# models.* (see the literal-enum comment below).
from utils import analyse_taxonomies as _tax
from utils import cron_schedule as _cron
from utils import docx_templatize as _templatize
from utils import civilite as _civilite
from utils import contact_roles as _contact_roles
from utils import kyc as _kyc
from utils import occupations as _occupations
from utils import phases
from utils import recurrence

# ── Money / date formatting (§10.1 conventions) ─────────────────────────

_NBSP = " "


#: The reason a refusal is logged under when its raiser names none.
DEFAULT_REFUSAL_REASON = "argument_refused"


class ToolArgumentError(Exception):
    """Argument-level failure a handler detects beyond the schema
    (bad date string, mutually exclusive params). Maps to JSON-RPC -32602.

    The MESSAGE goes to the client and nowhere else: it describes
    user-supplied content (a note's text, a party's name), so it is kept
    out of spans and logs. ``reason`` is what the refusal is LOGGED under
    (``mcp_write_refused``) — a machine-stable snake_case code, never text.
    Keyword-only, so every existing one-message raise is unchanged and
    logs as :data:`DEFAULT_REFUSAL_REASON`.

    ``keep_claim`` (fixups of lot 3) — the one raise that is NOT a promise
    that nothing was written: the model's write RAISED, and a raise out of
    a commit does not prove the commit did not land
    (``models.invoice.CREATE_OUTCOME_UNCERTAIN``). ``run_write`` then KEEPS
    the call's idempotency claim pending instead of releasing it, so a
    same-key retry is refused (in flight, then « interrompu », which asks
    for a re-read) and can never write a second time. The message says the
    outcome is uncertain; it never says « nothing was written ».
    """

    def __init__(
        self, message: str, *, reason: str = DEFAULT_REFUSAL_REASON,
        keep_claim: bool = False,
    ) -> None:
        self.client_message = str(message)
        super().__init__(self.client_message)
        self.reason = reason
        self.keep_claim = bool(keep_claim)


class CommittedWriteError(Exception):
    """A write COMMITTED, then a later step of the same call failed.

    Raised by :func:`mcp.write_support.run_write` when ``execute`` fails
    AFTER a model noted a commit (``models.provenance.note_commit``) — a
    CTag bump, the re-read of a cascaded step, the payload builder. Before
    it existed that exception reached the endpoint's blanket ``except`` and
    came back as a retryable « internal error », so a caller retried and the
    write happened twice. This one says the opposite, loudly: the write is
    there, do NOT retry, re-read it.

    Deliberately NOT a :class:`ToolArgumentError`: a refusal is a -32602
    that promises nothing was written; this is a tool RESULT (``isError``)
    saying something WAS. Every field is an id, a collection name or a
    count — never content — because the endpoint logs them
    (``mcp_write_partial``) and the message reaches the client verbatim.
    ``replay`` is true when the error is re-raised from a stored ``partial``
    idempotency record rather than from this call's own failure.

    The endpoint answers with :attr:`client_message` — the sentence built
    here from those fields — and never ``str(exc)`` (2026-09-30): the text a
    client reads is the server's own, whatever an exception's string ever
    becomes, and CodeQL's py/stack-trace-exposure no longer has an exception
    rendering to follow into the response.
    """

    def __init__(
        self,
        tool: str,
        entity_id: Optional[str],
        dossier_id: Optional[str],
        *,
        collection: str = "",
        rows: int = 1,
        replay: bool = False,
    ) -> None:
        self.tool = tool
        self.entity_id = entity_id or None
        self.dossier_id = dossier_id or None
        self.collection = collection or ""
        self.rows = int(rows or 0)
        self.replay = bool(replay)
        self.client_message = self._message()
        super().__init__(self.client_message)

    def _message(self) -> str:
        parts = []
        if self.entity_id:
            parts.append(
                f"{self.collection}/{self.entity_id}" if self.collection
                else f"id {self.entity_id}"
            )
        if self.dossier_id:
            parts.append(f"dossier {self.dossier_id}")
        if self.rows > 1:
            parts.append(f"{self.rows} éléments écrits")
        where = f" ({', '.join(parts)})" if parts else ""
        return (
            f"L'écriture est ENREGISTRÉE{where} mais une étape qui la suit a "
            "échoué. NE PAS RÉESSAYER : relisez l'élément pour constater son "
            "état — tout nouvel appel qui ne reprend pas la même "
            "idempotency_key (un appel sans clé compris) l'écrirait une "
            "seconde fois."
        )


# The shape of a server-minted document id (Architecture Rule 6: UUIDv4).
# Fixed-count character classes only — linear, nothing to backtrack over.
_ID_SHAPE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def loggable_id(value: Any) -> Optional[str]:
    """*value* when it is shaped like an id, else None.

    For any identifier that is about to reach a log line or a stored record
    without having been proven to be one: a refused call's ``dossier_id`` is
    still the caller's string (the schema bounds it to 64 characters and
    nothing more), and a title or a party name pasted into it must not reach
    a log the RedactionFilter does not scrub for names. ``fullmatch``, never
    ``match`` + ``$``: ``$`` also matches before a trailing newline.
    """
    if isinstance(value, str) and _ID_SHAPE.fullmatch(value):
        return value
    return None


def format_cents(cents: int) -> str:
    """Integer cents → fr-CA display string, e.g. 1234567 → "12 345,67 $".

    Group separator and the space before ``$`` are U+00A0 (no-break
    space). No locale dependency.
    """
    value = int(cents)
    sign = "-" if value < 0 else ""
    dollars, rem = divmod(abs(value), 100)
    grouped = f"{dollars:,}".replace(",", _NBSP)
    return f"{sign}{grouped},{rem:02d}{_NBSP}$"


def date_str(value: Any) -> Optional[str]:
    """Date-only field (stored midnight UTC) → its UTC calendar date.

    Never route these through ``to_mtl`` — a Montréal conversion shifts a
    midnight-UTC date to the previous day.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def iso_mtl(value: Any) -> Optional[str]:
    """True timestamp → ISO 8601 with offset in America/Montreal."""
    if value is None:
        return None
    if isinstance(value, datetime):
        converted = to_mtl(value)
        return converted.isoformat() if converted else None
    return str(value)


def _jsonable(value: Any) -> Any:
    """Deep-convert a payload to JSON-native types (defensive sweep).

    Handlers pre-serialize their date fields explicitly; any stray
    datetime is a true timestamp and rendered ISO-Montreal.
    """
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return iso_mtl(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ── Image results (2026-11-10) ──────────────────────────────────────────
#
# The lawyer decided that Claude may SEE a photographed pièce justificative:
# ``get_admin_receipt`` hands the picture back as an MCP ``image`` content
# item, AFTER the JSON text item — never inside the payload, so neither the
# text block nor ``structuredContent`` carries bytes, and ``outputSchema``
# describes the metadata alone. The handler returns the image under
# :data:`IMAGE_RESULT_KEY`; the endpoint takes it out (:func:`split_image`)
# before the payload is serialized. Only the tools of
# :data:`IMAGE_RESULT_TOOLS` may: any other — a write above all — raising
# there becomes « internal error », never an image.

IMAGE_RESULT_TOOLS = frozenset({"get_admin_receipt"})
IMAGE_RESULT_KEY = "_image"
IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png"})
#: utils/receipt_image.OUTPUT_MAX_BYTES, as a literal (tests pin the two).
RECEIPT_IMAGE_MAX_BYTES = 700 * 1024


@dataclass(frozen=True)
class ImageAttachment:
    """One image for the result envelope — JPEG or PNG, within the cap."""

    mime_type: str
    data: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if (self.mime_type not in IMAGE_MIME_TYPES
                or not isinstance(self.data, bytes)
                or not 0 < len(self.data) <= RECEIPT_IMAGE_MAX_BYTES):
            raise ValueError("an image attachment out of bounds")


def split_image(tool_name: str, payload: Any) -> tuple[Any, Optional[ImageAttachment]]:
    """*payload* without its image, and the image — only for a tool of
    :data:`IMAGE_RESULT_TOOLS`; any other tool that carries one raises."""
    if not isinstance(payload, dict) or IMAGE_RESULT_KEY not in payload:
        return payload, None
    if tool_name not in IMAGE_RESULT_TOOLS or tool_name in WRITE_TOOLS:
        raise RuntimeError(f"{tool_name} may not return an image")
    payload = dict(payload)
    image = payload.pop(IMAGE_RESULT_KEY)
    if image is not None and not isinstance(image, ImageAttachment):
        raise RuntimeError("an image result is an ImageAttachment")
    return payload, image


def tool_result(payload: Any, protocol_version: str = "", *,
                image: Optional[ImageAttachment] = None) -> dict:
    """Wrap a handler payload in the MCP tools/call result envelope.

    The text block is COMPACT JSON (no indent, no space after a separator):
    every byte is paid in the conversation's context. ``outputSchema`` and
    duplicate ``structuredContent`` were retired on the wire (aligned with
    MCP-Socle) while ``OUTPUT_SCHEMAS`` stays enforced server-side in pytest.
    An *image* (:func:`split_image`) follows the text item, base64, and
    nowhere else.
    """
    _ = protocol_version
    clean = _jsonable(payload)
    result: dict[str, Any] = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    clean, ensure_ascii=False, separators=(",", ":")
                ),
            }
        ],
        "isError": False,
    }
    if image is not None:
        result["content"].append({
            "type": "image",
            "data": base64.b64encode(image.data).decode("ascii"),
            "mimeType": image.mime_type,
        })
    return result


def error_result(message: str) -> dict:
    """Tool execution error as an MCP result (not a JSON-RPC error)."""
    return {"content": [{"type": "text", "text": message}], "isError": True}


# ── Subset JSON-Schema validator (§10.2) ────────────────────────────────

def validate_args(schema: dict, args: Any) -> list[str]:
    """Validate a value against a subset JSON Schema; return error strings.

    Supported keywords: ``type`` (object, string, integer, number, boolean,
    array, null — or a LIST of those for nullable fields), ``properties``,
    ``required``, ``enum``, ``minimum``, ``maximum``, ``maxLength``,
    ``minLength``, ``minItems``, ``maxItems``, ``items`` (a single schema,
    applied to every element — recursively, so an object inside an array is
    validated in full), ``anyOf``, ``additionalProperties: false``. Empty
    list = valid.

    Despite the name, this validates OUTPUT payloads too: the conformance
    tests run every handler and check its real payload against the declared
    ``outputSchema`` with this same validator, so a schema the validator
    cannot express cannot be declared — the contract and its enforcement
    use one grammar.

    ⚠ An UNLISTED keyword is not an error: it is silently ignored. That is
    how ``minItems``/``maxItems`` sat declared on the bulk phase tools,
    advertised to the client and enforced by nothing but the handler,
    until 2026-09-25. Implement a keyword here before a schema relies on it.
    The same holds for three FORMS of listed keywords: an unknown ``type``
    name accepts any value, an ``additionalProperties`` SCHEMA (anything
    but ``false``) is ignored, and a tuple-form ``items`` list crashes the
    walk. ``tests/test_mcp_tools.py`` refuses all of these in every
    declared schema.

    A field below the top level is named by its PATH — ``entries[12].hours``,
    ``adjustment.label`` — never by its bare key. Until 2026-09-30 an array
    item's fields came back bare (« `hours` is required »), which on a batch
    CREATOR left the caller no way to find the row: a new row has no id, so
    its index is the only handle, and the handler that adds one never runs
    when this check refuses first (``endpoint._tools_call`` runs it before
    the handler). Top-level arguments stay bare (« `days` must be … »).
    """
    return _validate_value(schema, args, "arguments", root=True)


def _type_ok(expected: Any, value: Any) -> bool:
    if isinstance(expected, (list, tuple)):
        # JSON Schema union types — used by output schemas for nullable
        # fields (e.g. ["string", "null"]); input schemas stay single-typed.
        return any(_type_ok(e, value) for e in expected)
    if expected == "null":
        return value is None
    if expected == "object":
        return isinstance(value, dict)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    return True


def _nearest_argument(key: str, properties: dict) -> str:
    """The supported name a bad one was probably meant to be, or "".

    Deliberately narrow — a prefix relation either way, case-folded. That
    covers the shapes actually observed (a spurious suffix, a truncation,
    a case slip) without the false confidence of an edit-distance guess:
    naming the WRONG neighbour would send the caller off to fix a field it
    never meant.
    """
    plie = key.casefold()
    candidats = [
        nom for nom in properties
        if plie.startswith(nom.casefold()) or nom.casefold().startswith(plie)
    ]
    if not candidats:
        return ""
    # Ne suggérer QUE si les candidats forment une chaîne — le plus long
    # étend tous les autres. Sinon ils divergent, et le plus long n'est
    # qu'un frère parmi d'autres : « dossier » propose alors
    # `dossier_status` là où l'appelant voulait `dossier_id`, et suivre la
    # suggestion pose un filtre qui RÉTRÉCIT la portée en silence. Sur les
    # 2 755 cas de suffixe parasite du corpus cette règle ne perd rien, et
    # elle éteint les 24 cas divergents.
    long = max(candidats, key=len)
    plie_long = long.casefold()
    if not all(plie_long.startswith(c.casefold()) for c in candidats):
        return ""
    # Et, quand c'est la CLÉ qui est plus courte, n'accepter que si le
    # candidat la prolonge à une frontière de mot. Sans cela `id` propose
    # `idempotency_key` — un nom sans rapport dont `id` n'est le préfixe que
    # par accident de lettres, et que la garde de sécurité épinglée refuse
    # précisément (un `id` fourni sur un outil d'écriture est une tentative
    # d'écraser un document, pas une faute de frappe).
    if len(plie) < len(plie_long) and plie_long[len(plie)] != "_":
        return ""
    return long


def _validate_value(
    schema: dict, value: Any, name: str, *, root: bool = False,
) -> list[str]:
    errors: list[str] = []

    def _field(key: str) -> str:
        # The path of a property of THIS value: bare at the top level (the
        # caller's own argument names), prefixed below it — see validate_args.
        return key if root else f"{name}.{key}"

    if "anyOf" in schema:
        # Valid when ANY branch accepts the value. Used by output schemas
        # whose payload has several shapes (found/not-found, global/dossier);
        # branches discriminate on an `enum` so a wrong-shape payload cannot
        # accidentally satisfy the other branch.
        #
        # OUTPUT schemas only. Sibling keywords are deliberately ignored on
        # a match (standard JSON Schema applies them conjunctively), so an
        # INPUT schema combining anyOf with `additionalProperties: false`
        # would silently skip that security control — never write one.
        for branch in schema["anyOf"]:
            if not _validate_value(branch, value, name, root=root):
                return errors
        errors.append(f"`{name}` ne correspond à aucune variante admise")
        return errors

    expected_type = schema.get("type")
    if expected_type is not None and not _type_ok(expected_type, value):
        if isinstance(expected_type, (list, tuple)):
            errors.append(
                f"`{name}` doit être de l'un des types : "
                + ", ".join(str(e) for e in expected_type)
            )
            return errors
        if (
            expected_type == "integer"
            and "minimum" in schema
            and "maximum" in schema
        ):
            errors.append(
                f"`{name}` doit être un entier entre "
                f"{schema['minimum']} et {schema['maximum']}"
            )
        else:
            errors.append(f"`{name}` doit être de type {expected_type}")
        return errors

    if value is None:
        # A null that passed the type gate has nothing further to satisfy
        # (never combined with enum/bounds in this codebase's schemas).
        return errors

    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(v) for v in schema["enum"])
        errors.append(f"`{name}` doit valoir l'une des valeurs : {allowed}")
        return errors

    if isinstance(value, bool):
        return errors

    if isinstance(value, (int, float)):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"`{name}` doit être >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"`{name}` doit être <= {schema['maximum']}")

    if isinstance(value, str) and "maxLength" in schema:
        if len(value) > schema["maxLength"]:
            errors.append(
                f"`{name}` doit compter au plus {schema['maxLength']} caractères"
            )

    if isinstance(value, str) and "minLength" in schema:
        # Needed by the write tools: an empty title/content otherwise passes
        # the schema and fails deep in the model with a French string that
        # reads to the client model like a server fault.
        if len(value.strip()) < schema["minLength"]:
            errors.append(
                f"`{name}` doit contenir au moins {schema['minLength']} "
                "caractères non blancs"
            )

    if isinstance(value, list):
        # Counted BEFORE the items are walked, and a count violation ends
        # the check: an oversized array would otherwise be validated item by
        # item and its per-item errors echoed back in one refusal — work and
        # a response both proportional to what the caller chose to send.
        count_errors: list[str] = []
        if "minItems" in schema and len(value) < schema["minItems"]:
            count_errors.append(
                f"`{name}` doit contenir au moins {schema['minItems']} "
                f"élément(s) ({len(value)} fourni(s))"
            )
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            count_errors.append(
                f"`{name}` doit contenir au plus {schema['maxItems']} "
                f"élément(s) ({len(value)} fourni(s))"
            )
        if count_errors:
            errors.extend(count_errors)
            return errors

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(
                _validate_value(schema["items"], item, f"{name}[{index}]")
            )

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    proche = _nearest_argument(key, properties)
                    detail = f"`{_field(key)}` n'est pas un paramètre pris en charge"
                    if proche:
                        detail += f" — vouliez-vous dire `{proche}` ?"
                    if properties:
                        detail += (
                            " (admis : "
                            + ", ".join(f"`{k}`" for k in sorted(properties))
                            + ")"
                        )
                    errors.append(detail)
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"`{_field(key)}` est requis")
        for key, subschema in properties.items():
            if key in value:
                errors.extend(
                    _validate_value(subschema, value[key], _field(key))
                )

    return errors


# ── Schema fragments ────────────────────────────────────────────────────

def _limit(default: int) -> dict:
    return {
        "type": "integer",
        "minimum": 1,
        "maximum": 50,
        "description": f"Nombre maximal d'éléments (défaut {default}, max 50).",
    }


def _id(description: str) -> dict:
    """A UUIDv4-id argument with a PER-USAGE description."""
    return {"type": "string", "maxLength": 64, "description": description}


def _date(description: str) -> dict:
    """A YYYY-MM-DD date argument with a per-usage description."""
    return {"type": "string", "maxLength": 10, "description": description}


def _write_protocol_props() -> dict:
    """idempotency_key — the shared write protocol (WP15)."""
    return {
        "idempotency_key": {
            "type": "string",
            "minLength": 8,
            "maxLength": 128,
            "description": (
                "Clé choisie pour CETTE écriture (rejeu sur 24 h sans seconde "
                "écriture ; refusée si les arguments changent). Passez-en une "
                "à chaque écriture."
            ),
        },
    }


# The CONCURRENCY policy of an edit tool (plan rule 3) — declared per tool as
# the ``"concurrency"`` spec key, beside ``"idempotency"``, and read by the
# guards in tests/test_mcp_framework_guards.py. Every member of EDIT_TOOLS
# declares one; a read tool declares none.
#
# * ``optional`` — ``expected_etag`` is accepted, not demanded. When the
#   caller omits it, the handler still compare-and-sets against the etag it
#   has just read, so a write landing between that read and the commit is
#   refused rather than overwritten. The tool names, in ``"etag_readers"``,
#   the read tools whose rows carry the etag it expects.
# * ``required`` — ``expected_etag`` is demanded (content replacement,
#   outbound actions — plan rule 3), and listed in the input schema's
#   ``required``. ``decide_rendez_vous`` (the outbound one) declares it since
#   lot 1b. ``update_note`` and ``edit_analyse`` stay ``optional`` at the
#   spec level because only SOME of their calls replace content: their
#   handlers demand the etag exactly when they do (a note's ``content``,
#   the théorie's ``operations``/``full``) and refuse its absence.
# * ``exempt`` — the tool accepts no ``expected_etag``; ``"concurrency_reason"``
#   says why, in one sentence a reviewer can check. A BATCH may still carry
#   its rows' versions under another name — ``clear_register_entries``'
#   ``expected_etags``, one per ``tx_ids`` item — and its reason says so.
CONCURRENCY_OPTIONAL = "optional"
CONCURRENCY_REQUIRED = "required"
CONCURRENCY_EXEMPT = "exempt"
CONCURRENCY_POLICIES: tuple[str, ...] = (
    CONCURRENCY_OPTIONAL, CONCURRENCY_REQUIRED, CONCURRENCY_EXEMPT,
)


def _expected_etag_prop(readers: tuple[str, ...]) -> dict:
    """``expected_etag`` — the optimistic-concurrency argument of an edit.

    Named ``expected_etag``, never ``etag``: a property literally called
    ``etag`` is forbidden on every write (test_mcp_tools), since it reads as
    « set the stored etag ». No ``minLength``: ``''`` is what the rows carry
    for a legacy record that never had an etag, and it must be sendable
    back. *readers* are the tools whose rows expose the etag — the same
    tuple the spec declares as ``"etag_readers"``, so the text a model reads
    and the contract the guards check cannot disagree.
    """
    return {
        "expected_etag": {
            "type": "string",
            "maxLength": 64,
            "description": (
                "`etag` de la dernière lecture ("
                + " / ".join(readers)
                + " ou dernière écriture) ; refusé sans écrire si l'élément a changé."
            ),
        },
    }


_PARTIE_ETAG_READERS = ("get_partie", "list_parties")
_DOSSIER_ETAG_READERS = ("get_dossier", "list_dossiers")
_TIME_ENTRY_ETAG_READERS = ("list_time_entries",)
_EXPENSE_ETAG_READERS = ("list_expenses",)
_TASK_ETAG_READERS = ("list_tasks",)
_NOTE_ETAG_READERS = ("get_note", "list_notes")
_PROTOCOL_ETAG_READERS = ("list_protocol_steps",)
_STEP_ETAG_READERS = ("list_protocol_steps", "get_agenda")
_HEARING_ETAG_READERS = ("list_hearings", "get_agenda")
_RENDEZ_VOUS_ETAG_READERS = ("list_hearings",)
_TRANSCRIPT_ETAG_READERS = ("get_meeting_transcripts",)


def _expected_etag_required_when(readers: tuple[str, ...], when: str) -> dict:
    """``expected_etag`` for a tool that DEMANDS it on some calls only."""
    prop = _expected_etag_prop(readers)
    prop["expected_etag"]["description"] = (
        f"REQUIS {when}. " + prop["expected_etag"]["description"]
    )
    return prop


def _expected_etag_only_for(readers: tuple[str, ...], which: str) -> dict:
    """``expected_etag`` for a tool whose OTHER calls have no version."""
    prop = _expected_etag_prop(readers)
    prop["expected_etag"]["description"] = (
        f"{which} seulement. " + prop["expected_etag"]["description"]
    )
    return prop


def _offset() -> dict:
    """Offset paging for the fully-materialized list tools (G07)."""
    return {
        "type": "integer",
        "minimum": 0,
        "maximum": 5000,
        "description": (
            "Décalage pour pagination par page (`next_offset`, défaut 0 ; "
            "non figé entre appels)."
        ),
    }


def _cursor(note: str = "") -> dict:
    """Keyset paging — stable alternative to `offset`."""
    return {
        "type": "string",
        "maxLength": 400,
        "description": (
            "Curseur `next_cursor` de la page précédente (omettre à la page 1 ; "
            "stable entre insertions)." + (" " + note if note else "")
        ),
    }


def _updated_since() -> dict:
    """Change-window argument (Montréal day or ISO-8601)."""
    return {
        "type": "string",
        "maxLength": 35,
        "description": (
            "Modifiés depuis ce moment (AAAA-MM-JJ Montréal ou ISO-8601 ; "
            "inclut les synchros techniques : candidats à vérifier)."
        ),
    }

_READ_ONLY_ANNOTATIONS = {"readOnlyHint": True, "openWorldHint": False}
# Per the MCP spec, destructiveHint defaults to TRUE and idempotentHint to
# FALSE once readOnlyHint is false — both must be stated explicitly or the
# client over-warns on a purely additive call.
_WRITE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,   # additive by default: see EDIT_TOOLS below
    "idempotentHint": False,    # a second call creates/appends again
    "openWorldHint": False,
}

# Which tools mutate. DERIVED from the disclosure registry
# (mcp/disclosure.FAMILIES): a write tool belongs to exactly one family, and
# the family is what describes it — in INSTRUCTIONS and on the consent
# screen — so a new write tool cannot ship without declaring itself AND
# being disclosed. Enforcement (mcp/endpoint.py) and advertisement
# (list_tool_descriptors) both derive from this set. The literal pin in
# tests/test_mcp_tools.test_write_tools_set_is_pinned stays as a second
# tripwire, and tests/test_mcp_framework_guards (b) checks that membership
# and each tool's declared scope are the same fact.
WRITE_TOOLS: frozenset[str] = disclosure.write_tools()

# Writes that REPLACE a stored value rather than adding one. Lot Q ended the
# era where destructiveHint could be a family constant: « destructive » in
# the MCP spec means « may perform destructive updates », which is exactly
# what an edit does. Under-warning here is worse than over-warning — a
# client uses the hint to decide whether to confirm with the user first.
# Derived into the annotations below, never restated per tool.
EDIT_TOOLS: frozenset[str] = frozenset({
    # Il REMPLACE la catégorie stockée du document — et cette catégorie
    # est ce que l'explorateur affiche et ce sur quoi le juriste filtre.
    # Le journal garde la précédente, mais un remplacement reste un
    # remplacement : sous-avertir est le mauvais côté de l'erreur.
    "record_document_analysis",
    "update_partie", "update_dossier",
    "update_time_entry", "update_expense",
    # import_invoice ne remplace aucune valeur, mais il BASCULE N sources à
    # « facturée » — après quoi les deux modèles refusent toute modification
    # ET toute suppression, jusqu'à l'annulation de la facture
    # (update_invoice depuis le lot 3b, ou l'application), qui ne rend
    # jamais son numéro : sous-avertir ici serait le pire endroit.
    "import_invoice",
    # Un reclassement REMPLACE le code stocké. Qu'il ne puisse pas déplacer
    # un montant ne le rend pas additif : le client se sert de l'indice pour
    # décider s'il confirme, et sous-avertir reste le mauvais côté de
    # l'erreur.
    "set_time_entry_phase_bulk", "set_expense_phase_bulk",
    # complete_task REPLACES a task's stored status — and, through the
    # model's cascade, the linked protocol step's, up to closing the whole
    # protocol. It was left out while it was « the only status change »,
    # which under-warned: a client uses the hint to decide whether to
    # confirm with the user first (plan lot 0a, disclosure step).
    "complete_task",
    # Lot 1b — the agenda edits. Each REPLACES a stored value: a task's
    # fields, a task's status (and, by the cascade, its step's and
    # protocol's), a note's fields or whole body, blocs of the théorie de la
    # cause. The replaced prose of a note is kept (revisions); the rest is
    # not — which is why the hint matters.
    "update_task", "reopen_task", "update_note", "edit_analyse",
    # Lot 1b (L6) — the protocol edits. Each REPLACES a stored value: a
    # protocol's fields or status (and, by a new start date, the template
    # deadlines and the linked tasks' due dates), a step's fields or
    # status (and, by the cascade, its task's and its protocol's). The
    # two creators (create_protocol, add_protocol_step) replace nothing.
    "update_protocol", "update_protocol_step",
    # Lot 1b (L7) — the calendar. update_hearing REPLACES an event's fields
    # (its slot, its status — annulée removes the Outlook copy —, its
    # notes, which are not kept) and can move it or take it out of its
    # series — on a CONFIRMED Bookings rendez-vous only its dossier and its
    # notes (D10, 2026-09-27). decide_rendez_vous REPLACES a Bookings
    # request's decision gate, and its refusal cancels the client's Outlook
    # meeting: the one write here whose effect leaves the building
    # (OUTBOUND_TOOLS below).
    "update_hearing", "decide_rendez_vous", "decide_meeting_transcript",
    # Lot 2A (T7) — FILES. update_document REPLACES a document's filing
    # fields (its name, date, tags, folder, a presumed category) — none kept;
    # move_documents REPLACES each row's folder; manage_folder REPLACES a
    # folder's name or parent (its create replaces nothing, but one tool
    # carries one hint, and under-warning is the wrong side).
    "update_document", "move_documents", "manage_folder",
    # Lot 2A (T9) — finalize_upload's gabarit « replace » mode REPLACES a
    # stored template's file (the version in force: every future letter is
    # printed from it — the previous one is KEPT, restorable in the
    # application). Its document branch replaces nothing; one tool carries
    # one hint, and under-warning is the wrong side (review of lot 2).
    "finalize_upload",
    # Lot 2A (T10) — update_template REPLACES a template's name,
    # description, category or kind (none kept), or its file in force (the
    # previous version KEPT, restorable). create_template replaces nothing.
    "update_template",
    # Lot 3b — BILL. create_invoice replaces no value, but it CONSUMES the
    # year's next number for ever and freezes its sources (the import_invoice
    # reason, plus a number no void gives back). update_invoice REPLACES a
    # draft's fields or the invoice's status — its void releases every
    # source and retires the number. create_budget_version SUPERSEDES the
    # reference budget, whose « Estimation » is a client document (every
    # earlier version kept).
    "create_invoice", "update_invoice", "create_budget_version",
    # Lot 4b — DOSSIERS. set_dossier_status REPLACES the stored status — and
    # with it the phone's view of the dossier (its DavX5 collection drained
    # or restored) and its place in the prescription alerts.
    # update_dossier_party REPLACES a party's roles or lawyer, DETACHES a
    # party (a link — the contact stays) or rewrites name snapshots.
    "set_dossier_status", "update_dossier_party",
    # Lot 4b — CONTACTS. update_partie_mandataire REPLACES a representation's
    # kind or notes, or DETACHES it (a link — the mandataire stays).
    # record_kyc_status REPLACES a check's stored status (and appends to its
    # notes) — presumed, never over the lawyer's attestation, yet a
    # replacement all the same: under-warning is the wrong side.
    "update_partie_mandataire", "record_kyc_status",
    # Lot 5b — ACCOUNTING. Every register write is PERMANENT (an entry is
    # never deleted, only reversed — the pair stays for good) and moves
    # stored money figures: the account's and the client's balances and —
    # a fee payment, an encaissement, their reversals — an invoice's
    # recorded payment and status. update_admin_entry REPLACES an entry's
    # fields; clear_register_entries REPLACES a status (and, at trust,
    # releases the client's funds); reverse_register_entry turns an entry
    # annulée or mints its counter-movement. The two recorders replace no
    # field of their own, but each writes a balance, and possibly a
    # payment, that no call takes back: under-warning here is the worst
    # place.
    "record_trust_entry", "record_admin_entry", "update_admin_entry",
    "clear_register_entries", "reverse_register_entry",
    # Courrier Phase 4 — déplacement et étiquetage Outlook sous Archive > Dossiers.
    "move_courriels",
})

# Writes with an effect OUTSIDE the practice's own records — a message a
# third party receives. MCP's openWorldHint is DERIVED from this set in
# list_tool_descriptors, never restated per tool (the override is refused
# by tests/test_mcp_framework_guards (d)). decide_rendez_vous's refusal
# cancels the Outlook meeting of a « Bookings with me » request, which
# notifies the client: the connector's first outbound effect (plan D10).
# A member must also declare idempotency AND concurrency « required »
# (plan rules 3 and 7), and the guards derive membership from the code: a
# tool whose closure reaches an outbound call is a member, and a member
# that reaches none is refused as a false alarm.
OUTBOUND_TOOLS: frozenset[str] = frozenset({"decide_rendez_vous"})

# Names that PROMISE an edit. A tool whose name starts with one of these must
# be in EDIT_TOOLS (destructiveHint) unless exempted with a reason in
# tests/test_mcp_framework_guards (c); an EDIT_TOOLS member without such a
# name (record_document_analysis, import_invoice, complete_task) is declared
# there with the reason it replaces or irreversibly flips a stored value.
# Deliberately NOT « complete_ » — complete_dossier fills empty fields only.
EDIT_NAME_PREFIXES: tuple[str, ...] = (
    "update_", "set_", "replace_", "rewrite_", "move_", "void_", "reopen_",
    "remove_", "reverse_", "clear_", "cancel_", "refuse_", "confirm_",
    "reschedule_", "unlink_", "detach_", "edit_",
)

# The idempotency POLICY of a write tool — declared per tool as the
# ``"idempotency"`` spec key and read by mcp.write_support.run_write, never
# passed by the handler, so the registry and the protocol cannot disagree.
#
# * ``optional`` — an ``idempotency_key`` is accepted, not demanded. The
#   store fails OPEN: a Firestore blip on the claim must not block a
#   legitimate first write. Every write tool but the 14 below is
#   ``optional``.
# * ``required`` — the key is demanded (a call without one is refused), and
#   the store fails CLOSED: an unreadable claim refuses the call rather than
#   risk a second write, and a failure before any commit keeps the claim
#   ``pending`` so a same-key retry is refused until the caller re-reads.
#   Reserved for money, outbound and series tools (plan rule 7): since lot
#   1b ``create_hearing_series`` (a series) and ``decide_rendez_vous`` (the
#   outbound one) declare it, since lot 3b ``create_invoice`` (a permanent
#   number), since lot 5b the five accounting writes (a register entry is
#   never deleted), the two bulk creators ``create_time_entries_bulk``
#   and ``create_expenses_bulk`` (money: a second batch the next invoice
#   sweeps), ``save_courriels`` (external OneDrive writes and permanent
#   document/registry rows), ``move_courriels`` (Outlook folder/category
#   writes and permanent classification state), and
#   ``record_courriels_temps`` (billable time entries and permanent
#   Courrier links). Such a tool must also list ``idempotency_key`` in its
#   input schema's ``required`` (pinned by test_mcp_framework_guards).
IDEMPOTENCY_OPTIONAL = "optional"
IDEMPOTENCY_REQUIRED = "required"
IDEMPOTENCY_POLICIES: tuple[str, ...] = (IDEMPOTENCY_OPTIONAL, IDEMPOTENCY_REQUIRED)


def idempotency_policy(name: str) -> str:
    """The declared idempotency policy of write tool *name*.

    Absent → ``optional`` (the historical posture). A value outside
    :data:`IDEMPOTENCY_POLICIES` — a typo in the registry — reads as
    ``required``: when the declaration cannot be trusted, the protocol
    takes the side that can never write twice. (The registry guard fails
    the build on such a typo long before it could matter.) An unregistered
    *name* raises ``KeyError``: run_write is keyed by the tool's own
    registered name, and a handler that is not is a bug to surface, not a
    default to guess.
    """
    value = TOOLS[name].get("idempotency", IDEMPOTENCY_OPTIONAL)
    return value if value in IDEMPOTENCY_POLICIES else IDEMPOTENCY_REQUIRED


# Per-call content ceiling, deliberately far below models.note's
# CONTENT_MAX_LENGTH (100_000). Two reasons: an oversized write is refused
# LOUDLY here (-32602) instead of being silently truncated by
# security.sanitize, and the gap leaves room for several appends before a
# note is full. ~20 000 chars ≈ a 3 500-word memo.
CONTENT_MAX_CHARS = 20_000
NOTE_TITLE_MAX_CHARS = 200

# A full REPLACEMENT of a note body (update_note) or of the théorie de la
# cause (edit_analyse `full`) must carry the whole note, so it gets the
# note's own scale — but strictly BELOW models.note.CONTENT_MAX_LENGTH
# (100 000): the handler prepends a one-line dated provenance stamp, and the
# assembled string is what must fit, never a truncated one (pinned by
# tests/test_mcp_agenda_writes.py). 99 000 characters at the worst JSON
# escaping (6 bytes) is ~594 KB — under the 1 MB /mcp request cap.
NOTE_REPLACE_MAX_CHARS = 99_000
# At most one operation per zone of the théorie (the entête + blocs A to H).
ANALYSE_OPERATIONS_MAX = 9

# Phase N — get_document_text: per-call character ceiling. The model PAGES
# through a long document (page_range + next_page) instead of receiving
# megabytes; 40 000 chars ≈ 15-20 dense pages per call.
DOCUMENT_TEXT_MAX_CHARS = 40_000
# Copied exactly from models.note.VALID_CATEGORIES (they are French).
# tests/test_mcp_tools.py pins the two lists against each other. Kept as a
# literal (not derived) because importing models.* runs firestore.Client()
# at module load — see models/__init__.py.
_NOTE_CATEGORIES = [
    "rencontre", "consultation", "analyse", "recherche",
    "stratégie", "vacation", "autre",
]

# Enum values copied exactly from the data model (they are French).
_DOSSIER_STATUSES = ["actif", "en_attente", "fermé", "archivé"]
# Derived, not hand-copied: mcp.coverage is pure (it imports no model), so
# importing it here costs nothing and the enum can never drift from the
# checks that actually run.
_COVERAGE_CODES = coverage.ALL_CODES
_INVOICE_STATUSES = ["brouillon", "envoyée", "payée", "en_retard", "annulée"]
_TASK_STATUSES = ["à_faire", "en_cours", "terminée", "annulée"]
_DOCUMENT_CATEGORIES = [
    "procédure", "pièce", "jugement", "correspondance",
    "déboursé", "facture", "preuve", "procès_verbal",
    "procès_verbal_signification", "procès_verbal_audience",
    "transcription", "mandat", "autre",
]
# Copied exactly from models.doc_template.VALID_KINDS / VALID_CATEGORIES
# (French). A gabarit's category is its OWN narrow taxonomy — never the
# documents one above. tests/test_mcp_template_reads.py pins both pairs; a
# literal, not an import, because importing models.* runs firestore.Client()
# at load.
_TEMPLATE_KINDS = ["gabarit", "note_honoraires", "note_credit", "note", "modele"]
_TEMPLATE_CATEGORIES = ["procédure", "correspondance", "autre"]
# How many folders list_documents(include_folders) returns — a dossier holds
# tens; the cap only bounds a pathological tree (folders_truncated says so).
FOLDER_TREE_MAX = 200
# Lot 2A (T7) — the document and folder edits. Literals copied from the
# models (an import would run firestore.Client() at load);
# tests/test_mcp_file_writes.py pins each against its model constant.
DOCUMENT_NAME_MAX_CHARS = 300      # models.document.DISPLAY_NAME_MAX
DOCUMENT_TAG_MAX_CHARS = 200       # models.document.TAG_MAX
# BELOW models.document.TAGS_MAX_ITEMS (30): the connector's list is the
# lawyer's filing vocabulary, and twenty is already more than a screen shows.
DOCUMENT_TAGS_MAX = 20
DOCUMENT_MOVE_MAX = 50             # models.document.MOVE_BULK_MAX
FOLDER_NAME_MAX_CHARS = 100        # models.folder.MAX_NAME_LENGTH
# The categories an EDIT may set: the vocabulary minus the legacy
# « procès_verbal », still readable and filterable but no longer offered at
# entry (models.document.CATEGORY_CHOICES).
_DOCUMENT_CATEGORY_CHOICES = [c for c in _DOCUMENT_CATEGORIES if c != "procès_verbal"]
# A document's etag is on its list_documents row; a folder's on the
# list_documents `folders` tree (include_folders).
_DOCUMENT_ETAG_READERS = ("list_documents",)
_FOLDER_ETAG_READERS = ("list_documents",)
# Lot 2A (T8) — the generations. Literals copied from their sources
# (services/gabarit_champs and models/document import the Firestore client
# at load); tests/test_mcp_generation.py pins each against its source.
BLOCS_MAX_ITEMS = 12                  # services.gabarit_champs.BLOCS_MAX_ITEMS
CHAMPS_MANUELS_MAX_ITEMS = 12         # …CHAMPS_MANUELS_MAX_ITEMS
BLOC_MAX_CHARS = 20_000               # …BLOC_MAX_CHARS
BLOCS_TOTAL_MAX_CHARS = 60_000        # …BLOCS_TOTAL_MAX_CHARS
CHAMP_MANUEL_MAX_CHARS = 2_000        # …CHAMP_MANUEL_MAX_CHARS
PLACEHOLDER_NAME_MAX_CHARS = 64
# A Markdown document is a whole text, not a bloc — bounded, like the blocs
# of one call, well under the 1 MB /mcp body cap.
MARKDOWN_DOCUMENT_MAX_CHARS = 60_000
DOCUMENT_TITLE_MAX_CHARS = 200
_CREATE_DOCUMENT_SOURCES = ["markdown", "copy", "invoice_note"]

# ── Lot 3b — BILL ───────────────────────────────────────────────────────
# Hand-copied from the models (importing a model here would build a
# Firestore client at load); tests/test_mcp_billing_writes.py pins each
# against its source, so none can drift.
INVOICE_SOURCES_MAX = 200               # each list, and both together
INVOICE_NOTES_MAX_CHARS = 1500          # models.invoice.DRAFT_NOTES_MAX_LENGTH
VOID_REASON_MAX_CHARS = 500             # …VOID_REASON_MAX_LENGTH
# The statuses update_invoice can SET — « annulée » is the void, never a bare
# status write. Never « payée » (only a recorded payment says so) nor
# « brouillon » (an issued invoice is corrected by voiding it).
_INVOICE_TARGET_STATUSES = ["envoyée", "en_retard", "annulée"]
_INVOICE_ETAG_READERS = ("get_invoice",)
BUDGET_HISTORY_MAX = 50
BUDGET_NOTE_MAX_CHARS = 2000            # models.budget._sanitize_data's cap
_BUDGET_MODES = ["replace", "merge"]
# DERIVED from the pure phase module (the _PHASE_CODES precedent): every
# sub-code a budget may carry — ADM and HOR are withdrawn from the client
# quote (D-14), so they are not in the enum at all.
_BUDGET_SOUS_PHASE_CODES = sorted(
    code for code in phases.SOUS_CODES
    if phases.phase_of(code) not in phases.PHASES_NON_FACTURABLES
)


def _invoice_selection_props() -> dict:
    """The selection preview_invoice and create_invoice share."""
    ids = {"type": "string", "maxLength": 64,
           "description": "UUIDv4 de la source."}
    return {
        "dossier_id": _id("Dossier facturé (list_dossiers). Requis."),
        "time_entry_ids": {
            "type": "array", "items": dict(ids),
            "maxItems": INVOICE_SOURCES_MAX,
            "description": (
                "Entrées de temps à facturer (list_time_entries). Avec "
                f"expense_ids : max {INVOICE_SOURCES_MAX} sources au total."),
        },
        "expense_ids": {
            "type": "array", "items": dict(ids),
            "maxItems": INVOICE_SOURCES_MAX,
            "description": "Déboursés à facturer (list_expenses).",
        },
        "all_unbilled": {
            "type": "boolean",
            "description": (
                "true : toutes les sources facturables non facturées du dossier."),
        },
        "date": _date("Date de facture AAAA-MM-JJ (défaut : aujourd'hui Montréal ; "
                      "jamais future)."),
        "due_date": _date("Échéance AAAA-MM-JJ, >= `date` (défaut : date + 1 mois "
                          "civil)."),
    }
# Lot 2A (T9) — the upload ticket. Literals copied from the models (an
# import would run firestore.Client() at load); tests/test_mcp_upload.py
# pins each against its source.
UPLOAD_FILENAME_MAX_CHARS = 200                    # models.upload_ticket.MAX_FILENAME_CHARS
UPLOAD_DOCUMENT_MAX_BYTES = 200 * 1024 * 1024      # models.document.MAX_FILE_SIZE
UPLOAD_TEMPLATE_MAX_BYTES = 10 * 1024 * 1024       # models.doc_template.MAX_TEMPLATE_SIZE
UPLOAD_RECEIPT_MAX_BYTES = 10 * 1024 * 1024        # models.admin_ledger.MAX_RECEIPT_SIZE
UPLOAD_ACCEPT_MAX_ITEMS = 50                       # models.upload_ticket.MAX_LIST_ITEMS
UPLOAD_ACCEPT_MAX_CHARS = 200                      # its per-item ceiling
TEMPLATE_NAME_MAX_CHARS = 120                      # models.doc_template.NAME_MAX
TEMPLATE_DESCRIPTION_MAX_CHARS = 2_000             # models.doc_template.DESCRIPTION_MAX
_UPLOAD_PURPOSES = ["document", "gabarit", "piece_justificative"]
_TEMPLATE_MODES = ["create", "replace"]
# Lot 2A (T10) — a template's etag is on its list_templates row (list mode
# and detail mode alike: both render _template_summary).
_TEMPLATE_ETAG_READERS = ("list_templates",)
# Lot 2B — templatizing. DERIVED from the pure engine (no Firestore at
# import), never copied: the schema and the engine cannot drift.
TEMPLATIZE_SUBSTITUTIONS_MAX = _templatize.MAX_SUBSTITUTIONS
TEMPLATIZE_LITERAL_MIN_CHARS = _templatize.MIN_LITERAL_CHARS
TEMPLATIZE_LITERAL_MAX_CHARS = _templatize.MAX_LITERAL_CHARS
# A name, or the same name written {{name}}.
TEMPLATIZE_PLACEHOLDER_MAX_CHARS = _templatize.MAX_PLACEHOLDER_CHARS + 4
TEMPLATIZE_EXPECTED_MAX = _templatize.MAX_EXPECTED_OCCURRENCES
# The contact roles — the one vocabulary of utils/contact_roles.py, which
# models/partie.py reads too (tests/test_contact_roles.py pins them).
_CONTACT_ROLES = list(_contact_roles.ROLES)
_PARTIE_TYPES = ["individual", "organization"]

# Lot Q. Language, gender and pronouns are declared in models/partie.py and
# NEVER checked by its _validate — the web form constrains them with a
# <select>, the model does not. On the connector's path the schema enum is
# therefore the ONLY guard: without it « gender: banana » persists silently
# onto a vCard. The title and the occupation are checked by the model too
# (2026-10-30); their enums derive from the pure vocabularies.
_PARTIE_PREFIXES = list(_civilite.PREFIXES)
_PARTIE_OCCUPATIONS = list(_occupations.OCCUPATIONS)
_PARTIE_LANGUAGES = ["fr", "en", "es"]
_PARTIE_GENDERS = ["M", "F", "O", "N", "U"]
_PARTIE_PRONOUNS = ["il/lui", "elle", "iel", "he/him", "she/her", "they/them"]

# The six keys of ONE address block. They travel together or not at all —
# see _require_address_bloc in the handlers for why a partial block silently
# relocates a contact.
_ADDRESS_KEYS = ("street", "unit", "city", "province", "postal_code", "country")
_ADDRESS_KEY_FR = {
    "street": "rue",
    "unit": "bureau/app.",
    "city": "ville",
    "province": "province",
    "postal_code": "code postal",
    "country": "pays",
}


def _address_props(prefix: str, which: str) -> dict:
    """The six flat address keys of one block, as fresh dicts."""
    return {
        f"{prefix}_{key}": {
            "type": "string",
            "maxLength": 200,
            "description": (
                f"Adresse {which} — {_ADDRESS_KEY_FR[key]}. Envoyer les 6 clés "
                "du bloc ensemble (sinon défaut Montréal/QC/Canada)."
            ),
        }
        for key in _ADDRESS_KEYS
    }


def _partie_identity_props() -> dict:
    """Identity and contact fields shared by create_partie and update_partie."""
    return {
        "prefix": {
            "type": "string", "enum": _PARTIE_PREFIXES,
            "description": "Civilité (préfixe vCard N).",
        },
        "first_name": {"type": "string", "maxLength": 200,
                       "description": "Prénom (personne physique)."},
        "middle_name": {"type": "string", "maxLength": 200,
                        "description": "Autres prénoms."},
        "last_name": {"type": "string", "maxLength": 200,
                      "description": "Nom de famille — REQUIS pour individual."},
        "suffix": {"type": "string", "maxLength": 100,
                   "description": "Suffixe honorifique (Ad. E., j.c.s.)."},
        "organization_name": {
            "type": "string", "maxLength": 300,
            "description": (
                "Raison sociale — REQUISE pour organization (sert à "
                "display_name, jamais le nom commercial)."
            ),
        },
        "trade_name": {"type": "string", "maxLength": 300,
                       "description": "Nom commercial / « faisant affaires sous »."},
        "governing_law": {"type": "string", "maxLength": 300,
                          "description": "Loi constitutive."},
        "language": {"type": "string", "enum": _PARTIE_LANGUAGES,
                     "description": "Langue de correspondance (vCard LANG)."},
        "gender": {"type": "string", "enum": _PARTIE_GENDERS,
                   "description": "Genre (vCard GENDER)."},
        "pronouns": {"type": "string", "enum": _PARTIE_PRONOUNS,
                     "description": "Pronoms (vCard PRONOUNS)."},
        "occupation": {"type": "string", "enum": _PARTIE_OCCUPATIONS,
                       "description": "Profession/statut (art. 14, hors vCard)."},
        "job_title": {"type": "string", "maxLength": 200,
                      "description": "Poste ou fonction (vCard TITLE)."},
        "organization": {"type": "string", "maxLength": 300,
                         "description": "Employeur ou organisme (vCard ORG)."},
        "email": {"type": "string", "maxLength": 254,
                  "description": "Courriel personnel (normalisé en minuscules)."},
        "email_work": {"type": "string", "maxLength": 254,
                       "description": "Courriel professionnel."},
        "phone_home": {"type": "string", "maxLength": 40,
                       "description": "Téléphone domicile (format E.164)."},
        "phone_cell": {"type": "string", "maxLength": 40,
                       "description": "Cellulaire (format E.164)."},
        "phone_work": {"type": "string", "maxLength": 40,
                       "description": "Téléphone travail (format E.164)."},
        "fax": {"type": "string", "maxLength": 40,
                "description": "Télécopieur (format E.164)."},
        "bar_number": {"type": "string", "maxLength": 60,
                       "description": "Numéro du Barreau (avocat)."},
        "company_neq": {"type": "string", "maxLength": 60,
                        "description": "NEQ du Québec (personne morale)."},
        "notes": {"type": "string", "maxLength": 2000,
                  "description": "Notes libres sur le contact."},
        **_address_props("address", "personnelle"),
        **_address_props("work_address", "professionnelle"),
    }


_PARTY_ROLES = [
    "demandeur", "défendeur", "demandeur reconventionnel",
    "défendeur reconventionnel", "mis en cause", "intervenant",
    "appelant", "intimé", "requérant", "autre",
]


def _party_entry_props() -> dict:
    """One party on a dossier."""
    return {
        "type": "object",
        "properties": {
            "partie_id": _id(
                "UUIDv4 d'un contact EXISTANT (refusé si introuvable)."
            ),
            "roles": {
                "type": "array",
                "items": {"type": "string", "enum": _PARTY_ROLES},
                "description": "Rôles procéduraux (rôle inconnu refusé).",
            },
            "avocat_partie_id": _id(
                "Avocat de cette partie (UUIDv4 d'un contact). Optionnel."
            ),
        },
        "required": ["partie_id"],
        "additionalProperties": False,
    }


def _forum_props() -> dict:
    """Forum fields."""
    return {
        "forum_type": {
            "type": "string",
            "enum": ["judiciaire", "administratif", "federal", "prejudiciaire"],
            "description": (
                "judiciaire (numéro analysé), administratif/federal (organisme "
                "dans `forum`, district vidé) ou prejudiciaire (numéro forcé à "
                "« Préjudiciaire »)."
            ),
        },
        "forum": {
            "type": "string", "maxLength": 40,
            "description": (
                "Code d'organisme administratif/fédéral via "
                "get_reference_vocabulary(kind=\"forums\")."
            ),
        },
        "district_judiciaire": {
            "type": "string", "maxLength": 60,
            "description": "District judiciaire (écarté hors judiciaire/préjudiciaire).",
        },
    }


def _dossier_field_props() -> dict:
    """The classification/financial block shared by complete/create/update_dossier."""
    return {
        "domaine": {"type": "string", "maxLength": 10,
                    "description": "Code famille (get_reference_vocabulary)."},
        "action": {"type": "string", "maxLength": 10,
                   "description": "Code recours (préfixe = domaine)."},
        "action_precision": {"type": "string", "maxLength": 2000,
                             "description": "Précision libre (requise pour « Autre »)."},
        "sommaire": {"type": "string", "maxLength": 5000,
                     "description": "Sommaire libre du dossier (max 5000 car.)."},
        "mandate_type": {"type": "string", "maxLength": 40,
                         "description": "judiciaire | service_conseils | general | special."},
        "court_file_number": {
            "type": "string", "maxLength": 40,
            "description": (
                "Ex. « 500-05-123456-241 » (greffe/juridiction/district "
                "dérivés sur forum judiciaire)."
            ),
        },
        "prescription_type": {"type": "string", "maxLength": 40,
                              "description": "Clé de délai (get_reference_vocabulary)."},
        "fee_type": {"type": "string", "maxLength": 30,
                     "description": "hourly | flat | contingency | mixed | pro_bono | aide_juridique."},
        "fee_notes": {"type": "string", "maxLength": 2000,
                      "description": "Précisions sur l'entente d'honoraires."},
        "valeur": {"type": "integer", "minimum": 1, "maximum": 100000000000,
                   "description": "Valeur en litige, en cents entiers."},
        "hourly_rate": {
            "type": "integer", "minimum": 0, "maximum": 100000000,
            "description": (
                "Taux horaire en cents (0 = pro bono/aide juridique ; défaut "
                "de create_time_entries_bulk)."
            ),
        },
        "flat_fee": {"type": "integer", "minimum": 1, "maximum": 100000000000,
                     "description": "Forfait en cents entiers."},
        "contingency_percent": {
            "type": "integer", "minimum": 1, "maximum": 10000,
            "description": "Points de base : 2500 = 25,00 %.",
        },
        "droit_action_date": _date("Point de départ du droit d'action, AAAA-MM-JJ."),
        "date_avis": _date("Date confirmée d'avis préalable, AAAA-MM-JJ."),
        "prise_action_date": _date("Date d'acte interruptif déposé, AAAA-MM-JJ."),
        "prescription_notes": {
            "type": "string", "maxLength": 2000,
            "description": "Notes libres sur l'analyse de prescription.",
        },
    }


def _legacy_ref_prop() -> dict:
    return {
        "legacy_ref": {
            "type": "string",
            "maxLength": 64,
            "description": (
                "Référence d'origine (reprise > 24 h) pour find_imported ; "
                "refusée si déjà portée sur la même collection."
            ),
        }
    }

# WP16 write-tool enums — literals for the same firestore-at-import reason,
# each pinned against its model by tests/test_mcp_tools.py.
_TASK_PRIORITIES = ["haute", "normale", "basse"]
_TASK_CATEGORIES = [
    "rédaction", "recherche", "correspondance", "dépôt",
    "signification", "suivi", "admin", "autre",
]
_HEARING_TYPES = [
    # judiciaire tier…
    "conférence_de_gestion", "conférence_de_règlement",
    "conférence_préparatoire", "audience", "instruction",
    # …extrajudiciaire tier (forum derives from the type)
    "consultation", "rencontre", "conférence", "interrogatoire", "autre",
]
_EXPENSE_CATEGORIES = [
    "signification", "expertise", "transcription", "deplacement",
    "photocopie", "timbre_judiciaire", "autre",
]
# Lot 1b (L6) — the protocol vocabularies, copied from models.protocol
# (VALID_PROTOCOL_TYPES, VALID_STATUSES, STEP_STATUS_TARGETS) for the same
# firestore-at-import reason; pinned against it by
# tests/test_mcp_protocol_writes.py.
_PROTOCOL_TYPES = ["cq_simplifié", "cs_ordinaire", "conventionnel"]
_PROTOCOL_STATUSES = ["actif", "suspendu", "complété"]
_STEP_STATUS_TARGETS = ["complété", "à_venir"]

# Lot 1b (L7) — the calendar vocabularies, copied from models.hearing
# (VALID_MODALITES, VALID_STATUSES, VALID_REMINDER_MINUTES) for the same
# firestore-at-import reason; pinned against it by
# tests/test_mcp_hearing_writes.py. The series frequencies and ceiling are
# DERIVED from utils/recurrence (pure), the phase precedent.
_HEARING_MODALITES = ["présentiel", "visioconférence", "téléphonique"]
_HEARING_STATUSES = [
    "confirmée", "à_confirmer", "reportée", "annulée", "terminée",
]
# A creation records a date that is either certain or still to confirm —
# never a postponement, a cancellation or an event already held.
_HEARING_CREATE_STATUSES = ["à_confirmer", "confirmée"]
_HEARING_REMINDERS = [15, 30, 60, 120, 1440, 2880, 10080]
_SERIES_FREQUENCIES = list(recurrence.VALID_FREQUENCIES)
_SERIES_MAX = recurrence.MAX_SERIE_OCCURRENCES
_RENDEZ_VOUS_ACTIONS = ["confirmer", "refuser"]

# Lot 4b — CONTACTS. The kinds of representation: a hand LITERAL (models.*
# may not be imported here — models/__init__ builds the Firestore client),
# pinned against models.partie.MANDATAIRE_KIND_LABELS by
# tests/test_mcp_contact_writes.py.
_MANDATAIRE_KINDS = [
    "mandataire", "tuteur", "curateur", "représentant_légal", "autre",
]
# The compliance statuses — DERIVED from utils/kyc (pure: no model import,
# the utils.phases precedent): identity's, then conflict's own, in order.
# The handler refuses a value outside the chosen check's vocabulary.
_KYC_STATUSES = list(dict.fromkeys(
    _kyc.IDENTITY_STATUSES + _kyc.CONFLICT_STATUSES))
# record_kyc_status's check names, its per-check statuses with the labels the
# fiche shows, and its decided list — DERIVED as well (one source per list),
# so its description can never name a status the vocabulary dropped
# (« exempté », 2026-11).
_KYC_CHECK_NAMES = list(_kyc.CHECK_FIELDS)
_KYC_STATUS_TEXT = " ".join(
    f"{check}: "
    + ", ".join(f"{key} (« {label} »)" for key, label in _kyc.choices(field))
    + "."
    for check, field in _kyc.CHECK_FIELDS.items()
)
_KYC_DECIDED_TEXT = ", ".join(dict.fromkeys(
    _kyc.DECIDED[_kyc.FIELD_IDENTITY] + _kyc.DECIDED[_kyc.FIELD_CONFLICT]))

# Analyse documentaire — DÉRIVÉS eux aussi, du même précédent.
# `utils/analyse_taxonomies` est pur (aucun import de modèle, aucun client
# Firestore au chargement), donc l'enum ne peut pas dériver du vocabulaire
# que le modèle valide. ⚠ La CATÉGORIE n'est PAS un paramètre : elle est
# dérivée de la sous-nature par `nature_of()`. L'ajouter ici rouvrirait
# exactement ce que l'écart assumé avec la §5.3 rend supportable.
_SOUS_NATURE_CODES = sorted(_tax.VALID_SOUS_NATURES)
_PRIVILEGE_CODES = sorted(_tax.VALID_PRIVILEGES)
# Annexe C — les deux axes du droit de la preuve. L'ORDRE de la table est
# conservé (art. 2811 d'abord) : il se lit comme la disposition, et un tri
# alphabétique mettrait « AVEU » avant « ECRIT » sans raison.
_MOYEN_PREUVE_CODES = list(_tax.MOYENS_PREUVE)
_QUALIFICATION_ECRIT_CODES = list(_tax.QUALIFICATIONS_ECRIT)
_QUALITE_RECONNAISSANCE = list(_tax.QUALITES_RECONNAISSANCE)

# Phase O — DERIVED, not hand-copied (the _COVERAGE_CODES precedent):
# utils/phases.py is pure (no model import, no Firestore at load), so the
# enum can never drift from the vocabulary the models validate against.
# "" is deliberately EXCLUDED from the input enums: an MCP caller either
# phases the entry or omits the parameter — passing "" would be noise.
_PHASE_CODES = [c for c in phases.VALID_PHASES if c]
_SOUS_PHASE_CODES = [c for c in phases.VALID_SOUS_PHASES if c]

# The phase pair shared by every phased write tool.
# Concise on purpose (2026-09-30): repeated on twelve tools, beside enums
# that are the input contract and stay whole. « Optionnel : omis = non
# renseignée » left the SHARED text — it was the creators' truth only: on
# an update an omitted pair leaves the classification ALONE, and the
# reclassifiers refuse a call with neither key. So what omitting BOTH keys
# does is said PER TOOL: ``_phase_props(on_omit=…)``, keyword-only and
# without a default, so a new phased tool cannot forget to decide it. A
# creator stores the record UNCLASSIFIED — never the protocol's current
# phase, which is what the web forms offer (routes/tasks, routes/
# time_expenses → models/protocol.get_current_phase_for_dossier; no
# handler calls it), so a model that saw the form would otherwise expect
# it here. ``None`` = the tool's own description already says it (update_
# task, update_time_entry). tests/test_mcp_tools.py checks that every
# phased tool states it somewhere, and the one of the three that fits.
_PHASE_DESCRIPTION = (
    "Code phase du litige (axe 1 — libellés : get_reference_vocabulary "
    "kind phases ; déduit de `sous_phase` si omis)."
)
_SOUS_PHASE_DESCRIPTION = (
    "Sous-code (ex. « CTS-02 » ; doit correspondre à `phase` ; sinon "
    "« -00 » Général)."
)
_PHASE_OMIT_UNCLASSIFIED = (
    "Omettre les deux : stockée non renseignée (jamais déduite du protocole)."
)
_PHASE_OMIT_KEPT = "Omettre les deux : la paire actuelle reste inchangée."


def _phase_props(*, on_omit: Optional[str]) -> dict:
    """The ``phase`` / ``sous_phase`` pair."""
    return {
        "phase": {
            "type": "string",
            "enum": _PHASE_CODES,
            "description": (
                f"{_PHASE_DESCRIPTION} {on_omit}" if on_omit
                else _PHASE_DESCRIPTION
            ),
        },
        "sous_phase": {
            "type": "string",
            "enum": _SOUS_PHASE_CODES,
            "description": _SOUS_PHASE_DESCRIPTION,
        },
    }


def _hearing_modality_props() -> dict:
    """modalite / conference_uri / reminder_minutes."""
    return {
        "modalite": {
            "type": "string",
            "enum": _HEARING_MODALITES,
            "description": "Modalité (défaut à la création : présentiel).",
        },
        "conference_uri": {
            "type": "string",
            "maxLength": 2000,
            "description": "Lien vidéo http:// ou https:// (affiché sur téléphone et Outlook).",
        },
        "reminder_minutes": {
            "type": "integer",
            "enum": _HEARING_REMINDERS,
            "description": "Rappel avant le début en minutes (défaut 1440 = 24 h).",
        },
    }


def _hearing_create_props() -> dict:
    """The create_hearing arguments — shared with create_hearing_series."""
    return {
        "dossier_id": _id(
            "Dossier (UUIDv4). Omettre pour un événement « Général »."
        ),
        "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": 300,
            "description": "Titre en français.",
        },
        "hearing_type": {
            "type": "string",
            "enum": _HEARING_TYPES,
            "description": (
                "Type (détermine le forum ; défaut 'rencontre' extrajudiciaire)."
            ),
        },
        "date": _date("Date AAAA-MM-JJ (Montréal). Requis."),
        "start_time": {
            "type": "string",
            "maxLength": 5,
            "description": "Début HH:MM Montréal (omettre = journée entière).",
        },
        "end_time": {
            "type": "string",
            "maxLength": 5,
            "description": "Fin HH:MM Montréal (défaut : début + 1 h).",
        },
        "all_day": {
            "type": "boolean",
            "description": "true force une journée entière.",
        },
        "location": {
            "type": "string",
            "maxLength": 300,
            "description": "Salle, adresse ou palais de justice.",
        },
        "court": {
            "type": "string",
            "maxLength": 200,
            "description": "Tribunal (événements judiciaires).",
        },
        "judge": {
            "type": "string",
            "maxLength": 200,
            "description": "Juge si connu.",
        },
        "notes": {
            "type": "string",
            "maxLength": 1500,
            "description": "Notes libres en français (mention ajoutée auto).",
        },
        **_hearing_modality_props(),
        "status": {
            "type": "string",
            "enum": _HEARING_CREATE_STATUSES,
            "description": "à_confirmer (défaut) ou confirmée.",
        },
    }

PHASE_BULK_MAX = disclosure.PHASE_BULK_MAX


def _phase_bulk_items(id_key: str, id_description: str) -> dict:
    """The `entries` array of a bulk phase reclassification."""
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": PHASE_BULK_MAX,
        "description": (
            f"1 à {PHASE_BULK_MAX} lignes à reclasser (chacune doit nommer "
            "`phase` ou `sous_phase`, sinon refusée)."
        ),
        "items": {
            "type": "object",
            "properties": {
                id_key: _id(id_description),
                **_phase_props(on_omit=None),
            },
            "required": [id_key],
            "additionalProperties": False,
        },
    }


ENTRY_BULK_MAX = disclosure.ENTRY_BULK_MAX


def _time_entry_fields() -> dict:
    """The fields of ONE time entry in create_time_entries_bulk."""
    return {
        "dossier_id": _id("Dossier (UUIDv4). Requis."),
        "date": _date("Date du travail, AAAA-MM-JJ. Requis."),
        "description": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000,
            "description": (
                "Libellé français imprimé VERBATIM sur la facture client "
                "(aucune note interne)."
            ),
        },
        "hours": {
            "type": "number",
            "minimum": 0.01,
            "maximum": 24,
            "description": (
                "Heures, au plus DEUX décimales (ex. 0.25 ; plus fin refusé, "
                "jamais arrondi)."
            ),
        },
        "rate_cents": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1000000,
            "description": "Taux horaire en cents (défaut : taux du dossier).",
        },
        "billable": {
            "type": "boolean",
            "description": "Défaut true (false enregistre un montant de 0).",
        },
        **_phase_props(on_omit=_PHASE_OMIT_UNCLASSIFIED),
        **_legacy_ref_prop(),
    }


_TIME_ENTRY_REQUIRED = ["dossier_id", "date", "description", "hours"]


def _expense_fields() -> dict:
    """The fields of ONE disbursement in create_expenses_bulk."""
    return {
        "dossier_id": _id("Dossier (UUIDv4). Requis."),
        "date": _date("Date du déboursé, AAAA-MM-JJ. Requis."),
        "description": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000,
            "description": (
                "Libellé français imprimé VERBATIM sur la facture client."
            ),
        },
        "amount_cents": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100000000,
            "description": "Montant en cents entiers (15000 = 150,00 $).",
        },
        "category": {
            "type": "string",
            "enum": _EXPENSE_CATEGORIES,
            "description": "Défaut 'autre'.",
        },
        "taxable": {
            "type": "boolean",
            "description": "Défaut true.",
        },
        **_phase_props(on_omit=_PHASE_OMIT_UNCLASSIFIED),
        **_legacy_ref_prop(),
    }


_EXPENSE_REQUIRED = ["dossier_id", "date", "description", "amount_cents"]


def _entry_bulk_items(label: str, fields: dict, required: list[str]) -> dict:
    """The ``entries`` array of a bulk creator."""
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": ENTRY_BULK_MAX,
        "description": (
            f"1 à {ENTRY_BULK_MAX} {label}. Une ligne refusée fait refuser "
            "tout l'appel (`entries[i]` en base 0)."
        ),
        "items": {
            "type": "object",
            "properties": fields,
            "required": list(required),
            "additionalProperties": False,
        },
    }


def _substitutions_prop(*, expected_required: bool) -> dict:
    """The `substitutions` array of lot 2B."""
    expected: dict = {
        "type": "integer", "minimum": 1, "maximum": TEMPLATIZE_EXPECTED_MAX,
        "description": (
            "REQUIS : compte exact `substituted` retourné par "
            "preview_templatize."
            if expected_required else
            "Optionnel : compte attendu pour vérification."
        ),
    }
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": TEMPLATIZE_SUBSTITUTIONS_MAX,
        "description": (
            f"1 à {TEMPLATIZE_SUBSTITUTIONS_MAX} remplacements littéral → "
            "champ (plus long d'abord)."
        ),
        "items": {
            "type": "object",
            "properties": {
                "literal": {
                    "type": "string",
                    "minLength": TEMPLATIZE_LITERAL_MIN_CHARS,
                    "maxLength": TEMPLATIZE_LITERAL_MAX_CHARS,
                    "description": (
                        "Texte EXACT sensible à la casse, mots entiers (sans "
                        "accolade, tabulation ni saut de ligne)."
                    ),
                },
                "placeholder": {
                    "type": "string", "minLength": 1,
                    "maxLength": TEMPLATIZE_PLACEHOLDER_MAX_CHARS,
                    "description": (
                        "Champ cible (ex. client.nom_complet ou "
                        "{{client.nom_complet}} ; MAJUSCULES = valeur en "
                        "majuscules)."
                    ),
                },
                "expected_occurrences": expected,
            },
            "required": (["literal", "placeholder", "expected_occurrences"]
                         if expected_required else ["literal", "placeholder"]),
            "additionalProperties": False,
        },
    }


# ── Lot 5b — ACCOUNTING (scope athena:write since 2026-10-05) ────────────
# Hand-copied from models.trust / models.admin_ledger (importing a model here
# would build the Firestore client at load); tests/test_mcp_accounting.py
# pins each against its source, so none can drift.
_REGISTERS = ["trust", "admin"]
_TRUST_DIRECTIONS = ["recette", "déboursé"]
# models.trust.VALID_PURPOSES minus the two the MODEL reserves: « correction »
# (a reversal mints it, nothing else may) and « virement_inter_dossiers »
# (decision D24, 2026-09-29: only the two-leg transfer writes it —
# trust.create_inter_dossier_transfer, which the connector never reaches;
# moving one client's funds to another client stays in the application).
_TRUST_ENTRY_PURPOSES = [
    "avance_honoraires", "dépôt_client", "règlement", "virement_honoraires",
    "remise_client", "déboursé_tiers", "intérêts", "frais_bancaires", "autre",
]
_TRUST_METHODS = ["chèque", "virement", "traite", "dépôt_direct", "comptant"]
# models.admin_ledger: the kinds a create may carry (VALID_KINDS but the
# correction; paiement_carte through its own two-leg writer), the kinds an
# edit may keep or become (_EDITABLE_KINDS — a change of kind moves only
# along KIND_MOVES, the model's table, which the handler repeats through
# services/comptabilite), and every kind a row may show, in VALID_KINDS
# order (correction: minted by a reversal only). The natures outside the
# firm's results (NON_RESULT_KINDS: the lawyer's prélèvement and apport, a
# virement interne either way — 2026-10-07 —, a TPS/TVQ remittance or
# refund — 2026-11-07 —, a client's advance received here — 2026-11-08) and
# the refund of an expense (REFUND_KINDS, 2026-11-08) are in all three.
_ADMIN_ENTRY_KINDS = [
    "dépense", "recette_autre", "encaissement_facture", "prélèvement",
    "apport", "virement_interne_sortant", "virement_interne_entrant",
    "remise_tps_tvq", "remboursement_tps_tvq", "paiement_carte",
    "remboursement_depense", "avance_honoraires",
]
_ADMIN_EDIT_KINDS = [
    "dépense", "recette_autre", "prélèvement", "apport",
    "virement_interne_sortant", "virement_interne_entrant",
    "remise_tps_tvq", "remboursement_tps_tvq",
    "remboursement_depense", "avance_honoraires",
]
_ADMIN_KINDS = [
    "encaissement_facture", "recette_autre", "dépense", "remboursement_depense",
    "prélèvement", "apport", "virement_interne_sortant",
    "virement_interne_entrant", "avance_honoraires", "remise_tps_tvq",
    "remboursement_tps_tvq", "paiement_carte", "correction",
]
# The purpose a virement interne sortant may carry (admin_ledger
# .TRANSFER_PURPOSES): « avances » — the advances received here, moved to
# trust; "" — any other transfer.
_ADMIN_TRANSFER_PURPOSES = ["avances", ""]
_ADMIN_METHODS = [
    "chèque", "virement", "prélèvement", "dépôt_direct", "carte", "comptant",
    "autre",
]
_ADMIN_CATEGORIES = [
    "loyer", "location_salle", "internet", "téléphone", "abonnements",
    "infonuagique_ia", "équipement", "fournitures", "assurances",
    "cotisations_professionnelles", "formation", "documentation", "recherche",
    "publicité", "notaire", "expert", "avocat", "honoraires_professionnels",
    "huissier", "sténographe", "timbres_judiciaires", "frais_bancaires",
    "intérêts", "taxes_permis", "autre",
]
_REGISTER_STATUSES = ["en_circulation", "compensée", "annulée"]
# How a dépense's TPS/TVQ split is given: extracted from the taxes-included
# amount (models.admin_ledger.extract_taxes_from_gross), no tax credit
# claimed (net = amount), or the three amounts themselves.
_VENTILATION_MODES = ["ventiler", "sans_taxe", "détaillée"]
REGISTER_CLEAR_MAX = 50
REVERSAL_REASON_MAX_CHARS = 500           # models.fee_payment's motif ceiling
COUNTERPARTY_MAX_CHARS = 300
REGISTER_REFERENCE_MAX_CHARS = 200
REGISTER_DESCRIPTION_MAX_CHARS = 2000     # both models' _sanitize_data cap
ADMIN_LEDGER_WINDOW_DAYS = 90
# get_financial_statements: the periods one call may ask for (two years of
# months), the sections of the statements — a literal of
# models/financial_statements.SECTION_TITLES, in its order (pinned by
# tests/test_mcp_financial_statements.py) — and the ids and refs one « point
# à vérifier » carries before it says `truncated`.
STATEMENT_PERIODS_MAX = 24
_STATEMENT_SECTIONS = [
    "resultats", "flux", "bilan", "avoir", "taxes", "redressements",
    "fideicommis", "travaux",
]
STATEMENT_POINT_REFS_MAX = 20
MONEY_MAX_CENTS = 100_000_000_000         # 1 000 000 000 $ — a typo guard
_ADMIN_ETAG_READERS = ("get_admin_ledger",)


def _money_arg(description: str) -> dict:
    return {"type": "integer", "minimum": 1, "maximum": MONEY_MAX_CENTS,
            "description": description}


def _ventilation_props() -> dict:
    """A dépense's TPS/TVQ split."""
    part = {"type": "integer", "minimum": 0, "maximum": MONEY_MAX_CENTS}
    return {
        "ventilation": {
            "type": "string", "enum": _VENTILATION_MODES,
            "description": (
                "Ventilation TPS/TVQ (dépense / remboursement_depense) : "
                "ventiler, sans_taxe (net = montant) ou détaillée (net_cents + "
                "gst_cents + qst_cents = montant)."
            ),
        },
        "net_cents": {**part, "description": "détaillée seulement : montant net."},
        "gst_cents": {**part, "description": "détaillée seulement : TPS."},
        "qst_cents": {**part, "description": "détaillée seulement : TVQ."},
    }


def _register_text_props(counterparty_rule: str = "") -> dict:
    return {
        "counterparty": {
            "type": "string", "maxLength": COUNTERPARTY_MAX_CHARS,
            "description": (
                "Payeur ou bénéficiaire (« Somme reçue de / Bénéficiaire ») "
                "tel que nommé sur la pièce bancaire."
                + (" " + counterparty_rule if counterparty_rule else "")
            ),
        },
        "reference": {
            "type": "string", "maxLength": REGISTER_REFERENCE_MAX_CHARS,
            "description": "Numéro de chèque ou référence bancaire.",
        },
        "description": {
            "type": "string", "maxLength": REGISTER_DESCRIPTION_MAX_CHARS,
            "description": "Libellé libre inscrit au registre, en français.",
        },
    }


# ── Registry ────────────────────────────────────────────────────────────

TOOLS: dict[str, dict] = {
    "get_agenda": {
        "title": "Agenda et priorités",
        "description": (
            (
            "Bilan quotidien : audiences à venir, tâches et étapes de "
            "protocole urgentes, alertes de prescription (60 j) et stats du "
            "cabinet (dossiers ouverts, en-cours, impayés). Premier appel "
            "conseillé pour « qu'est-ce qui s'en vient ? ». Fenêtre en jours "
            "civils de Montréal : s'ouvre à MINUIT (audiences plus tôt "
            "aujourd'hui incluses) jusqu'à la fin de `window.to` ; chaque "
            "indicateur de retard suit ce même jour de Montréal "
            "(`window.from` et `is_overdue` concordent toujours). Pour la "
            "prescription, `last_action_date` est le dernier jour juridique "
            "de PROCÉDURE CIVILE au plus tard à l'échéance pour déposer la "
            "demande (égal à `prescription_date` un jour ouvrable ; vérifier "
            "`last_action_differs`, vrai aussi le 26 déc. ou 2 janv. car "
            "Athéna calcule la prescription sur l'art. 61 L.i., sans les 2 "
            "jours de l'art. 82 C.p.c.)."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 90,
                    "description": "Fenêtre d'anticipation en jours (défaut 14).",
                },
            },
            "additionalProperties": False,
        },
        "handler": "get_agenda",
    },
    "list_dossiers": {
        "title": "Liste des dossiers",
        "description": (
            (
            "Liste les dossiers, filtrés par `status` ou recherche libre sur "
            "titre, numéro interne, numéro de cour et `sommaire`. Retourne "
            "des résumés, plus récents en premier ; appeler `get_dossier` "
            "pour le détail complet. Paginer en repassant `next_cursor` "
            "comme `cursor` jusqu'à `null`."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": _DOSSIER_STATUSES,
                    "description": "Filtrer par statut de dossier. Omettre pour tout voir.",
                },
                "query": {
                    "type": "string",
                    "maxLength": 120,
                    "description": ("Recherche libre sur titre, numéro interne, numéro de cour et sommaire."),
                },
                "cursor": {
                    "type": "string",
                    "maxLength": 400,
                    "description": (
                        "Curseur opaque `next_cursor` de la réponse précédente (reprend après la dernière ligne). Omettre en page 1 ; valeur invalide repart en page 1."
                    ),
                },
                "limit": _limit(20),
            },
            "additionalProperties": False,
        },
        "handler": "list_dossiers",
    },
    "get_dossier": {
        "title": "Détail d'un dossier",
        "description": (
            # ≤ 2 048 characters: Claude Code cuts a description there
            # (tests/test_mcp_descriptor_budget.py). Tightened 2026-09-30
            # from 2 154 — every rule kept, the wording shortened; « exactly
            # one » of the two ids is said by both input properties. The
            # INDICATIVE warning moved from the end (past the cut) to the
            # front, and was made TRUE on the way: it said « interruption/
            # suspension are not computed », while this very payload's
            # prescription_status / prescription_date_effective derive both
            # from the recorded prescription_events (derive_prescription —
            # arts. 2892/2896, 2904 C.c.Q.).
            (
            "Fiche complète d'un dossier par `dossier_id` ou `file_number` : "
            "`sommaire`, données judiciaires, recours et prescription, plus "
            "résumés par module (tâches, audiences, notes, documents, temps, "
            "déboursés, factures, protocole). Tout délai est INDICATIF : le "
            "point de départ relève des faits et seuls les "
            "`prescription_events` enregistrés comptent "
            "(`prescription_status` / `prescription_date_effective` ; "
            "`prescription_date` brute reste fixe). "
            "`summaries.protocol.upcoming` = étapes dues d'ici 7 j ; "
            "`next_deadline_date` = prochaine échéance (due aujourd'hui = à "
            "venir). `forum_type` : 'judiciaire' (numéro analysé), "
            "'administratif'/'federal' (`tribunal`, numéro brut), "
            "'prejudiciaire'. Taxonomie : `domaine`/`action`, `delai` "
            "suggéré, `delai_types` (PE/PA/D/DR/A/R/N/I/S/V/F), `a_valider`, "
            "`avis`, `prescription_type` (délai confirmé)."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "UUIDv4 du dossier (ex. de list_dossiers). Fournir exactement dossier_id OU file_number."
                ),
                "file_number": {
                    "type": "string",
                    "maxLength": 20,
                    "description": (
                        "Numéro interne exact attribué au dossier (ex. « 2026-001 » ; espaces retirés). Fournir dossier_id OU file_number. Un échec de lecture renvoie une erreur plutôt que found: false — un found: false est donc fiable."
                    ),
                },
            },
            "additionalProperties": False,
        },
        "handler": "get_dossier",
    },
    "list_tasks": {
        "title": "Liste des tâches",
        "description": (
            (
            "Liste les tâches triées par échéance (sans date à la fin). Par "
            "défaut, actives seulement (`à_faire`, `en_cours`) ; passer "
            "`status` ou `include_completed=true` pour le reste. "
            "`is_overdue` suit le jour de Montréal : due aujourd'hui, sans "
            "date, `terminée` ou `annulée` n'est jamais en retard."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "Limiter à ce dossier (UUIDv4). Omettre pour toutes les tâches."
                ),
                "status": {
                    "type": "string",
                    "enum": _TASK_STATUSES,
                    "description": ("Filtrer à un statut précis (vocabulaire français) ; remplace la vue active par défaut."),
                },
                "include_completed": {
                    "type": "boolean",
                    "description": ("true inclut aussi les tâches terminée et annulée dans la vue par défaut."),
                },
                "updated_since": _updated_since(),
                "offset": _offset(),
                "limit": _limit(25),
            },
            "additionalProperties": False,
        },
        "handler": "list_tasks",
    },
    "list_hearings": {
        "title": "Liste des audiences",
        "description": (
            (
            "Liste audiences et événements entre deux dates (défaut : "
            "aujourd'hui à +60 j, max 366 j), ou pour un dossier. Inclut les "
            "`annulée` (vérifier `status`). Chaque ligne porte le forum "
            "dérivé (`judiciaire`/`extrajudiciaire`), la modalité, "
            "`conference_uri` et `etag` (`update_hearing`). Deux modes "
            "exclusifs des dates : `serie_id` liste toute une série "
            "récurrente (passé inclus) ; `bookings` \"pending\" liste les "
            "demandes Bookings et portail en attente de décision (`decide_rendez_vous`)."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": _date(
                    "Début de fenêtre, AAAA-MM-JJ (Montréal). Défaut : aujourd'hui."
                ),
                "date_to": _date(
                    "Fin de fenêtre, AAAA-MM-JJ inclusif. Défaut : date_from + 60 j."
                ),
                "dossier_id": _id(
                    "Audiences de ce dossier seulement (UUIDv4). Omettre pour tout voir."
                ),
                "serie_id": _id(
                    "Lister toute cette série récurrente, passé inclus (d'un `serie_id` retourné). Exclut dates, dossier_id et bookings."
                ),
                "bookings": {
                    "type": "string",
                    "enum": ["pending"],
                    "description": (
                        "\"pending\" : demandes Bookings et portail en attente — à_confirmer ou annulée_client (refuser la retire). Exclut dates, dossier_id et serie_id."
                    ),
                },
                "limit": _limit(25),
                "cursor": _cursor(
                    "Pagine du plus ancien au plus récent (ordre d'agenda)."
                ),
            },
            "additionalProperties": False,
        },
        "handler": "list_hearings",
    },
    "get_meeting_transcripts": {
        "title": "Transcriptions et notes Teams post-réunion",
        "description": (
            "LECTURE SEULE — lit les transcriptions WebVTT et résumés Copilot Teams "
            "préparés post-réunion (`a_revoir` par défaut, ou `versee`/`ecartee`, "
            "ou un `hearing_id` précis). Retourne métadonnées, locuteurs, aperçu, "
            "`copilot_markdown`, suggestions de classement et `etag` (pour "
            "`decide_meeting_transcript`). Aucun lien de téléchargement ni chemin GCS."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hearing_id": _id(
                    "UUIDv4 d'une réunion précise (optionnel). Omettre pour lister selon `status`."
                ),
                "status": {
                    "type": "string",
                    "enum": ["a_revoir", "versee", "ecartee"],
                    "description": (
                        "Filtre de statut lorsque `hearing_id` est omis (défaut : \"a_revoir\")."
                    ),
                },
                "limit": _limit(20),
            },
            "additionalProperties": False,
        },
        "handler": "get_meeting_transcripts",
    },
    "list_notes": {
        "title": "Notes (dossier ou cabinet)",
        "description": (
            (
            "Liste les notes avec aperçu texte de 280 car. CHOISIR LE CORPUS "
            ": sans `dossier_id` ni `scope`, retourne UNIQUEMENT les notes « "
            "Général » (sans dossier). Passer `scope=\"cabinet\"` pour "
            "chercher dans tous les dossiers (chaque ligne identifie son "
            "dossier), ou `dossier_id` pour un dossier. `query` cherche dans "
            "tout le titre ET tout le corps : lire la note via `get_note` "
            "avant d'écarter un résultat. `is_analyse: true` désigne la « "
            "Théorie de la cause » — modifier uniquement via `edit_analyse`."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["general", "dossier", "cabinet"],
                    "description": (
                        "Corpus : \"general\" (défaut, notes sans dossier), \"dossier\" (implicite avec dossier_id), \"cabinet\" (TOUTES les notes). Combinaisons contradictoires refusées."
                    ),
                },
                "dossier_id": _id(
                    "Dossier ciblé (UUIDv4, implique scope=\"dossier\"). Omettre pour « Général » ou passer scope=\"cabinet\"."
                ),
                "dossier_status": {
                    "type": "string",
                    "enum": _DOSSIER_STATUSES,
                    "description": (
                        "Scope cabinet uniquement : filtrer selon le statut du dossier."
                    ),
                },
                "query": {
                    "type": "string",
                    "maxLength": 120,
                    "description": (
                        "Sous-chaîne insensible à la casse dans le titre ET tout le contenu."
                    ),
                },
                "category": {
                    "type": "string",
                    "enum": _NOTE_CATEGORIES,
                    "description": "Filtrer à une catégorie de note.",
                },
                "pinned": {
                    "type": "boolean",
                    "description": (
                        "true = épinglées seulement ; false = non épinglées. Omettre pour tout."
                    ),
                },
                "date_from": _date(
                    "Date minimale de création (Montréal), AAAA-MM-JJ inclusif."
                ),
                "date_to": _date(
                    "Date maximale de création (Montréal), AAAA-MM-JJ inclusif."
                ),
                "updated_since": _updated_since(),
                "offset": _offset(),
                "cursor": _cursor(
                    "Scope cabinet uniquement (les autres utilisent offset) ; ordre immuable (création + id)."
                ),
                "limit": _limit(20),
            },
            "additionalProperties": False,
        },
        "handler": "list_notes",
    },
    "get_note": {
        "title": "Détail d'une note",
        "description": (
            (
            "Lit une note avec son Markdown complet et son `etag`. Si "
            "`is_analyse` est true (« Théorie de la cause »), `structure` "
            "décrit l'entête et les blocs A–H ; modifier uniquement avec "
            "`edit_analyse`."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "note_id": _id("UUIDv4 de la note (de list_notes)."),
            },
            "required": ["note_id"],
            "additionalProperties": False,
        },
        "handler": "get_note",
    },
    "list_documents": {
        "title": "Documents (dossier ou cabinet)",
        "description": (
            (
            (
            (
            (
            (
            "Liste les MÉTADONNÉES de documents (noms, catégories, tailles, "
            "versions — jamais le contenu ni de lien). `query` porte "
            "UNIQUEMENT sur les métadonnées (nom, fichier, résumé d'analyse, "
            "`notes_internes`, `genere_depuis`, étiquettes `tags`), jamais "
            "le texte (lire via `get_document_text`). Défaut : un dossier "
            "(`dossier_id` requis), ou `scope=\"cabinet\"` (`folder_path=\"\"`). "
            "Retourne `document_date` (date propre ou null), `etag`, "
            "`category_source` (`mcp` = présumé jusqu'à confirmation) et "
            "`folder_system_role`. `include_folders=true` joint toute "
            "l'arborescence."
        )
        )
        )
        )
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["dossier", "cabinet"],
                    "description": (
                        "\"dossier\" (défaut, dossier_id REQUIS) ou \"cabinet\" (tous dossiers ; dossier_id et folder_id alors refusés)."
                    ),
                },
                "dossier_id": _id(
                    "UUIDv4 du dossier. Requis sauf avec scope=\"cabinet\"."
                ),
                "dossier_status": {
                    "type": "string",
                    "enum": _DOSSIER_STATUSES,
                    "description": (
                        "Scope cabinet uniquement : filtrer par statut du dossier."
                    ),
                },
                "folder_id": _id(
                    "Limiter à un répertoire (UUIDv4). Omettre pour tous."
                ),
                "category": {
                    "type": "string",
                    "enum": _DOCUMENT_CATEGORIES,
                    "description": "Filtrer par catégorie de document.",
                },
                "query": {
                    "type": "string",
                    "maxLength": 120,
                    "description": (
                        "Recherche texte libre sur les métadonnées listées — jamais le contenu du fichier."
                    ),
                },
                "date_from": _date(
                    "Date effective min, AAAA-MM-JJ inclusif (document_date si définie, sinon date de téléversement)."
                ),
                "date_to": _date(
                    "Date effective max, AAAA-MM-JJ inclusif."
                ),
                "updated_since": _updated_since(),
                "include_folders": {
                    "type": "boolean",
                    "description": (
                        "Scope dossier seulement : inclut toute l'arborescence dans `folders` (même vides). Erreur de lecture refusée plutôt que vue vide. Défaut false."
                    ),
                },
                "offset": _offset(),
                "cursor": _cursor(
                    "Scope cabinet uniquement (le scope dossier utilise offset)."
                ),
                "limit": _limit(25),
            },
            # `dossier_id` is deliberately NOT in `required` any more: cabinet
            # scope has no dossier. Relaxing `required` is additive on the
            # wire (a schema that demands less accepts strictly more), and the
            # HANDLER re-imposes it outside cabinet scope — so an omitted
            # dossier_id still fails loudly instead of silently becoming a
            # firm-wide scan.
            "additionalProperties": False,
        },
        "handler": "list_documents",
    },
    "list_templates": {
        "title": "Gabarits (modèles Word)",
        "description": (
            (
            "Gabarits Word du cabinet — métadonnées seulement (jamais le "
            "fichier ni de lien). Sans `template_id` : liste. Avec "
            "`template_id` : champs classés en AUTO (remplis par Athéna), "
            "MANUELS et BLOCS (noms exacts). Passer `dossier_id` (et slots) "
            "vérifie la résolution auto : tout manque donne « [CHAMP "
            "MANQUANT : nom] » (à signaler, ne pas contourner ; valeurs "
            "jamais retournées). Au plus un modèle actif par sorte spéciale "
            "(`note_honoraires`, `note_credit`, `note`), désigné par "
            "l'avocat dans l'app. Sortes `modele` : contrats adaptés à la "
            "main, jamais remplis (`fill_gabarit` refuse)."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "template_id": _id(
                    "Inspecter UN modèle (UUIDv4). Omettre pour lister."
                ),
                "kind": {
                    "type": "string",
                    "enum": _TEMPLATE_KINDS,
                    "description": (
                        "Liste : « gabarit » (actes/lettres), « note_honoraires », « note_credit », « note » ou « modele » (jamais rempli)."
                    ),
                },
                "category": {
                    "type": "string",
                    "enum": _TEMPLATE_CATEGORIES,
                    "description": "Liste : catégorie propre du modèle.",
                },
                "query": {
                    "type": "string",
                    "maxLength": 120,
                    "description": "Liste : cherche dans le nom et la description.",
                },
                "dossier_id": _id(
                    "Avec template_id : résout les champs auto sur ce dossier."
                ),
                "client_id": _id(
                    "Avec template_id : client du slot « client » (requis si multiple et utilisé). Refusé pour note/note_honoraires/note_credit."
                ),
                "adverse_id": _id(
                    "Avec template_id : partie adverse du slot « adverse » (si multiple). Refusé pour note/note_honoraires/note_credit."
                ),
                "destinataire_id": _id(
                    "Avec template_id : contact destinataire (pas de défaut). Client de la facture pour note_honoraires/note_credit ; refusé pour note."
                ),
                "offset": _offset(),
                "limit": _limit(20),
            },
            "additionalProperties": False,
        },
        "handler": "list_templates",
    },
    "list_parties": {
        "title": "Liste des contacts",
        "description": (
            (
            "Liste les contacts (parties), filtrés par `contact_role`, "
            "`type` ou recherche nom/courriel/téléphone. Retourne des lignes "
            "sommaires ; appeler `get_partie` pour la fiche complète."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_role": {
                    "type": "string",
                    "enum": _CONTACT_ROLES,
                    "description": ("Filtrer par rôle du contact au cabinet."),
                },
                "type": {
                    "type": "string",
                    "enum": _PARTIE_TYPES,
                    "description": ("individual = personne physique ; organization = personne morale."),
                },
                "query": {
                    "type": "string",
                    "maxLength": 120,
                    "description": "Recherche libre sur nom, courriel et téléphone.",
                },
                "updated_since": _updated_since(),
                "offset": _offset(),
                "limit": _limit(20),
            },
            "additionalProperties": False,
        },
        "handler": "list_parties",
    },
    "get_partie": {
        "title": "Fiche d'un contact",
        "description": (
            (
            "Fiche complète d'un contact : coordonnées personnelles et "
            "professionnelles, identifiants légaux, statuts KYC / conflit "
            "d'intérêts (avec `*_source` et `*_presumed` si inscrit par ce "
            "connecteur en attente de confirmation), mandataires et dossiers "
            "liés. Les notes KYC/conflit peuvent être sensibles."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "partie_id": _id(
                    "UUIDv4 du contact (de list_parties)."
                ),
            },
            "required": ["partie_id"],
            "additionalProperties": False,
        },
        "handler": "get_partie",
    },
    "get_billing_snapshot": {
        "title": "Portrait de facturation",
        "description": (
            (
            "Portrait de facturation. Sans `dossier_id` : totaux non "
            "facturés du cabinet (honoraires et `unbilled_expenses`), solde "
            "impayé, factures et détail `by_dossier`. Avec `dossier_id` : "
            "résumés temps/déboursés/factures et jusqu'à 50 lignes non "
            "facturées par type. `total_hours` inclut le temps non "
            "facturable ; les montants non facturés ne comptent que le "
            "facturable non encore facturé."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "Cibler un dossier (UUIDv4). Omettre pour le portrait global du cabinet."
                ),
            },
            "additionalProperties": False,
        },
        "handler": "get_billing_snapshot",
    },
    "list_time_entries": {
        "title": "Entrées de temps",
        "description": (
            (
            "Entrées de temps du cabinet ou d'un dossier — facturées ET non "
            "facturées (seule façon de voir le temps facturé), plus récentes "
            "d'abord. `billable_filter='non_facture'` garde le non encore "
            "facturé (inclut le non facturable à 0 $) ; `'billable'` filtre "
            "le facturable (facturé ou non). Combiner `dossier_id` et plage "
            "de dates pour un relevé."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "Limiter à un dossier (UUIDv4). Omettre pour tout le cabinet."
                ),
                "date_from": _date(
                    "Date minimale de l'entrée, AAAA-MM-JJ inclusif."
                ),
                "date_to": _date(
                    "Date maximale de l'entrée, AAAA-MM-JJ inclusif."
                ),
                "billable_filter": {
                    "type": "string",
                    "enum": ["billable", "non_facture"],
                    "description": (
                        "'billable' = facturable seulement ; 'non_facture' = non encore facturé (inclut non facturable). Omettre pour tout."
                    ),
                },
                "limit": _limit(25),
                "cursor": _cursor(
                    "Requis pour parcourir une période complète si `truncated` est true."
                ),
            },
            "additionalProperties": False,
        },
        "handler": "list_time_entries",
    },
    "list_expenses": {
        "title": "Déboursés",
        "description": (
            (
            "Déboursés du cabinet ou d'un dossier — facturés ET non "
            "facturés, plus récents d'abord. `billable_filter='non_facture'` "
            "garde seulement les non facturés. Catégories en clés françaises "
            "(`signification`, `expertise`, `transcription`, `deplacement`, "
            "`photocopie`, `timbre_judiciaire`, `autre`)."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "Limiter à un dossier (UUIDv4). Omettre pour tout le cabinet."
                ),
                "date_from": _date(
                    "Date minimale du déboursé, AAAA-MM-JJ inclusif."
                ),
                "date_to": _date(
                    "Date maximale du déboursé, AAAA-MM-JJ inclusif."
                ),
                "billable_filter": {
                    "type": "string",
                    "enum": ["non_facture"],
                    "description": (
                        "'non_facture' = non encore facturé. Omettre pour tout."
                    ),
                },
                "limit": _limit(25),
                "cursor": _cursor(
                    "Requis pour parcourir toute la période si `truncated` est true."
                ),
            },
            "additionalProperties": False,
        },
        "handler": "list_expenses",
    },
    "list_invoices": {
        "title": "Registre des factures",
        "description": (
            (
            (
            (
            (
            (
            "Registre des factures, plus récentes d'abord. Sans filtre, "
            "inclut `brouillon` (jamais transmise) et `annulée` (aucun "
            "montant dû) ; filtrer par `status` ou `status_group=\"impayée\"` "
            "(`envoyée` + `en_retard`). `envoyée` = marquée envoyée (Athéna "
            "ne transmet rien). `amount_paid` = paiements aux registres "
            "comptables ; `balance = amount_due - amount_paid - written_off` "
            "(radiations) ; `payment_basis=\"none\"` = aucun paiement inscrit. "
            "`amount_due` est figé à l'émission : ne jamais l'additionner. "
            "Sommer `balance` sur `status_group=\"impayée\"` sur toutes pages "
            "(`cursor=null`) égale `get_billing_snapshot.outstanding_cents` "
            "au cent près — contrairement à "
            "`get_dossier.summaries.invoices.total_outstanding` (somme "
            "`total`, inclut brouillons, traite `payée` comme réglée : les "
            "deux ne concordent pas à dessein)."
        )
        )
        )
        )
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "Limiter à un dossier (UUIDv4). Omettre pour tout le cabinet."
                ),
                "status": {
                    "type": "string",
                    "enum": _INVOICE_STATUSES,
                    "description": (
                        "Un statut exact. Exclusif de status_group."
                    ),
                },
                "status_group": {
                    "type": "string",
                    "enum": ["impayée"],
                    "description": (
                        "\"impayée\" = envoyée + en_retard (même définition que get_billing_snapshot). Exclusif de status."
                    ),
                },
                "date_from": _date("Date minimale de facture, AAAA-MM-JJ inclusif."),
                "date_to": _date("Date maximale de facture, AAAA-MM-JJ inclusif."),
                "limit": _limit(25),
                "cursor": _cursor(),
            },
            "additionalProperties": False,
        },
        "handler": "list_invoices",
    },
    "get_invoice": {
        "title": "Facture",
        "description": (
            (
            "Détail d'une facture : parties, montants (honoraires, "
            "déboursés, TPS, TVQ, provision, total, paiements, radiations, "
            "solde), `settlement_label`, `write_offs` (`credit_note_number`) "
            "et lignes (citer VERBATIM). `subtotal_matches_line_items: "
            "false` signale un écart sans réadditionner ; `warnings` non "
            "vide = échec de lecture des lignes. Fournit `etag` et "
            "`connector_transitions` pour `update_invoice`."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_id": _id("UUIDv4 de la facture à lire."),
            },
            "required": ["invoice_id"],
            "additionalProperties": False,
        },
        "handler": "get_invoice",
    },
    "preview_invoice": {
        "title": "Aperçu d'une facture",
        "description": (
            (
            "LECTURE — simule `create_invoice` sur cette sélection via la "
            "même logique du modèle ; n'écrit rien et ne consomme aucun "
            "numéro. `ready: false` indique un refus (`refusals` détaille "
            "chaque source). Si prêt, appeler `create_invoice` avec la MÊME "
            "sélection et `total_cents` comme `expected_total_cents`."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": _invoice_selection_props(),
            "required": ["dossier_id"],
            "additionalProperties": False,
        },
        "handler": "preview_invoice",
    },
    "get_budget": {
        "title": "Budget d'un dossier",
        "description": (
            (
            "LECTURE — budget par phase d'un dossier : version en vigueur "
            "(sous-codes, taux horaire, note, totaux), réel vs budget (temps "
            "facturable + tous déboursés ; `warn` >= 80 %, `over` > 100 %) "
            "et historique des versions. Passer `base_version` à "
            "`create_budget_version` (0 = aucun budget). Un échec de lecture "
            "du réel refuse l'appel."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id("Dossier (list_dossiers). Requis."),
                "include_history": {
                    "type": "boolean",
                    "description": (
                        f"Lister les versions récentes d'abord, max {BUDGET_HISTORY_MAX} (défaut true)."),
                },
            },
            "required": ["dossier_id"],
            "additionalProperties": False,
        },
        "handler": "get_budget",
    },
    "get_coverage_report": {
        "title": "Rapport de couverture",
        "description": (
            (
            (
            (
            (
            (
            "Audit de conformité des dossiers ouverts en un appel "
            "(protocole, signification, numéro de cour, KYC/conflits). "
            "Gravités : `manquement` (obligatoire, ex. KYC/conflits) et "
            "`signalement`. Codes stables. CHAQUE CONSTAT EST UNE "
            "OBSERVATION, jamais une directive : `detail` dit quoi faire à "
            "l'app et nomme l'outil équivalent (`create_protocol`, "
            "`record_signification`, `complete_dossier`, `complete_task`… ; "
            "`record_kyc_status` inscrit seulement identité/conflit PRÉSUMÉ "
            "jusqu'à confirmation à l'écran). TOUJOURS lire "
            "`scope.checks_skipped` et `data_completeness` avant de déclarer "
            "conforme (un échec de lecture suspend le contrôle). "
            "`cross_scope_findings` couvre les dossiers fermés."
        )
        )
        )
        )
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": _DOSSIER_STATUSES,
                    "description": (
                        "Statut de dossier à auditer ; défaut \"actif\"."
                    ),
                },
                "checks": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(_COVERAGE_CODES)},
                    "description": (
                        "Limiter à ces codes. Omettre pour tout contrôler (les exclus paraissent dans checks_skipped)."
                    ),
                },
                "limit": _limit(25),
                "cursor": _cursor("Pagine par numéro de dossier."),
            },
            "additionalProperties": False,
        },
        "handler": "get_coverage_report",
    },
    "get_reference_vocabulary": {
        "title": "Vocabulaires de référence",
        "description": (
            (
            "Énumère un vocabulaire contrôlé validé par le modèle (qui "
            "refuse sans lister les choix) — consulter AVANT d'écrire une "
            "classification. `kind` : `domaines`, `actions` (filtrer par "
            "`domaine`, préfixe = domaine), `prescription_types`, `forums` "
            "(tribunaux admin./fédéraux), `districts`, `phases`. `note` sur "
            "une action = délai INDICATIF de la taxonomie (suggestion à "
            "confirmer par l'avocat, souvent vide). Données de référence "
            "pures."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
                        "domaines", "actions", "prescription_types",
                        "forums", "districts", "phases",
                        # Les vocabulaires de l'ANALYSE documentaire. Sans
                        # eux, les codes de `record_document_analysis` sont
                        # des énums nues : pas de libellé, pas d'ancrage
                        # légal, pas de niveau, pas de réserve.
                        "sous_natures", "privileges",
                        "moyens_preuve", "qualifications_ecrit",
                    ],
                    "description": (
                        "Vocabulaire à énumérer. Pour `record_document_analysis` : `sous_natures` (dérive la catégorie), `privileges` (cumulatifs, avec niveau/fondement/réserve), `moyens_preuve` et `qualifications_ecrit` (Annexe C)."
                    ),
                },
                "domaine": {
                    "type": "string",
                    "maxLength": 10,
                    "description": (
                        "Limiter `actions` à une famille, ex. « REC ». Refusé avec tout autre kind."
                    ),
                },
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
        "handler": "get_reference_vocabulary",
    },
    "find_imported": {
        "title": "Retrouver un enregistrement importé",
        "description": (
            (
            "Cherche ce qu'une reprise historique a déjà créé via sa "
            "référence d'origine (`legacy_ref`). Garde durable au-delà des "
            "24 h d'`idempotency_key` : vérifier chaque ligne avant création "
            "(aucune suppression ici). Couvre contacts, dossiers, temps, "
            "déboursés et factures (filtrable via `entity_type`). `matches: "
            "[]` prouve l'absence (une lecture défaillante renvoie une "
            "erreur)."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "legacy_ref": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": (
                        "Identifiant d'origine dans le système précédent (n° de ligne, référence, n° de facture) à garder stable pour toute la reprise."
                    ),
                },
                "entity_type": {
                    "type": "string",
                    "enum": [
                        "partie", "dossier", "time_entry", "expense", "invoice",
                    ],
                    "description": "Limiter la recherche à un type d'enregistrement.",
                },
            },
            "required": ["legacy_ref"],
            "additionalProperties": False,
        },
        "handler": "find_imported",
    },
    "get_import_audit": {
        "title": "Vérifier l'import d'un dossier",
        "description": (
            (
            "Rapproche un dossier après reprise (temps, déboursés et "
            "factures) avant de passer à la ligne suivante. Constats = "
            "OBSERVATIONS seulement (aucune suppression de doublon ici) : "
            "`IMP-01` temps non facturé sur dossier fermé ; `IMP-02` "
            "sous-total ≠ lignes ; `IMP-03` source absente ; `IMP-04` "
            "doublons probables ; `IMP-05` dossier fermé sans date de "
            "clôture ; `IMP-06` entrée facturée sans facture ; `IMP-07` "
            "facture importée en `brouillon`. Lire `checks_skipped` (un "
            "contrôle sauté ne vaut pas conformité)."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "Dossier à rapprocher. Fournir exactement dossier_id OU file_number."
                ),
                "file_number": {
                    "type": "string",
                    "maxLength": 20,
                    "description": (
                        "Numéro exact du dossier (ex. « 2019-014 »). Alternative à dossier_id — fournir un des deux."
                    ),
                },
            },
            "additionalProperties": False,
        },
        "handler": "get_import_audit",
    },
    "list_deletions": {
        "title": "Journal des suppressions",
        "description": (
            (
            "Journal append-only des suppressions, plus récentes en premier "
            ": enregistrements supprimés (titre + statut minimaux) et LIENS "
            "détachés (`dossier_party`, `mandataire` — le contact subsiste), "
            "depuis l'app ou le connecteur. Distingue « supprimé » d'« "
            "inexistant ». Commence au déploiement du journal et couvre les "
            "200 derniers événements (absent au-delà = hors fenêtre)."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_type": {
                    "type": "string",
                    # Kept in step with VALID_ENTITY_TYPES minus
                    # CONNECTOR_HIDDEN_ENTITY_TYPES (D7), both in
                    # models/audit_event (hand-mirrored — the models/*
                    # import ban): a hidden type is never offered here, and
                    # list_deletions drops its rows. Additive
                    # input-enum growth is safe; the OUTPUT schema types
                    # entity_type as a plain string, so new values never
                    # violate the structuredContent contract.
                    "enum": [
                        "task", "hearing", "note", "document", "expense",
                        "time_entry", "invoice", "partie", "protocol",
                        "protocol_step", "folder", "doc_template",
                        "dossier", "admin_transaction", "hearing_series",
                        "dossier_party", "mandataire",
                    ],
                    "description": (
                        "Filtrer à un type. `dossier_party` et `mandataire` tracent un LIEN retiré (le contact subsiste) : `dossier_party` (`entity_id` = contact, `status` = camp quitté) ; `mandataire` (`entity_id` = mandataire, `title` = contact REPRÉSENTÉ, `status` = sorte)."
                    ),
                },
                "dossier_id": _id(
                    "Limiter aux événements de ce dossier (UUIDv4)."
                ),
                "date_from": _date(
                    "Date minimale (Montréal), AAAA-MM-JJ inclusif."
                ),
                "limit": _limit(25),
            },
            "additionalProperties": False,
        },
        "handler": "list_deletions",
    },
    "list_protocol_steps": {
        "title": "Étapes du protocole",
        "description": (
            (
            "Calendrier du protocole d'instance d'un dossier : étapes "
            "ordonnées et échéances du protocole actif. `status` est DÉRIVÉ "
            "aujourd'hui (Montréal) et fait foi (`is_overdue` == `status == "
            "\"en_retard\"`), contrairement à `status_stored` figé à "
            "l'affichage web (`status_differs: true`). "
            "`include_history=true` inclut les protocoles antérieurs. "
            "Vérifier `regime_mismatch`: `true` signale un modèle C.p.c. "
            "inadapté au tribunal (échéances à signaler). Porte `etag` du "
            "protocole et de chaque étape."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "Dossier dont lire le protocole (UUIDv4)."
                ),
                "include_history": {
                    "type": "boolean",
                    "description": ("true inclut aussi jusqu'à 10 protocoles antérieurs (complétés/suspendus)."),
                },
            },
            "required": ["dossier_id"],
            "additionalProperties": False,
        },
        "handler": "list_protocol_steps",
    },
    "compute_judicial_deadline": {
        "title": "Calcul de délai judiciaire",
        "description": (
            (
            "Calcule un délai judiciaire québécois selon l'art. 83 C.p.c. : "
            "tous les jours civils comptent ; si l'échéance tombe un jour "
            "non juridique (fin de semaine, férié québécois, 26 décembre ou "
            "2 janvier — art. 82 C.p.c.), proroge dans le sens du calcul "
            "(`'after'` ou `'before'`) au prochain jour juridique. Procédure "
            "civile seulement : pour la prescription C.c.Q. (art. 2879), "
            "Athéna utilise l'art. 61 L.i. sans ces deux jours."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": _date(
                    "Date de départ du calcul, AAAA-MM-JJ."
                ),
                "delay_days": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 3650,
                    "description": ("Nombre de jours civils du délai (art. 83 C.p.c.)."),
                },
                "direction": {
                    "type": "string",
                    "enum": ["after", "before"],
                    "description": ("'after' compte vers l'avant depuis start_date ; 'before' vers l'arrière (prorogation dans le même sens)."),
                },
            },
            "required": ["start_date", "delay_days", "direction"],
            "additionalProperties": False,
        },
        "handler": "compute_judicial_deadline",
    },
    "parse_court_file_number": {
        "title": "Analyse d'un numéro de dossier judiciaire",
        "description": (
            (
            "Analyse un numéro de dossier judiciaire québécois "
            "(NNN-NN-NNNNNN-NNN) en greffe, tribunal, compétence, palais de "
            "justice et district judiciaire. Un préfixe de lettres (TAL, "
            "TAQ…) signale un tribunal administratif."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "court_file_number": {
                    "type": "string",
                    "maxLength": 30,
                    "description": ("Numéro brut, ex. « 500-05-123456-241 » (préfixe alphabétique = tribunal administratif)."),
                },
            },
            "required": ["court_file_number"],
            "additionalProperties": False,
        },
        "handler": "parse_court_file_number",
    },
    "get_trust_balance": {
        "title": "Solde en fidéicommis d'un dossier",
        "description": (
            (
            "Soldes en fidéicommis d'un dossier par client : comptable "
            "(registre), compensé (disponible pour déboursé) et dépôts en "
            "circulation. Montants en cents + format fr-CA. Lecture seule."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "Dossier dont lire les soldes en fidéicommis (UUIDv4)."
                ),
            },
            "required": ["dossier_id"],
            "additionalProperties": False,
        },
        "handler": "get_trust_balance",
    },
    "list_trust_transactions": {
        "title": "Registre des opérations en fidéicommis",
        "description": (
            (
            "Journal de fidéicommis, plus récent en premier. `dossier_id` + "
            "`client_id` = carte-client d'un bénéficiaire ; sans filtre = "
            "tout le journal. PAGINATION ASYMÉTRIQUE : `account_id` seul "
            "suit l'index décroissant et pagine tout le registre via "
            "`cursor` ; tout autre filtre lit une fenêtre bornée en ordre "
            "croissant réordonnée ici (`next_cursor: null`) — si `truncated: "
            "true`, resserrer les dates ou passer `account_id` seul. Jamais "
            "de numéro de transit/compte bancaire."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": _id(
                    "Limiter à un compte en fidéicommis (UUIDv4). Omettre pour tous."
                ),
                "dossier_id": _id(
                    "Avec client_id, cible une carte-client (UUIDv4)."
                ),
                "client_id": _id(
                    "Avec dossier_id, cible une carte-client — un bénéficiaire (UUIDv4)."
                ),
                "date_from": _date(
                    "Écritures datées du ou après le AAAA-MM-JJ."
                ),
                "date_to": _date(
                    "Écritures datées du ou avant le AAAA-MM-JJ."
                ),
                "status": {
                    "type": "string",
                    "enum": ["en_circulation", "compensée", "annulée"],
                    "description": ("en_circulation = non compensé ; compensée = compensé à la banque ; annulée = contrepassé."),
                },
                "limit": _limit(25),
                "cursor": _cursor(
                    "Honoré SEULEMENT avec account_id seul ; sinon next_cursor: null."
                ),
            },
            "additionalProperties": False,
        },
        "handler": "list_trust_transactions",
    },
    "get_trust_snapshot": {
        "title": "Aperçu des fonds en fidéicommis",
        "description": (
            (
            "Portrait global du fidéicommis : solde comptable et bancaire de "
            "chaque compte, état de conciliation (dernière période, "
            "`never_reconciled`, `overdue` passé 30 j), total détenu, "
            "chèques en circulation avec date d'émission, dépôts en "
            "circulation et `by_dossier` (détail client via "
            "`get_trust_balance`). Lecture seule ; jamais de numéro de "
            "transit/compte."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": "get_trust_snapshot",
    },
    # ── Write tools (require athena:write) ──────────────────────────────
    "create_note": {
        "title": "Créer une note dans un dossier",
        "description": (
            (
            "ÉCRITURE — crée une note Markdown en français (recherches, "
            "résumés, analyses). Avec `dossier_id` : classée au dossier (id "
            "invalide refusé ; omettre SEULEMENT hors dossier « Général »). "
            "Synchronisée au téléphone ; modifiable via `update_note`, "
            "jamais supprimable ici. HTML brut refusé (liens `<https://…>` "
            "auto-convertis). Défaut catégorie `'recherche'` ; ouvre avec « "
            "Note rédigée par Claude le … ». Passer `idempotency_key` et "
            "confirmer avant d'appeler."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "Dossier cible (UUIDv4). Omettre SEULEMENT hors dossier (« Général »). Id inexistant refusé."
                ),
                "title": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": NOTE_TITLE_MAX_CHARS,
                    "description": "Titre de la note, en français.",
                },
                "content": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": CONTENT_MAX_CHARS,
                    "description": (
                        f"Corps Markdown en français (max {CONTENT_MAX_CHARS} car.)."
                    ),
                },
                "category": {
                    "type": "string",
                    "enum": _NOTE_CATEGORIES,
                    "description": "Défaut 'recherche'.",
                },
                **_write_protocol_props(),
            },
            "required": ["title", "content"],
            "additionalProperties": False,
        },
        "handler": "create_note",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "append_to_note": {
        "title": "Ajouter du texte à une note existante",
        "description": (
            (
            "ÉCRITURE — ajoute du Markdown en français à la FIN d'une note "
            "sous séparateur daté « Ajouté par Claude ». Additif seulement "
            "(conserve une révision). Lire d'abord avec `get_note` ; en cas "
            "d'erreur incertaine, relire avant de réessayer (un second appel "
            "ajoute une 2e copie). Refuse au-delà du plafond. Refuse "
            "`is_analyse` (« Théorie de la cause » : utiliser "
            "`edit_analyse`)."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "note_id": _id(
                    "UUIDv4 de la note à compléter (de list_notes)."
                ),
                "content": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": CONTENT_MAX_CHARS,
                    "description": (
                        f"Markdown français à ajouter (max {CONTENT_MAX_CHARS} car.)."
                    ),
                },
                **_write_protocol_props(),
            },
            "required": ["note_id", "content"],
            "additionalProperties": False,
        },
        "handler": "append_to_note",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "update_note": {
        "title": "Modifier une note",
        "annotations": {
            # Values already stored write nothing; a content replacement
            # demands the current etag, so a repeat is refused, not doubled.
            "idempotentHint": True,
        },
        "description": (
            (
            (
            (
            "ÉCRITURE — REMPLACE les champs nommés (omis = intact) : "
            "`title`, `category`, `pinned`, `content`, `dossier_id` (déplace "
            "la note, `\"\"` = « Général », suivi sur téléphone). `content` "
            "remplace TOUT le corps : relire via `get_note`, envoyer complet "
            "avec `expected_etag` (l'ancien texte va aux révisions comme "
            "dans l'app ou sur téléphone ; mention « Révisée par Claude » "
            "ajoutée). HTML brut ou dépassement refusés. Refuse `is_analyse` "
            "(`edit_analyse`). Préférer `append_to_note` pour ajouter en fin."
        )
        )
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "note_id": _id("UUIDv4 de la note à modifier (de list_notes)."),
                "title": {
                    "type": "string", "minLength": 1,
                    "maxLength": NOTE_TITLE_MAX_CHARS,
                    "description": "Nouveau titre, en français.",
                },
                "category": {
                    "type": "string", "enum": _NOTE_CATEGORIES,
                    "description": "Nouvelle catégorie.",
                },
                "pinned": {
                    "type": "boolean",
                    "description": "Épingler (true) ou désépingler (false) la note.",
                },
                "content": {
                    "type": "string", "minLength": 1,
                    "maxLength": NOTE_REPLACE_MAX_CHARS,
                    "description": (
                        f"Nouveau corps Markdown COMPLET, en français (max {NOTE_REPLACE_MAX_CHARS} car.)."
                    ),
                },
                "dossier_id": _id(
                    "DÉPLACER vers ce dossier (UUIDv4) ou \"\" pour « Général ». Id invalide refusé."
                ),
                **_expected_etag_required_when(
                    _NOTE_ETAG_READERS, "lorsque `content` est envoyé"),
                **_write_protocol_props(),
            },
            "required": ["note_id"],
            "additionalProperties": False,
        },
        "handler": "update_note",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _NOTE_ETAG_READERS,
    },
    "edit_analyse": {
        "title": "Rédiger la théorie de la cause",
        "annotations": {
            # The init finds and writes nothing twice; an edit demands the
            # current etag, so its repeat is refused, never applied twice.
            "idempotentHint": True,
        },
        "description": (
            (
            "ÉCRITURE — « Théorie de la cause » d'un dossier (`entete` + "
            "blocs `## Bloc A` … `H`). Sans `operations` ni `full` : "
            "initialise si absente (idempotent) et retourne structure + "
            "`etag`. `operations` : remplace ou complète des blocs nommés "
            "(aucun titre `#`/`##` interne). `full` : réécrit tout en "
            "gardant les titres dans l'ordre. Toute écriture EXIGE "
            "`expected_etag`, archive une révision et ajoute une mention "
            "datée « par Claude ». Titres manquants/doublés refusés."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "UUIDv4 du dossier de la théorie de la cause."
                ),
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": ANALYSE_OPERATIONS_MAX,
                    "description": (
                        f"1 à {ANALYSE_OPERATIONS_MAX} modifications de blocs en tout-ou-rien. Exclusif de `full`."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "bloc": {
                                "type": "string",
                                "enum": ["entete", "A", "B", "C", "D", "E",
                                         "F", "G", "H"],
                                "description": (
                                    "« entete » (avant le bloc A) ou lettre du bloc."
                                ),
                            },
                            "mode": {
                                "type": "string",
                                "enum": ["replace", "append"],
                                "description": (
                                    "replace = tout le corps du bloc (\"\" le vide, sauf la ligne de révision datée) ; append = ajoute à la fin."
                                ),
                            },
                            "content": {
                                "type": "string",
                                "maxLength": CONTENT_MAX_CHARS,
                                "description": "Markdown, en français.",
                            },
                        },
                        "required": ["bloc", "mode", "content"],
                        "additionalProperties": False,
                    },
                },
                "full": {
                    "type": "string", "minLength": 1,
                    "maxLength": NOTE_REPLACE_MAX_CHARS,
                    "description": (
                        "Note complète (Markdown). Exclusif de `operations`."
                    ),
                },
                **_expected_etag_required_when(
                    _NOTE_ETAG_READERS,
                    "avec `operations` ou `full` (id de note = `analyse_note_id` de get_dossier)"),
                **_write_protocol_props(),
            },
            "required": ["dossier_id"],
            "additionalProperties": False,
        },
        "handler": "edit_analyse",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _NOTE_ETAG_READERS,
    },
    "complete_task": {
        "title": "Clore une tâche",
        "annotations": {
            # A second call with the same status writes nothing at all.
            "idempotentHint": True,
        },
        "description": (
            (
            (
            (
            (
            (
            "ÉCRITURE — ferme une tâche (`terminée`, `annulée`) ou passe une "
            "tâche ouverte à `en_cours`. Ne rouvre pas une tâche fermée "
            "(`reopen_task`), n'édite ni ne supprime. CASCADE : `terminée` "
            "complète l'étape liée — et ferme le protocole si dernière étape "
            "ouverte (`protocol_step_effect` vérifie après coup ; "
            "`reopen_task` rouvre les deux). Asymétrie : `annulée` ne "
            "cascade PAS ; passer entre statuts fermés ou vers `en_cours` si "
            "fermée est REFUSÉ. Rare : `en_cours` sur tâche ouverte à étape "
            "`complété` rouvre celle-ci (non reflétée par "
            "`protocol_step_effect` sur protocole fermé). Statut déjà porté "
            "= no-op (`already_completed: true`)."
        )
        )
        )
        )
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": _id("UUIDv4 de la tâche à fermer."),
                "status": {
                    "type": "string",
                    "enum": ["terminée", "annulée", "en_cours"],
                    "description": (
                        "Défaut « terminée ». « en_cours » refusé si déjà fermée (utiliser reopen_task)."
                    ),
                },
                "completion_note": {
                    "type": "string",
                    "maxLength": 1000,
                    "description": (
                        "Note française optionnelle ajoutée sous mention datée « par Claude ». Refusée si le total dépasse 2000 car."
                    ),
                },
                **_write_protocol_props(),
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "Accepts no expected_etag (update_task and reopen_task do); the "
            "handler compare-and-sets against the task it has just read, so "
            "a change landing during the call is refused, never overwritten "
            "(a same-state call writes nothing)."
        ),
        "handler": "complete_task",
    },
    "update_task": {
        "title": "Modifier une tâche",
        "annotations": {
            # Values already stored write nothing: a replay is a no-op.
            "idempotentHint": True,
        },
        "description": (
            (
            "ÉCRITURE — REMPLACE les champs nommés (omis = intact ; "
            "identique = sans écriture). Jamais le statut "
            "(`complete_task`/`reopen_task`). `description` remplace TOUT le "
            "texte sans historique : relire via `list_tasks` et renvoyer "
            "complet. `dossier_id` DÉPLACE (`` = « Général », suivi sur "
            "téléphone ; refusé si liée à un protocole). Omettre les deux "
            "clés de phase laisse le classement intact."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": _id("UUIDv4 de la tâche à modifier (de list_tasks)."),
                "title": {
                    "type": "string", "minLength": 1, "maxLength": 300,
                    "description": "Nouveau titre, en français.",
                },
                "description": {
                    "type": "string", "maxLength": 2000,
                    "description": (
                        "Nouvelle description — remplace tout le texte (\"\" vide). Refusée au-delà de 2000 car."
                    ),
                },
                "priority": {
                    "type": "string", "enum": _TASK_PRIORITIES,
                    "description": "Nouvelle priorité.",
                },
                "category": {
                    "type": "string", "enum": _TASK_CATEGORIES,
                    "description": "Nouvelle catégorie.",
                },
                "due_date": _date(
                    "Nouvelle échéance, AAAA-MM-JJ (\"\" la retire ; sans date = absente des urgences)."
                ),
                **_phase_props(on_omit=None),
                "dossier_id": _id(
                    "DÉPLACER vers ce dossier (UUIDv4) ou \"\" pour « Général ». Id invalide refusé."
                ),
                **_expected_etag_prop(_TASK_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        "handler": "update_task",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _TASK_ETAG_READERS,
    },
    "reopen_task": {
        "title": "Rouvrir une tâche",
        "annotations": {
            # The same target twice writes nothing at all.
            "idempotentHint": True,
        },
        "description": (
            (
            "ÉCRITURE — rouvre une tâche `terminée` (ou `annulée` avec "
            "`reopen_cancelled: true` après confirmation) vers `à_faire` "
            "(défaut) ou `en_cours`, ou remet `en_cours` à `à_faire`. "
            "CASCADE : rouvre l'étape de protocole liée si `complété` (et le "
            "protocole fermé automatiquement ; refusé si un autre protocole "
            "est actif). `protocol_step_effect` reflète l'état réel après "
            "écriture."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": _id("UUIDv4 de la tâche à rouvrir (de list_tasks)."),
                "status": {
                    "type": "string",
                    "enum": ["à_faire", "en_cours"],
                    "description": (
                        "Défaut « à_faire ». Pour passer une tâche ouverte à « en_cours », utiliser complete_task."
                    ),
                },
                "reopen_cancelled": {
                    "type": "boolean",
                    "description": (
                        "Requis à true pour rouvrir une tâche « annulée »."
                    ),
                },
                **_expected_etag_prop(_TASK_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        "handler": "reopen_task",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _TASK_ETAG_READERS,
    },
    "create_protocol": {
        "title": "Créer un protocole",
        "description": (
            (
            "ÉCRITURE — crée le protocole d'instance d'un dossier : "
            "`cq_simplifié` (Cour du Québec : délais C.p.c. verrouillés), "
            "`cs_ordinaire` (Cour supérieure : dates suggérées à confirmer) "
            "ou `conventionnel` (ajouter étapes via `add_protocol_step`). Au "
            "plus un protocole `actif` par dossier ; refusé si régime "
            "incompatible avec le tribunal. Échéances calculées depuis "
            "`start_date` (art. 83 C.p.c.). `create_linked_tasks: true` crée "
            "les tâches liées au téléphone. Retourne étapes, échéances, "
            "phases et `etag`."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "UUIDv4 du dossier (id inexistant refusé)."),
                "protocol_type": {
                    "type": "string", "enum": _PROTOCOL_TYPES,
                    "description": (
                        "Modèle C.p.c. ; son régime doit régir le tribunal du dossier (`regime_mismatch`)."),
                },
                "start_date": _date(
                    "AAAA-MM-JJ — date de départ des échéances du modèle (ex. signification de la demande)."),
                "title": {
                    "type": "string", "maxLength": 200,
                    "description": (
                        "Défaut « Protocole de l'instance »."),
                },
                "notes": {
                    "type": "string", "maxLength": 2000,
                    "description": "Notes libres en français sur le protocole.",
                },
                "create_linked_tasks": {
                    "type": "boolean",
                    "description": (
                        "Défaut false. true crée une tâche liée par étape sur le dossier."),
                },
                **_write_protocol_props(),
            },
            "required": ["dossier_id", "protocol_type", "start_date"],
            "additionalProperties": False,
        },
        "handler": "create_protocol",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "update_protocol": {
        "title": "Modifier un protocole",
        "annotations": {
            # Values already stored write nothing: a replay is a no-op.
            "idempotentHint": True,
        },
        "description": (
            (
            "ÉCRITURE — REMPLACE les champs nommés du protocole (identique = "
            "sans écriture). Un nouveau `start_date` RECALCULE les échéances "
            "du modèle sauf étapes complétées et dates CS confirmées par "
            "l'avocat (`recompute`) ; tâches liées non modifiées à la main "
            "suivent (`linked_tasks`). `status` `suspendu`/`complété` retire "
            "les étapes de `get_agenda` et arrête la cascade ; `actif` "
            "refusé si autre protocole actif. `notes` remplace sans "
            "historique."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "protocol_id": _id(
                    "UUIDv4 du protocole (de list_protocol_steps)."),
                "title": {
                    "type": "string", "minLength": 1, "maxLength": 200,
                    "description": "Nouveau titre.",
                },
                "notes": {
                    "type": "string", "maxLength": 2000,
                    "description": "Nouvelles notes — remplacent tout le texte.",
                },
                "court": {
                    "type": "string", "maxLength": 200,
                    "description": "Tribunal saisi du protocole.",
                },
                "start_date": _date(
                    "Nouvelle date de départ, AAAA-MM-JJ — recalcule les échéances du modèle."),
                "status": {
                    "type": "string", "enum": _PROTOCOL_STATUSES,
                    "description": (
                        "« suspendu » / « complété » ferment (closed_by « mcp ») ; « actif » réactive."),
                },
                **_expected_etag_prop(_PROTOCOL_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["protocol_id"],
            "additionalProperties": False,
        },
        "handler": "update_protocol",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _PROTOCOL_ETAG_READERS,
    },
    "add_protocol_step": {
        "title": "Ajouter une étape de protocole",
        "description": (
            (
            "ÉCRITURE — ajoute une étape personnalisée en fin d'un protocole "
            "ACTIF (réactiver d'abord si suspendu/complété). Jamais "
            "verrouillée ni obligatoire ; calculer son échéance via "
            "`compute_judicial_deadline`. `create_linked_task: true` crée la "
            "tâche liée synchronisée sur le téléphone."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "protocol_id": _id(
                    "UUIDv4 du protocole (de list_protocol_steps)."),
                "title": {
                    "type": "string", "minLength": 1, "maxLength": 300,
                    "description": "Titre de l'étape, en français.",
                },
                "description": {
                    "type": "string", "maxLength": 2000,
                    "description": "Description de ce que l'étape requiert.",
                },
                "cpc_reference": {
                    "type": "string", "maxLength": 200,
                    "description": "Ex. « art. 246 C.p.c. ».",
                },
                "deadline_date": _date("AAAA-MM-JJ (omettre si aucune échéance)."),
                **_phase_props(on_omit=_PHASE_OMIT_UNCLASSIFIED),
                "notes": {
                    "type": "string", "maxLength": 2000,
                    "description": "Notes libres sur l'étape.",
                },
                "create_linked_task": {
                    "type": "boolean",
                    "description": "Défaut false (voir description).",
                },
                **_write_protocol_props(),
            },
            "required": ["protocol_id", "title"],
            "additionalProperties": False,
        },
        "handler": "add_protocol_step",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "update_protocol_step": {
        "title": "Modifier une étape de protocole",
        "annotations": {
            # Same values, or the same target status, write nothing.
            "idempotentHint": True,
        },
        "description": (
            (
            "ÉCRITURE — soit REMPLACER des champs (`deadline_date`, `notes`, "
            "`phase`, plus `title`/`description`/`cpc_reference` sur étape "
            "personnalisée seulement — texte C.p.c. et délais CQ verrouillés "
            "refusés), soit fixer `status` (`complété` ou `à_venir`, cible "
            "explicite, jamais ensemble). CASCADE : la tâche liée suit "
            "(`annulée` intacte) ; la dernière étape complétée ferme le "
            "protocole ; rouvrir une étape le réactive si aucun autre n'est "
            "actif. Changer `deadline_date` décale la tâche liée non "
            "déplacée et confirme une date CS. `notes` sans historique."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "protocol_id": _id(
                    "UUIDv4 du protocole de l'étape (de list_protocol_steps)."),
                "step_id": _id("UUIDv4 de l'étape."),
                "deadline_date": _date(
                    "Nouvelle échéance, AAAA-MM-JJ ; \"\" la retire (refusé sur étape gabarit)."),
                "notes": {
                    "type": "string", "maxLength": 2000,
                    "description": (
                        "Nouvelles notes — remplacent tout le texte sans historique."),
                },
                **_phase_props(on_omit=_PHASE_OMIT_KEPT),
                "title": {
                    "type": "string", "minLength": 1, "maxLength": 300,
                    "description": "Nouveau titre — étapes personnalisées seulement.",
                },
                "description": {
                    "type": "string", "maxLength": 2000,
                    "description": "Nouvelle description — étapes personnalisées seulement.",
                },
                "cpc_reference": {
                    "type": "string", "maxLength": 200,
                    "description": "Nouvelle référence — étapes personnalisées seulement.",
                },
                "status": {
                    "type": "string", "enum": _STEP_STATUS_TARGETS,
                    "description": (
                        "État cible souhaité — seul dans l'appel."),
                },
                **_expected_etag_prop(_STEP_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["protocol_id", "step_id"],
            "additionalProperties": False,
        },
        "handler": "update_protocol_step",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _STEP_ETAG_READERS,
    },
    "create_task": {
        "title": "Créer une tâche",
        "description": (
            (
            "ÉCRITURE — crée une tâche (`à_faire`), point d'entrée des "
            "échéances et alertes d'agenda. Avec `dossier_id` : classée au "
            "dossier (id inexistant refusé ; omettre seulement pour « "
            "Général »). Synchronisée au téléphone ; gérée ensuite via "
            "`update_task`/`complete_task`/`reopen_task`, jamais supprimable "
            "ici. Confirmer avant d'appeler et passer une `idempotency_key`."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "Dossier cible (UUIDv4). Omettre pour une tâche « Général »."
                ),
                "title": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                    "description": "Titre de la tâche en français — l'action à accomplir.",
                },
                "description": {
                    "type": "string",
                    "maxLength": 1500,
                    "description": (
                        "Détail optionnel (fondement du délai, article, calcul), en français. Mention ajoutée auto."
                    ),
                },
                "due_date": _date(
                    "Échéance, AAAA-MM-JJ (obligatoire pour figurer dans les listes urgentes)."
                ),
                "priority": {
                    "type": "string",
                    "enum": _TASK_PRIORITIES,
                    "description": "Défaut 'normale'.",
                },
                "category": {
                    "type": "string",
                    "enum": _TASK_CATEGORIES,
                    "description": "Défaut 'autre'.",
                },
                **_phase_props(on_omit=_PHASE_OMIT_UNCLASSIFIED),
                **_write_protocol_props(),
            },
            "required": ["title"],
            "additionalProperties": False,
        },
        "handler": "create_task",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "create_hearing": {
        "title": "Créer un événement au calendrier",
        "description": (
            (
            "ÉCRITURE — crée un événement d'agenda (audience, rencontre, "
            "interrogatoire…) partagé sur le téléphone. `hearing_type` "
            "détermine le forum (types judiciaires vs défaut `'rencontre'` "
            "extrajudiciaire). Heures de Montréal (`HH:MM` ; sans "
            "`start_time` = journée entière). Créé `à_confirmer` sauf "
            "`status=\"confirmée\"`. Modifiable via `update_hearing`, jamais "
            "supprimable ici ; pour une série, utiliser "
            "`create_hearing_series`. Confirmer avant d'appeler."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                **_hearing_create_props(),
                **_write_protocol_props(),
            },
            "required": ["title", "date"],
            "additionalProperties": False,
        },
        "handler": "create_hearing",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "create_hearing_series": {
        "title": "Créer une série d'événements",
        "description": (
            f"ÉCRITURE — crée une série RÉCURRENTE atomique (`all or nothing`, même `serie_id`, synchro téléphone). Mêmes arguments que `create_hearing`, plus `frequency` et EXACTEMENT une borne : `count` (1 à {_SERIES_MAX}) ou `until` (AAAA-MM-JJ inclusif, refusé au-delà de {_SERIES_MAX} occurrences). Conserve l'heure locale de Montréal à l'heure d'été et ne déplace pas les fériés. `idempotency_key` REQUISE. Modifier une occurrence via `update_hearing` (`detach_from_series` pour détacher). Confirmer avant d'appeler."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                **_hearing_create_props(),
                "frequency": {
                    "type": "string",
                    "enum": _SERIES_FREQUENCIES,
                    "description": "Fréquence de répétition de l'événement.",
                },
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _SERIES_MAX,
                    "description": (
                        "Nombre d'occurrences (1re incluse). Exclusif de `until`."
                    ),
                },
                "until": _date(
                    "Dernier jour possible, AAAA-MM-JJ inclusif. Exclusif de `count`."
                ),
                **_write_protocol_props(),
            },
            "required": ["title", "date", "frequency", "idempotency_key"],
            "additionalProperties": False,
        },
        "handler": "create_hearing_series",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_REQUIRED,
    },
    "update_hearing": {
        "title": "Modifier un événement du calendrier",
        # NOT idempotentHint, unlike update_task / update_note: values
        # already stored write nothing, but `notes_append` appends AGAIN on
        # every identical call — so « calling repeatedly with the same
        # arguments has no additional effect » would be false for one of its
        # arguments, and the hint is what a client trusts before a blind
        # retry. The family default (false) is the honest one here.
        "description": (
            f"ÉCRITURE — REMPLACE les champs nommés (omis = intact ; identique = sans écriture). Reporter : `date`/`start_time`/`end_time` (Montréal ; heure/durée gardées si omises). `status=\"annulée\"` retire de `get_agenda` et d'Outlook au prochain cycle ({_cron.cadence_fr('outlook')}, Montréal ; téléphone mis à jour aussitôt). `dossier_id` DÉPLACE (`\"\"` = « Général » ; série : exige `detach_from_series: true`). Bookings en attente/refusé = refusé (`decide_rendez_vous`). Bookings CONFIRMÉ : seuls `dossier_id`, `notes`, `notes_append` sont admis ici (reporter ou annuler se fait dans Outlook ; toute autre modification à l'application — seul ce connecteur est restreint). Notes sans historique."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hearing_id": _id(
                    "UUIDv4 de l'événement à modifier (de list_hearings ou get_agenda)."
                ),
                "title": {
                    "type": "string", "minLength": 1, "maxLength": 300,
                    "description": "Nouveau titre, en français.",
                },
                "hearing_type": {
                    "type": "string", "enum": _HEARING_TYPES,
                    "description": "Nouveau type (dérive le forum).",
                },
                "date": _date(
                    "Nouveau jour, AAAA-MM-JJ (Montréal). Omis : jour conservé."
                ),
                "start_time": {
                    "type": "string", "maxLength": 5,
                    "description": (
                        "Nouveau début, HH:MM Montréal ; rend l'événement horodaté."
                    ),
                },
                "end_time": {
                    "type": "string", "maxLength": 5,
                    "description": (
                        "Nouvelle fin, HH:MM Montréal, même jour après début. Omis : durée conservée."
                    ),
                },
                "all_day": {
                    "type": "boolean",
                    "description": (
                        "true = journée entière (sans start_time/end_time) ; false = horodaté (exige start_time)."
                    ),
                },
                "location": {
                    "type": "string", "maxLength": 300,
                    "description": "Nouveau lieu ; \"\" vide.",
                },
                "court": {
                    "type": "string", "maxLength": 200,
                    "description": "Nouveau tribunal ; \"\" vide.",
                },
                "judge": {
                    "type": "string", "maxLength": 200,
                    "description": "Nouveau juge ; \"\" vide.",
                },
                **_hearing_modality_props(),
                "notes": {
                    "type": "string", "maxLength": 2000,
                    "description": (
                        "REMPLACE toutes les notes (\"\" vide) sans historique ; max 2000 car. Exclusif de `notes_append`."
                    ),
                },
                "notes_append": {
                    "type": "string", "minLength": 1, "maxLength": 1500,
                    "description": (
                        "Ajouté sous mention datée « Ajouté par Claude » (total <= 2000 car.). Rejouer seulement avec même idempotency_key. Exclusif de `notes`."
                    ),
                },
                "status": {
                    "type": "string", "enum": _HEARING_STATUSES,
                    "description": (
                        "Nouveau statut. terminée refusé sur une date future."
                    ),
                },
                "detach_from_series": {
                    "type": "boolean",
                    "description": (
                        "true = détache cette occurrence de sa série (dans la même écriture)."
                    ),
                },
                "dossier_id": _id(
                    "DÉPLACER vers ce dossier (UUIDv4) ou \"\" pour « Général ». Id invalide refusé."
                ),
                **_expected_etag_prop(_HEARING_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["hearing_id"],
            "additionalProperties": False,
        },
        "handler": "update_hearing",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _HEARING_ETAG_READERS,
    },
    "decide_rendez_vous": {
        "title": "Confirmer ou refuser un rendez-vous",
        "annotations": {
            # The same decision twice writes nothing and never contacts the
            # client again.
            "idempotentHint": True,
        },
        "description": (
            "ÉCRITURE EXTÉRIEURE — décide d'une demande Bookings ou portail "
            "en attente (`list_hearings`, `bookings=\"pending\"`). `confirmer` "
            "ajoute l'événement au calendrier et au téléphone (et crée "
            "l'invitation Teams pour un rendez-vous portail) ; `lier_partie` "
            "(défaut true) lie le contact au courriel exact du demandeur. "
            "`refuser` ANNULE LA RÉUNION OUTLOOK ET NOTIFIE LE CLIENT avec texte "
            "fixe pour Bookings (pour le portail : met à jour le statut sans "
            "courriel sortant ici). Un rendez-vous déjà CONFIRMÉ n'est pas "
            "refusé ici : il s'annule dans Outlook ou l'application. "
            "`expected_etag` et `idempotency_key` REQUIS."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hearing_id": _id(
                    "Demande en attente (UUIDv4), de list_hearings avec bookings=\"pending\"."
                ),
                "action": {
                    "type": "string", "enum": _RENDEZ_VOUS_ACTIONS,
                    "description": (
                        "confirmer (accepte) ou refuser (décline — notifie le client)."
                    ),
                },
                "lier_partie": {
                    "type": "boolean",
                    "description": (
                        "confirmer seulement (défaut true) : lie le contact au courriel exact du demandeur."
                    ),
                },
                **_expected_etag_prop(_RENDEZ_VOUS_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": [
                "hearing_id", "action", "expected_etag", "idempotency_key",
            ],
            "additionalProperties": False,
        },
        "handler": "decide_rendez_vous",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_REQUIRED,
        "concurrency": CONCURRENCY_REQUIRED,
        "etag_readers": _RENDEZ_VOUS_ETAG_READERS,
    },
    "decide_meeting_transcript": {
        "title": "Verser ou écarter une transcription Teams",
        "annotations": {
            "idempotentHint": True,
        },
        "description": (
            "ÉCRITURE — traite une transcription/notes Teams préparée "
            "(`get_meeting_transcripts`). `verser` crée la Note Athéna, dépose "
            "la transcription `.docx` dans « Interne » (+ OneDrive si "
            "configuré) et purge le WebVTT temporaire. `ecarter` purge le "
            "WebVTT sans rien verser au dossier. Catégorie/classement posé par "
            "le connecteur (présumé). `expected_etag` et `idempotency_key` REQUIS ; "
            "toujours confirmer avec l'avocat avant d'appeler."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hearing_id": _id(
                    "UUIDv4 de la réunion (de `get_meeting_transcripts`)."
                ),
                "action": {
                    "type": "string",
                    "enum": ["verser", "ecarter"],
                    "description": (
                        "\"verser\" classe la note + .docx au dossier ; \"ecarter\" "
                        "purge la transcription temporaire."
                    ),
                },
                "dossier_id": _id(
                    "Dossier de destination (UUIDv4) — requis pour `action=\"verser\"`."
                ),
                "note_title": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": (
                        "Titre de la Note Athéna (requis pour `action=\"verser\"`)."
                    ),
                },
                "note_category": {
                    "type": "string",
                    "enum": ["consultation", "rencontre"],
                    "description": (
                        "Catégorie de la Note Athéna (requis pour `action=\"verser\"`)."
                    ),
                },
                "copilot_markdown": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 100_000,
                    "description": (
                        "Contenu Markdown des notes (optionnel pour `verser` ; "
                        "défaut : notes préparées dans Athéna)."
                    ),
                },
                **_expected_etag_prop(_TRANSCRIPT_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": [
                "hearing_id", "action", "expected_etag", "idempotency_key",
            ],
            "additionalProperties": False,
        },
        "handler": "decide_meeting_transcript",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_REQUIRED,
        "concurrency": CONCURRENCY_REQUIRED,
        "etag_readers": _TRANSCRIPT_ETAG_READERS,
    },
    "create_time_entries_bulk": {
        "title": "Créer plusieurs entrées de temps",
        "description": (
            f"ÉCRITURE — TOUT OU RIEN : une ligne refusée fait refuser TOUT l'appel (rien n'est écrit) ; index base 0 (`entries[3]`, `entries[3].hours`). idempotency_key REQUISE ; confirmer le lot avant d'appeler. Crée 1 à {ENTRY_BULK_MAX} entrées de temps non facturées en bloc atomique : `dossier_id` requis, `rate_cents` au taux du dossier par défaut, `hours` max 2 décimales (jamais arrondi), `description` en libellé français imprimé VERBATIM sur la facture (aucune note interne), `legacy_ref` refusée si doublon. `entities` revient dans le même ordre avec chaque `etag`. Corriger avant facture via `update_time_entry`, reclasser via `set_time_entry_phase_bulk` ; jamais supprimer ici."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entries": _entry_bulk_items(
                    "entrées de temps", _time_entry_fields(),
                    _TIME_ENTRY_REQUIRED,
                ),
                **_write_protocol_props(),
            },
            "required": ["entries", "idempotency_key"],
            "additionalProperties": False,
        },
        "handler": "create_time_entries_bulk",
        "scope": SCOPE_WRITE,
        # Money: a retry without its key would write a SECOND batch the
        # next invoice sweeps, and nothing here can delete the first.
        "idempotency": IDEMPOTENCY_REQUIRED,
    },
    "create_expenses_bulk": {
        "title": "Créer plusieurs déboursés",
        "description": (
            f"ÉCRITURE — TOUT OU RIEN : une ligne refusée fait refuser TOUT l'appel (rien n'est écrit) ; index base 0 (`entries[3]`, `entries[3].amount_cents`). idempotency_key REQUISE ; confirmer le lot avant d'appeler. Crée 1 à {ENTRY_BULK_MAX} déboursés non facturés en bloc atomique : `dossier_id` requis, montant en cents entiers, défaut `category` 'autre' et `taxable: true`, `description` en libellé français imprimé VERBATIM sur la facture (aucune note interne), `legacy_ref` refusée si doublon. `entities` revient dans le même ordre. Corriger avant facture via `update_expense`, reclasser via `set_expense_phase_bulk` ; jamais supprimer ici."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entries": _entry_bulk_items(
                    "déboursés", _expense_fields(), _EXPENSE_REQUIRED,
                ),
                **_write_protocol_props(),
            },
            "required": ["entries", "idempotency_key"],
            "additionalProperties": False,
        },
        "handler": "create_expenses_bulk",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_REQUIRED,
    },
    "complete_dossier": {
        "title": "Compléter les champs vides d'un dossier",
        "description": (
            (
            "ÉCRITURE — remplissage SEULEMENT si vide ou encore à la valeur "
            "par défaut (classification, valeur en litige, départ de "
            "prescription…). Un champ déjà différent n'est JAMAIS écrasé : "
            "tout l'appel est refusé en listant les conflits "
            "(`update_dossier` sert à modifier). `court_file_number` dérive "
            "aussi les métadonnées judiciaires. Montants en cents entiers ; "
            "`contingency_percent` en points de base (`2500` = 25 %) ; dates "
            "AAAA-MM-JJ. Confirmer avant d'appeler ; retourne le portrait de "
            "prescription résultant."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id("UUIDv4 du dossier à compléter. Requis."),
                "domaine": {
                    "type": "string", "maxLength": 10,
                    "description": ("Code famille de taxonomie (ex. REC) — validé par le modèle."),
                },
                "action": {
                    "type": "string", "maxLength": 10,
                    "description": ("Code recours de taxonomie (ex. REC-01) ; doit appartenir à `domaine`."),
                },
                "action_precision": {
                    "type": "string", "maxLength": 500,
                    "description": "Précision en texte libre (requise pour les recours -99).",
                },
                "sommaire": {
                    "type": "string", "maxLength": 5000,
                    "description": (
                        "Sommaire du dossier en français — seulement si vide (max 5000 car.)."
                    ),
                },
                "mandate_type": {
                    "type": "string",
                    "enum": ["judiciaire", "service_conseils", "general", "special"],
                    "description": "Nature du mandat.",
                },
                "court_file_number": {
                    "type": "string", "maxLength": 40,
                    "description": ("Numéro de cour ; dérive les métadonnées judiciaires si analysable."),
                },
                "prescription_type": {
                    "type": "string", "maxLength": 40,
                    "description": ("Clé de délai confirmée (ex. 3_ans) — validée par le vocabulaire ; calcule la date pour agir."),
                },
                "fee_type": {
                    "type": "string",
                    "enum": ["hourly", "flat", "contingency", "mixed",
                             "pro_bono", "aide_juridique"],
                    "description": "Entente d'honoraires.",
                },
                "fee_notes": {
                    "type": "string", "maxLength": 1000,
                    "description": "Texte libre sur l'entente d'honoraires.",
                },
                "valeur": {
                    "type": "integer", "minimum": 1,
                    "description": "Valeur en litige, en cents entiers.",
                },
                "hourly_rate": {
                    "type": "integer", "minimum": 1,
                    "description": ("Taux horaire en cents — rempli seulement si encore au défaut 30000."),
                },
                "flat_fee": {
                    "type": "integer", "minimum": 1,
                    "description": "Forfait en cents.",
                },
                "contingency_percent": {
                    "type": "integer", "minimum": 1, "maximum": 10000,
                    "description": "Points de base (2500 = 25,00 %).",
                },
                "droit_action_date": _date(
                    "Point de départ du délai de prescription, AAAA-MM-JJ."),
                "date_avis": _date(
                    "Date confirmée d'avis préalable, AAAA-MM-JJ."),
                "prise_action_date": _date(
                    "Date du dépôt de la demande (art. 2892) — coupe l'alerte. Préférer `record_prescription_event`."),
                **_write_protocol_props(),
            },
            "required": ["dossier_id"],
            "additionalProperties": False,
        },
        "handler": "complete_dossier",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "record_signification": {
        "title": "Consigner une signification",
        "description": (
            (
            "ÉCRITURE append-only — inscrit une signification à une partie "
            "DU dossier (une par partie signifiée, délais arts. 145/147 "
            "C.p.c.). Partie hors dossier refusée. Passer `supersedes` "
            "lorsqu'un PV corrigé remplace un antérieur (marqué remplacé, "
            "jamais supprimé). Jamais modifiable ni supprimable ici. "
            "Confirmer avant d'appeler."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id("UUIDv4 du dossier. Requis."),
                "partie_id": _id(
                    "Contact de la partie signifiée — doit figurer AU dossier (voir get_dossier)."
                ),
                "date": _date("Date de signification, AAAA-MM-JJ. Requis."),
                "mode": {
                    "type": "string",
                    "enum": ["personnelle", "domicile", "huissier",
                             "notification", "avocat", "publication"],
                    "description": "Mode de signification. Défaut 'huissier'.",
                },
                "huissier_id": _id(
                    "Id optionnel du contact de l'huissier."
                ),
                "pv_document_id": _id(
                    "Id optionnel du document du procès-verbal."
                ),
                "supersedes": _id(
                    "Id de la signification ANTÉRIEURE remplacée (get_dossier.significations) — cas du 2e PV corrigé."
                ),
                "confirmee": {
                    "type": "boolean",
                    "description": "true dès que le procès-verbal est en main.",
                },
                **_write_protocol_props(),
            },
            "required": ["dossier_id", "partie_id", "date"],
            "additionalProperties": False,
        },
        "handler": "record_signification",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "record_prescription_event": {
        "title": "Consigner un événement de prescription",
        "description": (
            (
            "ÉCRITURE append-only — inscrit un événement C.c.Q. de "
            "prescription sur le dossier : `interruption_depot` (art. 2892 — "
            "demande déposée, coupe l'alerte, date effective null art. "
            "2896), `interruption_reconnaissance` (art. 2898 — repart de la "
            "date), `suspension` (art. 2904 — requiert `end_date`), "
            "`renonciation` (art. 2883). `prescription_date` brute reste "
            "intacte ; retourne `prescription_status` et "
            "`prescription_date_effective`. Jamais modifiable ni supprimable "
            "ici. Confirmer avant d'appeler."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id("UUIDv4 du dossier. Requis."),
                "type": {
                    "type": "string",
                    "enum": ["interruption_depot",
                             "interruption_reconnaissance",
                             "suspension", "renonciation"],
                    "description": "Type d'événement C.c.Q.",
                },
                "date": _date("Date de l'événement, AAAA-MM-JJ. Requis."),
                "end_date": _date(
                    "Fin de suspension, AAAA-MM-JJ — requise seulement pour type=suspension."
                ),
                "reference": {
                    "type": "string", "maxLength": 300,
                    "description": ("Texte libre : article, pièce, circonstance (français)."),
                },
                "document_id": _id(
                    "Id optionnel du document appuyant l'événement."
                ),
                **_write_protocol_props(),
            },
            "required": ["dossier_id", "type", "date"],
            "additionalProperties": False,
        },
        "handler": "record_prescription_event",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "update_time_entry": {
        "title": "Corriger une entrée de temps",
        "description": (
            (
            "ÉCRITURE — REMPLACE les champs nommés (omis = intact). Corrige "
            "avant facturation ; une fois facturée, seule la phase reste "
            "reclassable (`set_time_entry_phase_bulk`), sauf annulation de "
            "facture (`update_invoice` `annulée`). `dossier_id` DÉPLACE avec "
            "son réel budgétaire (non facturable = 0 $). `amount` est "
            "toujours recalculé (`hours × rate`, ou 0 si non facturable). "
            "Omettre `billable` sauf pour le changer. Omettre les deux clés "
            "de phase laisse le classement intact."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "time_entry_id": _id("UUIDv4 de l'entrée à corriger. Requis."),
                "date": _date("Corriger la date, AAAA-MM-JJ."),
                "description": {
                    "type": "string", "minLength": 1, "maxLength": 2000,
                    "description": (
                        "Libellé français imprimé VERBATIM sur la facture client (aucune mention ajoutée)."
                    ),
                },
                "hours": {
                    "type": "number", "minimum": 0.01, "maximum": 24,
                    "description": (
                        "Au plus deux décimales (0.25 = quart d'heure) ; plus fin refusé, jamais arrondi."
                    ),
                },
                "rate_cents": {
                    "type": "integer", "minimum": 0, "maximum": 100000000,
                    "description": "Taux horaire en cents ; 0 est admis.",
                },
                "billable": {
                    "type": "boolean",
                    "description": (
                        "Envoyer SEULEMENT pour changer. Non facturable vaut toujours 0 $."
                    ),
                },
                "dossier_id": _id(
                    "Déplacer l'entrée vers ce dossier (list_dossiers)."),
                **_phase_props(on_omit=None),
                **_legacy_ref_prop(),
                **_expected_etag_prop(_TIME_ENTRY_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["time_entry_id"],
            "additionalProperties": False,
        },
        "handler": "update_time_entry",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _TIME_ENTRY_ETAG_READERS,
    },
    "update_expense": {
        "title": "Corriger un déboursé",
        "description": (
            (
            "ÉCRITURE — REMPLACE les champs nommés (omis = intact). Après "
            "facturation, seule la phase reste reclassable "
            "(`set_expense_phase_bulk`) sauf annulation de facture. "
            "`dossier_id` DÉPLACE vers un autre dossier avec son impact "
            "budgétaire. `amount_cents` reste exact tel que saisi. Omettre "
            "`taxable` sauf pour le changer ; omettre les deux clés de phase "
            "conserve la paire."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "expense_id": _id("UUIDv4 du déboursé à corriger. Requis."),
                "date": _date("Corriger la date, AAAA-MM-JJ."),
                "description": {
                    "type": "string", "minLength": 1, "maxLength": 2000,
                    "description": "Libellé imprimé VERBATIM sur la facture client.",
                },
                "amount_cents": {
                    "type": "integer", "minimum": 1, "maximum": 100000000,
                    "description": "Montant en cents entiers.",
                },
                "category": {
                    "type": "string", "enum": _EXPENSE_CATEGORIES,
                    "description": "Catégorie du déboursé.",
                },
                "taxable": {
                    "type": "boolean",
                    "description": "Envoyer SEULEMENT pour changer.",
                },
                "dossier_id": _id(
                    "Déplacer le déboursé vers ce dossier (list_dossiers)."),
                **_phase_props(on_omit=_PHASE_OMIT_KEPT),
                **_legacy_ref_prop(),
                **_expected_etag_prop(_EXPENSE_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["expense_id"],
            "additionalProperties": False,
        },
        "handler": "update_expense",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _EXPENSE_ETAG_READERS,
    },
    "set_time_entry_phase_bulk": {
        "title": "Reclasser la phase de plusieurs entrées de temps",
        "description": (
            (
            "ÉCRITURE — reclasse la phase (`phase`/`sous_phase`) de 1 à 50 "
            "entrées de temps (même déjà facturées : la phase ne paraît sur "
            "aucune facture, seulement au suivi budgétaire réel vs prévu). "
            "`sous_phase` seul dérive `phase` ; `phase` seul impute à « -00 "
            "» Général. `results` suit l'ordre d'entrée (appliqué, inchangé "
            "si identique, ou refus isolé sans bloquer les voisins). Doublon "
            "d'id = refus total. Rejeu sur `idempotency_key` renvoie le "
            "rapport stocké : utiliser une NOUVELLE clé après correction."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entries": _phase_bulk_items(
                    "time_entry_id", "Entrée de temps à reclasser."
                ),
                **_write_protocol_props(),
            },
            "required": ["entries"],
            "additionalProperties": False,
        },
        "handler": "set_time_entry_phase_bulk",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "A batch carries no per-row token yet (an item-level "
            "expected_etag can come later); each row is still compared "
            "and set against the etag the handler has just read."
        ),
        "annotations": {"idempotentHint": True},
    },
    "set_expense_phase_bulk": {
        "title": "Reclasser la phase de plusieurs déboursés",
        "description": (
            (
            "ÉCRITURE — reclasse la phase de 1 à 50 déboursés (même déjà "
            "facturés ; alimentent la moitié « frais » du réel budgétaire). "
            "Même contrat que `set_time_entry_phase_bulk` : résultats "
            "ordonnés, refus isolé sans bloquer les voisins, doublon d'id "
            "refuse tout l'appel, NOUVELLE `idempotency_key` après "
            "correction."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entries": _phase_bulk_items(
                    "expense_id", "Déboursé à reclasser."
                ),
                **_write_protocol_props(),
            },
            "required": ["entries"],
            "additionalProperties": False,
        },
        "handler": "set_expense_phase_bulk",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "Same as set_time_entry_phase_bulk: each row is compared and "
            "set against the etag the handler has just read."
        ),
        "annotations": {"idempotentHint": True},
    },
    "import_invoice": {
        "title": "Importer une facture du système précédent",
        "description": (
            (
            "ÉCRITURE — recrée une facture du système précédent sous SON "
            "PROPRE numéro et sa date (compteur annuel intact). APPROCHE PAR "
            "SOURCES : créer temps et déboursés réels d'abord. "
            "`expected_total_cents` REQUIS (total papier AVANT provision) : "
            "tout écart refuse sans tolérance (`adjustment` nomme une "
            "remise/arrondi réel). Comparer sous-total, TPS et TVQ au PDF. "
            "Atterrit en `brouillon` (aucun envoi ; `update_invoice` marque "
            "`envoyée` sur mot de l'avocat ; paiements par registres "
            "comptables). Gèle les sources sauf phase (`set_*_phase_bulk`) "
            "jusqu'à annulation (`update_invoice` `annulée`) ; la facture "
            "annulée garde son n° (réimportable seulement après suppression à "
            "l'app)."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id("Dossier auquel cette facture appartient."),
                "invoice_number": {
                    "type": "string", "minLength": 1, "maxLength": 32,
                    "description": (
                        "Numéro déjà porté par la facture. Refusé s'il entre dans la série live `YYYY-F…` de l'année, existe déjà ou dépasse la longueur."
                    ),
                },
                "date": _date("Date ORIGINALE de facture, AAAA-MM-JJ (jamais future UTC)."),
                "due_date": _date("Défaut : date + 1 mois civil (31 janv. → 28/29 févr.)."),
                "expected_total_cents": {
                    "type": "integer", "minimum": 0, "maximum": 100000000000,
                    "description": (
                        "Grand total imprimé sur la facture papier en cents AVANT provision — pas le solde dû."
                    ),
                },
                "time_entry_ids": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 64},
                    "description": (
                        "Entrées de temps à facturer (toutes existantes, non facturées et de ce dossier, sinon refus total)."
                    ),
                },
                "expense_ids": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 64},
                    "description": "Déboursés à facturer, mêmes règles.",
                },
                "retainer_applied_cents": {
                    "type": "integer", "minimum": 0, "maximum": 100000000000,
                    "description": (
                        "Provision déduite sur l'original (sinon solde surestimé)."
                    ),
                },
                "adjustment": {
                    "type": "object",
                    "description": (
                        "Recours quand le total imprimé diffère des lignes ; crée une ligne d'honoraires nommée."
                    ),
                    "properties": {
                        "amount_cents": {
                            "type": "integer",
                            "description": (
                                "Peut être négatif (réduction) ; jamais 0."
                            ),
                        },
                        "description": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 500,
                            "description": (
                                "Libellé français imprimé sur la facture (« Remise de courtoisie », « Arrondi »). Requis."
                            ),
                        },
                        "taxable": {
                            "type": "boolean",
                            "description": (
                                "true (défaut) : TPS/TVQ calculées sur le montant ajusté ; false : rabais après taxes."
                            ),
                        },
                    },
                    "required": ["amount_cents", "description"],
                    "additionalProperties": False,
                },
                "notes": {
                    "type": "string", "maxLength": 1500,
                    "description": "Notes portées sur la facture.",
                },
                **_legacy_ref_prop(),
                **_write_protocol_props(),
            },
            "required": [
                "dossier_id", "invoice_number", "date", "expected_total_cents",
            ],
            "additionalProperties": False,
        },
        "handler": "import_invoice",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "Creates an invoice and replaces no value it names; the sources "
            "it flips are re-read and etag-compared inside the invoice's own "
            "transaction."
        ),
    },
    # ── Lot 3b — BILL ───────────────────────────────────────────────────
    "create_invoice": {
        "title": "Émettre une facture",
        "description": (
            (
            "ÉCRITURE — émet une NOUVELLE facture depuis des sources "
            "facturables non facturées du dossier (au 1er client). Appeler "
            "`preview_invoice` d'abord avec la même sélection et passer son "
            "`total_cents` dans `expected_total_cents`. Consomme "
            "définitivement le prochain `AAAA-F###` (jamais réémis, même "
            "annulé) et crée en `brouillon` (rien d'envoyé). Sources gelées "
            "(sauf phase via `set_*_phase_bulk`) jusqu'à annulation. "
            "`idempotency_key` REQUISE ; confirmer d'abord. Si issue "
            "incertaine, relire `list_invoices` avant de réessayer avec la "
            "MÊME clé."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                **_invoice_selection_props(),
                "notes": {
                    "type": "string", "maxLength": INVOICE_NOTES_MAX_CHARS,
                    "description": "Imprimées sur la facture, en français.",
                },
                "expected_total_cents": {
                    "type": "integer", "minimum": 0,
                    "description": (
                        "Total `total_cents` de preview_invoice pour cette sélection. Requis."),
                },
                **_write_protocol_props(),
            },
            "required": ["dossier_id", "expected_total_cents",
                         "idempotency_key"],
            "additionalProperties": False,
        },
        "handler": "create_invoice",
        "scope": SCOPE_WRITE,
        # A second invoice is a second PERMANENT number: a retry must replay.
        "idempotency": IDEMPOTENCY_REQUIRED,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "Creates an invoice and replaces no value it names; its sources "
            "are re-read and etag-compared inside the invoice transaction, "
            "and expected_total_cents ties it to the preview."
        ),
    },
    "update_invoice": {
        "title": "Modifier une facture",
        "annotations": {
            # The state asked for, when already stored, writes nothing.
            "idempotentHint": True,
        },
        "description": (
            (
            (
            (
            (
            (
            "ÉCRITURE — un changement par appel contre `expected_etag` "
            "(`get_invoice`). BROUILLON : `notes`, `due_date`, "
            "`refresh_billing_address` (montants, lignes, client, numéro "
            "figés). STATUT : `brouillon` → `envoyée` (ne transmet rien au "
            "client, atteste l'envoi sur mot de l'avocat — requis pour "
            "paiement fidéicommis, art. 56 2°). Lire `is_overdue` (jamais le "
            "statut stocké) : un job quotidien à 09:00 Montréal lorsqu'actif "
            "(l'avocat peut le désactiver) passe `envoyée` → `en_retard` (ou "
            "`connector_transitions`). ANNULATION : `annulée` + "
            "`void_reason` libère les sources (refusée si paiement actif) — "
            "une facture émise ne se corrige qu'en l'annulant puis créant "
            "une nouvelle ; n° conservé (un n° importé n'est réimportable "
            "qu'après suppression à l'app). Jamais `payée` ici."
        )
        )
        )
        )
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_id": _id("UUIDv4 de la facture (list_invoices). Requis."),
                "status": {
                    "type": "string", "enum": _INVOICE_TARGET_STATUSES,
                    "description": "STATUT ou ANNULATION (annulée).",
                },
                "void_reason": {
                    "type": "string", "minLength": 1,
                    "maxLength": VOID_REASON_MAX_CHARS,
                    "description": (
                        "annulée : REQUIS — motif conservé sur la facture, en français."),
                },
                "notes": {
                    "type": "string", "maxLength": INVOICE_NOTES_MAX_CHARS,
                    "description": "BROUILLON : remplace les notes ; \"\" les vide.",
                },
                "due_date": _date("BROUILLON : AAAA-MM-JJ, pas avant la date de facture."),
                "refresh_billing_address": {
                    "type": "boolean",
                    "description": (
                        "BROUILLON : true recopie l'adresse actuelle du client."),
                },
                **_expected_etag_prop(_INVOICE_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["invoice_id", "expected_etag"],
            "additionalProperties": False,
        },
        "handler": "update_invoice",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_REQUIRED,
        "etag_readers": _INVOICE_ETAG_READERS,
    },
    "create_budget_version": {
        "title": "Nouvelle version du budget",
        "annotations": {
            # A version identical to the one in force writes nothing.
            "idempotentHint": True,
        },
        "description": (
            (
            "ÉCRITURE — enregistre une NOUVELLE version de budget (référence "
            "du PDF client « Estimation ») ; les versions antérieures sont "
            "conservées intactes. `mode=\"replace\"` : `lines` remplace tout "
            "(heure/frais omis = 0). `mode=\"merge\"` : met à jour les valeurs "
            "nommées par sous-code (les deux à 0 supprime la ligne). Exige "
            "`base_version` de `get_budget` (refusé si dépassé ; identique = "
            "sans écriture)."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id("Dossier (list_dossiers). Requis."),
                "base_version": {
                    "type": "integer", "minimum": 0,
                    "description": (
                        "Champ `base_version` de get_budget (0 = aucun budget). Requis."),
                },
                "mode": {
                    "type": "string", "enum": _BUDGET_MODES,
                    "description": "replace ou merge. Requis.",
                },
                "lines": {
                    "type": "array",
                    "maxItems": len(_BUDGET_SOUS_PHASE_CODES),
                    "description": "Une ligne par sous-code, chacun au plus une fois.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sous_phase": {
                                "type": "string",
                                "enum": _BUDGET_SOUS_PHASE_CODES,
                                "description": (
                                    "Sous-code de phase (get_reference_vocabulary kind phases)."),
                            },
                            "hours": {
                                "type": "number", "minimum": 0,
                                "maximum": 10000,
                                "description": (
                                    "Heures estimées, max deux décimales."),
                            },
                            "frais_cents": {
                                "type": "integer", "minimum": 0,
                                "maximum": 100000000,
                                "description": "Déboursés estimés, en cents.",
                            },
                        },
                        "required": ["sous_phase"],
                        "additionalProperties": False,
                    },
                },
                "hourly_rate_cents": {
                    "type": "integer", "minimum": 0, "maximum": 100000000,
                    "description": (
                        "Défaut : taux de la version en vigueur, sinon du dossier."),
                },
                "note": {
                    "type": "string", "maxLength": BUDGET_NOTE_MAX_CHARS,
                    "description": (
                        "Hypothèses en français. Omise : conservée ; \"\" la vide."),
                },
                **_write_protocol_props(),
            },
            "required": ["dossier_id", "base_version", "mode"],
            "additionalProperties": False,
        },
        "handler": "create_budget_version",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "Appends a version and overwrites none; base_version is its "
            "version check, compared inside the model's counter transaction."
        ),
    },
    "create_dossier": {
        "title": "Créer un dossier",
        "description": (
            (
            "ÉCRITURE — ouvre un dossier (adapté à la reprise : `status` "
            "`fermé`/`archivé` et `opened_date`/`closed_date` acceptés ; un "
            "dossier créé fermé reste hors DavX5). Chaque `partie_id` est "
            "résolu et ses noms figés (pour gabarits) ; id inconnu refusé. "
            "Consulter `get_reference_vocabulary` pour `domaine`, `action`, "
            "`prescription_type`, `forum`, `district`. `hourly_rate` accepte "
            "0 (pro bono/aide juridique ; défaut des entrées de temps). "
            "Vérifier `find_imported` et passer `legacy_ref` (aucune "
            "suppression ici). Crée l'arborescence par défaut (sinon "
            "avertissement)."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_number": {
                    "type": "string", "minLength": 1, "maxLength": 40,
                    "description": (
                        "Numéro du dossier au cabinet (ex. « 2019-014 »). Refusé si déjà utilisé."
                    ),
                },
                "title": {
                    "type": "string", "minLength": 1, "maxLength": 300,
                    "description": "Ex. « Tremblay c. Lavoie ».",
                },
                "clients": {
                    "type": "array",
                    "description": (
                        "Au moins un. Chaque entrée référence un contact EXISTANT."
                    ),
                    "items": _party_entry_props(),
                },
                "opposing_parties": {
                    "type": "array",
                    "description": "Parties adverses, même structure que `clients`.",
                    "items": _party_entry_props(),
                },
                "status": {
                    "type": "string", "enum": _DOSSIER_STATUSES,
                    "description": (
                        "Défaut « actif ». En reprise, souvent « fermé » ou « archivé ». Modifiable ensuite via set_dossier_status."
                    ),
                },
                "opened_date": _date("Date d'ouverture, AAAA-MM-JJ."),
                "closed_date": _date(
                    "Date de clôture, AAAA-MM-JJ. Auto-inscrite si statut fermé/archivé sans date."
                ),
                **_forum_props(),
                **_dossier_field_props(),
                **_legacy_ref_prop(),
                **_write_protocol_props(),
            },
            "required": ["file_number", "title", "clients"],
            "additionalProperties": False,
        },
        "handler": "create_dossier",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "update_dossier": {
        "title": "Corriger un dossier",
        "description": (
            (
            "ÉCRITURE — REMPLACE les champs nommés (omis = intact). Utiliser "
            "`complete_dossier` pour ne remplir que les champs vides sans "
            "risquer d'écraser. `status` et `closed_date` relèvent de "
            "`set_dossier_status` (DavX5) ; `file_number` est refusé (figé "
            "aux factures). Parties UNIQUEMENT additives via `add_clients` / "
            "`add_opposing_parties` (doublon sur un côté refusé). Rôles, "
            "avocat, détachement et rafraîchissement des noms relèvent de "
            "`update_dossier_party`."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id("UUIDv4 du dossier à corriger. Requis."),
                "title": {"type": "string", "maxLength": 300,
                          "description": "Nouveau titre."},
                "sommaire": {"type": "string", "maxLength": 5000,
                             "description": "Sommaire libre du dossier."},
                "opened_date": _date("Corriger la date d'ouverture, AAAA-MM-JJ."),
                "add_clients": {
                    "type": "array",
                    "description": (
                        "Parties à AJOUTER comme clients. Refusé si déjà liées au dossier."
                    ),
                    "items": _party_entry_props(),
                },
                "add_opposing_parties": {
                    "type": "array",
                    "description": (
                        "Parties à AJOUTER en adverse. Refusé si déjà liées au dossier."
                    ),
                    "items": _party_entry_props(),
                },
                **_forum_props(),
                **_dossier_field_props(),
                **_legacy_ref_prop(),
                **_expected_etag_prop(_DOSSIER_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["dossier_id"],
            "additionalProperties": False,
        },
        "handler": "update_dossier",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _DOSSIER_ETAG_READERS,
    },
    "set_dossier_status": {
        "title": "Changer le statut d'un dossier",
        "description": (
            (
            (
            (
            (
            (
            "ÉCRITURE — change le statut comme l'app, DavX5 inclus : "
            "`fermé`/`archivé` VIDE la collection téléphone (données gardées "
            "à l'app, alertes coupées) ; `actif`/`en_attente` la RESTAURE "
            "(rouvrir efface `closed_date`). Entre `actif` et `en_attente`, "
            "rien ne change au téléphone. Même statut = sans écriture (sauf "
            "nouveau `closed_date`) et réapplique DavX5. Rejeu : (1) "
            "`dav.complete: false` → MÊME statut + MÊME clé ; (2) en cours → "
            "attendre + MÊME clé ; (3) interrompu ou `warnings` indiquent "
            "statut déplacé → relire `get_dossier` puis NOUVELLE clé. Lire "
            "`warnings`."
        )
        )
        )
        )
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "UUIDv4 du dossier (list_dossiers / get_dossier)."
                ),
                "status": {
                    "type": "string", "enum": _DOSSIER_STATUSES,
                    "description": "Statut cible à appliquer.",
                },
                "closed_date": _date(
                    "fermé / archivé seulement : AAAA-MM-JJ (jamais future ni avant ouverture ; défaut aujourd'hui à la fermeture)."
                ),
                **_write_protocol_props(),
            },
            "required": ["dossier_id", "status"],
            "additionalProperties": False,
        },
        "handler": "set_dossier_status",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "a status is a TARGET, never a value rebuilt from a read — asking "
            "again for the one stored is the tool's repair — and the write "
            "compare-and-sets against the tool's own read"
        ),
        "annotations": {"idempotentHint": True},
    },
    "update_dossier_party": {
        "title": "Tenir les parties d'un dossier",
        "description": (
            (
            "ÉCRITURE — un LIEN partie-dossier. `update` : remplace `roles` "
            "(`[]` vide) et/ou `avocat_partie_id` (`` retire) et recalcule "
            "le rôle pour gabarits. `remove` : DÉTACHE (contact subsiste, "
            "tracé dans `list_deletions` `dossier_party`) — refusé pour le "
            "dernier client, partie signifiée ou avec fidéicommis sur ce "
            "dossier (pour ajouter : `update_dossier`). `refresh_names` : "
            "actualise les noms figés (un dossier ou <= 50 dossiers d'un "
            "contact ; réessayer selon `warnings` si un dossier refuse ; "
            "factures, fidéicommis et documents générés gardent "
            "leur nom d'origine)."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["update", "remove", "refresh_names"],
                    "description": "Action souhaitée.",
                },
                "dossier_id": _id(
                    "update / remove : le dossier. refresh_names : UN dossier (ou passer partie_id)."
                ),
                "partie_id": _id(
                    "update / remove : id du contact lié (get_dossier). refresh_names : tous ses dossiers (partie ou avocat)."
                ),
                "side": {
                    "type": "string", "enum": ["clients", "opposing_parties"],
                    "description": (
                        "update / remove : requis seulement si un ancien dossier liste le contact des deux côtés."
                    ),
                },
                "roles": {
                    "type": "array",
                    "items": {"type": "string", "enum": _PARTY_ROLES},
                    "maxItems": len(_PARTY_ROLES),
                    "description": (
                        "update : rôles procéduraux — remplacement COMPLET sans doublon."
                    ),
                },
                "avocat_partie_id": _id(
                    "update : contact de l'avocat (list_parties) ou \"\" pour retirer ; nom figé (`avocat_id_before`/`_after`)."
                ),
                **_expected_etag_only_for(
                    _DOSSIER_ETAG_READERS, "update / remove"),
                **_write_protocol_props(),
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        "handler": "update_dossier_party",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _DOSSIER_ETAG_READERS,
    },
    "create_partie": {
        "title": "Créer un contact",
        "description": (
            (
            "ÉCRITURE — crée un contact (client, partie adverse, avocat, "
            "expert, huissier, sténographe, `fournisseur`…). Vérifier "
            "`find_imported` et passer `legacy_ref` en reprise (aucune "
            "suppression ici). `individual` exige `last_name` ; "
            "`organization` exige `organization_name` (ne pas mêler). "
            "ADRESSE EN BLOC : envoyer les 6 clés ensemble (`street`, "
            "`unit`, `city`, `province`, `postal_code`, `country`), sinon "
            "les absentes prennent Montréal / Québec / Canada par défaut. "
            "KYC/conflit : utiliser `record_kyc_status` (inscrit PRÉSUMÉ)."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string", "enum": _PARTIE_TYPES,
                    "description": "individual (personne physique) ou organization (personne morale).",
                },
                "contact_role": {
                    "type": "string", "enum": _CONTACT_ROLES,
                    "description": (
                        "Rôle du contact. Défaut « client ». Un « fournisseur » ne synchronise pas au téléphone."
                    ),
                },
                **_partie_identity_props(),
                **_legacy_ref_prop(),
                **_write_protocol_props(),
            },
            "required": ["type"],
            "additionalProperties": False,
        },
        "handler": "create_partie",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "update_partie": {
        "title": "Corriger un contact",
        "description": (
            (
            "ÉCRITURE — REMPLACE les champs nommés : envoyer SEULEMENT ce "
            "qui change (omis = intact ; vide `` = EFFACÉ ; ne jamais "
            "renvoyer toute la fiche `get_partie`). Même règle du BLOC "
            "ADRESSE à 6 clés : relire `get_partie` et renvoyer complet. "
            "`type` est immuable ; KYC via `record_kyc_status` ; mandataires "
            "via `update_partie_mandataire`. Le modèle revalide toute la "
            "fiche fusionnée (corriger d'abord un vieux téléphone invalide)."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "partie_id": _id("UUIDv4 du contact à corriger. Requis."),
                "contact_role": {
                    "type": "string", "enum": _CONTACT_ROLES,
                    "description": (
                        "Changer le rôle : « fournisseur » retire du téléphone, tout autre l'y remet."
                    ),
                },
                **_partie_identity_props(),
                **_legacy_ref_prop(),
                **_expected_etag_prop(_PARTIE_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["partie_id"],
            "additionalProperties": False,
        },
        "handler": "update_partie",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _PARTIE_ETAG_READERS,
    },
    "update_partie_mandataire": {
        "title": "Tenir les mandataires d'un contact",
        "description": (
            (
            "ÉCRITURE — une représentation d'un contact (mandataire, tuteur, "
            "curateur…). `add` : le mandataire doit être un contact "
            "`individual` du MÊME `contact_role`, jamais soi-même (doublon "
            "identique = sans écriture ; modification = `update`). `update` "
            "remplace `kind` et/ou `notes`. `remove` DÉTACHE (contact "
            "subsiste, tracé dans `list_deletions` `mandataire`). `notes` "
            "refusées passé 2000 car. ou avec chevrons."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string", "enum": ["add", "update", "remove"],
                    "description": "Action souhaitée.",
                },
                "partie_id": _id("Contact REPRÉSENTÉ (UUIDv4)."),
                "mandataire_partie_id": _id(
                    "Contact du mandataire (UUIDv4, list_parties)."
                ),
                "kind": {
                    "type": "string", "enum": _MANDATAIRE_KINDS,
                    "description": (
                        "add (requis) / update : type de représentation."
                    ),
                },
                "notes": {
                    "type": "string", "maxLength": 2000,
                    "description": (
                        "add / update : notes de représentation — REMPLACÉES sur update (\"\" vide)."
                    ),
                },
                **_expected_etag_prop(_PARTIE_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["action", "partie_id", "mandataire_partie_id"],
            "additionalProperties": False,
        },
        "handler": "update_partie_mandataire",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _PARTIE_ETAG_READERS,
    },
    "record_kyc_status": {
        "title": "Inscrire une vérification de conformité (présumée)",
        "description": (
            f"ÉCRITURE — inscrit l'identité ou le conflit d'intérêts d'un client comme PRÉSUMÉ (« inscrit par Claude le … — à confirmer » ; reste OUVERT dans `get_coverage_report` jusqu'à confirmation dans l'app — jamais confirmé ici). REFUSÉ sur statut décidé — {_KYC_DECIDED_TEXT} — dont `*_presumed` est false : lui seul modifie son attestation. Client seulement. `notes` AJOUTÉES sous « [AAAA-MM-JJ — inscrit par Claude] ». `non_vérifié` (« À faire ») retire votre propre inscription présumée. Tout conflit détecté doit être signalé aussitôt à l'avocat (aucune notification automatique)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "partie_id": _id("Contact du client (UUIDv4)."),
                "check": {
                    "type": "string", "enum": _KYC_CHECK_NAMES,
                    "description": (
                        "identity = vérification d'identité ; conflict = contrôle de conflit d'intérêts."
                    ),
                },
                "status": {
                    "type": "string", "enum": _KYC_STATUSES,
                    "description": (
                        _KYC_STATUS_TEXT + " Pour identity, identifié = identité au dossier sans pièce ; vérifié = contrôlée sur documents fiables. N'inscrire que sur directive de l'avocat."
                    ),
                },
                "notes": {
                    "type": "string", "maxLength": 1000,
                    "description": (
                        "Ajoutées aux notes du contrôle sous ligne datée (éléments vérifiés, méthode ; jamais recitées)."
                    ),
                },
                **_expected_etag_prop(_PARTIE_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["partie_id", "check", "status"],
            "additionalProperties": False,
        },
        "handler": "record_kyc_status",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _PARTIE_ETAG_READERS,
    },
    # ════════════════════════════════════════════════════════════════════
    # Lecture du CONTENU d'un document (2026-08). L'écran de consentement
    # nomme le fait que le contenu intégral d'une pièce transite par
    # claude.ai — c'est la divulgation qui rend cet outil acceptable.
    # ════════════════════════════════════════════════════════════════════
    "get_document_text": {
        "title": "Texte d'un document",
        "description": (
            f"Lit la COUCHE TEXTE d'un document stocké (PDF et .docx ; pas les modèles), compagnon de `list_documents`. Page numérisée/image sans texte signalée honnêtement (`has_text: false`, `pages_without_text`) : aucune OCR ; échec de lecture signalé (`page_extraction_failed:<n>`). Sortie plafonnée à {DOCUMENT_TEXT_MAX_CHARS} car. par appel : suivre `next_page` via `page_range`. PDF chiffrés, `.doc`, images, ZIP refusés en français (consulter à l'app). Document privilégié : citer le strict nécessaire, JAMAIS dans `web_search`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": _id(
                    "UUIDv4 du document à lire (list_documents)."
                ),
                "page_range": {
                    "type": "string",
                    "maxLength": 12,
                    "description": (
                        "\"4\" = dès page 4 ; \"2-6\" = plage inclusive (1-based). Omis = depuis page 1 ; repasser `next_page` pour continuer."
                    ),
                },
            },
            "required": ["document_id"],
            "additionalProperties": False,
        },
        "handler": "get_document_text",
    },
    "record_document_analysis": {
        "title": "Enregistrer l'analyse d'un document",
        "description": (
            (
            "ÉCRITURE — enregistre l'analyse d'un document lu via "
            "`get_document_text` (jamais sur le seul nom de fichier). "
            "Fournir une `sous_nature` de la table fermée (Annexe A) : la "
            "catégorie en est DÉRIVÉE et remplace toute catégorie "
            "présumée/défaut, JAMAIS celle choisie par l'avocat "
            "(`category_set_by_lawyer` : conservée avec alerte "
            "`analyse.divergence_categorie`). Toujours marquée PRÉSUMÉE "
            "jusqu'à confirmation (une confirmation antérieure ne couvre "
            "jamais un nouveau passage) ; chaque passage est journalisé "
            "append-only. Privilèges CUMULATIFS vers le haut seulement (seul "
            "l'avocat abaisse à l'app ; ne jamais inventer public). Passer "
            "`idempotency_key`."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": _id(
                    "UUIDv4 du document analysé (list_documents)."
                ),
                "sous_nature": {
                    "type": "string",
                    "enum": _SOUS_NATURE_CODES,
                    "description": (
                        "Code fermé (Annexe A) dont dérive la catégorie (sans paramètre catégorie). Ne jamais inventer un code."
                    ),
                },
                "privileges": {
                    "type": "array",
                    "items": {"type": "string", "enum": _PRIVILEGE_CODES},
                    "description": (
                        "Régimes de protection CUMULATIFS (Annexe D) ; en cas de doute envoyer LES DEUX. [] = confidentiel par défaut, jamais public."
                    ),
                },
                "resume": {
                    "type": "string",
                    "maxLength": 2000,
                    "description": (
                        "Court paragraphe en français de ce qu'est et dit le document (affiché dans l'app)."
                    ),
                },
                "numero_dossier_cour": {
                    "type": "string",
                    "description": (
                        "Numéro de cour tel qu'inscrit VERBATIM sur la pièce ; omettre si absent (signal d'un projet non déposé)."
                    ),
                },
                "tribunal": {
                    "type": "string",
                    "description": (
                        "Tribunal nommé sur le document. Omettre si absent."
                    ),
                },
                "district_judiciaire": {
                    "type": "string",
                    "description": (
                        "District judiciaire nommé sur le document. Omettre si absent."
                    ),
                },
                "auteur": {
                    "type": "string",
                    "description": (
                        "Signataire, huissier, juge, greffier ou sténographe nommé. Omettre si absent."
                    ),
                },
                "parties_mentionnees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Noms des parties tels qu'ÉCRITS (chaînes libres — ne jamais résoudre en contact du dossier)."
                    ),
                },
                "date_document_str": {
                    "type": "string",
                    "description": (
                        "Date SUR le document, AAAA-MM-JJ. Omettre si absente."
                    ),
                },
                "date_signature_str": {
                    "type": "string",
                    "description": (
                        "Date de signature, AAAA-MM-JJ. Omettre si absente."
                    ),
                },
                "contient_dispositif": {
                    "type": "boolean",
                    "description": (
                        "true si un PV d'audience contient le dispositif du jugement (signale un départ de délai d'appel sans le calculer)."
                    ),
                },
                "dispositif": {
                    "type": "string",
                    "maxLength": 2000,
                    "description": (
                        "Texte exact du dispositif lorsque présent."
                    ),
                },
                "indices_protection": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Indices OBSERVÉS justifiant les privilèges (« en-tête d'avocat », « sous toutes réserves ») pour vérification par l'avocat."
                    ),
                },
                "langue_detectee": {
                    "type": "string",
                    "enum": ["fr", "en", "autre"],
                    "description": "Langue principale du document.",
                },
                "confiance": {
                    "type": "string",
                    "enum": ["haute", "moyenne", "faible"],
                    "description": (
                        "Degré de confiance. Indiquer « faible » plutôt que deviner."
                    ),
                },
                "extraction_tronquee": {
                    "type": "boolean",
                    "description": (
                        "true si seule une partie du texte a été lue."
                    ),
                },
                # ── Annexe C — the two axes of evidence law ──────────
                # These four were readable and editable but had NO input
                # property, so the model could not supply them and they
                # stayed empty on every analysis. Spotted in production
                # 2026-08-27 on a TAL decision.
                "moyen_preuve": {
                    "type": "string",
                    "enum": _MOYEN_PREUVE_CODES,
                    "description": (
                        "Moyen de preuve (art. 2811 C.c.Q.) ; omettre sur une procédure ; NON_DETERMINE en cas de doute."
                    ),
                },
                "qualification_ecrit": {
                    "type": "string",
                    "enum": _QUALIFICATION_ECRIT_CODES,
                    "description": (
                        "Qualification de l'écrit (SEULEMENT si moyen_preuve == ECRIT) ; NON_DETERMINE sauf mention probante (art. 2813-2814 C.c.Q.)."
                    ),
                },
                "parait_original": {
                    "type": "boolean",
                    "description": (
                        "Si la pièce PARAÎT être l'original (sceau, signature manuscrite, art. 2860 C.c.Q.) — observation seulement."
                    ),
                },
                "qualite_reconnaissance": {
                    "type": "string",
                    "enum": _QUALITE_RECONNAISSANCE,
                    "description": (
                        "Qualité d'extraction texte pour scan/photo (« faible » signale un besoin d'OCR)."
                    ),
                },
                **_expected_etag_prop(("list_documents",)),
                **_write_protocol_props(),
            },
            "required": ["document_id", "sous_nature"],
            "additionalProperties": False,
        },
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        # Optional (finitions, contracts-6): the exemption this replaced
        # promised « adopts expected_etag with the document edit tools
        # (plan lot 2) », which lot 2 never did — and a lawyer's edit or
        # confirmation of the analysis landing mid-analysis was overwritten.
        # Omitted, the handler compare-and-sets against its own read.
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": ("list_documents",),
        "handler": "record_document_analysis",
    },
    # ── Lot 2A (T7) — FILES: the document and folder edits ─────────────
    "update_document": {
        "title": "Classer un document",
        "annotations": {
            # Values already stored write nothing: a repeat is a no-op.
            "idempotentHint": True,
        },
        "description": (
            (
            (
            (
            "ÉCRITURE — REMPLACE les champs de classement nommés (omis = "
            "intact ; identique = sans écriture) : `display_name`, "
            "`document_date`, `tags`, `folder_id` (reclasse), `category`. "
            "Jamais le fichier ni `notes_internes`. Une `category` écrite "
            "ici est PRÉSUMÉE ; refusée si posée par l'avocat "
            "(`category_set_by_lawyer: true`, ou catégorie antérieure au suivi "
            "autre qu'« autre », qu'une analyse conserve aussi) "
            "ou sur tout document déjà analysé (qui dérive sa catégorie via "
            "`record_document_analysis`)."
        )
        )
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": _id(
                    "UUIDv4 du document (list_documents)."
                ),
                "display_name": {
                    "type": "string", "minLength": 1,
                    "maxLength": DOCUMENT_NAME_MAX_CHARS,
                    "description": (
                        "Nouveau nom affiché en français (chevrons HTML refusés)."
                    ),
                },
                "document_date": _date(
                    "Date PROPRE du document, AAAA-MM-JJ ; \"\" l'efface (date invalide refusée)."
                ),
                "tags": {
                    "type": "array",
                    "maxItems": DOCUMENT_TAGS_MAX,
                    "items": {
                        "type": "string", "minLength": 1,
                        "maxLength": DOCUMENT_TAG_MAX_CHARS,
                        "description": "Une étiquette sans virgule.",
                    },
                    "description": (
                        "Liste COMPLÈTE des étiquettes ([] vide ; sans virgule ni doublon)."
                    ),
                },
                "folder_id": _id(
                    "Reclasser dans ce répertoire du MÊME dossier (include_folders), ou \"\" pour la racine."
                ),
                "category": {
                    "type": "string", "enum": _DOCUMENT_CATEGORY_CHOICES,
                    "description": (
                        "Catégorie stockée PRÉSUMÉE (`category_source` « mcp »). Refusée si document analysé ou posée par l'avocat."
                    ),
                },
                **_expected_etag_prop(_DOCUMENT_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["document_id"],
            "additionalProperties": False,
        },
        "handler": "update_document",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _DOCUMENT_ETAG_READERS,
    },
    "move_documents": {
        "title": "Déplacer des documents",
        "annotations": {
            # A second identical call finds every row already filed there:
            # « unchanged », nothing written.
            "idempotentHint": True,
        },
        "description": (
            f"ÉCRITURE — reclasse jusqu'à {DOCUMENT_MOVE_MAX} documents d'UN dossier dans un répertoire ou à la racine en une écriture atomique. `results` suit l'ordre demandé : `moved`, `unchanged` ou refus individuel sans bloquer les voisins. Répertoire cible inconnu, doublon d'id ou lecture défaillante refusent tout l'appel."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "UUIDv4 du dossier des documents."
                ),
                "document_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": DOCUMENT_MOVE_MAX,
                    "items": {
                        "type": "string", "minLength": 1, "maxLength": 64,
                        "description": "UUIDv4 d'un document (list_documents).",
                    },
                    "description": (
                        "Documents à reclasser sans doublon (ordre suivi dans le rapport)."
                    ),
                },
                "folder_id": _id(
                    "Répertoire cible du dossier (include_folders) ou \"\" pour la racine."
                ),
                **_write_protocol_props(),
            },
            "required": ["dossier_id", "document_ids", "folder_id"],
            "additionalProperties": False,
        },
        "handler": "move_documents",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "A batch carries no per-row token; ONE transaction reads every "
            "row and the target folder, so a write landing meanwhile re-runs "
            "it on fresh data — and it writes folder_id alone."
        ),
    },
    "manage_folder": {
        "title": "Organiser les dossiers de classement",
        "description": (
            (
            (
            (
            (
            (
            "ÉCRITURE — gère les répertoires d'un dossier : `create` (`name` "
            "+ `parent_folder_id`, refus ou `if_exists=\"reuse\"` si doublon), "
            "`rename` (`folder_id` + `name`), `move` (`folder_id` + "
            "`parent_folder_id`, max 5 niveaux). Répertoires système "
            "(`system_role`, non renommables ni déplaçables ici) : « "
            "Factures » et « Déboursés » sous « Mandat », « Projets » sous « "
            "Interne », « Reçus du portail » sous « Autres », et ces trois "
            "parents. « Projets » et « Reçus du portail » restent réservés à "
            "la racine. Aucune suppression."
        )
        )
        )
        )
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string", "enum": ["create", "rename", "move"],
                    "description": "Action souhaitée.",
                },
                "dossier_id": _id(
                    "UUIDv4 du dossier dont modifier l'arborescence."
                ),
                "folder_id": _id(
                    "rename / move : répertoire (include_folders). Refusé pour create."
                ),
                "name": {
                    "type": "string", "minLength": 1,
                    "maxLength": FOLDER_NAME_MAX_CHARS,
                    "description": "create / rename : nom en français.",
                },
                "parent_folder_id": _id(
                    "create : emplacement (\"\"/omis = racine). move : nouveau parent REQUIS (\"\" = racine)."
                ),
                "if_exists": {
                    "type": "string", "enum": ["refuse", "reuse"],
                    "description": (
                        "create seulement : si le nom existe déjà là, \"refuse\" (défaut) ou \"reuse\" (retourne l'existant)."
                    ),
                },
                **_expected_etag_only_for(
                    _FOLDER_ETAG_READERS, "rename / move"),
                **_write_protocol_props(),
            },
            "required": ["action", "dossier_id"],
            "additionalProperties": False,
        },
        "handler": "manage_folder",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _FOLDER_ETAG_READERS,
    },
    # ── Lot 2A (T8) — FILES: new Word documents, never a changed one ────
    "fill_gabarit": {
        "title": "Remplir un gabarit",
        "description": (
            (
            "ÉCRITURE — remplit un gabarit (`kind=\"gabarit\"`) pour UN "
            "dossier et sauve le `.docx` dans son répertoire « Projets » "
            "(sous « Interne »). Consulter d'abord "
            "`list_templates(template_id, dossier_id)` et fournir seulement "
            "les `blocs` et `champs_manuels` exacts demandés (champs auto "
            "remplis par Athéna). Allégations en paragraphes non numérotés "
            "séparés par une LIGNE VIDE (`markdown: true` seulement pour "
            "titres/gras/tableaux). « `{{` » ou « `}}` » refusés. Brouillon "
            "jamais envoyé ; passer `idempotency_key`."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "template_id": _id(
                    "UUIDv4 du gabarit (`kind` « gabarit » seulement, list_templates)."
                ),
                "dossier_id": _id(
                    "REQUIS : dossier source des données et de destination (« Projets »)."
                ),
                "client_id": _id(
                    "Slot « client » : un client de CE dossier (si multiple et utilisé)."
                ),
                "adverse_id": _id(
                    "Slot « adverse » : une partie adverse de CE dossier (si multiple)."
                ),
                "destinataire_id": _id(
                    "Contact destinataire (n'importe quel contact, list_parties ; aucun défaut)."
                ),
                "blocs": {
                    "type": "array",
                    "maxItems": BLOCS_MAX_ITEMS,
                    "description": (
                        "Blocs de contenu par nom (un bloc omis laisse « {{nom}} » pour Word)."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "nom": {
                                "type": "string", "minLength": 1,
                                "maxLength": PLACEHOLDER_NAME_MAX_CHARS,
                                "description": (
                                    "Nom exact du bloc sans accolades tel que listé par list_templates (sensible à la casse)."
                                ),
                            },
                            "contenu": {
                                "type": "string", "minLength": 1,
                                "maxLength": BLOC_MAX_CHARS,
                                "description": (
                                    "Texte en français (paragraphes séparés par une ligne vide)."
                                ),
                            },
                            "markdown": {
                                "type": "boolean",
                                "description": (
                                    "true = formatage Word depuis Markdown (défaut false ; jamais pour allégations numérotées)."
                                ),
                            },
                        },
                        "required": ["nom", "contenu"],
                        "additionalProperties": False,
                    },
                },
                "champs_manuels": {
                    "type": "array",
                    "maxItems": CHAMPS_MANUELS_MAX_ITEMS,
                    "description": (
                        "Champs manuels listés par list_templates (omis = défaut ou « [À COMPLÉTER : nom] »)."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "nom": {
                                "type": "string", "minLength": 1,
                                "maxLength": PLACEHOLDER_NAME_MAX_CHARS,
                                "description": (
                                    "Nom exact du champ sans accolades."
                                ),
                            },
                            "valeur": {
                                "type": "string",
                                "maxLength": CHAMP_MANUEL_MAX_CHARS,
                                "description": (
                                    "Valeur (parmi les options si listées par list_templates)."
                                ),
                            },
                        },
                        "required": ["nom", "valeur"],
                        "additionalProperties": False,
                    },
                },
                **_write_protocol_props(),
            },
            "required": ["template_id", "dossier_id"],
            "additionalProperties": False,
        },
        "handler": "fill_gabarit",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "create_document": {
        "title": "Créer un document Word",
        "description": (
            (
            (
            (
            (
            (
            "ÉCRITURE — ajoute un NOUVEAU `.docx` au dossier (aucune "
            "modification d'existant). `source=\"markdown\"` : `title` + "
            "`markdown` mis en page sur le gabarit `note` actif (`category` "
            "PRÉSUMÉE). `source=\"copy\"` : copie d'un `.docx` dans SON PROPRE "
            "dossier (protection conservée PRÉSUMÉE, catégorie PRÉSUMÉE sauf "
            "choix antérieur de l'avocat). Atterrissent dans « Projets » "
            "sauf `folder_id`. `source=\"invoice_note\"` : note d'honoraires "
            "de `invoice_id` (gabarit `note_honoraires` actif) dans « "
            "Factures » (sous « Mandat », réutilise l'identique sauf "
            "`regenerate: true`). Passer `idempotency_key`."
        )
        )
        )
        )
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string", "enum": _CREATE_DOCUMENT_SOURCES,
                    "description": "Source servant à produire le document.",
                },
                "dossier_id": _id(
                    "markdown : REQUIS. copy : optionnel (si fourni, même dossier que la source)."
                ),
                "title": {
                    "type": "string", "minLength": 1,
                    "maxLength": DOCUMENT_TITLE_MAX_CHARS,
                    "description": (
                        "markdown : REQUIS — titre en français ({{note.titre}} et nom du fichier)."
                    ),
                },
                "markdown": {
                    "type": "string", "minLength": 1,
                    "maxLength": MARKDOWN_DOCUMENT_MAX_CHARS,
                    "description": (
                        "markdown : REQUIS — corps Markdown (HTML brut et chevrons refusés ; liens `[texte](url)`)."
                    ),
                },
                "category": {
                    "type": "string", "enum": _DOCUMENT_CATEGORY_CHOICES,
                    "description": (
                        "markdown : stockée PRÉSUMÉE (défaut : catégorie du gabarit)."
                    ),
                },
                "document_date": _date(
                    "markdown : date PROPRE du document, AAAA-MM-JJ."
                ),
                "document_id": _id(
                    "copy : REQUIS — `.docx` stocké à copier (list_documents)."
                ),
                "display_name": {
                    "type": "string", "minLength": 1,
                    "maxLength": DOCUMENT_NAME_MAX_CHARS,
                    "description": (
                        "copy : nom de la copie (défaut « Copie de … »)."
                    ),
                },
                "folder_id": _id(
                    "Répertoire du dossier (include_folders) ou \"\" pour racine (défaut « Projets »)."
                ),
                "invoice_id": _id(
                    "invoice_note : REQUIS — UUIDv4 de la facture (list_invoices)."),
                "regenerate": {
                    "type": "boolean",
                    "description": (
                        "invoice_note : créer une nouvelle note même si une copie identique existe (défaut false)."),
                },
                **_write_protocol_props(),
            },
            "required": ["source"],
            "additionalProperties": False,
        },
        "handler": "create_document",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    # ── Lot 2A (T9) — FILES: the upload ticket (plan D4) ────────────────
    # begin_upload's `upload_url` is the ONE capability URL a tool output
    # carries (the documented exception to « no signed URL in output »):
    # its persist/rehydrate hooks (mcp/handlers.py) keep it out of
    # mcp_idempotency, and a replay re-opens a session for the SAME ticket.
    "begin_upload": {
        "title": "Ouvrir un téléversement",
        "description": (
            (
            "ÉCRITURE — étape 1 d'envoi d'un fichier externe : ticket "
            "WRITE-ONLY valide 1 h. `purpose=\"document\"` (classe au dossier, "
            "catégorie PRÉSUMÉE), `gabarit` (`create` ou `replace`, exige "
            "`dossier_id` source pour contrôler les résidus sauf "
            "`aucun_dossier_source: true`), `piece_justificative` (reçu "
            "d'écriture `tx_id`, max 10 Mo ; `replaces_receipt_md5` pour "
            "remplacer en gardant l'ancien ; copie auto au dossier si "
            "dépense liée). Déclarer `size_bytes` et `md5_base64` exacts, "
            "faire le PUT depuis le sandbox vers `upload_url` (ne JAMAIS "
            "montrer cette URL ; requiert accès `storage.googleapis.com`), "
            "puis appeler `finalize_upload`."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "purpose": {
                    "type": "string", "enum": _UPLOAD_PURPOSES,
                    "description": "Destination du fichier.",
                },
                "filename": {
                    "type": "string", "minLength": 1,
                    "maxLength": UPLOAD_FILENAME_MAX_CHARS,
                    "description": (
                        "Nom complet avec extension (sans barre oblique ; `.docx` pour gabarit ; PDF/image/courriel pour pièce justificative)."
                    ),
                },
                "size_bytes": {
                    "type": "integer", "minimum": 1,
                    "maximum": UPLOAD_DOCUMENT_MAX_BYTES,
                    "description": (
                        "Taille EXACTE en octets (max 10 Mo pour gabarit ou pièce justificative)."
                    ),
                },
                "md5_base64": {
                    "type": "string", "minLength": 24, "maxLength": 24,
                    "description": (
                        "Base64 du condensat MD5 brut sur 16 octets (`base64.b64encode(hashlib.md5(data).digest())`), jamais en hex."
                    ),
                },
                "dossier_id": _id(
                    "document : REQUIS. gabarit : dossier source pour contrôle des résidus (ou aucun_dossier_source)."
                ),
                "folder_id": _id(
                    "document : répertoire du dossier (omis/\"\" = racine)."
                ),
                "category": {
                    "type": "string", "enum": _DOCUMENT_CATEGORY_CHOICES,
                    "description": (
                        "document : PRÉSUMÉE. gabarit create : procédure, correspondance ou autre (défaut)."
                    ),
                },
                "display_name": {
                    "type": "string", "minLength": 1,
                    "maxLength": DOCUMENT_NAME_MAX_CHARS,
                    "description": (
                        "document : nom affiché (défaut : nom du fichier)."
                    ),
                },
                "document_date": _date(
                    "document : date PROPRE, AAAA-MM-JJ."
                ),
                "tags": {
                    "type": "array",
                    "maxItems": DOCUMENT_TAGS_MAX,
                    "items": {
                        "type": "string", "minLength": 1,
                        "maxLength": DOCUMENT_TAG_MAX_CHARS,
                        "description": "Une étiquette sans virgule.",
                    },
                    "description": "document : étiquettes sans doublon.",
                },
                "template_mode": {
                    "type": "string", "enum": _TEMPLATE_MODES,
                    "description": (
                        "gabarit : REQUIS — `create` (nouveau) ou `replace` (nouvelle version)."
                    ),
                },
                "name": {
                    "type": "string", "minLength": 1,
                    "maxLength": TEMPLATE_NAME_MAX_CHARS,
                    "description": (
                        "gabarit create : REQUIS — nom affiché sur tout document généré (contrôlé comme le fichier)."
                    ),
                },
                "description": {
                    "type": "string",
                    "maxLength": TEMPLATE_DESCRIPTION_MAX_CHARS,
                    "description": "gabarit create : description d'usage.",
                },
                "kind": {
                    "type": "string", "enum": _TEMPLATE_KINDS,
                    "description": (
                        "gabarit create : défaut « gabarit ». Sorte spéciale créée inactive ; « modele » jamais rempli."
                    ),
                },
                "template_id": _id(
                    "gabarit replace : REQUIS — modèle cible (list_templates)."
                ),
                "expected_version": {
                    "type": "integer", "minimum": 1,
                    "description": (
                        "gabarit replace : REQUIS — version lue."
                    ),
                },
                "accept_residual": {
                    "type": "array",
                    "maxItems": UPLOAD_ACCEPT_MAX_ITEMS,
                    "items": {
                        "type": "string", "minLength": 1,
                        "maxLength": UPLOAD_ACCEPT_MAX_CHARS,
                        "description": (
                            "Un identifiant exact tel que nommé dans le refus."
                        ),
                    },
                    "description": (
                        "gabarit : résidus que l'AVOCAT accepte de conserver."
                    ),
                },
                "scrub_properties": {
                    "type": "boolean",
                    "description": (
                        "gabarit : efface d'abord titre, sujet, auteur, dernier éditeur et description (défaut false)."
                    ),
                },
                "aucun_dossier_source": {
                    "type": "boolean",
                    "description": (
                        "gabarit sans dossier_id : true déclare hors dossier (aucun contrôle ; demander si doute)."
                    ),
                },
                "tx_id": _id(
                    "piece_justificative : écriture admin cible (`id` de get_admin_ledger)."
                ),
                "replaces_receipt_md5": {
                    "type": "string", "minLength": 24, "maxLength": 24,
                    "description": (
                        "piece_justificative : `md5_base64` du reçu actuel pour le REMPLACER (l'ancien reste gardé)."
                    ),
                },
                **_write_protocol_props(),
            },
            "required": ["purpose", "filename", "size_bytes", "md5_base64"],
            "additionalProperties": False,
        },
        "handler": "begin_upload",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "finalize_upload": {
        "title": "Verser un fichier téléversé",
        "annotations": {
            # A ticket already filed answers its stored result again and
            # writes nothing — even without an idempotency_key.
            "idempotentHint": True,
        },
        "description": (
            (
            "ÉCRITURE — étape 2 : classe les octets PUT sous `ticket_id` si "
            "taille et MD5 correspondent exactement à `begin_upload` (sinon "
            "refusés et rejetés). `document` → document ; `gabarit` → "
            "contrôle résidus puis gabarit/version ; `piece_justificative` → "
            "joint si reçu identique (`stale_etag` sinon) + copie au dossier "
            "si dépense liée. Si rien encore reçu : refusé mais ticket reste "
            "ouvert. Ticket déjà classé = rejeu ; expiré/refusé = "
            "recommencer avec NOUVELLE `idempotency_key`."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": _id("`ticket_id` retourné par begin_upload."),
                **_write_protocol_props(),
            },
            "required": ["ticket_id"],
            "additionalProperties": False,
        },
        "handler": "finalize_upload",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "Everything is bound at begin_upload — a replacement's "
            "expected_version included, re-checked in the template's own "
            "transaction, and a receipt's replaces_receipt_md5 (the entry's "
            "receipt as read), re-checked in the attach's — and a ticket is "
            "claimed transactionally: one finalizer at a time, with no etag "
            "to pass."
        ),
    },
    # ── Lot 2B — TEMPLATIZE a document already in a dossier (READ) ──────
    # Interpretation B (plan D5): its literals become {{…}} fields. This
    # read counts; create_template's `substitutions` writes.
    "preview_templatize": {
        "title": "Aperçu d'une transformation en gabarit",
        "description": (
            (
            (
            (
            (
            "LECTURE — exécuter AVANT `create_template` avec "
            "`substitutions`. Pour un `.docx` stocké (`document_id`), compte "
            "où chaque remplacement littéral → `{{champ}}` s'applique "
            "(corps, en-têtes, pieds de page) vs intact, classe les champs "
            "(`auto`, `manual`, `passthrough`) et liste les identifiants "
            "résiduels du dossier source. Sensible à la casse (prévoir une "
            "règle dédiée pour chaque variante en MAJUSCULES). Itérer "
            "jusqu'à `ready_to_create`, puis passer `document_id` en "
            "`source_document_id` et chaque `substituted` en "
            "`expected_occurrences` à `create_template`. Ne modifie ni ne "
            "lit le texte."
        )
        )
        )
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": _id(
                    "`.docx` stocké (UUIDv4, list_documents) ; son dossier source est contrôlé."
                ),
                "substitutions": _substitutions_prop(expected_required=False),
            },
            "required": ["document_id", "substitutions"],
            "additionalProperties": False,
        },
        "handler": "preview_templatize",
    },
    # ── Lot 2A (T10) — TEMPLATES from a document already in a dossier ───
    # Interpretation A (plan D5): the stored .docx is registered UNCHANGED
    # (bar the opt-in core-properties scrub) — or, given `substitutions`
    # (lot 2B, interpretation B), as a TEMPLATIZED copy.
    "create_template": {
        "title": "Enregistrer un document comme gabarit",
        "description": (
            (
            "ÉCRITURE — enregistre un `.docx` DÉJÀ au dossier (max 10 Mo) "
            "comme NOUVEAU modèle du cabinet. Sans `substitutions` : octets "
            "intacts. Avec : copie gabaritisée tout-ou-rien selon les "
            "comptes de `preview_templatize` (`scrub_properties` forcé). "
            "Refusé si le fichier ou `name` porte encore des identifiants du "
            "dossier source (passer `accept_residual` SEULEMENT sur mot de "
            "l'avocat). Sortes spéciales créées inactives. Document source "
            "intact ; fichier externe = `begin_upload` (`document`) d'abord."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_document_id": _id(
                    "`.docx` stocké (list_documents) passé à preview_templatize ; son dossier est contrôlé."
                ),
                "name": {
                    "type": "string", "minLength": 1,
                    "maxLength": TEMPLATE_NAME_MAX_CHARS,
                    "description": (
                        "Nom en français repris sur chaque document généré (contrôlé ; jamais le nom d'un client)."
                    ),
                },
                "description": {
                    "type": "string",
                    "maxLength": TEMPLATE_DESCRIPTION_MAX_CHARS,
                    "description": "Description d'usage en français.",
                },
                "category": {
                    "type": "string", "enum": _TEMPLATE_CATEGORIES,
                    "description": "Catégorie propre du gabarit.",
                },
                "kind": {
                    "type": "string", "enum": _TEMPLATE_KINDS,
                    "description": (
                        "Défaut « gabarit ». Sorte spéciale créée inactive ; « modele » jamais rempli."
                    ),
                },
                "accept_residual": {
                    "type": "array",
                    "maxItems": UPLOAD_ACCEPT_MAX_ITEMS,
                    "items": {
                        "type": "string", "minLength": 1,
                        "maxLength": UPLOAD_ACCEPT_MAX_CHARS,
                        "description": (
                            "Un identifiant exact tel que nommé dans le refus."
                        ),
                    },
                    "description": (
                        "Résidus que l'AVOCAT accepte de garder dans le gabarit."
                    ),
                },
                "scrub_properties": {
                    "type": "boolean",
                    "description": (
                        "Efface les propriétés du fichier (forcé true avec `substitutions`)."
                    ),
                },
                "substitutions": _substitutions_prop(expected_required=True),
                **_write_protocol_props(),
            },
            "required": ["source_document_id", "name", "category"],
            "additionalProperties": False,
        },
        "handler": "create_template",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "update_template": {
        "title": "Corriger un gabarit",
        "annotations": {
            # Values already stored — or a file identical to the version in
            # force — write nothing: a repeat is a no-op.
            "idempotentHint": True,
        },
        "description": (
            (
            (
            (
            (
            (
            "ÉCRITURE — corrige un modèle en un de deux appels. MÉTADONNÉES "
            "(`name`, `description`, `category`, `kind` — omis = intact ; "
            "`kind` d'un spécial actif immuable ; nouveau `name` vérifié "
            "contre dossiers sources). OU NOUVEAU FICHIER "
            "(`source_document_id` + `expected_version` de `list_templates`) "
            ": installé en NOUVELLE version (l'actuelle reste restaurable ; "
            "résidus refusés sauf `accept_residual` sur mot de l'avocat). Un "
            "nouveau fichier sur le modèle actif s'imprime aussitôt sur tout "
            "nouveau document ; ne change jamais quel modèle est actif."
        )
        )
        )
        )
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "template_id": _id(
                    "UUIDv4 du modèle (list_templates)."
                ),
                "name": {
                    "type": "string", "minLength": 1,
                    "maxLength": TEMPLATE_NAME_MAX_CHARS,
                    "description": (
                        "Métadonnées : nouveau nom en français (jamais un nom de client ; contrôlé contre dossiers sources)."
                    ),
                },
                "description": {
                    "type": "string",
                    "maxLength": TEMPLATE_DESCRIPTION_MAX_CHARS,
                    "description": "Métadonnées : description d'usage ; \"\" vide.",
                },
                "category": {
                    "type": "string", "enum": _TEMPLATE_CATEGORIES,
                    "description": "Métadonnées : catégorie propre du modèle.",
                },
                "kind": {
                    "type": "string", "enum": _TEMPLATE_KINDS,
                    "description": (
                        "Métadonnées : sorte spéciale jamais activée ici ; « modele » jamais rempli."
                    ),
                },
                "source_document_id": _id(
                    "Fichier : `.docx` stocké (list_documents) devenant la nouvelle version."
                ),
                "expected_version": {
                    "type": "integer", "minimum": 1,
                    "description": (
                        "Fichier : REQUIS — version lue."
                    ),
                },
                "accept_residual": {
                    "type": "array",
                    "maxItems": UPLOAD_ACCEPT_MAX_ITEMS,
                    "items": {
                        "type": "string", "minLength": 1,
                        "maxLength": UPLOAD_ACCEPT_MAX_CHARS,
                        "description": (
                            "Un identifiant exact nommé au refus."
                        ),
                    },
                    "description": (
                        "Résidus que l'AVOCAT accepte de conserver."
                    ),
                },
                "scrub_properties": {
                    "type": "boolean",
                    "description": (
                        "Fichier : efface les propriétés du document d'abord."
                    ),
                },
                **_expected_etag_prop(_TEMPLATE_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["template_id"],
            "additionalProperties": False,
        },
        "handler": "update_template",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _TEMPLATE_ETAG_READERS,
    },
    # ── Lot 5b — ACCOUNTING ────────────────────────────────────────────
    # The six tools of plan decision D1, every one through
    # services/comptabilite — the door the two web registers use. Since
    # 2026-10-05 the READ is an ordinary read (athena:read, like the trust
    # reads) and the five writes ordinary writes (athena:write): the lawyer
    # removed their separate scope and switch. The writes demand an
    # idempotency_key (a register entry is never deleted: a duplicate is for
    # ever).
    "get_admin_ledger": {
        "title": "Registre d'administration",
        "description": (
            "COMPTABILITÉ — LECTURE. Grand livre d'administration : comptes "
            "et cartes (soldes, plancher de conciliation complétée, état — "
            "jamais transit/numéro de compte) et écritures de la fenêtre "
            "(défaut 90 j Montréal, plus récentes d'abord, bornées à `limit` "
            "avec `truncated`). Chaque ligne porte `etag`, `locked` et "
            "`receipt` (`get_admin_receipt`). Solde cumulatif si un seul "
            "compte sans filtre. Revenus = `encaissement_facture` et "
            "`recette_autre` ; dépenses = `dépense` moins "
            "`remboursement_depense` (seuls avec net/TPS/TVQ significatifs). "
            "Hors résultats ni total CTI/RTI : `prélèvement`, "
            "`apport`, `virement_interne_sortant`, "
            "`virement_interne_entrant`, `avance_honoraires`, "
            "`remise_tps_tvq`, `remboursement_tps_tvq` (solde TPS/TVQ), "
            "`paiement_carte`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": _id(
                    "Un compte d'administration (`id` dans `accounts`). Omettre pour tous."
                ),
                "date_from": _date(
                    "Premier jour, AAAA-MM-JJ (défaut : 90 j avant date_to)."
                ),
                "date_to": _date(
                    "Dernier jour, AAAA-MM-JJ (défaut : aujourd'hui Montréal)."
                ),
                "kind": {"type": "string", "enum": _ADMIN_KINDS,
                         "description": "Seulement les écritures de cette nature."},
                "status": {"type": "string", "enum": _REGISTER_STATUSES,
                           "description": "Seulement les écritures à ce statut."},
                "category": {"type": "string", "enum": _ADMIN_CATEGORIES,
                             "description": (
                                 "Seulement les dépenses/remboursements de CETTE catégorie (« Honoraires professionnels » regroupe notaire, expert, avocat, huissier, sténographe et honoraires_professionnels « Autres honoraires professionnels » — lire sans filtre).")},
                "limit": _limit(25),
            },
            "additionalProperties": False,
        },
        "handler": "get_admin_ledger",
    },
    # 2026-11-10 (the lawyer's request): an entry's pièce justificative,
    # CONSULTED — its text, or the photo itself (tools.IMAGE_RESULT_TOOLS).
    "get_admin_receipt": {
        "title": "Pièce justificative d'une écriture",
        "description": (
            (
            "COMPTABILITÉ — LECTURE. Pièce justificative de `tx_id` "
            "(`get_admin_ledger`) : métadonnées, `document_id` au dossier et "
            "CONTENU (PDF : couche texte paginée sans OCR ; `.eml`/`.msg` : "
            "en-têtes, corps, texte PDF joints ; JPG/PNG : image réduite "
            "`content_kind=\"image\"` — jamais décrite si non vue). TIFF, "
            "image trop grande ou illisible → `content_kind=\"none\"` avec "
            "raison française. Jamais de lien ni chemin ; jamais dans "
            "`web_search`. Joindre/remplacer via `begin_upload` "
            "(`piece_justificative`)."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tx_id": _id(
                    "Écriture admin (`id` de get_admin_ledger)."
                ),
                "page_range": {
                    "type": "string", "maxLength": 12,
                    "description": (
                        "Reçu PDF seulement : \"4\" dès page 4, \"2-6\" plage inclusive (défaut : depuis page 1)."
                    ),
                },
            },
            "required": ["tx_id"],
            "additionalProperties": False,
        },
        "handler": "get_admin_receipt",
    },
    # 2026-11-10: the statements of the « Finances » page, for a series of
    # periods computed from ONE read — the lawyer's comparatives.
    "get_financial_statements": {
        "title": "États financiers",
        "description": (
            (
            (
            (
            "LECTURE — états financiers du cabinet (« États financiers » et "
            "PDF) jusqu'à 24 périodes en une lecture cohérente (`AAAA-MM`, "
            "`AAAA-TQ`, `AAAA`, dès `2025-01-01`). Chaque section a "
            "`columns`, `rows` à `key` stable et "
            "`values_cents`/`values_display`. Revenus = factures "
            "`envoyée`/`en_retard`/`payée` HT ; une radiation ne réduit rien "
            "au revenu : nette de taxes, elle passe en charges directes (« "
            "Créances irrécouvrables », `creances_irrecouvrables`) + "
            "`tps_creances_irrecouvrables`/`tvq_creances_irrecouvrables` et "
            "`redressements`. Fidéicommis et en-cours = hors totaux. "
            "Toujours rapporter `unavailable`, `points` et "
            "`points_incomplete`, et présenter « selon l'état actuel des "
            "inscriptions » (non audités)."
        )
        )
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "periods": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": STATEMENT_PERIODS_MAX,
                    "items": {"type": "string", "maxLength": 7},
                    "description": (
                        "Périodes dans l'ordre d'affichage (`AAAA-MM`, `AAAA-TQ` T1-T4 ou `AAAA`), sans doublon."
                    ),
                },
                "sections": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "enum": _STATEMENT_SECTIONS},
                    "description": (
                        "Sections voulues seulement (défaut : toutes) sans doublon ; en-tête, `kpis` et `points` toujours inclus."
                    ),
                },
            },
            "required": ["periods"],
            "additionalProperties": False,
        },
        "handler": "get_financial_statements",
    },
    "get_courrier_connexion": {
        "title": "État de la connexion Courrier",
        "description": (
            "Vérifie la configuration et l'accès Microsoft Graph du module "
            "Courrier : compte cible, portées accordées sur le jeton, validité "
            "et expiration (heure de Montréal), absence de Mail.Send, accès "
            "effectif à la boîte de réception et au dossier racine OneDrive, "
            "état de l'interrupteur COURRIER_ACTIF et région d'exécution."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": "get_courrier_connexion",
    },
    "list_courriels": {
        "title": "Inventaire de la boîte de réception Courrier",
        "description": (
            "Inventorie la boîte de réception Outlook du plus ancien au plus "
            "récent (`premier`, `nombre` <= 25) sans rien écrire ni modifier : "
            "clé 16 hex (`cle`), identifiant immuable (`id`), horodatage "
            "Montréal (`recu`), expéditeur, destinataires, objet, corps "
            "texte (<= 1 500 car.), pièces jointes avec aperçu page 1 "
            "(<= 400 car. pour PDF, DOCX, TXT, CSV <= 10 Mo ; pièces "
            "incorporées, liens infonuagiques, .ics/.vcf et images de "
            "signature ignorés sans téléchargement), fil de conversation "
            "(`fil`) et état au registre (`etat`, `legacy_ref`). Au premier "
            "appel (`premier` à 0), retourne aussi `outlook` et `onedrive`. "
            "S'arrête au budget (~25 s) avec `suite` à true ; utiliser "
            "`list_dossiers` / `get_dossier` pour rattacher un courriel."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "premier": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 10000,
                    "description": "Décalage de pagination (0 par défaut).",
                },
                "nombre": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 25,
                    "description": (
                        "Nombre de courriels à inventorier (5 par défaut, max 25)."
                    ),
                },
            },
            "additionalProperties": False,
        },
        "handler": "list_courriels",
    },
    "record_trust_entry": {
        "title": "Inscrire une écriture au fidéicommis",
        "description": (
            (
            "COMPTABILITÉ — ÉCRITURE au registre en fidéicommis (RLRQ c. "
            "B-1, r. 5) pour un mouvement bancaire RÉEL à sa date (jamais "
            "futur, jamais <= conciliation complétée ni avant dernière "
            "écriture du compte). Append-only (correction : "
            "`reverse_register_entry`). Déboursé exige fonds COMPENSÉS "
            "suffisants du client ; aucun retrait comptant (art. 57). "
            "`virement_honoraires` fait en UNE transaction : retrait "
            "chèque/virement (art. 58), recette compte admin "
            "`admin_account_id` à `admin_date` et paiement sur facture "
            "`invoice_id` (envoyée, même dossier et client ; INSCRIT UN "
            "PAIEMENT). Tout refus n'écrit rien. `idempotency_key` REQUISE."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": _id("Compte en fidéicommis (get_trust_snapshot)."),
                "direction": {
                    "type": "string", "enum": _TRUST_DIRECTIONS,
                    "description": "recette (entrée) ou déboursé (sortie).",
                },
                "purpose": {
                    "type": "string", "enum": _TRUST_ENTRY_PURPOSES,
                    "description": (
                        "Objet compatible avec `direction` : recettes (`avance_honoraires`, `dépôt_client`) ; déboursés (`remise_client`, `déboursé_tiers`, `virement_honoraires`). `intérêts`/`frais_bancaires` sans dossier en recette seulement."
                    ),
                },
                "amount_cents": _money_arg("Cents entiers, > 0."),
                "date": _date("Jour du mouvement bancaire, AAAA-MM-JJ."),
                "method": {
                    "type": "string", "enum": _TRUST_METHODS,
                    "description": "Mode de paiement.",
                },
                "dossier_id": _id(
                    "Dossier du client (omis avec client_id seulement pour intérêts/frais_bancaires)."
                ),
                "client_id": _id(
                    "Client concerné — client de `dossier_id` (get_dossier)."
                ),
                **_register_text_props(
                    "Requis sauf `virement_honoraires` où seule l'avocat ou son cabinet est admis (art. 58 ; défaut : avocat par chèque, cabinet par virement)."
                ),
                "invoice_id": _id(
                    "virement_honoraires seulement, requis : facture réglée (list_invoices)."
                ),
                "admin_account_id": _id(
                    "virement_honoraires seulement, requis : compte admin destinataire (get_admin_ledger)."
                ),
                "admin_date": _date(
                    "virement_honoraires seulement : date d'arrivée au compte admin, AAAA-MM-JJ (défaut date fidéicommis ; ni antérieure ni future)."
                ),
                **_write_protocol_props(),
            },
            "required": ["account_id", "direction", "purpose", "amount_cents",
                         "date", "method", "idempotency_key"],
            "additionalProperties": False,
        },
        "handler": "record_trust_entry",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_REQUIRED,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "a creation: no stored entry to present a version of — the "
            "model re-reads the account, the dossier and the invoice inside "
            "its own transaction, and a concurrent write re-runs it"
        ),
    },
    "record_admin_entry": {
        "title": "Inscrire une écriture d'administration",
        "description": (
            (
            (
            (
            (
            (
            "COMPTABILITÉ — ÉCRITURE au grand livre admin (mouvement RÉEL à "
            "sa date, jamais future ni verrouillée). `kind` fixe le sens : "
            "`dépense`/`remboursement_depense` (`category`+`ventilation`), "
            "`recette_autre`, `encaissement_facture` (paie `invoice_id` — "
            "INSCRIT UN PAIEMENT), `paiement_carte` (`account_id` → "
            "`card_account_id`). Hors "
            "résultats : `prélèvement` (retrait avocat), `apport`, "
            "`virement_interne_sortant`/`virement_interne_entrant` (compte "
            "externe en `counterparty`), `avance_honoraires` "
            "(`transfer_purpose`), `remise_tps_tvq`/`remboursement_tps_tvq` "
            "(sans dossier ; intérêts/pénalités à part). Honoraires "
            "fidéicommis = `record_trust_entry`. `already_cleared_date` : "
            "née compensée. `idempotency_key` REQUISE."
        )
        )
        )
        )
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": _id(
                    "Compte admin (get_admin_ledger) — pour `paiement_carte`, le compte d'opérations payeur."
                ),
                "kind": {
                    "type": "string", "enum": _ADMIN_ENTRY_KINDS,
                    "description": (
                        "Nature déterminant le sens (`prélèvement` = retrait de l'avocat, jamais le mode préautorisé)."
                    ),
                },
                "amount_cents": _money_arg(
                    "Cents entiers > 0 (taxes comprises pour une dépense)."
                ),
                "date": _date("Jour du mouvement, AAAA-MM-JJ."),
                "method": {
                    "type": "string", "enum": _ADMIN_METHODS,
                    "description": (
                        "Mode de paiement (« prélèvement » ici = débit préautorisé)."
                    ),
                },
                "category": {
                    "type": "string", "enum": _ADMIN_CATEGORIES,
                    "description": (
                        "Requis pour `dépense` / `remboursement_depense`. IA/cloud à l'usage (Anthropic, API, hébergement) : `infonuagique_ia` (forfait mensuel : `abonnements`) ; recueils/livres : `documentation` ; recherche à l'acte (SOQUIJ, registres) : `recherche` ; timbres/greffe : `timbres_judiciaires` (`taxes_permis` : permis/droits) ; notaire/expert/avocat : clé dédiée (sinon `honoraires_professionnels`) ; salle ponctuelle : `location_salle` (bureau : `loyer`) ; cours : `formation`."
                    ),
                },
                **_ventilation_props(),
                "invoice_id": _id(
                    "encaissement_facture seulement, requis : facture payée (list_invoices)."
                ),
                "card_account_id": _id(
                    "paiement_carte seulement, requis : carte payée (get_admin_ledger)."
                ),
                "dossier_id": _id(
                    "Lien optionnel pour dépense, remboursement_depense, recette_autre, avance_honoraires ou virement interne (jamais prélèvement, apport, remise_tps_tvq, remboursement_tps_tvq)."
                ),
                "supplier_invoice_ref": {
                    "type": "string", "maxLength": REGISTER_REFERENCE_MAX_CHARS,
                    "description": (
                        "dépense / remboursement_depense / recette_autre seulement : n° de facture du fournisseur."
                    ),
                },
                "transfer_purpose": {
                    "type": "string", "enum": _ADMIN_TRANSFER_PURPOSES,
                    "description": (
                        "virement_interne_sortant seulement : « avances » si transfert d'avances client vers fidéicommis (\"\" sinon)."
                    ),
                },
                **_register_text_props(
                    "Requis sauf `paiement_carte` (aucun). Sur virement interne : autre compte ; sur `remise_tps_tvq` ou `remboursement_tps_tvq` : autorité fiscale ; sur `avance_honoraires` : le client."
                ),
                "already_cleared_date": _date(
                    "Date du relevé bancaire (AAAA-MM-JJ >= `date`) si déjà compensé (pas pour `paiement_carte`)."
                ),
                **_write_protocol_props(),
            },
            "required": ["account_id", "kind", "amount_cents", "date",
                         "method", "idempotency_key"],
            "additionalProperties": False,
        },
        "handler": "record_admin_entry",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_REQUIRED,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "a creation: no stored entry to present a version of — the "
            "model re-reads the account and the invoice inside its own "
            "transaction, and a concurrent payment re-runs it"
        ),
    },
    "update_admin_entry": {
        "title": "Corriger une écriture d'administration",
        # idempotentHint (the update_note precedent): values already stored
        # write nothing, and every change demands the CURRENT etag — an
        # identical second call is refused, never applied twice.
        "annotations": {"idempotentHint": True},
        "description": (
            "COMPTABILITÉ — REMPLACE les champs nommés d'UNE écriture admin "
            "contre `expected_etag` (`get_admin_ledger`) ; omis = intact ; "
            "garde 25 révisions. Seulement si ÉDITABLE (`en_circulation`, "
            "après conciliation, hors facture/virement honoraires/carte/"
            "contrepassation — sinon `reverse_register_entry`). `kind` "
            "conserve le sens bancaire : `dépense`, `prélèvement`, "
            "`virement_interne_sortant` et `remise_tps_tvq` (déboursés) ; "
            "`recette_autre`, `apport`, `virement_interne_entrant`, "
            "`remboursement_tps_tvq`, `remboursement_depense` et "
            "`avance_honoraires` (recettes) — entre sens, seul "
            "`dépense` <-> `recette_autre` (sinon contrepasser/réinscrire). "
            "`ventilation`/`category` requis si `dépense`/`remboursement_depense`. "
            "`prélèvement`, `apport`, `remise_tps_tvq` et "
            "`remboursement_tps_tvq` sans dossier (`dossier_id=\"\"`). "
            "`idempotency_key` REQUISE."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tx_id": _id("ID de l'écriture (get_admin_ledger)."),
                **_expected_etag_prop(_ADMIN_ETAG_READERS),
                "date": _date("Nouvelle date, AAAA-MM-JJ."),
                "kind": {"type": "string", "enum": _ADMIN_EDIT_KINDS,
                         "description": (
                             "Nouvelle nature de même sens (ou dépense <-> recette_autre)."
                         )},
                "amount_cents": _money_arg("Nouveau montant, cents entiers."),
                "method": {"type": "string", "enum": _ADMIN_METHODS,
                           "description": (
                               "Nouveau mode (« prélèvement » = débit préautorisé)."
                           )},
                "category": {"type": "string", "enum": _ADMIN_CATEGORIES,
                             "description": ("Nouvelle catégorie d'une `dépense` ou `remboursement_depense`.")},
                **_ventilation_props(),
                "dossier_id": {
                    "type": "string", "maxLength": 64,
                    "description": (
                        "Nouveau dossier (\"\" retire ; jamais pour prélèvement, apport, remise_tps_tvq, remboursement_tps_tvq)."
                    ),
                },
                "transfer_purpose": {
                    "type": "string", "enum": _ADMIN_TRANSFER_PURPOSES,
                    "description": (
                        "virement_interne_sortant seulement : « avances » marque le transfert d'avances (\"\" retire)."
                    ),
                },
                "supplier_invoice_ref": {
                    "type": "string", "maxLength": REGISTER_REFERENCE_MAX_CHARS,
                    "description": "N° de facture du fournisseur.",
                },
                **_register_text_props(),
                **_write_protocol_props(),
            },
            "required": ["tx_id", "expected_etag", "idempotency_key"],
            "additionalProperties": False,
        },
        "handler": "update_admin_entry",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_REQUIRED,
        "concurrency": CONCURRENCY_REQUIRED,
        "etag_readers": _ADMIN_ETAG_READERS,
    },
    "clear_register_entries": {
        "title": "Compenser des écritures",
        "description": (
            (
            "COMPTABILITÉ — ÉCRITURE. Marque de 1 à 50 écritures d'UN compte "
            "« compensée » à `cleared_date` (date du RELEVÉ BANCAIRE : >= "
            "date de l'écriture, jamais future, après dernière "
            "conciliation). Tout ou rien (aucun « décompenser »). En "
            "fidéicommis, compenser un dépôt rend ses fonds disponibles pour "
            "déboursé (`released_funds`). En admin (éditable jusqu'à "
            "compensation), `expected_etags` est REQUIS dans l'ordre de "
            "`tx_ids`. `idempotency_key` REQUISE."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "register": {
                    "type": "string", "enum": _REGISTERS,
                    "description": (
                        "trust (list_trust_transactions) ou admin (get_admin_ledger)."
                    ),
                },
                "tx_ids": {
                    "type": "array", "minItems": 1,
                    "maxItems": REGISTER_CLEAR_MAX,
                    "items": {"type": "string", "maxLength": 64,
                              "description": "ID d'une écriture."},
                    "description": (
                        f"1 à {REGISTER_CLEAR_MAX} écritures distinctes d'un compte, en circulation."
                    ),
                },
                "cleared_date": _date(
                    "Date du relevé bancaire, AAAA-MM-JJ. Requis."
                ),
                "expected_etags": {
                    "type": "array", "minItems": 1,
                    "maxItems": REGISTER_CLEAR_MAX,
                    "items": {"type": "string", "maxLength": 64,
                              "description": "etag d'une écriture."},
                    "description": (
                        "admin seulement et REQUIS : `etag` lu sur get_admin_ledger pour chaque écriture dans l'ORDRE de `tx_ids` (refusé sur trust)."
                    ),
                },
                **_write_protocol_props(),
            },
            "required": ["register", "tx_ids", "cleared_date",
                         "idempotency_key"],
            "additionalProperties": False,
        },
        "handler": "clear_register_entries",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_REQUIRED,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "a BATCH of status targets, with no single `expected_etag` to "
            "give: at admin, where an entry stays editable until cleared, "
            "`expected_etags` (required there) carries the version of each "
            "entry the caller read, checked by the handler and again in the "
            "model's transaction; a trust entry never changes, and one no "
            "longer en circulation is refused in that transaction, so no "
            "stale view can clear it twice"
        ),
    },
    "reverse_register_entry": {
        "title": "Contre-passer une écriture",
        "description": (
            (
            "COMPTABILITÉ — ÉCRITURE. Seule correction d'une écriture en "
            "fidéicommis ou d'une écriture admin verrouillée : crée une « "
            "Correction » opposée (les deux restent). `en_circulation` → les "
            "deux passent `annulée` ; `compensée` → originale reste "
            "compensée et contrepassation passe `en_circulation`. "
            "Fidéicommis daté aujourd'hui ; admin accepte `reversal_date`. "
            "Contrepasse en UNE transaction : paiement honoraires + recette "
            "admin + paiement facture, paire `paiement_carte`, ou "
            "`encaissement_facture`. Refuse correction, déjà annulée, "
            "recette admin d'honoraires ou transfert inter-dossiers. "
            "`idempotency_key` REQUISE."
        )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "register": {
                    "type": "string", "enum": _REGISTERS,
                    "description": (
                        "trust (list_trust_transactions) ou admin (get_admin_ledger)."
                    ),
                },
                "tx_id": _id("Écriture à contrepasser."),
                "reason": {
                    "type": "string", "minLength": 1,
                    "maxLength": REVERSAL_REASON_MAX_CHARS,
                    "description": "Motif en français — inscrit au registre.",
                },
                "reversal_date": _date(
                    "admin seulement, AAAA-MM-JJ : entre date d'origine et aujourd'hui, après dernière conciliation (défaut aujourd'hui)."
                ),
                **_write_protocol_props(),
            },
            "required": ["register", "tx_id", "reason", "idempotency_key"],
            "additionalProperties": False,
        },
        "handler": "reverse_register_entry",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_REQUIRED,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "a reversal decided inside the model's transaction on the stored "
            "entry: one already reversed is refused there, and the handler "
            "compare-and-sets against its OWN read — the status that decides "
            "the reversal's, and the one it reports"
        ),
    },
    "save_courriels": {
        "title": "Enregistrer des courriels dans OneDrive et Athéna",
        "description": (
            "ÉCRITURE — Enregistre des courriels Outlook et pièces jointes "
            "dans OneDrive (`4 - Dossiers`) et Athéna (`category_source=\"mcp\"`, "
            "catégorie présumée), sans jamais écraser ni déplacer dans Outlook. "
            "Vérifie le dossier Athéna (`AAAA-NNN`), la correspondance `cle` "
            "<-> `internetMessageId` et le dossier OneDrive "
            "(`creer_dossier_onedrive`). Le nom `.eml` (`AAAA-NNN - AAAA-MM-JJ "
            "HHhMM - {expéditeur}.eml`, <= 80 car.) est imposé côté serveur ; "
            "les pièces gardent leur extension d'origine (<= 100 car.). Conflits "
            "OneDrive : contenu identique -> `deja_present`, contenu différent "
            "-> ` (2)`. Types non pris en charge dans Athéna (ex. `.heic`) : "
            "sauvés dans OneDrive, `statut_athena=\"non_pris_en_charge\"`. "
            "Exige `idempotency_key` ; `essai=true` simule sans rien écrire."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "messages": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 25,
                    "description": "Liste des courriels à enregistrer (1 à 25).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "cle": {
                                "type": "string",
                                "minLength": 16,
                                "maxLength": 16,
                                "description": "Clé 16 hex (`list_courriels`).",
                            },
                            "id": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 512,
                                "description": "Identifiant immuable Graph.",
                            },
                            "dossier": {
                                "type": "string",
                                "minLength": 8,
                                "maxLength": 8,
                                "description": "Numéro Athéna (`AAAA-NNN`).",
                            },
                            "dossier_onedrive": {
                                "type": "string",
                                "minLength": 10,
                                "maxLength": 150,
                                "description": (
                                    "Nom du dossier OneDrive (`AAAA-NNN ...`)."
                                ),
                            },
                            "creer_dossier_onedrive": {
                                "type": "boolean",
                                "description": (
                                    "Créer le dossier OneDrive s'il est absent "
                                    "(défaut false)."
                                ),
                            },
                            "eml": {
                                "type": "object",
                                "properties": {
                                    "sous_dossier": {
                                        "type": "string",
                                        "enum": [
                                            "Correspondance\\Courriels",
                                            "Correspondance\\Emails",
                                        ],
                                    },
                                    "nom": {"type": "string", "maxLength": 200},
                                },
                                "additionalProperties": False,
                                "description": "Optionnel : cible du `.eml`.",
                            },
                            "pieces": {
                                "type": "array",
                                "maxItems": 50,
                                "description": "Pièces jointes à enregistrer.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "rang": {
                                            "type": "integer",
                                            "minimum": 1,
                                            "maximum": 100,
                                        },
                                        "id": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": 512,
                                        },
                                        "nom_origine": {
                                            "type": "string",
                                            "maxLength": 260,
                                        },
                                        "sous_dossier": {
                                            "type": "string",
                                            "enum": [
                                                "",
                                                "Correspondance",
                                                "Correspondance\\Courriels",
                                                "Correspondance\\Emails",
                                                "Procédures",
                                                "Jugements",
                                                "Pièces",
                                                "Documents du client",
                                            ],
                                        },
                                        "nom": {
                                            "type": "string",
                                            "maxLength": 200,
                                        },
                                    },
                                    "required": ["id", "sous_dossier"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["cle", "id", "dossier", "dossier_onedrive"],
                        "additionalProperties": False,
                    },
                },
                "essai": {
                    "type": "boolean",
                    "description": (
                        "Simulation complète sans écriture (défaut false)."
                    ),
                },
                **_write_protocol_props(),
            },
            "required": ["messages", "idempotency_key"],
            "additionalProperties": False,
        },
        "handler": "save_courriels",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_REQUIRED,
    },
    "move_courriels": {
        "title": "Classer ou étiqueter des courriels dans Outlook",
        "description": (
            "ÉCRITURE — Classe des courriels Outlook sous `Archive > Dossiers > "
            "{Actifs, Suspendu, Services-conseils}` (`action=\"classer\"`) ou "
            "pose l'étiquette « Réception — à classer » (`action=\"laisser\"`). "
            "`classer` exige d'abord l'état `enregistre` (ou `classe`) au "
            "registre Courrier, vérifie la correspondance `cle` <-> "
            "`internetMessageId`, crée le sous-dossier Outlook au besoin "
            "(`outlook_creer`), préserve l'état non-lu (`isRead`), contrôle "
            "l'identifiant immuable après déplacement et passe le registre à "
            "`classe`. Exige `idempotency_key` ; `essai=true` simule sans rien écrire."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "messages": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 25,
                    "description": "Liste des courriels à classer ou laisser (1 à 25).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "cle": {
                                "type": "string",
                                "minLength": 16,
                                "maxLength": 16,
                                "description": "Clé 16 hex (`list_courriels`).",
                            },
                            "id": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 512,
                                "description": "Identifiant immuable Graph.",
                            },
                            "action": {
                                "type": "string",
                                "enum": ["classer", "laisser"],
                                "description": (
                                    "`classer` sous Archive > Dossiers ou `laisser` en boîte."
                                ),
                            },
                            "dossier": {
                                "type": "string",
                                "minLength": 8,
                                "maxLength": 8,
                                "description": (
                                    "Numéro Athéna (`AAAA-NNN`), requis pour `classer`."
                                ),
                            },
                            "outlook_id": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 512,
                                "description": (
                                    "Identifiant du sous-dossier Outlook cible (`Archive > Dossiers`)."
                                ),
                            },
                            "outlook_creer": {
                                "type": "object",
                                "properties": {
                                    "statut": {
                                        "type": "string",
                                        "enum": [
                                            "Actifs",
                                            "Suspendu",
                                            "Services-conseils",
                                        ],
                                    },
                                    "nom": {
                                        "type": "string",
                                        "minLength": 10,
                                        "maxLength": 150,
                                    },
                                },
                                "required": ["statut", "nom"],
                                "additionalProperties": False,
                                "description": (
                                    "Sous-dossier Outlook à créer s'il est absent."
                                ),
                            },
                        },
                        "required": ["cle", "id", "action"],
                        "additionalProperties": False,
                    },
                },
                "essai": {
                    "type": "boolean",
                    "description": (
                        "Simulation complète sans écriture (défaut false)."
                    ),
                },
                **_write_protocol_props(),
            },
            "required": ["messages", "idempotency_key"],
            "additionalProperties": False,
        },
        "handler": "move_courriels",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_REQUIRED,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "Batch Outlook classification validates each message cle against Graph "
            "internetMessageId and enforces transactional state transitions in "
            "courrier_messages (enregistre -> classe, never overwriting or downgrading)."
        ),
    },
    "record_courriels_temps": {
        "title": "Inscrire les entrées de temps du courrier",
        "description": (
            "ÉCRITURE — TOUT OU RIEN : crée des entrées de temps non facturées "
            "et estampille atomiquement chaque courriel couvert dans le registre "
            "Courrier (`recep:{dossier}:{date}:{première_clé}`). Refuse tout "
            "courriel non enregistré, d'un autre dossier ou déjà compté. "
            "`hours` à au plus deux décimales (jamais arrondi), `description` "
            "imprimée verbatim sur la facture. Exige `idempotency_key` ; "
            "`essai=true` valide tout sans rien écrire."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entrees": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 25,
                    "description": "Entrées de temps Courrier à créer (1 à 25).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "legacy_ref": {
                                "type": "string",
                                "minLength": 27,
                                "maxLength": 64,
                                "description": (
                                    "Référence `recep:{dossier}:{date}:{première_clé_triée}`."
                                ),
                            },
                            "cles": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 25,
                                "description": (
                                    "Clés 16 hex des courriels couverts par cette entrée."
                                ),
                                "items": {
                                    "type": "string",
                                    "minLength": 16,
                                    "maxLength": 16,
                                },
                            },
                            "entree": {
                                "type": "object",
                                "properties": {
                                    k: v
                                    for k, v in _time_entry_fields().items()
                                    if k != "legacy_ref"
                                },
                                "required": _TIME_ENTRY_REQUIRED,
                                "additionalProperties": False,
                                "description": (
                                    "Paramètres de l'entrée de temps (comme "
                                    "`create_time_entries_bulk`)."
                                ),
                            },
                        },
                        "required": ["legacy_ref", "cles", "entree"],
                        "additionalProperties": False,
                    },
                },
                "essai": {
                    "type": "boolean",
                    "description": (
                        "Simulation complète sans écriture (défaut false)."
                    ),
                },
                **_write_protocol_props(),
            },
            "required": ["entrees", "idempotency_key"],
            "additionalProperties": False,
        },
        "handler": "record_courriels_temps",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_REQUIRED,
    },
}


# The key's description is DERIVED from the tool's declared policy (lot-5
# completeness review): _write_protocol_props() says « Pass one on every
# write » (the finitions aligned it with INSTRUCTIONS — contracts-7), the
# optional policy's truth — on
# the ``required`` tools it sat beside a ``required`` list naming the
# key and a description saying « idempotency_key REQUIRED », and a model
# reads a property description as a fact about the code. One text per
# policy, applied here so a ninth required tool is described by its
# declaration alone.
REQUIRED_KEY_DESCRIPTION = (
    "Clé choisie pour CETTE écriture — REQUISE ici (refusée sans clé ou si "
    "le journal de rejeu est illisible ; rejeu 24 h)."
)


def _describe_required_keys(registry: dict) -> None:
    for spec in registry.values():
        if spec.get("idempotency") == IDEMPOTENCY_REQUIRED:
            prop = spec["input_schema"]["properties"].get("idempotency_key")
            if prop is not None:
                prop["description"] = REQUIRED_KEY_DESCRIPTION


_describe_required_keys(TOOLS)


# The accounting WRITES (plan decision D1): trust and administration register
# entries — the ACCOUNTING family of mcp/disclosure, DERIVED from it, never
# listed by hand. Ordinary write tools since 2026-10-05 (scope athena:write,
# no switch of their own); the set survives because the write protocol hooks
# their replay (mcp/handlers registers their persistence hooks) and the reach
# test (tests/test_mcp_accounting) pins that no OTHER tool reaches a register
# writer.
ACCOUNTING_WRITE_TOOLS: frozenset[str] = frozenset(
    name for family in disclosure.FAMILIES if family.key == "accounting"
    for name in family.tools
)


def required_scope(name: str) -> str:
    """Scope a tool needs. Unlisted tools default to read — never to write."""
    return TOOLS[name].get("scope", SCOPE_READ)


def list_tool_descriptors(granted: Optional[frozenset[str]] = None) -> list[dict]:
    """Registry entries in MCP tools/list wire format, filtered by scope.

    A read-only connection must not see the write tools: advertising them
    would have the client model call one and take a 403 on every attempt,
    and ``_forbidden`` does not feed the failure brake — an unthrottled
    refusal loop. ``granted=None`` means "no scope filtering" (tests, docs).
    """
    scopes = granted if granted is not None else None
    out = []
    for name, spec in TOOLS.items():
        if scopes is not None and required_scope(name) not in scopes:
            continue
        annotations = dict(
            _WRITE_ANNOTATIONS if name in WRITE_TOOLS else _READ_ONLY_ANNOTATIONS
        )
        if name in EDIT_TOOLS:
            # DERIVED from EDIT_TOOLS, never restated per tool: an edit that
            # replaces a stored value IS a destructive update in the spec's
            # sense, and a future editor gets the honest hint by membership
            # alone rather than by someone remembering to add an override.
            annotations["destructiveHint"] = True
        # DERIVED from OUTBOUND_TOOLS: a tool whose effect reaches a third
        # party (the Outlook cancellation a refused Bookings request sends
        # the client) interacts with an « open world » in the spec's sense;
        # every other tool touches the practice's own records only.
        annotations["openWorldHint"] = name in OUTBOUND_TOOLS
        # A tool may correct a hint the family default gets wrong for it.
        # complete_task is genuinely idempotent — a second call with the
        # same status is a no-op that writes nothing — while every creator
        # would append again.
        annotations.update(spec.get("annotations") or {})
        # Verify the server-side output contract exists without emitting
        # outputSchema on the wire (MCP-Socle parity; conformance stays pinned
        # by tests/test_mcp_output_schemas.py against the real handlers).
        _ = OUTPUT_SCHEMAS[name]
        out.append(
            {
                "name": name,
                "title": spec["title"],
                "description": spec["description"],
                "inputSchema": spec["input_schema"],
                "annotations": annotations,
            }
        )
    return out


def get_handler(name: str) -> Callable[[dict], Any]:
    """Resolve a tool's handler function (lazy import breaks the cycle)."""
    from mcp import handlers

    return getattr(handlers, TOOLS[name]["handler"])
