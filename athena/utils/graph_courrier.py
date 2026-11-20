"""Microsoft Graph access for the Courrier module (Application mode).

Dedicated to ``get_courrier_connexion`` and the Courrier intake pipeline using
its own Entra ID app registration (client-credentials flow), separate from
``utils/graph.py`` (Bookings / outbound email). By design this client never
holds ``Mail.Send`` and exposes no email-sending or deletion operations.

Pure Graph transport + configuration reads: no Flask, no ``models`` import.
Error messages carry HTTP status codes or exception class names only — never
response bodies, URLs, UPNs, file paths, or tokens.
"""

from __future__ import annotations

import base64
import json
import math
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from typing import Any, Iterator, Optional
from urllib.parse import quote, urlsplit

import requests

from config import Config

_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_GRAPH_ORIGIN_PREFIX = "https://graph.microsoft.com/"
_METADATA_REGION_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/region"
)

_TOKEN_MARGIN_SECONDS = 300
_HTTP_TIMEOUT = 30
_MAX_RETRIES = 3
_MAX_RETRY_AFTER_SECONDS = 15.0
_RETRYABLE_STATUS_CODES = frozenset({429, 503, 504})
_DOWNLOAD_CHUNK_BYTES = 65536
ONEDRIVE_SIMPLE_MAX_BYTES = 4 * 1024 * 1024
ONEDRIVE_UPLOAD_CHUNK_BYTES = 10 * 320 * 1024  # 3.2 MiB (multiple of 320 KiB)
_UPLOAD_HOST_SUFFIXES = (".sharepoint.com", ".1drv.com", ".onedrive.com")

CATEGORIE_CLASSE = "Réception — classé"
CATEGORIE_A_CLASSER = "Réception — à classer"
COULEUR_CATEGORIE_CLASSE = "preset4"
COULEUR_CATEGORIE_A_CLASSER = "preset1"

_session = requests.Session()
_sleep = time.sleep


class CourrierGraphError(RuntimeError):
    """A Courrier Graph or token-endpoint call failed (status code only)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        reason: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


class CourrierNotConfigured(CourrierGraphError):
    """The COURRIER_* configuration is incomplete."""


@dataclass(frozen=True)
class TokenInfo:
    """Cached Entra application access token and parsed claims."""

    access_token: str
    expires_at_utc: datetime
    roles: tuple[str, ...]


_token_lock = threading.Lock()
_cached_info: Optional[TokenInfo] = None
_token_monotonic_deadline: float = 0.0


def _reset_token_cache_for_tests() -> None:
    """Clear the worker-local token cache (test isolation)."""
    global _cached_info, _token_monotonic_deadline
    with _token_lock:
        _cached_info = None
        _token_monotonic_deadline = 0.0


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    """Decode the JWT payload segment without signature verification.

    Used strictly to inspect granted application ``roles`` and ``exp`` on the
    token just issued by Entra's TLS token endpoint. Non-JWT opaque strings
    (e.g. unit test fixtures) return ``{}``.
    """
    parts = token.split(".")
    if len(parts) < 2 or not parts[1]:
        return {}
    segment = parts[1]
    padded = segment + ("=" * ((-len(segment)) % 4))
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        claims = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _parse_retry_after(header_val: Optional[str]) -> Optional[float]:
    """Parse HTTP Retry-After seconds or HTTP-date, clamped to [0, 15s]."""
    raw = (header_val or "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        seconds = None
    if seconds is not None:
        if not math.isfinite(seconds) or seconds < 0:
            return None
        return min(seconds, _MAX_RETRY_AFTER_SECONDS)
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (dt.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, min(delta, _MAX_RETRY_AFTER_SECONDS))


def _request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: Optional[dict[str, Any]] = None,
    data: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    raw_bytes: Optional[bytes] = None,
    timeout: int = _HTTP_TIMEOUT,
    stream: bool = False,
) -> requests.Response:
    """Execute an HTTP call with bounded retries on 429/503/504."""
    caller = getattr(_session, method.lower())
    kwargs: dict[str, Any] = {"headers": headers, "timeout": timeout}
    if params is not None:
        kwargs["params"] = params
    if json_body is not None:
        kwargs["json"] = json_body
    elif raw_bytes is not None:
        kwargs["data"] = raw_bytes
    elif data is not None:
        kwargs["data"] = data
    if stream:
        kwargs["stream"] = True

    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = caller(url, **kwargs)
        except requests.RequestException as exc:
            raise CourrierGraphError(
                f"Échec réseau Graph Courrier ({type(exc).__name__})."
            ) from exc

        if response.status_code not in _RETRYABLE_STATUS_CODES or attempt >= _MAX_RETRIES:
            return response

        retry_headers = getattr(response, "headers", None) or {}
        delay = _parse_retry_after(retry_headers.get("Retry-After"))
        if delay is None:
            delay = min(float(2 ** attempt), _MAX_RETRY_AFTER_SECONDS)
        close_fn = getattr(response, "close", None)
        if callable(close_fn):
            close_fn()
        if delay > 0:
            _sleep(delay)

    raise CourrierGraphError("Échec réseau Graph Courrier (tentatives épuisées).")


def _build_headers(
    token: str,
    *,
    immutable_id: bool = True,
    prefer: tuple[str, ...] = (),
) -> dict[str, str]:
    """Build Authorization and Prefer headers for Graph requests."""
    headers = {"Authorization": f"Bearer {token}"}
    prefs: list[str] = []
    if immutable_id:
        prefs.append('IdType="ImmutableId"')
    for item in prefer:
        cleaned = str(item or "").strip()
        if cleaned and cleaned not in prefs:
            prefs.append(cleaned)
    if prefs:
        headers["Prefer"] = ", ".join(prefs)
    return headers


def jeton_courrier() -> TokenInfo:
    """Return a cached Courrier application token (client credentials)."""
    global _cached_info, _token_monotonic_deadline

    if not Config.courrier_configured():
        raise CourrierNotConfigured("Le module Courrier n'est pas configuré.")

    with _token_lock:
        if _cached_info is not None and time.monotonic() < _token_monotonic_deadline:
            return _cached_info

        response = _request_with_retry(
            "POST",
            _TOKEN_URL.format(tenant=Config.GRAPH_TENANT_ID),
            headers={},
            data={
                "grant_type": "client_credentials",
                "client_id": Config.COURRIER_GRAPH_CLIENT_ID,
                "client_secret": Config.COURRIER_GRAPH_CLIENT_SECRET,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        if response.status_code != 200:
            raise CourrierGraphError(
                f"Échec du jeton Graph Courrier (HTTP {response.status_code}).",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CourrierGraphError(
                "Réponse JSON du jeton Graph Courrier invalide.",
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise CourrierGraphError(
                "Réponse du jeton Graph Courrier invalide.",
                status_code=response.status_code,
            )

        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise CourrierGraphError(
                "Réponse du point de jeton Graph Courrier sans access_token.",
                status_code=response.status_code,
            )

        try:
            expires_in = float(payload.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600.0
        if not math.isfinite(expires_in) or expires_in < 0:
            expires_in = 0.0

        now_utc = datetime.now(timezone.utc)
        claims = _decode_jwt_claims(token)
        raw_roles = claims.get("roles")
        roles = (
            tuple(sorted({str(r) for r in raw_roles if isinstance(r, str) and r.strip()}))
            if isinstance(raw_roles, list)
            else ()
        )
        raw_exp = claims.get("exp")
        expires_at_utc: Optional[datetime] = None
        if (
            isinstance(raw_exp, (int, float))
            and not isinstance(raw_exp, bool)
            and math.isfinite(raw_exp)
        ):
            try:
                expires_at_utc = datetime.fromtimestamp(raw_exp, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                expires_at_utc = None
        if expires_at_utc is None:
            expires_at_utc = now_utc + timedelta(seconds=expires_in)

        info = TokenInfo(
            access_token=token,
            expires_at_utc=expires_at_utc,
            roles=roles,
        )
        _cached_info = info
        _token_monotonic_deadline = time.monotonic() + max(
            expires_in - _TOKEN_MARGIN_SECONDS, 0.0
        )
        return info


def _resolve_graph_url(path: str) -> str:
    """Resolve a Graph-relative path or verify a Graph HTTPS URL (SSRF guard)."""
    if path.startswith("https://") or path.startswith("http://"):
        if not path.startswith(_GRAPH_ORIGIN_PREFIX):
            raise CourrierGraphError("URL Graph Courrier refusée.")
        return path
    normalized = path if path.startswith("/") else "/" + path
    return _GRAPH_BASE + normalized


def courrier_get(
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    immutable_id: bool = True,
    prefer: tuple[str, ...] = (),
    follow_next: bool = True,
) -> dict[str, Any]:
    """GET a Microsoft Graph resource for Courrier, merging paged collections."""
    url = _resolve_graph_url(path)

    merged: Optional[dict[str, Any]] = None
    while url:
        if not url.startswith(_GRAPH_ORIGIN_PREFIX):
            raise CourrierGraphError("URL Graph Courrier refusée.")
        info = jeton_courrier()
        headers = _build_headers(
            info.access_token, immutable_id=immutable_id, prefer=prefer
        )
        response = _request_with_retry(
            "GET",
            url,
            headers=headers,
            params=params if merged is None else None,
        )
        if response.status_code != 200:
            raise CourrierGraphError(
                f"Échec d'un GET Graph Courrier (HTTP {response.status_code}).",
                status_code=response.status_code,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise CourrierGraphError(
                "Réponse JSON Graph Courrier invalide.",
                status_code=response.status_code,
            ) from exc
        if not isinstance(data, dict):
            raise CourrierGraphError(
                "Réponse JSON Graph Courrier invalide.",
                status_code=response.status_code,
            )
        if "value" not in data:
            return data
        if not isinstance(data["value"], list):
            raise CourrierGraphError(
                "Réponse JSON Graph Courrier invalide.",
                status_code=response.status_code,
            )
        if merged is None:
            merged = data
        else:
            merged["value"].extend(data["value"])
        url = (data.get("@odata.nextLink") or "") if follow_next else ""

    if merged is None:
        raise CourrierGraphError("Réponse Graph Courrier vide.")
    merged.pop("@odata.nextLink", None)
    return merged


def courrier_get_bytes(
    path: str,
    *,
    max_bytes: int,
    immutable_id: bool = True,
) -> bytes:
    """Stream a binary Graph resource into memory up to ``max_bytes``."""
    if max_bytes < 0:
        raise ValueError("max_bytes doit être positif ou nul.")
    url = _resolve_graph_url(path)
    info = jeton_courrier()
    headers = _build_headers(info.access_token, immutable_id=immutable_id)
    response: Optional[requests.Response] = None
    try:
        response = _request_with_retry("GET", url, headers=headers, stream=True)
        if response.status_code != 200:
            raise CourrierGraphError(
                f"Échec d'un GET binaire Graph Courrier (HTTP {response.status_code}).",
                status_code=response.status_code,
            )
        resp_headers = getattr(response, "headers", None) or {}
        raw_len = resp_headers.get("Content-Length")
        if raw_len is not None and str(raw_len).strip():
            try:
                declared_len = int(str(raw_len).strip())
            except ValueError:
                declared_len = None
            if declared_len is not None and declared_len > max_bytes:
                raise CourrierGraphError(
                    "Pièce Graph Courrier au-delà du plafond d'aperçu.",
                    reason="too_large",
                )
        buf = bytearray()
        for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK_BYTES):
            if not chunk:
                continue
            if len(buf) + len(chunk) > max_bytes:
                raise CourrierGraphError(
                    "Pièce Graph Courrier au-delà du plafond d'aperçu.",
                    reason="too_large",
                )
            buf.extend(chunk)
        return bytes(buf)
    finally:
        if response is not None:
            close_fn = getattr(response, "close", None)
            if callable(close_fn):
                close_fn()


@contextmanager
def courrier_ouvrir_flux(
    path: str,
    *,
    max_bytes: int,
    immutable_id: bool = True,
) -> Iterator[Iterator[bytes]]:
    """Open a streaming binary Graph GET resource yielding chunks up to ``max_bytes``."""
    if max_bytes <= 0:
        raise ValueError("max_bytes doit être positif.")
    url = _resolve_graph_url(path)
    info = jeton_courrier()
    headers = _build_headers(info.access_token, immutable_id=immutable_id)
    response: Optional[requests.Response] = None
    try:
        response = _request_with_retry("GET", url, headers=headers, stream=True)
        if response.status_code != 200:
            raise CourrierGraphError(
                f"Échec du flux Graph Courrier (HTTP {response.status_code}).",
                status_code=response.status_code,
            )
        resp_headers = getattr(response, "headers", None) or {}
        raw_len = resp_headers.get("Content-Length")
        if raw_len is not None and str(raw_len).strip():
            try:
                declared_len = int(str(raw_len).strip())
            except ValueError:
                declared_len = None
            if declared_len is not None and declared_len > max_bytes:
                raise CourrierGraphError(
                    "Fichier Graph Courrier au-delà du plafond autorisé.",
                    reason="too_large",
                )

        active_response = response

        def _chunks() -> Iterator[bytes]:
            total = 0
            for chunk in active_response.iter_content(chunk_size=_DOWNLOAD_CHUNK_BYTES):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise CourrierGraphError(
                        "Fichier Graph Courrier au-delà du plafond autorisé.",
                        reason="too_large",
                    )
                yield chunk

        yield _chunks()
    finally:
        if response is not None:
            close_fn = getattr(response, "close", None)
            if callable(close_fn):
                close_fn()


def _parse_json_object(response: requests.Response, label: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise CourrierGraphError(
            f"Réponse JSON {label} invalide.",
            status_code=response.status_code,
        ) from exc
    if not isinstance(payload, dict):
        raise CourrierGraphError(
            f"Réponse JSON {label} invalide.",
            status_code=response.status_code,
        )
    return payload


def _validate_upload_url(upload_url: str) -> str:
    cleaned = str(upload_url or "").strip()
    parts = urlsplit(cleaned)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        raise CourrierGraphError("URL de téléversement OneDrive invalide.")
    if host == "graph.microsoft.com" or host.endswith(_UPLOAD_HOST_SUFFIXES):
        return cleaned
    raise CourrierGraphError("Hôte de téléversement OneDrive refusé.")


def courrier_lire_element_onedrive(
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """GET a OneDrive item, returning ``None`` cleanly on HTTP 404."""
    url = _resolve_graph_url(path)
    info = jeton_courrier()
    headers = _build_headers(info.access_token, immutable_id=False)
    response = _request_with_retry("GET", url, headers=headers, params=params)
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise CourrierGraphError(
            f"Échec de lecture OneDrive (HTTP {response.status_code}).",
            status_code=response.status_code,
        )
    return _parse_json_object(response, "OneDrive")


def courrier_creer_dossier_onedrive(
    parent_item_id: str,
    nom_dossier: str,
    *,
    encoded_upn: str,
) -> tuple[dict[str, Any], bool]:
    """Create a OneDrive child folder with ``conflictBehavior=fail`` (idempotent on existing folder)."""
    encoded_parent = quote(parent_item_id, safe="")
    url = _resolve_graph_url(f"/users/{encoded_upn}/drive/items/{encoded_parent}/children")
    info = jeton_courrier()
    headers = _build_headers(info.access_token, immutable_id=False)
    response = _request_with_retry(
        "POST",
        url,
        headers=headers,
        json_body={
            "name": nom_dossier,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        },
    )
    if response.status_code in (200, 201):
        return _parse_json_object(response, "OneDrive"), True
    if response.status_code == 409:
        encoded_child = quote(nom_dossier, safe="")
        existing = courrier_lire_element_onedrive(
            f"/users/{encoded_upn}/drive/items/{encoded_parent}:/{encoded_child}",
            params={"$select": "id,name,folder"},
        )
        if existing is not None and isinstance(existing.get("folder"), dict):
            return existing, False
        raise CourrierGraphError(
            "Conflit OneDrive : un fichier porte déjà ce nom de dossier.",
            status_code=409,
            reason="conflict",
        )
    raise CourrierGraphError(
        f"Échec de création du dossier OneDrive (HTTP {response.status_code}).",
        status_code=response.status_code,
    )


def courrier_deposer_petit_fichier_onedrive(
    parent_item_id: str,
    nom_fichier: str,
    contenu: bytes,
    *,
    encoded_upn: str,
) -> Optional[dict[str, Any]]:
    """Upload a <= 4 MiB file into OneDrive with ``conflictBehavior=fail`` (None on 409)."""
    encoded_parent = quote(parent_item_id, safe="")
    encoded_name = quote(nom_fichier, safe="")
    url = _resolve_graph_url(
        f"/users/{encoded_upn}/drive/items/{encoded_parent}:/{encoded_name}:/content"
    )
    info = jeton_courrier()
    headers = _build_headers(info.access_token, immutable_id=False)
    headers["Content-Type"] = "application/octet-stream"
    response = _request_with_retry(
        "PUT",
        url,
        headers=headers,
        params={"@microsoft.graph.conflictBehavior": "fail"},
        raw_bytes=contenu,
    )
    if response.status_code == 409:
        return None
    if response.status_code not in (200, 201):
        raise CourrierGraphError(
            f"Échec du dépôt OneDrive (HTTP {response.status_code}).",
            status_code=response.status_code,
        )
    return _parse_json_object(response, "OneDrive")


def courrier_ouvrir_session_onedrive(
    parent_item_id: str,
    nom_fichier: str,
    *,
    encoded_upn: str,
) -> Optional[str]:
    """Open a resumable OneDrive upload session (``conflictBehavior=fail``; None on 409)."""
    encoded_parent = quote(parent_item_id, safe="")
    encoded_name = quote(nom_fichier, safe="")
    url = _resolve_graph_url(
        f"/users/{encoded_upn}/drive/items/{encoded_parent}:/{encoded_name}:/createUploadSession"
    )
    info = jeton_courrier()
    headers = _build_headers(info.access_token, immutable_id=False)
    response = _request_with_retry(
        "POST",
        url,
        headers=headers,
        json_body={
            "item": {
                "@microsoft.graph.conflictBehavior": "fail",
                "name": nom_fichier,
            }
        },
    )
    if response.status_code == 409:
        return None
    if response.status_code not in (200, 201):
        raise CourrierGraphError(
            f"Échec de session OneDrive (HTTP {response.status_code}).",
            status_code=response.status_code,
        )
    payload = _parse_json_object(response, "session OneDrive")
    return _validate_upload_url(str(payload.get("uploadUrl") or ""))


def courrier_televerser_tranche_onedrive(
    upload_url: str,
    chunk: bytes,
    *,
    start: int,
    total: int,
) -> Optional[dict[str, Any]]:
    """PUT one chunk to a pre-authenticated OneDrive upload session URL."""
    validated_url = _validate_upload_url(upload_url)
    if not chunk:
        raise ValueError("La tranche de téléversement ne peut pas être vide.")
    end = start + len(chunk) - 1
    headers = {
        "Content-Length": str(len(chunk)),
        "Content-Range": f"bytes {start}-{end}/{total}",
    }
    response = _request_with_retry(
        "PUT",
        validated_url,
        headers=headers,
        raw_bytes=chunk,
    )
    if response.status_code == 202:
        return None
    if response.status_code in (200, 201):
        return _parse_json_object(response, "finalisation OneDrive")
    if response.status_code == 409:
        raise CourrierGraphError(
            "Conflit OneDrive à la finalisation de la session.",
            status_code=409,
            reason="conflict",
        )
    raise CourrierGraphError(
        f"Échec d'une tranche OneDrive (HTTP {response.status_code}).",
        status_code=response.status_code,
    )


def courrier_lire_courriel(
    msg_id: str,
    *,
    encoded_upn: str,
    select: str = "id,internetMessageId,parentFolderId,categories,isRead",
) -> Optional[dict[str, Any]]:
    """GET one Outlook message with ImmutableId, returning ``None`` cleanly on 404."""
    encoded_msg_id = quote(msg_id, safe="")
    url = _resolve_graph_url(f"/users/{encoded_upn}/messages/{encoded_msg_id}")
    info = jeton_courrier()
    headers = _build_headers(info.access_token, immutable_id=True)
    response = _request_with_retry(
        "GET",
        url,
        headers=headers,
        params={"$select": select},
    )
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise CourrierGraphError(
            f"Échec de lecture du courriel Outlook (HTTP {response.status_code}).",
            status_code=response.status_code,
        )
    return _parse_json_object(response, "courriel Outlook")


def courrier_assurer_categories_maitres(
    *,
    encoded_upn: str,
    noms_couleurs: tuple[tuple[str, str], ...] = (
        (CATEGORIE_CLASSE, COULEUR_CATEGORIE_CLASSE),
        (CATEGORIE_A_CLASSER, COULEUR_CATEGORIE_A_CLASSER),
    ),
) -> bool:
    """Ensure Outlook master categories exist; returns False on HTTP 403."""
    url = _resolve_graph_url(f"/users/{encoded_upn}/outlook/masterCategories")
    info = jeton_courrier()
    headers = _build_headers(info.access_token, immutable_id=False)
    response = _request_with_retry("GET", url, headers=headers)
    if response.status_code == 403:
        return False
    if response.status_code != 200:
        raise CourrierGraphError(
            f"Échec de lecture des catégories maîtres Outlook (HTTP {response.status_code}).",
            status_code=response.status_code,
        )
    payload = _parse_json_object(response, "catégories maîtres Outlook")
    raw_items = payload.get("value")
    if not isinstance(raw_items, list):
        raise CourrierGraphError(
            "Réponse JSON catégories maîtres Outlook invalide.",
            status_code=response.status_code,
        )
    existing = {
        str(item.get("displayName") or "").strip().casefold()
        for item in raw_items
        if isinstance(item, dict)
    }
    for nom, couleur in noms_couleurs:
        if nom.strip().casefold() in existing:
            continue
        post_info = jeton_courrier()
        post_headers = _build_headers(post_info.access_token, immutable_id=False)
        post_resp = _request_with_retry(
            "POST",
            url,
            headers=post_headers,
            json_body={"displayName": nom, "color": couleur},
        )
        if post_resp.status_code == 403:
            return False
        if post_resp.status_code not in (200, 201, 409):
            raise CourrierGraphError(
                f"Échec de création d'une catégorie maître Outlook (HTTP {post_resp.status_code}).",
                status_code=post_resp.status_code,
            )
    return True


def courrier_modifier_categories_courriel(
    msg_id: str,
    categories: list[str],
    *,
    encoded_upn: str,
) -> dict[str, Any]:
    """PATCH only ``categories`` on an Outlook message (preserves ``isRead``)."""
    encoded_msg_id = quote(msg_id, safe="")
    url = _resolve_graph_url(f"/users/{encoded_upn}/messages/{encoded_msg_id}")
    info = jeton_courrier()
    headers = _build_headers(info.access_token, immutable_id=True)
    response = _request_with_retry(
        "PATCH",
        url,
        headers=headers,
        json_body={"categories": list(categories)},
    )
    if response.status_code != 200:
        raise CourrierGraphError(
            f"Échec de mise à jour des catégories Outlook (HTTP {response.status_code}).",
            status_code=response.status_code,
        )
    return _parse_json_object(response, "catégories Outlook")


def courrier_creer_dossier_outlook(
    parent_folder_id: str,
    nom_dossier: str,
    *,
    encoded_upn: str,
) -> tuple[dict[str, Any], bool]:
    """Create a child mailFolder under ``Archive > Dossiers > {statut}`` (idempotent on 409)."""
    encoded_parent = quote(parent_folder_id, safe="")
    url = _resolve_graph_url(
        f"/users/{encoded_upn}/mailFolders/{encoded_parent}/childFolders"
    )
    info = jeton_courrier()
    headers = _build_headers(info.access_token, immutable_id=True)
    response = _request_with_retry(
        "POST",
        url,
        headers=headers,
        json_body={"displayName": nom_dossier},
    )
    if response.status_code in (200, 201):
        return _parse_json_object(response, "dossier Outlook"), True
    if response.status_code == 409:
        children_res = courrier_get(
            f"/users/{encoded_upn}/mailFolders/{encoded_parent}/childFolders",
            params={"$select": "id,displayName", "$top": 250},
            immutable_id=True,
        )
        needle = nom_dossier.strip().casefold()
        for item in children_res.get("value") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("displayName") or "").strip().casefold() == needle:
                return item, False
        raise CourrierGraphError(
            "Conflit lors de la création du dossier Outlook.",
            status_code=409,
            reason="conflict",
        )
    raise CourrierGraphError(
        f"Échec de création du dossier Outlook (HTTP {response.status_code}).",
        status_code=response.status_code,
    )


def courrier_deplacer_courriel(
    msg_id: str,
    destination_folder_id: str,
    *,
    encoded_upn: str,
) -> dict[str, Any]:
    """Move an Outlook message with ImmutableId into ``destination_folder_id``."""
    encoded_msg_id = quote(msg_id, safe="")
    url = _resolve_graph_url(f"/users/{encoded_upn}/messages/{encoded_msg_id}/move")
    info = jeton_courrier()
    headers = _build_headers(info.access_token, immutable_id=True)
    response = _request_with_retry(
        "POST",
        url,
        headers=headers,
        json_body={"destinationId": destination_folder_id},
    )
    if response.status_code not in (200, 201):
        raise CourrierGraphError(
            f"Échec du déplacement Outlook (HTTP {response.status_code}).",
            status_code=response.status_code,
        )
    return _parse_json_object(response, "déplacement Outlook")


def format_graph_error_fr(exc: CourrierGraphError) -> str:
    """Format a Graph error using status_code only (CodeQL-safe, zero-PII)."""
    if exc.status_code is not None:
        return f"Échec Microsoft Graph (HTTP {exc.status_code})."
    return "Échec de communication avec Microsoft Graph."


_probe_error_fr = format_graph_error_fr


@lru_cache(maxsize=1)
def detect_execution_region() -> str:
    """Return execution region ('northamerica-northeast1' on GAE, 'local' in dev)."""
    if os.environ.get("GAE_ENV") != "standard":
        return "local"
    try:
        response = _session.get(
            _METADATA_REGION_URL,
            headers={"Metadata-Flavor": "Google"},
            timeout=2,
        )
        if response.status_code == 200 and response.text:
            region = response.text.strip().rsplit("/", 1)[-1]
            return region or "unknown"
    except requests.RequestException:
        pass
    return "unknown"


def verifier_connexion() -> dict[str, Any]:
    """Run read-only Courrier connection, token, scope, and root-folder probes."""
    interrupteur_actif = bool(Config.COURRIER_ACTIF)
    configure = Config.courrier_configured()
    compte = Config.COURRIER_UPN or ""
    racine_onedrive = Config.COURRIER_ONEDRIVE_ROOT or ""
    region = detect_execution_region()

    result: dict[str, Any] = {
        "interrupteur_actif": interrupteur_actif,
        "configure": configure,
        "compte": compte,
        "racine_onedrive": racine_onedrive,
        "region": region,
        "mode_auth": "application",
        "jeton_valide": False,
        "expiration_utc": None,
        "reconsentement_requis": False,
        "portees": [],
        "envoi_absent": True,
        "boite_accessible": False,
        "onedrive_accessible": False,
        "erreur": None,
    }
    if not configure:
        result["erreur"] = "Le module Courrier n'est pas configuré."
        return result

    try:
        info = jeton_courrier()
    except CourrierGraphError as exc:
        result["reconsentement_requis"] = exc.status_code in (400, 401, 403)
        result["erreur"] = _probe_error_fr(exc)
        return result

    result["jeton_valide"] = True
    result["expiration_utc"] = info.expires_at_utc
    result["portees"] = list(info.roles)
    envoi_absent = "Mail.Send" not in info.roles
    result["envoi_absent"] = envoi_absent
    if not envoi_absent:
        result["erreur"] = (
            "Permission interdite détectée sur le jeton Courrier : "
            "Mail.Send doit être retirée."
        )
        return result

    encoded_upn = quote(compte, safe="")
    probe_errors: list[str] = []

    try:
        courrier_get(
            f"/users/{encoded_upn}/mailFolders/Inbox",
            params={"$select": "id,totalItemCount"},
            immutable_id=True,
        )
        result["boite_accessible"] = True
    except CourrierGraphError as exc:
        if exc.status_code in (401, 403):
            result["reconsentement_requis"] = True
        probe_errors.append(f"Boîte de réception : {_probe_error_fr(exc)}")

    encoded_root = quote(racine_onedrive, safe="/")
    try:
        drive_data = courrier_get(
            f"/users/{encoded_upn}/drive/root:/{encoded_root}",
            params={"$select": "id,folder"},
            immutable_id=False,
        )
        if isinstance(drive_data.get("folder"), dict):
            result["onedrive_accessible"] = True
        else:
            probe_errors.append("OneDrive : le chemin racine n'est pas un dossier.")
    except CourrierGraphError as exc:
        if exc.status_code in (401, 403):
            result["reconsentement_requis"] = True
        probe_errors.append(f"OneDrive : {_probe_error_fr(exc)}")

    if probe_errors:
        result["erreur"] = " ".join(probe_errors)
    return result
